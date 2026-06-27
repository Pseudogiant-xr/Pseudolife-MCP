# Atlas Stage 1 — Project-Scoping Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the knowledge graph a project/topic dimension — derive each entity's project(s) from source provenance, keep it fresh, and expose a seedless, project-scoped graph read API.

**Architecture:** An additive `entity_sources` table denormalizes the entity→project mapping, keyed by `entity_id`. It is bulk-derived from `facts.entity_id ⋈ memory_traces ⋈ entries.source` (the authoritative fact-provenance link), refreshed incrementally at the end of each dream, and overridable by hand. Two read endpoints expose it: a seedless+scoped `GET /api/graph` and a new `GET /api/graph/projects`. No retrieval/dream-fact behavior changes.

**Tech Stack:** Python 3.11, psycopg (raw SQL via `self.conn.execute`), pgvector Postgres, vanilla-ESM web layer with a sync route table; pytest under `.venv`.

## Global Constraints

- **Test runner (verbatim):** `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest <args> -v`. NOT bare `python`. Postgres is up at `127.0.0.1:5433` (ops dev container), so PG-backed tests run; if it were down they'd skip.
- **Fixtures (already defined — do not redefine):**
  - Storage-layer tests use the `storage` fixture in `tests/test_graph.py` — a `PostgresStorage(pg_url)` (has `.conn`, `ensure_entity`, `find_entity`, `add_alias`, `upsert_edge`, `load_graph`, `add_trace`, etc.).
  - Service-layer tests use the module-scoped `svc` fixture in `tests/test_graph.py` — a PG-backed `MemoryService(database_url=pg_url)`. Access storage via `svc._storage`. **`svc` is module-scoped and accumulates state across tests** — use unique entity/source names and subset/`>=` assertions, never exact-equality on global maps/counts.
  - Web-layer tests use the `svc` fixture in `tests/test_web.py` — a file-mode `FixtureService` (no PG).
  - New entries must be created via `svc.store(text, source=...)` (it embeds) — never raw-insert the `embedding vector(384)` column.
- **Schema changes are additive only:** `CREATE TABLE IF NOT EXISTS`, and bump `SCHEMA_META_VERSION` by exactly 1 (15 → 16). **On the bump you MUST also update the two hardcoded pins** `tests/test_schema_v13.py:26` and `tests/test_temporal_stamp.py:29` (`assert SCHEMA_META_VERSION == 15` → `== 16`) — this is a known regression trap.
- `entity_sources.origin` ∈ `{derived, manual}`. `manual` rows are authoritative and never overwritten by derivation. An entity may belong to multiple projects (set-valued; PK is `(entity_id, source)`).
- Deploy (applies on daemon startup): `ops/backup.ps1` first, rebuild + `up -d --no-deps pseudolife-daemon`, never `down -v`.

---

### Task 1: Schema v16 — `entity_sources` table + version pins

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (add table to `SCHEMA_SQL`; `SCHEMA_META_VERSION` 15→16)
- Modify: `tests/test_schema_v13.py:26` and `tests/test_temporal_stamp.py:29` (pin → 16)
- Test: `tests/test_schema_v16.py` (new)

**Interfaces:**
- Produces: table `entity_sources(entity_id, source, count, origin, updated_at)`; `SCHEMA_META_VERSION == 16`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_v16.py
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_schema_version_is_16():
    assert SCHEMA_META_VERSION == 16


