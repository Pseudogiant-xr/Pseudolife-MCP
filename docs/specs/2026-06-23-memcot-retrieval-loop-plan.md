# MemCoT Iterative Retrieval Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `evals/memcot_bench.py`, a dev-only harness that measures whether an iterative search→graph-expand→re-query loop lifts multi-hop recall over single-shot `search`.

**Architecture:** A pure loop engine (`run_loop`) driven by a swappable `Controller` (mechanical now, LLM seam later), run in three arms (baseline / loop-no-graph / loop+graph) over a small deterministic multi-hop corpus whose edges are seeded directly into the graph. Pure logic is unit-tested with a duck-typed `FakeService`; one PG-backed smoke test exercises the real `MemoryService`.

**Tech Stack:** Python 3.11, stdlib only in the engine; `pseudolife_memory.service.MemoryService`; pytest; Postgres+pgvector (bench DB only) via reused `ladder_sweep` helpers.

## Global Constraints

- Reuse `reset_bench`, `build_service`, `approx_tokens`, `value_present` from `evals/ladder_sweep.py` — do not duplicate them.
- Dedicated `pseudolife_memory_bench` DB only; the live `pseudolife_memory` bank is NEVER opened.
- CPU only (`CUDA_VISIBLE_DEVICES=-1`), set before any torch/service import; no served LLM; no network.
- Engine module-level imports are stdlib + `ladder_sweep` only (no torch at import) so unit tests import it without PG.
- Closed relation vocabulary only: `depends-on`, `runs-on`, `part-of`, `uses`, `stores-data-in` (plus `configures`, `related-to` available but unused here).
- Tests run with `HF_HUB_OFFLINE=1`; dev invokes `python -m pytest` from the project venv.
- Measure-first: no hard pass/fail threshold in code; report the curve.

---

### Task 1: Corpus + entity vocabulary + entity spotter

**Files:**
- Create: `evals/memcot_bench.py`
- Test: `tests/test_memcot_bench.py`

**Interfaces:**
- Produces: `CORPUS: list[dict]` (each: `snippet:str`, `edges:list[tuple[str,str,str]]`, optionally `question:str`/`gold:str`/`hops:int`), `DISTRACTORS: list[str]`, `KNOWN_ENTITIES: set[str]`, `QUESTIONS: list[dict]` (each `question`, `gold`, `hops`), `spot_entities(text: str, known: set[str]) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memcot_bench.py
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import memcot_bench as mb  # noqa: E402


def test_known_entities_cover_all_edge_endpoints():
    endpoints = set()
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            endpoints.add(src)
            endpoints.add(dst)
    assert endpoints <= mb.KNOWN_ENTITIES


def test_every_edge_uses_closed_vocab():
    allowed = {"depends-on", "runs-on", "part-of", "uses", "stores-data-in"}
    for rec in mb.CORPUS:
        for _src, rel, _dst in rec["edges"]:
            assert rel in allowed


def test_every_question_gold_is_reachable_within_its_hops():
    # BFS over the seeded edges (undirected) from any entity named in the
    # question; gold must be reachable within `hops` steps.
    adj: dict[str, set[str]] = {}
    for rec in mb.CORPUS:
        for src, _rel, dst in rec["edges"]:
            adj.setdefault(src, set()).add(dst)
            adj.setdefault(dst, set()).add(src)
    for q in mb.QUESTIONS:
        seeds = mb.spot_entities(q["question"], mb.KNOWN_ENTITIES)
        seen = set(seeds)
        frontier = set(seeds)
        for _ in range(q["hops"]):
            nxt = set()
            for e in frontier:
                nxt |= adj.get(e, set())
            seen |= nxt
            frontier = nxt
        assert q["gold"] in seen, q


def test_spot_entities_word_boundary():
    known = {"checkout-svc", "jvm-21"}
    assert mb.spot_entities("checkout-svc depends-on billing-lib", known) == ["checkout-svc"]
    assert mb.spot_entities("nothing relevant here", known) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memcot_bench.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'memcot_bench'`.

- [ ] **Step 3: Write minimal implementation**

