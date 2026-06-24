# Recall Hub-Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `memory_recall` from exploding through high-degree hub entities by including hubs as results but not expanding through them, plus degree-aware frontier ordering, a per-hop budget, and two new graph MCP tools.

**Architecture:** Pure graph helpers (`degree_counts`, `degrees_by_name`, `shortest_path`) go in `graph.py`. `run_recall` gains optional gating params driven by a pure `_select_frontier` helper; the service computes a global hub threshold from the read-model degree distribution and injects a `degree_fn` + threshold + budget. Two thin MCP tools (`get_neighbors`, `memory_path`) expose 1-hop neighbors and a targeted bidirectional shortest path. The memcot bench is reworked to drive the real `run_recall` with a hub fixture.

**Tech Stack:** Python 3.10+, NetworkX (already a dependency), pytest. Postgres read-model accessed read-only via `storage.load_graph()`.

## Global Constraints

- Offline baseline: `HF_HUB_OFFLINE=1`, CPU only — no network, no GPU. (Tests set these via conftest.)
- No new third-party dependencies — NetworkX is already vendored in.
- No DB schema change / migration — degree and path are read-only over existing edges; new `RecallConfig` knobs are dataclass defaults that load from existing YAML.
- Backward compatibility is a hard requirement: with gating disabled (`degree_fn=None`), `run_recall` output must be byte-identical to today. The existing `tests/test_recall.py` suite (11 tests) must stay green.
- Spec: [docs/specs/2026-06-24-recall-hub-gating-design.md](2026-06-24-recall-hub-gating-design.md).

---

## File Structure

- `pseudolife_memory/graph.py` — **modify**: add pure helpers `degree_counts`, `degrees_by_name`, `shortest_path`.
- `pseudolife_memory/memory/recall.py` — **modify**: add `_select_frontier`, `_hub_threshold`, and gating params to `run_recall`.
- `pseudolife_memory/utils/config.py` — **modify**: add four `RecallConfig` knobs.
- `pseudolife_memory/service.py` — **modify**: `_graph_degrees`, `graph_path`, `recall()` wiring + `top_k`/`default_hops` clamps.
- `pseudolife_memory/mcp_server.py` — **modify**: `get_neighbors`, `memory_path` tools.
- `evals/memcot_bench.py` — **modify**: 3-arm rework, RecallState adapter, `shared-config` hub fixture, `entities` metric.
- Tests: `tests/test_graph.py`, `tests/test_recall.py`, `tests/test_service.py`.

---

## Task 1: Pure graph helpers (degree + shortest path)

**Files:**
- Modify: `pseudolife_memory/graph.py` (add three module functions near `build_subgraph`)
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: nothing (pure, stdlib + networkx).
- Produces:
  - `degree_counts(edges: list[dict]) -> dict[int, int]` — undirected asserted degree by entity id. `edges` items have `src_id`, `dst_id`.
  - `degrees_by_name(edges: list[dict], entities: list[dict]) -> dict[str, int]` — degree keyed by `display` name. `entities` items have `id`, `display`.
  - `shortest_path(edges: list[dict], src_id: int, dst_id: int, *, max_hops: int = 8) -> list[int] | None` — node-id path inclusive of both ends, or `None` when no path or shortest path exceeds `max_hops` edges.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_graph.py`:

```python
from pseudolife_memory import graph as G


def test_degree_counts_undirected():
    edges = [
        {"src_id": 1, "dst_id": 2},
        {"src_id": 1, "dst_id": 3},
        {"src_id": 2, "dst_id": 3},
    ]
    assert G.degree_counts(edges) == {1: 2, 2: 2, 3: 2}


def test_degree_counts_empty():
    assert G.degree_counts([]) == {}


def test_degrees_by_name_maps_display():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3}]
    entities = [
        {"id": 1, "display": "hub"},
        {"id": 2, "display": "a"},
        {"id": 3, "display": "b"},
    ]
    assert G.degrees_by_name(edges, entities) == {"hub": 2, "a": 1, "b": 1}


def test_shortest_path_direct():
    edges = [{"src_id": 1, "dst_id": 2}]
    assert G.shortest_path(edges, 1, 2) == [1, 2]


def test_shortest_path_two_hop():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}]
    assert G.shortest_path(edges, 1, 3) == [1, 2, 3]


def test_shortest_path_same_node():
    assert G.shortest_path([{"src_id": 1, "dst_id": 2}], 1, 1) == [1]


def test_shortest_path_no_path():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 3, "dst_id": 4}]
    assert G.shortest_path(edges, 1, 4) is None


