# Superseded-Row Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the unbounded growth of `status='superseded'` records in the three canonical stores (facts / world_facts / lessons) with a keep-newest-N-per-slot + min-age compaction job that runs on the dream sweep tick.

**Architecture:** A pure duck-typed helper (`compact_store`) mutates the in-memory store (`records` / `_current` / `dirty_slots`); the existing per-slot PG sync (`replace_slot_*`) propagates deletions because PG converges to memory per dirty slot. A `MemoryService.compact_superseded()` wrapper applies config and saves; `run_sweep_once` calls it each tick before the dream's backlog gate. No DDL, no schema bump.

**Tech Stack:** Python 3.11 dataclasses, torch tensors (embeddings on records), psycopg/Postgres (bench server 127.0.0.1:5433), pytest.

**Spec:** `docs/superpowers/specs/2026-07-14-superseded-row-compaction-design.md` — read it first; the reader-enumeration table there is the contract this plan must not break.

## Global Constraints

- Never purge records with `status` `current` or `contested`. Only `superseded` and `retired` are purge-eligible ("non-live").
- Per slot: keep the newest `keep_per_slot` non-live records unconditionally; purge the rest only when `superseded_at < now − min_age_days*86400`. `superseded_at=None` sorts as 0.0 (oldest).
- Newest-first ordering key: `(superseded_at or 0.0, asserted_at, insertion index)` descending — the insertion ordinal makes ties deterministic.
- Defaults: `enabled=True`, `keep_per_slot=3`, `min_age_days=30.0`.
- Every purged slot MUST be added to `dirty_slots` (this is the invalidation hook that deletes the PG rows on next sync). Spot-check it is load-bearing during Task 4 (comment it out, watch the PG test fail).
- entries and edges are OUT OF SCOPE (see spec §2) — do not touch band entries, `SUPERSEDED_SCORE_MULT`, or edge supersession.
- Suite runs as `HF_HUB_OFFLINE=1 python -m pytest tests/ -x -q` with bench PG up; PG tests skip silently without it, which is NOT a pass.
- Windows note: run pytest via PowerShell (`$env:HF_HUB_OFFLINE=1; python -m pytest …`) or `HF_HUB_OFFLINE=1 python -m pytest …` in Git Bash from the repo root.

---

### Task 1: Compaction core — `compact_store` + policy/invariant unit tests

**Files:**
- Create: `pseudolife_memory/memory/compaction.py`
- Test: `tests/test_compaction.py`

**Interfaces:**
- Consumes: `CortexStore` / `WorldCortexStore` / `LessonStore` duck-type: `records: list`, `_current: dict[tuple[str,str], int]`, `dirty_slots: set[tuple[str,str]]`, records with `.key`, `.status`, `.superseded_at`, `.asserted_at`; `CortexStore._reindex_current()`.
- Produces: `compact_store(store, *, keep_per_slot: int, min_age_days: float, now: float | None = None) -> int` (returns purge count). Task 4 calls this from the service.

- [ ] **Step 1: Write the failing tests**