```python
# evals/memcot_bench.py
#!/usr/bin/env python
"""MemCoT-style iterative retrieval loop — measurement harness (dev-only).

Measures whether an iterative search->graph-expand->re-query loop lifts
multi-hop recall over single-shot search. Three arms: baseline (single
search), loop-no-graph, loop+graph. Deterministic seeded edges isolate the
retrieval loop from extraction. See
docs/specs/2026-06-23-memcot-retrieval-loop-design.md.

Isolation: dedicated pseudolife_memory_bench DB, CPU only, no served LLM,
live bank untouched.
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder_sweep import approx_tokens, value_present  # noqa: E402

# ---------------------------------------------------------------------------
# Corpus — each fact appears twice: as an ingested snippet AND a seeded edge.
# Questions span 1/2/3 hops; gold = terminal entity name.
# ---------------------------------------------------------------------------
CORPUS: list[dict] = [
    # chain 1 (2-hop): checkout-svc -> billing-lib -> jvm-21
    {"snippet": "The checkout-svc depends-on the billing-lib module.",
     "edges": [("checkout-svc", "depends-on", "billing-lib")]},
    {"snippet": "Internally, billing-lib runs-on the jvm-21 runtime.",
     "edges": [("billing-lib", "runs-on", "jvm-21")]},
    # chain 2 (3-hop): web-frontend -> api-gateway -> auth-svc -> session-store
    {"snippet": "The web-frontend uses the api-gateway for all calls.",
     "edges": [("web-frontend", "uses", "api-gateway")]},
    {"snippet": "Our api-gateway depends-on the auth-svc for tokens.",
     "edges": [("api-gateway", "depends-on", "auth-svc")]},
    {"snippet": "The auth-svc stores-data-in the session-store backend.",
     "edges": [("auth-svc", "stores-data-in", "session-store")]},
    # chain 3 (2-hop): order-svc -> commerce-platform -> k8s-prod
    {"snippet": "order-svc is part-of the commerce-platform.",
     "edges": [("order-svc", "part-of", "commerce-platform")]},
    {"snippet": "The commerce-platform runs-on the k8s-prod cluster.",
     "edges": [("commerce-platform", "runs-on", "k8s-prod")]},
    # 1-hop guardrail facts
    {"snippet": "report-svc runs-on the jvm-17 runtime.",
     "edges": [("report-svc", "runs-on", "jvm-17")]},
    {"snippet": "cache-svc uses redis-7 for hot keys.",
     "edges": [("cache-svc", "uses", "redis-7")]},
    {"snippet": "search-svc stores-data-in the es-cluster index.",
     "edges": [("search-svc", "stores-data-in", "es-cluster")]},
]

DISTRACTORS: list[str] = [
    "The internal wiki lives at wiki.corp.local.",
    "Daily standups are at 9:30am in the main channel.",
    "The frontend bundle is about 2MB after tree-shaking.",
    "Release notes ship to the changelog every Friday.",
    "The staging autoscaler kicks in above 70% CPU.",
]

QUESTIONS: list[dict] = [
    # 1-hop
    {"question": "What runtime does report-svc run on?", "gold": "jvm-17", "hops": 1},
    {"question": "What does cache-svc use for hot keys?", "gold": "redis-7", "hops": 1},
    {"question": "Where does search-svc store its data?", "gold": "es-cluster", "hops": 1},
    # 2-hop
    {"question": "What runtime does checkout-svc run on?", "gold": "jvm-21", "hops": 2},
    {"question": "What cluster does order-svc run on?", "gold": "k8s-prod", "hops": 2},
    # 3-hop
    {"question": "Where does the web-frontend ultimately store data?",
     "gold": "session-store", "hops": 3},
]

KNOWN_ENTITIES: set[str] = {
    e for rec in CORPUS for (s, _r, d) in rec["edges"] for e in (s, d)
}


def spot_entities(text: str, known: set[str]) -> list[str]:
    """Known entity names present in ``text`` (word-boundary match).

    Mechanical stand-in for NER: the LLM controller would name entities by
    reading; the mechanical controller matches against the known vocabulary.
    """
    return [name for name in known if value_present(text, name)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memcot_bench.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add evals/memcot_bench.py tests/test_memcot_bench.py
git commit -m "feat(memcot): corpus, entity vocab, entity spotter"
```

