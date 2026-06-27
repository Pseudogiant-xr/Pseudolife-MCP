# Atlas Stage 3a — review analyzer + graph mutations (backend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the backend a read-only `graph_review` analyzer (duplicate/orphan/dubious-edge/test-artifact/unattributed findings) and four confirm-gated graph-mutation endpoints (assign-scope, unrelate, delete-entity, merge) so the Atlas workbench (Stage 3b) can surface and fix graph-hygiene issues.

**Architecture:** `graph_review.py` is a pure, DB-free analyzer module mirroring `graph_insight.py` (the service supplies edges/entities/entity_sources, it returns findings). Mutations are small, explicitly-enumerated service methods wrapping storage primitives, each taking the single shared lock like `graph_relate`/`graph_unrelate`. Destructive ops null the no-cascade `facts`/`lessons` FK refs before deleting an entity. No UI here — that's Stage 3b.

**Tech Stack:** Python 3.11, psycopg raw SQL, networkx (already used by graph_insight), the enumerated web route table; pytest under `.venv` against the dev Postgres.

## Global Constraints

- **Runner:** `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest <args> -v` (NOT bare python). Postgres up at `127.0.0.1:5433`.
- **Fixtures (already defined — do not redefine):** `storage` (= `PostgresStorage(pg_url)`) and module-scoped PG `svc` (= `MemoryService(database_url=pg_url)`) in `tests/test_graph.py`; file-mode `FixtureService` `svc` in `tests/test_web.py`. `svc` accrues state → unique names + subset/`>=` assertions. Pure-analyzer tests need no fixture (mirror `graph_insight` tests — plain dicts).
- **`graph_review.py` is read-only and DB-free** — pure functions over `edges`/`entities`/`entity_sources_map` dicts, like `graph_insight.py`. No storage imports.
- **Mutations are confirm-gated in the UI (Stage 3b) and enumerated server-side.** Each is one explicit service method under `self._lock`; none is a generic proxy.
- **`facts` and `lessons` FK `entities(id)` with NO `ON DELETE` action** — deleting/merging an entity MUST null (delete) or re-point (merge) `facts.entity_id`, `facts.object_entity_id`, `lessons.entity_id`, `lessons.object_entity_id` first, or the `DELETE` raises a FK violation. `edges`/`entity_aliases`/`entity_sources`/`entity_communities` ARE `ON DELETE CASCADE`.
- **`edges` has `UNIQUE (src_id, relation, dst_id)`** — re-pointing edges in a merge must drop would-be duplicates and self-loops first.
- No backend behaviour change to retrieval/dream/recall.

## File Structure

- `pseudolife_memory/memory/graph_review.py` — NEW. Pure analyzer (`review` + per-detector functions).
- `pseudolife_memory/service.py` — MODIFY. `graph_review`, `graph_assign_scope`, `graph_delete_entity`, `graph_merge` methods (near `graph_relate`/`graph_unrelate`).
- `pseudolife_memory/storage/postgres.py` — MODIFY. `delete_entity`, `merge_entity` primitives (near `add_alias`/`upsert_edge`).
- `pseudolife_memory/web/routes.py` — MODIFY. `GET /api/graph/review` + `POST /api/graph/{assign-scope,unrelate,delete-entity,merge}`.
- `pseudolife_memory/web/fixtures.py` — MODIFY. `graph_review`, `graph_assign_scope`, `graph_delete_entity`, `graph_merge` stubs.
- Tests: `tests/test_graph_review.py` (NEW, pure), `tests/test_graph.py` (mutations, PG), `tests/test_web.py` (routes).

---

### Task 1: `graph_review` analyzer + service + route

