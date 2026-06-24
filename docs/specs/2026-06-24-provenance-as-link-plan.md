# Provenance-as-link — Plan 1: the engram index + retrieval

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each cortex fact a bidirectional link to the dense episodes it was consolidated from, dereferenceable on demand, and let an agent reinforce a useful episode.

**Architecture:** A new `memory_traces` link table (`fact_id ↔ entry_id`) written by the dream as it consolidates; a `reinforcements` counter on entries bumped at encoding and by an explicit reinforce; new `memory_get` / `memory_reinforce` MCP tools; `source_entries` surfaced on fact reads. This is **Phase 1 of 2** — Phase 2 (the MTT eviction-retention boost that *reads* `reinforcements`) is a separate plan.

**Tech Stack:** Python 3.10+, psycopg3, Postgres, pytest.

## Global Constraints

- Offline baseline (`HF_HUB_OFFLINE=1`, CPU). No new dependency. Test runner: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest`. Bench Postgres at `127.0.0.1:5433`.
- PG integration tests use the `_pg_up()` skipif + `build_service(tmp_path)` pattern (from `evals/ladder_sweep`) already in `tests/test_recall.py`.
- Additive schema only (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`); `SCHEMA_META_VERSION` 12→13; no destructive change.
- The whole feature is gated by `config.memory.traces.enabled` (default True) and must be a no-op when disabled.
- Scope is **fact traces only** (entity→attribute→value facts ↔ source entries). The dream's separate *relation* extraction (graph edges) is unchanged in Phase 1.
- Spec: [docs/specs/2026-06-24-provenance-as-link-design.md](2026-06-24-provenance-as-link-design.md).

## Grounding (verified signatures)
- `entries` table: `id BIGSERIAL PK, text, embedding, source, ...`. `MemoryEntry.db_id` is `entries.id`.
- `facts` table: `id BIGINT, entity, attribute, entity_norm, attribute_norm, value, status, ...`. Facts are *superseded* (status), not deleted, on forget.
- `MemoryService.cortex_write(entity, attribute, value, *, confidence, support, ...) -> {"action", ...}`; the underlying `CortexStore.write_fact` returns a `WriteResult(action, record)` where `record` is a `CortexRecord` **with no `id`** → the fact's row id must be resolved by slot.
- The dream (`service.dream_run`): `pulled = self.dream_pull(...)`; `entries = pulled["entries"]` (dicts: `text`, `timestamp`, `episode_id` — **no `db_id` yet**); `claims = extractor.extract(texts, vocab)` where `extract(texts: list[str], vocab) -> list[Claim]` (claims carry no source index); per-claim `self.cortex_write(ent, attr, c["value"], ...)`.
- Storage idiom: `self.conn.execute(SQL, params).fetchall()`; `with self.conn.cursor() as cur: cur.executemany(...)`; every write ends `self.conn.commit()`.
- `cortex._norm_key(s)` normalises entity/attribute for the `*_norm` columns.

## File structure
- `pseudolife_memory/storage/schema.py` — `memory_traces` table + `entries.reinforcements` column; version 13.
- `pseudolife_memory/storage/postgres.py` — `add_trace`, `fact_id_for_slot`, `traces_for_fact`, `facts_for_entry`, `get_entry`, `bump_reinforcements`.
- `pseudolife_memory/utils/config.py` — `TracesConfig` (`enabled`).
- `pseudolife_memory/service.py` — `dream_pull` exposes `db_id`; dream attributes claims per-entry and writes traces; `memory_get`/`reinforce` service methods; `source_entries` on `cortex_records`/`cortex_search`.
- `pseudolife_memory/mcp_server.py` — `memory_get`, `memory_reinforce` tools.
- Tests: `tests/test_schema_v13.py` (new), additions to `tests/test_graph.py` (storage) and `tests/test_recall.py` (integration + tools).

---

## Task 1: Schema v13 + TracesConfig

**Files:**
- Modify: `pseudolife_memory/storage/schema.py`
- Modify: `pseudolife_memory/utils/config.py`
- Test: `tests/test_schema_v13.py` (new), `tests/test_dream.py` (config default)

**Interfaces:**
- Produces: `memory_traces(fact_id, entry_id, created_at)` table + `entries.reinforcements` column; `SCHEMA_META_VERSION == 13`; `TracesConfig(enabled=True)` on `MemoryConfig.traces`.

- [ ] **Step 1: Add the DDL + bump version**