---

### Task 2: LoopState + Controller protocol + MechanicalController

**Files:**
- Modify: `evals/memcot_bench.py`
- Test: `tests/test_memcot_bench.py`

**Interfaces:**
- Consumes: nothing from Task 1 at runtime.
- Produces:
  - `LoopState` dataclass: `entities:set[str]`, `texts:list[str]`, `facts:list[str]`, `iterations:int=0`, `queries_issued:int=0`, `latency_ms:float=0.0`, `low_confidence:bool=False`, `top_score:float=0.0`.
  - `assembled_context(state: LoopState) -> list[str]` → `texts + facts + sorted(entities)`.
  - `Controller` protocol: `seed_queries(question:str)->list[str]`; `expand(question:str, newly:list[str])->tuple[list[str], bool]` (returns `(next_queries, stop)`).
  - `MechanicalController` implementing it: `seed_queries` → `[question]`; `expand` → `([], True)` if `newly` empty else `([f"{question} {n}" for n in newly], False)`.

- [ ] **Step 1: Write the failing test**

```python
def test_assembled_context_unions_texts_facts_entities():
    st = mb.LoopState(entities={"jvm-21"}, texts=["t1"], facts=["runtime=jvm-21"])
    assert mb.assembled_context(st) == ["t1", "runtime=jvm-21", "jvm-21"]


def test_mechanical_controller_seeds_with_question():
    c = mb.MechanicalController()
    assert c.seed_queries("what runs checkout-svc?") == ["what runs checkout-svc?"]


def test_mechanical_controller_expands_on_new_entities():
    c = mb.MechanicalController()
    queries, stop = c.expand("q", ["billing-lib", "jvm-21"])
    assert stop is False
    assert queries == ["q billing-lib", "q jvm-21"]


def test_mechanical_controller_stops_when_no_new_entities():
    c = mb.MechanicalController()
    assert c.expand("q", []) == ([], True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memcot_bench.py -k "controller or assembled" -v`
Expected: FAIL — `AttributeError: module 'memcot_bench' has no attribute 'LoopState'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/memcot_bench.py`:

```python
from dataclasses import dataclass, field  # noqa: E402
from typing import Protocol  # noqa: E402


@dataclass
class LoopState:
    entities: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    iterations: int = 0
    queries_issued: int = 0
    latency_ms: float = 0.0
    low_confidence: bool = False
    top_score: float = 0.0


def assembled_context(state: LoopState) -> list[str]:
    """Everything the arm 'read' — scored for gold presence + token cost."""
    return list(state.texts) + list(state.facts) + sorted(state.entities)


class Controller(Protocol):
    def seed_queries(self, question: str) -> list[str]: ...
    def expand(self, question: str, newly: list[str]) -> tuple[list[str], bool]: ...


class MechanicalController:
    """Deterministic controller: re-query with each newly discovered entity;
    stop when an iteration discovers nothing new. The LLM seam is a future
    subclass implementing the same two methods over a served model."""

    def seed_queries(self, question: str) -> list[str]:
        return [question]

    def expand(self, question: str, newly: list[str]) -> tuple[list[str], bool]:
        if not newly:
            return [], True
        return [f"{question} {name}" for name in newly], False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memcot_bench.py -k "controller or assembled" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add evals/memcot_bench.py tests/test_memcot_bench.py
git commit -m "feat(memcot): LoopState, Controller protocol, MechanicalController"
```

---

### Task 3: The loop engine (`run_loop`)

**Files:**
- Modify: `evals/memcot_bench.py`
- Test: `tests/test_memcot_bench.py`

**Interfaces:**
- Consumes: `LoopState`, `Controller`, `spot_entities`, `assembled_context` (Tasks 1–2).
- Produces: `run_loop(svc, question:str, controller:Controller, *, use_graph:bool, known_entities:set[str], hop_cap:int=3, top_k:int=5) -> LoopState`. `svc` is any object exposing `search(query, top_k)->{"entries":[{"text","score"}], "low_confidence":bool}` and `graph_neighborhood(entity, depth)->{"found":bool, "nodes":[{"entity","facts":[{"attribute","value"}]}]}`.

