# `memory_recall` Live Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a read-only `memory_recall` MCP tool that runs the MemCoT iterative search→graph-expand→re-query loop over the live bank, then deploy it (one daemon rebuild that also lands the already-merged GAM #2 graph-population).

**Architecture:** A new pure-orchestration module `pseudolife_memory/memory/recall.py` (loop engine + controllers, injected callables, unit-testable without a DB) is wired by a new `service.recall()` that composes the existing public `search` + `graph_neighborhood` and a live entity-vocabulary load. A thin `@mcp.tool() memory_recall` exposes it. `memory_search` is untouched.

**Tech Stack:** Python 3.11, stdlib only in `recall.py` (+ `re`, `urllib`, `json`); `pseudolife_memory.service.MemoryService`; pytest; Postgres+pgvector (bench DB only) for integration tests.

## Global Constraints

- `memory_search` and every existing retrieval path are UNCHANGED — add only.
- `recall` is strictly READ-ONLY (no writes).
- `recall.py` must NOT import from `evals/`; it depends only on stdlib + injected callables. (Tests MAY add `evals/` to `sys.path` to reuse `ladder_sweep.build_service` for PG integration, mirroring `tests/test_memcot_bench.py`.)
- `service.recall` holds NO lock while looping (it composes the already-locked public `search`/`graph_neighborhood`); the entity-vocab load is a short separate locked read.
- Graph expansion is depth-1 per iteration (N hops = N iterations).
- Driver default `mechanical`; override via `config.memory.recall.driver` and env `PSEUDOLIFE_RECALL_DRIVER`.
- Cost caps: `hops` default 3 (hard max 5), `top_k` default 5, `max_entities` default 50.
- Tests run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest <path> -v`.
- Closed relation vocab is read, never modified by recall.

---

### Task 1: `recall.py` core — state, controller protocol, mechanical controller, loop engine

**Files:**
- Create: `pseudolife_memory/memory/recall.py`
- Test: `tests/test_recall.py`

**Interfaces:**
- Produces:
  - `RecallState` dataclass: `seeds:list[str]`, `entities:list[str]`, `entity_facts:dict[str,list[dict]]`, `texts:list[str]`, `edges:list[dict]`, `paths:list[list[str]]`, `iterations:int=0`, `low_confidence:bool=False`.
  - `RecallController` Protocol: `seed_entities(query:str, hits:list[str], vocab:list[str])->list[str]`; `next_queries(query:str, newly:list[str])->list[str]`.
  - `MechanicalController` implementing it.
  - `run_recall(search_fn, graph_fn, vocab, query, controller, *, hops=3, top_k=5, max_entities=50) -> RecallState`. `search_fn(query, top_k)->{"entries":[{"text":str}]}`; `graph_fn(entity, depth)->{"found":bool,"nodes":[{"entity":str,"facts":[...]}],"edges":[{"src","relation","dst","derived"}],"paths":[[str]]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recall.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.memory import recall as rc  # noqa: E402


class _FakeSvc:
    """Weak search (returns only snippets sharing a query token) + a structural
    graph, so multi-hop terminals are reachable ONLY via graph traversal."""

    def __init__(self, snippets, edges):
        self.snippets = snippets
        self.edges = edges  # list[(src, rel, dst)]

    def search(self, query, top_k=5):
        import re
        toks = set(re.findall(r"[\w-]+", query.lower()))
        hits = [s for s in self.snippets
                if toks & set(re.findall(r"[\w-]+", s.lower()))][:top_k]
        return {"entries": [{"text": s} for s in hits]}

    def graph(self, entity, depth=1):
        nbrs = set()
        for (s, _r, d) in self.edges:
            if s == entity:
                nbrs.add(d)
            if d == entity:
                nbrs.add(s)
        nodes = [{"entity": entity, "facts": [{"attribute": "t", "value": entity}]}]
        nodes += [{"entity": n, "facts": []} for n in sorted(nbrs)]
        edges = [{"src": s, "relation": r, "dst": d, "derived": False}
                 for (s, r, d) in self.edges if s == entity or d == entity]
        return {"found": True, "nodes": nodes, "edges": edges, "paths": []}


def _two_hop():
    snippets = ["alpha depends-on beta", "ZZZ runtime note gamma here"]
    edges = [("alpha", "depends-on", "beta"), ("beta", "runs-on", "gamma")]
    return _FakeSvc(snippets, edges)


def test_mechanical_seeds_from_query_and_hits():
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does alpha run on", ["alpha depends-on beta"],
                            ["alpha", "beta", "gamma"])
    assert seeds == ["alpha", "beta"]  # both present in query+hits, vocab order