In `pseudolife_memory/storage/schema.py`, change `SCHEMA_META_VERSION = 12` to `13`. Append to `SCHEMA_SQL` (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS memory_traces (
  fact_id    BIGINT NOT NULL REFERENCES facts(id)   ON DELETE CASCADE,
  entry_id   BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (fact_id, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_traces_entry_idx ON memory_traces (entry_id);
```

The `entries.reinforcements` column is an additive ALTER. In `ensure_schema` (after `cur.execute(SCHEMA_SQL)`), add:

```python
        cur.execute(
            "ALTER TABLE entries ADD COLUMN IF NOT EXISTS reinforcements "
            "INTEGER NOT NULL DEFAULT 0"
        )
```

- [ ] **Step 2: Add `TracesConfig`**

In `pseudolife_memory/utils/config.py`, near `GraphInsightConfig`:

```python
@dataclass
class TracesConfig:
    """Engram cross-index (provenance-as-link). When enabled, the dream links
    each consolidated fact to the dense episodes it came from and bumps their
    reinforcement counter. retention_boost (Phase 2) reads that counter."""
    enabled: bool = True
```

And add to `MemoryConfig` (alongside `graph_insight`):

```python
    traces: TracesConfig = field(default_factory=TracesConfig)
```

- [ ] **Step 3: Write the failing tests**

Append to `tests/test_dream.py`:

```python
def test_traces_config_default():
    from pseudolife_memory.utils.config import TracesConfig, MemoryConfig
    assert TracesConfig().enabled is True
    assert MemoryConfig().traces.enabled is True
```

Create `tests/test_schema_v13.py`:

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
def test_schema_v13_traces_and_reinforcements(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    assert SCHEMA_META_VERSION == 13
    svc = build_service(tmp_path)
    svc._ensure_init()  # noqa: SLF001
    st = svc._storage  # noqa: SLF001
    assert st.conn.execute("SELECT to_regclass('public.memory_traces')").fetchone()[0]
    col = st.conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='entries' AND column_name='reinforcements'").fetchone()
    assert col is not None
```

- [ ] **Step 4: Run tests**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_dream.py::test_traces_config_default tests/test_schema_v13.py -v`
Expected: PASS (config test pure; schema test RAN against bench PG, both objects present, version 13).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/utils/config.py tests/test_dream.py tests/test_schema_v13.py
git commit -m "feat(traces): schema v13 memory_traces table + reinforcements column + TracesConfig"
```

---

## Task 2: Storage — trace read/write + reinforcement + get_entry

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py`
- Test: `tests/test_graph.py` (uses its PG `svc` fixture's `_storage`)

**Interfaces:**
- Consumes: schema v13 (Task 1).
- Produces: `add_trace(fact_id, entry_id, now)`, `fact_id_for_slot(entity_norm, attribute_norm) -> int | None`, `traces_for_fact(fact_id) -> list[int]`, `facts_for_entry(entry_id) -> list[dict]`, `get_entry(entry_id) -> dict | None`, `bump_reinforcements(entry_id, delta) -> None`, `bump_access_count(entry_id, delta) -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph.py`:

```python
def test_memory_traces_storage_roundtrip(svc):
    st = svc._storage  # noqa: SLF001
    # Real ids: create two entities + a fact + an entry via the public paths.
    svc.graph_relate("tr-a", "depends-on", "tr-b")
    svc.cortex_write("tr-a", "role", "frontend", support="user")
    g = st.load_graph()
    # A stored memory entry to link to.
    svc.store("tr-a is the frontend role", source="general")
    import time as _t
    entry_id = st.conn.execute(
        "SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    from pseudolife_memory.memory.cortex import _norm_key
    fid = st.fact_id_for_slot(_norm_key("tr-a"), _norm_key("role"))
    assert fid is not None
    st.add_trace(fid, entry_id, _t.time())
    st.add_trace(fid, entry_id, _t.time())          # idempotent on PK
    assert st.traces_for_fact(fid) == [entry_id]
    assert any(f["id"] == fid for f in st.facts_for_entry(entry_id))
    assert st.get_entry(entry_id)["text"] == "tr-a is the frontend role"
    assert st.get_entry(10_000_000) is None
    st.bump_reinforcements(entry_id, 2)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (entry_id,)).fetchone()[0] == 2
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k memory_traces_storage -v`
Expected: FAIL — `AttributeError: 'PostgresStorage' object has no attribute 'add_trace'`.

- [ ] **Step 2: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, add to `PostgresStorage` (near `load_graph`):

```python
    def add_trace(self, fact_id: int, entry_id: int, now: float) -> None:
        self.conn.execute(
            "INSERT INTO memory_traces (fact_id, entry_id, created_at) "
            "VALUES (%s, %s, %s) ON CONFLICT (fact_id, entry_id) DO NOTHING",
            (fact_id, entry_id, now),
        )
        self.conn.commit()

    def fact_id_for_slot(self, entity_norm: str, attribute_norm: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM facts WHERE entity_norm = %s AND attribute_norm = %s "
            "AND status = 'current' ORDER BY id DESC LIMIT 1",
            (entity_norm, attribute_norm),
        ).fetchone()
        return row[0] if row else None

    def traces_for_fact(self, fact_id: int) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT entry_id FROM memory_traces WHERE fact_id = %s ORDER BY entry_id",
            (fact_id,)).fetchall()]

    def facts_for_entry(self, entry_id: int) -> list[dict]:
        cols = ("id", "entity", "attribute", "value")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            f"SELECT f.{', f.'.join(cols)} FROM facts f "
            "JOIN memory_traces t ON t.fact_id = f.id WHERE t.entry_id = %s "
            "ORDER BY f.id", (entry_id,)).fetchall()]

    def get_entry(self, entry_id: int) -> dict | None:
        cols = ("id", "text", "source", "ts")
        row = self.conn.execute(
            "SELECT id, text, source, ts FROM entries WHERE id = %s",
            (entry_id,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def bump_reinforcements(self, entry_id: int, delta: int) -> None:
        self.conn.execute(
            "UPDATE entries SET reinforcements = reinforcements + %s WHERE id = %s",
            (delta, entry_id))
        self.conn.commit()

    def bump_access_count(self, entry_id: int, delta: int) -> None:
        self.conn.execute(
            "UPDATE entries SET access_count = access_count + %s WHERE id = %s",
            (delta, entry_id))
        self.conn.commit()
```

(`entries` columns are `id, band, text, embedding, surprise, ts, access_count,
source, ...` — `get_entry` selects `id/text/source/ts`. `update_access_counts`
already exists for the bulk save-cadence path; `bump_access_count` is the
single-entry version `memory_get` needs.)

- [ ] **Step 3: Run the test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k memory_traces_storage -v`
Expected: PASS (ran against bench PG).

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_graph.py
git commit -m "feat(traces): storage — add_trace/fact_id_for_slot/traces/facts_for_entry/get_entry/bump"
```

---

## Task 3: Dream wiring — attribute claims to entries + write traces

**Files:**
- Modify: `pseudolife_memory/service.py` (`dream_pull`, `dream_run`)
- Test: `tests/test_recall.py` (PG integration)

**Interfaces:**
- Consumes: storage methods (Task 2); `config.memory.traces.enabled` (Task 1).
- Produces: `dream_pull` entries carry `db_id`; after consolidation, `memory_traces` rows exist for each fact↔source-entry and `entries.reinforcements` is bumped per trace. `dream_run` summary gains `traces` count.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_dream_writes_fact_traces(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import RegexExtractor
    svc = build_service(tmp_path)
    # A memory whose text the regex floor extracts a fact from.
    svc.store("trace-svc runs-on jdk-21", source="general")
    out = svc.dream_run(RegexExtractor())
    assert out["pulled"] >= 1
    assert out.get("traces", 0) >= 1
    st = svc._storage  # noqa: SLF001
    # The entry that produced the fact is now reinforced + linked.
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert st.facts_for_entry(eid)                      # entry -> fact(s)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0] >= 1
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k dream_writes_fact_traces -v`
Expected: FAIL — `KeyError: 'traces'` (and no trace rows).

- [ ] **Step 2: Expose `db_id` from `dream_pull`**

In `dream_pull`'s returned entries dict, add the row id:

```python
                "entries": [
                    {
                        "text": e.text,
                        "timestamp": e.timestamp,
                        "episode_id": e.episode_id,
                        "db_id": e.db_id,
                    }
                    for e in rows
                ],
```

- [ ] **Step 3: Attribute claims per-entry and write traces in `dream_run`**

Replace the batched extract + fact-write block. Where `dream_run` currently does
`texts = [e["text"] for e in entries]` / `claims = extractor.extract(texts, vocab)` /
the `for c in claims:` loop, use per-entry extraction so each claim carries its
source `db_id`, and write a trace after each `cortex_write`:

```python
        from pseudolife_memory.memory.cortex import _norm_key
        import time as _time
        traces_cfg = self.config.memory.traces
        vocab = self.cortex_vocab().get("slots", [])
        tally = {"inserted": 0, "confirmed": 0, "contested": 0, "superseded": 0}
        traces_n = 0
        try:
            for e in entries:
                src_id = e.get("db_id")
                e_claims = extractor.extract([e["text"]], vocab)
                for c in e_claims:
                    ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
                    res = self.cortex_write(
                        ent, attr, c["value"],
                        confidence=float(c.get("confidence", 0.55)),
                        support=c.get("origin", "agent"))
                    tally[res["action"]] = tally.get(res["action"], 0) + 1
                    if traces_cfg.enabled and src_id is not None and self._storage is not None:
                        fid = self._storage.fact_id_for_slot(_norm_key(ent), _norm_key(attr))
                        if fid is not None:
                            self._storage.add_trace(fid, src_id, _time.time())
                            self._storage.bump_reinforcements(src_id, 1)
                            traces_n += 1
        except Exception as exc:  # noqa: BLE001 — an extractor must never break a dream
            logger.warning("dream extractor failed (%s); cursor NOT advanced, "
                           "will retry next sweep", exc)
            return {"pulled": len(entries), "claims": 0, "inserted": 0,
                    "confirmed": 0, "contested": 0, "superseded": 0, "relations": 0,
                    "cursor": self._cortex.dream_cursor, "extractor_failed": True,
                    "lessons": {"signals": 0, "lessons": 0}}
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        texts = [e["text"] for e in entries]
        relations_n = self._dream_extract_relations(extractor, texts)
        lessons = self.synthesize_lessons(extractor)
        graph_insight = self._safe_refresh_graph_insight()
        return {"pulled": len(entries), "claims": sum(tally.values()),
                "cursor": newest, "relations": relations_n, **tally,
                "lessons": lessons, "graph_insight": graph_insight,
                "traces": traces_n}
```

(This replaces the existing `texts`/`claims`/`for c in claims` block and the existing
main-path `return`; keep the no-entries and the structure above it intact. `claims`
is now the per-tally sum.)

- [ ] **Step 4: Run the test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "dream_writes_fact_traces or graph_insight or hub_gating" -v`
Expected: PASS — traces written, reinforcements bumped, and the existing dream/recall PG tests still green.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(traces): dream attributes claims per-entry and writes fact->episode traces"
```

---

## Task 4: Retrieval surface — memory_get, memory_reinforce, source_entries

**Files:**
- Modify: `pseudolife_memory/service.py` (`memory_get`, `reinforce`, `source_entries` on fact reads)
- Modify: `pseudolife_memory/mcp_server.py` (`memory_get`, `memory_reinforce` tools)
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: storage methods (Task 2); dream traces (Task 3).
- Produces: `MemoryService.get_entry(entry_id) -> dict`, `MemoryService.reinforce(entry_id) -> dict`; `source_entries` on `cortex_records` / `cortex_search`; MCP tools `memory_get`, `memory_reinforce`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_memory_get_and_reinforce_roundtrip(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import RegexExtractor
    import pseudolife_memory.mcp_server as srv
    svc = build_service(tmp_path)
    svc.store("getme-svc runs-on jdk-22", source="general")
    svc.dream_run(RegexExtractor())
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    monkeypatch.setattr(srv, "service", svc, raising=False)
    got = srv.memory_get(eid)
    assert got["found"] is True and "getme-svc" in got["text"]
    assert got["consolidated_into"]                      # entry -> facts
    before = st.conn.execute("SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0]
    assert srv.memory_reinforce(eid)["reinforced"] is True
    after = st.conn.execute("SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0]
    assert after == before + 1
    assert srv.memory_get(9_000_001) == {"found": False, "faded": True}
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k memory_get_and_reinforce -v`
Expected: FAIL — `AttributeError: module 'pseudolife_memory.mcp_server' has no attribute 'memory_get'`.

- [ ] **Step 2: Service methods**

In `pseudolife_memory/service.py`, add to `MemoryService`:

```python
    def get_entry(self, entry_id: int) -> dict[str, Any]:
        """Dereference a trace pointer: the dense episode + the facts it formed.
        Bumps access_count (ambient reinforcement). {found: False, faded: True}
        when the episode has evicted."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"found": False, "faded": True}
            row = self._storage.get_entry(int(entry_id))
            if row is None:
                return {"found": False, "faded": True}
            self._storage.bump_access_count(int(entry_id), 1)
            facts = self._storage.facts_for_entry(int(entry_id))
        return {"found": True, "entry_id": row["id"], "text": row["text"],
                "source": row.get("source"), "consolidated_into": facts}

    def reinforce(self, entry_id: int) -> dict[str, Any]:
        """The 'this episode was useful' signal — bump reinforcements (Phase-2
        retention reads it). No-op on a faded episode."""
        with self._lock:
            self._ensure_init()
            if self._storage is None or self._storage.get_entry(int(entry_id)) is None:
                return {"reinforced": False, "faded": True}
            self._storage.bump_reinforcements(int(entry_id), 1)
        return {"reinforced": True, "entry_id": int(entry_id)}
```

(`bump_access_count` was added in Task 2.)

For `source_entries` surfacing: in `cortex_records` and `cortex_search`, after each
record dict `d` is built, attach the source entries (the fact's id must be resolved
the same way as the dream — by slot):

```python
            if self._storage is not None:
                fid = self._storage.fact_id_for_slot(
                    _norm_key(r.entity), _norm_key(r.attribute))
                d["source_entries"] = (
                    self._storage.traces_for_fact(fid) if fid is not None else [])
```

(Import `_norm_key` from `pseudolife_memory.memory.cortex` at the top of the methods,
matching the dream's usage. These reads already run under the cortex lock.)

- [ ] **Step 3: MCP tools**

In `pseudolife_memory/mcp_server.py`, add (near `memory_facts`):

```python
@mcp.tool()
def memory_get(entry_id: int) -> dict[str, Any]:
    """Dereference a source-entry pointer from a fact's `source_entries`: returns
    the full dense memory episode and `consolidated_into` (the facts it formed).
    Reading it gently reinforces it. `{found: false, faded: true}` if the episode
    has since been forgotten.
    """
    return service.get_entry(entry_id)


@mcp.tool()
def memory_reinforce(entry_id: int) -> dict[str, Any]:
    """After reading an episode via `memory_get` and finding it genuinely useful,
    call this to strengthen it — a deliberate 'this mattered' signal that helps the
    episode resist forgetting. Read first, then reinforce.
    """
    return service.reinforce(entry_id)
```

- [ ] **Step 4: Run the test + the full recall suite**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: PASS (the round-trip test + all prior recall/trace tests).

- [ ] **Step 5: Update docs + commit**

Add a one-line CHANGELOG entry under `## [Unreleased]` → `### Added`:

```markdown
- **Provenance-as-link (Phase 1)** — the dream now links each consolidated fact to
  the dense episodes it came from (`memory_traces`); facts surface `source_entries`,
  and new `memory_get` / `memory_reinforce` tools dereference and strengthen them.
```

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py tests/test_recall.py CHANGELOG.md
git commit -m "feat(traces): memory_get + memory_reinforce + source_entries on facts"
```

---

## Final verification
- [ ] `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_schema_v13.py tests/test_dream.py tests/test_graph.py tests/test_recall.py -q`
- [ ] Confirm `SCHEMA_META_VERSION == 13`; the new PG integration tests RAN (not skipped); existing suite green.

## Self-review notes (coverage vs spec, Phase 1)
- Engram table + CASCADE + bidirectional (`traces_for_fact` / `facts_for_entry`) → Tasks 1–2.
- Multi-trace accrual (idempotent PK; a fresh confirming episode adds its own trace) → Task 2 PK + Task 3 per-entry loop.
- Consolidation wiring (per-entry attribution; fact id resolved by slot) → Task 3.
- `reinforcements` written at encoding + on explicit reinforce → Tasks 3 + 4. (The eviction *boost* that reads it = Phase 2, separate plan.)
- `memory_get` (+ ambient access bump), `memory_reinforce`, `source_entries` surfacing → Task 4.
- Faded handling (`{found:false, faded:true}`) → Task 4 + Task 2 `get_entry` None.
- Config gate (`traces.enabled`) → Tasks 1 + 3.

## Phase 2 (separate plan, after this lands + deploys)
MTT retention: `MemoryEntry.reinforcements` field + load it; `RetentionPolicy.retention_boost` + `source_weighted_score += retention_boost·log1p(reinforcements)`; thread `config.memory.traces.retention_boost` into band policy construction; in-memory sync of `reinforcements` on bump; deterministic eviction-order tests. Default `retention_boost=0.0` (no behaviour change) for thorough tuning.