- [ ] **Step 1: Write the failing test**

```python
class _FakeSvc:
    """Duck-typed MemoryService for engine unit tests.

    `search` is deliberately weak — it returns only snippets that contain a
    query token verbatim — so multi-hop terminals are NOT retrievable by
    re-query alone; the graph must do the traversal.
    """

    def __init__(self, snippets, edges):
        self.snippets = snippets
        self.edges = edges  # list[(src, rel, dst)]

    def search(self, query, top_k=5):
        toks = set(re.findall(r"[\w-]+", query.lower()))
        hits = [s for s in self.snippets
                if toks & set(re.findall(r"[\w-]+", s.lower()))]
        hits = hits[:top_k]
        return {"entries": [{"text": s, "score": 0.9} for s in hits],
                "low_confidence": len(hits) == 0, "count": len(hits)}

    def graph_neighborhood(self, entity, depth=1, **kw):
        nbrs = set()
        for (s, _r, d) in self.edges:
            if s == entity:
                nbrs.add(d)
            if d == entity:
                nbrs.add(s)
        nodes = [{"entity": entity, "facts": []}]
        nodes += [{"entity": n, "facts": []} for n in sorted(nbrs)]
        return {"found": True, "entity": entity, "depth": 1,
                "nodes": nodes, "edges": [], "paths": []}


def _two_hop_fake():
    # checkout-svc -> billing-lib -> jvm-21; terminal snippet shares NO token
    # with the question, so search alone can't reach jvm-21.
    snippets = ["checkout-svc depends-on billing-lib",
                "ZZZ runtime detail jvm-21 here"]
    edges = [("checkout-svc", "depends-on", "billing-lib"),
             ("billing-lib", "runs-on", "jvm-21")]
    return _FakeSvc(snippets, edges)


def test_graph_loop_reaches_two_hop_terminal():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    # seed entity comes from the question text
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=True,
                     known_entities=known)
    assert "jvm-21" in st.entities
    assert any(mb.value_present(s, "jvm-21") for s in mb.assembled_context(st))


def test_search_only_loop_misses_unretrievable_terminal():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=False,
                     known_entities=known)
    assert "jvm-21" not in st.entities


def test_hop_cap_is_respected():
    svc = _two_hop_fake()
    known = {"checkout-svc", "billing-lib", "jvm-21"}
    st = mb.run_loop(svc, "what does checkout-svc run on",
                     mb.MechanicalController(), use_graph=True,
                     known_entities=known, hop_cap=1)
    assert st.iterations <= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memcot_bench.py -k "loop or hop" -v`
Expected: FAIL — `AttributeError: module 'memcot_bench' has no attribute 'run_loop'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/memcot_bench.py`:

```python
import time  # noqa: E402


def run_loop(svc, question: str, controller: Controller, *, use_graph: bool,
             known_entities: set[str], hop_cap: int = 3,
             top_k: int = 5) -> LoopState:
    """Iterative search(+graph) loop. Depth=1 graph expansion per iteration so
    N hops costs N iterations (honest iteration cost)."""
    state = LoopState()
    # Seed entities from the question itself (the LLM would read them; we spot).
    seeds = spot_entities(question, known_entities)
    state.entities.update(seeds)
    pending = list(seeds)              # entities awaiting graph expansion
    queries = controller.seed_queries(question)
    t0 = time.perf_counter()
    while True:
        state.iterations += 1
        newly: list[str] = []
        # 1) search step
        for q in queries:
            state.queries_issued += 1
            res = svc.search(q, top_k=top_k)
            if state.iterations == 1 and q == question:
                state.low_confidence = bool(res.get("low_confidence"))
                entries0 = res.get("entries", [])
                state.top_score = float(entries0[0]["score"]) if entries0 else 0.0
            for e in res.get("entries", []):
                txt = e.get("text", "")
                if txt and txt not in state.texts:
                    state.texts.append(txt)
                    for nm in spot_entities(txt, known_entities):
                        if nm not in state.entities:
                            state.entities.add(nm)
                            newly.append(nm)
        # 2) graph expansion step (arm A only): expand entities found so far
        next_pending: list[str] = []
        if use_graph:
            for nm in pending:
                nb = svc.graph_neighborhood(nm, depth=1)
                if not nb.get("found"):
                    continue
                for node in nb.get("nodes", []):
                    en = node.get("entity", "")
                    if en and en not in state.entities:
                        state.entities.add(en)
                        newly.append(en)
                        next_pending.append(en)
                    for f in node.get("facts", []):
                        fs = f"{f.get('attribute')}={f.get('value')}"
                        if fs not in state.facts:
                            state.facts.append(fs)
        # 3) controller decides continuation
        queries, stop = controller.expand(question, newly)
        pending = next_pending
        if stop or not queries or state.iterations >= hop_cap:
            break
    state.latency_ms = (time.perf_counter() - t0) * 1000
    return state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memcot_bench.py -k "loop or hop" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add evals/memcot_bench.py tests/test_memcot_bench.py
git commit -m "feat(memcot): iterative loop engine with depth-1/iter graph expansion"
```

---

### Task 4: Baseline, scoring, gate, and metric aggregation

**Files:**
- Modify: `evals/memcot_bench.py`
- Test: `tests/test_memcot_bench.py`

**Interfaces:**
- Consumes: `LoopState`, `assembled_context`, `spot_entities`, `value_present`, `approx_tokens` (Tasks 1–3).
- Produces:
  - `run_baseline(svc, question:str, *, top_k:int=5) -> LoopState` (single search; `iterations=1`; `texts`=hit texts; sets `low_confidence`/`top_score`).
  - `gold_recovered(state:LoopState, gold:str) -> bool`.
  - `tokens_read(state:LoopState) -> int`.
  - `would_gate(state:LoopState, thin:float=0.5) -> bool` → `state.low_confidence or state.top_score < thin`.
  - `aggregate(records:list[dict]) -> dict` where each record is `{"hops":int,"recovered":bool,"iterations":int,"tokens":int,"latency_ms":float}`; returns `{"overall":{...}, "by_hops":{1:{...},2:{...},3:{...}}}` with keys `n, recall, mean_iterations, mean_tokens, mean_latency_ms`.

- [ ] **Step 1: Write the failing test**

```python
def test_gold_recovered_checks_assembled_context():
    st = mb.LoopState(entities={"jvm-21"}, texts=["unrelated"])
    assert mb.gold_recovered(st, "jvm-21") is True
    assert mb.gold_recovered(st, "k8s-prod") is False


def test_tokens_read_sums_assembled_context():
    st = mb.LoopState(texts=["abcd" * 4], facts=["x=y"])  # 16 + 3 chars
    assert mb.tokens_read(st) == mb.approx_tokens("abcd" * 4) + mb.approx_tokens("x=y")


def test_would_gate_fires_on_thin_or_low_conf():
    assert mb.would_gate(mb.LoopState(low_confidence=True, top_score=0.9)) is True
    assert mb.would_gate(mb.LoopState(low_confidence=False, top_score=0.3)) is True
    assert mb.would_gate(mb.LoopState(low_confidence=False, top_score=0.8)) is False


def test_aggregate_buckets_by_hops_and_overall():
    recs = [
        {"hops": 1, "recovered": True, "iterations": 1, "tokens": 10, "latency_ms": 1.0},
        {"hops": 2, "recovered": False, "iterations": 2, "tokens": 20, "latency_ms": 2.0},
        {"hops": 2, "recovered": True, "iterations": 2, "tokens": 30, "latency_ms": 4.0},
    ]
    agg = mb.aggregate(recs)
    assert agg["overall"]["n"] == 3
    assert abs(agg["overall"]["recall"] - 2 / 3) < 1e-6
    assert agg["by_hops"][2]["n"] == 2
    assert abs(agg["by_hops"][2]["recall"] - 0.5) < 1e-6
    assert agg["by_hops"][2]["mean_tokens"] == 25.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memcot_bench.py -k "gold or tokens or gate or aggregate" -v`
