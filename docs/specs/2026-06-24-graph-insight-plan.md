# Graph Insight Layer Implementation Plan (Track B v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute topology insight (communities, god-nodes, surprising-connections, suggested-questions) over the entity graph during `dream`, persist communities + a digest, and surface them via MCP tools.

**Architecture:** A pure, DB-free analytics module (`graph_insight.py`, Louvain via NetworkX) produces a community partition + digest from `(asserted edges, entities, prior assignment, contested facts)`. The service persists communities to two new tables + the digest JSON to `meta`, driven from inside `dream_run`. Read-only MCP tools expose the results; `memory_graph` is enriched with per-node community.

**Tech Stack:** Python 3.10+, NetworkX (already vendored — Louvain), psycopg3, Postgres, pytest.

## Global Constraints

- Offline baseline: `HF_HUB_OFFLINE=1`, CPU only. No new mandatory dependency — Louvain via `networkx.community.louvain_communities`; graspologic Leiden is optional (auto-used only if importable, else fall back to Louvain).
- Test runner: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest ...` (the venv has psycopg; bare `python` does not). Bench Postgres is at `127.0.0.1:5433`.
- PG integration tests use the established pattern: module-level `_pg_up()` skipif + `build_service(tmp_path)` from `evals/ladder_sweep` (see `tests/test_recall.py`); the `svc` fixture in `tests/test_graph.py` for graph-level. NOT `pristine_service` (no graph store).
- Analytics run over **asserted** edges (`storage.load_graph`): edge dicts are `{id, src_id, relation, dst_id, confidence, origin, asserted_at}` — `confidence` float, `origin` in {user, action, agent, ...}; NO `derived` flag. Entities are `{id, canonical, display, etype}`.
- The analytics module is pure: stdlib + networkx only, no DB, no service imports. Determinism: Louvain `seed=42`; community IDs re-indexed size-desc with a `tuple(sorted(ids))` tiebreak.
- Additive only: no destructive schema ALTER (new tables are `CREATE TABLE IF NOT EXISTS`); existing tests stay green; `memory_search`/existing graph/recall behavior untouched.
- Spec: [docs/specs/2026-06-24-graph-insight-design.md](2026-06-24-graph-insight-design.md).

## Module / file structure

- `pseudolife_memory/memory/graph_insight.py` — **new**, pure analytics.
- `pseudolife_memory/utils/config.py` — **modify**: `GraphInsightConfig`.
- `pseudolife_memory/storage/schema.py` — **modify**: two tables + version bump.
- `pseudolife_memory/storage/postgres.py` — **modify**: `replace_communities`, `load_communities`, `get_meta`, `set_meta`.
- `pseudolife_memory/service.py` — **modify**: `_refresh_graph_insight`, dream hook, `communities()`/`graph_digest()` reads, `graph_neighborhood` enrichment, `stats` count.
- `pseudolife_memory/mcp_server.py` — **modify**: `memory_digest`, `memory_communities`.
- Tests: `tests/test_graph_insight.py` (new, pure) + additions to `tests/test_graph.py` / `tests/test_recall.py` / a schema test.

## Shared interfaces (used across tasks — names/types are fixed here)

```python
# graph_insight.py — all pure
detect_communities(edges: list[dict], *, resolution: float = 1.0,
                   max_community_fraction: float = 0.25,
                   algorithm: str = "louvain") -> dict[int, list[int]]
cohesion_score(edges: list[dict], member_ids: list[int]) -> float
remap_to_previous(communities: dict[int, list[int]],
                  prior: dict[int, int]) -> dict[int, list[int]]
summarize_communities(communities: dict[int, list[int]], edges: list[dict],
                      entities: list[dict]) -> list[dict]   # [{id,label,size,cohesion}]
god_nodes(edges: list[dict], entities: list[dict], *, top_n: int = 10,
          exclude_etypes: tuple[str, ...] = ()) -> list[dict]   # [{entity_id,display,degree}]
surprising_connections(edges: list[dict], entities: list[dict],
                       node_community: dict[int, int], *, top_n: int = 10) -> list[dict]
suggest_questions(edges: list[dict], entities: list[dict],
                  communities: dict[int, list[int]], node_community: dict[int, int],
                  contested_facts: list[dict], summaries: list[dict], *,
                  top_n: int = 7, betweenness_sample: int = 200) -> list[dict]
build_digest(communities: dict[int, list[int]], summaries: list[dict],
             edges: list[dict], entities: list[dict], contested_facts: list[dict],
             computed_at: float, *, god_top_n: int = 10, surprises_top_n: int = 10,
             questions_top_n: int = 7, betweenness_sample: int = 200) -> dict

# storage/postgres.py
replace_communities(assignment: dict[int, int], summaries: list[dict],
                    computed_at: float) -> None
load_communities() -> dict   # {"assignment": {entity_id: community_id}, "communities": [..]}
get_meta(key: str) -> object | None
set_meta(key: str, value) -> None

# contested_facts item shape (built by the service from cortex_records):
#   {"entity": str, "attribute": str, "value": str,
#    "contender_value": str, "contender_origin": str}
```

---

## Task 1: Community detection (pure)

**Files:**
- Create: `pseudolife_memory/memory/graph_insight.py`
- Test: `tests/test_graph_insight.py`

**Interfaces:**
- Produces: `detect_communities`, `cohesion_score`, `remap_to_previous`, `summarize_communities`, and a private `_node_community(communities) -> dict[int,int]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_graph_insight.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pseudolife_memory.memory import graph_insight as gi  # noqa: E402


