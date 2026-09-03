"""Retire-not-delete for lesson/world forgets (2026-09-03, schema v37).

The 2026-09-02 review-queue triage hard-deleted eleven lessons; three
carried guidance no survivor had, and nothing in the bank could bring
them back (``lesson_write`` is dream-only and the rows were gone). Contracts:

* ``lesson_forget`` / ``world_forget`` RETIRE the slot's current records
  (``status='retired'``, row kept) and write an FK-free ``store_decisions``
  audit row carrying the verbatim record;
* retired records are served nowhere (dump / search / lookup) and are
  subject to the existing compaction rule like any non-live record;
* ``lesson_restore`` / ``world_restore`` bring a slot back from its retired
  record — or, once compaction has purged it, from the audit snapshot;
* a slot that is live again is never overwritten by a restore;
* the curation judge's ``auto`` forget goes through the same path stamped
  ``decided_by='dream-judge'``.

PG-backed (skips without the bench server).
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    with s._lock:
        s._ensure_init()
    yield s
    s.flush()


def _pg_rows(svc, table, entity_norm):
    return svc._storage.conn.execute(
        f"SELECT status, value FROM {table} WHERE entity_norm = %s ORDER BY id",
        (entity_norm,)).fetchall()


# ── lessons ───────────────────────────────────────────────────────────────

def test_lesson_forget_retires_keeps_the_row_and_audits(svc):
    svc.lesson_write("deploy engine", "approach", "back up first",
                     about="engine-host", confidence=0.9)
    out = svc.lesson_forget("deploy engine", "approach",
                            decided_by="agent", reason="duplicate of x")
    assert out["removed"] == 1 and out["retired"] == 1
    assert svc.lessons_dump()["count"] == 0
    assert svc.lesson_search("deploy engine")["count"] == 0
    assert _pg_rows(svc, "lessons", "deploy-engine") == [("retired", "back up first")]
    dec = svc._storage.store_decisions("lesson")
    assert len(dec) == 1
    d = dec[0]
    assert (d["action"], d["decided_by"], d["reason"]) == ("retire", "agent", "duplicate of x")
    assert (d["entity_norm"], d["attribute_norm"]) == ("deploy-engine", "approach")
    assert d["record"]["lesson"] == "back up first"
    assert d["record"]["about"] == "engine-host"
    assert d["record"]["confidence"] == 0.9
    assert svc._lessons.stats()["retired"] == 1


def test_lesson_forget_is_idempotent_on_a_retired_slot(svc):
    svc.lesson_write("deploy engine", "approach", "back up first")
    assert svc.lesson_forget("deploy engine")["removed"] == 1
    assert svc.lesson_forget("deploy engine")["removed"] == 0
    assert len(svc._storage.store_decisions("lesson")) == 1


def test_lesson_forget_whole_task_retires_every_aspect(svc):
    svc.lesson_write("deploy engine", "approach", "back up first")
    svc.lesson_write("deploy engine", "pitfall", "never skip the health check")
    assert svc.lesson_forget("deploy engine")["removed"] == 2
    assert svc.lessons_dump()["count"] == 0
    out = svc.lesson_restore("deploy engine")
    assert out["restored"] == 2
    assert svc.lessons_dump()["count"] == 2


def test_lesson_restore_brings_back_the_retired_record(svc):
    svc.lesson_write("deploy engine", "approach", "back up first",
                     about="engine-host", confidence=0.9, provenance={"ep1"})
    svc.lesson_forget("deploy engine", "approach")
    out = svc.lesson_restore("deploy engine", "approach", decided_by="agent")
    assert out["restored"] == 1 and out["source"] == "retired_record"
    got = svc.lessons_dump()["entries"]
    assert len(got) == 1 and got[0]["lesson"] == "back up first"
    assert got[0]["provenance"] == ["ep1"] and got[0]["confidence"] == 0.9
    assert got[0]["about"] == "engine-host"
    assert _pg_rows(svc, "lessons", "deploy-engine") == [("current", "back up first")]
    assert svc.lesson_search("deploy engine")["count"] == 1
    acts = sorted(d["action"] for d in svc._storage.store_decisions("lesson"))
    assert acts == ["restore", "retire"]
    assert svc.curation_retired("lesson")["entries"] == []


def test_lesson_restore_refuses_when_the_slot_is_live_again(svc):
    svc.lesson_write("deploy engine", "approach", "old")
    svc.lesson_forget("deploy engine", "approach")
    svc.lesson_write("deploy engine", "approach", "new")
    out = svc.lesson_restore("deploy engine", "approach")
    assert out["restored"] == 0 and out["reason"] == "slot_live"
    assert svc.lessons_dump()["entries"][0]["lesson"] == "new"


def test_lesson_restore_reports_nothing_to_restore(svc):
    out = svc.lesson_restore("never written", "approach")
    assert out["restored"] == 0 and out["reason"] == "nothing_retired"


def test_lesson_restore_falls_back_to_the_audit_snapshot_after_compaction(svc):
    svc.lesson_write("deploy engine", "approach", "back up first",
                     about="engine-host", polarity="-", outcome="failure",
                     confidence=0.8, provenance={"ep1"})
    svc.lesson_forget("deploy engine", "approach")
    # Compaction purges the retired row like any non-live record once it
    # is old enough; the audit snapshot is then the only copy.
    cfg = svc.config.memory.compaction
    cfg.keep_per_slot = 0
    cfg.min_age_days = 0
    assert svc.compact_superseded()["lessons"] == 1
    assert _pg_rows(svc, "lessons", "deploy-engine") == []
    out = svc.lesson_restore("deploy engine", "approach", decided_by="agent")
    assert out["restored"] == 1 and out["source"] == "audit_snapshot"
    got = svc.lessons_dump()["entries"][0]
    assert got["lesson"] == "back up first"
    assert got["polarity"] == "-" and got["outcome"] == "failure"
    assert got["about"] == "engine-host" and got["confidence"] == 0.8
    assert got["provenance"] == ["ep1"]
    assert svc.curation_retired("lesson")["entries"] == []


def test_restore_keeps_the_stamps_so_re_verify_survives(svc):
    """A restore must not touch last_confirmed: _annotate_lesson_staleness
    reads max(asserted_at, last_confirmed) against the about-entity's
    fact churn, so a fresh stamp would silently clear re_verify on a
    lesson whose subject changed while it was retired (review, 2026-09-03)."""
    svc.lesson_write("deploy engine", "approach", "use tar --no-same-owner",
                     about="engine-host", now=100.0)
    svc.cortex_write("engine-host", "os", "ubuntu-24")          # later churn
    assert svc.lesson_search("deploy engine")["entries"][0]["re_verify"] is True
    svc.lesson_forget("deploy engine", "approach")
    assert svc.lesson_restore("deploy engine", "approach")["restored"] == 1
    row = svc.lesson_search("deploy engine")["entries"][0]
    assert row["re_verify"] is True
    assert row["last_confirmed"] == 100.0 and row["asserted_at"] == 100.0


def test_whole_task_restore_mixes_retired_records_and_snapshots(svc):
    """"Bring back every retired slot" holds when one slot's retired row
    was already compacted: the in-memory pass covers what it can and the
    audit-snapshot fallback covers the rest, in one call."""
    svc.lesson_write("deploy engine", "approach", "back up first", now=100.0)
    svc.lesson_write("deploy engine", "pitfall", "never skip the health check")
    svc.lesson_forget("deploy engine")
    # purge only the older retire (superseded_at is the retire time; the
    # cutoff sits between the two forgets' stamps)
    with svc._lock:
        recs = {r.key: r for r in svc._lessons.records}
        recs[("deploy-engine", "approach")].superseded_at = 50.0
        svc._lessons.dirty_slots.add(("deploy-engine", "approach"))
        svc._save_lessons()
    cfg = svc.config.memory.compaction
    cfg.keep_per_slot = 0
    cfg.min_age_days = 0
    from pseudolife_memory.memory.compaction import compact_store
    with svc._lock:
        assert compact_store(svc._lessons, keep_per_slot=0, min_age_days=0,
                             now=1000.0) == 1                  # only the 50.0 one
        svc._save_lessons()
    assert _pg_rows(svc, "lessons", "deploy-engine") == [("retired", "never skip the health check")]
    out = svc.lesson_restore("deploy engine")
    assert out["restored"] == 2 and out["source"] == "mixed"
    got = {e["aspect"]: e["lesson"] for e in svc.lessons_dump()["entries"]}
    assert got == {"approach": "back up first",
                   "pitfall": "never skip the health check"}
    assert svc.curation_retired("lesson")["entries"] == []


def test_curation_retired_lists_retired_slots_newest_first(svc):
    svc.lesson_write("a task", "approach", "x")
    svc.lesson_write("b task", "pitfall", "y")
    svc.lesson_forget("a task")
    svc.lesson_forget("b task", decided_by="agent", reason="noise")
    listing = svc.curation_retired("lesson")
    keys = [(e["entity_norm"], e["attribute_norm"]) for e in listing["entries"]]
    assert keys == [("b-task", "pitfall"), ("a-task", "approach")]
    top = listing["entries"][0]
    assert top["key"] == "b-task|pitfall"                  # what restore_slot takes
    assert top["decided_by"] == "agent" and top["reason"] == "noise"
    assert top["record"]["lesson"] == "y"
    svc.lesson_restore("a task")
    assert [e["entity_norm"] for e in svc.curation_retired("lesson")["entries"]] == ["b-task"]
    # store=None lists both stores under one roof
    assert svc.curation_retired()["count"] == 1


def test_curation_retired_drops_a_slot_that_was_written_again(svc):
    """A retired slot the dream re-mints (the common case for lessons) is
    live again: the listing must not keep offering a restore that can only
    answer ``slot_live`` (review finding, 2026-09-03)."""
    svc.lesson_write("deploy engine", "approach", "old")
    svc.lesson_forget("deploy engine", "approach")
    assert [e["entity_norm"] for e in svc.curation_retired("lesson")["entries"]] == ["deploy-engine"]
    svc.lesson_write("deploy engine", "approach", "new")            # re-minted
    assert svc.curation_retired("lesson")["entries"] == []
    assert svc.curation_retired()["count"] == 0


def test_curation_auto_forget_retires_through_the_same_path(svc):
    dup = ("Always take a pg_dump backup via ops/backup.ps1 before deploying "
           "the daemon to the homelab host.")
    svc.lesson_write("deploy daemon to homelab host", "approach", dup)
    svc.lesson_write("deploy the daemon to the host", "pitfall", dup)
    pair = frozenset({"deploy-daemon-to-homelab-host|approach",
                      "deploy-the-daemon-to-the-host|pitfall"})

    class _SlotJudge:
        model = "stub-slot-judge"

        def judge_slot_pairs(self, rows):
            return [{"n": r["n"], "verdict": "duplicate", "keep": "a",
                     "fold": None, "confidence": 0.95, "note": "same rule"}
                    for r in rows
                    if frozenset((r["a_key"], r["b_key"])) == pair]

    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto"
    cfg.curation_forget_min_confidence = 0.9
    out = svc.deep_dream_judge_curation(_SlotJudge())
    assert out["judged"] == 1 and out["applied"] == 1
    assert len(svc._lessons.current_records()) == 1
    assert _pg_rows(svc, "lessons", "deploy-the-daemon-to-the-host") == [("retired", dup)]
    dec = svc._storage.store_decisions("lesson")
    assert len(dec) == 1 and dec[0]["action"] == "retire"
    assert dec[0]["decided_by"] == "dream-judge"
    assert "same rule" in (dec[0]["reason"] or "")
    # and the human undo is one call
    assert svc.lesson_restore("deploy the daemon to the host", "pitfall")["restored"] == 1
    assert len(svc._lessons.current_records()) == 2


# ── world facts ───────────────────────────────────────────────────────────

def test_world_forget_retires_and_restores_with_the_citation(svc):
    svc.world_write("acme", "ceo", "jane", source_url="https://x.test/a",
                    source_quote="Jane is CEO", confidence=0.8)
    out = svc.world_forget("acme", "ceo", decided_by="agent")
    assert out["removed"] == 1 and out["retired"] == 1
    assert svc.world_lookup("acme", "ceo") is None
    assert svc.world_dump()["count"] == 0
    assert _pg_rows(svc, "world_facts", "acme") == [("retired", "jane")]
    d = svc._storage.store_decisions("world")[0]
    assert d["record"]["source_url"] == "https://x.test/a"
    assert d["record"]["source_quote"] == "Jane is CEO"
    assert svc._world.stats()["retired"] == 1
    out = svc.world_restore("acme", "ceo")
    assert out["restored"] == 1 and out["source"] == "retired_record"
    rec = svc.world_lookup("acme", "ceo")
    assert rec["value"] == "jane" and rec["source_url"] == "https://x.test/a"
    assert _pg_rows(svc, "world_facts", "acme") == [("current", "jane")]


def test_world_restore_from_the_audit_snapshot_keeps_the_citation(svc):
    svc.world_write("acme", "ceo", "jane", source_url="https://x.test/a",
                    source_quote="Jane is CEO", freshness_class="slow",
                    confidence=0.8)
    svc.world_forget("acme", "ceo")
    cfg = svc.config.memory.compaction
    cfg.keep_per_slot = 0
    cfg.min_age_days = 0
    assert svc.compact_superseded()["world_facts"] == 1
    out = svc.world_restore("acme", "ceo")
    assert out["restored"] == 1 and out["source"] == "audit_snapshot"
    rec = svc.world_lookup("acme", "ceo")
    assert rec["value"] == "jane"
    assert rec["source_url"] == "https://x.test/a"
    assert rec["source_quote"] == "Jane is CEO"
    assert rec["freshness_class"] == "slow"
    assert rec["confidence"] == 0.8


def test_world_forget_whole_entity_then_restore_whole_entity(svc):
    svc.world_write("acme", "ceo", "jane", source_url="https://x.test/a")
    svc.world_write("acme", "hq", "berlin", source_url="https://x.test/b")
    assert svc.world_forget("acme")["removed"] == 2
    assert svc.world_dump()["count"] == 0
    assert svc.world_restore("acme")["restored"] == 2
    assert svc.world_dump()["count"] == 2
