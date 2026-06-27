# Atlas Stage 1 — Project-Scoping Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the knowledge graph a project/topic dimension — derive each entity's project(s) from source provenance, keep it fresh, and expose a seedless, project-scoped graph read API.

**Architecture:** An additive `entity_sources` table denormalizes the entity→project mapping. It is bulk-derived from `memory_traces ⋈ entries.source` (the existing fact-provenance link), refreshed incrementally at the end of each dream, and overridable by hand. Two read endpoints expose it: a seedless+scoped `GET /api/graph` and a new `GET /api/graph/projects`. No retrieval/dream-fact behavior changes.

**Tech Stack:** Python 3.11, psycopg (raw SQL, `self.conn.execute`), pgvector Postgres, vanilla-ESM web layer with a sync route table; pytest (`.venv`).

## Global Constraints

- Schema changes are additive only: `CREATE TABLE IF NOT EXISTS`, bump `SCHEMA_META_VERSION` (currently `15` → `16`) — verbatim from spec §A.
- `entity_sources.origin` ∈ `{derived, manual}`. `manual` rows are authoritative and never overwritten by derivation — spec §A.
- An entity may belong to multiple projects (shared infra like `postgres`); attribution is a set, never single-valued — spec §A.
- Deploy procedure (Stage applies on daemon startup): `ops/backup.ps1` first, rebuild + `up -d --no-deps pseudolife-daemon`, never `down -v` — spec §"Migration & rollout".
- Backend tests use the real PG `st` fixture for storage (mirrors `tests/test_graph.py`) and `FixtureService` for the web layer (mirrors `tests/test_web.py`, runs under `.venv`).
- Norm invariant: `entities.canonical == graph.norm_name(display) == memory_traces.entity_norm`. Joins rely on this.

---

### Task 1: Schema v16 — `entity_sources` table

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (add table to `SCHEMA_SQL`; bump `SCHEMA_META_VERSION` 15→16)
- Test: `tests/test_schema_v16.py` (new)

**Interfaces:**
- Produces: table `entity_sources(entity_id, source, count, origin, updated_at)`; `SCHEMA_META_VERSION == 16`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_v16.py
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_schema_version_is_16():
    assert SCHEMA_META_VERSION == 16