def test_run_recall_reaches_two_hop_terminal():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController())
    assert "gamma" in st.entities
    assert any(e["dst"] == "gamma" for e in st.edges)
    assert st.low_confidence is False


def test_run_recall_low_confidence_when_no_seed():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "totally unrelated question", rc.MechanicalController())
    assert st.low_confidence is True
    assert st.seeds == []


def test_run_recall_respects_hops_cap():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController(), hops=1)
    assert st.iterations <= 1


def test_run_recall_respects_max_entities():
    svc = _two_hop()
    st = rc.run_recall(svc.search, svc.graph, ["alpha", "beta", "gamma"],
                       "what does alpha run on", rc.MechanicalController(),
                       max_entities=1)
    assert len(st.entities) <= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError: module ... has no attribute 'RecallState'`.

- [ ] **Step 3: Write minimal implementation**

```python
# pseudolife_memory/memory/recall.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/recall.py tests/test_recall.py
git commit -m "feat(recall): loop engine + mechanical controller (read-only)"
```

---

### Task 2: `LLMController` + `simple_complete` (real-but-minimal LLM driver)

**Files:**
- Modify: `pseudolife_memory/memory/recall.py`
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `RecallController` shape, `MechanicalController.next_queries` reuse, `_mentions` (Task 1).
- Produces:
  - `LLMController(complete: Callable[[str], str])` implementing `RecallController`; `seed_entities` asks the model which vocab entities the query refers to (JSON list), filtered to `vocab`; `next_queries` reuses the mechanical phrasing.
  - `simple_complete(dream_cfg, prompt: str) -> str` — stdlib OpenAI-compat `/chat/completions` call using the dream extractor's base-url/model/key; returns `""` on any failure.
  - `_parse_name_list(raw: str) -> list[str]` — tolerant JSON-array parse.

- [ ] **Step 1: Write the failing test**

```python
def test_llm_controller_seeds_from_completion_filtered_to_vocab():
    calls = {}

    def fake_complete(prompt):
        calls["prompt"] = prompt
        return '["alpha", "not-in-vocab"]'

    c = rc.LLMController(fake_complete)
    seeds = c.seed_entities("which thing runs alpha",
                            ["alpha depends-on beta"], ["alpha", "beta", "gamma"])
    assert seeds == ["alpha"]            # not-in-vocab dropped
    assert "alpha" in calls["prompt"]    # vocab/query passed to the model


def test_llm_controller_next_queries_match_mechanical():
    c = rc.LLMController(lambda p: "[]")
    assert c.next_queries("q", ["beta"]) == ["q beta"]


def test_parse_name_list_tolerates_noise():
    assert rc._parse_name_list('junk ["a", "b"] trailing') == ["a", "b"]
    assert rc._parse_name_list("not json at all") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -k "llm or parse_name" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'LLMController'`.

- [ ] **Step 3: Write minimal implementation**

Append to `pseudolife_memory/memory/recall.py`:

```python
import json  # noqa: E402
import urllib.request  # noqa: E402


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
    return [str(x) for x in arr if isinstance(x, str)]


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
    import os
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
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/recall.py tests/test_recall.py
git commit -m "feat(recall): real-but-minimal injected LLMController + simple_complete"
```

---

### Task 3: `RecallConfig` + `service.recall()` (live wiring) + PG integration

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (add `RecallConfig`; nest under `MemoryConfig`)
- Modify: `pseudolife_memory/service.py` (add `recall` + `_recall_vocab`)
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `run_recall`, `MechanicalController`, `LLMController`, `simple_complete` (Tasks 1–2); `self.search`, `self.graph_neighborhood`, `self._storage.load_graph()`, `self.config.memory.dream`.
- Produces:
  - `RecallConfig` dataclass: `driver:str="mechanical"`, `default_hops:int=3`, `default_top_k:int=5`, `max_entities:int=50`; `MemoryConfig.recall: RecallConfig`.
  - `MemoryService.recall(query, hops=None, top_k=None, driver=None) -> dict` returning `{query, seeds, entities:[{entity,facts}], edges, paths, texts, iterations, hops, low_confidence}`.

- [ ] **Step 1: Write the failing test (PG-backed integration)**

```python
import os
import pytest

