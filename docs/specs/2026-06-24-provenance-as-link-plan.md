# Provenance-as-link — Plan 1: the engram index + retrieval

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each cortex fact a bidirectional link to the dense episodes it was consolidated from, dereferenceable on demand, and let an agent reinforce a useful episode.

**Architecture:** A new `memory_traces` link table keyed on the **stable canonical slot** (`entity_norm, attribute_norm` ↔ `entry_id`) — written by the dream as it consolidates; a `reinforcements` counter on entries bumped at encoding and by an explicit reinforce; new `memory_get` / `memory_reinforce` MCP tools; `source_entries` surfaced on fact reads. This is **Phase 1 of 2** — Phase 2 (the MTT eviction-retention boost that *reads* `reinforcements`) is a separate plan.

> **Anchor = the slot, NOT `facts.id` (2026-06-25 correction).** The cortex persists by **snapshot rewrite**: `cortex_write → _save_cortex → snapshot_cortex → PostgresStorage.replace_facts`, which does `DELETE FROM facts` then re-`INSERT`s every current row *without* an id (fresh `BIGSERIAL` each time), after **every** cortex write. So `facts.id` is **ephemeral** and a trace keyed on it with `ON DELETE CASCADE` would be wiped at the next cortex write. We therefore key `memory_traces` on the cortex's stable slot `(entity_norm, attribute_norm)` (which matches `facts.entity_norm/attribute_norm`). The episode side keeps `entry_id REFERENCES entries(id) ON DELETE CASCADE` because `entries.id` **is** stable. See [the design](2026-06-24-provenance-as-link-design.md), "Anchor correction".

**Tech Stack:** Python 3.10+, psycopg3, Postgres, pytest.

## Global Constraints

- Offline baseline (`HF_HUB_OFFLINE=1`, CPU). No new dependency. Test runner: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest`. Bench Postgres at `127.0.0.1:5433`.
- PG integration tests use the `_pg_up()` skipif + `build_service(tmp_path)` pattern (from `evals/ladder_sweep`) already in `tests/test_recall.py`. The `tests/test_graph.py` storage tests use that file's module-scoped `svc` fixture.
- Additive schema only (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`); `SCHEMA_META_VERSION` 12→13; no destructive change. v13 is **unreleased**, so the slot-keyed table shape is simply the v13 definition (no production migration from any earlier shape).
- The whole feature is gated by `config.memory.traces.enabled` (default True) and must be a no-op when disabled.
- Scope is **fact traces only** (entity→attribute→value slots ↔ source entries). The dream's separate *relation* extraction (graph edges) is unchanged in Phase 1 and stays **batched**.

## Grounding (verified signatures)
- `entries` table: `id BIGSERIAL PK, band, text, embedding, surprise, ts, access_count, source, ...` — `entries.id` is **stable** (insert once, update in place). `MemoryEntry.db_id` is `entries.id`.
- `facts` table: `id BIGSERIAL, entity, attribute, entity_norm, attribute_norm, value, status, ...` with `facts_slot_idx` on `(entity_norm, attribute_norm, status)`. Current facts have `status='current'` (cortex writes that; superseded/forgotten facts are kept, not deleted). **`facts.id` churns on every save** (snapshot rewrite) — never reference it from another table.
- `MemoryService.cortex_write(entity, attribute, value, *, confidence, support, ...) -> {"action", ...}`; it calls `_save_cortex()` (→ `replace_facts`) after every write. The `CortexRecord` carries **no `id`** — the slot `(entity_norm, attribute_norm)` is its stable identity.
- The dream (`service.dream_run`): `pulled = self.dream_pull(...)`; `entries = pulled["entries"]` (dicts: `text`, `timestamp`, `episode_id` — **no `db_id` yet**); `claims = extractor.extract(texts, vocab)` where `extract(texts: list[str], vocab) -> list[Claim]` (claims carry no source index); per-claim `ent, attr = self._resolve_dream_slot(...)` then `self.cortex_write(ent, attr, c["value"], ...)`.
- Storage idiom: `self.conn.execute(SQL, params).fetchall()/.fetchone()`; `with self.conn.cursor() as cur: cur.executemany(...)`; every write ends `self.conn.commit()`.
- `cortex._norm_key(s)` normalises entity/attribute for the `*_norm` columns (the same normalisation `facts.entity_norm/attribute_norm` are stored under).

