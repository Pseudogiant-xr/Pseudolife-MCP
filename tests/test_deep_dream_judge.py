"""Autonomous Step-C judge (2026-08-16 design): the sweep shadow-judges
pending merge proposals with the configured model; auto-apply is gated by
``deep_dream.judge_mode`` and confidence. Contracts:

* shadow mode records verdicts on the rows and applies NOTHING;
* auto-reject mode applies only reject verdicts at/above the confidence
  floor (``decided_by='dream-judge'`` in merge_decisions, pair dismissed);
  accept verdicts are never applied by the judge at any mode;
* already-judged proposals are not re-sent; a judge failure never raises.

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


class _StubJudge:
    """Fixed verdict per (from, into) display pair; records what it saw."""

    model = "stub-judge"

    def __init__(self, verdicts):
        self._verdicts = verdicts       # {(from, into): (verdict, conf)}
        self.seen: list[tuple[str, str]] = []

    def judge_merges(self, proposals):
        out = []
        for p in proposals:
            key = (p["from"]["display"], p["into"]["display"])
            self.seen.append(key)
            v = self._verdicts.get(key)
            if v is not None:
                out.append({"n": p["n"], "verdict": v[0],
                            "confidence": v[1], "note": "stub"})
        return out


def _propose(svc, frm, into):
    import time
    st = svc._storage
    st.ensure_entity(frm, display=frm)
    st.ensure_entity(into, display=into)
    a = st.find_entity(frm)["id"]
    b = st.find_entity(into)["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())
    assert pid is not None
    return pid


def _row(svc, pid):
    return next((p for p in svc._storage.pending_entity_proposals()
                 if p["id"] == pid), None)


# ── the storage-level gate under the sweep (schema v30) ──────────────────

def test_judgment_round_trips_and_gates_on_pending(pg_url):  # noqa: F811
    """The verdict is an OPINION recorded on a PENDING row. Once a decision
    path ratifies the row, the verdict freezes with it — a later judge call
    must be refused rather than rewriting the history of a decided merge."""
    import time

    from pseudolife_memory.storage.postgres import PostgresStorage

    st = PostgresStorage(pg_url)
    st.ensure_entity("alpha", display="alpha")
    st.ensure_entity("alpha service", display="alpha service")
    a = st.find_entity("alpha")["id"]
    b = st.find_entity("alpha service")["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.9, "test", time.time())
    assert st.set_entity_proposal_judgment(
        pid, verdict="reject", confidence=0.9, note="siblings",
        model="stub", at=time.time())
    row = next(p for p in st.pending_entity_proposals() if p["id"] == pid)
    assert row["judge_verdict"] == "reject"
    assert row["judge_confidence"] == 0.9
    assert row["judge_note"] == "siblings"
    # A decided row can no longer be re-judged (the verdict froze with it).
    st.set_entity_proposal_status(pid, "rejected")
    assert not st.set_entity_proposal_judgment(
        pid, verdict="accept", confidence=0.5, note=None, model="stub",
        at=time.time())


# ── judge modes ──────────────────────────────────────────────────────────

def test_shadow_mode_records_and_applies_nothing(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "alpha svc", "alpha service")
    judge = _StubJudge({("alpha svc", "alpha service"): ("reject", 0.95)})
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 1 and out.get("auto_rejected", 0) == 0
    row = _row(svc, pid)
    assert row is not None and row["status"] == "pending"    # still queued
    assert row["judge_verdict"] == "reject"
    assert row["judge_model"] == "stub-judge"


def test_auto_reject_applies_only_confident_rejects(svc):
    svc.config.memory.deep_dream.judge_mode = "auto-reject"
    svc.config.memory.deep_dream.judge_reject_min_confidence = 0.8
    hi = _propose(svc, "beta svc", "beta harness")
    lo = _propose(svc, "gamma svc", "gamma harness")
    acc = _propose(svc, "delta svc", "delta service")
    judge = _StubJudge({
        ("beta svc", "beta harness"): ("reject", 0.9),      # applies
        ("gamma svc", "gamma harness"): ("reject", 0.5),    # below floor
        ("delta svc", "delta service"): ("accept", 0.99),   # never applied
    })
    out = svc.deep_dream_judge(judge)
    assert out["judged"] == 3 and out["auto_rejected"] == 1
    assert _row(svc, hi) is None                             # rejected, gone
    assert _row(svc, lo)["status"] == "pending"
    assert _row(svc, acc)["status"] == "pending"             # accept = opinion
    # The applied reject is a durable dream-judge decision + dismissed pair.
    decisions = svc._storage.recent_entity_decisions(limit=10)
    assert any(d["decided_by"] == "dream-judge"
               and d["status"] == "rejected" for d in decisions)
    assert ("beta harness", "beta svc") in {
        tuple(sorted(p)) for p in svc._storage.dismissed_pairs()}


def test_judged_rows_are_not_resent(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    _propose(svc, "eps svc", "eps service")
    judge = _StubJudge({("eps svc", "eps service"): ("leave", 0.4)})
    assert svc.deep_dream_judge(judge)["judged"] == 1
    again = _StubJudge({})
    assert svc.deep_dream_judge(again)["judged"] == 0
    assert again.seen == []                                  # nothing re-sent


def test_skipped_rows_become_zero_confidence_leaves(svc):
    # A model that returns no verdict for a row must not cause that row to
    # be re-sent every sweep (queue-head starvation): it is recorded as an
    # explicit abstain instead.
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "iota svc", "iota service")
    judge = _StubJudge({})                       # returns nothing for the row
    assert svc.deep_dream_judge(judge)["judged"] == 1
    row = _row(svc, pid)
    assert row["judge_verdict"] == "leave"
    assert row["judge_confidence"] == 0.0
    again = _StubJudge({})
    assert svc.deep_dream_judge(again)["judged"] == 0
    assert again.seen == []                      # not re-sent


def test_judge_failure_never_raises(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    _propose(svc, "zeta svc", "zeta service")

    class _Boom:
        model = "boom"

        def judge_merges(self, proposals):
            raise RuntimeError("endpoint down")

    out = svc.deep_dream_judge(_Boom())
    assert out["judged"] == 0 and "error" in out


def test_off_mode_is_inert(svc):
    svc.config.memory.deep_dream.judge_mode = "off"
    _propose(svc, "eta svc", "eta service")
    judge = _StubJudge({("eta svc", "eta service"): ("reject", 0.99)})
    assert svc.deep_dream_judge(judge) == {"judged": 0, "skipped": "disabled"}
    assert judge.seen == []


def test_merge_candidates_listing_carries_the_shadow_verdict():
    # The Console's review payload is built by graph_review.merge_candidates
    # — the judge block must survive it, or the human reviewer never sees
    # the pre-judgment (found post-deploy on 2026-08-17: the verdict lived
    # only in the deep response).
    from pseudolife_memory.memory.graph_review import merge_candidates

    rows = [{"id": 7, "kind": "merge", "entity_id": 1, "into_id": 2,
             "entity": "alpha svc", "into": "alpha service", "score": 0.9,
             "reason": "write-dedup", "judge_verdict": "reject",
             "judge_confidence": 0.92, "judge_note": "siblings",
             "judge_model": "claude-opus-5"},
            {"id": 8, "kind": "merge", "entity_id": 3, "into_id": 4,
             "entity": "beta", "into": "beta svc", "score": 0.9,
             "reason": "write-dedup"}]
    merges = merge_candidates(rows)[0]["merges"]
    judged = next(m for m in merges if m["id"] == 7)
    assert judged["judge"] == {"verdict": "reject", "confidence": 0.92,
                               "note": "siblings", "model": "claude-opus-5"}
    assert "judge" not in next(m for m in merges if m["id"] == 8)


def test_review_payload_carries_the_shadow_verdict(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "theta svc", "theta service")
    judge = _StubJudge({("theta svc", "theta service"): ("reject", 0.85)})
    svc.deep_dream_judge(judge)
    deep = svc.deep_dream(apply=False)
    row = next(p for p in deep["merge_proposals"] if p["id"] == pid)
    assert row["judge"]["verdict"] == "reject"
    assert row["judge"]["model"] == "stub-judge"


# ── evidence-quality signal in the judge payload (2026-08-21 shadow) ─────

def test_format_judge_proposal_marks_low_differential():
    from pseudolife_memory.memory.dream import format_judge_proposal

    base = {"n": 1, "reason": "token-subset", "score": 0.9,
            "from": {"display": "a", "degree": 0, "scopes": [],
                     "snippets": ["shared evidence line"]},
            "into": {"display": "a svc", "degree": 1, "scopes": [],
                     "snippets": ["shared evidence line"]}}
    plain = format_judge_proposal(dict(base))
    flagged = format_judge_proposal({**base, "low_differential": True})
    assert "low-differential" in flagged.lower()
    # Absent key serializes exactly as before — the frozen ladder fixtures
    # (and every published judge number) keep their byte-identical prompts.
    assert "low-differential" not in plain.lower()
    assert flagged != plain


def test_judge_payload_carries_low_differential_flag(svc):
    class _RecordingJudge:
        model = "stub-judge"

        def __init__(self):
            self.proposals = []

        def judge_merges(self, proposals):
            self.proposals = proposals
            return [{"n": p["n"], "verdict": "leave", "confidence": 0.1,
                     "note": "stub"} for p in proposals]

    # Only shared evidence exists for this pair -> the flag must reach the
    # judge payload so the prompt can carry the caution line.
    assert svc.store("beta gadget service exports the metrics feed",
                     source="sq")["stored"]
    assert svc.store("the beta gadget service restarts after deploys",
                     source="sq")["stored"]
    _propose(svc, "beta gadget", "beta gadget service")
    svc.config.memory.deep_dream.judge_mode = "shadow"
    judge = _RecordingJudge()
    out = svc.deep_dream_judge(extractor=judge)
    assert out["judged"] == 1
    assert judge.proposals[0]["low_differential"] is True
