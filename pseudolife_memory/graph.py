"""Graph logic — normalization, ontology-lite validation, on-read inference.

Pure functions over plain data (no storage, no torch) so the weak-model
guardrails (spec §5.3) are testable without Postgres:

* :func:`norm_name` — deterministic normalization (lowercase, separators
  folded to ``-``) so weak-model variants (``depends_on`` / ``Depends On``)
  resolve to the registry name without ever erroring.
* :func:`resolve_relation` — closed-vocabulary check with top-3 fuzzy
  suggestions on miss ("did you mean 'depends-on'?").
* :func:`derive_edges` — transitive closure + inverse mirroring computed
  at query time. Derived edges return marked ``derived: True`` with rule
  provenance (``via: ["transitive:depends-on"]``) so a weak model receives
  complete multi-hop conclusions as plain facts — the server does the
  reasoning, the model does the reading. No stored materialization, no
  invalidation problem: the graph is small enough to derive on read.
* :func:`build_subgraph` — depth-capped neighborhood (≤ 3 hops) +
  optional path between two named entities, via NetworkX.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Hashable

import networkx as nx

MAX_DEPTH = 3

_SEP_RE = re.compile(r"[\s_/\\.:]+")


def norm_name(s: str) -> str:
    """Normalize an entity / relation name: lowercase, separators → ``-``."""
    s = (s or "").strip().lower()
    s = _SEP_RE.sub("-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def resolve_relation(
    known: list[str], raw: str,
) -> tuple[str | None, list[str]]:
    """Resolve ``raw`` against the relation registry.

    Returns ``(name, [])`` when the normalized form is registered, else
    ``(None, suggestions)`` with the top-3 fuzzy matches — validation
    with suggestion, not silent failure.
    """
    n = norm_name(raw)
    if n in known:
        return n, []
    return None, difflib.get_close_matches(n, known, n=3, cutoff=0.5)


def derive_edges(
    edges: list[dict[str, Any]],
    relations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute derived edges: transitive closure + inverse mirrors.

    ``edges`` rows need ``src`` / ``relation`` / ``dst`` (nodes are any
    hashable). ``relations`` maps name → ``{"transitive": bool,
    "inverse_of": str|None}``. The inverse pairing applies in both
    directions regardless of which side declared it, and mirrors
    transitive-derived edges too (A runs-on B, B part-of C ⇒ C hosts A
    is out of scope — inversion never crosses relations).
    """
    base = {(e["src"], e["relation"], e["dst"]) for e in edges}
    derived: dict[tuple, list[str]] = {}

    for name, meta in relations.items():
        if not meta.get("transitive"):
            continue
        g = nx.DiGraph()
        g.add_edges_from(
            (e["src"], e["dst"]) for e in edges if e["relation"] == name
        )
        if not g:
            continue
        closure = nx.transitive_closure(g, reflexive=False)
        for u, v in closure.edges():
            key = (u, name, v)
            if key not in base and u != v:
                derived.setdefault(key, []).append(f"transitive:{name}")

    inverse: dict[str, str] = {}
    for name, meta in relations.items():
        other = meta.get("inverse_of")
        if other:
            inverse[name] = other
            inverse.setdefault(other, name)
    if inverse:
        for (u, r, v) in list(base) + list(derived):
            mirror = inverse.get(r)
            if mirror is None:
                continue
            key = (v, mirror, u)
            if key not in base and key not in derived:
                derived.setdefault(key, []).append(f"inverse:{r}")

    return [
        {"src": s, "relation": r, "dst": d, "derived": True, "via": via}
        for (s, r, d), via in derived.items()
    ]


def build_subgraph(
    edges: list[dict[str, Any]],
    relations: dict[str, dict[str, Any]],
    root: Hashable,
    depth: int = 1,
    to: Hashable | None = None,
) -> dict[str, Any]:
    """Neighborhood of ``root`` within ``depth`` hops (clamped to 3).

    Hop counting treats edges as bidirectional (you can see what points
    AT an entity, not only what it points at); the returned edges keep
    their direction. Derived (inferred) edges count as hops too — a
    weak model asking depth=1 still sees the full multi-hop conclusion.

    When ``to`` is given, the shortest undirected path root→to is
    returned under ``paths`` and its nodes are folded into the
    neighborhood even when beyond ``depth``.
    """
    depth = max(1, min(int(depth), MAX_DEPTH))
    all_edges = [
        {**e, "derived": False, "via": []} for e in edges
    ] + derive_edges(edges, relations)

    undirected = nx.Graph()
    undirected.add_node(root)
    for e in all_edges:
        undirected.add_edge(e["src"], e["dst"])

    nodes = set(
        nx.single_source_shortest_path_length(
            undirected, root, cutoff=depth,
        )
    )

    paths: list[list[Hashable]] = []
    if to is not None and to in undirected and nx.has_path(undirected, root, to):
        path = nx.shortest_path(undirected, root, to)
        paths = [path]
        nodes |= set(path)

    kept = [e for e in all_edges if e["src"] in nodes and e["dst"] in nodes]
    return {"nodes": nodes, "edges": kept, "paths": paths}