```python
"""Superseded-row compaction (spec 2026-07-14).

Policy: per slot, pool non-live records (superseded/retired), keep the
newest ``keep_per_slot``, purge the rest when older than ``min_age_days``.
current/contested are never touched.
"""
from __future__ import annotations

import torch

from pseudolife_memory.memory.compaction import compact_store
from pseudolife_memory.memory.cortex import CortexStore, CortexRecord
from pseudolife_memory.memory.lessons import LessonStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.memory.world_cortex import WorldCortexStore

EMB = torch.zeros(8)
T0 = 1_000_000.0
DAY = 86400.0


def _facts_store(n_versions: int = 6) -> CortexStore:
    """One slot, n_versions successive user-tier values -> 1 current +
    (n_versions - 1) superseded, superseded_at = T0+10 ... T0+(n-1)*10."""
    s = CortexStore()
    for i in range(n_versions):
        s.write_fact(Slot("proj", "version", f"v{i}"), EMB,
                     support="user", now=T0 + i * 10)
    return s


def _non_live(store):
    return [r for r in store.records if r.status in ("superseded", "retired")]


# ── policy ──────────────────────────────────────────────────────────────

def test_keeps_newest_n_purges_older():
    s = _facts_store(6)                       # 5 superseded
    n = compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 3
    kept = sorted(r.value for r in _non_live(s))
    assert kept == ["v3", "v4"]               # the two newest priors
    assert s.lookup("proj", "version").value == "v5"   # current untouched


def test_min_age_guard_blocks_recent_purge():
    s = _facts_store(6)
    # Everything superseded within the last day -> nothing qualifies.
    n = compact_store(s, keep_per_slot=0, min_age_days=1.0, now=T0 + 100)
    assert n == 0
    assert len(_non_live(s)) == 5


def test_min_age_partial_window():
    s = _facts_store(6)
    # now such that only superseded_at <= T0+30 are older than 1 day:
    now = T0 + 30 + DAY + 1
    n = compact_store(s, keep_per_slot=0, min_age_days=1.0, now=now)
    assert n == 3                              # v0(T0+10), v1(+20), v2(+30)
    assert sorted(r.value for r in _non_live(s)) == ["v3", "v4"]


def test_never_touches_current_or_contested():
    s = CortexStore()
    s.write_fact(Slot("proj", "owner", "alice"), EMB, support="user", now=T0)
    s.write_fact(Slot("proj", "owner", "bob"), EMB, support="user", now=T0 + 10)
    # Weaker tier -> parked as contender (status='contested').
    s.write_fact(Slot("proj", "owner", "carol"), EMB, support="agent", now=T0 + 20)
    n = compact_store(s, keep_per_slot=0, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1                              # only the superseded "alice"
    assert s.lookup("proj", "owner").value == "bob"
    assert [r.value for r in s.contenders_for("proj", "owner")] == ["carol"]


def test_retired_records_pool_with_superseded():
    s = CortexStore()
    s.write_fact(Slot("proj", "owner", "alice"), EMB, support="user", now=T0)
    s.write_fact(Slot("proj", "owner", "carol"), EMB, support="agent", now=T0 + 10)
    s.resolve("proj", "owner", accept=False, now=T0 + 20)   # -> retired
    assert any(r.status == "retired" for r in s.records)
    n = compact_store(s, keep_per_slot=0, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1
    assert all(r.status != "retired" for r in s.records)


def test_legacy_none_superseded_at_purged_first():
    s = _facts_store(3)                        # superseded at T0+10, T0+20
    legacy = CortexRecord(entity="proj", attribute="version", value="v-legacy",
                          status="superseded", asserted_at=0.0,
                          superseded_at=None)
    s.records.insert(0, legacy)
    s._reindex_current()
    n = compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1
    assert all(r.value != "v-legacy" for r in s.records)


def test_tie_break_is_deterministic_by_insertion_order():
    s = CortexStore()
    for i, v in enumerate(("a", "b", "c")):
        s.records.append(CortexRecord(
            entity="proj", attribute="version", value=v,
            status="superseded", asserted_at=T0, superseded_at=T0 + 10))
    s._reindex_current()
    n = compact_store(s, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    # Identical timestamps: the LAST-inserted record is newest.
    assert [r.value for r in _non_live(s)] == ["c"]


def test_keep_zero_and_negative_inputs_clamped():
    s = _facts_store(4)
    n = compact_store(s, keep_per_slot=-5, min_age_days=-1.0, now=T0 + 10 * DAY)
    assert n == 3                              # keep clamps to 0, age to 0
    assert _non_live(s) == []


# ── invariants ──────────────────────────────────────────────────────────

def test_current_index_rebuilt_and_lookup_survives():
    s = _facts_store(6)
    s.write_fact(Slot("proj", "license", "MIT"), EMB, support="user", now=T0)
    compact_store(s, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert s.lookup("proj", "version").value == "v5"
    assert s.lookup("proj", "license").value == "MIT"


def test_purged_slots_marked_dirty():
    s = _facts_store(6)
    s.dirty_slots.clear()
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert ("proj", "version") in s.dirty_slots


def test_untouched_slots_not_marked_dirty():
    s = _facts_store(6)
    s.write_fact(Slot("proj", "license", "MIT"), EMB, support="user", now=T0)
    s.dirty_slots.clear()
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert ("proj", "license") not in s.dirty_slots


def test_survivor_order_preserved():
    s = _facts_store(6)
    before = [r.value for r in s.records]
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    after = [r.value for r in s.records]
    assert after == [v for v in before if v in set(after)]


# ── other store types ───────────────────────────────────────────────────

def test_world_store_compaction():
    w = WorldCortexStore()
    for i in range(4):
        w.write_fact("pkg", "version", f"{i}.0", None, now=T0 + i * 10)
    n = compact_store(w, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    assert w.lookup("pkg", "version").value == "3.0"
    assert [r.value for r in _non_live(w)] == ["2.0"]
    assert ("pkg", "version") in w.dirty_slots


def test_lesson_store_compaction():
    ls = LessonStore()
    for i in range(4):
        ls.write_fact("deploy", "pitfall", f"lesson {i}", None, now=T0 + i * 10)
    n = compact_store(ls, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    assert ls.lookup("deploy", "pitfall").value == "lesson 3"
    assert ("deploy", "pitfall") in ls.dirty_slots
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py -x -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'pseudolife_memory.memory.compaction'`