**Files:**
- Create: `pseudolife_memory/memory/graph_review.py`
- Create: `tests/test_graph_review.py`
- Modify: `pseudolife_memory/service.py` (new `graph_review`), `pseudolife_memory/web/routes.py`, `pseudolife_memory/web/fixtures.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `graph_review.review(edges, entities, entity_sources_map) -> {"findings": [...], "counts": {...}}`; each finding `{"type","severity","label","action", and "entities":[display,...] or "edges":[{src,relation,dst,confidence},...]}`. Service `graph_review(scope=None) -> same`. Route `GET /api/graph/review?scope=`.

- [ ] **Step 1: Write the failing pure-analyzer tests**

```python
# tests/test_graph_review.py
from pseudolife_memory.memory import graph_review as gr


def _ents(*names):
    return [{"id": i + 1, "display": n, "canonical": n.lower(), "etype": None}
            for i, n in enumerate(names)]


def test_duplicate_candidates_flags_near_identical_names():
    ents = _ents("Cortex Console web frontend", "web frontend (Cortex Console)", "postgres")
    dups = gr.duplicate_candidates(ents)
    assert dups and dups[0]["type"] == "duplicate" and dups[0]["action"] == "merge"
    assert "postgres" not in dups[0]["label"]


def test_test_artifacts_matches_known_patterns():
    ents = _ents("payments/payments-db", "pl-healthcheck-target", "pseudolife-mcp")
    arts = gr.test_artifacts(ents)
    assert arts and arts[0]["action"] == "delete"
    assert set(arts[0]["entities"]) == {"payments/payments-db", "pl-healthcheck-target"}


def test_orphans_flags_degree_le_1():
    ents = _ents("a", "b", "lonely")
    edges = [{"src_id": 1, "relation": "uses", "dst_id": 2, "origin": "action", "confidence": 0.9}]
    orph = gr.orphans(edges, ents)
    assert orph and "lonely" in orph[0]["entities"]


def test_dubious_edges_flags_low_conf_agent():
    ents = _ents("memory_recall", "docker-desktop")
    edges = [{"src_id": 1, "relation": "runs-on", "dst_id": 2, "origin": "agent", "confidence": 0.6}]
    dub = gr.dubious_edges(ents and edges, ents)
    assert dub and dub[0]["action"] == "prune" and dub[0]["edges"][0]["src"] == "memory_recall"


def test_unattributed_flags_entities_without_sources():
    ents = _ents("attributed", "orphan-of-project")
    un = gr.unattributed(ents, {1: ["pseudolife"]})
    assert un and un[0]["entities"] == ["orphan-of-project"] and un[0]["action"] == "assign"


