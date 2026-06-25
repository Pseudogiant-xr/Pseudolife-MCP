# MTT Retention (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make band eviction read `entries.reinforcements` so a reinforced episode resists forgetting in proportion to its strength (MTT), tuned by one knob `retention_boost` (default 0.0 = today's behaviour), and ship an offline bench to pick a data-driven default.

**Architecture:** Add one additive `retention_boost * log1p(reinforcements)` term to `RetentionPolicy.source_weighted_score`; thread `config.memory.traces.retention_boost` through the policy factories into every band; make `reinforcements` a DB-authoritative read-cache on `MemoryEntry` (loaded at hydrate, synced in-memory on bump, never written back); add an eviction-dynamics bench that sweeps `retention_boost` over the real `MIRASBand`.

**Tech Stack:** Python 3.10+, dataclasses, torch (band tensors), psycopg3/Postgres (load path), pytest.

## Global Constraints

- Offline baseline (`HF_HUB_OFFLINE=1`, CPU). No new dependency. Test runner: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest`. Bench Postgres at `127.0.0.1:5433`.
- PG integration tests use the `_pg_up()` skipif + `build_service(tmp_path)` pattern already in `tests/test_recall.py`.
- **Code-only — NO schema change.** `entries.reinforcements` already shipped in schema v13 (Phase 1, live). Do not touch `schema.py` / `SCHEMA_META_VERSION`.
- `retention_boost` defaults to **0.0** and MUST be byte-identical to today's eviction at 0.0 (`log1p(0)==0`, and the term scales by `retention_boost`).
- The boost is added **after** the source-weight multiply: `(base + 1.0) * weight + retention_boost * log1p(reinforcements)`. Source-independent, absolute.
- `reinforcements` is **DB-authoritative**: written only by `PostgresStorage.bump_reinforcements` (already exists); loaded into `MemoryEntry` at hydrate; bumped in-memory on each bump path; **never** added to `_ENTRY_COLS`, `update_access_counts`, or `entry_to_row` (no clobber).
- Single tuning knob: `retention_boost` only. `reinforce_increment` is deferred (reinforce stays +1). Decay of `reinforcements` is tabled (future research).
- Spec: [docs/specs/2026-06-25-mtt-retention-design.md](2026-06-25-mtt-retention-design.md).

## Grounding (verified signatures)
- `RetentionPolicy` (`pseudolife_memory/memory/miras/protocols.py`): dataclass fields `weight_decay, decay_factor_on_contradiction, eviction_score, name, source_weights`. `source_weighted_score(self, entry, now) -> float` currently returns `(self.eviction_score(entry, now) + 1.0) * self.source_weights.get(entry.source, 1.0)`.
- `MemoryEntry` (`pseudolife_memory/memory/titans_memory.py`): dataclass; last field is `db_id: int | None = None`. No `reinforcements` yet.
- Factories (`pseudolife_memory/memory/miras/retention.py`): `balanced(weight_decay=0.001)`, `recency_heavy(weight_decay=0.005)`, `surprise_heavy(weight_decay=0.0005)` each `-> RetentionPolicy`; `build_policy(name, weight_decay=None) -> RetentionPolicy`; `now_seconds()`.
- `build_band(spec, embedding_dim, device) -> MIRASBand` (`pseudolife_memory/memory/miras/band.py:251`) calls `build_policy(spec.retention_policy)`. `MIRASBand` exposes `.retention`, `.entries` (list of `MemoryEntry`), `.add(text, embedding, source="", surprise=0.0)`, `.max_entries`, `_evict_one()` (calls `now_seconds()`; band.py imports `now_seconds` into its own namespace).
- `ContinuumMemorySystem.__init__(self, config, nli_scorer=None, reranker=None, storage=None)` (`pseudolife_memory/memory/cms.py`): builds `self.bands = [build_band(spec, embedding_dim=config.embedding_dim, device=device) for spec in config.miras.bands]` (line ~125). Here `config` is a `MemoryConfig` (passed as `self.config.memory` from `service.py:372`), so `config.traces.retention_boost` is reachable.
- `MemoryConfig().miras.bands` auto-populates from the `continuum` preset (8 bands) via `MIRASConfig.__post_init__`. `MIRASBandSpec(name=..., max_entries=..., retention_policy="balanced", ...)` — all fields defaulted.
- `PostgresStorage` (`pseudolife_memory/storage/postgres.py`): `_ENTRY_COLS` (line 28) is shared by `insert_entry` and `load_entries` and excludes `reinforcements`. `load_entries` builds `cols = ("id",) + _ENTRY_COLS` then `SELECT {cols} FROM entries`. `bump_reinforcements(entry_id, delta)` exists (Phase 1).
- `row_to_entry(row, device="cpu") -> MemoryEntry` (`pseudolife_memory/storage/sync.py:72`).
- `MemoryService.reinforce(entry_id)` (`service.py:~1577`) holds `self._lock`, calls `self._storage.bump_reinforcements(int(entry_id), 1)`. `self._cms` is the `ContinuumMemorySystem`. The dream loop (`dream_run`, `service.py:~1755`) calls `self._storage.bump_reinforcements(src_id, 1)` inside `with self._lock:`.

## File structure
- `pseudolife_memory/memory/titans_memory.py` — `MemoryEntry.reinforcements` field.
- `pseudolife_memory/memory/miras/protocols.py` — `RetentionPolicy.retention_boost` + boost term.
- `pseudolife_memory/memory/miras/retention.py` — factories + `build_policy` thread `retention_boost`.
- `pseudolife_memory/memory/miras/band.py` — `build_band` threads `retention_boost`.
- `pseudolife_memory/memory/cms.py` — pass `config.traces.retention_boost` to `build_band`; `bump_entry_reinforcements`.
- `pseudolife_memory/storage/postgres.py` — `load_entries` selects `reinforcements`.
- `pseudolife_memory/storage/sync.py` — `row_to_entry` loads `reinforcements`.
- `pseudolife_memory/service.py` — in-memory sync calls in `reinforce` + the dream loop.
- `pseudolife_memory/utils/config.py` — `TracesConfig.retention_boost`.
- `evals/retention_bench.py` (new), `evals/results/retention.json` (output).
- Tests: `tests/test_retention_boost.py` (new, pure), additions to `tests/test_recall.py` (PG), `tests/test_retention_bench.py` (new, smoke).

---

## Task 1: The retention_boost eviction mechanism, wired (config → policy → band)

**Files:**
- Modify: `pseudolife_memory/memory/titans_memory.py`
- Modify: `pseudolife_memory/memory/miras/protocols.py`
- Modify: `pseudolife_memory/memory/miras/retention.py`
- Modify: `pseudolife_memory/memory/miras/band.py`
- Modify: `pseudolife_memory/memory/cms.py`
- Modify: `pseudolife_memory/utils/config.py`
- Test: `tests/test_retention_boost.py` (new)

**Interfaces:**
- Produces: `MemoryEntry.reinforcements: int = 0`; `RetentionPolicy.retention_boost: float = 0.0` + boost term in `source_weighted_score`; `balanced/recency_heavy/surprise_heavy(weight_decay=..., retention_boost=0.0)`; `build_policy(name, weight_decay=None, retention_boost=0.0)`; `build_band(spec, embedding_dim, device, retention_boost=0.0)`; `ContinuumMemorySystem` passes `config.traces.retention_boost` to each band; `TracesConfig.retention_boost: float = 0.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retention_boost.py`:

```python
import math
import time