- [ ] **Step 3: Write the implementation**

```python
"""Superseded-row compaction over the canonical stores (spec 2026-07-14).

Duck-typed over CortexStore / WorldCortexStore / LessonStore: each has a
flat ``records`` list, a ``_current`` slot index, and a ``dirty_slots``
set consumed by the per-slot PG sync (``replace_slot_*`` deletes every
row at a dirty slot and reinserts the in-memory survivors, so marking a
purged slot dirty IS the delete path — no separate SQL).

Policy (uniform across the three stores): per slot, pool the non-live
records (status ``superseded`` or ``retired``), keep the newest
``keep_per_slot`` unconditionally, and purge the rest only when their
``superseded_at`` is older than ``min_age_days``. ``current`` and
``contested`` records are never touched; entries and edges are out of
scope (bounded eviction / load-bearing tombstones — see the spec).
"""
from __future__ import annotations

import time

_NON_LIVE = ("superseded", "retired")


def compact_store(store, *, keep_per_slot: int, min_age_days: float,
                  now: float | None = None) -> int:
    """Purge old non-live records from ``store``. Returns the purge count."""
    keep = max(0, int(keep_per_slot))
    t = time.time() if now is None else float(now)
    cutoff = t - max(0.0, float(min_age_days)) * 86400.0

    # Pool non-live records per slot with their insertion ordinal — the
    # ordinal breaks timestamp ties deterministically (the slot-index lesson).
    pools: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    for i, r in enumerate(store.records):
        if r.status in _NON_LIVE:
            pools.setdefault(r.key, []).append(
                (r.superseded_at or 0.0, r.asserted_at, i))

    victims: set[int] = set()
    for key, pool in pools.items():
        pool.sort(reverse=True)               # newest first
        for sup_at, _asserted, idx in pool[keep:]:
            if sup_at < cutoff:
                victims.add(idx)

    if not victims:
        return 0

    purged_slots = {store.records[i].key for i in victims}
    store.records = [r for i, r in enumerate(store.records)
                     if i not in victims]
    # Rebuild the slot -> index map. CortexStore's own rebuild keeps its
    # duplicate-healing semantics; the world/lesson stores use the plain
    # comprehension their hydrators use.
    reindex = getattr(store, "_reindex_current", None)
    if callable(reindex):
        reindex()
    else:
        store._current = {r.key: i for i, r in enumerate(store.records)
                          if r.status == "current"}
    # The invalidation hook: the next per-slot sync rewrites these slots,
    # deleting the purged rows from PG.
    store.dirty_slots |= purged_slots
    return len(victims)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py -x -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/compaction.py tests/test_compaction.py
git commit -m "feat(memory): compact_store — keep-newest-N + min-age purge of superseded records"
```

---

### Task 2: In-memory `supersession_log` cap

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (the `_log` method, ~line 330)
- Test: `tests/test_compaction.py` (append)