## File structure
- `pseudolife_memory/storage/schema.py` — slot-keyed `memory_traces` table + `entries.reinforcements` column; version 13.
- `pseudolife_memory/storage/postgres.py` — `add_trace`, `traces_for_slot`, `facts_for_entry`, `get_entry`, `bump_reinforcements`, `bump_access_count`.
- `pseudolife_memory/utils/config.py` — `TracesConfig` (`enabled`).
- `pseudolife_memory/service.py` — `dream_pull` exposes `db_id`; dream attributes claims per-entry and writes slot traces; `memory_get`/`reinforce` service methods; `source_entries` on `cortex_search`/`cortex_lookup`/`cortex_dump`.
- `pseudolife_memory/mcp_server.py` — `memory_get`, `memory_reinforce` tools.
- Tests: `tests/test_schema_v13.py` (new), additions to `tests/test_graph.py` (storage) and `tests/test_recall.py` (integration + tools).

---

## Task 1: Schema v13 + TracesConfig

**Files:**
- Modify: `pseudolife_memory/storage/schema.py`
- Modify: `pseudolife_memory/utils/config.py`
- Test: `tests/test_schema_v13.py` (new), `tests/test_dream.py` (config default)

**Interfaces:**
- Produces: slot-keyed `memory_traces(entity_norm, attribute_norm, entry_id, created_at)` table + `entries.reinforcements` column; `SCHEMA_META_VERSION == 13`; `TracesConfig(enabled=True)` on `MemoryConfig.traces`.

- [ ] **Step 1: Add the DDL + bump version**

In `pseudolife_memory/storage/schema.py`, change `SCHEMA_META_VERSION = 12` to `13`. Append to `SCHEMA_SQL` (before the closing `"""`, after the v12 community tables):

```sql
-- v13 engram cross-index (provenance-as-link). Keyed on the STABLE canonical
-- slot (entity_norm, attribute_norm) — NOT facts.id, which is regenerated on
-- every cortex snapshot save. entry_id keeps a CASCADE FK (entries.id is stable),
-- so an evicting episode auto-removes its traces.
CREATE TABLE IF NOT EXISTS memory_traces (
  entity_norm    TEXT   NOT NULL,
  attribute_norm TEXT   NOT NULL,
  entry_id       BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  created_at     DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_norm, attribute_norm, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_traces_entry_idx ON memory_traces (entry_id);
```

