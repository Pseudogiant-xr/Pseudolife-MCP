"""Text-keyed tombstones for reject/keep verdicts (2026-09-03).

``entity_proposals`` rows ``ON DELETE CASCADE`` with their entity, so a
rejected merge proposal ("distinct") and a rejected junk proposal ("keep")
vanished the moment either entity was deleted — and a re-mint of the same
name was re-filed and re-judged as if no verdict had ever been given
(2026-09-02 triage audit). Contracts:

* a merge reject writes the pair's STORED canonicals to ``dismissed_pairs``
  — the text-keyed store every filing gate already consults — whoever
  decided it (human, agent, dream-judge);
* a junk reject writes a namespaced ``junk:<canonical>`` keep tombstone to
  the same table plus a ``merge_decisions`` audit row, and the junk
  detector never files (or auto-deletes) a kept name again, even after the
  entity is deleted and re-minted.

PG-backed (skips without the bench server).
"""
from __future__ import annotations

import time

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


def _merge_rows(svc):
    return [p for p in svc._storage.pending_entity_proposals()
            if p.get("kind") == "merge"]


# ── merge rejects ─────────────────────────────────────────────────────────

def test_merge_reject_tombstones_the_stored_canonical_pair(svc):
    st = svc._storage
    a = st.ensure_entity("gnd", display="GND (Enshrouded server)")   # canonical != norm(display)
    b = st.ensure_entity("gnd-box", display="GND box")
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())
    out = svc.graph_reject_entity_proposal(pid, decided_by="agent")
    assert out["rejected"]
    assert ("gnd", "gnd-box") in st.dismissed_pairs()
    assert st.get_entity_proposal(pid)["status"] == "rejected"


def test_merge_reject_survives_entity_churn(svc):
    """Reject, delete one side (CASCADE eats the proposal row), re-mint it:
    the write-time dedup scan must not file the pair again."""
    st = svc._storage
    a = st.ensure_entity("pseudolife-daemon", display="pseudolife daemon")
    b = st.ensure_entity("pseudolife-daemon-service",
                         display="pseudolife daemon service")
    with svc._lock:
        svc._propose_write_dedup(b, "pseudolife daemon service")
    rows = _merge_rows(svc)
    assert len(rows) == 1 and {rows[0]["entity_id"], rows[0]["into_id"]} == {a, b}
    assert svc.graph_reject_entity_proposal(rows[0]["id"], decided_by="human")["rejected"]
    assert st.delete_entity(b)
    assert st.get_entity_proposal(rows[0]["id"]) is None          # CASCADE
    b2 = st.ensure_entity("pseudolife-daemon-service",
                          display="pseudolife daemon service")
    assert b2 != b
    with svc._lock:
        svc._propose_write_dedup(b2, "pseudolife daemon service")
    assert _merge_rows(svc) == []                                  # tombstoned


def test_judge_auto_reject_tombstones_without_a_second_dismiss(svc):
    """The dream-judge reject path used to add its own display-keyed
    dismissal after the reject; the reject itself now carries the
    canonical pair, so one verdict = one row."""
    st = svc._storage
    a = st.ensure_entity("gnd", display="GND (Enshrouded server)")
    b = st.ensure_entity("gnd-box", display="GND box")
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())

    class _Judge:
        model = "stub"

        def judge_merges(self, proposals):
            return [{"n": p["n"], "verdict": "reject", "confidence": 0.95,
                     "note": "distinct"} for p in proposals]

    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    calls: list[tuple] = []
    real = st.dismiss_pair
    st.dismiss_pair = lambda a, b: calls.append((a, b)) or real(a, b)
    out = svc.deep_dream_judge(_Judge())
    assert out["auto_rejected"] == 1
    assert st.get_entity_proposal(pid)["status"] == "rejected"
    assert ("gnd", "gnd-box") in st.dismissed_pairs()
    # exactly one write, from the reject itself (a second display-keyed
    # dismissal would land on the same PK under ON CONFLICT DO NOTHING,
    # so the row count alone could not see it)
    assert calls == [("gnd", "gnd-box")]


# ── junk keeps ────────────────────────────────────────────────────────────

def test_junk_reject_records_a_keep_tombstone_and_an_audit_row(svc):
    st = svc._storage
    e = st.ensure_entity("ok", display="ok")
    pid = st.insert_entity_proposal("junk", e, None, None, "too-short", time.time())
    out = svc.graph_reject_entity_proposal(pid, decided_by="agent")
    assert out["rejected"]
    assert ("junk:ok", "junk:ok") in st.dismissed_pairs()
    dec = st.recent_entity_decisions(5)
    assert any(d["entity"] == "ok" and d["status"] == "rejected"
               and d["into"] is None and d["decided_by"] == "agent"
               for d in dec)
    # a keep is not a delete: the junk tombstone list stays empty
    assert st.junk_accepted_displays() == []


def test_junk_keep_survives_delete_and_remint(svc):
    """Keep verdict, entity churn, same junk-shaped name re-minted with zero
    structure: the deep dream must neither re-file nor auto-delete it."""
    st = svc._storage
    e = st.ensure_entity("ok", display="ok")
    pid = st.insert_entity_proposal("junk", e, None, None, "too-short", time.time())
    assert svc.graph_reject_entity_proposal(pid, decided_by="human")["rejected"]
    assert st.delete_entity(e)
    e2 = st.ensure_entity("ok", display="ok")
    out = svc.deep_dream(apply=True)
    assert out["junk_proposed"] == 0 and out["junk_deleted"] == 0
    assert st.find_entity("ok") is not None
    assert not any(p["entity_id"] == e2 for p in st.pending_entity_proposals())
    # control: a never-judged sibling of the same shape is still handled
    st.ensure_entity("no", display="no")
    out = svc.deep_dream(apply=True)
    assert out["junk_proposed"] + out["junk_deleted"] >= 1
    assert st.find_entity("ok") is not None


def test_dry_run_listing_omits_kept_junk(svc):
    st = svc._storage
    e = st.ensure_entity("ok", display="ok")
    pid = st.insert_entity_proposal("junk", e, None, None, "too-short", time.time())
    svc.graph_reject_entity_proposal(pid, decided_by="human")
    st.delete_entity(e)
    st.ensure_entity("ok", display="ok")
    st.ensure_entity("no", display="no")                 # never-judged control
    listed = {j["entity"] for j in svc.deep_dream(apply=False)["would_junk"]}
    assert "no" in listed and "ok" not in listed