**Interfaces:**
- Consumes: `CortexStore._log` (private, called by every supersession/contest path).
- Produces: `pseudolife_memory.memory.cortex.SUPERSESSION_LOG_CAP = 200` (module constant; `storage/sync.py` keeps its own `[-200:]` slice — do not change sync).

- [ ] **Step 1: Write the failing test** (append to `tests/test_compaction.py`)

```python
def test_supersession_log_capped_in_memory():
    from pseudolife_memory.memory.cortex import SUPERSESSION_LOG_CAP
    s = CortexStore()
    for i in range(SUPERSESSION_LOG_CAP + 50):
        s.write_fact(Slot("proj", "version", f"v{i}"), EMB,
                     support="user", now=T0 + i)
    assert len(s.supersession_log) == SUPERSESSION_LOG_CAP
    # Newest entries survive the trim.
    assert s.supersession_log[-1]["new_value"] == f"v{SUPERSESSION_LOG_CAP + 49}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py::test_supersession_log_capped_in_memory -x -q`
Expected: FAIL — `ImportError: cannot import name 'SUPERSESSION_LOG_CAP'`

- [ ] **Step 3: Implement**

In `pseudolife_memory/memory/cortex.py`, add below `SUPPORT_PRECEDENCE`:

```python
# In-RAM cap on the supersession audit log. Persistence already stores only
# the newest 200 (storage/sync.py); without this in-place trim the list grew
# for the daemon's whole uptime — same growth class as superseded rows.
SUPERSESSION_LOG_CAP = 200
```

and at the END of `_log(...)` (after the `append`):

```python
        if len(self.supersession_log) > SUPERSESSION_LOG_CAP:
            del self.supersession_log[:-SUPERSESSION_LOG_CAP]
```

- [ ] **Step 4: Run the compaction + cortex suites**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py tests/test_cortex.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_compaction.py
git commit -m "fix(cortex): cap the in-memory supersession_log at its persisted size"
```

---

### Task 3: Config — `CompactionConfig` + YAML parse + console knobs

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (new dataclass near `LessonsConfig`; field on `MemoryConfig`; parse block in `load_config` beside the `"traces"` block)
- Modify: `pseudolife_memory/web/config_io.py` (three `KNOBS` entries, new group "Retention")
- Test: `tests/test_compaction.py` (append)

**Interfaces:**
- Produces: `config.memory.compaction.enabled: bool = True`, `.keep_per_slot: int = 3`, `.min_age_days: float = 30.0`. Task 4 reads these.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_compaction.py`)

```python
def test_compaction_config_defaults_and_yaml(tmp_path):
    from pseudolife_memory.utils.config import AppConfig, load_config
    c = AppConfig().memory.compaction
    assert (c.enabled, c.keep_per_slot, c.min_age_days) == (True, 3, 30.0)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "memory:\n  compaction:\n    enabled: false\n"
        "    keep_per_slot: 5\n    min_age_days: 7\n")
    loaded = load_config(cfg_file).memory.compaction
    assert (loaded.enabled, loaded.keep_per_slot, loaded.min_age_days) == (False, 5, 7)


def test_compaction_console_knobs_registered():
    from pseudolife_memory.web.config_io import KNOBS
    paths = {k["path"] for k in KNOBS}
    assert {"memory.compaction.enabled", "memory.compaction.keep_per_slot",
            "memory.compaction.min_age_days"} <= paths
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py -q -k compaction_config or compaction_console`
(use: `-k "compaction_config or compaction_console"`)
Expected: FAIL — `AttributeError: ... no attribute 'compaction'`

- [ ] **Step 3: Implement**

`pseudolife_memory/utils/config.py` — add near `LessonsConfig`:

```python
@dataclass
class CompactionConfig:
    """Superseded-row compaction over facts/world_facts/lessons (spec
    2026-07-14). Per slot: keep the newest ``keep_per_slot`` non-live
    records; purge the rest once older than ``min_age_days``. Runs on the
    dream sweep tick."""
    enabled: bool = True
    keep_per_slot: int = 3
    min_age_days: float = 30.0
```

On `MemoryConfig` (beside the `lessons` field):

