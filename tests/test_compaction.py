"""Superseded-row compaction (spec 2026-07-14).

Policy: per slot, pool non-live records (superseded/retired), keep the
newest ``keep_per_slot``, purge the rest when older than ``min_age_days``.
current/contested are never touched.
"""
from __future__ import annotations

import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.compaction import compact_store
from pseudolife_memory.memory.cortex import CortexStore, CortexRecord
from pseudolife_memory.memory.lessons import LessonStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.memory.world_cortex import WorldCortexStore

EMB = torch.zeros(384)   # facts.embedding is vector(384) — PG enforces it
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
    for v in ("a", "b", "c"):
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


# ── supersession_log cap (same growth class) ────────────────────────────

def test_supersession_log_capped_in_memory():
    from pseudolife_memory.memory.cortex import SUPERSESSION_LOG_CAP
    s = CortexStore()
    for i in range(SUPERSESSION_LOG_CAP + 50):
        s.write_fact(Slot("proj", "version", f"v{i}"), EMB,
                     support="user", now=T0 + i)
    assert len(s.supersession_log) == SUPERSESSION_LOG_CAP
    # Newest entries survive the trim.
    assert s.supersession_log[-1]["new_value"] == f"v{SUPERSESSION_LOG_CAP + 49}"


# ── config ──────────────────────────────────────────────────────────────

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


# ── service integration ─────────────────────────────────────────────────

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


# ── PG persistence (the dirty-slots hook is load-bearing) ───────────────

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


# ── dream sweep trigger ─────────────────────────────────────────────────

def test_sweep_compacts_even_when_dream_gate_closed(pristine_service, monkeypatch):
    from pseudolife_memory.memory.dream import run_sweep_once
    svc = pristine_service
    monkeypatch.setattr(svc.config.memory.dream, "enabled", True)
    calls = []
    monkeypatch.setattr(svc, "compact_superseded",
                        lambda: calls.append(1) or {"total": 7})
    monkeypatch.setattr(svc, "dream_status",
                        lambda: {"would_fire": False, "backlog": 0})
    out = run_sweep_once(svc)
    assert calls == [1]
    assert out["fired"] is False and out["compacted"] == 7
