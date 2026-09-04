"""MemCoT-style iterative retrieval loop — live, read-only.

Promoted from the measurement harness (evals/memcot_bench.py). Pure
orchestration over injected callables (search_fn, graph_fn, entity vocab) so it
unit-tests without a daemon or DB. See
docs/specs/2026-06-23-memcot-live-wiring-design.md.
"""
from __future__ import annotations

import json  # noqa: E402
import os  # noqa: E402
import re
import time
import urllib.request  # noqa: E402
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


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
    # Discovery hop per entity (0 = seed, 1..hops = the iteration it first
    # appeared in). Emission order alone doesn't carry this — entities within
    # one hop arrive in _select_frontier's ascending-degree EXPANSION order,
    # not a relevance order — so callers that want to preserve deep bridging
    # hops under a size cap (issue #186) need the hop tag, not just position.
    entity_hop: dict[str, int] = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)
    # How many of the leading entries in ``texts`` came from the flat SEED
    # search (before any hop ran) vs. from hop-driven re-queries — the two
    # phases have very different relevance characters and a caller capping
    # ``texts`` needs to keep both, not just a prefix of the seed batch.
    seed_text_count: int = 0
    edges: list[dict] = field(default_factory=list)
    # Discovery hop per edge, parallel to ``edges`` (same index each).
    edge_hop: list[int] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    iterations: int = 0
    low_confidence: bool = False
    # Cost telemetry: how many ``search_fn`` calls the walk issued (the
    # seed search included), and whether a hard ceiling — the total-search
    # cap or the time budget — cut the walk short. Always tracked in
    # process; serialized only when ``truncated`` (see
    # ``recall_state_to_dict``), so an untruncated response is unchanged.
    searches_issued: int = 0
    truncated: bool = False


class RecallController(Protocol):
    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]: ...
    def next_queries(self, query: str, newly: list[str]) -> list[str]: ...


class MechanicalController:
    """Deterministic: seeds = vocab entities word-present in the QUERY (hits only
    as fallback); re-query each newly discovered entity by name."""

    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]:
        # Query-first: the question names its subject(s); seed those only.
        # On a populous bank, co-mentioning search hits drag in unrelated
        # entities, so hit-derived matches are used ONLY as a fallback when the
        # query names no known entity. (Bench: precision 1.0 vs 0.262, zero
        # recall loss — intermediates are reached via the graph, not seeded.)
        q = [name for name in vocab if _mentions(query, name)]
        if q:
            return q
        return [name for name in vocab if _mentions(" ".join(hits), name)]

    def next_queries(self, query: str, newly: list[str]) -> list[str]:
        return [f"{query} {name}" for name in newly]


def _add_edge(state: RecallState, ed: dict, hop: int) -> None:
    key = (ed.get("src"), ed.get("relation"), ed.get("dst"))
    for e in state.edges:
        if (e["src"], e["relation"], e["dst"]) == key:
            return
    state.edges.append({"src": ed.get("src"), "relation": ed.get("relation"),
                        "dst": ed.get("dst"), "derived": ed.get("derived", False)})
    state.edge_hop.append(hop)


def _select_frontier(frontier: list[str], seed_set: set[str],
                     degree_fn: Callable[[str], int] | None,
                     hub_threshold: int | None,
                     expand_budget: int | None) -> list[str]:
    """Choose which frontier entities to expand THROUGH this hop.

    Seeds always expand (exempt from gate, ordering, and budget). For
    non-seeds: drop hubs (degree >= hub_threshold), order survivors by
    ascending degree with a (degree, name) tiebreak, then cap at
    expand_budget. When degree_fn is None the frontier is returned unchanged
    (gating off — byte-identical legacy behavior).
    """
    if degree_fn is None:
        return list(frontier)
    seeds = [n for n in frontier if n in seed_set]
    others = [n for n in frontier if n not in seed_set]
    if hub_threshold is not None:
        others = [n for n in others if (degree_fn(n) or 0) < hub_threshold]
    # Ascending degree is load-bearing, not cosmetic: under the caller's
    # max_entities cap it decides which non-seeds survive truncation.
    others.sort(key=lambda n: ((degree_fn(n) or 0), n))
    if expand_budget:  # 0 / None == no per-hop cap (per spec)
        others = others[:expand_budget]
    return seeds + others


def _hub_threshold(degrees, percentile: float, floor: int) -> int:
    """max(floor, p-th percentile of the degree distribution)."""
    vals = sorted(degrees)
    if not vals:
        return floor
    idx = min(len(vals) - 1, int(len(vals) * percentile / 100.0))
    return max(floor, vals[idx])