```python
    # Superseded-row compaction (keep-newest-N + min-age; spec 2026-07-14).
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
```

In `load_config` (beside the `"traces"` block):

```python
        if "compaction" in mem_raw:
            config.memory.compaction = _dict_to_dataclass(
                CompactionConfig, mem_raw["compaction"],
            )
```

`pseudolife_memory/web/config_io.py` — append to `KNOBS` (match the existing dict shape exactly):

```python
    # ── Retention ──────────────────────────────────────────────────────────
    {"path": "memory.compaction.enabled", "group": "Retention",
     "label": "Superseded-row compaction", "type": "bool", "default": True,
     "restart": False,
     "help": "Purge old superseded fact/world/lesson versions on the dream "
             "sweep (keep-newest-N per slot + min-age)."},
    {"path": "memory.compaction.keep_per_slot", "group": "Retention",
     "label": "Versions kept per slot", "type": "int", "default": 3,
     "min": 0, "max": 50, "step": 1, "restart": False,
     "help": "Superseded versions always kept per (entity, attribute) slot."},
    {"path": "memory.compaction.min_age_days", "group": "Retention",
     "label": "Min age before purge (days)", "type": "float", "default": 30.0,
     "min": 0.0, "max": 365.0, "step": 1.0, "restart": False,
     "help": "A superseded version younger than this is never purged, "
             "whatever the per-slot count."},
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py tests/test_web.py -q`
Expected: all PASS (test_web covers the settings registry rendering)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/web/config_io.py tests/test_compaction.py
git commit -m "feat(config): memory.compaction knobs (enabled / keep_per_slot / min_age_days)"
```

---

### Task 4: Service integration — `compact_superseded()` + PG persistence + reader preservation

**Files:**
- Modify: `pseudolife_memory/service.py` (new public method near `cortex_dump`, ~line 1871)
- Test: `tests/test_compaction.py` (append)

**Interfaces:**
- Consumes: `compact_store` (Task 1), `config.memory.compaction` (Task 3), `self._cortex` / `self._world` / `self._lessons`, `_save_cortex` / `_save_world` / `_save_lessons`.
- Produces: `MemoryService.compact_superseded() -> dict` returning `{"facts": int, "world_facts": int, "lessons": int, "total": int}` (all zeros + `"skipped": "disabled"` when off). Task 5 calls this from the sweep.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_compaction.py`)

```python
def _seed_versions(svc, n=6, base=T0):
    """n successive values at one cortex slot through the public API."""
    for i in range(n):
        svc.cortex_write("proj", "version", f"v{i}", support="user",
                         now=base + i * 10)


def test_service_compact_disabled_is_noop(pristine_service):
    svc = pristine_service
    svc.config.memory.compaction.enabled = False
    try:
        _seed_versions(svc)
        out = svc.compact_superseded()
        assert out["total"] == 0 and out.get("skipped") == "disabled"
    finally:
        svc.config.memory.compaction.enabled = True
        svc.cortex_forget("proj")


def test_service_compact_purges_and_preserves_history(pristine_service):
    svc = pristine_service
    cfg = svc.config.memory.compaction
    cfg.keep_per_slot, cfg.min_age_days = 2, 0.0
    try:
        _seed_versions(svc)
        out = svc.compact_superseded()
        assert out["facts"] == 3 and out["total"] == 3
        h = svc.history("proj", "version")
        values = [v["value"] for v in h["versions"]]
        assert values == ["v3", "v4", "v5"]          # newest-2 priors + current
        # Churn signal preserved: latest change ts is the current record's
        # asserted_at, which survives compaction.
        idx = svc._cortex_change_index()
        assert idx["proj"] == T0 + 50
    finally:
        cfg.keep_per_slot, cfg.min_age_days = 3, 30.0
        svc.cortex_forget("proj")
```

API verified against service.py: `cortex_write(entity, attribute, value, *,
confidence=0.7, provenance=None, support=None, now=None)` (service.py:1278)
and `cortex_forget(entity, attribute=None)` (service.py:2021). `now=` sets
`asserted_at`/`superseded_at` wall-clock while HLC stays fresh-monotonic, so
successive writes supersede as normal and the `idx["proj"] == T0 + 50`
assertion holds.