def test_shortest_path_exceeds_max_hops():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3},
             {"src_id": 3, "dst_id": 4}]
    assert G.shortest_path(edges, 1, 4, max_hops=2) is None
    assert G.shortest_path(edges, 1, 4, max_hops=3) == [1, 2, 3, 4]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graph.py -k "degree or shortest_path" -v`
Expected: FAIL with `AttributeError: module 'pseudolife_memory.graph' has no attribute 'degree_counts'`.

- [ ] **Step 3: Implement the helpers**

Add to `pseudolife_memory/graph.py` (it already does `import networkx as nx`):

```python
def degree_counts(edges: list[dict]) -> dict[int, int]:
    """Undirected degree per entity id over asserted edges.

    Each edge adds 1 to both endpoints. Derived/transitive edges are NOT
    counted (they are re-derived on read) — degree reflects asserted
    connectivity, the stable signal hub-gating wants.
    """
    deg: dict[int, int] = {}
    for e in edges:
        s, d = e["src_id"], e["dst_id"]
        deg[s] = deg.get(s, 0) + 1
        deg[d] = deg.get(d, 0) + 1
    return deg


def degrees_by_name(edges: list[dict], entities: list[dict]) -> dict[str, int]:
    """Asserted undirected degree keyed by entity display name."""
    by_id = {e["id"]: e["display"] for e in entities}
    out: dict[str, int] = {}
    for eid, d in degree_counts(edges).items():
        name = by_id.get(eid)
        if name:
            out[name] = d
    return out


def shortest_path(edges: list[dict], src_id: int, dst_id: int, *,
                  max_hops: int = 8) -> list[int] | None:
    """Targeted bidirectional shortest path (undirected) between two ids.

    Returns the node-id path inclusive of both ends, or None when no path
    exists or the shortest path exceeds ``max_hops`` edges. Searches toward
    the target, so cost is bounded by path length, not branching factor.
    """
    if src_id == dst_id:
        return [src_id]
    g = nx.Graph()
    for e in edges:
        g.add_edge(e["src_id"], e["dst_id"])
    if src_id not in g or dst_id not in g or not nx.has_path(g, src_id, dst_id):
        return None
    path = nx.bidirectional_shortest_path(g, src_id, dst_id)
    if len(path) - 1 > max_hops:
        return None
    return path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_graph.py -k "degree or shortest_path" -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/graph.py tests/test_graph.py
git commit -m "feat(graph): pure degree + bidirectional shortest-path helpers"
```

---

## Task 2: Hub-gated frontier in run_recall

**Files:**
- Modify: `pseudolife_memory/memory/recall.py`
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: nothing new (pure orchestration; gating is injected).
- Produces:
  - `_select_frontier(frontier: list[str], seed_set: set[str], degree_fn: Callable[[str], int] | None, hub_threshold: int | None, expand_budget: int | None) -> list[str]`
  - `_hub_threshold(degrees, percentile: float, floor: int) -> int`
  - `run_recall(..., *, hops=3, top_k=5, max_entities=50, degree_fn=None, hub_threshold=None, expand_budget=None)` — three new keyword-only params; when `degree_fn is None`, behavior is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recall.py` (the file already has `from pseudolife_memory.memory import recall as rc` and a `_FakeSvc` whose `.graph(entity, depth)` returns neighbor nodes). These tests inject `degree_fn` explicitly, exactly as the service will:

```python
def _hub_svc():
    # S-B, S-H, B-T(gold); H is a degree-5 hub fanning to X1..X4.
    edges = [("S", "r", "B"), ("S", "r", "H"), ("B", "r", "T"),
             ("H", "r", "X1"), ("H", "r", "X2"), ("H", "r", "X3"), ("H", "r", "X4")]
    snippets = ["S relates to B and H"]
    return _FakeSvc(snippets, edges)


_HUB_DEGREE = {"S": 2, "B": 2, "H": 5, "T": 1,
               "X1": 1, "X2": 1, "X3": 1, "X4": 1}


def test_hub_included_but_not_expanded():
    svc = _hub_svc()
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "B", "H", "T", "X1", "X2", "X3", "X4"],
        query="about S", controller=rc.MechanicalController(),
        hops=3, degree_fn=_HUB_DEGREE.get, hub_threshold=4, expand_budget=None)
    ents = set(state.entities)
    assert "H" in ents          # hub still surfaced as a result
    assert "T" in ents          # gold still reached via the non-hub branch
    assert "X1" not in ents     # hub NOT expanded through — no blast radius


def test_no_gating_pulls_in_hub_neighbors():
    svc = _hub_svc()
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "B", "H", "T", "X1", "X2", "X3", "X4"],
        query="about S", controller=rc.MechanicalController(), hops=3)  # degree_fn=None
    assert "X1" in set(state.entities)  # un-gated expansion fans out through H


def test_seed_that_is_a_hub_still_expands():
    # Seed S is itself a degree-5 hub; seed exemption must let it expand to T.
    edges = [("S", "r", "T"), ("S", "r", "A"), ("S", "r", "B"),
             ("S", "r", "C"), ("S", "r", "D")]
    svc = _FakeSvc(["S relates to things"], edges)
    deg = {"S": 5, "T": 1, "A": 1, "B": 1, "C": 1, "D": 1}
    state = rc.run_recall(
        svc.search, svc.graph, vocab=["S", "T", "A", "B", "C", "D"],
        query="about S", controller=rc.MechanicalController(),
        hops=2, degree_fn=deg.get, hub_threshold=3)
    assert "T" in set(state.entities)


def test_select_frontier_orders_and_budgets():
    frontier = ["c", "a", "b"]               # none are seeds
    deg = {"a": 5, "b": 1, "c": 3}
    out = rc._select_frontier(frontier, set(), deg.get, hub_threshold=100,
                              expand_budget=2)
    assert out == ["b", "c"]                  # ascending degree, capped at 2


def test_select_frontier_seeds_exempt_from_gate_and_budget():
    frontier = ["seed", "x", "y"]
    deg = {"seed": 99, "x": 1, "y": 1}
    out = rc._select_frontier(frontier, {"seed"}, deg.get, hub_threshold=10,
                              expand_budget=1)
    assert out[0] == "seed"                   # seed always present, never gated
    assert set(out) == {"seed", "x"} or set(out) == {"seed", "y"}
    assert len(out) == 2                       # seed + 1 budgeted non-seed


def test_select_frontier_off_is_identity():
    frontier = ["c", "a", "b"]
    assert rc._select_frontier(frontier, set(), None, None, None) == ["c", "a", "b"]


def test_hub_threshold_percentile_and_floor():
    # All low-degree -> percentile lands at 1, floor wins.
    assert rc._hub_threshold([1, 1, 1, 1, 1], percentile=95.0, floor=4) == 4
    # A clear hub -> percentile (50) exceeds the floor and wins.
    assert rc._hub_threshold([1, 2, 3, 50], percentile=95.0, floor=2) == 50
    # Empty distribution -> floor.
    assert rc._hub_threshold([], percentile=95.0, floor=7) == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_recall.py -k "hub or select_frontier or threshold" -v`
Expected: FAIL — `AttributeError: ... has no attribute '_select_frontier'` and `run_recall() got an unexpected keyword argument 'degree_fn'`.

- [ ] **Step 3: Add the helpers**

Add to `pseudolife_memory/memory/recall.py` (after `_add_edge`):

```python
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
```

- [ ] **Step 4: Add the gating params and apply them in the loop**

In `run_recall`, change the signature:

```python
def run_recall(search_fn: Callable, graph_fn: Callable, vocab: list[str],
               query: str, controller: RecallController, *,
               hops: int = 3, top_k: int = 5,
               max_entities: int = 50,
               degree_fn: Callable[[str], int] | None = None,
               hub_threshold: int | None = None,
               expand_budget: int | None = None) -> RecallState:
```

Immediately after `seen: set[str] = set(state.seeds)` (and the `max_entities` seed-trim block), add:

```python
    seed_set = set(state.seeds)
```

Inside the `while` loop, replace the line `for name in frontier:` with:

```python
        for name in _select_frontier(frontier, seed_set, degree_fn,
                                      hub_threshold, expand_budget):
```

Everything else in the loop body is unchanged.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_recall.py -v`
Expected: PASS — the new gating tests plus all 11 pre-existing tests (backward compat).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/recall.py tests/test_recall.py
git commit -m "feat(recall): hub-gated frontier with degree ordering + per-hop budget"
```

---

## Task 3: Config knobs + service wiring

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (`RecallConfig`)
- Modify: `pseudolife_memory/service.py` (`recall()`, new `_graph_degrees`)
- Test: `tests/test_recall.py` (config defaults + PG integration)

> **Integration-test convention (verified against the existing suite):** graph-touching tests need the bench Postgres. `tests/test_recall.py` already defines module-level `_ADMIN` + `_pg_up()` and uses `@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")` with `build_service(tmp_path)` imported from `evals/ladder_sweep` (see `test_recall_bridges_two_hop_on_real_service`). Reuse that exact pattern — do **not** use the `pristine_service` fixture (it does not back the graph store). Tests skip cleanly when the bench DB is down; the pure unit tests are the gate that always runs.

