"""The judge-agreement report: how often each recorded judge vote agreed
with the decision a human or agent later took on the same row.

A judge mode left in ``shadow`` records votes and applies nothing, and
nothing scored those votes against the decisions that followed, so the
promotion to ``auto`` had no number to rest on (2026-10-17: both v56
modes shipped in shadow with no path to leave it). An accepted merge
CASCADE-deletes its proposal row with the votes on it, so the votes are
snapshotted into ``merge_decisions.votes`` (schema v56) at decision time;
link rows survive their decision and are scored in place.
"""

from __future__ import annotations

import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.graph_review import relate_note
from pseudolife_memory.memory.judge_agreement import (
    compute_judge_agreement, proposal_votes)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    with s._lock:
        s._ensure_init()
    yield s
    s.flush()


# ── pure: the vote snapshot ───────────────────────────────────────────────

def test_proposal_votes_reads_each_recorded_opinion_and_its_relation():
    row = {
        "judge_verdict": "relate", "judge_confidence": 0.8, "judge_model": "m1",
        "judge_note": relate_note("part-of", "thin") + " | 2nd (m2): relate:uses 0.70 [split]"
                      " | 3rd (m3): accept 0.90 [no majority]",
        "judge2_verdict": "relate", "judge2_confidence": 0.7, "judge2_model": "m2",
        "judge2_served_model": "m2-served",
        "judge3_verdict": "accept", "judge3_confidence": 0.9, "judge3_model": "m3",
        "judge3_served_model": None,
    }
    votes = proposal_votes(row)
    assert votes == {
        "judge": {"verdict": "relate", "confidence": 0.8, "model": "m1",
                  "relation": "part-of"},
        "judge2": {"verdict": "relate", "confidence": 0.7, "model": "m2",
                   "served_model": "m2-served", "relation": "uses"},
        "judge3": {"verdict": "accept", "confidence": 0.9, "model": "m3",
                   "relation": None},
    }


def test_proposal_votes_is_none_when_no_opinion_was_recorded():
    assert proposal_votes({"judge_verdict": None, "judge2_verdict": None,
                           "judge3_verdict": None}) is None
    # A first opinion alone is a snapshot of one.
    one = proposal_votes({"judge_verdict": "accept", "judge_confidence": 0.9,
                          "judge_model": "m1", "judge_note": "same file"})
    assert list(one) == ["judge"]


# ── pure: the report ──────────────────────────────────────────────────────

def _merge(status, decided_by, votes, into="B"):
    return {"status": status, "decided_by": decided_by,
            "into_display": into, "votes": votes}


def _v(verdict, conf, model, relation=None, served=None):
    out = {"verdict": verdict, "confidence": conf, "model": model,
           "relation": relation}
    if served:
        out["served_model"] = served
    return out


def test_report_scores_each_merge_opinion_against_the_decision():
    rows = [
        # Both opinions said accept, a human accepted: both right.
        _merge("accepted", "human", {"judge": _v("accept", 0.9, "m1"),
                                     "judge2": _v("accept", 0.8, "m2")}),
        # First said relate (reject-class), an agent rejected: right;
        # second abstained.
        _merge("rejected", "agent", {"judge": _v("relate", 0.8, "m1", "uses"),
                                     "judge2": _v("leave", 0.3, "m2")}),
        # First said accept, a human rejected: wrong.
        _merge("rejected", "human", {"judge": _v("accept", 0.7, "m1")}),
        # The judge's own application is not a label.
        _merge("rejected", "dream-judge", {"judge": _v("reject", 0.95, "m1")}),
        _merge("accepted", "dream-auto", {"judge": _v("accept", 0.95, "m1")}),
    ]
    out = compute_judge_agreement(rows, [], accept_gate=0.6, reject_gate=0.7,
                                  link_gate=0.7, window_days=90)
    assert out["window_days"] == 90
    assert out["scored"] == {"merge": 3, "junk": 0, "link": 0}
    assert out["labels"] == {"human": 2, "agent": 1}
    j1 = out["merge"]["judge"]
    assert j1["voted"] == 3 and j1["agree"] == 2 and j1["abstain"] == 0
    assert j1["precision"] == pytest.approx(2 / 3, abs=0.001)
    assert j1["by_verdict"] == {"accept": {"n": 2, "agree": 1},
                                "reject": {"n": 1, "agree": 1}}
    j2 = out["merge"]["judge2"]
    assert j2["voted"] == 1 and j2["agree"] == 1 and j2["abstain"] == 1
    j3 = out["merge"]["judge3"]
    assert j3["voted"] == 0 and j3["precision"] is None