def _requery_order(newly: list[str], hits: list[str],
                   degree_fn: Callable[[str], int] | None) -> list[str]:
    """Rank newly discovered entities by how worthwhile a re-query is.

    (1) mention count in the SEED search hits, descending — an entity the
    question's own top hits already talk about is the one whose extra
    context is on topic; (2) degree ascending — a spoke's re-query is
    about that spoke, a hub's drags the corpus in (the same reasoning as
    ``_select_frontier``'s expansion order, applied to the SEARCH budget);
    (3) name, so the cut is deterministic.
    """
    counts = {n: sum(1 for t in hits if _mentions(t, n)) for n in newly}
    return sorted(newly, key=lambda n: (-counts[n],
                                        (degree_fn(n) or 0) if degree_fn else 0,
                                        n))


def run_recall(search_fn: Callable, graph_fn: Callable, vocab: list[str],
               query: str, controller: RecallController, *,
               hops: int = 3, top_k: int = 5,
               max_entities: int = 50,
               degree_fn: Callable[[str], int] | None = None,
               hub_threshold: int | None = None,
               expand_budget: int | None = None,
               max_searches_per_hop: int | None = None,
               max_total_searches: int | None = None,
               time_budget_seconds: float | None = None,
               skip_part_of_expansion: bool = False) -> RecallState:
    """Iterative search(+graph) loop. Depth-1 graph expansion per iteration.

    The four fan-out arguments bound the SEARCH cost (graph expansion is
    already bounded by ``hub_threshold`` / ``expand_budget`` /
    ``max_entities``). Each is off when falsy, and with all four off the
    walk is the pre-2026-09-04 one, entity for entity and byte for byte —
    ``max_searches_per_hop`` only reorders when it actually cuts, and
    ``skip_part_of_expansion`` only collects relation provenance when
    enabled (``tests/test_recall.py``'s byte-identity pin).

    ``max_searches_per_hop`` re-queries only the top-N newly discovered
    entities per hop (``_requery_order``); the rest are still returned as
    entities with their facts. ``max_total_searches`` is a hard ceiling
    over the whole call including the seed search, and
    ``time_budget_seconds`` a wall-clock one; either stops the walk and
    sets ``truncated``, returning what the walk has rather than raising.
    ``skip_part_of_expansion`` drops entities reached ONLY by ``part-of``
    edges from the re-query set (they are still returned).
    """
    state = RecallState()
    started = time.monotonic()
    # Normalise to "positive int/float, or None for off". A hand-edited
    # config can carry a negative, and a negative per-hop cap would slice
    # ``targets[:-1]`` — silently dropping one re-query instead of doing
    # nothing. The Console's own minimums are 0.
    max_searches_per_hop = max(0, int(max_searches_per_hop or 0)) or None
    max_total_searches = max(0, int(max_total_searches or 0)) or None
    time_budget_seconds = max(0.0, float(time_budget_seconds or 0.0)) or None

    def _out_of_time() -> bool:
        return (bool(time_budget_seconds)
                and (time.monotonic() - started) >= float(time_budget_seconds))

    def _search(q: str) -> dict:
        state.searches_issued += 1
        return search_fn(q, top_k)

    hits = [e.get("text", "") for e in _search(query).get("entries", [])]
    for t in hits:
        if t and t not in state.texts:
            state.texts.append(t)
    state.seed_text_count = len(state.texts)
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
    for s in state.seeds:
        state.entity_hop[s] = 0
    seed_set = set(state.seeds)
    frontier = list(state.seeds)
    while frontier and state.iterations < hops and len(seen) < max_entities:
        if _out_of_time():
            state.truncated = True
            break
        state.iterations += 1
        hop_num = state.iterations
        newly: list[str] = []
        # Relations that reached each entity this hop — only collected when
        # ``skip_part_of_expansion`` needs it, so the default path is
        # exactly the pre-change loop.
        rel_touch: dict[str, set[str]] = {}
        stopped = False
        for name in _select_frontier(frontier, seed_set, degree_fn,
                                      hub_threshold, expand_budget):
            # The budget is checked here too, not only at the search
            # boundaries: expanding through a 132-degree hub is one
            # ``graph_fn`` call, and a walk that blew its budget inside the
            # expansion loop would otherwise run the hop out first.
            if _out_of_time():
                state.truncated = True
                stopped = True
                break
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
                    state.entity_hop[en] = hop_num
            for ed in nb.get("edges", []):
                _add_edge(state, ed, hop_num)
                if skip_part_of_expansion:
                    # ONLY the edges incident to the node being expanded say
                    # how its neighbours were reached. ``graph_fn`` returns
                    # the INDUCED subgraph (graph.py's ``build_subgraph``
                    # keeps every edge whose endpoints are both in the
                    # neighborhood), so crediting both endpoints of every
                    # edge would let a neighbour-to-neighbour link disguise
                    # a containment-only arrival as a domain one.
                    src, dst = ed.get("src"), ed.get("dst")
                    if src == name and dst:
                        rel_touch.setdefault(dst, set()).add(ed.get("relation"))
                    elif dst == name and src:
                        rel_touch.setdefault(src, set()).add(ed.get("relation"))
            for p in nb.get("paths", []):
                if p not in state.paths:
                    state.paths.append(p)
            if len(seen) >= max_entities:
                break

        targets = newly
        if skip_part_of_expansion:
            # Reached ONLY by containment: reading its facts is free, but a
            # re-query on it is the corpus's filler relation buying a
            # search. (19.0% of the live bank copy's edges are ``part-of``,
            # and 1,046 of the 1,763 entities recall added across the
            # 2026-09-04 20-question run arrived via ``part-of`` alone —
            # evals/results/recall-fanout-cap-20260904.json.)
            targets = [n for n in targets
                       if rel_touch.get(n, set()) != {"part-of"}]
        if max_searches_per_hop and len(targets) > max_searches_per_hop:
            targets = _requery_order(
                targets, hits, degree_fn)[:max_searches_per_hop]

        for q in ([] if stopped else controller.next_queries(query, targets)):
            if (max_total_searches
                    and state.searches_issued >= int(max_total_searches)):
                state.truncated = True
                stopped = True
                break
            if _out_of_time():
                state.truncated = True
                stopped = True
                break
            for e in _search(q).get("entries", []):
                t = e.get("text", "")
                if t and t not in state.texts:
                    state.texts.append(t)
        if stopped:
            break
        frontier = newly
    return state


