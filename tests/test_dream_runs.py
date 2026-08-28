"""Dream-run audit + pre-image journal + rollback (schema v27).

PG-backed (skips without the bench server). The load-bearing test here is
``test_journal_survives_compaction``: the whole feature exists because the
facts supersession chain is purged by ``compact_superseded`` in steady
state, so a rollback source must live outside it.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


class _Stub:
    """Fixed claims regardless of input (drives dream_run's claim loop)."""

    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab, known_facts=None):
        return [dict(c) for c in self._claims]


def _scalar(entity, attribute, value, source=0, **kw):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.8, "origin": "agent", "source": source, **kw}


def _runs(svc):
    return svc._storage.recent_dream_runs(limit=20)


def _journal(svc, run_id):
    return svc._storage.dream_run_journal(run_id)


# ── storage shape the journal depends on ─────────────────────────────────
# Direct SQL rather than through the service: these pin the DDL contracts
# rollback rests on (CASCADE, the JSONB tallies blob, the nullable
# pre-image) independently of whether dream_run happens to exercise them.


def test_run_delete_cascades_to_journal(pg_conn):  # noqa: F811
    """Pruning a run must take its journal with it — the FK is ON DELETE
    CASCADE precisely so pruning cannot orphan pre-images."""
    pg_conn.execute(
        "INSERT INTO dream_runs (started_at, cursor_before, pulled, status) "
        "VALUES (1.0, 0.0, 3, 'committed')")
    run_id = pg_conn.execute(
        "SELECT id FROM dream_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    pg_conn.execute(
        "INSERT INTO dream_run_slots (run_id, seq, entity, attribute, "
        "entity_norm, attribute_norm, kind, action, at) "
        "VALUES (%s, 0, 'proj', 'lang', 'proj', 'lang', 'scalar', "
        "'inserted', 1.0)", (run_id,))
    pg_conn.commit()
    pg_conn.execute("DELETE FROM dream_runs WHERE id = %s", (run_id,))
    pg_conn.commit()
    left = pg_conn.execute(
        "SELECT count(*) FROM dream_run_slots WHERE run_id = %s",
        (run_id,)).fetchone()[0]
    assert left == 0


def test_null_prev_status_and_jsonb_tallies_round_trip(pg_conn):  # noqa: F811
    """``tallies`` must read back as a dict (JSONB, not text), and a slot
    with no pre-image — an insert — must store NULL rather than a sentinel
    string that rollback would then try to restore."""
    pg_conn.execute(
        "INSERT INTO dream_runs (started_at, cursor_before, pulled, status, "
        "tallies) VALUES (1.0, 0.0, 2, 'committed', "
        "'{\"inserted\": 2, \"literal_dropped\": 1}'::jsonb)")
    run_id = pg_conn.execute(
        "SELECT id FROM dream_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    pg_conn.execute(
        "INSERT INTO dream_run_slots (run_id, seq, entity, attribute, "
        "entity_norm, attribute_norm, kind, prev_status, action, at) "
        "VALUES (%s, 0, 'p', 'a', 'p', 'a', 'scalar', NULL, 'inserted', "
        "1.0)", (run_id,))
    pg_conn.commit()
    tallies = pg_conn.execute(
        "SELECT tallies FROM dream_runs WHERE id = %s", (run_id,)).fetchone()[0]
    assert tallies == {"inserted": 2, "literal_dropped": 1}
    prev = pg_conn.execute(
        "SELECT prev_status FROM dream_run_slots WHERE run_id = %s",
        (run_id,)).fetchone()[0]
    assert prev is None


# ── run rows ─────────────────────────────────────────────────────────────

def test_dream_run_records_a_run_row(svc):
    svc.store("the mascot is a fox", source="notes")
    out = svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    assert out["inserted"] == 1
    runs = _runs(svc)
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "committed"
    assert run["pulled"] == 1 and run["claims"] == 1
    assert run["cursor_after"] is not None
    assert run["cursor_after"] > run["cursor_before"]
    assert run["finished_at"] is not None
    assert run["tallies"].get("inserted") == 1


def test_zero_pull_dream_records_no_run(svc):
    out = svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    assert out["pulled"] == 0
    assert _runs(svc) == []


def test_empty_extraction_records_no_run(svc):
    svc.store("idle chatter with nothing durable", source="notes")
    out = svc.dream_run(_Stub([]))
    assert out["pulled"] == 1 and out["claims"] == 0
    assert _runs(svc) == []