def test_report_scores_two_agreeing_votes_and_a_three_vote_majority():
    rows = [
        # Agreeing accepts from distinct models at mean 0.85 >= 0.6: scored,
        # and the human agreed.
        _merge("accepted", "human", {"judge": _v("accept", 0.9, "m1"),
                                     "judge2": _v("accept", 0.8, "m2")}),
        # Agreeing accepts below the gate: not an agreed pair.
        _merge("accepted", "human", {"judge": _v("accept", 0.5, "m1"),
                                     "judge2": _v("accept", 0.6, "m2")}),
        # Agreeing rejects from the SAME served model: one opinion twice,
        # never an agreed pair.
        _merge("rejected", "human", {"judge": _v("reject", 0.9, "m1"),
                                     "judge2": _v("reject", 0.9, "m2", served="m1")}),
        # Agreeing reject-class (reject + relate) from distinct models at
        # mean 0.8: scored; the human accepted, so the pair was wrong.
        _merge("accepted", "human", {"judge": _v("reject", 0.8, "m1"),
                                     "judge2": _v("relate", 0.8, "m2", "uses")}),
        # Three votes, 2-1 reject at mean 0.75 >= 0.7: scored, and right.
        _merge("rejected", "agent", {"judge": _v("accept", 0.9, "m1"),
                                     "judge2": _v("reject", 0.7, "m2"),
                                     "judge3": _v("relate", 0.8, "m3", "part-of")}),
        # Three votes, 2-1 accept at mean 0.65 >= 0.6: scored, and wrong.
        _merge("rejected", "human", {"judge": _v("accept", 0.6, "m1"),
                                     "judge2": _v("reject", 0.9, "m2"),
                                     "judge3": _v("accept", 0.7, "m3")}),
        # Three votes, 2-1 reject below the gate: no majority scored.
        _merge("rejected", "human", {"judge": _v("accept", 0.9, "m1"),
                                     "judge2": _v("reject", 0.5, "m2"),
                                     "judge3": _v("reject", 0.6, "m3")}),
    ]
    out = compute_judge_agreement(rows, [], accept_gate=0.6, reject_gate=0.7,
                                  link_gate=0.7, window_days=30)
    two = out["merge"]["agreed_two"]
    assert two["accept"] == {"n": 1, "agree": 1, "precision": 1.0}
    assert two["reject"] == {"n": 1, "agree": 0, "precision": 0.0}
    three = out["merge"]["majority_three"]
    assert three["reject"] == {"n": 1, "agree": 1, "precision": 1.0}
    assert three["accept"] == {"n": 1, "agree": 0, "precision": 0.0}