def test_review_aggregates_all_groups():
    ents = _ents("payments-db", "lonely")
    out = gr.review([], ents, {})
    types = {f["type"] for f in out["findings"]}
    assert {"test_artifact", "orphan", "unattributed"} <= types
    assert out["counts"]["total"] == len(out["findings"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_review.py -v`
Expected: FAIL — `ModuleNotFoundError: ... graph_review`.

- [ ] **Step 3: Implement `graph_review.py`**

```python
# pseudolife_memory/memory/graph_review.py
"""Pure graph-hygiene analyzer (Atlas Stage 3). DB-free + unit-testable like
graph_insight.py: the service supplies edges/entities/entity_sources_map; this
returns review findings the Atlas workbench surfaces. READ-ONLY — no mutation."""
from __future__ import annotations

import re

from pseudolife_memory.graph import degree_counts

_DUBIOUS_CONF = 0.6
_TEST_PATTERNS = re.compile(
    r"(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent"
    r"|\btest-|-test\b|\bfixture\b)", re.I)


def _disp(entities):
    return {e["id"]: e["display"] for e in entities}


def _token_set(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if len(t) > 2}


def duplicate_candidates(entities, *, min_jaccard=0.6):
    toks = [(e["id"], e["display"], _token_set(e["display"])) for e in entities]
    out = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i][2], toks[j][2]
            if not a or not b:
                continue
            jac = len(a & b) / len(a | b)
            if jac >= min_jaccard:
                out.append({"type": "duplicate", "severity": "warn",
                            "label": f"{toks[i][1]} ↔ {toks[j][1]}",
                            "entities": [toks[i][1], toks[j][1]],
                            "score": round(jac, 3), "action": "merge"})
    out.sort(key=lambda f: -f["score"])
    return out


def orphans(edges, entities, *, max_degree=1):
    deg = degree_counts(edges)
    names = sorted(e["display"] for e in entities if deg.get(e["id"], 0) <= max_degree)
    if not names:
        return []
    return [{"type": "orphan", "severity": "info",
             "label": f"{len(names)} weakly-connected entities",
             "entities": names, "action": "review"}]


def dubious_edges(edges, entities, *, conf=_DUBIOUS_CONF):
    disp = _disp(entities)
    rows = [{"src": disp.get(e["src_id"], str(e["src_id"])),
             "relation": e.get("relation", ""),
             "dst": disp.get(e["dst_id"], str(e["dst_id"])),
             "confidence": e.get("confidence")}
            for e in edges
            if e.get("origin") == "agent" and (e.get("confidence") or 1.0) < conf]
    if not rows:
        return []
    return [{"type": "dubious_edge", "severity": "warn",
             "label": f"{len(rows)} low-confidence inferred edges",
             "edges": rows, "action": "prune"}]


def test_artifacts(entities):
    names = sorted(e["display"] for e in entities if _TEST_PATTERNS.search(e["display"]))
    if not names:
        return []
    return [{"type": "test_artifact", "severity": "warn",
             "label": f"{len(names)} test/smoke artifacts",
             "entities": names, "action": "delete"}]


def unattributed(entities, entity_sources_map):
    names = sorted(e["display"] for e in entities if e["id"] not in entity_sources_map)
    if not names:
        return []
    return [{"type": "unattributed", "severity": "info",
             "label": f"{len(names)} entities with no project",
             "entities": names, "action": "assign"}]


def review(edges, entities, entity_sources_map):
    findings = (duplicate_candidates(entities) + test_artifacts(entities)
                + dubious_edges(edges, entities) + orphans(edges, entities)
                + unattributed(entities, entity_sources_map))
    return {"findings": findings, "counts": {"total": len(findings)}}
```

- [ ] **Step 4: Run to verify pure tests pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_review.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Add the service method + route + fixture + a web test**

In `service.py`, near `graph_relate`:

```python
    def graph_review(self, scope: str | None = None) -> dict[str, Any]:
        from pseudolife_memory.memory import graph_review as gr
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"findings": [], "counts": {"total": 0}}
            g = self._storage.load_graph()
            src_map = self._storage.entity_sources_map()
        entities, edges = g["entities"], g["edges"]
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
            entities = [e for e in entities if e["id"] in keep]
            edges = [e for e in edges if e["src_id"] in keep and e["dst_id"] in keep]
        return gr.review(edges, entities, src_map)
```

In `routes.py`, in the `# ---- graph ----` block:

```python
        g("/api/graph/review", lambda q, b: svc.graph_review(scope=_s(q, "scope")))
```

In `fixtures.py`, add:

```python
    def graph_review(self, scope=None):
        return {"findings": [
            {"type": "duplicate", "severity": "warn", "action": "merge",
             "label": "Cortex Console web frontend ↔ web frontend (Cortex Console)",
             "entities": ["Cortex Console web frontend", "web frontend (Cortex Console)"]},
            {"type": "test_artifact", "severity": "warn", "action": "delete",
             "label": "2 test/smoke artifacts", "entities": ["payments-db", "pl-healthcheck-target"]},
            {"type": "dubious_edge", "severity": "warn", "action": "prune",
             "label": "1 low-confidence inferred edge",
             "edges": [{"src": "memory_recall", "relation": "runs-on", "dst": "docker-desktop", "confidence": 0.6}]},
            {"type": "unattributed", "severity": "info", "action": "assign",
             "label": "3 entities with no project", "entities": ["a", "b", "c"]},
        ], "counts": {"total": 4}}
```

In `tests/test_web.py`:

```python
def test_graph_review_route(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("GET", "/api/graph/review", {"scope": "all"}, {})
    assert "findings" in out and out["counts"]["total"] == len(out["findings"])
    assert any(f["action"] == "merge" for f in out["findings"])
```

- [ ] **Step 6: Run + commit**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph_review.py tests/test_web.py -k "review or graph" -v`
Expected: PASS.

```bash
git add pseudolife_memory/memory/graph_review.py tests/test_graph_review.py pseudolife_memory/service.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "feat(graph): graph_review analyzer + /api/graph/review"
```

---

### Task 2: assign-scope + unrelate mutations (routes + service)

**Files:**
- Modify: `pseudolife_memory/service.py` (new `graph_assign_scope`), `pseudolife_memory/web/routes.py`, `pseudolife_memory/web/fixtures.py`, `tests/test_graph.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `storage.find_entity`, `storage.upsert_entity_source` (Stage 1), existing `service.graph_unrelate`.
- Produces: `graph_assign_scope(entity, source) -> {"assigned": bool, "entity", "source"}`; routes `POST /api/graph/assign-scope` `{entity, source}` and `POST /api/graph/unrelate` `{src, relation, dst}`.

- [ ] **Step 1: Write the failing PG test (service)**

```python
# add to tests/test_graph.py (service section, uses svc)
def test_graph_assign_scope_writes_manual_source(svc):
    from pseudolife_memory import graph as G
    st = svc._storage
    svc.graph_relate("as-target", "uses", "as-other")   # creates the entity
    res = svc.graph_assign_scope("as-target", "as-proj")
    assert res["assigned"] is True
    eid = st.find_entity(G.norm_name("as-target"))["id"]
    rows = {r["source"]: r["origin"] for r in st.sources_for_entity(eid)}
    assert rows["as-proj"] == "manual"
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_assign_scope_writes_manual_source -v`
Expected: FAIL — `AttributeError: ... 'graph_assign_scope'`.

- [ ] **Step 3: Implement the service method + routes + fixtures**

In `service.py`:

```python
    def graph_assign_scope(self, entity: str, source: str) -> dict[str, Any]:
        from pseudolife_memory import graph as G
        import time as _time
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            e = self._storage.find_entity(G.norm_name(entity))
            if e is None:
                return {"assigned": False, "reason": "unknown_entity", "entity": entity}
            self._storage.upsert_entity_source(e["id"], source, "manual", _time.time())
        return {"assigned": True, "entity": e["display"], "source": source}
```

In `routes.py` (`# ---- graph ----` block):

```python
        p("/api/graph/assign-scope", lambda q, b: svc.graph_assign_scope(b["entity"], b["source"]))
        p("/api/graph/unrelate", lambda q, b: svc.graph_unrelate(b["src"], b["relation"], b["dst"]))
```

In `fixtures.py`:

```python
    def graph_assign_scope(self, entity, source):
        return {"assigned": True, "entity": entity, "source": source}

    def graph_unrelate(self, src, relation, dst):
        return {"removed": True, "src": src, "relation": relation, "dst": dst}
```

In `tests/test_web.py`:

```python
def test_assign_scope_and_unrelate_routes(svc):
    r = ConsoleRoutes(svc)
    a = r.dispatch("POST", "/api/graph/assign-scope", {}, {"entity": "x", "source": "p"})
    assert a["assigned"] is True
    u = r.dispatch("POST", "/api/graph/unrelate", {}, {"src": "a", "relation": "uses", "dst": "b"})
    assert u["removed"] is True
```

- [ ] **Step 4: Run to verify pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_assign_scope_writes_manual_source tests/test_web.py -k "assign or unrelate" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_graph.py tests/test_web.py
git commit -m "feat(graph): assign-scope + unrelate mutation routes"
```

---

### Task 3: delete-entity mutation

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new `delete_entity`), `pseudolife_memory/service.py` (new `graph_delete_entity`), `pseudolife_memory/web/routes.py`, `pseudolife_memory/web/fixtures.py`, `tests/test_graph.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `storage.delete_entity(entity_id) -> bool`; `service.graph_delete_entity(entity) -> {"deleted": bool, "entity"}`; route `POST /api/graph/delete-entity` `{entity}`.

