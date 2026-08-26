"""Read telemetry (schema v33) — slot reads + the explicit-reinforce split.

Storage half: ``bump_slot_reads`` upsert semantics and
``bump_explicit_reinforcements`` against a live PG. Service half: the two
fact-serving endpoints (``cortex_lookup`` / ``cortex_search``) count a served
slot, write paths and the dream rollback's ``track=False`` lookup do not, an
explicit ``reinforce()`` bumps both reinforcement counters while the dream's
direct ``bump_reinforcements`` leaves the explicit counter alone, and
``stats()`` carries the ``read_audit`` section. Slot counters survive a
cortex snapshot save (facts rows regenerate; ``slot_reads`` is slot-keyed).

Skips cleanly without a PG server (mirrors test_retrieval_log.py).
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.service import MemoryService

    yield MemoryService(data_dir=tmp_path, database_url=pg_url)


def _slot_row(pg_conn, entity_norm: str, attribute_norm: str):
    return pg_conn.execute(
        "SELECT read_count, last_read_at FROM slot_reads "
        "WHERE entity_norm = %s AND attribute_norm = %s",
        (entity_norm, attribute_norm),
    ).fetchone()


def _stored_entry_id(svc, text: str) -> int:
    svc.store(text, source="test")
    res = svc.search(text)
    assert res["count"] >= 1
    return int(res["entries"][0]["id"])


# ------------------------------------------------------------------
# Storage half
# ------------------------------------------------------------------

def test_bump_slot_reads_upserts_and_increments(storage, pg_conn):
    storage.bump_slot_reads([("server", "ip")], now=1000.0)
    assert _slot_row(pg_conn, "server", "ip") == (1, 1000.0)
    storage.bump_slot_reads([("server", "ip"), ("server", "os")], now=2000.0)
    assert _slot_row(pg_conn, "server", "ip") == (2, 2000.0)
    assert _slot_row(pg_conn, "server", "os") == (1, 2000.0)


def test_bump_slot_reads_empty_is_noop(storage, pg_conn):
    storage.bump_slot_reads([], now=1000.0)
    n = pg_conn.execute("SELECT COUNT(*) FROM slot_reads").fetchone()[0]
    assert n == 0


# ------------------------------------------------------------------
# Service half
# ------------------------------------------------------------------

def test_fact_get_counts_a_slot_read(svc, pg_conn):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    assert _slot_row(pg_conn, "dev-box", "gpu") is None, (
        "a write (which looks up the slot internally) must not count as a read")
    rec = svc.cortex_lookup("dev-box", "gpu")
    assert rec is not None and rec["value"] == "RTX 4090"
    row = _slot_row(pg_conn, "dev-box", "gpu")
    assert row is not None and row[0] == 1
    svc.cortex_lookup("dev-box", "gpu")
    assert _slot_row(pg_conn, "dev-box", "gpu")[0] == 2


def test_fact_get_miss_counts_nothing(svc, pg_conn):
    assert svc.cortex_lookup("nobody", "nothing") is None
    assert _slot_row(pg_conn, "nobody", "nothing") is None


def test_untracked_lookup_counts_nothing(svc, pg_conn):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    rec = svc.cortex_lookup("dev-box", "gpu", track=False)
    assert rec is not None
    assert _slot_row(pg_conn, "dev-box", "gpu") is None


def test_set_slot_lookup_counts_once(svc, pg_conn):
    svc.set_add("user", "bikes", "gravel bike", origin="user")
    svc.set_add("user", "bikes", "road bike", origin="user")
    res = svc.cortex_lookup("user", "bikes")
    assert res is not None and res["kind"] == "set"
    row = _slot_row(pg_conn, "user", "bikes")
    assert row is not None and row[0] == 1, (
        "a set slot is one slot — served once, counted once")


def test_cortex_search_counts_served_slots(svc, pg_conn):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    res = svc.cortex_search("what gpu does the dev box have", top_k=3,
                            min_score=0.0)
    assert res["count"] >= 1
    row = _slot_row(pg_conn, "dev-box", "gpu")
    assert row is not None and row[0] == 1


def test_read_tracking_kill_switch(svc, pg_conn):
    svc.config.memory.cortex.read_tracking = False
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    svc.cortex_lookup("dev-box", "gpu")
    svc.cortex_search("what gpu does the dev box have", top_k=3)
    n = pg_conn.execute("SELECT COUNT(*) FROM slot_reads").fetchone()[0]
    assert n == 0


def test_reinforce_bumps_both_counters_dream_bumps_one(svc, pg_conn):
    eid = _stored_entry_id(svc, "the quick brown fox jumps over the lazy dog")
    svc.reinforce(eid)
    row = pg_conn.execute(
        "SELECT reinforcements, explicit_reinforcements FROM entries "
        "WHERE id = %s", (eid,)).fetchone()
    assert row == (1, 1)
    # The dream's trace path calls bump_reinforcements directly
    # (service.py dream_run) — the explicit counter must not move.
    svc._storage.bump_reinforcements(eid, 1)
    row = pg_conn.execute(
        "SELECT reinforcements, explicit_reinforcements FROM entries "
        "WHERE id = %s", (eid,)).fetchone()
    assert row == (2, 1)


def test_get_entry_reports_explicit_reinforcements(svc):
    eid = _stored_entry_id(svc, "the quick brown fox jumps over the lazy dog")
    svc.reinforce(eid)
    got = svc.get_entry(eid)
    assert got["found"] and got["explicit_reinforcements"] == 1


def test_slot_reads_survive_cortex_snapshot(svc, pg_conn):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    svc.cortex_lookup("dev-box", "gpu")
    assert _slot_row(pg_conn, "dev-box", "gpu")[0] == 1
    svc.save()  # explicit save runs snapshot_cortex (facts rows regenerate)
    assert _slot_row(pg_conn, "dev-box", "gpu")[0] == 1


def test_empty_set_slot_counts_nothing(svc, pg_conn):
    # A set slot whose members were all removed is routed down the miss
    # path by callers — serving nothing must not count as a read.
    svc.set_add("user", "bikes", "gravel bike", origin="user")
    svc.set_remove("user", "bikes", "gravel bike")
    pg_conn.execute("DELETE FROM slot_reads")
    pg_conn.commit()
    res = svc.cortex_lookup("user", "bikes")
    assert res is not None and res["kind"] == "set" and res["members"] == []
    assert _slot_row(pg_conn, "user", "bikes") is None


def test_forget_drops_slot_read_counters(svc, pg_conn):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    svc.cortex_lookup("dev-box", "gpu")
    assert _slot_row(pg_conn, "dev-box", "gpu") is not None
    svc.cortex_forget("dev-box", "gpu")
    assert _slot_row(pg_conn, "dev-box", "gpu") is None


def test_bump_failure_is_swallowed_and_counted(svc, monkeypatch):
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")

    def _boom(*a, **k):
        raise RuntimeError("simulated telemetry failure")

    monkeypatch.setattr(svc._storage, "bump_slot_reads", _boom)
    # The serve must succeed untouched...
    rec = svc.cortex_lookup("dev-box", "gpu")
    assert rec is not None and rec["value"] == "RTX 4090"
    res = svc.cortex_search("what gpu does the dev box have", top_k=3)
    assert res["count"] >= 1
    # ...and the failures must be visible, not silent (a dead counter
    # would otherwise read as "no fact is ever used").
    assert svc._slot_read_errors == 2
    audit = svc.stats().get("read_audit")
    assert audit["slot_read_write_errors"] == 2


def test_stats_carries_read_audit(svc):
    svc.store("the quick brown fox jumps over the lazy dog", source="test")
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    svc.cortex_lookup("dev-box", "gpu")
    audit = svc.stats().get("read_audit")
    assert audit is not None and not audit.get("unavailable")
    entries = audit["entries"]
    assert entries["total"] >= 1
    assert 0.0 <= entries["never_read_pct"] <= 100.0
    assert {"lt_14d", "d14_45", "gt_45d"} <= set(entries["by_age"])
    assert isinstance(audit["worst_sources"], list)
    slots = audit["slots"]
    assert slots["current_slots"] >= 1
    assert slots["slots_read"] >= 1
    assert audit["reinforcements"]["explicit"] >= 0