_ADMIN = os.environ.get(
    "PSEUDOLIFE_BENCH_ADMIN_URL",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres",
)


def _pg_up() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_bridges_two_hop_on_real_service(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service  # reuse isolated bench DB
    svc = build_service(tmp_path)
    svc.store("checkout-svc depends on the billing-lib package.", source="bench")
    svc.store("billing-lib is compiled against the jdk-21 toolchain.", source="bench")
    assert not svc.graph_relate("checkout-svc", "depends-on", "billing-lib").get("error")
    assert not svc.graph_relate("billing-lib", "runs-on", "jdk-21").get("error")

    out = svc.recall("what does checkout-svc run on?")
    assert out["low_confidence"] is False
    assert "checkout-svc" in out["seeds"]
    visited = {n["entity"] for n in out["entities"]}
    assert "jdk-21" in visited                       # bridged 2 hops via graph
    assert any(e["dst"] == "jdk-21" for e in out["edges"])


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_low_confidence_when_query_names_no_entity(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("checkout-svc depends on the billing-lib package.", source="bench")
    svc.graph_relate("checkout-svc", "depends-on", "billing-lib")
    out = svc.recall("what is the airspeed velocity of an unladen swallow?")
    assert out["low_confidence"] is True
    assert out["entities"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -k "real_service or names_no_entity" -v`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute 'recall'` (or SKIP if PG down; bring the bench PG up to exercise it).

- [ ] **Step 3a: Add `RecallConfig` to `config.py`**

Insert this dataclass immediately before `class MemoryConfig:` (around line 396 in `pseudolife_memory/utils/config.py`):

```python
@dataclass
class RecallConfig:
    """memory_recall — live MemCoT iterative retrieval (read-only).

    ``driver`` selects seed resolution: "mechanical" (word-match vocab; default,
    no model) or "llm" (the dream extractor names seeds). Env override:
    ``PSEUDOLIFE_RECALL_DRIVER``.
    """
    driver: str = "mechanical"
    default_hops: int = 3
    default_top_k: int = 5
    max_entities: int = 50
```

Then add this field inside `MemoryConfig` (next to `lessons`):

```python
    # memory_recall — live MemCoT iterative retrieval (read-only).
    recall: RecallConfig = field(default_factory=RecallConfig)
```

- [ ] **Step 3b: Add `recall` + `_recall_vocab` to `service.py`**

Add these methods to `MemoryService` (place them after `graph_neighborhood`, near the end of the class). `import os` is already at the top of `service.py`; do not re-add it.

```python
    def _recall_vocab(self) -> list[str]:
        """Live entity vocabulary (display names + aliases) for seed matching.
        Short locked read; released before the lock-free recall loop."""
        from pseudolife_memory import graph as _G  # noqa: F401 (parity w/ other methods)
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return []
            g = self._storage.load_graph()
        names: list[str] = [e["display"] for e in g.get("entities", [])]
        for al in g.get("aliases", {}).values():
            names.extend(al)
        return list(dict.fromkeys(n for n in names if n))

    def recall(self, query: str, hops: int | None = None,
               top_k: int | None = None, driver: str | None = None) -> dict[str, Any]:
        """Read-only multi-hop retrieval: search → graph-expand → re-query.

        Composes the public ``search`` + ``graph_neighborhood`` (each manages the
        lock); ``recall`` holds no lock itself. Returns the bridging
        edges/facts/paths single-shot search can't produce. ``low_confidence`` is
        True when no seed entity resolves (caller falls back to ``search``)."""
        from pseudolife_memory.memory.recall import (
            LLMController, MechanicalController, run_recall, simple_complete,
        )
        cfg = self.config.memory.recall
        hops = cfg.default_hops if hops is None else max(1, min(int(hops), 5))
        top_k = cfg.default_top_k if top_k is None else int(top_k)
        driver = driver or os.environ.get("PSEUDOLIFE_RECALL_DRIVER", cfg.driver)
        query = (query or "").strip()
        if not query:
            return {"query": "", "seeds": [], "entities": [], "edges": [],
                    "paths": [], "texts": [], "iterations": 0, "hops": hops,
                    "low_confidence": True}
        vocab = self._recall_vocab()
        if driver == "llm":
            dcfg = self.config.memory.dream
            controller = LLMController(lambda p: simple_complete(dcfg, p))
        else:
            controller = MechanicalController()
        state = run_recall(
            self.search, self.graph_neighborhood, vocab, query, controller,
            hops=hops, top_k=top_k, max_entities=cfg.max_entities,
        )
        return {
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
        }
```

Note: `run_recall` calls `search_fn(query, top_k)` and `graph_fn(entity, 1)`. `self.search(query, top_k)` and `self.graph_neighborhood(entity, 1)` match positionally (verified against their signatures).

- [ ] **Step 4: Run test to verify it passes**

Run (bench PG up): `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: all PASS (10 incl. 2 PG, or 8 + 2 SKIP if PG down).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(recall): RecallConfig + service.recall live wiring + PG integration"
```

---

### Task 4: `memory_recall` MCP tool + docs + full verification

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (add the tool)
- Modify: `CHANGELOG.md`, `README.md`
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: `service.recall` (Task 3).
- Produces: `memory_recall(query, hops=3, top_k=5) -> dict` MCP tool delegating to `service.recall`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_recall_tool_delegates(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.store("web-portal uses the gateway-proxy for calls.", source="bench")
    svc.store("the gateway-proxy is deployed on the edge-cluster.", source="bench")
    svc.graph_relate("web-portal", "uses", "gateway-proxy")
    svc.graph_relate("gateway-proxy", "runs-on", "edge-cluster")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_recall("what does web-portal run on?")
    assert "edge-cluster" in {n["entity"] for n in out["entities"]}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -k "tool_delegates" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'memory_recall'` (or SKIP if PG down).

- [ ] **Step 3: Add the tool**

Add to `pseudolife_memory/mcp_server.py` immediately after the `memory_graph` tool (around line 1110). Follow the existing `@mcp.tool()` + `service.<method>` delegation pattern:

```python
@mcp.tool()
def memory_recall(query: str, hops: int = 3, top_k: int = 5) -> dict[str, Any]:
    """Multi-hop retrieval: follow the knowledge graph to answer RELATIONAL
    questions that single-shot ``memory_search`` can't (it returns flat
    similarity, not chains).

    Use ``memory_recall`` for questions whose answer is reached by following
    links — "what does X ultimately run on?", "where does Y's data end up?",
    "what is X connected to?", "how does A reach C?". Use ``memory_search`` for
    direct lookups ("what is X's port?").

    It searches for a seed entity, walks its graph neighbourhood one hop per
    iteration (up to ``hops``, max 5), and gathers the bridging entities, facts,
    edges, and paths. Read-only — it never writes.

    Returns ``seeds``, ``entities`` (each with current facts), ``edges`` (with a
    ``derived`` flag for inferred transitive/inverse links), ``paths``, the
    supporting ``texts``, and ``iterations``. ``low_confidence: true`` means no
    seed entity matched the query — fall back to ``memory_search``.

    Args:
        query: A natural-language relational question.
        hops: Max graph hops / iterations (default 3, capped at 5).
        top_k: Results per internal search (default 5).
    """
    return service.recall(query, hops=hops, top_k=top_k)
```

- [ ] **Step 4: Run the tool test + full recall suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: all PASS/SKIP, no failures.

- [ ] **Step 5: Docs**

Add a `## memory_recall (multi-hop retrieval)` subsection to `README.md` (near the memory tools / search docs): what it does, when to use it vs `memory_search`, the `low_confidence` fallback, read-only, the `recall.driver` config (mechanical default; `PSEUDOLIFE_RECALL_DRIVER=llm` to use the dream endpoint for seed resolution). Add a `CHANGELOG.md` Unreleased entry: "Added `memory_recall` — read-only multi-hop graph-traversal retrieval (MemCoT loop); mechanical default + optional LLM seed driver." Read both files first and match their style.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_recall.py README.md CHANGELOG.md
git commit -m "feat(recall): memory_recall MCP tool + docs"
```

---

### Task 5: GATED live deploy (one rebuild lands GAM #2 + recall)

> **GATE:** Do NOT run this task without explicit user approval at execution time. It rebuilds and restarts the live daemon. Read-only tool, but it touches the running stack — backup first, never `down -v`.

**Files:** none (operational).

- [ ] **Step 1: Merge to master + verify additive**

```bash
git checkout master && git merge --ff-only feat/memory-recall-tool
git diff --name-only HEAD~4..HEAD | grep -E '^(pseudolife_memory|ops)/' || echo "(none)"
```
Expected: the merge fast-forwards; runtime files changed are only `pseudolife_memory/memory/recall.py`, `service.py`, `mcp_server.py`, `utils/config.py` (all additive). `memory_search` unchanged.

- [ ] **Step 2: Back up the live bank FIRST**

Run: `pwsh -File ops/backup.ps1`
Expected: a fresh `pseudolife_memory-<ts>.sql.gz` is written. Confirm the file exists before proceeding.

- [ ] **Step 3: Rebuild + restart the daemon (preserve volumes)**

```bash
docker compose -f ops/docker-compose.yml build daemon
docker compose -f ops/docker-compose.yml up -d
```
Do NOT use `down -v`. The two external volumes (`ops_pseudolife_pgdata`, `ops_pseudolife_data`) must persist.
Then health-check: `curl -s http://127.0.0.1:8765/health` → expect schema 11, status ok.

- [ ] **Step 4: Live smoke**

Via the live MCP tools (this session):
1. `memory_recall("what does pseudolife-mcp run on, ultimately?")` → assert `docker-desktop` appears in `entities`/`edges` (bridged `pseudolife-mcp → postgres → docker-desktop`), `low_confidence: false`.
2. `memory_search("pseudolife-mcp")` → unchanged shape, still returns results (no regression).
3. Trigger a dream: `memory_dream_run` (or `memory_dream_status` to confirm it will fire), then `memory_graph` a touched entity and confirm at least one `origin: "agent"` edge now exists (GAM #2 relation-extraction live).

- [ ] **Step 5: Record outcome**

Update memory (`memory_store`) with the deploy result (daemon rebuilt from `<sha>`, recall live, GAM #2 now writing agent edges, backup file name). Push master if the user asks.

---

## Self-Review

**1. Spec coverage:**
- recall.py loop engine + controllers → Tasks 1–2. ✓
- live entity-vocab seeding → Task 3 (`_recall_vocab`). ✓
- service.recall composing public methods, no lock held → Task 3. ✓
- RecallConfig (driver/hops/top_k/max_entities) + env override → Task 3. ✓
- mechanical default + real-but-minimal injected LLMController → Tasks 2–3. ✓
- memory_recall tool + docstring gate → Task 4. ✓
- memory_search untouched → Global Constraints; no task edits it. ✓
- cost caps (hops max 5, top_k, max_entities) → Task 1 (`run_recall`) + Task 3 (clamp). ✓
- read-only → no write calls in any task. ✓
- gated deploy (backup-first, rebuild, live smoke, GAM #2) → Task 5. ✓
- return shape (seeds/entities/edges/paths/texts/iterations/low_confidence) → Task 3. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; docs step (Task 4 Step 5) names exact files + content to add. ✓

**3. Type consistency:** `RecallState` fields, `run_recall(search_fn, graph_fn, vocab, query, controller, *, hops, top_k, max_entities)`, `RecallController.seed_entities/next_queries`, `MechanicalController`/`LLMController`, `simple_complete(dream_cfg, prompt)`, `_parse_name_list`, `service.recall(query, hops, top_k, driver)` and its return keys are used identically across Tasks 1–4. The tool's `(query, hops=3, top_k=5)` matches `service.recall`. ✓