- [ ] **Step 1: Write the failing PG test**

```python
# add to tests/test_graph.py (service section)
def test_graph_delete_entity_removes_node_and_edges(svc):
    from pseudolife_memory import graph as G
    st = svc._storage
    svc.graph_relate("del-victim", "uses", "del-bystander")
    svc.cortex_write("del-victim", "role", "junk", support="user")  # a fact references it (no-cascade FK)
    eid = st.find_entity(G.norm_name("del-victim"))["id"]

    res = svc.graph_delete_entity("del-victim")
    assert res["deleted"] is True
    assert st.find_entity(G.norm_name("del-victim")) is None
    # its edge is gone; the fact's entity_id was nulled (fact row may remain, unlinked)
    assert all(e["src_id"] != eid and e["dst_id"] != eid for e in st.load_graph()["edges"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_delete_entity_removes_node_and_edges -v`
Expected: FAIL — `AttributeError: ... 'graph_delete_entity'`.

- [ ] **Step 3: Implement storage + service + route + fixture**

In `postgres.py` (near `add_alias`):

```python
    def delete_entity(self, entity_id: int) -> bool:
        """Remove a graph entity. edges/aliases/sources/community are ON DELETE
        CASCADE; facts/lessons FK have NO cascade, so null those refs first
        (the fact/lesson rows survive, just unlinked from the deleted node)."""
        for tbl in ("facts", "lessons"):
            self.conn.execute(f"UPDATE {tbl} SET entity_id = NULL WHERE entity_id = %s", (entity_id,))
            self.conn.execute(f"UPDATE {tbl} SET object_entity_id = NULL WHERE object_entity_id = %s", (entity_id,))
        row = self.conn.execute(
            "DELETE FROM entities WHERE id = %s RETURNING id", (entity_id,)).fetchone()
        self.conn.commit()
        return row is not None
```

In `service.py`:

```python
    def graph_delete_entity(self, entity: str) -> dict[str, Any]:
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            e = self._storage.find_entity(G.norm_name(entity))
            if e is None:
                return {"deleted": False, "reason": "unknown_entity", "entity": entity}
            ok = self._storage.delete_entity(e["id"])
        return {"deleted": ok, "entity": e["display"]}
```

In `routes.py`: `p("/api/graph/delete-entity", lambda q, b: svc.graph_delete_entity(b["entity"]))`
In `fixtures.py`:

```python
    def graph_delete_entity(self, entity):
        return {"deleted": True, "entity": entity}
```

In `tests/test_web.py`:

```python
def test_delete_entity_route(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("POST", "/api/graph/delete-entity", {}, {"entity": "junk"})
    assert out["deleted"] is True
```

- [ ] **Step 4: Run to verify pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_delete_entity_removes_node_and_edges tests/test_web.py::test_delete_entity_route -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py pseudolife_memory/service.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_graph.py tests/test_web.py
git commit -m "feat(graph): delete-entity mutation (null no-cascade refs + cascade)"
```

---

### Task 4: merge mutation

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new `merge_entity`), `pseudolife_memory/service.py` (new `graph_merge`), `pseudolife_memory/web/routes.py`, `pseudolife_memory/web/fixtures.py`, `tests/test_graph.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `storage.find_entity`.
- Produces: `storage.merge_entity(from_id, into_id) -> bool`; `service.graph_merge(from_entity, into_entity) -> {"merged": bool, "from", "into"}`; route `POST /api/graph/merge` `{from, into}`.

- [ ] **Step 1: Write the failing PG test**