Expected: FAIL — `AttributeError: ... 'run_baseline'`/`'aggregate'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/memcot_bench.py`:

```python
def run_baseline(svc, question: str, *, top_k: int = 5) -> LoopState:
    """Single-shot search — the control arm."""
    state = LoopState(iterations=1, queries_issued=1)
    t0 = time.perf_counter()
    res = svc.search(question, top_k=top_k)
    state.low_confidence = bool(res.get("low_confidence"))
    entries = res.get("entries", [])
    state.top_score = float(entries[0]["score"]) if entries else 0.0
    for e in entries:
        txt = e.get("text", "")
        if txt:
            state.texts.append(txt)
    state.latency_ms = (time.perf_counter() - t0) * 1000
    return state


def gold_recovered(state: LoopState, gold: str) -> bool:
    return any(value_present(s, gold) for s in assembled_context(state))


def tokens_read(state: LoopState) -> int:
    return sum(approx_tokens(s) for s in assembled_context(state))


def would_gate(state: LoopState, thin: float = 0.5) -> bool:
    """Whether a shipped gate WOULD enter the loop (reported, not enforced)."""
    return bool(state.low_confidence) or state.top_score < thin


def _means(recs: list[dict]) -> dict:
    n = len(recs)
    if n == 0:
        return {"n": 0, "recall": 0.0, "mean_iterations": 0.0,
                "mean_tokens": 0.0, "mean_latency_ms": 0.0}
    return {
        "n": n,
        "recall": round(sum(1 for r in recs if r["recovered"]) / n, 3),
        "mean_iterations": round(sum(r["iterations"] for r in recs) / n, 2),
        "mean_tokens": round(sum(r["tokens"] for r in recs) / n, 1),
        "mean_latency_ms": round(sum(r["latency_ms"] for r in recs) / n, 1),
    }


def aggregate(records: list[dict]) -> dict:
    by_hops = {}
    for h in sorted({r["hops"] for r in records}):
        by_hops[h] = _means([r for r in records if r["hops"] == h])
    return {"overall": _means(records), "by_hops": by_hops}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memcot_bench.py -k "gold or tokens or gate or aggregate" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add evals/memcot_bench.py tests/test_memcot_bench.py
git commit -m "feat(memcot): baseline, scoring, would-gate, metric aggregation"
```

---

### Task 5: Real-service arms, runner, report, JSON + PG smoke test

**Files:**
- Modify: `evals/memcot_bench.py`
- Test: `tests/test_memcot_bench.py`

**Interfaces:**
- Consumes: everything above; `reset_bench`/`build_service` from `ladder_sweep`; `MemoryService.store`, `MemoryService.graph_relate`.
- Produces:
  - `seed_bench(svc) -> None` — ingest snippets + distractors as `source="bench"`, seed every edge via `svc.graph_relate(src, rel, dst, origin="bench")`.
  - `run_all(svc, *, top_k:int=5, hop_cap:int=3) -> dict` — runs baseline + arm B + arm A over `QUESTIONS`; returns `{"baseline":agg, "loop_no_graph":agg, "loop_graph":agg, "gate_would_fire":int, "lift_from_looping":float, "lift_from_graph":float}`.
  - `report(results:dict) -> None` — prints the per-arm × hop-class table + attribution.
  - `main()` argparse: `--run`, `--show-corpus`, `--top-k`, `--hop-cap`.

- [ ] **Step 1: Write the failing test**

```python
import os
import pytest

_BENCH_PG = os.environ.get(
    "PSEUDOLIFE_BENCH_ADMIN_URL",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
)


def _pg_reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(_BENCH_PG, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="bench Postgres not reachable")
def test_run_all_arm_graph_beats_baseline_on_a_two_hop_case():
    import tempfile
    from ladder_sweep import build_service
    with tempfile.TemporaryDirectory(prefix="plmemcot_", ignore_cleanup_errors=True) as td:
        svc = mb.build_service(Path(td)) if hasattr(mb, "build_service") else build_service(Path(td))
        mb.seed_bench(svc)
        results = mb.run_all(svc)
    # structural
    for arm in ("baseline", "loop_no_graph", "loop_graph"):
        assert "overall" in results[arm]
    # core hypothesis smoke: graph loop recall >= baseline overall
    assert results["loop_graph"]["overall"]["recall"] >= results["baseline"]["overall"]["recall"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memcot_bench.py -k "run_all" -v`