def _two_clusters():
    # {1,2,3} clique and {4,5,6} clique joined by a single bridge 3-4.
    return [
        {"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}, {"src_id": 1, "dst_id": 3},
        {"src_id": 4, "dst_id": 5}, {"src_id": 5, "dst_id": 6}, {"src_id": 4, "dst_id": 6},
        {"src_id": 3, "dst_id": 4},
    ]


def test_detect_two_communities():
    comms = gi.detect_communities(_two_clusters())
    # Two clusters recovered; ids are size-desc, 0-indexed.
    assert set(comms.keys()) == {0, 1}
    members = sorted(sorted(v) for v in comms.values())
    assert members == [[1, 2, 3], [4, 5, 6]]


def test_detect_empty():
    assert gi.detect_communities([]) == {}


def test_cohesion_score_full_triangle():
    # 3 nodes, 3 edges = complete -> cohesion 1.0
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3}, {"src_id": 1, "dst_id": 3}]
    assert gi.cohesion_score(edges, [1, 2, 3]) == 1.0
    assert gi.cohesion_score(edges, [1]) == 1.0          # singleton


def test_remap_to_previous_keeps_ids_stable():
    # New partition identical to prior but with permuted temp ids -> prior ids restored.
    communities = {0: [4, 5, 6], 1: [1, 2, 3]}
    prior = {1: 7, 2: 7, 3: 7, 4: 9, 5: 9, 6: 9}
    remapped = gi.remap_to_previous(communities, prior)
    # {1,2,3} carried prior id 7; {4,5,6} carried prior id 9.
    assert {min(v): k for k, v in remapped.items()} == {1: 7, 4: 9}


def test_remap_first_run_no_prior():
    communities = {0: [1, 2], 1: [3]}
    assert gi.remap_to_previous(communities, {}) == {0: [1, 2], 1: [3]}


def test_summarize_communities_labels_by_top_degree():
    edges = _two_clusters()
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 7)]
    comms = gi.detect_communities(edges)
    summ = gi.summarize_communities(comms, edges, entities)
    assert {s["id"] for s in summ} == set(comms.keys())
    for s in summ:
        assert s["size"] >= 1 and 0.0 <= s["cohesion"] <= 1.0 and s["label"].startswith("e")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pseudolife_memory.memory.graph_insight'`.

- [ ] **Step 3: Implement the module**

Create `pseudolife_memory/memory/graph_insight.py`:

```python
"""Pure topology analytics over the asserted entity graph (Track B).

DB-free and unit-testable like recall.py/graph.py: the service supplies
edges/entities/prior-assignment/contested-facts and persists the results.
Louvain via networkx by default; graspologic Leiden used only if importable.
"""
from __future__ import annotations

import networkx as nx

from pseudolife_memory.graph import degree_counts

_MAX_COMMUNITY_FRACTION = 0.25
_MIN_SPLIT_SIZE = 10


def _undirected(edges: list[dict]) -> nx.Graph:
    g = nx.Graph()
    for e in edges:
        g.add_edge(e["src_id"], e["dst_id"])
    return g


def _partition(g: nx.Graph, *, resolution: float, algorithm: str) -> list[set]:
    """Return a list of node-sets. Leiden if asked-for AND importable, else Louvain."""
    if algorithm == "leiden":
        try:
            from graspologic.partition import leiden
            result = leiden(g, resolution=resolution, random_seed=42, trials=1)
            groups: dict[int, set] = {}
            for node, cid in result.items():
                groups.setdefault(cid, set()).add(node)
            return list(groups.values())
        except Exception:
            pass  # fall back to Louvain
    return list(nx.community.louvain_communities(g, seed=42, resolution=resolution))


def _reindex(groups: list[list[int]]) -> dict[int, list[int]]:
    """Size-desc, total-ordered (tuple(sorted(ids)) tiebreak) -> {cid: [ids]}."""
    groups = [sorted(map(int, g)) for g in groups]
    groups.sort(key=lambda ids: (-len(ids), tuple(ids)))
    return {i: ids for i, ids in enumerate(groups)}


def detect_communities(edges: list[dict], *, resolution: float = 1.0,
                       max_community_fraction: float = 0.25,
                       algorithm: str = "louvain") -> dict[int, list[int]]:
    if not edges:
        return {}
    g = _undirected(edges)
    n = g.number_of_nodes()
    groups = [list(s) for s in _partition(g, resolution=resolution, algorithm=algorithm)]
    # Split oversized communities with a second partition pass on the subgraph.
    max_size = max(_MIN_SPLIT_SIZE, int(n * max_community_fraction))
    final: list[list[int]] = []
    for members in groups:
        if len(members) > max_size and g.subgraph(members).number_of_edges() > 0:
            sub = _partition(g.subgraph(members), resolution=resolution, algorithm=algorithm)
            final.extend([list(s) for s in sub] if len(sub) > 1 else [members])
        else:
            final.append(members)
    return _reindex(final)


def cohesion_score(edges: list[dict], member_ids: list[int]) -> float:
    n = len(member_ids)
    if n <= 1:
        return 1.0
    members = set(member_ids)
    actual = sum(1 for e in edges
                 if e["src_id"] in members and e["dst_id"] in members
                 and e["src_id"] != e["dst_id"])
    possible = n * (n - 1) / 2
    return actual / possible if possible else 0.0


def remap_to_previous(communities: dict[int, list[int]],
                      prior: dict[int, int]) -> dict[int, list[int]]:
    """Greedy overlap match: each new community inherits the prior community id it
    most overlaps; unmatched get fresh ids in deterministic (size-desc) order."""
    if not prior:
        return communities
    old_sets: dict[int, set] = {}
    for node, oid in prior.items():
        old_sets.setdefault(oid, set()).add(node)
    overlaps = []
    for new_cid, ids in communities.items():
        s = set(ids)
        for old_cid, old in old_sets.items():
            inter = len(s & old)
            if inter:
                overlaps.append((inter, old_cid, new_cid))
    overlaps.sort(key=lambda x: (-x[0], x[1], x[2]))
    new_to_final: dict[int, int] = {}
    used_old: set[int] = set()
    matched_new: set[int] = set()
    for _inter, old_cid, new_cid in overlaps:
        if old_cid in used_old or new_cid in matched_new:
            continue
        new_to_final[new_cid] = old_cid
        used_old.add(old_cid)
        matched_new.add(new_cid)
    unmatched = [c for c in communities if c not in matched_new]
    unmatched.sort(key=lambda c: (-len(communities[c]), tuple(sorted(communities[c]))))
    nxt = 0
    for new_cid in unmatched:
        while nxt in used_old:
            nxt += 1
        new_to_final[new_cid] = nxt
        used_old.add(nxt)
        nxt += 1
    return {new_to_final[c]: sorted(ids) for c, ids in communities.items()}


def _node_community(communities: dict[int, list[int]]) -> dict[int, int]:
    return {n: cid for cid, ids in communities.items() for n in ids}


def summarize_communities(communities: dict[int, list[int]], edges: list[dict],
                          entities: list[dict]) -> list[dict]:
    """Per-community {id, label, size, cohesion}. Label = highest-degree member's display."""
    deg = degree_counts(edges)
    disp = {e["id"]: e["display"] for e in entities}
    out = []
    for cid, ids in sorted(communities.items()):
        top = max(ids, key=lambda i: (deg.get(i, 0), i)) if ids else None
        out.append({
            "id": cid,
            "label": disp.get(top, str(top)) if top is not None else f"community-{cid}",
            "size": len(ids),
            "cohesion": round(cohesion_score(edges, ids), 4),
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_insight.py tests/test_graph_insight.py
git commit -m "feat(graph-insight): community detection + cohesion + stable remap"
```