def test_report_scores_junk_and_link_decisions():
    junk = [
        _merge("accepted", "human", {"judge": _v("delete", 0.9, "m1")}, into=None),
        _merge("rejected", "human", {"judge": _v("keep", 0.9, "m1")}, into=None),
        _merge("rejected", "agent", {"judge": _v("delete", 0.9, "m1")}, into=None),
    ]
    links = [
        {"status": "accepted", "decided_by": "human",
         "judge_verdict": "accept", "judge_confidence": 0.7, "judge_model": "m1",
         "judge2_verdict": "accept", "judge2_confidence": 0.8, "judge2_model": "m2",
         "judge2_served_model": "m2"},
        {"status": "retyped", "decided_by": "human",
         "judge_verdict": "retype", "judge_confidence": 0.9, "judge_model": "m1",
         "judge2_verdict": None},
        {"status": "rejected", "decided_by": "agent",
         "judge_verdict": "accept", "judge_confidence": 0.9, "judge_model": "m1",
         "judge2_verdict": "reject", "judge2_confidence": 0.9, "judge2_model": "m2",
         "judge2_served_model": "m2"},
        {"status": "accepted", "decided_by": "dream-judge",
         "judge_verdict": "accept", "judge_confidence": 0.9, "judge_model": "m1",
         "judge2_verdict": None},
    ]
    out = compute_judge_agreement(junk, links, accept_gate=0.6, reject_gate=0.7,
                                  link_gate=0.7, window_days=90)
    assert out["scored"] == {"merge": 0, "junk": 3, "link": 3}
    jj = out["junk"]["judge"]
    assert jj["voted"] == 3 and jj["agree"] == 2
    assert jj["by_verdict"] == {"delete": {"n": 2, "agree": 1},
                                "keep": {"n": 1, "agree": 1}}
    lj = out["link"]["judge"]
    assert lj["voted"] == 3 and lj["agree"] == 2
    assert lj["by_verdict"] == {"accept": {"n": 2, "agree": 1},
                                "retype": {"n": 1, "agree": 1}}
    lj2 = out["link"]["judge2"]
    assert lj2["voted"] == 2 and lj2["agree"] == 2
    # One agreed accept pair at mean 0.75 >= 0.7, and the human agreed; the
    # split row is no pair.
    assert out["link"]["agreed_two"] == {
        "accept": {"n": 1, "agree": 1, "precision": 1.0},
        "reject": {"n": 0, "agree": 0, "precision": None}}


# ── PG: the snapshot at decision time, and the status block ──────────────

def _two(svc, a, b):
    svc.graph_relate(a, "related-to", f"{a}-anchor", origin="agent")
    svc.graph_relate(b, "related-to", f"{b}-anchor", origin="agent")
    ids = {e["canonical"]: e["id"] for e in svc._storage.load_graph()["entities"]}
    return ids[a], ids[b]


def _votes_for(svc, proposal_id):
    row = svc._storage.conn.execute(
        "SELECT votes FROM merge_decisions WHERE proposal_id = %s",
        (proposal_id,)).fetchone()
    return row[0] if row else "no audit row"


def test_accepting_a_merge_snapshots_its_votes_into_the_audit(svc):
    st = svc._storage
    frm, into = _two(svc, "snap-a", "snap-b")
    now = time.time()
    pid = st.insert_entity_proposal("merge", frm, into, 0.9, "write-dedup", now)
    assert st.set_entity_proposal_judgment(
        pid, verdict="accept", confidence=0.85, note="same file", model="m1", at=now)
    assert st.set_entity_proposal_second_judgment(
        pid, verdict="accept", confidence=0.75, model="m2", at=now,
        served_model="m2-served")
    out = svc.graph_accept_entity_merge(pid, decided_by="human")
    assert out["accepted"] is True
    # The proposal row is gone with the folded entity; the audit kept the votes.
    assert st.get_entity_proposal(pid) is None
    assert _votes_for(svc, pid) == {
        "judge": {"verdict": "accept", "confidence": 0.85, "model": "m1",
                  "relation": None},
        "judge2": {"verdict": "accept", "confidence": 0.75, "model": "m2",
                   "served_model": "m2-served", "relation": None}}


