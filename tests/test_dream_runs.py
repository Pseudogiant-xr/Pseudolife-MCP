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
