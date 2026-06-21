# Writer-aware temporal memory (v0.4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp every canonical write with a robust temporal + provenance tuple — `(tx_time, valid_time, hlc, writer_id, session_id, version)` — giving the agent a real sense of time, deterministic jitter-proof ordering, and per-writer attribution; and eliminate the role/AGE-schema name collision at the root.

**Architecture:** Add the stamp columns additively (schema v11). Make an HLC-lite clock the ordering authority for supersession (wall-clock becomes display-only). Stamp writer/session via a per-connection handshake through the single daemon. Add a `valid_time` (event time) point. Lay a `write_mode: snapshot|occ` config seam — `snapshot` is the only live path; the `occ` branch is a clearly-marked dormant stub (its full build is a separate Phase-2 plan). Eliminate the schema collision by renaming the AGE graph off the role name, pinning `search_path=public` on every connection, dropping the stale shadow tables, and fixing `meta.schema_version` to update on upgrade — via a guarded, backup-first one-time migration.

**Tech Stack:** Python 3.11/3.12, torch (CPU), PostgreSQL + pgvector + Apache AGE (psycopg), pytest, Docker Compose.

**Spec:** `docs/specs/2026-06-21-writer-aware-temporal-memory-design.md`

**Scope:** Phase 1 (live, shared-daemon) only. Phase 2 (the real OCC write path / distributed HLC / cache invalidation) is **out of scope** here — this plan lays its schema (`version`, `hlc_*`) and a stub seam so Phase 2 is a clean later addition. Phase 3 (flipping `write_mode=occ`) is a future deploy decision.

**Test invocation (this repo):** `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest <args>`
PG-backed tests use the `pg_conn`/`pg_url` fixtures (`tests/pg_fixtures.py`) and skip cleanly without a test server. The collision-migration tests need the AGE-enabled dev Postgres (the compose `pseudolife-pg` image).

---

## File Structure

- `pseudolife_memory/memory/hlc.py` — **new.** `HybridLogicalClock` (pure, injectable): `tick()` → `(phys, logical)`, `observe(remote_phys, remote_logical)` (Phase-2 receive rule, unit-tested but only called under `occ`).
- `pseudolife_memory/storage/schema.py` — bump `SCHEMA_META_VERSION 10 → 11`; guarded `ALTER TABLE … ADD COLUMN IF NOT EXISTS` + backfill for the stamp columns on `facts`/`world_facts`/`lessons`/`edges`; change the `schema_version` write from insert-or-keep to upsert-update.
- `pseudolife_memory/storage/postgres.py` — extend `_FACT_COLS`/`_WORLD_FACT_COLS`/`_LESSON_COLS` + edge insert with the stamp columns; helpers stay generic.
- `pseudolife_memory/storage/sync.py` — map the stamp fields in every `_*_record_to_row`/`hydrate_*`.
- `pseudolife_memory/storage/age.py` — `GRAPH_NAME` from config (`graph.name`, default `pseudolife_graph`); `resync` unchanged (rebuild-from-truth is the rename mechanism).
- `pseudolife_memory/memory/cortex.py` — `CortexRecord` gains stamp fields; `_should_supersede` orders by HLC, not `asserted_at`.
- `pseudolife_memory/memory/world_cortex.py`, `…/memory/lessons.py` — records gain stamp fields (mirror cortex).
- `pseudolife_memory/service.py` — hold the HLC + a per-call `(writer_id, session_id)` context; stamp every write + the supersession log; set `valid_time`; `memory_history`; relative-age in serialisers.
- `pseudolife_memory/daemon.py` — read `X-PL-Writer` header, mint a `session_id` per MCP connection, bind to a contextvar.
- `pseudolife_memory/mcp_server.py` — `memory_history` tool; pass the writer/session context into service writes.
- `pseudolife_memory/shim.py` — forward `PSEUDOLIFE_WRITER_ID` as the `X-PL-Writer` header.
- `pseudolife_memory/utils/config.py` — `StorageConfig.write_mode`; `GraphConfig.name`; `TimeConfig.relative_age`; `MemoryConfig` wiring.
- `ops/migrate_v04.py` — **new.** Guarded (dry-run default, backup-first) one-time migration: rename AGE graph, drop shadow tables, bump meta version.
- `ops/docker-compose.yml` — set `PSEUDOLIFE_WRITER_ID` + `PSEUDOLIFE_GRAPH_NAME` on the daemon.
- `README.md`, `CHANGELOG.md` — the temporal/keying model, the `write_mode` seam, the collision fix.
- Tests: `tests/test_hlc.py` (new), `tests/test_temporal_stamp.py` (new, PG), `tests/test_writer_keying.py` (new, PG/daemon), `tests/test_collision_migration.py` (new, PG+AGE), `tests/test_lessons*.py` / `tests/test_cortex*.py` / `tests/test_pg_storage.py` (extend for new columns), `tests/test_mcp_server.py` (register `memory_history`).