**Interfaces:**
- Consumes: `graph.degrees_by_name` (Task 1); `run_recall`, `_hub_threshold` (Task 2).
- Produces: `RecallConfig` fields `hub_gate: bool`, `hub_percentile: float`, `hub_floor: int`, `expand_budget: int`; `MemoryService._graph_degrees() -> dict[str, int]`; `recall()` injects gating.

- [ ] **Step 1: Add the config knobs**

In `pseudolife_memory/utils/config.py`, in `RecallConfig` (currently ends with `max_entities: int = 50`), add:

```python
    # Hub-gating (graphify-derived): include high-degree hubs as results but
    # don't expand THROUGH them. hub_floor / expand_budget are bench-tuned.
    hub_gate: bool = True
    hub_percentile: float = 95.0
    hub_floor: int = 8
    expand_budget: int = 0   # per-hop expansion cap; 0 = unlimited
```

- [ ] **Step 2: Write the failing config test**

Add to `tests/test_recall.py`:

```python
def test_recall_config_hub_defaults():
    from pseudolife_memory.utils.config import RecallConfig
    c = RecallConfig()
    assert c.hub_gate is True
    assert c.hub_percentile == 95.0
    assert c.hub_floor == 8
    assert c.expand_budget == 0
```

Run: `python -m pytest tests/test_recall.py::test_recall_config_hub_defaults -v`
Expected: PASS immediately (defaults added in Step 1). This guards against accidental default drift.

- [ ] **Step 3: Add `_graph_degrees` and wire `recall()`**

In `pseudolife_memory/service.py`, add the method (place near `_recall_vocab`):

```python
    def _graph_degrees(self) -> dict[str, int]:
        """Asserted undirected degree by display name, from the read-model.
        Short locked read; released before the lock-free recall loop."""
        from pseudolife_memory.graph import degrees_by_name
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {}
            g = self._storage.load_graph()
        return degrees_by_name(g["edges"], g["entities"])
```

Update the import in `recall()` to pull in `_hub_threshold`:

```python
        from pseudolife_memory.memory.recall import (
            LLMController, MechanicalController, run_recall, simple_complete,
            _hub_threshold,
        )
```

The method currently has these three lines:

```python
        cfg = self.config.memory.recall
        hops = cfg.default_hops if hops is None else max(1, min(int(hops), 5))
        top_k = cfg.default_top_k if top_k is None else int(top_k)
```

Leave the `cfg = ...` line and the `driver = ...` line that follows untouched;
replace only the `hops`/`top_k` lines with clamped versions (this folds in the
deferred minors — clamping `default_hops` to 5 and `top_k` to ≥ 1):

```python
        hops = (max(1, min(int(cfg.default_hops), 5)) if hops is None
                else max(1, min(int(hops), 5)))
        top_k = (cfg.default_top_k if top_k is None else max(1, int(top_k)))
```

Replace the `run_recall(...)` call with the gating-injected version:

```python
        degrees = self._graph_degrees() if cfg.hub_gate else {}
        threshold = (_hub_threshold(degrees.values(), cfg.hub_percentile,
                                    cfg.hub_floor) if cfg.hub_gate else None)
        state = run_recall(
            self.search, self.graph_neighborhood, vocab, query, controller,
            hops=hops, top_k=top_k, max_entities=cfg.max_entities,
            degree_fn=(degrees.get if cfg.hub_gate else None),
            hub_threshold=threshold,
            expand_budget=(cfg.expand_budget or None),
        )
```

- [ ] **Step 4: Write the failing integration test**

Append to `tests/test_recall.py`, alongside the existing PG integration tests
(reuse the module-level `_pg_up` already defined there). A shared seed helper
avoids duplicating the graph setup across the two tests:

```python
def _seed_hub_graph(svc):
    # checkout -> billing -> jdk-21 (gold), plus a shared-config hub that many
    # heads depend on (degree 6), fanning out to unrelated services.
    svc.graph_relate("checkout-service", "depends-on", "billing-engine")
    svc.graph_relate("billing-engine", "runs-on", "jdk-21")
    svc.graph_relate("checkout-service", "depends-on", "shared-config")
    for head in ("order-service", "web-portal", "mobile-app",
                 "analytics-ui", "notify-service"):
        svc.graph_relate(head, "depends-on", "shared-config")


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_hub_gating_keeps_gold_drops_blast_radius(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_hub_graph(svc)
    svc.config.memory.recall.hub_gate = True
    svc.config.memory.recall.hub_floor = 3       # shared-config has degree 6
    out = svc.recall("What does checkout-service run on?", hops=3)
    names = {e["entity"] for e in out["entities"]}
    assert "jdk-21" in names                      # gold still reached
    assert "order-service" not in names           # hub not expanded through


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_recall_no_gating_pulls_in_hub_siblings(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_hub_graph(svc)
    svc.config.memory.recall.hub_gate = False
    out = svc.recall("What does checkout-service run on?", hops=3)
    names = {e["entity"] for e in out["entities"]}
    assert "order-service" in names               # un-gated fan-out through hub
```

Run: `python -m pytest tests/test_recall.py -k "hub_gating or hub_siblings" -v`
Expected: PASS when the bench Postgres is up (gating on keeps `jdk-21`, drops
`order-service`; gating off pulls it in); SKIPPED when the bench DB is
unreachable. The `build_service` tmp DB is per-test, so no cross-test
pollution.

- [ ] **Step 5: Run the recall suite (unit + PG integration)**

Run: `python -m pytest tests/test_recall.py -v`
Expected: PASS — config-default test + the pre-existing unit/PG tests; the two
new hub-gating tests PASS with the bench DB up or SKIP when it is down.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(recall): wire hub-gating through RecallConfig + service.recall"
```

---

## Task 4: graph_path service method + MCP tools

**Files:**
- Modify: `pseudolife_memory/service.py` (`graph_path`)
- Modify: `pseudolife_memory/mcp_server.py` (`get_neighbors`, `memory_path`)
- Test: `tests/test_graph.py` (`graph_path` via the existing module-scoped `svc`
  fixture, which builds a `MemoryService(database_url=pg_url)`); `tests/test_recall.py`
  (the two MCP tools, via `build_service` + `monkeypatch.setattr(srv, "service", svc)`,
  mirroring the existing `test_memory_recall_tool_delegates`).

> Both files' graph tests require the bench Postgres and skip without it.
> `test_graph.py`'s `svc` fixture is module-scoped (shared), so use unique
> entity names per test to avoid cross-test edges.

**Interfaces:**
- Consumes: `graph.shortest_path` (Task 1); existing `storage.find_entity`, `storage.load_graph`, `graph.norm_name`, `service.graph_neighborhood`, `service._GRAPH_UNAVAILABLE`.
- Produces: `MemoryService.graph_path(source, target, max_hops=8) -> dict`; MCP tools `get_neighbors`, `memory_path`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph.py` (uses the existing module-scoped `svc` fixture;
unique `gp-*` names avoid collisions with other tests sharing that service).
The relations `depends-on` and `runs-on` are already registered in this suite:

```python
def test_graph_path_returns_chain(svc):
    svc.graph_relate("gp-a", "depends-on", "gp-b")
    svc.graph_relate("gp-b", "depends-on", "gp-c")
    out = svc.graph_path("gp-a", "gp-c")
    assert out["found"] is True
    assert out["path"] == ["gp-a", "gp-b", "gp-c"]
    assert out["hops"] == 2
    assert out["edges"][0]["src"] == "gp-a" and out["edges"][0]["dst"] == "gp-b"


def test_graph_path_missing_endpoint(svc):
    svc.graph_relate("gp-d", "depends-on", "gp-e")
    out = svc.graph_path("gp-d", "gp-nope")
    assert out["found"] is False
    assert out["missing"] == "gp-nope"


def test_graph_path_no_path_within_hops(svc):
    svc.graph_relate("gp-f", "depends-on", "gp-g")
    svc.graph_relate("gp-g", "depends-on", "gp-h")
    out = svc.graph_path("gp-f", "gp-h", max_hops=1)
    assert out["found"] is True
    assert out["path"] == [] and out["hops"] is None
```

Run: `python -m pytest tests/test_graph.py -k graph_path -v`
Expected (bench PG up): FAIL — `AttributeError: 'MemoryService' object has no
attribute 'graph_path'`. (SKIP if the bench DB is down — bring it up to drive
this task.)

- [ ] **Step 2: Implement `graph_path`**

In `pseudolife_memory/service.py`, add (place near `graph_neighborhood`):