import torch

from pseudolife_memory.memory.miras.protocols import RetentionPolicy
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _entry(reinforcements=0, access_count=0, source="user", ts=None):
    return MemoryEntry(
        text="x", embedding=torch.zeros(4), source=source,
        access_count=access_count, reinforcements=reinforcements,
        timestamp=ts if ts is not None else time.time())


def _policy(retention_boost=0.0):
    # constant base score isolates the boost term
    return RetentionPolicy(
        weight_decay=0.0, decay_factor_on_contradiction=0.3,
        eviction_score=lambda e, now: 0.0, name="t",
        retention_boost=retention_boost)


def test_memory_entry_reinforcements_default():
    assert _entry().reinforcements == 0


def test_boost_zero_ignores_reinforcements():
    now = time.time()
    p = _policy(0.0)
    assert p.source_weighted_score(_entry(reinforcements=0), now) == \
           p.source_weighted_score(_entry(reinforcements=100), now)


def test_boost_positive_protects_reinforced():
    now = time.time()
    p = _policy(2.0)
    lo = p.source_weighted_score(_entry(reinforcements=0), now)
    hi = p.source_weighted_score(_entry(reinforcements=10), now)
    assert hi > lo
    # additive AFTER the source-weight multiply: difference is exactly the term
    assert math.isclose(hi - lo, 2.0 * math.log1p(10))