def test_rejecting_a_merge_and_keeping_a_junk_entity_snapshot_their_votes(svc):
    st = svc._storage
    a, b = _two(svc, "rej-a", "rej-b")
    now = time.time()
    merge = st.insert_entity_proposal("merge", a, b, 0.9, "write-dedup", now)
    assert st.set_entity_proposal_judgment(
        merge, verdict="relate", confidence=0.8,
        note=relate_note("uses", "distinct tools"), model="m1", at=now)
    assert svc.graph_reject_entity_proposal(merge, decided_by="agent")["rejected"]
    assert _votes_for(svc, merge) == {
        "judge": {"verdict": "relate", "confidence": 0.8, "model": "m1",
                  "relation": "uses"}}
    junk = st.insert_entity_proposal("junk", a, None, None, "status-word", now)
    assert st.set_entity_proposal_judgment(
        junk, verdict="keep", confidence=0.9, note="a real tool", model="m1", at=now)
    assert svc.graph_reject_entity_proposal(junk, decided_by="human")["rejected"]
    assert _votes_for(svc, junk) == {
        "judge": {"verdict": "keep", "confidence": 0.9, "model": "m1",
                  "relation": None}}


def test_a_decision_with_no_recorded_vote_leaves_the_audit_votes_null(svc):
    st = svc._storage
    frm, into = _two(svc, "novote-a", "novote-b")
    pid = st.insert_entity_proposal("merge", frm, into, 0.9, "write-dedup", time.time())
    assert svc.graph_accept_entity_merge(pid, decided_by="human")["accepted"]
    assert _votes_for(svc, pid) is None


def test_dream_status_carries_the_agreement_report(svc):
    st = svc._storage
    now = time.time()
    frm, into = _two(svc, "st-a", "st-b")
    pid = st.insert_entity_proposal("merge", frm, into, 0.9, "write-dedup", now)
    st.set_entity_proposal_judgment(pid, verdict="accept", confidence=0.9,
                                    note="same", model="m1", at=now)
    svc.graph_accept_entity_merge(pid, decided_by="human")
    c, d = _two(svc, "st-c", "st-d")
    pid2 = st.insert_entity_proposal("merge", c, d, 0.9, "write-dedup", now)
    st.set_entity_proposal_judgment(pid2, verdict="accept", confidence=0.9,
                                    note="same", model="m1", at=now)
    svc.graph_reject_entity_proposal(pid2, decided_by="agent")
    # The judge's own application is recorded but is not a label.
    e, f = _two(svc, "st-e", "st-f")
    pid3 = st.insert_entity_proposal("merge", e, f, 0.9, "write-dedup", now)
    st.set_entity_proposal_judgment(pid3, verdict="reject", confidence=0.95,
                                    note="siblings", model="m1", at=now)
    svc.graph_reject_entity_proposal(pid3, decided_by="dream-judge")
    # A link decided in place is scored from its row.
    g, h = _two(svc, "st-g", "st-h")
    lid = st.insert_proposal(g, "uses", h, 0.6, None, None, "deep-dream", now)
    st.set_proposal_judgment(lid, verdict="accept", confidence=0.7, note=None,
                             model="m1", relation=None, at=now)
    st.set_proposal_status(lid, "accepted", decided_by="human", decided_at=now)

    rq = svc.dream_status()["review_queue"]
    rep = rq["judge_agreement"]
    assert rep["scored"] == {"merge": 2, "junk": 0, "link": 1}
    assert rep["labels"] == {"human": 2, "agent": 1}
    assert rep["merge"]["judge"]["voted"] == 2
    assert rep["merge"]["judge"]["agree"] == 1
    assert rep["link"]["judge"] == {
        "voted": 1, "agree": 1, "abstain": 0, "precision": 1.0,
        "by_verdict": {"accept": {"n": 1, "agree": 1}}}
    # Pre-v56 audit rows (no votes) and out-of-window rows are not scored.
    st.conn.execute("UPDATE merge_decisions SET decided_at = decided_at - %s",
                    (400 * 86400.0,))
    st.conn.commit()
    assert svc.judge_agreement()["scored"]["merge"] == 0
    assert svc.judge_agreement(days=500)["scored"]["merge"] == 2


def test_the_report_never_fails_the_status_call(svc, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("audit table unreadable")
    monkeypatch.setattr(svc._storage, "judge_vote_decisions", boom)
    rq = svc.review_queue_health()
    assert rq["pending"]["merge"] == 0
    assert rq["judge_agreement"]["error"].startswith("RuntimeError")