```python
    def graph_path(self, source: str, target: str,
                   max_hops: int = 8) -> dict[str, Any]:
        """Targeted shortest path between two entities (how A connects to C).

        Bidirectional BFS over the read-model; ``max_hops`` is a path-length
        cutoff. Read-only. Returns ``{found, path, edges, hops, source,
        target}`` — ``path=[]`` / ``hops=None`` when no path within max_hops.
        """
        from pseudolife_memory import graph as Gmod
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            s = st.find_entity(Gmod.norm_name(source))
            t = st.find_entity(Gmod.norm_name(target))
            if s is None:
                return {"found": False, "missing": source}
            if t is None:
                return {"found": False, "missing": target}
            g = st.load_graph()
        by_id = {e["id"]: e for e in g["entities"]}
        rel: dict[tuple[int, int], str] = {}
        for e in g["edges"]:
            rel[(e["src_id"], e["dst_id"])] = e["relation"]
        node_path = Gmod.shortest_path(g["edges"], s["id"], t["id"],
                                       max_hops=max_hops)
        if node_path is None:
            return {"found": True, "path": [], "edges": [], "hops": None,
                    "source": source, "target": target}
        labels = [by_id[nid]["display"] for nid in node_path]
        edges = []
        for a, b in zip(node_path, node_path[1:]):
            if (a, b) in rel:
                edges.append({"src": by_id[a]["display"],
                              "relation": rel[(a, b)],
                              "dst": by_id[b]["display"]})
            elif (b, a) in rel:
                edges.append({"src": by_id[b]["display"],
                              "relation": rel[(b, a)],
                              "dst": by_id[a]["display"]})
        return {"found": True, "path": labels, "edges": edges,
                "hops": len(node_path) - 1, "source": source, "target": target}
```

- [ ] **Step 3: Run the graph_path test to verify it passes**

Run: `python -m pytest tests/test_graph.py -k graph_path -v`
Expected: PASS (3 tests) with the bench PG up.

- [ ] **Step 4: Add the MCP tools**

In `pseudolife_memory/mcp_server.py`, add two `@mcp.tool()` functions (near `memory_graph`):

```python
@mcp.tool()
def get_neighbors(entity: str, relation_filter: str | None = None) -> dict[str, Any]:
    """Direct (1-hop) neighbors of an entity, with typed edges.

    A focused shortcut for ``memory_graph(entity, depth=1)`` — use it for
    "what is X directly connected to?". Optional ``relation_filter`` keeps
    only edges whose relation contains that substring (case-insensitive).
    """
    out = service.graph_neighborhood(entity, depth=1)
    if relation_filter and out.get("edges"):
        rf = relation_filter.lower()
        out = dict(out)
        out["edges"] = [e for e in out["edges"]
                        if rf in str(e.get("relation", "")).lower()]
    return out


@mcp.tool()
def memory_path(source: str, target: str, max_hops: int = 8) -> dict[str, Any]:
    """Shortest path between two entities — how ``source`` connects to
    ``target``. Returns the entity chain and the typed edges along it, or an
    empty path when none exists within ``max_hops``. Read-only.
    """
    return service.graph_path(source, target, max_hops=max_hops)
```

- [ ] **Step 5: Write + run the MCP tool tests**