def test_entity_sources_table_present(pg_conn):
    assert pg_conn.execute(
        "SELECT to_regclass('public.entity_sources')").fetchone()[0]
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='entity_sources'").fetchall()}
    assert {"entity_id", "source", "count", "origin", "updated_at"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_schema_v16.py -v`
Expected: FAIL — version is 15; `entity_sources` missing.

- [ ] **Step 3: Implement the schema change + update the pins**

In `pseudolife_memory/storage/schema.py`, set `SCHEMA_META_VERSION = 16`. Append to `SCHEMA_SQL` (after the `memory_traces` block, before the closing `"""`):

```sql
-- v16 additive: per-entity project/topic attribution. Denormalized cache of
-- entity_id -> source(s). 'derived' rows are recomputed from
-- facts.entity_id ⋈ memory_traces ⋈ entries; 'manual' rows are user overrides
-- and are never auto-overwritten.
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

In `tests/test_schema_v13.py` line 26 and `tests/test_temporal_stamp.py` line 29, change `assert SCHEMA_META_VERSION == 15` to `== 16`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_schema_v16.py tests/test_schema_v13.py tests/test_temporal_stamp.py -v`
Expected: PASS (the `pg_conn` fixture runs `ensure_schema`, creating the table).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/schema.py tests/test_schema_v16.py tests/test_schema_v13.py tests/test_temporal_stamp.py
git commit -m "feat(graph): entity_sources table (schema v16)"
```

---

### Task 2: Storage CRUD for `entity_sources`

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new methods near `traces_for_slot`)
- Test: `tests/test_graph.py` (add to the storage-CRUD section; uses the `storage` fixture)

**Interfaces:**
- Produces (on `PostgresStorage`):
  - `upsert_entity_source(entity_id: int, source: str, origin: str, now: float) -> None`
  - `sources_for_entity(entity_id: int) -> list[dict]` → `[{"source","count","origin"}]`
  - `entity_sources_map() -> dict[int, list[str]]` (entity_id → sorted sources)
  - `project_source_counts() -> list[dict]` → `[{"source","entities"}]` desc by entities

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py (storage CRUD section). Unique names — svc/storage state accrues.
import time as _t


def test_entity_sources_upsert_and_read(storage):
    eid = storage.ensure_entity("es-postgres", display="es-postgres")
    storage.upsert_entity_source(eid, "es-proj-a", "derived", _t.time())
    storage.upsert_entity_source(eid, "es-proj-b", "derived", _t.time())
    assert {r["source"] for r in storage.sources_for_entity(eid)} == {"es-proj-a", "es-proj-b"}
    assert storage.entity_sources_map()[eid] == ["es-proj-a", "es-proj-b"]


def test_entity_sources_manual_not_clobbered(storage):
    eid = storage.ensure_entity("es-immerse", display="es-immerse")
    storage.upsert_entity_source(eid, "es-gw2", "manual", _t.time())
    storage.upsert_entity_source(eid, "es-gw2", "derived", _t.time())
    assert storage.sources_for_entity(eid)[0]["origin"] == "manual"


def test_entity_sources_project_counts(storage):
    a = storage.ensure_entity("es-c-a", display="es-c-a")
    b = storage.ensure_entity("es-c-b", display="es-c-b")
    storage.upsert_entity_source(a, "es-px", "derived", _t.time())
    storage.upsert_entity_source(b, "es-px", "derived", _t.time())
    storage.upsert_entity_source(b, "es-py", "derived", _t.time())
    counts = {r["source"]: r["entities"] for r in storage.project_source_counts()}
    assert counts["es-px"] == 2 and counts["es-py"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k entity_sources -v`
Expected: FAIL — `AttributeError: ... 'upsert_entity_source'`.

- [ ] **Step 3: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, add after `traces_for_slot`:

```python
    def upsert_entity_source(self, entity_id: int, source: str,
                             origin: str, now: float) -> None:
        """Attribute an entity to a project/source. A 'derived' upsert never
        downgrades an existing 'manual' row; it bumps count + updated_at. A
        'manual' upsert always wins."""
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

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k entity_sources -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_graph.py
git commit -m "feat(graph): entity_sources storage CRUD"
```

---

### Task 3: Backfill — derive `entity_sources` from the fact-provenance link

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new `backfill_entity_sources`)
- Test: `tests/test_graph.py` (service section; uses the `svc` fixture for real entries)

**Interfaces:**
- Consumes: `facts(entity_id, entity_norm, status)`, `memory_traces(entity_norm, entry_id)`, `entries(id, source)`.
- Produces: `backfill_entity_sources(now: float) -> int` (rows written/updated). Idempotent; writes `origin='derived'`; never touches `origin='manual'`. Keys by `facts.entity_id` (authoritative FK) to avoid the `norm_name`/`_norm_key` mismatch.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py (service-level section, uses svc)
def test_backfill_entity_sources_from_traces(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    st = svc._storage
    # cortex_write links facts.entity_id; two entries under two sources; trace both.
    svc.cortex_write("es-shared", "role", "thing", support="user")
    svc.store("es-shared mention x", source="es-src-x")
    ex = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    svc.store("es-shared mention y", source="es-src-y")
    ey = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en, an = _norm_key("es-shared"), _norm_key("role")
    st.add_trace(en, an, ex, _t.time())
    st.add_trace(en, an, ey, _t.time())

    n = st.backfill_entity_sources(_t.time())
    assert n >= 2
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    assert {"es-src-x", "es-src-y"} <= {r["source"] for r in st.sources_for_entity(eid)}
    # idempotent: second run keeps the same derived set
    st.backfill_entity_sources(_t.time())
    assert {"es-src-x", "es-src-y"} <= {r["source"] for r in st.sources_for_entity(eid)}


def test_backfill_preserves_manual(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    st = svc._storage
    svc.cortex_write("es-curated", "role", "thing", support="user")
    svc.store("es-curated mention", source="es-auto")
    e1 = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en = _norm_key("es-curated")
    st.add_trace(en, _norm_key("role"), e1, _t.time())
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    st.upsert_entity_source(eid, "es-hand", "manual", _t.time())
    st.backfill_entity_sources(_t.time())
    by_src = {r["source"]: r["origin"] for r in st.sources_for_entity(eid)}
    assert by_src["es-hand"] == "manual"
    assert by_src.get("es-auto") == "derived"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k backfill -v`
Expected: FAIL — `AttributeError: ... 'backfill_entity_sources'`.

- [ ] **Step 3: Implement the backfill**

In `pseudolife_memory/storage/postgres.py`, add after `project_source_counts`:

```python
    def backfill_entity_sources(self, now: float) -> int:
        """Derive entity->source attribution from the fact-provenance link:
        facts.entity_id is the authoritative FK to entities; facts.entity_norm
        shares the cortex normalization with memory_traces.entity_norm; entries
        carry the source. Keying by entity_id avoids the graph/cortex norm
        mismatch. Writes/refreshes origin='derived'; never overwrites 'manual'.
        Idempotent: count is recomputed from DISTINCT entries."""
        rows = self.conn.execute(
            "SELECT m.entity_id, en.source, COUNT(DISTINCT t.entry_id) AS cnt "
            "FROM (SELECT DISTINCT entity_id, entity_norm FROM facts "
            "      WHERE entity_id IS NOT NULL AND status = 'current') m "
            "JOIN memory_traces t ON t.entity_norm = m.entity_norm "
            "JOIN entries en ON en.id = t.entry_id "
            "WHERE en.source <> '' "
            "GROUP BY m.entity_id, en.source"
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

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k backfill -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_graph.py
git commit -m "feat(graph): backfill entity_sources from fact provenance"
```

---

### Task 4: Service — `graph_backfill_sources()` + incremental refresh in `dream_run`

**Files:**
- Modify: `pseudolife_memory/service.py` (new `graph_backfill_sources`; call it at the tail of `dream_run`'s non-empty-batch path)
- Test: `tests/test_graph.py` (service section)

**Interfaces:**
- Consumes: `storage.backfill_entity_sources` (Task 3).
- Produces: `graph_backfill_sources() -> dict` → `{"attributed": int}`. `dream_run`'s non-empty return dict gains `"sources_attributed": int`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py (service section)
def test_graph_backfill_sources_service(svc):
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    st = svc._storage
    svc.cortex_write("es-svc-target", "role", "thing", support="user")
    svc.store("es-svc-target note", source="es-svc-proj")
    e1 = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en = _norm_key("es-svc-target")
    st.add_trace(en, _norm_key("role"), e1, _t.time())

    res = svc.graph_backfill_sources()
    assert res["attributed"] >= 1
    eid = st.conn.execute(
        "SELECT entity_id FROM facts WHERE entity_norm=%s AND status='current' "
        "AND entity_id IS NOT NULL LIMIT 1", (en,)).fetchone()[0]
    assert "es-svc-proj" in st.entity_sources_map()[eid]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_backfill_sources_service -v`
Expected: FAIL — `AttributeError: ... 'graph_backfill_sources'`.

- [ ] **Step 3: Implement the service method + dream hook**

In `pseudolife_memory/service.py`, add a method near `_refresh_graph_insight`:

```python
    def graph_backfill_sources(self) -> dict[str, Any]:
        """Refresh entity->project attribution from fact provenance. Cheap,
        idempotent, manual overrides preserved. Takes the lock itself, so callers
        must NOT hold it (mirrors graph_backfill in dream_run, which runs after
        the lock is released)."""
        import time as _time
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"attributed": 0}
            n = self._storage.backfill_entity_sources(_time.time())
        return {"attributed": n}
```

In `dream_run`, in the **non-empty-batch** path, after `graph_insight = self._safe_refresh_graph_insight()` (~line 1795, before the `return`), add:

```python
        sources_attributed = self.graph_backfill_sources().get("attributed", 0)
```

and add `"sources_attributed": sources_attributed,` to that path's returned dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_graph_backfill_sources_service -v`
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
- Test: `tests/test_graph.py` (service section)

**Interfaces:**
- Consumes: `storage.load_graph`, `storage.entity_sources_map`, `storage.project_source_counts`, `storage.load_communities`.
- Produces:
  - `graph_projects() -> dict` → `{"projects": [{"source","entities"}]}`
  - `graph_neighborhood(entity=None, depth=1, include_facts=True, to=None, scope=None)` — blank `entity` returns the whole graph filtered to `scope` (a source; `None`/`"all"` = no filter); every node gains `"sources": [...]`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph.py (service section)
def test_graph_projects_lists_sources(svc):
    import time as _t
    st = svc._storage
    a = st.ensure_entity("es-proj-ent", display="es-proj-ent")
    st.upsert_entity_source(a, "es-proj-z", "derived", _t.time())
    assert any(p["source"] == "es-proj-z" for p in svc.graph_projects()["projects"])


def test_seedless_scoped_whole_graph(svc):
    import time as _t
    st = svc._storage
    keep = st.ensure_entity("es-keep-node", display="es-keep-node")
    drop = st.ensure_entity("es-drop-node", display="es-drop-node")
    st.upsert_entity_source(keep, "es-scope-a", "derived", _t.time())
    st.upsert_entity_source(drop, "es-scope-b", "derived", _t.time())

    full = svc.graph_neighborhood(entity=None, scope="all")
    names = {n["entity"] for n in full["nodes"]}
    assert {"es-keep-node", "es-drop-node"} <= names
    assert all("sources" in n for n in full["nodes"])

    scoped = svc.graph_neighborhood(entity=None, scope="es-scope-a")
    scoped_names = {n["entity"] for n in scoped["nodes"]}
    assert "es-keep-node" in scoped_names and "es-drop-node" not in scoped_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k "projects_lists or seedless" -v`
Expected: FAIL — `graph_projects` missing; `graph_neighborhood(entity=None)` returns `{"found": False}`.

- [ ] **Step 3: Implement**

In `pseudolife_memory/service.py`:

```python
    def graph_projects(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"projects": []}
            return {"projects": self._storage.project_source_counts()}
```

Change the `graph_neighborhood` signature to
`def graph_neighborhood(self, entity=None, depth=1, include_facts=True, to=None, scope=None)`,
and as its first body statement (before `from ... import graph as G` / the lock):

```python
        if not entity:
            return self._whole_graph(scope=scope, include_facts=include_facts)
```

Add the helper (shapes `load_graph()` like the seeded path, filtered by scope, keyed by entity_id):

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
        by_id, nodes = {}, []
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
                "nodes": nodes, "edges": edges, "paths": [], "truncated": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k "projects_lists or seedless" -v`
Expected: PASS.

- [ ] **Step 5: Run the seeded graph tests too (non-regression) + commit**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -v`
Expected: PASS (seeded `graph_neighborhood` unaffected — `entity` still works positionally/by-keyword).

```bash
git add pseudolife_memory/service.py tests/test_graph.py
git commit -m "feat(graph): seedless scoped whole-graph + graph_projects"
```

---

### Task 6: Routes + fixtures — expose scope and projects to the console

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (`/api/graph` gains `scope`; add `/api/graph/projects`)
- Modify: `pseudolife_memory/web/fixtures.py` (`graph_neighborhood` accepts `scope`; add `graph_projects`; nodes carry `sources`)
- Test: `tests/test_web.py` (uses the `FixtureService` `svc` fixture)

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

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_web.py -k "graph_scope or graph_projects_route" -v`
Expected: FAIL — `/api/graph/projects` not registered; fixture `graph_neighborhood` rejects `scope`.

- [ ] **Step 3: Implement routes + fixtures**

In `pseudolife_memory/web/routes.py`, replace the `/api/graph` registration and add the projects route (in the `# ---- graph ----` block):

```python
        g("/api/graph", lambda q, b: svc.graph_neighborhood(
            entity=_s(q, "entity"), depth=_i(q, "depth", 1),
            include_facts=_tribool(q, "include_facts") is not False,
            to=_s(q, "to"), scope=_s(q, "scope")))
        g("/api/graph/projects", lambda q, b: svc.graph_projects())
```

In `pseudolife_memory/web/fixtures.py`, change the graph fixture signature to accept `scope`, tag nodes with `sources`, and filter; add `graph_projects`. Inside `graph_neighborhood`, just before its `return`:

```python
        for nd in nodes:
            nd["sources"] = ["gw2-reshade"] if nd["entity"].startswith("GW2") \
                else ["pseudolife-mcp"]
        if scope and scope != "all":
            keep = {nd["entity"] for nd in nodes if scope in nd["sources"]}
            nodes = [nd for nd in nodes if nd["entity"] in keep]
            edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
```

(Change its signature to `def graph_neighborhood(self, entity, depth=1, include_facts=True, to=None, scope=None):`.) Add:

```python
    def graph_projects(self):
        return {"projects": [{"source": "pseudolife-mcp", "entities": 23},
                             {"source": "gw2-reshade", "entities": 16},
                             {"source": "hermes-infra", "entities": 9}]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_web.py -k graph -v`
Expected: PASS (new + existing graph dispatch tests).

- [ ] **Step 5: Run the touched suites + commit**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_web.py tests/test_graph.py tests/test_schema_v16.py -v`
Expected: PASS.

```bash
git add pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "feat(web): scope param + /api/graph/projects route"
```

---

## Self-Review

**Spec coverage (Stage 1):** `entity_sources` v16 → T1; retroactive derivation via fact provenance → T3; manual override preserved → T2,T3; multi-project (set-valued) → T2–T5; incremental refresh at dream tail → T4; seedless+scoped `GET /api/graph` with `sources` → T5,T6; `GET /api/graph/projects` → T5,T6; idempotent backfill → T3. ✓

**Out of Stage 1 (later stages):** `graph_review` analyzer, Atlas UI, mutation endpoints, "Show in Atlas" links, node-cap enforcement on the map (returns `truncated:False` for now).

**Placeholder scan:** none. **Type consistency:** `upsert_entity_source(entity_id,source,origin,now)`, `backfill_entity_sources(now)`, `entity_sources_map()->dict[int,list[str]]`, `graph_neighborhood(...,scope=None)`, `graph_projects()->{"projects":[...]}` consistent across T2→T6. ✓

**Harness correctness (pre-flight resolved):** real fixtures `storage`/`svc` (test_graph.py) + `FixtureService` (test_web.py); runner is `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe`; version pins updated in T1; backfill keys by `facts.entity_id` (not the graph/cortex norm); entries created via `svc.store` (never raw vector insert); module-scoped `svc` handled with unique names + subset assertions.