PG round-trip (storage-level, mirrors the sync contract):

```python
def test_compaction_deletes_pg_rows(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.sync import sync_cortex_slots

    storage = PostgresStorage(pg_url)
    try:
        s = _facts_store(6)                    # 1 current + 5 superseded
        sync_cortex_slots(s, storage)
        n_before = pg_conn.execute(
            "SELECT count(*) FROM facts WHERE status = 'superseded'"
        ).fetchone()[0]
        assert n_before == 5
        compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
        sync_cortex_slots(s, storage)          # dirty slot -> rows rewritten
        rows = pg_conn.execute(
            "SELECT value, status FROM facts ORDER BY id").fetchall()
        assert sorted(v for v, st in rows if st == 'superseded') == ["v3", "v4"]
        assert [v for v, st in rows if st == 'current'] == ["v5"]
    finally:
        storage.close()
```

Add the fixture import at the top of the file with the other imports:

```python
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py -q -k service_compact`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute 'compact_superseded'`

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py::test_compaction_deletes_pg_rows -q`
Expected: PASS already (it exercises Task 1 + existing sync) — it is the
regression net for the dirty-slots hook. **Spot-check it is load-bearing:**
temporarily comment out `store.dirty_slots |= purged_slots` in
`compaction.py`, re-run, confirm it FAILS (purged rows still in PG), restore.
If it doesn't go red, the test is decoration — fix it before continuing.

- [ ] **Step 3: Implement `compact_superseded`** (service.py, near `cortex_dump`)

```python
    def compact_superseded(self) -> dict[str, Any]:
        """Purge old superseded/retired versions from the three canonical
        stores (spec 2026-07-14): per slot keep the newest
        ``memory.compaction.keep_per_slot`` non-live records, purge the rest
        once older than ``min_age_days``. current/contested are never
        touched; the per-slot sync deletes the purged rows from PG. Runs on
        the dream sweep tick; safe to call any time."""
        from pseudolife_memory.memory.compaction import compact_store

        cfg = self.config.memory.compaction
        if not cfg.enabled:
            return {"facts": 0, "world_facts": 0, "lessons": 0, "total": 0,
                    "skipped": "disabled"}
        with self._lock:
            self._ensure_init()
            kw = dict(keep_per_slot=cfg.keep_per_slot,
                      min_age_days=cfg.min_age_days)
            out = {"facts": 0, "world_facts": 0, "lessons": 0}
            if self._cortex is not None:
                out["facts"] = compact_store(self._cortex, **kw)
                if out["facts"]:
                    self._save_cortex()
            if getattr(self, "_world", None) is not None:
                out["world_facts"] = compact_store(self._world, **kw)
                if out["world_facts"]:
                    self._save_world()
            if getattr(self, "_lessons", None) is not None:
                out["lessons"] = compact_store(self._lessons, **kw)
                if out["lessons"]:
                    self._save_lessons()
            out["total"] = sum(out.values())
            if out["total"]:
                logger.info("compaction purged %s", out)
            return out
```

Check the attribute names first (`grep -n "_world\b\|_lessons\b" pseudolife_memory/service.py | head`) — they are `self._world` / `self._lessons` per `_save_world`/`_save_lessons`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py -q`
Expected: all PASS (PG test requires the bench server on 127.0.0.1:5433 — start it if skipped: `docker compose -f ops/docker-compose.yml up -d postgres` or the documented bench command; a skip is NOT a pass)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_compaction.py
git commit -m "feat(service): compact_superseded() over facts/world_facts/lessons"
```

---

### Task 5: Trigger — dream sweep tick

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (`run_sweep_once`, ~line 628)
- Test: `tests/test_compaction.py` (append)

**Interfaces:**
- Consumes: `MemoryService.compact_superseded()` (Task 4).
- Produces: `run_sweep_once` result dict gains `"compacted": int` (total purge count) on every tick, including non-firing ones.

- [ ] **Step 1: Write the failing test** (append to `tests/test_compaction.py`)