---

## Task 1: Schema v11 — stamp columns (additive) + meta version-bump fix

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (`SCHEMA_META_VERSION`, `ensure_schema`)
- Test: `tests/test_temporal_stamp.py` (new)

- [ ] **Step 1: Write the failing test**

`tests/test_temporal_stamp.py`:
```python
"""Schema v11 stamp columns + meta version upsert (skips without PG)."""
from __future__ import annotations
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

_STAMPED = ("facts", "world_facts", "lessons", "edges")
_COLS = ("tx_time", "valid_time", "hlc_phys", "hlc_logical",
         "writer_id", "session_id", "version")

def test_stamp_columns_present(pg_conn):
    for tbl in _STAMPED:
        cols = {r[0] for r in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (tbl,)).fetchall()}
        assert set(_COLS) <= cols, f"{tbl} missing {set(_COLS)-cols}"

def test_meta_version_is_current(pg_conn):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION, ensure_schema
    # Simulate a stale recorded version, then re-ensure: it must update to current.
    pg_conn.execute("UPDATE meta SET value='8'::jsonb WHERE key='schema_version'")
    pg_conn.commit()
    ensure_schema(pg_conn)
    row = pg_conn.execute("SELECT value::text FROM meta WHERE key='schema_version'").fetchone()
    assert int(row[0].strip('\"')) == SCHEMA_META_VERSION == 11
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_temporal_stamp.py -v`
Expected: FAIL (columns absent; version stays 8).

- [ ] **Step 3: Implement**

In `schema.py`: bump `SCHEMA_META_VERSION = 11`. Append to the END of `SCHEMA_SQL` a guarded additive block (runs every init, idempotent):
```sql
-- v11 temporal/provenance stamp (additive; backfilled from asserted_at).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['facts','world_facts','lessons','edges'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tx_time DOUBLE PRECISION', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS valid_time DOUBLE PRECISION', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS hlc_phys BIGINT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS hlc_logical INT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS writer_id TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS session_id TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1', t);
    EXECUTE format('UPDATE %I SET tx_time=asserted_at WHERE tx_time IS NULL', t);
    EXECUTE format('UPDATE %I SET valid_time=asserted_at WHERE valid_time IS NULL', t);
    EXECUTE format('UPDATE %I SET writer_id=''legacy'' WHERE writer_id IS NULL', t);
  END LOOP;
END $$;
```
(`edges` uses `asserted_at` too — it has that column.) Then change the meta write:
```sql
INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
```

- [ ] **Step 4: Run to verify it passes**

Run: same as Step 2. Expected: PASS. Also `…/pytest tests/test_pg_storage.py -v` (ensure existing schema tests still green; `test_schema_version_recorded` now asserts 11).

- [ ] **Step 5: Commit**
```bash
git add pseudolife_memory/storage/schema.py tests/test_temporal_stamp.py
git commit -m "feat(schema): v11 temporal/provenance stamp columns + meta version upsert"
```

---

## Task 2: HLC-lite clock + HLC-ordered supersession