```python
# add to tests/test_graph.py (service section)
def test_graph_merge_folds_from_into(svc):
    from pseudolife_memory import graph as G
    st = svc._storage
    # `mg-from` and `mg-into` are the same thing stored twice; each has a distinct edge.
    svc.graph_relate("mg-from", "uses", "mg-dep")
    svc.graph_relate("mg-into", "stores-data-in", "mg-store")
    into_id = st.find_entity(G.norm_name("mg-into"))["id"]

    res = svc.graph_merge("mg-from", "mg-into")
    assert res["merged"] is True
    assert st.find_entity(G.norm_name("mg-from"))["id"] == into_id   # alias now resolves to into
    # into absorbed from's edge: an edge from `mg-into` to `mg-dep` now exists
    edges = st.load_graph()["edges"]
    assert any(e["src_id"] == into_id and st.load_graph() and True for e in edges)
    disp = {e["id"]: e["display"] for e in st.load_graph()["entities"]}
    pairs = {(disp.get(e["src_id"]), e["relation"], disp.get(e["dst_id"])) for e in edges}
    assert ("mg-into", "uses", "mg-dep") in pairs
    assert ("mg-into", "stores-data-in", "mg-store") in pairs
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_merge_folds_from_into -v`
Expected: FAIL — `AttributeError: ... 'graph_merge'`.

- [ ] **Step 3: Implement storage `merge_entity`**

In `postgres.py`:

```python
    def merge_entity(self, from_id: int, into_id: int) -> bool:
        """Fold `from` into `into`: drop edges that would duplicate or self-loop,
        re-point the rest, re-point fact/lesson refs, carry aliases + sources,
        then delete `from` (CASCADE clears its leftovers). edges UNIQUE
        (src,rel,dst) forces the dedup-before-repoint order."""
        if from_id == into_id:
            return False
        c = self.conn
        # 1a. drop from-edges that already exist on `into` (src side / dst side)
        c.execute("DELETE FROM edges f WHERE f.src_id = %s AND EXISTS ("
                  "SELECT 1 FROM edges t WHERE t.src_id = %s AND t.relation = f.relation "
                  "AND t.dst_id = f.dst_id)", (from_id, into_id))
        c.execute("DELETE FROM edges f WHERE f.dst_id = %s AND EXISTS ("
                  "SELECT 1 FROM edges t WHERE t.dst_id = %s AND t.relation = f.relation "
                  "AND t.src_id = f.src_id)", (from_id, into_id))
        # 1b. drop edges that would become self-loops (from<->into)
        c.execute("DELETE FROM edges WHERE (src_id = %s AND dst_id = %s) "
                  "OR (src_id = %s AND dst_id = %s)",
                  (from_id, into_id, into_id, from_id))
        # 1c. re-point
        c.execute("UPDATE edges SET src_id = %s WHERE src_id = %s", (into_id, from_id))
        c.execute("UPDATE edges SET dst_id = %s WHERE dst_id = %s", (into_id, from_id))
        # 2. fact/lesson refs
        for tbl in ("facts", "lessons"):
            c.execute(f"UPDATE {tbl} SET entity_id = %s WHERE entity_id = %s", (into_id, from_id))
            c.execute(f"UPDATE {tbl} SET object_entity_id = %s WHERE object_entity_id = %s", (into_id, from_id))
        # 3. aliases: from's canonical + its aliases become into's aliases
        frm = c.execute("SELECT canonical FROM entities WHERE id = %s", (from_id,)).fetchone()
        if frm:
            c.execute("INSERT INTO entity_aliases (alias, entity_id) VALUES (%s, %s) "
                      "ON CONFLICT (alias) DO NOTHING", (frm[0], into_id))
        c.execute("UPDATE entity_aliases SET entity_id = %s WHERE entity_id = %s "
                  "AND alias NOT IN (SELECT alias FROM entity_aliases WHERE entity_id = %s)",
                  (into_id, from_id, into_id))
        # 4. sources: carry from's sources onto into (keep existing)
        c.execute("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                  "SELECT %s, source, count, origin, updated_at FROM entity_sources WHERE entity_id = %s "
                  "ON CONFLICT (entity_id, source) DO NOTHING", (into_id, from_id))
        # 5. delete `from` (CASCADE removes its leftover aliases/sources/community/edges)
        c.execute("DELETE FROM entities WHERE id = %s", (from_id,))
        c.commit()
        return True
```

