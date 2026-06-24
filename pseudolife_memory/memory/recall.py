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
import urllib.request  # noqa: E402
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


def _add_edge(state: RecallState, ed: dict) -> None:
    key = (ed.get("src"), ed.get("relation"), ed.get("dst"))
    for e in state.edges:
        if (e["src"], e["relation"], e["dst"]) == key:
            return
    state.edges.append({"src": ed.get("src"), "relation": ed.get("relation"),
                        "dst": ed.get("dst"), "derived": ed.get("derived", False)})


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
    others.sort(key=lambda n: ((degree_fn(n) or 0), n))
    if expand_budget:
        others = others[:expand_budget]
    return seeds + others


def _hub_threshold(degrees, percentile: float, floor: int) -> int:
    """max(floor, p-th percentile of the degree distribution)."""
    vals = sorted(degrees)
    if not vals:
        return floor
    idx = min(len(vals) - 1, int(len(vals) * percentile / 100.0))
    return max(floor, vals[idx])


def run_recall(search_fn: Callable, graph_fn: Callable, vocab: list[str],
               query: str, controller: RecallController, *,
               hops: int = 3, top_k: int = 5,
               max_entities: int = 50,
               degree_fn: Callable[[str], int] | None = None,
               hub_threshold: int | None = None,
               expand_budget: int | None = None) -> RecallState:
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
    seed_set = set(state.seeds)
    frontier = list(state.seeds)
    while frontier and state.iterations < hops and len(seen) < max_entities:
        state.iterations += 1
        newly: list[str] = []
        for name in _select_frontier(frontier, seed_set, degree_fn,
                                      hub_threshold, expand_budget):
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