The `entries.reinforcements` column is an additive ALTER. In `ensure_schema` (after `cur.execute(SCHEMA_SQL)`, alongside the existing additive-ALTER block), add:

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
    each consolidated fact-slot to the dense episodes it came from and bumps their
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
    cols = {r[0] for r in st.conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='memory_traces'").fetchall()}
    # Slot-keyed shape (NOT fact_id) — lock it so a regression to the old anchor fails.
    assert {"entity_norm", "attribute_norm", "entry_id", "created_at"} <= cols
    assert "fact_id" not in cols
    col = st.conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='entries' AND column_name='reinforcements'").fetchone()
    assert col is not None
```

- [ ] **Step 4: Run tests**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_dream.py::test_traces_config_default tests/test_schema_v13.py -v`
Expected: PASS (config test pure; schema test RAN against bench PG — table present with the slot-keyed columns, `reinforcements` present, version 13).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/utils/config.py tests/test_dream.py tests/test_schema_v13.py
git commit -m "feat(traces): schema v13 slot-keyed memory_traces + reinforcements column + TracesConfig"
```

---

## Task 2: Storage — slot-keyed trace read/write + reinforcement + get_entry

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py`
- Test: `tests/test_graph.py` (uses its PG `svc` fixture's `_storage`)

**Interfaces:**
- Consumes: schema v13 (Task 1).
- Produces: `add_trace(entity_norm, attribute_norm, entry_id, now) -> bool` (True iff a new row inserted), `traces_for_slot(entity_norm, attribute_norm) -> list[int]`, `facts_for_entry(entry_id) -> list[dict]`, `get_entry(entry_id) -> dict | None`, `bump_reinforcements(entry_id, delta) -> None`, `bump_access_count(entry_id, delta) -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph.py`:

```python
def test_memory_traces_storage_roundtrip(svc):
    # Real ids: create two entities + a fact + an entry via the public paths.
    svc.graph_relate("tr-a", "depends-on", "tr-b")
    svc.cortex_write("tr-a", "role", "frontend", support="user")
    st = svc._storage  # noqa: SLF001  (fixture lazy-inits _storage on first svc call)
    svc.store("tr-a is the frontend role", source="general")
    import time as _t
    from pseudolife_memory.memory.cortex import _norm_key
    entry_id = st.conn.execute(
        "SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    en, an = _norm_key("tr-a"), _norm_key("role")
    assert st.add_trace(en, an, entry_id, _t.time()) is True
    assert st.add_trace(en, an, entry_id, _t.time()) is False   # idempotent on PK
    assert st.traces_for_slot(en, an) == [entry_id]
    assert any(f["entity"] == "tr-a" for f in st.facts_for_entry(entry_id))
    assert st.get_entry(entry_id)["text"] == "tr-a is the frontend role"
    assert st.get_entry(10_000_000) is None
    st.bump_reinforcements(entry_id, 2)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (entry_id,)).fetchone()[0] == 2
    # DURABILITY (the anchor-correction regression guard): a later cortex_write
    # triggers a full facts snapshot rewrite (DELETE+reinsert, new fact ids).
    # The slot-keyed trace MUST survive it.
    svc.cortex_write("tr-a", "language", "python", support="user")
    assert st.traces_for_slot(en, an) == [entry_id]
    assert any(f["entity"] == "tr-a" for f in st.facts_for_entry(entry_id))
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k memory_traces_storage -v`
Expected: FAIL — `AttributeError: 'PostgresStorage' object has no attribute 'add_trace'`.

- [ ] **Step 2: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, add to `PostgresStorage` (near `load_graph`):

```python
    def add_trace(self, entity_norm: str, attribute_norm: str,
                  entry_id: int, now: float) -> bool:
        """Link a cortex slot to a source episode. Idempotent on the PK; returns
        True iff a NEW row was inserted (so the caller bumps reinforcements only on
        genuine new formation, never on a re-assert)."""
        row = self.conn.execute(
            "INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (entity_norm, attribute_norm, entry_id) DO NOTHING "
            "RETURNING entry_id",
            (entity_norm, attribute_norm, entry_id, now),
        ).fetchone()
        self.conn.commit()
        return row is not None

    def traces_for_slot(self, entity_norm: str, attribute_norm: str) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT entry_id FROM memory_traces "
            "WHERE entity_norm = %s AND attribute_norm = %s ORDER BY entry_id",
            (entity_norm, attribute_norm)).fetchall()]

    def facts_for_entry(self, entry_id: int) -> list[dict]:
        cols = ("id", "entity", "attribute", "value")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            f"SELECT f.{', f.'.join(cols)} FROM facts f "
            "JOIN memory_traces t ON f.entity_norm = t.entity_norm "
            "AND f.attribute_norm = t.attribute_norm "
            "WHERE t.entry_id = %s AND f.status = 'current' "
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

(`get_entry` selects `id/text/source/ts`. `update_access_counts` already exists for
the bulk save-cadence path; `bump_access_count` is the single-entry version
`memory_get` needs. The `facts_for_entry` f-string interpolates only the hardcoded
`cols` tuple — no user input reaches it; `entry_id` is bound as a parameter.)

- [ ] **Step 3: Run the test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_graph.py -k memory_traces_storage -v`
Expected: PASS (ran against bench PG, including the durability assertion after the second `cortex_write`).

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/storage/postgres.py tests/test_graph.py
git commit -m "feat(traces): storage — slot-keyed add_trace/traces_for_slot/facts_for_entry/get_entry/bump"
```

---

## Task 3: Dream wiring — attribute claims to entries + write slot traces

**Files:**
- Modify: `pseudolife_memory/service.py` (`dream_pull`, `dream_run`)
- Test: `tests/test_recall.py` (PG integration)

**Interfaces:**
- Consumes: storage methods (Task 2); `config.memory.traces.enabled` (Task 1).
- Produces: `dream_pull` entries carry `db_id`; after consolidation, `memory_traces` rows exist for each slot↔source-entry and `entries.reinforcements` is bumped per new trace. `dream_run` summary gains `traces` count.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_dream_writes_fact_traces(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.memory.dream import RegexExtractor
    svc = build_service(tmp_path)
    # A memory whose text the regex floor extracts a fact from (lexicon-gated
    # attribute — "runtime"; verify RegexExtractor yields a claim for it).
    svc.store("trace-svc runtime: jdk-21", source="general")
    out = svc.dream_run(RegexExtractor())
    assert out["pulled"] >= 1
    assert out.get("traces", 0) >= 1
    st = svc._storage  # noqa: SLF001
    # The entry that produced the fact is now reinforced + linked.
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert st.facts_for_entry(eid)                      # entry -> fact(s)
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0] >= 1
    # Durability: the trace survives a SUBSEQUENT cortex write (snapshot rewrite).
    svc.cortex_write("unrelated-x", "kind", "probe", support="user")
    assert st.facts_for_entry(eid)
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k dream_writes_fact_traces -v`
Expected: FAIL — `assert out.get("traces", 0) >= 1` fails at 0 (no `traces` key / no trace rows).

> **Extractor note:** the test relies on `RegexExtractor` (`extract_slots`) yielding a claim for the seed text. The attribute lexicon is gated, so the slot phrasing matters (`"runtime: jdk-21"` is lexicon-friendly; `"runs-on"` is not). If you change the seed text, first confirm `RegexExtractor().extract([text], [])` returns ≥1 claim — otherwise the test can't pass regardless of trace wiring.

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

- [ ] **Step 3: Attribute claims per-entry and write slot traces in `dream_run`**

Replace the block that currently spans from `texts = [e["text"] for e in entries]` (the line *after* the no-entries early-return) through the method's final `return {...}`, with the per-entry version below. **Leave the no-entries `if not entries:` early-return block above it untouched.**

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
                for c in extractor.extract([e["text"]], vocab):
                    ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
                    res = self.cortex_write(
                        ent, attr, c["value"],
                        confidence=float(c.get("confidence", 0.55)),
                        support=c.get("origin", "agent"))
                    tally[res["action"]] = tally.get(res["action"], 0) + 1
                    if (traces_cfg.enabled and src_id is not None
                            and self._storage is not None):
                        if self._storage.add_trace(
                                _norm_key(ent), _norm_key(attr), src_id, _time.time()):
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

(This replaces the existing `texts`/`claims`/`for c in claims` block and the
existing main-path `return`. `claims` is now the per-tally sum; `vocab` is computed
once; relations extraction stays batched over `texts`. The trace write is inline —
no fact-id resolution and no `_save_cortex` ordering dependency, because the trace
is keyed on the stable slot and has no `facts` FK.)

- [ ] **Step 4: Run the test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "dream_writes_fact_traces or graph_insight or hub_gating" -v`
Expected: PASS — traces written + survive a later cortex write, reinforcements bumped, and the existing dream/recall PG tests still green.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(traces): dream attributes claims per-entry and writes slot->episode traces"
```

---

## Task 4: Retrieval surface — memory_get, memory_reinforce, source_entries

**Files:**
- Modify: `pseudolife_memory/service.py` (`memory_get`, `reinforce`, `source_entries` on fact reads)
- Modify: `pseudolife_memory/mcp_server.py` (`memory_get`, `memory_reinforce` tools)
- Test: `tests/test_recall.py`

**Interfaces:**
- Consumes: storage methods (Task 2); dream traces (Task 3).
- Produces: `MemoryService.get_entry(entry_id) -> dict`, `MemoryService.reinforce(entry_id) -> dict`; `source_entries` on `cortex_search` / `cortex_lookup` / `cortex_dump` (the real current-fact read surfaces — `cortex_records` does **not** exist); MCP tools `memory_get`, `memory_reinforce`.

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
    svc.store("getme-svc runtime: jdk-22", source="general")
    svc.dream_run(RegexExtractor())
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    monkeypatch.setattr(srv, "service", svc, raising=False)
    got = srv.memory_get(eid)
    assert got["found"] is True and "getme-svc" in got["text"]
    assert got["consolidated_into"]                      # entry -> facts
    # source_entries surfaces on a fact read (the fact advertises its episodes).
    facts = srv.memory_facts()["entries"]
    assert any(eid in (f.get("source_entries") or []) for f in facts)
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

For `source_entries` surfacing: attach the fact's source entries — resolved by the
fact's **slot** — to the per-record dict in each of the three current-fact read
surfaces: `cortex_search` (per hit `r`), `cortex_lookup` (the single record), and
`cortex_dump` (per record, the path `memory_facts` calls). Verify each builds a
mutable per-record dict; for a record `r` with a built dict `d`:

```python
            if self._storage is not None:
                d["source_entries"] = self._storage.traces_for_slot(
                    _norm_key(r.entity), _norm_key(r.attribute))
```

Import `_norm_key` from `pseudolife_memory.memory.cortex` (as the dream does).
These reads already run under the cortex lock. (`cortex_records` is named in the
design but does not exist — do not add it; surface on the three real methods.)

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
Expected: PASS (the round-trip test incl. `source_entries` on the fact read + all prior recall/trace tests).

- [ ] **Step 5: Update docs + commit**

Add a one-line CHANGELOG entry under `## [Unreleased]` → `### Added`:

```markdown
- **Provenance-as-link (Phase 1)** — the dream now links each consolidated fact-slot
  to the dense episodes it came from (`memory_traces`, keyed on the stable slot);
  facts surface `source_entries`, and new `memory_get` / `memory_reinforce` tools
  dereference and strengthen them.
```

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py tests/test_recall.py CHANGELOG.md
git commit -m "feat(traces): memory_get + memory_reinforce + source_entries on facts"
```

---

## Final verification
- [ ] `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_schema_v13.py tests/test_dream.py tests/test_graph.py tests/test_recall.py -q`
- [ ] Confirm `SCHEMA_META_VERSION == 13`; the new PG integration tests RAN (not skipped); existing suite green.
- [ ] Confirm the durability guards passed: a trace written by Task 3's dream and Task 2's roundtrip both survive a *subsequent* `cortex_write` (the snapshot-rewrite regression).

## Self-review notes (coverage vs spec, Phase 1)
- Slot-keyed engram table + entry-CASCADE + bidirectional (`traces_for_slot` / `facts_for_entry`) → Tasks 1–2.
- Durability under snapshot rewrite (traces survive later cortex writes) → Task 2 + Task 3 durability assertions (the anchor-correction fix).
- Multi-trace accrual (idempotent PK; a fresh confirming episode adds its own trace) → Task 2 PK + Task 3 per-entry loop.
- Consolidation wiring (per-entry attribution; slot keyed by `_norm_key`) → Task 3.
- `reinforcements` written at encoding (only on a *new* trace row) + on explicit reinforce → Tasks 3 + 4. (The eviction *boost* that reads it = Phase 2, separate plan.)
- `memory_get` (+ ambient access bump), `memory_reinforce`, `source_entries` surfacing on the three real fact-read surfaces → Task 4.
- Faded handling (`{found:false, faded:true}`) → Task 4 + Task 2 `get_entry` None.
- Config gate (`traces.enabled`) → Tasks 1 + 3.

## Phase 2 (separate plan, after this lands + deploys)
MTT retention: `MemoryEntry.reinforcements` field + load it; `RetentionPolicy.retention_boost` + `source_weighted_score += retention_boost·log1p(reinforcements)`; thread `config.memory.traces.retention_boost` into band policy construction; in-memory sync of `reinforcements` on bump; deterministic eviction-order tests. Default `retention_boost=0.0` (no behaviour change) for thorough tuning. (Unaffected by the slot-vs-fact-id anchor change — it reads `entries.reinforcements`.)