def test_entity_sources_table_present(st):
    assert st.conn.execute(
        "SELECT to_regclass('public.entity_sources')").fetchone()[0]
    cols = {r[0] for r in st.conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='entity_sources'").fetchall()}
    assert {"entity_id", "source", "count", "origin", "updated_at"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_schema_v16.py -v`
Expected: FAIL — `test_schema_version_is_16` asserts 16 but value is 15; table missing.

- [ ] **Step 3: Implement the schema change**

In `pseudolife_memory/storage/schema.py`, set `SCHEMA_META_VERSION = 16`. Add to `SCHEMA_SQL` (after the `memory_traces` block, before the closing `"""`):

```sql
-- v16 additive: per-entity project/topic attribution. Denormalized cache of
-- entity -> source(s); `derived` rows are recomputed from memory_traces ⋈
-- entries, `manual` rows are user overrides and are never auto-overwritten.
CREATE TABLE IF NOT EXISTS entity_sources (
  entity_id  BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source     TEXT   NOT NULL,
  count      INTEGER NOT NULL DEFAULT 1,
  origin     TEXT   NOT NULL DEFAULT 'derived',
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_id, source)
);
CREATE INDEX IF NOT EXISTS entity_sources_source_idx ON entity_sources (source);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_schema_v16.py -v`
Expected: PASS (the `st` fixture calls `ensure_schema`, creating the table).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/schema.py tests/test_schema_v16.py
git commit -m "feat(graph): entity_sources table (schema v16)"
```

---

### Task 2: Storage CRUD for `entity_sources`

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new methods near `add_trace`/`load_graph`)
- Test: `tests/test_entity_sources.py` (new)

**Interfaces:**
- Produces (on the PG storage object):
  - `upsert_entity_source(entity_id: int, source: str, origin: str, now: float) -> None`
  - `sources_for_entity(entity_id: int) -> list[dict]` → `[{"source","count","origin"}]`
  - `entity_sources_map() -> dict[int, list[str]]` (entity_id → sorted sources)
  - `project_source_counts() -> list[dict]` → `[{"source","entities"}]` desc by entities

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entity_sources.py
import time as _t


def _mk_entity(st, name):
    return st.ensure_entity(name, display=name, etype=None)


def test_upsert_and_read_back(st):
    eid = _mk_entity(st, "postgres")
    st.upsert_entity_source(eid, "pseudolife-mcp", "derived", _t.time())
    st.upsert_entity_source(eid, "hermes-infra", "derived", _t.time())
    srcs = {r["source"] for r in st.sources_for_entity(eid)}
    assert srcs == {"pseudolife-mcp", "hermes-infra"}
    assert st.entity_sources_map()[eid] == ["hermes-infra", "pseudolife-mcp"]


def test_manual_not_clobbered_by_derived(st):
    eid = _mk_entity(st, "gw2_immerse_natural-rtgi.ini")
    st.upsert_entity_source(eid, "gw2-reshade", "manual", _t.time())
    st.upsert_entity_source(eid, "gw2-reshade", "derived", _t.time())
    row = st.sources_for_entity(eid)[0]
    assert row["origin"] == "manual"   # derived upsert must not downgrade it


def test_project_source_counts(st):
    a, b = _mk_entity(st, "ent-a"), _mk_entity(st, "ent-b")
    st.upsert_entity_source(a, "proj-x", "derived", _t.time())
    st.upsert_entity_source(b, "proj-x", "derived", _t.time())
    st.upsert_entity_source(b, "proj-y", "derived", _t.time())
    counts = {r["source"]: r["entities"] for r in st.project_source_counts()}
    assert counts["proj-x"] == 2 and counts["proj-y"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_entity_sources.py -v`
Expected: FAIL — `AttributeError: 'PostgresStorage' object has no attribute 'upsert_entity_source'`.

- [ ] **Step 3: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, add near `traces_for_slot`:

```python
    def upsert_entity_source(self, entity_id: int, source: str,
                             origin: str, now: float) -> None:
        """Attribute an entity to a project/source. A 'derived' upsert never
        downgrades an existing 'manual' row (manual is authoritative); it bumps
        count + updated_at. A 'manual' upsert always wins."""
        self.conn.execute(
            "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
            "VALUES (%s, %s, 1, %s, %s) "
            "ON CONFLICT (entity_id, source) DO UPDATE SET "
            "  count = entity_sources.count + 1, "
            "  updated_at = EXCLUDED.updated_at, "
            "  origin = CASE WHEN entity_sources.origin = 'manual' "
            "                THEN 'manual' ELSE EXCLUDED.origin END",
            (entity_id, source, origin, now))
        self.conn.commit()

    def sources_for_entity(self, entity_id: int) -> list[dict]:
        cols = ("source", "count", "origin")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT source, count, origin FROM entity_sources "
            "WHERE entity_id = %s ORDER BY count DESC, source", (entity_id,)).fetchall()]

    def entity_sources_map(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for eid, source in self.conn.execute(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source"
        ).fetchall():
            out.setdefault(eid, []).append(source)
        return out

    def project_source_counts(self) -> list[dict]:
        cols = ("source", "entities")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT source, COUNT(DISTINCT entity_id) AS entities "
            "FROM entity_sources GROUP BY source ORDER BY entities DESC, source"
        ).fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_entity_sources.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_entity_sources.py
git commit -m "feat(graph): entity_sources storage CRUD"
```

---

### Task 3: Backfill — derive `entity_sources` from `memory_traces ⋈ entries`

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new `backfill_entity_sources`)
- Test: `tests/test_entity_sources.py` (extend)

**Interfaces:**
- Consumes: `entities.canonical`, `memory_traces(entity_norm, entry_id)`, `entries(id, source)`.
- Produces: `backfill_entity_sources(now: float) -> int` (rows written/updated). Idempotent; writes `origin='derived'`; never touches `origin='manual'`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_entity_sources.py
import time as _t


def _add_entry(st, text, source):
    return st.conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, source) "
        "VALUES ('working', %s, %s, %s, %s) RETURNING id",
        (text, [0.0] * 384, _t.time(), source)).fetchone()[0]


def test_backfill_derives_sources_from_traces(st):
    eid = st.ensure_entity("shared-thing", display="shared-thing", etype=None)
    e1 = _add_entry(st, "shared-thing in project x", "proj-x")
    e2 = _add_entry(st, "shared-thing in project y", "proj-y")
    st.add_trace("shared-thing", "status", e1, _t.time())
    st.add_trace("shared-thing", "status", e2, _t.time())

    n = st.backfill_entity_sources(_t.time())
    assert n >= 2
    assert {r["source"] for r in st.sources_for_entity(eid)} == {"proj-x", "proj-y"}
    # idempotent: a second run changes the derived set to the same value
    st.backfill_entity_sources(_t.time())
    assert {r["source"] for r in st.sources_for_entity(eid)} == {"proj-x", "proj-y"}


def test_backfill_preserves_manual(st):
    eid = st.ensure_entity("curated", display="curated", etype=None)
    e1 = _add_entry(st, "curated mention", "auto-src")
    st.add_trace("curated", "status", e1, _t.time())
    st.upsert_entity_source(eid, "hand-src", "manual", _t.time())
    st.backfill_entity_sources(_t.time())
    by_src = {r["source"]: r["origin"] for r in st.sources_for_entity(eid)}
    assert by_src["hand-src"] == "manual"     # untouched
    assert by_src.get("auto-src") == "derived"  # added alongside
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_entity_sources.py -k backfill -v`
Expected: FAIL — `AttributeError: ... 'backfill_entity_sources'`.

- [ ] **Step 3: Implement the backfill**

In `pseudolife_memory/storage/postgres.py`, add after `project_source_counts`:

```python
    def backfill_entity_sources(self, now: float) -> int:
        """Derive entity->source attribution from the fact-provenance link
        (entities.canonical == memory_traces.entity_norm; entries.source is the
        project). Writes/refreshes origin='derived' rows; never overwrites a
        'manual' row. Idempotent: count is recomputed from DISTINCT entries."""
        rows = self.conn.execute(
            "SELECT e.id, en.source, COUNT(DISTINCT t.entry_id) "
            "FROM entities e "
            "JOIN memory_traces t ON t.entity_norm = e.canonical "
            "JOIN entries en ON en.id = t.entry_id "
            "WHERE en.source <> '' "
            "GROUP BY e.id, en.source"
        ).fetchall()
        n = 0
        for entity_id, source, cnt in rows:
            self.conn.execute(
                "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                "VALUES (%s, %s, %s, 'derived', %s) "
                "ON CONFLICT (entity_id, source) DO UPDATE SET "
                "  count = EXCLUDED.count, updated_at = EXCLUDED.updated_at, "
                "  origin = CASE WHEN entity_sources.origin = 'manual' "
                "                THEN 'manual' ELSE 'derived' END",
                (entity_id, source, int(cnt), now))
            n += 1
        self.conn.commit()
        return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_entity_sources.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_entity_sources.py
git commit -m "feat(graph): backfill entity_sources from trace provenance"
```

---

### Task 4: Service — `graph_backfill_sources()` + incremental refresh in `dream_run`

**Files:**
- Modify: `pseudolife_memory/service.py` (new `graph_backfill_sources`; call it at the tail of `dream_run`)
- Test: `tests/test_graph.py` (extend — uses the warm `svc` fixture)

**Interfaces:**
- Consumes: `storage.backfill_entity_sources` (Task 3).
- Produces: `graph_backfill_sources() -> dict` → `{"attributed": int}`. `dream_run`'s return dict gains `"sources_attributed": int`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py
def test_graph_backfill_sources_service(svc):
    # seed an entity with a traced entry carrying a source, then attribute
    import time as _t
    st = svc._storage
    eid = st.ensure_entity("attrib-target", display="attrib-target", etype=None)
    e1 = st.conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, source) "
        "VALUES ('working','attrib-target note', %s, %s, 'svc-proj') RETURNING id",
        ([0.0] * 384, _t.time())).fetchone()[0]
    st.add_trace("attrib-target", "status", e1, _t.time())

    res = svc.graph_backfill_sources()
    assert res["attributed"] >= 1
    assert "svc-proj" in st.entity_sources_map()[eid]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_graph.py::test_graph_backfill_sources_service -v`
Expected: FAIL — `AttributeError: ... 'graph_backfill_sources'`.

- [ ] **Step 3: Implement the service method + dream hook**

In `pseudolife_memory/service.py`, add a method (near `_refresh_graph_insight`):

```python
    def graph_backfill_sources(self) -> dict[str, Any]:
        """Refresh entity->project attribution from trace provenance. Cheap,
        idempotent, manual overrides preserved. Read inputs + write under lock."""
        import time as _time
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"attributed": 0}
            n = self._storage.backfill_entity_sources(_time.time())
        return {"attributed": n}
```

Then, in `dream_run`, after `graph_insight = self._safe_refresh_graph_insight()` (the non-empty-batch path, ~line 1795), add:

```python
        sources_attributed = self.graph_backfill_sources().get("attributed", 0)
```

and add `"sources_attributed": sources_attributed,` to that path's return dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_graph.py::test_graph_backfill_sources_service -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_graph.py
git commit -m "feat(graph): service backfill + incremental refresh in dream_run"
```

---

### Task 5: Service — `graph_projects()` and seedless, scoped `graph_neighborhood`

**Files:**
- Modify: `pseudolife_memory/service.py` (`graph_neighborhood` signature + seedless branch; new `graph_projects`; new `_whole_graph` helper)
- Test: `tests/test_graph.py` (extend)

**Interfaces:**
- Consumes: `storage.load_graph`, `storage.entity_sources_map`, `storage.project_source_counts`, `load_communities`.
- Produces:
  - `graph_projects() -> dict` → `{"projects": [{"source","entities"}]}`
  - `graph_neighborhood(entity=None, depth=1, include_facts=True, to=None, scope=None)` — when `entity` is blank, returns the whole graph filtered to `scope` (a source; `None`/`"all"` = no filter); every node gains `"sources": [...]`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py
def test_graph_projects_lists_sources(svc):
    import time as _t
    st = svc._storage
    a = st.ensure_entity("proj-ent", display="proj-ent", etype=None)
    st.upsert_entity_source(a, "proj-z", "derived", _t.time())
    out = svc.graph_projects()
    assert any(p["source"] == "proj-z" for p in out["projects"])


def test_seedless_scoped_whole_graph(svc):
    import time as _t
    st = svc._storage
    keep = st.ensure_entity("keep-node", display="keep-node", etype=None)
    drop = st.ensure_entity("drop-node", display="drop-node", etype=None)
    st.upsert_entity_source(keep, "scope-a", "derived", _t.time())
    st.upsert_entity_source(drop, "scope-b", "derived", _t.time())

    full = svc.graph_neighborhood(entity=None, scope="all")
    names = {n["entity"] for n in full["nodes"]}
    assert {"keep-node", "drop-node"} <= names
    assert all("sources" in n for n in full["nodes"])

    scoped = svc.graph_neighborhood(entity=None, scope="scope-a")
    scoped_names = {n["entity"] for n in scoped["nodes"]}
    assert "keep-node" in scoped_names and "drop-node" not in scoped_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_graph.py -k "projects or seedless" -v`
Expected: FAIL — `graph_projects` missing; `graph_neighborhood(entity=None)` currently returns `{"found": False}`.

- [ ] **Step 3: Implement**

In `pseudolife_memory/service.py`, add `graph_projects` and a `_whole_graph` helper, and branch `graph_neighborhood` on a blank entity. Add `scope` to the signature (default `None`).

```python
    def graph_projects(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"projects": []}
            return {"projects": self._storage.project_source_counts()}
```

At the top of `graph_neighborhood`, after the signature change `def graph_neighborhood(self, entity=None, depth=1, include_facts=True, to=None, scope=None)`, before the existing `find_entity` logic:

```python
        if not entity:
            return self._whole_graph(scope=scope, include_facts=include_facts)
```

Add the helper (shapes `load_graph()` like the seeded path, filtered by scope):

```python
    def _whole_graph(self, scope: str | None, include_facts: bool) -> dict[str, Any]:
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            g = self._storage.load_graph()
            comm = self._storage.load_communities()["assignment"]
            src_map = self._storage.entity_sources_map()
            facts_by_norm: dict[str, list[dict]] = {}
            if include_facts and self._cortex is not None:
                for rec in self._cortex.current_records():
                    facts_by_norm.setdefault(G.norm_name(rec.entity), []).append({
                        "attribute": rec.attribute, "value": rec.value,
                        "origin": rec.origin,
                        "confidence": round(float(rec.confidence), 4)})
        keep = None
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
        nodes, by_id = [], {}
        for e in g["entities"]:
            if keep is not None and e["id"] not in keep:
                continue
            by_id[e["id"]] = e["display"]
            node = {"entity": e["display"], "canonical": e["canonical"],
                    "etype": e["etype"], "aliases": g["aliases"].get(e["id"], []),
                    "community": comm.get(e["id"]), "sources": src_map.get(e["id"], [])}
            if include_facts:
                node["facts"] = facts_by_norm.get(e["canonical"], [])
            nodes.append(node)
        edges = [
            {"src": by_id[e["src_id"]], "relation": e["relation"],
             "dst": by_id[e["dst_id"]], "derived": False,
             "confidence": round(float(e["confidence"]), 4),
             "origin": e.get("origin")}
            for e in g["edges"]
            if e["src_id"] in by_id and e["dst_id"] in by_id]
        return {"found": True, "entity": None, "scope": scope or "all",
                "nodes": nodes, "edges": edges, "paths": [],
                "truncated": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_graph.py -k "projects or seedless" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_graph.py
git commit -m "feat(graph): seedless scoped whole-graph + graph_projects"
```

---

### Task 6: Routes + fixtures — expose scope and projects to the console

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (`/api/graph` gains `scope`; add `/api/graph/projects`)
- Modify: `pseudolife_memory/web/fixtures.py` (`graph_neighborhood` accepts `scope`; add `graph_projects`; nodes carry `sources`)
- Test: `tests/test_web.py` (extend)

**Interfaces:**
- Consumes: `svc.graph_neighborhood(entity, depth, include_facts, to, scope)`, `svc.graph_projects()`.
- Produces: `GET /api/graph?scope=`, `GET /api/graph/projects`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_web.py
def test_graph_scope_param_dispatches(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("GET", "/api/graph", {"scope": "all"}, {})
    assert out["found"] is True
    assert all("sources" in n for n in out["nodes"])


def test_graph_projects_route(svc):
    r = ConsoleRoutes(svc)
    out = r.dispatch("GET", "/api/graph/projects", {}, {})
    assert "projects" in out and isinstance(out["projects"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_web.py -k "graph_scope or graph_projects_route" -v`
Expected: FAIL — `/api/graph/projects` not registered; fixture `graph_neighborhood` rejects `scope`.

- [ ] **Step 3: Implement routes + fixtures**

In `pseudolife_memory/web/routes.py`, replace the `/api/graph` registration and add the projects route:

```python
        g("/api/graph", lambda q, b: svc.graph_neighborhood(
            entity=_s(q, "entity"), depth=_i(q, "depth", 1),
            include_facts=_tribool(q, "include_facts") is not False,
            to=_s(q, "to"), scope=_s(q, "scope")))
        g("/api/graph/projects", lambda q, b: svc.graph_projects())
```

In `pseudolife_memory/web/fixtures.py`, update the graph fixture to accept `scope`, tag nodes with `sources`, and add `graph_projects`:

```python
    def graph_neighborhood(self, entity, depth=1, include_facts=True, to=None, scope=None):
        # ... existing node/edge construction ...
        # before the return, attach a stable demo source per node:
        for nd in nodes:
            nd["sources"] = ["gw2-reshade"] if nd["entity"].startswith("GW2") \
                else ["pseudolife-mcp"]
        if scope and scope != "all":
            keep = {nd["entity"] for nd in nodes if scope in nd["sources"]}
            nodes = [nd for nd in nodes if nd["entity"] in keep]
            edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
        return {"found": True, "entity": entity, "depth": depth,
                "nodes": nodes, "edges": edges,
                "paths": [["pseudolife-mcp", "postgres", "docker-desktop"]] if to else []}

    def graph_projects(self):
        return {"projects": [{"source": "pseudolife-mcp", "entities": 23},
                             {"source": "gw2-reshade", "entities": 16},
                             {"source": "hermes-infra", "entities": 9}]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_web.py -k "graph" -v`
Expected: PASS (new + existing graph tests).

- [ ] **Step 5: Run the full backend suite + commit**

Run: `.venv/Scripts/python -m pytest tests/test_web.py tests/test_graph.py tests/test_entity_sources.py tests/test_schema_v16.py -v`
Expected: PASS.

```bash
git add pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "feat(web): scope param + /api/graph/projects route"
```

---

## Self-Review

**Spec coverage (Stage 1 scope):**
- `entity_sources` table v16 → Task 1. ✓
- Retroactive derivation from `memory_traces ⋈ entries.source` → Task 3. ✓
- Manual override authoritative / never auto-overwritten → Tasks 2, 3 (CASE-preserve). ✓
- Multi-project entities (set-valued) → Tasks 2–5 (PK on entity_id+source). ✓
- "Forward stamp" → realized as incremental backfill at the tail of `dream_run` (Task 4) — the mechanism refinement noted in the spec reconciliation; precise because it rides the per-entry trace link rather than the batched relation extractor. ✓
- Seedless + scoped `GET /api/graph` with `sources` on nodes → Tasks 5, 6. ✓
- `GET /api/graph/projects` (project switcher data) → Tasks 5, 6. ✓
- Backfill is idempotent + re-runnable → Task 3 test. ✓
- Node ceiling / `truncated` flag → returned `truncated: False` in Task 5; enforcing a cap value is deferred to Stage 2 (the map is what needs it), tracked there.

**Out of Stage 1 (later stages, by design):** `graph_review` analyzer, Atlas UI, mutation endpoints (`assign-scope`/`merge`/`unrelate`/`delete-entity`), "Show in Atlas" links.

**Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**Type consistency:** `upsert_entity_source(entity_id, source, origin, now)`, `backfill_entity_sources(now)`, `entity_sources_map() -> dict[int,list[str]]`, `graph_neighborhood(..., scope=None)`, `graph_projects() -> {"projects":[...]}` — names/shapes match across Tasks 2→6. ✓

**Risk to verify during execution:** `entries` insert in tests assumes columns `(band, text, embedding, ts, source)` with a 384-d vector — confirm the embedding column accepts a Python list under the project's psycopg/pgvector setup (mirror `tests/test_graph.py:417`'s existing insert if it differs).