Expected: FAIL — `AttributeError: ... 'seed_bench'` (or SKIP if PG down; bring the bench PG up to exercise it).

- [ ] **Step 3: Write minimal implementation**

Append to `evals/memcot_bench.py`:

```python
import json  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def seed_bench(svc) -> None:
    """Ingest snippets + distractors as memories AND seed the graph edges."""
    for rec in CORPUS:
        svc.store(rec["snippet"], source="bench")
    for d in DISTRACTORS:
        svc.store(d, source="bench")
    for rec in CORPUS:
        for (src, rel, dst) in rec["edges"]:
            out = svc.graph_relate(src, rel, dst, origin="bench")
            if out.get("error"):
                raise RuntimeError(f"seed edge failed: {src} {rel} {dst}: {out}")


def run_all(svc, *, top_k: int = 5, hop_cap: int = 3) -> dict:
    base_recs, b_recs, a_recs = [], [], []
    gate_fire = 0
    for q in QUESTIONS:
        question, gold, hops = q["question"], q["gold"], q["hops"]
        base = run_baseline(svc, question, top_k=top_k)
        if would_gate(base):
            gate_fire += 1
        b = run_loop(svc, question, MechanicalController(), use_graph=False,
                     known_entities=KNOWN_ENTITIES, hop_cap=hop_cap, top_k=top_k)
        a = run_loop(svc, question, MechanicalController(), use_graph=True,
                     known_entities=KNOWN_ENTITIES, hop_cap=hop_cap, top_k=top_k)
        for st, sink in ((base, base_recs), (b, b_recs), (a, a_recs)):
            sink.append({"hops": hops, "recovered": gold_recovered(st, gold),
                         "iterations": st.iterations, "tokens": tokens_read(st),
                         "latency_ms": st.latency_ms})
    base_agg = aggregate(base_recs)
    b_agg = aggregate(b_recs)
    a_agg = aggregate(a_recs)
    return {
        "baseline": base_agg, "loop_no_graph": b_agg, "loop_graph": a_agg,
        "gate_would_fire": gate_fire, "questions": len(QUESTIONS),
        "lift_from_looping": round(
            b_agg["overall"]["recall"] - base_agg["overall"]["recall"], 3),
        "lift_from_graph": round(
            a_agg["overall"]["recall"] - b_agg["overall"]["recall"], 3),
    }


def report(results: dict) -> None:
    arms = [("baseline", "single-shot search"),
            ("loop_no_graph", "loop, no graph (B)"),
            ("loop_graph", "loop + graph (A)")]
    hdr = f"{'arm':<24}{'recall':>8}{'iters':>7}{'tok/q':>8}{'lat ms':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for key, label in arms:
        o = results[key]["overall"]
        print(f"{label:<24}{o['recall']:>8}{o['mean_iterations']:>7}"
              f"{o['mean_tokens']:>8}{o['mean_latency_ms']:>8}")
    print("\nby hop-class (recall):")
    print(f"{'arm':<24}{'1-hop':>8}{'2-hop':>8}{'3-hop':>8}")
    for key, label in arms:
        bh = results[key]["by_hops"]
        cells = "".join(f"{bh.get(h, {}).get('recall', '—'):>8}" for h in (1, 2, 3))
        print(f"{label:<24}{cells}")
    print(f"\nlift_from_looping (B - baseline): {results['lift_from_looping']}")
    print(f"lift_from_graph   (A - B):        {results['lift_from_graph']}")
    print(f"gate would fire on {results['gate_would_fire']}/{results['questions']} "
          f"questions")


def main() -> int:
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true", help="run all three arms")
    ap.add_argument("--show-corpus", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--hop-cap", type=int, default=3)
    args = ap.parse_args()

    if args.show_corpus:
        for q in QUESTIONS:
            print(f"  [{q['hops']}-hop] {q['question']}  -> {q['gold']}")
        return 0
    if args.run:
        import tempfile
        from ladder_sweep import build_service
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="plmemcot_",
                                         ignore_cleanup_errors=True) as td:
            svc = build_service(Path(td))
            seed_bench(svc)
            results = run_all(svc, top_k=args.top_k, hop_cap=args.hop_cap)
        (RESULTS_DIR / "memcot.json").write_text(json.dumps(results, indent=2))
        report(results)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

Note: `build_service` is imported from `ladder_sweep` inside `main`/tests; the test's `hasattr(mb, "build_service")` guard simply falls back to the direct import.

- [ ] **Step 4: Run test to verify it passes**

Run (bench PG up): `python -m pytest tests/test_memcot_bench.py -k "run_all" -v`
Expected: PASS (or SKIP if PG unreachable). Then run the full file:
Run: `python -m pytest tests/test_memcot_bench.py -v`
Expected: all PASS/SKIP, no failures.

- [ ] **Step 5: Commit**

```bash
git add evals/memcot_bench.py tests/test_memcot_bench.py
git commit -m "feat(memcot): real-service arms, runner, report, JSON + PG smoke"
```

---

### Task 6: Run the bench for real, capture results, document

**Files:**
- Create: `evals/results/memcot.json` (generated)
- Modify: `evals/README.md`

**Interfaces:**
- Consumes: the finished harness.

- [ ] **Step 1: Run the actual measurement**

Run: `python evals/memcot_bench.py --run`
Expected: the per-arm × hop-class table prints and `evals/results/memcot.json` is written. Capture the table.

- [ ] **Step 2: Add a memcot section to evals/README.md**

Document: what the bench measures, how to run it (`python evals/memcot_bench.py --run`), the bench-DB/CPU isolation, that it needs no served model, and how to read the three arms + the two attribution deltas. Match the existing README's section style (read the file first; append a `## MemCoT retrieval-loop bench` section).