---

## Task 2: God-nodes + surprising-connections (pure)

**Files:**
- Modify: `pseudolife_memory/memory/graph_insight.py`
- Test: `tests/test_graph_insight.py`

**Interfaces:**
- Consumes: `degree_counts`, `_node_community` (Task 1).
- Produces: `god_nodes`, `surprising_connections`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_insight.py`:

```python
def test_god_nodes_ranks_by_degree():
    # hub=1 connects to 2,3,4; leaf edge 5-6.
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3},
             {"src_id": 1, "dst_id": 4}, {"src_id": 5, "dst_id": 6}]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 7)]
    gods = gi.god_nodes(edges, entities, top_n=2)
    assert gods[0]["entity_id"] == 1 and gods[0]["degree"] == 3
    assert gods[0]["display"] == "e1" and len(gods) == 2


def test_god_nodes_excludes_etype():
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 1, "dst_id": 3}]
    entities = [{"id": 1, "display": "hub", "etype": "structural"},
                {"id": 2, "display": "a", "etype": None},
                {"id": 3, "display": "b", "etype": None}]
    gods = gi.god_nodes(edges, entities, top_n=5, exclude_etypes=("structural",))
    assert all(g["entity_id"] != 1 for g in gods)


def test_surprising_connections_flags_cross_community_bridge():
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 4, "dst_id": 5, "relation": "uses", "confidence": 0.9, "origin": "user"},
        # bridge between the two communities, agent-inferred + low confidence
        {"src_id": 2, "dst_id": 4, "relation": "relates-to", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 6)]
    node_comm = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}
    out = gi.surprising_connections(edges, entities, node_comm, top_n=5)
    assert out and out[0]["src"] == "e2" and out[0]["dst"] == "e4"
    assert "community" in out[0]["why"].lower()
    assert out[0]["origin"] == "agent"


def test_surprising_connections_dedup_by_community_pair():
    # Two edges between the same community pair -> only one representative kept.
    edges = [
        {"src_id": 1, "dst_id": 3, "relation": "r", "confidence": 0.5, "origin": "agent"},
        {"src_id": 2, "dst_id": 4, "relation": "r", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 5)]
    node_comm = {1: 0, 2: 0, 3: 1, 4: 1}
    out = gi.surprising_connections(edges, entities, node_comm, top_n=5)
    assert len(out) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -k "god or surprising" -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'god_nodes'`.

- [ ] **Step 3: Implement**

Append to `pseudolife_memory/memory/graph_insight.py`:

```python
_LOW_CONFIDENCE = 0.6
_PERIPHERAL_DEGREE = 2