def recall_state_to_dict(state: RecallState, query: str, hops: int) -> dict[str, Any]:
    """Wrap a finished ``RecallState`` into the public ``recall()`` response
    shape. Shared by ``MemoryService.recall`` and ``evals/recall_cap_probe.py``
    so both build the response the same way. Includes the per-hop provenance
    (``entity_hop`` / ``edge_hop`` / ``seed_text_count``) the MCP layer's
    output caps rely on (issue #186) to keep deep hops and hop-discovered
    texts from being crowded out by a flat prefix cap."""
    out: dict[str, Any] = {
        "query": query,
        "seeds": state.seeds,
        "entities": [{"entity": n, "facts": state.entity_facts.get(n, [])}
                     for n in state.entities],
        "edges": state.edges,
        "paths": state.paths,
        "texts": state.texts,
        "iterations": state.iterations,
        "hops": hops,
        "low_confidence": state.low_confidence,
        "entity_hop": dict(state.entity_hop),
        "edge_hop": list(state.edge_hop),
        "seed_text_count": state.seed_text_count,
    }
    # Served only when a hard ceiling actually cut the walk short: a
    # complete walk's response stays byte-identical to the pre-cap one
    # (the served-absent-when-default convention). The flag asserts only
    # what it knows — some re-queries, and possibly deeper hops, were
    # skipped, so supporting texts and deeper entities may be missing. It
    # does NOT mean the returned entity/edge set is partial: a ceiling
    # tripping inside the re-query loop of the last permitted hop leaves
    # that hop's graph expansion already complete and cuts only ``texts``.
    if state.truncated:
        out["truncated"] = True
        out["searches_issued"] = state.searches_issued
    return out


def _parse_name_list(raw: str) -> list[str]:
    """Extract the first JSON array of strings from a model response."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        arr = json.loads(raw[start:end + 1])
    except Exception:
        return []
    return [x for x in arr if isinstance(x, str)]


def _seed_prompt(query: str, hits: list[str], vocab: list[str]) -> str:
    allowed = ", ".join(vocab[:200])
    context = " ".join(hits[:5])
    return (
        "You resolve which known entities a question is about. "
        "From the ALLOWED list only, return a JSON array of the entity names "
        "the question/context refers to (the subjects to look up). "
        "Return [] if none.\n\n"
        f"ALLOWED: {allowed}\n\nQUESTION: {query}\n\nCONTEXT: {context}\n\n"
        "JSON array:"
    )


class LLMController:
    """Real-but-minimal LLM driver: the model resolves seed entities; expansion
    is structural (graph) and re-query phrasing reuses the mechanical rule.
    ``complete`` is injected so this is unit-tested without a served model."""

    def __init__(self, complete: Callable[[str], str]):
        self._complete = complete

    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]:
        names = _parse_name_list(self._complete(_seed_prompt(query, hits, vocab)))
        vset = set(vocab)
        return [n for n in names if n in vset]

    def next_queries(self, query: str, newly: list[str]) -> list[str]:
        return [f"{query} {name}" for name in newly]


def simple_complete(dream_cfg, prompt: str) -> str:
    """Minimal OpenAI-compatible /chat/completions call using the dream
    extractor endpoint. Returns "" on any failure (caller treats as no seeds)."""
    try:
        base = (os.environ.get("PSEUDOLIFE_DREAM_BASE_URL")
                or dream_cfg.extractor_base_url)
        model = os.environ.get("PSEUDOLIFE_DREAM_MODEL") or dream_cfg.extractor_model
        if not base or not model:
            return ""
        key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or dream_cfg.extractor_api_key
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 256,
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                     data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""