def test_extractor_failure_records_no_run(svc):
    class _Boom:
        def extract(self, texts, vocab, known_facts=None):
            raise RuntimeError("outage")

    svc.store("the mascot is a fox", source="notes")
    out = svc.dream_run(_Boom())
    assert out.get("extractor_failed") is True
    assert _runs(svc) == []


def test_claim_write_failure_records_failed_with_landed_journal(svc, monkeypatch):
    svc.store("alpha is 1 and beta is 2", source="notes")
    real = svc.cortex_write
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("write blew up")
        return real(*a, **kw)

    monkeypatch.setattr(svc, "cortex_write", flaky)
    out = svc.dream_run(_Stub([
        _scalar("proj", "alpha", "1"),
        _scalar("proj", "beta", "2"),
    ]))
    assert out.get("extractor_failed") is True     # held shape: cursor kept
    runs = _runs(svc)
    assert len(runs) == 1 and runs[0]["status"] == "failed"
    assert runs[0]["cursor_after"] is None
    rows = _journal(svc, runs[0]["id"])
    assert len(rows) == 1                          # only the landed write
    assert rows[0]["entity_norm"] == "proj" and rows[0]["action"] == "inserted"


# ── journal pre-images ───────────────────────────────────────────────────

def test_journal_captures_scalar_insert_pre_image(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    rows = _journal(svc, _runs(svc)[0]["id"])
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "scalar" and r["op"] is None
    assert r["prev_kind"] is None and r["prev_value"] is None
    assert r["prev_status"] is None
    assert r["new_value"] == "fox" and r["action"] == "inserted"
    assert r["src_entry_id"] is not None


def test_journal_captures_scalar_supersede_pre_image(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    svc.store("the mascot changed to an owl", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl")]))
    runs = _runs(svc)
    assert len(runs) == 2
    newest = runs[0]
    rows = _journal(svc, newest["id"])
    assert len(rows) == 1
    r = rows[0]
    assert r["prev_kind"] == "scalar"
    assert r["prev_value"] == "fox" and r["prev_status"] == "current"
    assert r["prev_confidence"] is not None
    assert r["new_value"] == "owl" and r["action"] == "superseded"


def test_journal_captures_member_add_and_remove(svc):
    svc.store("tried Rosa's Diner tonight", source="notes")
    svc.dream_run(_Stub([_scalar("user", "restaurants tried", "Rosa's Diner",
                                 op="add")]))
    svc.store("scratch Rosa's Diner off the list", source="notes")
    svc.dream_run(_Stub([_scalar("user", "restaurants tried", "Rosa's Diner",
                                 op="remove")]))
    runs = _runs(svc)
    add_rows = _journal(svc, runs[1]["id"])
    rm_rows = _journal(svc, runs[0]["id"])
    assert add_rows[0]["kind"] == "member" and add_rows[0]["op"] == "add"
    assert add_rows[0]["prev_status"] is None       # member did not exist
    assert add_rows[0]["action"] == "member_added"
    assert rm_rows[0]["op"] == "remove"
    assert rm_rows[0]["prev_value"] == "Rosa's Diner"
    assert rm_rows[0]["prev_status"] == "current"
    assert rm_rows[0]["action"] == "member_removed"


def test_gate_dropped_claim_journals_nothing(svc):
    svc.config.memory.dream.literal_gate = "enforce"
    svc.store("saw a flicker, that makes 32 species now", source="notes")
    out = svc.dream_run(_Stub([_scalar("user", "species count", "41")]))
    assert out["literal_dropped"] == 1
    runs = _runs(svc)
    assert len(runs) == 1                      # pass had claims -> row exists
    assert runs[0]["status"] == "committed"
    assert runs[0]["tallies"].get("literal_dropped") == 1
    assert _journal(svc, runs[0]["id"]) == []  # nothing written, no journal


def test_journal_survives_compaction(svc):
    """THE load-bearing property: the journal outlives the superseded fact
    row that compaction purges."""
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    svc.store("the mascot changed to an owl", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl")]))

    svc.config.memory.compaction.keep_per_slot = 0
    svc.config.memory.compaction.min_age_days = 0.0
    svc.compact_superseded()

    superseded = [r for r in svc._cortex.records_for("team", "mascot")
                  if r.status == "superseded"]
    assert superseded == [], "compaction should have purged the old row"

    newest = _runs(svc)[0]
    rows = _journal(svc, newest["id"])
    assert rows[0]["prev_value"] == "fox", (
        "the pre-image must survive compaction — it is the rollback source")


# ── retention ────────────────────────────────────────────────────────────

def test_prune_dream_runs_keeps_newest_n(svc, pg_conn):
    for i in range(4):
        svc.store(f"observation number {i} about the fleet", source="notes")
        svc.dream_run(_Stub([_scalar("fleet", f"obs-{i}", str(i),)]))
    assert len(_runs(svc)) == 4
    svc.config.memory.dream.runs_keep = 2
    pruned = svc.prune_dream_runs()
    assert pruned == 2
    runs = _runs(svc)
    assert len(runs) == 2
    assert svc._storage.dream_run_journal(runs[-1]["id"]) != []
    # CASCADE: journals of pruned runs are gone with their runs.
    total = pg_conn.execute(
        "SELECT count(*) FROM dream_run_slots").fetchone()[0]
    assert total == 2


def test_runs_listing_is_compact(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    run = _runs(svc)[0]
    assert {"id", "started_at", "finished_at", "cursor_before",
            "cursor_after", "pulled", "claims", "tallies", "status",
            "extractor"} <= set(run)


# ── rollback ─────────────────────────────────────────────────────────────

def test_rollback_restores_prev_scalar_via_supersede(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    svc.store("the mascot changed to an owl", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl")]))
    out = svc.dream_rollback()
    assert out.get("error") is None, out
    assert out["reverted"] == 1
    cur = svc.cortex_lookup("team", "mascot")
    assert cur is not None and cur["value"] == "fox"
    # History preserved, not rewritten: original -> dream -> revert.
    rows = svc._cortex.records_for("team", "mascot")
    assert len(rows) >= 3
    assert _runs(svc)[0]["status"] == "rolled_back"


def test_rollback_restores_the_superseded_records_stance(svc):
    """Review fix (2026-08-13): the journal's fixed columns carry no
    stance (v29 spec amendment 3), so ``_rewrite_prev`` recovers it from
    the superseded record itself — without that, a rollback silently
    converts a restored hedged fact into a plain assertion, the exact
    failure the stance field exists to prevent."""
    svc.store("the project is probably delayed", source="notes")
    svc.dream_run(_Stub([_scalar("project", "status", "maybe delayed",
                                 stance="probably")]))
    svc.store("the project is on track now", source="notes")
    svc.dream_run(_Stub([_scalar("project", "status", "on track",
                                 confidence=0.9)]))
    out = svc.dream_rollback()
    assert out.get("error") is None, out
    assert out["reverted"] == 1
    cur = svc.cortex_lookup("project", "status")
    assert cur is not None and cur["value"] == "maybe delayed"
    assert cur.get("stance") == "probably"


def test_rollback_retires_inserted_slot(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    out = svc.dream_rollback()
    assert out["reverted"] == 1
    assert svc.cortex_lookup("team", "mascot") is None
    rows = svc._cortex.records_for("team", "mascot")
    assert len(rows) == 1 and rows[0].status == "retired"


def test_rollback_forces_contested_revert(svc):
    # Seed a low-confidence prior, let the dream supersede it with high
    # confidence; the revert's re-write of the low-confidence prev would
    # normally park as a contender — rollback is explicit authority, so it
    # must still win the slot back.
    svc.cortex_write("team", "mascot", "fox", confidence=0.3, support="agent")
    svc.store("the mascot changed to an owl", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl", confidence=0.95)]))
    out = svc.dream_rollback()
    assert out.get("error") is None, out
    cur = svc.cortex_lookup("team", "mascot")
    assert cur is not None and cur["value"] == "fox"


def test_rollback_removes_added_member(svc):
    svc.set_add("user", "restaurants tried", "Old Haunt")
    svc.store("tried Rosa's Diner tonight", source="notes")
    svc.dream_run(_Stub([_scalar("user", "restaurants tried", "Rosa's Diner",
                                 op="add")]))
    out = svc.dream_rollback()
    assert out["reverted"] == 1
    got = svc.cortex_lookup("user", "restaurants tried")
    assert [m["value"] for m in got["members"]] == ["Old Haunt"]


def test_rollback_readds_removed_member(svc):
    svc.set_add("user", "restaurants tried", "Rosa's Diner")
    svc.store("scratch Rosa's Diner off the list", source="notes")
    svc.dream_run(_Stub([_scalar("user", "restaurants tried", "Rosa's Diner",
                                 op="remove")]))
    out = svc.dream_rollback()
    assert out["reverted"] == 1
    got = svc.cortex_lookup("user", "restaurants tried")
    assert [m["value"] for m in got["members"]] == ["Rosa's Diner"]


def test_rollback_unwinds_scalar_to_set_conversion(svc):
    svc.cortex_write("team", "mascot", "fox", confidence=0.9, support="user")
    svc.store("adding an owl to the mascots", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl", op="add")]))
    # Conversion happened: the scalar became a member alongside owl.
    assert svc.cortex_lookup("team", "mascot")["kind"] == "set"
    out = svc.dream_rollback()
    assert out.get("error") is None, out
    cur = svc.cortex_lookup("team", "mascot")
    assert cur is not None and cur.get("kind") != "set", cur
    assert cur["value"] == "fox"


def test_rollback_keeps_set_when_other_members_arrived(svc):
    svc.cortex_write("team", "mascot", "fox", confidence=0.9, support="user")
    svc.store("adding an owl to the mascots", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "owl", op="add")]))
    # A member the run did NOT add arrives afterwards.
    svc.set_add("team", "mascot", "crow")
    out = svc.dream_rollback()
    assert out["partial"] == 1
    got = svc.cortex_lookup("team", "mascot")
    values = {m["value"] for m in got["members"]}
    assert "owl" not in values and "crow" in values and "fox" in values


def test_rollback_is_refused_twice(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    assert svc.dream_rollback().get("error") is None
    again = svc.dream_rollback()
    assert again.get("error") == "no_committed_run"


def test_rollback_refused_when_newer_failed_run_exists(svc, monkeypatch):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    # Newer failed run: a write that blows up mid-loop.
    svc.store("alpha is 1", source="notes")

    def boom(*a, **kw):
        raise RuntimeError("write blew up")

    monkeypatch.setattr(svc, "cortex_write", boom)
    svc.dream_run(_Stub([_scalar("proj", "alpha", "1")]))
    monkeypatch.undo()
    out = svc.dream_rollback()
    assert out.get("error") == "newer_unjournaled_runs"


def test_rollback_with_stale_run_id_refused(svc):
    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    out = svc.dream_rollback(run_id=99999)
    assert out.get("error") == "stale_run_id"
    assert out["latest"] == _runs(svc)[0]["id"]


def test_rollback_keeps_traces_and_cursor(svc):
    from pseudolife_memory.memory.cortex import _norm_key

    svc.store("the mascot is a fox", source="notes")
    svc.dream_run(_Stub([_scalar("team", "mascot", "fox")]))
    cursor_before = svc._cortex.dream_cursor
    src_id = _journal(svc, _runs(svc)[0]["id"])[0]["src_entry_id"]
    svc.dream_rollback()
    assert svc._cortex.dream_cursor == cursor_before
    assert svc._storage.has_trace(
        _norm_key("team"), _norm_key("mascot"), src_id)


def test_rollback_requires_postgres(tmp_path):
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path)          # file mode, no PG
    assert s.dream_rollback().get("error") == "requires_postgres"


# ── memory_history as_of (per-slot point-in-time read) ───────────────────

def test_history_as_of_filters_versions(svc):
    import time as _t

    svc.cortex_write("team", "mascot", "fox", confidence=0.9, support="user")
    _t.sleep(0.02)
    mid = _t.time()
    _t.sleep(0.02)
    svc.cortex_write("team", "mascot", "owl", confidence=0.9, support="user")

    full = svc.history("team", "mascot")
    assert full["count"] == 2 and "as_of" not in full

    at_mid = svc.history("team", "mascot", as_of=mid)
    assert at_mid["count"] == 1
    assert at_mid["versions"][0]["value"] == "fox"
    assert at_mid["as_of"] == mid

    later = svc.history("team", "mascot", as_of=_t.time())
    assert later["count"] == 2


def test_history_as_of_accepts_iso_string(svc):
    from datetime import datetime, timedelta

    svc.cortex_write("team", "mascot", "fox", confidence=0.9, support="user")
    tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
    out = svc.history("team", "mascot", as_of=tomorrow)
    assert out["count"] == 1
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    out = svc.history("team", "mascot", as_of=yesterday)
    assert out["count"] == 0


def test_history_as_of_set_slot(svc):
    import time as _t

    svc.set_add("user", "tags", "alpha")
    _t.sleep(0.02)
    mid = _t.time()
    _t.sleep(0.02)
    svc.set_add("user", "tags", "beta")
    out = svc.history("user", "tags", as_of=mid)
    assert out["kind"] == "set"
    assert [v["value"] for v in out["versions"]] == ["alpha"]