**Files:**
- Create: `pseudolife_memory/memory/hlc.py`
- Modify: `pseudolife_memory/memory/cortex.py` (`CortexRecord`, `_should_supersede`)
- Test: `tests/test_hlc.py` (new); extend `tests/test_cortex.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_hlc.py`:
```python
from pseudolife_memory.memory.hlc import HybridLogicalClock

def test_monotonic_within_same_ms():
    c = HybridLogicalClock(now_ms=lambda: 1000)
    a, b = c.tick(), c.tick()
    assert a == (1000, 0) and b == (1000, 1)          # logical bumps on ties

def test_advances_with_wall():
    seq = iter([1000, 1001])
    c = HybridLogicalClock(now_ms=lambda: next(seq))
    assert c.tick() == (1000, 0) and c.tick() == (1001, 0)

def test_never_goes_backwards():
    seq = iter([1000, 990])                            # wall steps BACK
    c = HybridLogicalClock(now_ms=lambda: next(seq))
    assert c.tick() == (1000, 0)
    assert c.tick() == (1000, 1)                       # stays at 1000, bumps logical
```

In `tests/test_cortex.py`, a regression for the dropped-write bug:
```python
def test_backwards_wall_clock_still_supersedes():
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot
    import torch
    s = CortexStore()
    e = torch.ones(384)
    s.write_fact(Slot("server","port","8080"), e, support="user", now=2000.0, hlc=(2000000,0))
    r = s.write_fact(Slot("server","port","9090"), e, support="user", now=1.0, hlc=(2000001,0))
    assert r.action == "superseded"                    # later HLC wins despite earlier wall time
    assert s.lookup("server","port").value == "9090"
```

- [ ] **Step 2: Run to verify fail**

Run: `…/pytest tests/test_hlc.py tests/test_cortex.py::test_backwards_wall_clock_still_supersedes -v`
Expected: FAIL (`hlc` module/param missing).

- [ ] **Step 3: Implement**

`pseudolife_memory/memory/hlc.py`:
```python
"""Hybrid Logical Clock (lite). Ordering authority for writes — monotonic and
immune to wall-clock steps. In shared-daemon mode only tick() is used; observe()
is the Phase-2 receive rule (dormant until write_mode=occ)."""
from __future__ import annotations
import time

def _wall_ms() -> int:
    return int(time.time() * 1000)

class HybridLogicalClock:
    def __init__(self, now_ms=_wall_ms) -> None:
        self._now = now_ms
        self._phys = 0
        self._logical = 0

    def tick(self) -> tuple[int, int]:
        now = self._now()
        if now > self._phys:
            self._phys, self._logical = now, 0
        else:
            self._logical += 1            # same-or-backwards ms -> bump counter
        return (self._phys, self._logical)

    def observe(self, phys: int, logical: int) -> None:
        """Phase-2: advance past a remote stamp on read (occ mode only)."""
        if phys > self._phys:
            self._phys, self._logical = phys, logical
        elif phys == self._phys:
            self._logical = max(self._logical, logical)
```

In `cortex.py`: add `hlc_phys`/`hlc_logical`/`tx_time`/`valid_time`/`writer_id`/`session_id`/`version` to `CortexRecord`; thread an optional `hlc=(phys,logical)` through `write_fact`/`_insert`; rewrite `_should_supersede` to compare HLC (fallback to `asserted_at` when a record predates HLC):
```python
def _should_supersede(self, current, candidate_conf, candidate_hlc, candidate_t):
    cur_hlc = (current.hlc_phys or 0, current.hlc_logical or 0)
    cand_hlc = candidate_hlc or (0, 0)
    if cand_hlc < cur_hlc:                 # strictly-earlier HLC never wins
        return False
    if cand_hlc == cur_hlc and candidate_t < current.asserted_at:
        return False                       # legacy/no-HLC tiebreak
    if candidate_conf < current.confidence - self.supersede_confidence_margin:
        return False
    return True
```
(Keep the public `write_fact` signature back-compatible: `hlc=None` defaults to exact-key/legacy behaviour so existing callers/tests pass.)