def test_traces_config_retention_boost_default():
    from pseudolife_memory.utils.config import TracesConfig, MemoryConfig
    assert TracesConfig().retention_boost == 0.0
    assert MemoryConfig().traces.retention_boost == 0.0


def test_build_policy_threads_retention_boost():
    from pseudolife_memory.memory.miras.retention import build_policy
    assert build_policy("balanced", retention_boost=1.5).retention_boost == 1.5
    assert build_policy("balanced").retention_boost == 0.0


def test_build_band_threads_retention_boost():
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    spec = MIRASBandSpec(name="b", max_entries=10, retention_policy="balanced")
    band = build_band(spec, embedding_dim=8, device="cpu", retention_boost=2.0)
    assert band.retention.retention_boost == 2.0


def test_cms_bands_get_configured_retention_boost():
    from pseudolife_memory.utils.config import MemoryConfig
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    cfg = MemoryConfig()
    cfg.embedding_dim = 8
    cfg.traces.retention_boost = 3.0
    cms = ContinuumMemorySystem(cfg)
    assert cms.bands
    assert all(b.retention.retention_boost == 3.0 for b in cms.bands)


def test_evict_one_protects_reinforced_and_still_evicts():
    # Exercises the real _evict_one: with access_count=0 the balanced base score
    # is 0 for every entry (0/age + 0), so the ONLY differentiator is the
    # retention_boost term -> the unreinforced entry is the victim, the reinforced
    # one survives, and the band still drops exactly one (relative, no deadlock).
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    spec = MIRASBandSpec(name="b", max_entries=3, retention_policy="balanced")
    band = build_band(spec, embedding_dim=4, device="cpu", retention_boost=5.0)
    for i, reinf in enumerate([0, 5, 0]):
        band.store(text=f"e{i}", embedding=torch.zeros(4), source="agent_action")
        band.entries[-1].db_id = i
        band.entries[-1].reinforcements = reinf
    band.store(text="e3", embedding=torch.zeros(4), source="agent_action")  # triggers eviction
    band.entries[-1].db_id = 3
    ids = {e.db_id for e in band.entries}
    assert 1 in ids                 # the reinforced entry survived eviction
    assert len(band.entries) == 3   # still evicts its weakest (no deadlock)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_retention_boost.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'reinforcements'` (and `'retention_boost'`).

- [ ] **Step 3: Add `MemoryEntry.reinforcements`**

In `pseudolife_memory/memory/titans_memory.py`, after the `db_id` field of `MemoryEntry`:

```python
    db_id: int | None = None
    # Reinforcement strength (schema v13). DB-authoritative read-cache: loaded at
    # hydrate, bumped in-memory on each bump path, never written back via a save
    # path. Read by RetentionPolicy.source_weighted_score (MTT retention).
    reinforcements: int = 0
```