- [ ] **Step 3: Commit results + docs**

```bash
git add evals/results/memcot.json evals/README.md
git commit -m "evals(memcot): first measurement results + README"
```

- [ ] **Step 4: Verification**

Run: `python -m pytest tests/test_memcot_bench.py -v`
Expected: green. Confirm `evals/results/memcot.json` contains `baseline`, `loop_no_graph`, `loop_graph`, `lift_from_looping`, `lift_from_graph`.

---

## Self-Review

**1. Spec coverage:**
- Architecture / file / reuse → Tasks 1, 5 (module + `ladder_sweep` reuse). ✓
- Corpus (snippets + edges, hop-classes, gold) → Task 1. ✓
- Loop engine + Controller seam + depth-1/iter → Tasks 2–3. ✓
- 3 arms + attribution deltas → Task 5 (`run_all`). ✓
- Gate (reported not enforced) → Task 4 (`would_gate`), surfaced in Task 5. ✓
- Metrics by hop-class → Task 4 (`aggregate`), Task 5 (`report`). ✓
- Isolation/safety (bench DB, CPU, no LLM/network) → Global Constraints + Tasks 1/5. ✓
- Deterministic seeded edges → Task 5 (`seed_bench` via `graph_relate`). ✓
- Measure-first (no hard threshold) → Global Constraints; smoke test uses `>=` only. ✓
- LLM seam (future) → `Controller` protocol (Task 2), noted out-of-scope.

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; the only `—` are table glyphs for empty hop-classes. ✓

**3. Type consistency:** `LoopState` fields, `assembled_context`, `run_loop` kw-only signature (`use_graph`, `known_entities`, `hop_cap`, `top_k`), `MechanicalController.expand -> (queries, stop)`, `aggregate` keys (`n, recall, mean_iterations, mean_tokens, mean_latency_ms`) are used identically across Tasks 2–5. `seed_bench`/`run_all`/`report` names match between Task 5 interfaces, code, and tests. ✓