- [ ] **Step 4: Implement service `graph_merge` + route + fixture**

In `service.py`:

```python
    def graph_merge(self, from_entity: str, into_entity: str) -> dict[str, Any]:
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            a = self._storage.find_entity(G.norm_name(from_entity))
            b = self._storage.find_entity(G.norm_name(into_entity))
            if a is None or b is None:
                return {"merged": False, "reason": "unknown_entity",
                        "from": from_entity, "into": into_entity}
            if a["id"] == b["id"]:
                return {"merged": False, "reason": "same_entity", "into": b["display"]}
            ok = self._storage.merge_entity(a["id"], b["id"])
        return {"merged": ok, "from": a["display"], "into": b["display"]}
```

In `routes.py`: `p("/api/graph/merge", lambda q, b: svc.graph_merge(b["from"], b["into"]))`
In `fixtures.py`:

```python
    def graph_merge(self, **kw):
        return {"merged": True, "from": kw.get("from_entity") or kw.get("from"),
                "into": kw.get("into_entity") or kw.get("into")}
```

(Note: the route passes positional args; define the fixture as `def graph_merge(self, from_entity, into_entity): return {"merged": True, "from": from_entity, "into": into_entity}` to match the real signature.)

In `tests/test_web.py`:

```python
def test_merge_route(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("POST", "/api/graph/merge", {}, {"from": "dup", "into": "canonical"})
    assert out["merged"] is True and out["into"] == "canonical"
```

- [ ] **Step 5: Run + full graph/web suites + commit**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py tests/test_web.py tests/test_graph_review.py -v`
Expected: PASS (no regressions).

```bash
git add pseudolife_memory/storage/postgres.py pseudolife_memory/service.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_graph.py tests/test_web.py
git commit -m "feat(graph): merge mutation (re-point edges/refs, carry aliases+sources)"
```

---

## Self-Review

**Spec coverage (Stage 3a = spec §B mutations + §C graph_review):**
- `graph_review` analyzer (duplicate / orphan / dubious-edge / test-artifact / unattributed) → Task 1. ✓ (The spec's "belongs-to-another-project" finding is served by the Stage 2 project switcher; not re-implemented here.)
- `GET /api/graph/review` → Task 1. ✓
- `POST /api/graph/assign-scope` (manual `entity_sources`) → Task 2. ✓
- `POST /api/graph/unrelate` (wraps existing `graph_unrelate`) → Task 2. ✓
- `POST /api/graph/delete-entity` → Task 3. ✓
- `POST /api/graph/merge` (alias + re-point edges) → Task 4. ✓

**Out of Stage 3a (Stage 3b):** the Atlas review queue + action panel UI, confirm dialogs, "backed up first" wording, post-mutation re-fetch.

**Placeholder scan:** none — analyzer + each mutation has full code. **Type consistency:** `review(edges, entities, entity_sources_map)`, `graph_review(scope=None)`, `graph_assign_scope(entity, source)`, `delete_entity(entity_id)`/`graph_delete_entity(entity)`, `merge_entity(from_id, into_id)`/`graph_merge(from_entity, into_entity)` consistent across tasks and the route table.

**Safety notes for the implementer:**
- `degree_counts` is imported from `pseudolife_memory.graph` (same as `graph_insight.py` line 11) — confirm the import path during Task 1.
- The merge edge-dedup order (delete-colliders → delete-self-loops → re-point) matters; the Task 4 test asserts both surviving edges land on `into` — if the `UNIQUE (src_id, relation, dst_id)` still trips, widen the collider deletes and re-run.
- `delete_entity`/`merge_entity` run real `DELETE`/`UPDATE` on the dev test DB only; on the live bank they are reached solely through the confirm-gated, backup-first console (Stage 3b) — never auto-invoked.