- [ ] **Step 4: Add the boost term to `RetentionPolicy`**

In `pseudolife_memory/memory/miras/protocols.py`, add `import math` at the top of the imports, add the field after `source_weights`, and extend `source_weighted_score`:

```python
    source_weights: dict[str, float] = field(default_factory=_default_source_weights)
    # MTT retention (Phase 2). 0.0 = today's eviction exactly (log1p term vanishes).
    retention_boost: float = 0.0

    def source_weighted_score(self, entry: "MemoryEntry", now: float) -> float:
        """``(eviction_score + 1) × source_weights[source] + retention_boost ×
        log1p(reinforcements)``.

        The reinforcement term is added AFTER the source-weight multiply — an
        absolute, source-independent boost so a reinforced episode resists
        eviction regardless of its source tier. ``retention_boost = 0.0``
        (default) makes the term vanish → eviction is byte-identical to before.
        """
        base = self.eviction_score(entry, now)
        weight = self.source_weights.get(entry.source, 1.0)
        return ((base + 1.0) * weight
                + self.retention_boost * math.log1p(entry.reinforcements))
```

- [ ] **Step 5: Thread `retention_boost` through the factories + `build_policy`**

In `pseudolife_memory/memory/miras/retention.py`, add a `retention_boost` parameter to each factory and pass it into the `RetentionPolicy(...)` it builds:

```python
def balanced(weight_decay: float = 0.001, retention_boost: float = 0.0) -> RetentionPolicy:
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.3,
        eviction_score=_balanced_score,
        name="balanced",
        retention_boost=retention_boost,
    )


def recency_heavy(weight_decay: float = 0.005, retention_boost: float = 0.0) -> RetentionPolicy:
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.2,
        eviction_score=_recency_heavy_score,
        name="recency_heavy",
        retention_boost=retention_boost,
    )


def surprise_heavy(weight_decay: float = 0.0005, retention_boost: float = 0.0) -> RetentionPolicy:
    return RetentionPolicy(
        weight_decay=weight_decay,
        decay_factor_on_contradiction=0.5,
        eviction_score=_surprise_heavy_score,
        name="surprise_heavy",
        retention_boost=retention_boost,
    )
```