```python
def test_sweep_compacts_even_when_dream_gate_closed(pristine_service, monkeypatch):
    from pseudolife_memory.memory.dream import run_sweep_once
    svc = pristine_service
    svc.config.memory.dream.enabled = True
    calls = []
    monkeypatch.setattr(svc, "compact_superseded",
                        lambda: calls.append(1) or {"total": 7})
    monkeypatch.setattr(svc, "dream_status",
                        lambda: {"would_fire": False, "backlog": 0})
    out = run_sweep_once(svc)
    assert calls == [1]
    assert out["fired"] is False and out["compacted"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py::test_sweep_compacts_even_when_dream_gate_closed -q`
Expected: FAIL — `KeyError: 'compacted'`

- [ ] **Step 3: Implement**

In `run_sweep_once` (dream.py), after the `cfg.enabled` check and before the `would_fire` gate:

```python
def run_sweep_once(service) -> dict:
    """One headless sweep tick: if dreaming is enabled and the backlog+quiescence
    trigger would fire, run a dream with the configured extractor. Session-
    agnostic by construction (it keys on the cursor, not on session lifecycle).
    Returns ``{"fired": bool, ...}``; never raises into the daemon's timer."""
    cfg = service.config.memory.dream
    if not cfg.enabled:
        return {"fired": False, "reason": "disabled"}
    # Superseded-row compaction rides every tick (spec 2026-07-14) — it must
    # run even when no dream fires, or a quiet bank never compacts.
    compacted = service.compact_superseded().get("total", 0)
    status = service.dream_status()
    if not status["would_fire"]:
        return {"fired": False, "reason": "below_threshold",
                "backlog": status["backlog"], "compacted": compacted}
    result = service.dream_run_auto()
    logger.info("dream sweep fired: %s", result)
    return {"fired": True, "compacted": compacted, **result}
```

- [ ] **Step 4: Run the compaction + dream suites**

Run: `HF_HUB_OFFLINE=1 python -m pytest tests/test_compaction.py tests/test_dream.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/dream.py tests/test_compaction.py
git commit -m "feat(dream): run superseded-row compaction on every sweep tick"
```

---

### Task 6: CHANGELOG + full suite + review pass

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]`, existing dated-subsection style)

- [ ] **Step 1: Add the CHANGELOG entry** under `[Unreleased]` in the current dated subsection (create `### 2026-07-14` if the day's subsection doesn't exist; match surrounding style):

```markdown
- **Superseded-row compaction** — corrections no longer grow the canonical
  stores forever: on each dream sweep tick, `facts`/`world_facts`/`lessons`
  keep the newest 3 superseded/retired versions per slot and purge older
  ones after 30 days (config `memory.compaction.*`; per-slot sync deletes
  the PG rows). `memory_history` timelines keep their recent versions;
  entries (bounded band eviction, supersession is retrieval-load-bearing)
  and edges (sticky-removal tombstones) are deliberately untouched. The
  in-memory cortex supersession log is now capped at its persisted size
  (200). Spec: `docs/superpowers/specs/2026-07-14-superseded-row-compaction-design.md`.
```

- [ ] **Step 2: Run the FULL suite with bench PG up**

```bash
docker compose -f ops/docker-compose.yml ps   # confirm postgres on 5433 is up
HF_HUB_OFFLINE=1 python -m pytest tests/ -q
```

Expected: everything passes; confirm `test_compaction_deletes_pg_rows` shows as PASSED, not SKIPPED (grep the output).

- [ ] **Step 3: Independent review pass** (project discipline: derived-state/retention change class) — run `/code-review` medium or dispatch a reviewer subagent over the branch diff; address Critical/Important findings.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): superseded-row compaction entry"
```

---

## Self-review notes

- Spec coverage: §5.1→Task 1, §5.2→Tasks 2+4, §5.3→Task 5, §5.4→Task 3, §6 tests→Tasks 1–5, §7→Task 6. No DDL anywhere (spec §3).
- The service-level test names (`fact_set`/`cortex_forget`) are flagged inline for verification against the real service API before writing the test — the storage-level PG test is API-exact.
- Deploy (ops/update.ps1 + live verify) is intentionally NOT a plan task: per project convention it is its own gated step after merge.