Add to `tests/test_recall.py` (the tools live in `mcp_server`; test them via the
real module with `monkeypatch.setattr(srv, "service", svc)`, mirroring the
existing `test_memory_recall_tool_delegates`). These cover the only
tool-specific logic: `get_neighbors`' relation filter and `memory_path`'s
delegation:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_get_neighbors_relation_filter(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.graph_relate("gnx", "depends-on", "gny")
    svc.graph_relate("gnx", "runs-on", "gnz")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.get_neighbors("gnx", relation_filter="depends-on")
    rels = {e["relation"] for e in out["edges"]}
    assert rels == {"depends-on"}                 # runs-on filtered out


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_path_tool_delegates(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.graph_relate("mp-a", "depends-on", "mp-b")
    svc.graph_relate("mp-b", "depends-on", "mp-c")
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_path("mp-a", "mp-c")
    assert out["path"] == ["mp-a", "mp-b", "mp-c"] and out["hops"] == 2
```

Run: `python -m pytest tests/test_recall.py -k "get_neighbors or memory_path" -v`
Expected: PASS with the bench PG up (SKIP without it).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py tests/test_graph.py tests/test_recall.py
git commit -m "feat(graph): graph_path + memory_path/get_neighbors MCP tools"
```

---

## Task 5: Bench rework — drive the real run_recall with a hub fixture

**Files:**
- Modify: `evals/memcot_bench.py`
- Validation: manual run (dev-only harness, not CI).

**Interfaces:**
- Consumes: `service.recall` (real path, Task 3); `build_service` from `evals/ladder_sweep`.
- Produces: 3-arm comparison (`baseline` / `recall_nogate` / `recall_gate`) with `recall`, `mean_tokens`, `mean_entities` per arm; a `shared-config` hub fixture.

- [ ] **Step 1: Add the hub fixture to the corpus**

In `evals/memcot_bench.py`, append to `CORPUS` (faithful snippets that co-mention the hub):

```python
    # hub fixture: shared-config is depended on by many heads (high degree).
    {"snippet": "The checkout-service reads its limits from shared-config.",
     "edges": [("checkout-service", "depends-on", "shared-config")]},
    {"snippet": "The order-service reads feature flags from shared-config.",
     "edges": [("order-service", "depends-on", "shared-config")]},
    {"snippet": "The web-portal loads its theme from shared-config.",
     "edges": [("web-portal", "depends-on", "shared-config")]},
    {"snippet": "The mobile-app fetches toggles from shared-config.",
     "edges": [("mobile-app", "depends-on", "shared-config")]},
    {"snippet": "The analytics-ui reads dashboards config from shared-config.",
     "edges": [("analytics-ui", "depends-on", "shared-config")]},
    {"snippet": "The notify-service reads templates from shared-config.",
     "edges": [("notify-service", "depends-on", "shared-config")]},
```

`shared-config` now has degree 6, well above a small floor. The existing
2-hop question "What does the checkout-service run on? → jdk-21" becomes the
hub-adjacent demonstrator (checkout-service neighbors = {billing-engine,
shared-config}).

- [ ] **Step 2: Add a RecallState adapter**

Add to `evals/memcot_bench.py`:

```python
def assembled_from_recall(result: dict) -> list[str]:
    """Flatten service.recall output into the scorer's context list:
    texts + per-entity facts + entity names (mirrors assembled_context)."""
    out = list(result.get("texts", []))
    for ent in result.get("entities", []):
        for f in ent.get("facts", []):
            out.append(f"{f.get('attribute')}={f.get('value')}")
        out.append(ent.get("entity", ""))
    return [s for s in out if s]


def recall_record(result: dict, gold: str, hops: int) -> dict:
    ctx = assembled_from_recall(result)
    return {
        "hops": hops,
        "recovered": any(value_present(s, gold) for s in ctx),
        "iterations": result.get("iterations", 0),
        "tokens": sum(approx_tokens(s) for s in ctx),
        "entities": len(result.get("entities", [])),
        "latency_ms": 0.0,
    }
```

- [ ] **Step 3: Add the `entities` mean to the aggregator**

In `_means`, add an `entities` average alongside the existing keys:

```python
    return {
        "n": n,
        "recall": round(sum(1 for r in recs if r["recovered"]) / n, 3),
        "mean_iterations": round(sum(r["iterations"] for r in recs) / n, 2),
        "mean_tokens": round(sum(r["tokens"] for r in recs) / n, 1),
        "mean_entities": round(sum(r.get("entities", 0) for r in recs) / n, 2),
        "mean_latency_ms": round(sum(r["latency_ms"] for r in recs) / n, 1),
    }
```

(The empty-`recs` branch should also gain `"mean_entities": 0.0`.)

- [ ] **Step 4: Replace the arms in `run_all` to drive real `run_recall`**

Replace the per-question arm construction in `run_all` so the two loop arms
call `svc.recall` with gating off and on (baseline stays single-shot):

```python
def run_all(svc, *, top_k: int = 5, hop_cap: int = 3) -> dict:
    base_recs, nogate_recs, gate_recs = [], [], []
    gate_fire = 0
    cfg = svc.config.memory.recall
    cfg.hub_floor = 3          # shared-config (deg 6) is a hub; chain heads are not
    for q in QUESTIONS:
        question, gold, hops = q["question"], q["gold"], q["hops"]
        base = run_baseline(svc, question, top_k=top_k)
        if would_gate(base):
            gate_fire += 1
        base_recs.append({"hops": hops, "recovered": gold_recovered(base, gold),
                          "iterations": base.iterations, "tokens": tokens_read(base),
                          "entities": 0, "latency_ms": base.latency_ms})
        cfg.hub_gate = False
        nogate_recs.append(recall_record(
            svc.recall(question, hops=hop_cap, top_k=top_k), gold, hops))
        cfg.hub_gate = True
        gate_recs.append(recall_record(
            svc.recall(question, hops=hop_cap, top_k=top_k), gold, hops))
    base_agg = aggregate(base_recs)
    nogate_agg = aggregate(nogate_recs)
    gate_agg = aggregate(gate_recs)
    return {
        "baseline": base_agg, "recall_nogate": nogate_agg, "recall_gate": gate_agg,
        "gate_would_fire": gate_fire, "questions": len(QUESTIONS),
        "recall_delta": round(
            gate_agg["overall"]["recall"] - nogate_agg["overall"]["recall"], 3),
        "tokens_saved": round(
            nogate_agg["overall"]["mean_tokens"] - gate_agg["overall"]["mean_tokens"], 1),
        "entities_saved": round(
            nogate_agg["overall"]["mean_entities"] - gate_agg["overall"]["mean_entities"], 2),
    }
```

Replace `report()` with the three-arm version:

```python
def report(results: dict) -> None:
    arms = [("baseline", "single-shot search"),
            ("recall_nogate", "recall, gate off"),
            ("recall_gate", "recall, gate on")]
    hdr = f"{'arm':<24}{'recall':>8}{'iters':>7}{'tok/q':>8}{'ents/q':>8}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for key, label in arms:
        o = results[key]["overall"]
        print(f"{label:<24}{o['recall']:>8}{o['mean_iterations']:>7}"
              f"{o['mean_tokens']:>8}{o['mean_entities']:>8}")
    print("\nby hop-class (recall):")
    print(f"{'arm':<24}{'1-hop':>8}{'2-hop':>8}{'3-hop':>8}")
    for key, label in arms:
        bh = results[key]["by_hops"]
        cells = "".join(f"{bh.get(h, {}).get('recall', '—'):>8}" for h in (1, 2, 3))
        print(f"{label:<24}{cells}")
    print(f"\nrecall_delta (gate - nogate): {results['recall_delta']}  "
          f"(must be 0.0 — no regression)")
    print(f"tokens_saved:   {results['tokens_saved']}")
    print(f"entities_saved: {results['entities_saved']}")
    print(f"gate would fire on {results['gate_would_fire']}/{results['questions']} "
          f"questions")
```

- [ ] **Step 5: Run the bench and check the success criteria**

Run: `python evals/memcot_bench.py --run`
Expected (the verifiable target):
1. `recall_delta == 0.0` — no recall regression (gate-on recall == gate-off).
2. `entities_saved > 0` and `tokens_saved > 0` — blast radius drops on the
   hub-adjacent question.
3. Record the observed numbers in the commit message; if criterion 1 fails,
   stop and investigate (do not tune the floor to mask a recall regression).

- [ ] **Step 6: Tune and pin the defaults**

Using the bench, confirm a `hub_floor` that gates `shared-config` (deg 6) but
not legitimate chain heads, and an `expand_budget` that holds recall flat.
Set the chosen values as the `RecallConfig` defaults in
`pseudolife_memory/utils/config.py` (replace the placeholders from Task 3,
Step 1). Re-run `python -m pytest tests/test_recall.py tests/test_service.py`.

- [ ] **Step 7: Commit**

```bash
git add evals/memcot_bench.py pseudolife_memory/utils/config.py
git commit -m "bench(recall): drive real run_recall + hub fixture; pin hub-gate defaults"
```

---

## Final verification

- [ ] Run the full suite: `python -m pytest -q`
- [ ] Confirm `tests/test_recall.py`, `tests/test_graph.py`, `tests/test_service.py` all pass.
- [ ] Confirm the bench prints `recall_delta == 0.0` with `tokens_saved`/`entities_saved` > 0.
- [ ] Update `CHANGELOG.md` with a one-line entry under the unreleased section (hub-gated recall + `memory_path`/`get_neighbors` tools).

---

## Self-review notes (coverage against spec)

- Gating algorithm (spec §1) → Task 2 (`_select_frontier`, run_recall params).
- Data flow / display-name keying (spec §2) → Task 1 (`degrees_by_name`) + Task 3 (`_graph_degrees`, wiring).
- Deferred minors (top_k / default_hops clamps) → Task 3, Step 3.
- Config knobs (spec §3) → Task 3, Step 1; defaults pinned in Task 5, Step 6.
- MCP tools (spec §4) → Task 4 (`graph_path`, `memory_path`, `get_neighbors`).
- Bench rework + success criteria (spec Validation) → Task 5.
- Testing matrix (spec Testing) → Tasks 1–4 unit/integration tests; Task 5 manual bench.
- Edge cases (spec) → covered: empty/sparse graph (`_hub_threshold` empty→floor; degrees `{}`→threshold floor→inert), unknown entity degree 0 (`degree_fn(n) or 0`), all-hubs frontier (others empties → expansion stops), `max_entities` backstop unchanged, deterministic `(degree, name)` tiebreak.
