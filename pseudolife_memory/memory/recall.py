"""MemCoT-style iterative retrieval loop — live, read-only.

Promoted from the measurement harness (evals/memcot_bench.py). Pure
orchestration over injected callables (search_fn, graph_fn, entity vocab) so it
unit-tests without a daemon or DB. See
docs/specs/2026-06-23-memcot-live-wiring-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol


def _mentions(text: str, name: str) -> bool:
    """Word-boundary, case-insensitive membership (hyphens are boundaries, so
    'k8s' does not match 'k8s-prod'). Canonical package copy of the bench's
    value_present."""
    if not text or not name:
        return False
    return re.search(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])",
                     text, re.IGNORECASE) is not None


@dataclass
class RecallState:
    seeds: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    entity_facts: dict[str, list[dict]] = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    iterations: int = 0
    low_confidence: bool = False


class RecallController(Protocol):
    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]: ...
    def next_queries(self, query: str, newly: list[str]) -> list[str]: ...


class MechanicalController:
    """Deterministic: seeds = vocab entities word-present in query+hits; re-query
    each newly discovered entity by name."""

    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]:
        blob = query + " " + " ".join(hits)
        return [name for name in vocab if _mentions(blob, name)]

    def next_queries(self, query: str, newly: list[str]) -> list[str]:
        return [f"{query} {name}" for name in newly]


def _add_edge(state: RecallState, ed: dict) -> None:
    key = (ed.get("src"), ed.get("relation"), ed.get("dst"))
    for e in state.edges:
        if (e["src"], e["relation"], e["dst"]) == key:
            return
    state.edges.append({"src": ed.get("src"), "relation": ed.get("relation"),
                        "dst": ed.get("dst"), "derived": ed.get("derived", False)})


def run_recall(search_fn: Callable, graph_fn: Callable, vocab: list[str],
               query: str, controller: RecallController, *,
               hops: int = 3, top_k: int = 5,
               max_entities: int = 50) -> RecallState:
    """Iterative search(+graph) loop. Depth-1 graph expansion per iteration."""
    state = RecallState()
    hits = [e.get("text", "") for e in search_fn(query, top_k).get("entries", [])]
    for t in hits:
        if t and t not in state.texts:
            state.texts.append(t)
    seeds = controller.seed_entities(query, hits, vocab)
    if not seeds:
        state.low_confidence = True
        return state
    state.seeds = list(dict.fromkeys(seeds))
    seen: set[str] = set(state.seeds)
    # Respect max_entities even for seeds
    if len(state.seeds) > max_entities:
        state.seeds = state.seeds[:max_entities]
        seen = set(state.seeds)
    state.entities.extend(state.seeds)
    frontier = list(state.seeds)
    while frontier and state.iterations < hops and len(seen) < max_entities:
        state.iterations += 1
        newly: list[str] = []
        for name in frontier:
            nb = graph_fn(name, 1)
            if not nb.get("found"):
                continue
            for node in nb.get("nodes", []):
                en = node.get("entity", "")
                if not en:
                    continue
                if en not in state.entity_facts:
                    state.entity_facts[en] = node.get("facts", [])
                if en not in seen:
                    seen.add(en)
                    newly.append(en)
                    state.entities.append(en)
            for ed in nb.get("edges", []):
                _add_edge(state, ed)
            for p in nb.get("paths", []):
                if p not in state.paths:
                    state.paths.append(p)
            if len(seen) >= max_entities:
                break
        for q in controller.next_queries(query, newly):
            for e in search_fn(q, top_k).get("entries", []):
                t = e.get("text", "")
                if t and t not in state.texts:
                    state.texts.append(t)
        frontier = newly
    return state