- [ ] **Step 4: Run to verify pass** — Steps-2 commands → PASS; then `…/pytest tests/test_cortex.py tests/test_cortex_contenders.py -q` (no regressions).

- [ ] **Step 5: Commit**
```bash
git add pseudolife_memory/memory/hlc.py pseudolife_memory/memory/cortex.py tests/test_hlc.py tests/test_cortex.py
git commit -m "feat(hlc): HLC-lite clock; supersession ordered by HLC not wall-clock"
```

---

## Task 3: Persist + hydrate the stamp (storage + sync + records)

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (`_FACT_COLS`, `_WORLD_FACT_COLS`, `_LESSON_COLS`, edge insert)
- Modify: `pseudolife_memory/storage/sync.py` (all `_*_record_to_row` + `hydrate_*`)
- Modify: `pseudolife_memory/memory/world_cortex.py`, `…/lessons.py` (record fields)
- Test: extend `tests/test_temporal_stamp.py` with a service round-trip

- [ ] **Step 1: Write the failing test** — append to `tests/test_temporal_stamp.py`:
```python
import tempfile, pytest

@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try: yield s
        finally:
            if s._storage is not None: s._storage.close()

def test_fact_write_persists_stamp(svc):
    svc.cortex_write("server","port","8080", support="user")
    rows = svc._storage.load_facts()
    r = next(x for x in rows if x["entity"]=="server")
    assert r["hlc_phys"] and r["hlc_phys"] > 0
    assert r["tx_time"] and r["valid_time"]
    assert r["writer_id"]            # stamped (default until Task 4 sets identity)
    # survives a fresh hydrate
    from pseudolife_memory.storage import sync
    from pseudolife_memory.memory.cortex import CortexStore
    c = CortexStore(); sync.hydrate_cortex(c, svc._storage)
    rec = c.lookup("server","port")
    assert rec.hlc_phys == r["hlc_phys"] and rec.value == "8080"
```

- [ ] **Step 2: Run → FAIL** (`load_facts` rows lack the keys / `CortexRecord` lacks `hlc_phys`).