(Keep each factory's existing docstring.) Then update `build_policy`:

```python
def build_policy(name: str, weight_decay: float | None = None,
                 retention_boost: float = 0.0) -> RetentionPolicy:
    """Construct a named policy. ``weight_decay`` overrides the default;
    ``retention_boost`` sets the MTT reinforcement-retention term."""
    try:
        factory = POLICY_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown retention_policy {name!r}. Available: {list(POLICY_REGISTRY)}"
        ) from exc
    if weight_decay is None:
        return factory(retention_boost=retention_boost)
    return factory(weight_decay=weight_decay, retention_boost=retention_boost)
```

- [ ] **Step 6: Thread `retention_boost` through `build_band`**

In `pseudolife_memory/memory/miras/band.py`, extend `build_band`:

```python
def build_band(spec: "MIRASBandSpec", embedding_dim: int, device: str,
               retention_boost: float = 0.0) -> MIRASBand:
    """Construct a :class:`MIRASBand` from a :class:`MIRASBandSpec` — a plain
    cosine store with the spec's capacity / cadence / promotion / eviction.
    ``retention_boost`` sets the band policy's MTT reinforcement-retention term."""
    policy = build_policy(spec.retention_policy, retention_boost=retention_boost)
    return MIRASBand(
        name=spec.name,
        embedding_dim=embedding_dim,
        retention=policy,
        max_entries=spec.max_entries,
        update_interval=spec.update_interval,
        promotion_access_count=spec.promotion_access_count,
        promotion_surprise=spec.promotion_surprise,
        device=device,
    )
```

- [ ] **Step 7: Pass the config value from the CMS**

In `pseudolife_memory/memory/cms.py`, update the band-construction list comprehension (~line 125) to pass the configured boost:

```python
        _retention_boost = getattr(getattr(config, "traces", None),
                                   "retention_boost", 0.0)
        self.bands: list[MIRASBand] = [
            build_band(spec, embedding_dim=config.embedding_dim, device=device,
                       retention_boost=_retention_boost)
            for spec in config.miras.bands
        ]
```

(Defensive `getattr` mirrors the file's existing `config.nli` guard at cms.py:110-112, so a partial/stub config without `traces` still constructs.)

- [ ] **Step 8: Add the config field**

In `pseudolife_memory/utils/config.py`, add to `TracesConfig` (after `enabled`):

```python
    enabled: bool = True
    # MTT retention (Phase 2). Weight on log1p(reinforcements) in band eviction
    # scoring; 0.0 = today's eviction exactly. A positive value makes reinforced
    # episodes resist forgetting in proportion to their strength.
    retention_boost: float = 0.0
```

- [ ] **Step 9: Run the tests**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_retention_boost.py -v`
Expected: PASS (all 8).

- [ ] **Step 10: Commit**

```bash
git add pseudolife_memory/memory/titans_memory.py pseudolife_memory/memory/miras/protocols.py pseudolife_memory/memory/miras/retention.py pseudolife_memory/memory/miras/band.py pseudolife_memory/memory/cms.py pseudolife_memory/utils/config.py tests/test_retention_boost.py
git commit -m "feat(retention): retention_boost*log1p(reinforcements) eviction term + config wiring"
```

---

## Task 2: Load `reinforcements` from the DB into resident entries

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (`load_entries`)
- Modify: `pseudolife_memory/storage/sync.py` (`row_to_entry`)
- Test: `tests/test_recall.py` (PG integration)

**Interfaces:**
- Consumes: `MemoryEntry.reinforcements` (Task 1); `bump_reinforcements` (Phase 1).
- Produces: `load_entries()` row dicts carry `reinforcements`; `row_to_entry(row)` sets `MemoryEntry.reinforcements`. (`_ENTRY_COLS`, `insert_entry`, `update_access_counts`, `entry_to_row` are UNCHANGED — no clobber.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_reinforcements_loads_into_entry(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    from pseudolife_memory.storage.sync import row_to_entry
    svc = build_service(tmp_path)
    svc.store("retain-me runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]
    st.bump_reinforcements(eid, 3)
    row = next(r for r in st.load_entries() if r["id"] == eid)
    assert row["reinforcements"] == 3
    assert row_to_entry(row).reinforcements == 3
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k reinforcements_loads_into_entry -v`
Expected: FAIL — `KeyError: 'reinforcements'` (load_entries row dict has no such key).

- [ ] **Step 2: Select `reinforcements` in `load_entries`**

In `pseudolife_memory/storage/postgres.py`, `load_entries` currently starts `cols = ("id",) + _ENTRY_COLS`. Change ONLY that line to append `reinforcements` as a read-only column (do NOT add it to `_ENTRY_COLS`, which drives inserts):

```python
    def load_entries(self) -> list[dict]:
        cols = ("id",) + _ENTRY_COLS + ("reinforcements",)
```

(The rest of `load_entries` — the `SELECT {', '.join(cols)} FROM entries ...` and the `dict(zip(cols, row))` mapping — already consumes `cols`, so the new column flows through with no other change.)

- [ ] **Step 3: Load it in `row_to_entry`**

In `pseudolife_memory/storage/sync.py`, add to the `MemoryEntry(...)` constructed by `row_to_entry` (after `db_id=row["id"]`):

```python
        db_id=row["id"],
        reinforcements=row.get("reinforcements", 0),
    )
```

- [ ] **Step 4: Run the test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k reinforcements_loads_into_entry -v`
Expected: PASS (ran against bench PG).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py pseudolife_memory/storage/sync.py tests/test_recall.py
git commit -m "feat(retention): load reinforcements from the DB into resident MemoryEntry"
```

---

## Task 3: In-memory sync of `reinforcements` on bump

**Files:**
- Modify: `pseudolife_memory/memory/cms.py` (`bump_entry_reinforcements`)
- Modify: `pseudolife_memory/service.py` (`reinforce` + the dream loop)
- Test: `tests/test_recall.py` (PG integration)

**Interfaces:**
- Consumes: `load`/`reinforcements` (Tasks 1-2); `bump_reinforcements` (Phase 1).
- Produces: `ContinuumMemorySystem.bump_entry_reinforcements(db_id, delta) -> bool`; `service.reinforce` and the dream loop bump the resident in-memory entry in lockstep with the DB.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recall.py`:

```python
@pytest.mark.skipif(not _pg_up(), reason="bench Postgres not reachable")
def test_reinforce_syncs_in_memory(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    from ladder_sweep import build_service
    svc = build_service(tmp_path)
    svc.store("sync-me runtime: jdk-21", source="general")
    st = svc._storage  # noqa: SLF001
    eid = st.conn.execute("SELECT id FROM entries ORDER BY id DESC LIMIT 1").fetchone()[0]

    def resident():
        for b in svc._cms.bands:                      # noqa: SLF001
            for e in b.entries:
                if e.db_id == eid:
                    return e
        return None

    r = resident()
    assert r is not None and r.reinforcements == 0
    out = svc.reinforce(eid)
    assert out["reinforced"] is True
    assert resident().reinforcements == 1            # in-memory synced
    assert st.conn.execute(
        "SELECT reinforcements FROM entries WHERE id=%s", (eid,)).fetchone()[0] == 1
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k reinforce_syncs_in_memory -v`
Expected: FAIL — `assert 0 == 1` (DB bumped, but the resident in-memory entry is still 0).

- [ ] **Step 2: Add the CMS sync helper**

In `pseudolife_memory/memory/cms.py`, add a method to `ContinuumMemorySystem` (near the other band-iterating helpers):

```python
    def bump_entry_reinforcements(self, db_id: int, delta: int) -> bool:
        """Bump the resident entry's in-memory reinforcement counter to match a
        DB bump, so eviction scoring reflects it without a reload. Returns True
        if the entry was resident (a no-op + False otherwise — e.g. already
        evicted; the DB value stands and is reloaded on next hydrate)."""
        for band in self.bands:
            for e in band.entries:
                if e.db_id == db_id:
                    e.reinforcements += delta
                    return True
        return False
```

- [ ] **Step 3: Sync in `service.reinforce`**

In `pseudolife_memory/service.py`, `reinforce`, after the storage bump line `self._storage.bump_reinforcements(int(entry_id), 1)` (still inside the `with self._lock:` block), add the in-memory sync:

```python
            self._storage.bump_reinforcements(int(entry_id), 1)
            if self._cms is not None:
                self._cms.bump_entry_reinforcements(int(entry_id), 1)
```

- [ ] **Step 4: Sync in the dream loop**

In `pseudolife_memory/service.py`, `dream_run`, inside the `with self._lock:` trace block, after `self._storage.bump_reinforcements(src_id, 1)`, add the matching in-memory sync:

```python
                        with self._lock:
                            if self._storage.add_trace(
                                    _norm_key(ent), _norm_key(attr), src_id, _time.time()):
                                self._storage.bump_reinforcements(src_id, 1)
                                if self._cms is not None:
                                    self._cms.bump_entry_reinforcements(src_id, 1)
                                traces_n += 1
```

- [ ] **Step 5: Run the test + the recall trace tests**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_recall.py -k "reinforce_syncs_in_memory or dream_writes_fact_traces or memory_get_and_reinforce" -v`
Expected: PASS (the new sync test + the Phase-1 trace/get tests still green).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/cms.py pseudolife_memory/service.py tests/test_recall.py
git commit -m "feat(retention): sync in-memory reinforcements on reinforce + dream bump"
```

---

## Task 4: Eviction-dynamics tuning bench

**Files:**
- Create: `evals/retention_bench.py`
- Create: `evals/results/retention.json` (written by the bench)
- Test: `tests/test_retention_bench.py` (new, smoke)

**Interfaces:**
- Consumes: `build_band`/`build_policy` + the boost term (Task 1); `MemoryEntry.reinforcements` (Task 1).
- Produces: `evals/retention_bench.py` with `run_bench() -> list[dict]` and a `__main__` that writes `evals/results/retention.json`; deterministic.

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_retention_bench.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))


def test_retention_bench_runs_and_shows_protection():
    from retention_bench import run_bench, GRID
    rows = run_bench()
    assert [r["retention_boost"] for r in rows] == GRID
    by_boost = {r["retention_boost"]: r for r in rows}
    # boost=0 must match today's eviction: reinforced entries get NO protection,
    # so their survival rate is no better than the unreinforced baseline.
    z = by_boost[0.0]
    assert z["reinforced_survival_rate"] <= z["unreinforced_survival_rate"] + 1e-9
    # a high boost must measurably protect reinforced entries vs boost=0.
    assert by_boost[max(GRID)]["reinforced_survival_rate"] > z["reinforced_survival_rate"]
```

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_retention_bench.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'retention_bench'`.

- [ ] **Step 2: Write the bench**

Create `evals/retention_bench.py`:

```python
"""Eviction-dynamics bench for ``retention_boost`` (provenance-as-link Phase 2).

Drives the REAL ``MIRASBand`` + production ``RetentionPolicy`` under capacity
pressure to measure how ``retention_boost`` protects reinforced episodes versus
how it respects recency. Dev-only, CPU, deterministic, no Postgres.

Run: HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe evals/retention_bench.py
Writes: evals/results/retention.json
"""
from __future__ import annotations

import json
from pathlib import Path

import torch

import pseudolife_memory.memory.miras.band as bandmod
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.utils.config import MIRASBandSpec

DIM = 8
CAP = 50                 # band capacity
N_TOTAL = 200            # 4x capacity -> heavy, sustained eviction
REINFORCED_EVERY = 4     # every 4th entry is "reinforced" (got used)
REINFORCE_LEVEL = 5      # reinforcements on a reinforced entry
SOURCE = "agent_action"  # neutral source weight (1.0) so the boost is isolated
GRID = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0]

# Deterministic "now" pinned past every entry timestamp so age = NOW - i is stable.
_NOW = float(N_TOTAL + 1)


def _run_one(retention_boost: float) -> dict:
    spec = MIRASBandSpec(name="bench", max_entries=CAP,
                         retention_policy="balanced")
    band = build_band(spec, embedding_dim=DIM, device="cpu",
                      retention_boost=retention_boost)
    reinforced, fresh_unreinforced = set(), set()
    for i in range(N_TOTAL):
        band.store(text=f"e{i}", embedding=torch.zeros(DIM), source=SOURCE, surprise=0.0)
        e = band.entries[-1]
        e.db_id = i
        e.timestamp = float(i)           # higher i = newer
        # access_count correlated with recency (newer entries were used more),
        # so the base eviction score has a real recency/usage gradient to
        # compete against the reinforcement boost.
        e.access_count = i
        if i % REINFORCED_EVERY == 0:
            e.reinforcements = REINFORCE_LEVEL
            reinforced.add(i)
        else:
            fresh_unreinforced.add(i)

    survivors = {e.db_id for e in band.entries}
    surv_ts = [float(db_id) for db_id in survivors]
    rs = survivors & reinforced
    us = survivors & fresh_unreinforced
    return {
        "retention_boost": retention_boost,
        "reinforced_total": len(reinforced),
        "reinforced_survived": len(rs),
        "reinforced_survival_rate": len(rs) / max(1, len(reinforced)),
        "unreinforced_total": len(fresh_unreinforced),
        "unreinforced_survived": len(us),
        "unreinforced_survival_rate": len(us) / max(1, len(fresh_unreinforced)),
        # recency-displacement signal: mean recency (timestamp) of survivors.
        # Falls as the boost pins older reinforced entries over fresher ones.
        "mean_survivor_timestamp": (sum(surv_ts) / len(surv_ts)) if surv_ts else 0.0,
    }


def run_bench() -> list[dict]:
    # Pin the eviction clock for determinism (band._evict_one calls now_seconds).
    orig = bandmod.now_seconds
    bandmod.now_seconds = lambda: _NOW
    try:
        return [_run_one(b) for b in GRID]
    finally:
        bandmod.now_seconds = orig


def main() -> None:
    rows = run_bench()
    out = Path(__file__).resolve().parent / "results" / "retention.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    hdr = (f"{'boost':>6}  {'reinf_surv':>10}  {'unreinf_surv':>12}  "
           f"{'mean_surv_ts':>12}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['retention_boost']:>6}  "
              f"{r['reinforced_survival_rate']:>10.2f}  "
              f"{r['unreinforced_survival_rate']:>12.2f}  "
              f"{r['mean_survivor_timestamp']:>12.1f}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the smoke test**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_retention_bench.py -v`
Expected: PASS (boost=0 gives reinforced no edge; max boost protects reinforced).

- [ ] **Step 4: Run the bench + inspect the curve**

Run: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe evals/retention_bench.py`
Expected: a printed table + `evals/results/retention.json`. Read the curve: pick the **knee** — the smallest `retention_boost` where `reinforced_survival_rate` is high while `mean_survivor_timestamp` has not collapsed (recency still respected). Record the suggested default in the report for the controller/user (the knob still ships at 0.0; choosing a live value is a follow-on decision). If boost=0 vs high boost shows no meaningful separation, the workload is too gentle — raise `N_TOTAL`/`REINFORCE_LEVEL` or lower `CAP` and re-run (mirrors the memcot/seed bench corpus-hardening pattern).

- [ ] **Step 5: Commit**

```bash
git add evals/retention_bench.py evals/results/retention.json tests/test_retention_bench.py
git commit -m "feat(retention): eviction-dynamics bench sweeping retention_boost"
```

---

## Final verification
- [ ] `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pytest tests/test_retention_boost.py tests/test_retention_bench.py tests/test_recall.py -q`
- [ ] Confirm: `retention_boost=0.0` path is byte-identical (Task 1 `test_boost_zero_ignores_reinforcements` + existing eviction tests green); the new PG tests RAN (not skipped); the bench emits the survival-vs-boost curve.
- [ ] No `schema.py` / `SCHEMA_META_VERSION` change (code-only phase).

## Self-review notes (coverage vs spec)
- Boost term, additive after source-weight multiply, default 0.0 no-op → Task 1 (`test_boost_zero_ignores_reinforcements`, `test_boost_positive_protects_reinforced`).
- Deterministic eviction-order + relative/no-deadlock (boost changes the victim; band still evicts) → Task 1 (`test_evict_one_protects_reinforced_and_still_evicts`).
- `reinforcements` on `MemoryEntry` + config knob + factory/band/cms threading → Task 1.
- DB-authoritative load (no `_ENTRY_COLS`/insert/save change) → Task 2.
- In-memory sync on both bump paths (reinforce + dream), resident-only no-op → Task 3.
- Tuning bench (real band, sweep, reinforced-survival + recency-displacement, JSON + table, knee selection) → Task 4.
- Deferred/tabled (reinforcement decay, reinforce_increment, learned policy) → not implemented, by design.

## Phase 2 follow-on (after this lands)
Choose a `retention_boost` default from the bench curve; if non-zero, ship it via a small config change + deploy (gated: backup → rebuild daemon only → verify). Reinforcement-decay remains tabled future research.