def god_nodes(edges: list[dict], entities: list[dict], *, top_n: int = 10,
              exclude_etypes: tuple[str, ...] = ()) -> list[dict]:
    deg = degree_counts(edges)
    excluded = {e["id"] for e in entities if e.get("etype") in exclude_etypes}
    disp = {e["id"]: e["display"] for e in entities}
    ranked = sorted(
        ((eid, d) for eid, d in deg.items() if eid not in excluded),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [{"entity_id": eid, "display": disp.get(eid, str(eid)), "degree": d}
            for eid, d in ranked[:top_n]]


def surprising_connections(edges: list[dict], entities: list[dict],
                           node_community: dict[int, int], *,
                           top_n: int = 10) -> list[dict]:
    deg = degree_counts(edges)
    disp = {e["id"]: e["display"] for e in entities}
    god_cutoff = _god_degree_cutoff(deg)
    scored = []
    for e in edges:
        u, v = e["src_id"], e["dst_id"]
        if u == v:
            continue
        score, reasons = 0, []
        if (e.get("confidence", 1.0) < _LOW_CONFIDENCE) or (e.get("origin") == "agent"):
            score += 2
            reasons.append("agent-inferred or low-confidence")
        cu, cv = node_community.get(u), node_community.get(v)
        cross = cu is not None and cv is not None and cu != cv
        if cross:
            score += 3
            reasons.append(f"bridge between community {cu} and {cv}")
        du, dv = deg.get(u, 0), deg.get(v, 0)
        if min(du, dv) <= _PERIPHERAL_DEGREE and max(du, dv) >= god_cutoff:
            score += 1
            reasons.append("peripheral node reaches a hub")
        if score <= 0:
            continue
        pair = tuple(sorted((cu, cv))) if cross else None
        scored.append({
            "src": disp.get(u, str(u)), "dst": disp.get(v, str(v)),
            "relation": e.get("relation", ""), "confidence": e.get("confidence"),
            "origin": e.get("origin"), "score": score,
            "why": "; ".join(reasons), "_pair": pair,
        })
    scored.sort(key=lambda s: (-s["score"], s["src"], s["dst"]))
    seen_pairs: set = set()
    out = []
    for s in scored:
        pair = s.pop("_pair")
        if pair is not None:
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
        out.append(s)
        if len(out) >= top_n:
            break
    return out


def _god_degree_cutoff(deg: dict[int, int]) -> int:
    """Degree at/above which a node counts as a 'hub' for peripheral->hub. The
    10th-highest degree, floored at 5 (mirrors the god_nodes top-N notion)."""
    if not deg:
        return 5
    top = sorted(deg.values(), reverse=True)
    return max(5, top[min(len(top) - 1, 9)])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_insight.py tests/test_graph_insight.py
git commit -m "feat(graph-insight): god-nodes + provenance-adapted surprising-connections"
```

---

## Task 3: Suggested questions + digest (pure)

**Files:**
- Modify: `pseudolife_memory/memory/graph_insight.py`
- Test: `tests/test_graph_insight.py`

**Interfaces:**
- Consumes: `god_nodes`, `surprising_connections`, `summarize_communities`, `_node_community`, `cohesion_score` (Tasks 1–2).
- Produces: `suggest_questions`, `build_digest`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_insight.py`:

```python
def test_suggest_questions_contested_fact():
    contested = [{"entity": "postgres", "attribute": "port", "value": "5433",
                  "contender_value": "5432", "contender_origin": "agent"}]
    qs = gi.suggest_questions([], [], {}, {}, contested, [], top_n=5)
    assert any(q["type"] == "contested_fact" and "5433" in q["question"]
               and "5432" in q["question"] for q in qs)


def test_suggest_questions_isolated_entity():
    edges = [{"src_id": 1, "dst_id": 2, "relation": "r", "confidence": 0.9, "origin": "user"}]
    entities = [{"id": 1, "display": "a", "etype": None},
                {"id": 2, "display": "b", "etype": None},
                {"id": 3, "display": "lonely", "etype": None}]  # degree 0
    comms = {0: [1, 2], 1: [3]}
    qs = gi.suggest_questions(edges, entities, comms, gi._node_community(comms), [], [], top_n=5)
    assert any(q["type"] == "isolated_entity" and "lonely" in q["question"] for q in qs)


def test_build_digest_assembles_all_sections():
    edges = [
        {"src_id": 1, "dst_id": 2, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 2, "dst_id": 3, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 1, "dst_id": 3, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 4, "dst_id": 5, "relation": "uses", "confidence": 0.9, "origin": "user"},
        {"src_id": 3, "dst_id": 4, "relation": "x", "confidence": 0.5, "origin": "agent"},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 6)]
    comms = gi.detect_communities(edges)
    summ = gi.summarize_communities(comms, edges, entities)
    digest = gi.build_digest(comms, summ, edges, entities, [], 123.0)
    assert digest["computed_at"] == 123.0
    assert digest["totals"] == {"entities": 5, "edges": 5, "communities": len(comms)}
    assert {"communities", "god_nodes", "surprises", "questions"} <= set(digest)
    assert digest["god_nodes"][0]["degree"] >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -k "suggest or digest" -v`
Expected: FAIL — `AttributeError: ... 'suggest_questions'`.

- [ ] **Step 3: Implement**

Append to `pseudolife_memory/memory/graph_insight.py`:

```python
_LOW_COHESION = 0.15
_MIN_COHESION_SIZE = 5
_AGENT_EDGE_MIN = 2


def suggest_questions(edges: list[dict], entities: list[dict],
                      communities: dict[int, list[int]], node_community: dict[int, int],
                      contested_facts: list[dict], summaries: list[dict], *,
                      top_n: int = 7, betweenness_sample: int = 200) -> list[dict]:
    disp = {e["id"]: e["display"] for e in entities}
    deg = degree_counts(edges)
    label = {s["id"]: s["label"] for s in summaries}
    questions: list[dict] = []

    # 1. contested facts (our richest signal)
    for c in contested_facts:
        questions.append({
            "type": "contested_fact",
            "question": (f"Which value of `{c['attribute']}` for `{c['entity']}` is "
                         f"correct — `{c['value']}` or `{c['contender_value']}`?"),
            "why": f"Contested fact; rival from origin={c.get('contender_origin')}.",
        })

    g = _undirected(edges)
    # 2. bridge entities (high betweenness spanning >=2 communities)
    if g.number_of_edges():
        k = betweenness_sample if (betweenness_sample and g.number_of_nodes() > betweenness_sample) else None
        bc = nx.betweenness_centrality(g, k=k, seed=42)
        bridges = sorted(((n, s) for n, s in bc.items() if s > 0),
                         key=lambda kv: (-kv[1], kv[0]))
        for n, _s in bridges[:3]:
            cid = node_community.get(n)
            other = {node_community.get(nb) for nb in g.neighbors(n)} - {cid, None}
            if other:
                names = ", ".join(label.get(c, f"community {c}") for c in sorted(other))
                questions.append({
                    "type": "bridge_entity",
                    "question": f"Why does `{disp.get(n, n)}` connect `{label.get(cid, cid)}` to {names}?",
                    "why": "High betweenness — a cross-community bridge.",
                })

    # 3. verify a god-node with many agent-origin edges
    for gnode in god_nodes(edges, entities, top_n=5):
        eid = gnode["entity_id"]
        agent_n = sum(1 for e in edges
                      if (e["src_id"] == eid or e["dst_id"] == eid) and e.get("origin") == "agent")
        if agent_n >= _AGENT_EDGE_MIN:
            questions.append({
                "type": "verify_inferred",
                "question": f"Are the {agent_n} inferred relationships involving `{gnode['display']}` correct?",
                "why": f"{agent_n} agent-origin (dream-inferred) edges need verification.",
            })
            break

    # 4. isolated entities (degree <= 1)
    isolated = [e["display"] for e in entities if deg.get(e["id"], 0) <= 1]
    if isolated:
        shown = ", ".join(f"`{n}`" for n in isolated[:3])
        questions.append({
            "type": "isolated_entity",
            "question": f"What connects {shown} to the rest of the graph?",
            "why": f"{len(isolated)} weakly-connected entities — possible gaps.",
        })

    # 5. low-cohesion communities
    for s in summaries:
        if s["cohesion"] < _LOW_COHESION and s["size"] >= _MIN_COHESION_SIZE:
            questions.append({
                "type": "low_cohesion",
                "question": f"Should community `{s['label']}` be split into tighter groups?",
                "why": f"Cohesion {s['cohesion']} over {s['size']} entities.",
            })

    return questions[:top_n]


def build_digest(communities: dict[int, list[int]], summaries: list[dict],
                 edges: list[dict], entities: list[dict], contested_facts: list[dict],
                 computed_at: float, *, god_top_n: int = 10, surprises_top_n: int = 10,
                 questions_top_n: int = 7, betweenness_sample: int = 200) -> dict:
    node_comm = _node_community(communities)
    return {
        "computed_at": computed_at,
        "communities": summaries,
        "god_nodes": god_nodes(edges, entities, top_n=god_top_n),
        "surprises": surprising_connections(edges, entities, node_comm, top_n=surprises_top_n),
        "questions": suggest_questions(edges, entities, communities, node_comm,
                                       contested_facts, summaries, top_n=questions_top_n,
                                       betweenness_sample=betweenness_sample),
        "totals": {"entities": len(entities), "edges": len(edges),
                   "communities": len(communities)},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_insight.py tests/test_graph_insight.py
git commit -m "feat(graph-insight): suggested-questions + digest assembly"
```

---

## Task 4: Config + schema v12

**Files:**
- Modify: `pseudolife_memory/utils/config.py`
- Modify: `pseudolife_memory/storage/schema.py`
- Test: `tests/test_graph_insight.py` (config), `tests/test_schema_v12.py` (new, PG)

**Interfaces:**
- Produces: `GraphInsightConfig`; `communities` + `entity_communities` tables; `SCHEMA_META_VERSION == 12`.

- [ ] **Step 1: Add `GraphInsightConfig`**

In `pseudolife_memory/utils/config.py`, add the dataclass near `RecallConfig`:

```python
@dataclass
class GraphInsightConfig:
    """Topology analytics computed during dream (Track B). Communities persisted;
    god-nodes/surprises/questions stored as the meta['graph_digest'] snapshot."""
    enabled: bool = True
    algorithm: str = "louvain"          # "louvain" | "leiden" (leiden needs graspologic; falls back)
    resolution: float = 1.0
    max_community_fraction: float = 0.25
    god_nodes_top_n: int = 10
    surprises_top_n: int = 10
    questions_top_n: int = 7
    betweenness_sample: int = 200       # k-sample betweenness above this node count (0 = exact)
```

And add the field to `MemoryConfig` (alongside `recall`):

```python
    graph_insight: GraphInsightConfig = field(default_factory=GraphInsightConfig)
```

- [ ] **Step 2: Write the failing config test**

Append to `tests/test_graph_insight.py`:

```python
def test_graph_insight_config_defaults():
    from pseudolife_memory.utils.config import GraphInsightConfig
    c = GraphInsightConfig()
    assert c.enabled is True and c.algorithm == "louvain"
    assert c.resolution == 1.0 and c.max_community_fraction == 0.25
    assert c.betweenness_sample == 200
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py::test_graph_insight_config_defaults -v`
Expected: PASS (after Step 1).

- [ ] **Step 3: Add the schema tables + bump version**

In `pseudolife_memory/storage/schema.py`: change `SCHEMA_META_VERSION = 11` to `12`. Append to `SCHEMA_SQL` (before the closing `"""`), after the `outcome_signals` block:

```sql
CREATE TABLE IF NOT EXISTS communities (
  id          BIGINT PRIMARY KEY,
  label       TEXT,
  size        INTEGER NOT NULL,
  cohesion    DOUBLE PRECISION NOT NULL,
  computed_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_communities (
  entity_id    BIGINT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  community_id BIGINT NOT NULL,
  computed_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS entity_communities_cid_idx ON entity_communities (community_id);
```

- [ ] **Step 4: Write + run the PG schema test**

Create `tests/test_schema_v12.py`:

```python
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ADMIN = os.environ.get("PSEUDOLIFE_BENCH_ADMIN_URL",
                        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres")


def _pg_up() -> bool:
    try:
        import psycopg
        with psycopg.connect(_ADMIN, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_schema_v12_creates_community_tables(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    assert SCHEMA_META_VERSION == 12
    svc = build_service(tmp_path)
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage  # noqa: SLF001
    for tbl in ("communities", "entity_communities"):
        row = st.conn.execute(
            "SELECT to_regclass(%s)", (f"public.{tbl}",)).fetchone()
        assert row[0] is not None, f"{tbl} not created"
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_schema_v12.py tests/test_graph_insight.py -v`
Expected: PASS (schema test RAN against bench PG; both tables present; version 12).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/storage/schema.py tests/test_graph_insight.py tests/test_schema_v12.py
git commit -m "feat(graph-insight): GraphInsightConfig + schema v12 community tables"
```

---

## Task 5: Storage — community + meta persistence

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py`
- Test: `tests/test_graph.py` (uses its `svc` fixture's storage)

**Interfaces:**
- Consumes: schema v12 tables (Task 4).
- Produces: `replace_communities(assignment, summaries, computed_at)`, `load_communities()`, `get_meta(key)`, `set_meta(key, value)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph.py` (the module already has the `svc` fixture backed by Postgres; use the storage directly):

```python
def test_replace_and_load_communities(svc):
    st = svc._storage  # noqa: SLF001
    # Need real entity ids — create two entities via the public graph path.
    svc.graph_relate("ci-a", "depends-on", "ci-b")
    g = st.load_graph()
    ids = {e["display"]: e["id"] for e in g["entities"]}
    a, b = ids["ci-a"], ids["ci-b"]
    summaries = [{"id": 0, "label": "ci-a", "size": 2, "cohesion": 1.0}]
    st.replace_communities({a: 0, b: 0}, summaries, 100.0)
    loaded = st.load_communities()
    assert loaded["assignment"][a] == 0 and loaded["assignment"][b] == 0
    assert loaded["communities"][0]["label"] == "ci-a"
    # Replace is wholesale: a second call with fewer rows clears the old ones.
    st.replace_communities({a: 3}, [{"id": 3, "label": "ci-a", "size": 1, "cohesion": 1.0}], 101.0)
    loaded2 = st.load_communities()
    assert loaded2["assignment"] == {a: 3}


def test_get_set_meta_roundtrip(svc):
    st = svc._storage  # noqa: SLF001
    st.set_meta("graph_digest", {"computed_at": 5.0, "god_nodes": []})
    assert st.get_meta("graph_digest")["computed_at"] == 5.0
    assert st.get_meta("does-not-exist") is None
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k "communities or meta" -v`
Expected: FAIL — `AttributeError: 'PostgresStorage' object has no attribute 'replace_communities'`.

- [ ] **Step 2: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, add `import json` at the top if not present, and add these methods to `PostgresStorage` (near `load_graph`):

```python
    def replace_communities(self, assignment: dict[int, int],
                            summaries: list[dict], computed_at: float) -> None:
        """Wholesale replace the community partition (truncate + bulk insert).
        The shared entities hub is never touched."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM entity_communities")
            cur.execute("DELETE FROM communities")
            if summaries:
                cur.executemany(
                    "INSERT INTO communities (id, label, size, cohesion, computed_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(s["id"], s["label"], s["size"], s["cohesion"], computed_at)
                     for s in summaries],
                )
            if assignment:
                cur.executemany(
                    "INSERT INTO entity_communities (entity_id, community_id, computed_at) "
                    "VALUES (%s, %s, %s)",
                    [(eid, cid, computed_at) for eid, cid in assignment.items()],
                )
        self.conn.commit()

    def load_communities(self) -> dict:
        assignment = {
            eid: cid for eid, cid in self.conn.execute(
                "SELECT entity_id, community_id FROM entity_communities").fetchall()
        }
        cols = ("id", "label", "size", "cohesion", "computed_at")
        communities = [
            dict(zip(cols, r)) for r in self.conn.execute(
                f"SELECT {', '.join(cols)} FROM communities ORDER BY id").fetchall()
        ]
        return {"assignment": assignment, "communities": communities}

    def get_meta(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k "communities or meta" -v`
Expected: PASS (2 tests, ran against bench PG).

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_graph.py
git commit -m "feat(graph-insight): community + meta persistence on PostgresStorage"
```

---

## Task 6: Service — refresh, dream hook, reads, enrichment

**Files:**
- Modify: `pseudolife_memory/service.py`
- Test: `tests/test_recall.py` (PG integration via `build_service`)

**Interfaces:**
- Consumes: `graph_insight` (Tasks 1–3); storage methods (Task 5); `GraphInsightConfig` (Task 4).
- Produces: `MemoryService._refresh_graph_insight() -> dict`, `MemoryService.graph_digest() -> dict`, `MemoryService.communities(community_id=None) -> dict`; `dream_run` summary gains `graph_insight`; `graph_neighborhood` nodes gain `community`; `stats` gains `communities`.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_recall.py` (reuses module-level `_pg_up`):

```python
def _seed_two_communities(svc):
    # cluster 1: alpha-svc <-> alpha-db <-> alpha-cache (triangle)
    svc.graph_relate("alpha-svc", "depends-on", "alpha-db")
    svc.graph_relate("alpha-db", "depends-on", "alpha-cache")
    svc.graph_relate("alpha-svc", "depends-on", "alpha-cache")
    # cluster 2: beta-svc <-> beta-db
    svc.graph_relate("beta-svc", "depends-on", "beta-db")
    # bridge
    svc.graph_relate("alpha-cache", "relates-to", "beta-svc")


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_refresh_graph_insight_persists_and_is_stable(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    out = svc._refresh_graph_insight()  # noqa: SLF001
    assert out["refreshed"] is True and out["communities"] >= 2
    loaded = svc._storage.load_communities()  # noqa: SLF001
    assert len(loaded["assignment"]) >= 5            # entities stamped
    digest = svc.graph_digest()
    assert digest["available"] is True
    assert {"god_nodes", "surprises", "questions", "communities"} <= set(digest["digest"])
    # Stable ids: a second refresh with no graph change keeps the assignment.
    before = svc._storage.load_communities()["assignment"]  # noqa: SLF001
    svc._refresh_graph_insight()  # noqa: SLF001
    after = svc._storage.load_communities()["assignment"]  # noqa: SLF001
    assert before == after


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_graph_neighborhood_carries_community(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    out = svc.graph_neighborhood("alpha-svc", depth=1)
    node = next(n for n in out["nodes"] if n["entity"] == "alpha-svc")
    assert isinstance(node["community"], int)
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "graph_insight or carries_community" -v`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute '_refresh_graph_insight'`.

- [ ] **Step 2: Implement `_refresh_graph_insight` + reads**

In `pseudolife_memory/service.py`, add these methods to `MemoryService` (near `graph_neighborhood`):

```python
    def _contested_facts(self) -> list[dict]:
        """Contested cortex facts shaped for graph_insight.suggest_questions.
        Mirrors how cortex_search detects contention: current_records() +
        contenders_for(). CortexRecord exposes .entity/.attribute/.value."""
        out = []
        with self._lock:
            self._ensure_init()
            if self._cortex is None:
                return out
            for r in self._cortex.current_records():
                conts = self._cortex.contenders_for(r.entity, r.attribute)
                if conts:
                    out.append({
                        "entity": r.entity, "attribute": r.attribute, "value": r.value,
                        "contender_value": conts[0].value,
                        "contender_origin": conts[0].origin,
                    })
        return out

    def _refresh_graph_insight(self) -> dict[str, Any]:
        """Recompute communities + digest from the live graph and persist. Read
        inputs under the lock, compute lock-free, persist under the lock."""
        import time as _time
        from pseudolife_memory.memory import graph_insight as gi
        cfg = self.config.memory.graph_insight
        if not cfg.enabled:
            return {"refreshed": False, "reason": "disabled"}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"refreshed": False, "reason": "no_storage"}
            g = self._storage.load_graph()
            prior = self._storage.load_communities()["assignment"]
        if not g["edges"]:
            return {"refreshed": False, "reason": "empty_graph"}
        contested = self._contested_facts()
        communities = gi.detect_communities(
            g["edges"], resolution=cfg.resolution,
            max_community_fraction=cfg.max_community_fraction, algorithm=cfg.algorithm)
        communities = gi.remap_to_previous(communities, prior)
        summaries = gi.summarize_communities(communities, g["edges"], g["entities"])
        assignment = {eid: cid for cid, ids in communities.items() for eid in ids}
        computed_at = _time.time()
        digest = gi.build_digest(
            communities, summaries, g["edges"], g["entities"], contested, computed_at,
            god_top_n=cfg.god_nodes_top_n, surprises_top_n=cfg.surprises_top_n,
            questions_top_n=cfg.questions_top_n, betweenness_sample=cfg.betweenness_sample)
        with self._lock:
            self._storage.replace_communities(assignment, summaries, computed_at)
            self._storage.set_meta("graph_digest", digest)
        return {"refreshed": True, "communities": len(summaries)}

    def graph_digest(self) -> dict[str, Any]:
        """The persisted digest snapshot, or {available: False} if dream hasn't run."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"available": False, "reason": "no_storage"}
            digest = self._storage.get_meta("graph_digest")
        if not digest:
            return {"available": False, "reason": "no_digest"}
        return {"available": True, "digest": digest}

    def communities(self, community_id: int | None = None) -> dict[str, Any]:
        """List communities, or the members of one when community_id is given."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            loaded = self._storage.load_communities()
            g = self._storage.load_graph()
        disp = {e["id"]: e["display"] for e in g["entities"]}
        if community_id is None:
            return {"communities": loaded["communities"]}
        members = [disp.get(eid, str(eid)) for eid, cid in loaded["assignment"].items()
                   if cid == community_id]
        return {"community_id": community_id, "members": sorted(members)}
```

- [ ] **Step 3: Enrich `graph_neighborhood` + `stats`, and hook `dream_run`**

(a) In `graph_neighborhood`, load the community map under the existing lock (where `load_graph` is read) and add `community` to each node. Inside the `with self._lock:` block, after `st = self._storage`, add:

```python
            _comm = st.load_communities()["assignment"]
```

Then in the node-building loop, set the field on each node dict:

```python
                node["community"] = _comm.get(nid)
```

(b) In `stats`, the current body is `return self._cms.stats()` (inside `with self._lock:`). Replace that single `return` line with an assign-enrich-return that adds the community count (still inside the lock):

```python
            result = self._cms.stats()
            if self._storage is not None:
                _c = self._storage.load_communities()["communities"]
                result["communities"] = len(_c)
                result["graph_digest_at"] = (self._storage.get_meta("graph_digest") or {}).get("computed_at")
            return result
```

(c) In `dream_run`, after `relations_n = self._dream_extract_relations(extractor, texts)` (the main success path), add the refresh, guarded so it never breaks the dream, and include it in the summary:

```python
        try:
            graph_insight = self._refresh_graph_insight()
        except Exception as exc:  # noqa: BLE001 — insight must never break a dream
            logger.warning("graph-insight refresh failed (%s); dream unaffected", exc)
            graph_insight = {"refreshed": False, "error": str(exc)}
        return {"pulled": len(entries), "claims": len(claims),
                "cursor": newest, "relations": relations_n, **tally,
                "lessons": lessons, "graph_insight": graph_insight}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py tests/test_graph.py -v`
Expected: PASS — the two new integration tests RAN (communities persisted, IDs stable, community on nodes) plus all prior tests green.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(graph-insight): dream-driven refresh, community reads, graph/stats enrichment"
```

---

## Task 7: MCP tools

**Files:**
- Modify: `pseudolife_memory/mcp_server.py`
- Test: `tests/test_recall.py` (tool tests via `build_service` + monkeypatch)

**Interfaces:**
- Consumes: `service.graph_digest()`, `service.communities()` (Task 6).
- Produces: MCP tools `memory_digest`, `memory_communities`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_digest_tool(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    monkeypatch.setattr(srv, "service", svc, raising=False)
    out = srv.memory_digest()
    assert out["available"] is True and "god_nodes" in out["digest"]


@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_communities_tool(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    _seed_two_communities(svc)
    svc._refresh_graph_insight()  # noqa: SLF001
    monkeypatch.setattr(srv, "service", svc, raising=False)
    listing = srv.memory_communities()
    assert listing["communities"]
    members = srv.memory_communities(community_id=listing["communities"][0]["id"])
    assert "members" in members
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "memory_digest or memory_communities" -v`
Expected: FAIL — `AttributeError: module 'pseudolife_memory.mcp_server' has no attribute 'memory_digest'`.

- [ ] **Step 2: Implement the tools**

In `pseudolife_memory/mcp_server.py`, add two `@mcp.tool()` functions (near `memory_graph`):

```python
@mcp.tool()
def memory_digest() -> dict[str, Any]:
    """Topology digest of the knowledge graph as of the last dream: most-connected
    entities (god-nodes), surprising cross-community connections, and questions
    the graph is uniquely positioned to answer. Read-only; returns
    {available: false} until a dream has produced one.
    """
    return service.graph_digest()


@mcp.tool()
def memory_communities(community_id: int | None = None) -> dict[str, Any]:
    """List the graph's communities (clusters of related entities) with size and
    cohesion, or — given a community_id — the members of that community. Read-only.
    """
    return service.communities(community_id=community_id)
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "memory_digest or memory_communities" -v`
Expected: PASS (2 tests, ran against bench PG).

- [ ] **Step 4: Update docs + commit**

Add one line to `CHANGELOG.md` under `## [Unreleased]` → `### Added`:

```markdown
- **Graph insight layer** — `dream` now computes graph communities (persisted),
  god-nodes, surprising connections, and suggested questions. New read-only
  tools `memory_digest` and `memory_communities`; `memory_graph` nodes carry a
  `community` field.
```

```bash
git add pseudolife_memory/mcp_server.py tests/test_recall.py CHANGELOG.md
git commit -m "feat(graph-insight): memory_digest + memory_communities MCP tools"
```

---

## Final verification

- [ ] Full touched-suite run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_insight.py tests/test_schema_v12.py tests/test_graph.py tests/test_recall.py -q`
- [ ] Confirm `SCHEMA_META_VERSION == 12` and existing tests still pass.
- [ ] Confirm the new PG integration tests RAN (not skipped) — communities persisted, IDs stable, digest readable, tools return data.

---

## Self-review notes (coverage against spec)

- A community detection (Louvain + oversized split + stable remap) → Task 1.
- B god-nodes → Task 2. C surprising-connections (provenance-adapted, dedup-by-pair) → Task 2.
- D suggested-questions (contested / bridge / verify-agent / isolated / low-cohesion) → Task 3. Digest assembly + mechanical labels → Task 3.
- Schema v12 (communities + entity_communities) + `GraphInsightConfig` → Task 4.
- Persistence (`replace_communities` truncate+rewrite, `load_communities`, meta get/set) → Task 5.
- Dream hook + `_refresh_graph_insight` + reads + `graph_neighborhood`/`stats` enrichment → Task 6.
- MCP tools `memory_digest`/`memory_communities` → Task 7.
- Edge cases: empty graph (`detect_communities([]) == {}`; `_refresh` returns `empty_graph`), first run no prior (`remap` identity), leiden-without-graspologic fallback (`_partition` try/except), refresh failure isolated from dream (Task 6 try/except), `k`-sampled betweenness (Task 3), `ON DELETE CASCADE` (Task 4 DDL).
- Asserted-edge model (no `derived`): surprise/verify use `origin=="agent"` + `confidence<0.6` — Tasks 2/3.