- [ ] **Step 3: Implement** — add the seven stamp columns to each `_*_COLS` tuple (after `embedding`/existing trailing cols); add matching fields to `WorldRecord`/`LessonRecord` (cortex done in Task 2); map them in every `sync._*_record_to_row` (write `r.hlc_phys` etc.) and `hydrate_*` (read them back, `torch`-agnostic — they're scalars). The edge insert (`upsert_edge`) writes `tx_time`/`hlc_*`/`writer_id`/`session_id`/`version` too. Service sets them on write (Task 2 already passes `hlc`; here ensure `_record_to_row` carries them).

- [ ] **Step 4: Run → PASS**; then `…/pytest tests/test_pg_storage.py tests/test_lessons_storage.py tests/test_write_through.py -q`.

- [ ] **Step 5: Commit**
```bash
git commit -am "feat(storage): persist + hydrate the temporal/provenance stamp across facts/world/lessons/edges"
```

---

## Task 4: Writer/session identity (config + handshake + stamping)

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (writer id default), `…/shim.py` (send header), `…/daemon.py` (read header → contextvar + session uuid), `…/service.py` (use the context on writes + supersession log), `…/mcp_server.py`
- Test: `tests/test_writer_keying.py` (new)

- [ ] **Step 1: Write the failing tests** — `tests/test_writer_keying.py` (PG + live daemon, mirror `test_daemon_http.py`): start a daemon with `PSEUDOLIFE_MCP_TOKEN`, call `memory_fact_set` over HTTP with an `X-PL-Writer: codex-test` header, then assert the persisted fact row has `writer_id='codex-test'` and a non-null `session_id`; a user-tier supersession records `writer_id` in the supersession log entry.

- [ ] **Step 2: Run → FAIL** (writer_id is the default, not `codex-test`).

- [ ] **Step 3: Implement**
  - `service.py`: a module `contextvars.ContextVar` `_WRITER_CTX = ("writer_id","session_id")`; writes read it (default `writer_id` from `PSEUDOLIFE_WRITER_ID` env or `"unknown"`). Stamp records + append `writer_id`/`session_id` to each supersession-log entry (`cortex._log`).
  - `daemon.py`: per MCP request, read `X-PL-Writer` header, mint `session_id=uuid4()` (once per connection, cached), set the contextvar for the call.
  - `shim.py`: set `X-PL-Writer` from `PSEUDOLIFE_WRITER_ID` on the outbound daemon connection.

- [ ] **Step 4: Run → PASS**; `…/pytest tests/test_writer_keying.py tests/test_daemon_http.py -q`.

- [ ] **Step 5: Commit** `feat(keying): per-connection writer_id/session_id handshake + write/supersession stamping`

---

## Task 5: Bitemporal `valid_time` wiring

**Files:** Modify `pseudolife_memory/service.py` (+ `dream` lesson synth), Test: extend `tests/test_lessons_service.py`

- [ ] **Step 1: Failing test** — a lesson synthesised from a signal carries `valid_time` = the signal's `created_at` (not the dream's `tx_time`); a plain `cortex_write` defaults `valid_time == tx_time`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `synthesize_lessons`/`lesson_write` accept `valid_time` (from the contributing signal's `created_at`); `cortex_write`/`world_write` default `valid_time=tx_time`. Thread through `_*_record_to_row`.
- [ ] **Step 4: Run → PASS** (`…/pytest tests/test_lessons_service.py -q`).
- [ ] **Step 5: Commit** `feat(time): bitemporal valid_time (event time) distinct from tx_time`

---

## Task 6: `write_mode` seam (snapshot live; occ dormant stub)

**Files:** Modify `pseudolife_memory/utils/config.py` (`StorageConfig.write_mode`), `…/storage/postgres.py`, Test: extend `tests/test_pg_storage.py`

- [ ] **Step 1: Failing test** — `MemoryService` default `config.storage.write_mode == "snapshot"`; calling the (internal) occ upsert path raises `NotImplementedError("write_mode=occ is Phase 2")`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — add `StorageConfig` (`write_mode: str = "snapshot"`) wired into `AppConfig`; a `replace_facts_occ(...)` stub that raises `NotImplementedError` (documented Phase-2 seam); snapshot path unchanged. `version` is written (defaults to 1) by the existing snapshot path.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(storage): write_mode config seam (snapshot live; occ dormant Phase-2 stub)`

---

## Task 7: Schema-collision elimination (rename + pin + cleanup migration)

**Files:**
- Modify: `pseudolife_memory/storage/age.py` (`GRAPH_NAME` from config), `…/utils/config.py` (`GraphConfig.name`), `…/service.py` (pass graph name; assert search_path pinned)
- Create: `ops/migrate_v04.py`
- Test: `tests/test_collision_migration.py` (new, needs AGE)

- [ ] **Step 1: Write the failing test** — `tests/test_collision_migration.py` (uses `pg_conn` against the AGE dev image): seed a fake shadow schema (`CREATE SCHEMA pseudolife; CREATE TABLE pseudolife.entries(...)` with a row) and an AGE graph named `pseudolife`; run `migrate_v04.run(conn, apply=True)`; assert: (a) a graph named `pseudolife_graph` exists and a Cypher round-trip works (`age_sync` rebuilt it), (b) the old `pseudolife` graph is gone, (c) the shadow `pseudolife.entries` table is dropped, (d) `meta.schema_version` is current. Also assert `migrate_v04.run(conn, apply=False)` (dry-run) mutates nothing.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**
  - `age.py`: `GRAPH_NAME = os.environ.get("PSEUDOLIFE_GRAPH_NAME", "pseudolife_graph")`; `AgeGraph(conn, name=...)` already parameterised — thread `config.graph.name` from the service.
  - `service.py`: after storage init, assert the connection's `search_path` begins with `public` (raise a clear error if not — the invariant), and construct `AgeGraph(conn, name=config.graph.name)`.
  - `ops/migrate_v04.py`: argparse `--apply` (default dry-run). Steps, each printed: backup reminder; `SET search_path TO public, ag_catalog`; if a graph named `pseudolife` exists → create `pseudolife_graph`, `AgeGraph(conn,'pseudolife_graph').resync(storage)` to rebuild from the truth tables, `SELECT drop_graph('pseudolife', true)`; drop shadow relational tables `pseudolife.{entries,facts,relations,edges,entities,entity_aliases,episodes,meta}` (only if a sibling `public.<t>` exists — safety); upsert `meta.schema_version`. Dry-run prints the plan and changes nothing.

- [ ] **Step 4: Run → PASS** (`…/pytest tests/test_collision_migration.py -v`).

- [ ] **Step 5: Commit** `feat(graph): rename AGE graph off the role name + guarded shadow-schema migration`

---

## Task 8: Presentation — relative-age, `memory_history`, `retire_by_writer`

**Files:** Modify `pseudolife_memory/service.py` (serialisers + `memory_history` + relative-age), `…/mcp_server.py` (`memory_history` tool), `…/utils/config.py` (`TimeConfig.relative_age`); Create `ops/retire_by_writer.py`; Test: extend `tests/test_cortex_service.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: Failing tests** — `_cortex_record_to_dict` includes a human `age` field (e.g. `"3 days ago"`) when `time.relative_age` is on; `memory_history("server","port")` returns the version timeline (current + superseded, each with `writer_id` + `tx_time`); `memory_history` is in `test_all_tools_registered`'s expected set.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — reuse `context_builder._relative_time`; add `MemoryService.history(entity, attribute)` (reads current + superseded records for the slot); `@mcp.tool() memory_history`; `ops/retire_by_writer.py` (dry-run-first: lists, `--apply` sets `status='superseded'` for that `writer_id`/`session_id`).
- [ ] **Step 4: Run → PASS** (`…/pytest tests/test_cortex_service.py tests/test_mcp_server.py -q`).
- [ ] **Step 5: Commit** `feat(time): relative-age on reads + memory_history + retire_by_writer ops`

---

## Task 9: Docs + full verification

**Files:** `README.md`, `CHANGELOG.md`, `ops/docker-compose.yml`

- [ ] **Step 1:** README — add a "Sense of time + multi-writer" section (the stamp, HLC ordering, writer keying, `write_mode` seam, the collision fix); capabilities table row; bump schema version to v11. CHANGELOG `[Unreleased]` entry. Compose: set `PSEUDOLIFE_WRITER_ID: claude-code` + `PSEUDOLIFE_GRAPH_NAME: pseudolife_graph` on the daemon.
- [ ] **Step 2: Full suite** — `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest -q`. Expected: all pass (prior 362 + new tests).
- [ ] **Step 3:** If green, finish the branch per superpowers:finishing-a-development-branch (PR or merge per the user). The live collision-migration is a SEPARATE, backup-first deploy step (`ops/migrate_v04.py --apply` after `ops/backup.ps1`), run with the user — **not** part of CI.
- [ ] **Step 4: Commit** `docs: v0.4 temporal/multi-writer-ready memory + compose writer id/graph name`

---

## Notes for the executor

- **DRY/YAGNI:** the stamp columns are identical across the four tables — factor the per-row mapping so you write it once where practical, but don't over-abstract the four `_*_COLS` tuples (they already differ).
- **Back-compat:** every new `write_fact`/`*_write` param defaults so existing tests/callers keep working; `hlc=None` ⇒ legacy ordering. Run the full suite after Tasks 2, 3, 7.
- **The live migration is deploy-time, not test-time.** Tests prove `migrate_v04` on a seeded test DB; the real run is `ops/backup.ps1` then `ops/migrate_v04.py --apply` against the live bank, with the user.
- **Phase 2 is out of scope.** Leave the `occ` path a clear `NotImplementedError` stub; do not build per-row CAS / distributed HLC / cache invalidation here.
