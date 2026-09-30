"""Review-queue autonomy (2026-09-02 design): the sweep-side judge stages
that reach the queues the v30 merge judge left for humans, plus the two
mechanical additions that stop them refilling. Contracts:

* every stage records its verdict on the row (or in the curation memo) and
  applies nothing in ``shadow``;
* ``auto`` applies only verdicts at/above the stage's confidence gate, and
  only where a wrong verdict is cheap or reversible — junk deletes stay
  behind an evidence bar, merge accepts need two independent votes on
  non-low-differential evidence;
* a judge failure or a skipped row never raises into the sweep and never
  starves the queue (skipped rows are stamped ``leave`` at 0 confidence).

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


# ── stubs ─────────────────────────────────────────────────────────────────

class _LinkJudge:
    model = "stub-link-judge"

    def __init__(self, verdicts):
        self._v = verdicts            # {(src, relation, dst): (verdict, conf, relation|None)}
        self.seen = []

    def judge_links(self, rows):
        out = []
        for r in rows:
            key = (r["src"], r["relation"], r["dst"])
            self.seen.append(key)
            v = self._v.get(key)
            if v:
                out.append({"n": r["n"], "verdict": v[0], "confidence": v[1],
                            "note": "stub", "relation": v[2]})
        return out


class _JunkJudge:
    model = "stub-junk-judge"

    def __init__(self, verdicts):
        self._v = verdicts            # {display: (verdict, conf)}
        self.seen = []

    def judge_junk(self, rows):
        out = []
        for r in rows:
            self.seen.append(r)
            v = self._v.get(r["display"])
            if v:
                out.append({"n": r["n"], "verdict": v[0], "confidence": v[1],
                            "note": "stub"})
        return out


class _SlotJudge:
    model = "stub-slot-judge"
    served_model = "stub-slot-judge"

    def __init__(self, verdicts):
        self._v = verdicts            # {frozenset(a_key,b_key): (verdict, keep, fold, conf)}
        self.seen = []

    def judge_slot_pairs(self, rows):
        out = []
        for r in rows:
            self.seen.append((r["a_key"], r["b_key"]))
            v = self._v.get(frozenset((r["a_key"], r["b_key"])))
            if v:
                out.append({"n": r["n"], "verdict": v[0], "keep": v[1],
                            "fold": v[2], "confidence": v[3], "note": "stub"})
        return out


class _MergeJudge:
    """Merge stub whose verdict map changes per CALL — first opinion, then
    second opinion — so two-vote agreement can be scripted."""

    model = "stub-merge-judge"
    served_model = "stub-merge-judge"

    def __init__(self, *rounds):
        # [{(from, into): (verdict, conf[, relation])}, ...]
        self._rounds = list(rounds)
        self.calls = 0

    def judge_merges(self, proposals):
        verdicts = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        out = []
        for p in proposals:
            v = verdicts.get((p["from"]["display"], p["into"]["display"]))
            if v:
                out.append({"n": p["n"], "verdict": v[0], "confidence": v[1],
                            "note": "stub",
                            "relation": v[2] if len(v) > 2 else None})
        return out


class _CandidateJudge:
    served_model = "stub-candidate-judge"
    model = "stub-candidate-judge"

    def __init__(self, verdicts):
        self._v = verdicts            # {(src, dst): (verdict, conf, relation, src, dst)}

    def judge_candidates(self, rows):
        out = []
        for r in rows:
            v = self._v.get((r["src"], r["dst"]))
            if v:
                out.append({"n": r["n"], "verdict": v[0], "confidence": v[1],
                            "relation": v[2], "src": v[3], "dst": v[4],
                            "rationale": "stub"})
        return out


def _link(svc, src, relation, dst):
    assert svc.graph_propose_links([{"src": src, "relation": relation,
                                     "dst": dst, "rationale": "t"}])["proposed"] == 1
    return next(p for p in svc._storage.pending_proposals()
                if p["src"] == src and p["dst"] == dst and p["relation"] == relation)["id"]


def _pending_link(svc, pid):
    return next((p for p in svc._storage.pending_proposals() if p["id"] == pid), None)


def _live_edges(svc):
    g = svc._storage.load_graph()
    disp = {e["id"]: e["display"] for e in g["entities"]}
    return {(disp[e["src_id"]], e["relation"], disp[e["dst_id"]], e["origin"])
            for e in g["edges"]}


# ── link judge ────────────────────────────────────────────────────────────

def test_link_judge_shadow_records_and_applies_nothing(svc):
    svc.config.memory.deep_dream.link_judge_mode = "shadow"
    pid = _link(svc, "tests/test_x.py", "uses", "x-feature")
    judge = _LinkJudge({("tests/test_x.py", "uses", "x-feature"): ("accept", 0.95, None)})
    out = svc.deep_dream_judge_links(judge)
    assert out["judged"] == 1 and out.get("applied", 0) == 0
    row = _pending_link(svc, pid)
    assert row is not None and row["judge_verdict"] == "accept"
    assert row["judge_model"] == "stub-link-judge"
    assert ("tests/test_x.py", "uses", "x-feature", "action") not in _live_edges(svc)


def test_link_judge_auto_applies_accept_reject_retype_at_gate(svc):
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = "auto"
    cfg.link_accept_min_confidence = 0.8
    cfg.link_reject_min_confidence = 0.8
    acc = _link(svc, "a-tool", "uses", "b-lib")
    rej = _link(svc, "c-thing", "part-of", "d-thing")
    ret = _link(svc, "e-file.py", "runs-on", "e-concept")
    low = _link(svc, "f-a", "uses", "f-b")
    lv = _link(svc, "g-a", "uses", "g-b")
    judge = _LinkJudge({
        ("a-tool", "uses", "b-lib"): ("accept", 0.9, None),
        ("c-thing", "part-of", "d-thing"): ("reject", 0.85, None),
        ("e-file.py", "runs-on", "e-concept"): ("retype", 0.9, "implements"),
        ("f-a", "uses", "f-b"): ("accept", 0.6, None),          # below gate
        ("g-a", "uses", "g-b"): ("leave", 0.5, None),
    })
    out = svc.deep_dream_judge_links(judge)
    assert out["judged"] == 5 and out["applied"] == 2
    edges = _live_edges(svc)
    assert ("a-tool", "uses", "b-lib", "action") in edges
    # A retype is recorded (verdict + corrected relation), never auto-written.
    assert not any(e[0] == "e-file.py" for e in edges)
    ret_row = _pending_link(svc, ret)
    assert ret_row["judge_verdict"] == "retype" and ret_row["judge_relation"] == "implements"
    assert _pending_link(svc, acc) is None and _pending_link(svc, rej) is None
    st = svc._storage
    assert st.get_proposal(rej)["status"] == "rejected"
    assert st.get_proposal(acc)["decided_by"] == "dream-judge"
    # The reviewer's own retype path still works, gated like propose.
    res = svc.graph_accept_proposal(ret, decided_by="agent", relation="implements")
    assert res["accepted"] and res["status"] == "retyped"
    assert ("e-file.py", "implements", "e-concept", "action") in _live_edges(svc)
    # below-gate and leave rows stay pending with the opinion attached
    assert _pending_link(svc, low)["judge_verdict"] == "accept"
    assert _pending_link(svc, lv)["judge_verdict"] == "leave"


def test_link_judge_retype_cannot_bypass_the_type_gate(svc):
    """A retype is an unattended write path; it must be no looser than
    graph_propose_links — no lesson relations, no hard type violations."""
    cfg = svc.config.memory.deep_dream
    cfg.link_judge_mode = "auto"
    pid = _link(svc, "user", "related-to", "windows 11")
    judge = _LinkJudge({("user", "related-to", "windows 11"): ("retype", 0.95, "runs-on")})
    out = svc.deep_dream_judge_links(judge)
    assert out["judged"] == 1 and out["applied"] == 0
    row = _pending_link(svc, pid)
    assert row is not None and row["judge_verdict"] == "retype"
    assert not any(e[1] == "runs-on" for e in _live_edges(svc))
    res = svc.graph_accept_proposal(pid, decided_by="dream-judge", relation="prefers")
    assert res["accepted"] is False and res["reason"] == "unknown_relation"


def test_link_judge_skips_already_judged_and_stamps_skipped_rows(svc):
    svc.config.memory.deep_dream.link_judge_mode = "shadow"
    a = _link(svc, "h-a", "uses", "h-b")
    b = _link(svc, "i-a", "uses", "i-b")
    judge = _LinkJudge({("h-a", "uses", "h-b"): ("accept", 0.9, None)})
    svc.deep_dream_judge_links(judge)
    assert _pending_link(svc, b)["judge_verdict"] == "leave"      # model skipped it
    assert _pending_link(svc, b)["judge_confidence"] == 0.0
    judge2 = _LinkJudge({})
    out = svc.deep_dream_judge_links(judge2)
    assert out["judged"] == 0 and judge2.seen == []               # nothing re-sent
    assert _pending_link(svc, a)["judge_verdict"] == "accept"


def test_link_judge_failure_marks_nothing(svc):
    svc.config.memory.deep_dream.link_judge_mode = "auto"
    pid = _link(svc, "j-a", "uses", "j-b")

    class _Boom:
        model = "boom"

        def judge_links(self, rows):
            raise RuntimeError("transport")

    out = svc.deep_dream_judge_links(_Boom())
    assert out["judged"] == 0 and "error" in out
    assert _pending_link(svc, pid)["judge_verdict"] is None


# ── junk judge ────────────────────────────────────────────────────────────

def _junk(svc, display, reason="list-artifact"):
    st = svc._storage
    from pseudolife_memory.graph import norm_name
    st.ensure_entity(norm_name(display), display=display)
    eid = st.find_entity(norm_name(display))["id"]
    pid = st.insert_entity_proposal("junk", eid, None, None, reason, time.time())
    assert pid is not None
    return pid, eid


def _junk_row(svc, pid):
    return next((p for p in svc._storage.pending_entity_proposals()
                 if p["id"] == pid), None)


def test_junk_judge_shadow_records_only(svc):
    svc.config.memory.deep_dream.junk_judge_mode = "shadow"
    pid, eid = _junk(svc, "evals/a.py, evals/b.py")
    judge = _JunkJudge({"evals/a.py, evals/b.py": ("delete", 0.95)})
    out = svc.deep_dream_judge_junk(judge)
    assert out["judged"] == 1 and out.get("applied", 0) == 0
    row = _junk_row(svc, pid)
    assert row["judge_verdict"] == "delete" and row["status"] == "pending"
    assert svc._storage.get_entity_proposal(pid) is not None
    # the evidence pack carried the lesson-object flag and the detector class
    seen = judge.seen[0]
    assert seen["reason"] == "list-artifact" and "lesson_object" in seen


def test_junk_judge_auto_keeps_and_deletes_under_evidence_bar(svc):
    cfg = svc.config.memory.deep_dream
    cfg.junk_judge_mode = "auto"
    cfg.junk_keep_min_confidence = 0.8
    cfg.junk_delete_min_confidence = 0.85
    cfg.junk_max_auto_degree = 3
    keep_pid, keep_eid = _junk(svc, "origin/master", "compound-artifact")
    del_pid, del_eid = _junk(svc, "evals/c.py, evals/d.py")
    svc.graph_relate("wire an eval arm", "prefers", "evals/c.py, evals/d.py",
                     origin="action")                        # lesson-object shape
    rich_pid, rich_eid = _junk(svc, "rich thing / other", "compound-artifact")
    for i in range(4):                                       # degree 4 > bar
        svc.graph_relate("rich thing / other", "uses", f"dep-{i}", origin="agent")
    judge = _JunkJudge({
        "origin/master": ("keep", 0.95),
        "evals/c.py, evals/d.py": ("delete", 0.9),
        "rich thing / other": ("delete", 0.99),
    })
    out = svc.deep_dream_judge_junk(judge)
    assert out["judged"] == 2 and out["applied"] == 2
    # A structural delete invalidates the remaining batch projection. The
    # next sweep evaluates that row against the changed graph.
    assert svc.deep_dream_judge_junk(judge)["judged"] == 1
    st = svc._storage
    assert st.get_entity_proposal(keep_pid)["status"] == "rejected"
    assert st.get_entity_proposal(keep_pid)["decided_by"] == "dream-judge"
    assert st.find_entity("origin-master") is not None
    assert st.get_entity_proposal(del_pid) is None            # CASCADEd with the entity
    assert st.find_entity("evals-c-py-evals-d-py") is None
    tomb = {r["entity"] for r in st.recent_entity_decisions()
            if r["decided_by"] == "dream-judge" and r["into"] is None}
    assert "evals/c.py, evals/d.py" in tomb
    # evidence-bearing: verdict recorded, node kept, row pending
    rich = _junk_row(svc, rich_pid)
    assert rich["status"] == "pending" and rich["judge_verdict"] == "delete"
    assert st.find_entity("rich-thing-other") is not None


# ── store-curation judge ──────────────────────────────────────────────────

_DUP = ("Always take a pg_dump backup via ops/backup.ps1 before deploying "
        "the daemon to the homelab host.")


def _stage_pair(svc):
    svc.lesson_write("deploy daemon to homelab host", "approach", _DUP)
    svc.lesson_write("deploy the daemon to the host", "pitfall", _DUP)
    return frozenset({"deploy-daemon-to-homelab-host|approach",
                      "deploy-the-daemon-to-the-host|pitfall"})


@pytest.mark.real_model
def test_curation_judge_distinct_dismisses_and_memoizes(svc):
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.8
    pair = _stage_pair(svc)
    judge = _SlotJudge({pair: ("distinct", None, None, 0.9)})
    out = svc.deep_dream_judge_curation(judge)
    assert out["judged"] == 1 and out["applied"] == 1
    assert svc.deep_dream(apply=False)["lesson_duplicates"] == []    # dismissed
    memo = svc._storage.curation_judgments("lesson")
    assert tuple(sorted(pair)) in memo and memo[tuple(sorted(pair))]["verdict"] == "distinct"
    assert len(svc._lessons.current_records()) == 2                  # nothing deleted


@pytest.mark.real_model
def test_curation_judge_duplicate_waits_in_auto_distinct_and_forgets_in_auto(svc):
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_forget_min_confidence = 0.9
    pair = _stage_pair(svc)
    judge = _SlotJudge({pair: ("duplicate", "a", "carry the backup step", 0.95)})
    out = svc.deep_dream_judge_curation(judge)
    assert out["judged"] == 1 and out["applied"] == 0
    assert len(svc._lessons.current_records()) == 2
    # memoised: the pair is not re-sent while the memo is fresh
    judge2 = _SlotJudge({pair: ("duplicate", "a", None, 0.95)})
    assert svc.deep_dream_judge_curation(judge2)["judged"] == 0
    assert judge2.seen == []
    # Auto mode never writes judge-authored prose. The invented fold is kept
    # as an opinion for review, while both original records remain live.
    cfg.curation_judge_mode = "auto"
    cfg.curation_rejudge_days = 0                                    # memo expired
    out = svc.deep_dream_judge_curation(judge)
    assert out["applied"] == 0
    assert len(svc._lessons.current_records()) == 2
    # With no invented fold, this exact-text class is safe to retire.
    out = svc.deep_dream_judge_curation(judge2)
    assert out["applied"] == 1
    recs = {r.key: r for r in svc._lessons.current_records()}
    keys = {"|".join(k) for k in recs}
    assert "deploy-daemon-to-homelab-host|approach" in keys
    assert "deploy-the-daemon-to-the-host|pitfall" not in keys
    survivor = recs[("deploy-daemon-to-homelab-host", "approach")]
    assert survivor.value == _DUP


# ── merge judge: second opinion + guarded auto-accept ─────────────────────

def _propose(svc, frm, into):
    st = svc._storage
    from pseudolife_memory.graph import norm_name
    st.ensure_entity(norm_name(frm), display=frm)
    st.ensure_entity(norm_name(into), display=into)
    a = st.find_entity(norm_name(frm))["id"]
    b = st.find_entity(norm_name(into))["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())
    assert pid is not None
    return pid


def _merge_row(svc, pid):
    return next((p for p in svc._storage.pending_entity_proposals()
                 if p["id"] == pid), None)


def test_second_opinion_two_vote_reject_applies_below_single_gate(svc):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_reject_min_confidence = 0.8
    cfg.judge_second_opinion = True
    cfg.judge_reject_min_confidence_2 = 0.7
    agree = _propose(svc, "alpha svc", "alpha harness")
    split = _propose(svc, "beta svc", "beta harness")
    judge = _MergeJudge(
        {("alpha svc", "alpha harness"): ("reject", 0.6),
         ("beta svc", "beta harness"): ("reject", 0.6)})
    # The two votes come from different models: since 2026-09-30 a
    # same-model pair never auto-rejects (see the refusal tests below).
    judge2 = _SecondJudge(
        {("alpha svc", "alpha harness"): ("reject", 0.85),
         ("beta svc", "beta harness"): ("accept", 0.7)})
    first = svc.deep_dream_judge(judge)
    assert first["judged"] == 2 and first["auto_rejected"] == 0     # both below 0.8
    assert _merge_row(svc, agree)["judge_verdict"] == "reject"
    second = svc.deep_dream_judge(judge, second_extractor=judge2)   # second opinion round
    assert second["second_opinions"] == 2 and second["auto_rejected"] == 1
    assert second["auto_reject_refused_same_model"] == 0
    assert _merge_row(svc, agree) is None
    assert svc._storage.get_entity_proposal(agree)["status"] == "rejected"
    assert svc._storage.get_entity_proposal(agree)["decided_by"] == "dream-judge"
    row = _merge_row(svc, split)
    assert row["status"] == "pending" and row["judge2_verdict"] == "accept"
    assert "split" in (row["judge_note"] or "")
    # a third call re-sends nothing: both opinions are on the row
    third = svc.deep_dream_judge(judge, second_extractor=judge2)
    assert third["judged"] == 0 and third.get("second_opinions", 0) == 0


class _SecondJudge(_MergeJudge):
    model = "stub-merge-judge-2"
    served_model = "stub-merge-judge-2"


def _two_reject_rounds(svc, pid_pair, second):
    """First opinion reject 0.6 (below the single-vote 0.8 gate), then a
    second opinion from ``second`` (None = the same extractor object)."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_reject_min_confidence = 0.8
    cfg.judge_second_opinion = True
    cfg.judge_reject_min_confidence_2 = 0.7
    judge = _MergeJudge({pid_pair: ("reject", 0.6)}, {pid_pair: ("reject", 0.9)})
    assert svc.deep_dream_judge(judge)["auto_rejected"] == 0
    return svc.deep_dream_judge(judge, second_extractor=second)


def test_two_vote_reject_needs_a_distinct_second_model(svc):
    """Two agreeing rejects from ONE model are one opinion asked twice. The
    accept gate has refused that since 2026-09-02; the reject gate did not,
    and on 2026-09-11 the maintainer put the merge judge back in shadow
    because same-model agreement (a substituted second model, see the next
    test) was authorizing rejects. The second vote is still recorded; it
    just authorizes nothing."""
    pid = _propose(svc, "lambda svc", "lambda harness")
    out = _two_reject_rounds(svc, ("lambda svc", "lambda harness"), None)
    assert out["second_opinions"] == 1
    assert out["auto_rejected"] == 0
    assert out["auto_reject_refused_same_model"] == 1
    row = _merge_row(svc, pid)
    assert row["status"] == "pending" and row["judge2_verdict"] == "reject"
    assert "auto-reject needs a distinct second model" in row["judge_note"]
    assert ("lambda harness", "lambda svc") not in {
        tuple(sorted(p)) for p in svc._storage.dismissed_pairs()}


class _SubstitutedJudge(_MergeJudge):
    """A second endpoint asked for another model that served the FIRST
    opinion's model instead: the Codex shim answers any non-gpt-*/codex-*
    name with its launch default, so from 2026-09-03 to 2026-09-11
    ``judge_second_model: claude-fable-5`` was answered by gpt-5.6-terra,
    the first opinion's model."""
    model = "claude-fable-5"
    served_model = "stub-merge-judge"          # == _MergeJudge.served_model


def test_two_vote_reject_refuses_a_substituted_second_model(svc, caplog):
    import logging

    pid = _propose(svc, "mu svc", "mu harness")
    with caplog.at_level(logging.WARNING):
        out = _two_reject_rounds(svc, ("mu svc", "mu harness"), _SubstitutedJudge({
            ("mu svc", "mu harness"): ("reject", 0.9)}))
    assert out["second_opinions"] == 1
    assert out["auto_rejected"] == 0 and out["auto_reject_refused_same_model"] == 1
    assert _merge_row(svc, pid)["status"] == "pending"
    # The substitution itself is visible, not only its consequence.
    assert out["served_model_mismatch"] == 1
    assert out["served_model_mismatches"] == [
        {"opinion": "second", "requested": "claude-fable-5",
         "served": "stub-merge-judge"}]
    assert any("claude-fable-5" in r.getMessage() and "stub-merge-judge" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


class _MislabelledJudge(_MergeJudge):
    model = "claude-opus-5"
    served_model = "gpt-5.6-terra"


def test_served_model_mismatch_is_reported_but_never_blocks_a_first_opinion(svc):
    svc.config.memory.deep_dream.judge_mode = "shadow"
    pid = _propose(svc, "nu svc", "nu harness")
    out = svc.deep_dream_judge(_MislabelledJudge({("nu svc", "nu harness"): ("leave", 0.5)}))
    assert out["judged"] == 1
    assert out["served_model_mismatch"] == 1
    assert out["served_model_mismatches"] == [
        {"opinion": "first", "requested": "claude-opus-5", "served": "gpt-5.6-terra"}]
    row = _merge_row(svc, pid)
    assert row["judge_verdict"] == "leave" and row["judge_model"] == "gpt-5.6-terra"


def test_served_model_match_rule():
    from pseudolife_memory.service_dream import DreamOps

    def ex(model, served):
        return type("Ex", (), {"model": model, "served_model": served})()

    mismatch = DreamOps._served_model_mismatch
    assert mismatch(ex("claude-fable-5", "gpt-5.6-terra")) == (
        "claude-fable-5", "gpt-5.6-terra")
    assert mismatch(ex("claude-opus-5", "claude-opus-5-5")) is not None   # another model
    assert mismatch(ex("claude-opus-5", "claude-opus-5")) is None
    assert mismatch(ex("gpt-4o", "gpt-4o-2024-08-06")) is None            # dated snapshot
    assert mismatch(ex("claude-opus-5", "claude-opus-5-20260901")) is None
    # Launch-default aliases serve whatever the endpoint was started with.
    for alias in ("judge", "extractor", "bench"):
        assert mismatch(ex(alias, "gpt-5.6-terra")) is None
    assert mismatch(ex("claude-opus-5", None)) is None                    # not reported
    assert mismatch(ex(None, "gpt-5.6-terra")) is None


def test_judge_second_url_builds_the_second_opinion_on_its_own_endpoint(svc, monkeypatch):
    cfg = svc.config.memory.deep_dream
    dream = svc.config.memory.dream
    monkeypatch.setenv("PSEUDOLIFE_JUDGE_SECOND_API_KEY", "sk-second")
    monkeypatch.delenv("PSEUDOLIFE_DREAM_API_KEY", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_JUDGE_API_KEY", raising=False)
    cfg.judge_url, cfg.judge_model = "http://127.0.0.1:8082/v1", "claude-opus-5-5"
    cfg.judge_second_url = "https://api.example.com/v1"
    cfg.judge_second_model = "gpt-5.6-terra"
    first, second = svc._judge_extractor(), svc._judge_second_extractor()
    assert (first.base_url, first.model, first.api_key) == (
        "http://127.0.0.1:8082/v1", "claude-opus-5-5", None)
    assert (second.base_url, second.model, second.api_key) == (
        "https://api.example.com/v1", "gpt-5.6-terra", "sk-second")
    assert second.max_tokens == dream.extractor_max_tokens
    assert second.timeout == dream.extractor_timeout_seconds
    # No second model named: the endpoint's launch default, like judge_url.
    cfg.judge_second_model = None
    assert svc._judge_second_extractor().model == "judge"
    # judge_second_url unset: today's behaviour — the second model on the
    # FIRST opinion's endpoint, and the second endpoint's key goes nowhere.
    cfg.judge_second_url = None
    cfg.judge_second_model = "claude-fable-5"
    same_host = svc._judge_second_extractor()
    assert (same_host.base_url, same_host.model, same_host.api_key) == (
        "http://127.0.0.1:8082/v1", "claude-fable-5", None)
    cfg.judge_second_model = ""
    assert svc._judge_second_extractor() is None       # reuse the first extractor


def test_the_sweep_judge_takes_its_second_opinion_from_the_second_endpoint(svc, monkeypatch):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_second_opinion = True
    cfg.judge_second_url = "https://api.example.com/v1"   # a distinct endpoint by config
    pair = ("xi svc", "xi harness")
    pid = _propose(svc, *pair)
    first = _MergeJudge({pair: ("reject", 0.6)})
    second = _SecondJudge({pair: ("reject", 0.9)})
    monkeypatch.setattr(svc, "_judge_extractor", lambda *a, **k: first)
    monkeypatch.setattr(svc, "_judge_second_extractor", lambda: second, raising=False)
    svc.deep_dream_judge()
    out = svc.deep_dream_judge()
    assert second.calls == 1 and first.calls == 1
    assert out["auto_rejected"] == 1
    assert svc._storage.get_entity_proposal(pid)["status"] == "rejected"


def test_judge_url_takes_its_own_env_key_and_no_other_endpoint_sees_it(svc, monkeypatch):
    """judge_url gets the same env-only key option as judge_second_url
    (maintainer decision 2026-09-30): PSEUDOLIFE_JUDGE_API_KEY goes to
    judge_url's endpoint, including the second model swapped onto it, and
    never to the second endpoint or the dream extractor."""
    cfg = svc.config.memory.deep_dream
    monkeypatch.setenv("PSEUDOLIFE_JUDGE_API_KEY", "sk-first")
    monkeypatch.setenv("PSEUDOLIFE_JUDGE_SECOND_API_KEY", "sk-second")
    monkeypatch.delenv("PSEUDOLIFE_DREAM_API_KEY", raising=False)
    cfg.judge_url, cfg.judge_model = "https://judge.example.com/v1", "claude-opus-5-5"
    assert svc._judge_extractor().api_key == "sk-first"
    cfg.judge_second_model = "claude-fable-5"               # swapped onto judge_url
    swapped = svc._judge_second_extractor()
    assert (swapped.base_url, swapped.api_key) == ("https://judge.example.com/v1", "sk-first")
    cfg.judge_second_url = "https://api.example.com/v1"
    assert svc._judge_second_extractor().api_key == "sk-second"
    cfg.judge_url = ""                                        # the dream extractor
    monkeypatch.setenv("PSEUDOLIFE_DREAM_BASE_URL", "https://dream.example.com/v1")
    monkeypatch.setenv("PSEUDOLIFE_DREAM_MODEL", "extractor")
    dream = svc._judge_extractor()
    assert dream.base_url == "https://dream.example.com/v1"
    assert dream.api_key is None


def _config_path_judges(svc, monkeypatch, first, second=None):
    """Drive deep_dream_judge down its CONFIG path (no extractor passed):
    the first opinion's endpoint is ``first``; the second is whatever
    _judge_second_extractor builds, or ``second`` when given."""
    monkeypatch.setattr(svc, "_judge_extractor", lambda *a, **k: first)
    if second is not None:
        monkeypatch.setattr(svc, "_judge_second_extractor", lambda: second)


@pytest.mark.parametrize("mode", ["shadow", "auto-reject"])
@pytest.mark.parametrize("second_model", ["", None, "stub-merge-judge"])
def test_same_model_by_config_skips_the_second_opinion_call(svc, monkeypatch, mode, second_model):
    """No judge_second_url and a second model that is empty or equal to the
    first's configured model: the second vote would be the first model again,
    which authorizes nothing in any mode (and informs no decision in
    shadow), so no model call is spent on it. The batch goes to first
    opinions instead (maintainer decision 2026-09-30)."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = mode
    cfg.judge_second_opinion = True
    cfg.judge_second_url = ""
    cfg.judge_second_model = second_model
    waiting = _propose(svc, "tau svc", "tau harness")
    judge = _MergeJudge({("tau svc", "tau harness"): ("reject", 0.6),
                         ("upsilon svc", "upsilon harness"): ("leave", 0.5)})
    _config_path_judges(svc, monkeypatch, judge)
    assert svc.deep_dream_judge(limit=1)["judged"] == 1
    fresh = _propose(svc, "upsilon svc", "upsilon harness")
    out = svc.deep_dream_judge(limit=1)
    assert out["second_opinions"] == 0 and out["second_opinion_skipped_same_model"] == 1
    assert out["judged"] == 1 and judge.calls == 2           # the fresh row, no second call
    assert _merge_row(svc, waiting)["judge2_verdict"] is None
    assert _merge_row(svc, fresh)["judge_verdict"] == "leave"
    # Nothing left to judge: the tick says why both waiting rows are idle.
    again = svc.deep_dream_judge(limit=1)
    assert again["judged"] == 0 and again["second_opinion_skipped_same_model"] == 2
    assert judge.calls == 2


def test_a_distinct_second_model_by_config_still_gets_its_call(svc, monkeypatch):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = True
    cfg.judge_second_url = ""
    cfg.judge_second_model = "stub-merge-judge-2"
    pair = ("phi svc", "phi harness")
    pid = _propose(svc, *pair)
    first, second = _MergeJudge({pair: ("reject", 0.6)}), _SecondJudge({pair: ("reject", 0.9)})
    _config_path_judges(svc, monkeypatch, first, second)
    svc.deep_dream_judge()
    out = svc.deep_dream_judge()
    assert second.calls == 1 and out["second_opinions"] == 1
    assert out.get("second_opinion_skipped_same_model", 0) == 0
    assert _merge_row(svc, pid)["judge2_verdict"] == "reject"


class _DownJudge(_SecondJudge):
    def judge_merges(self, proposals):
        from pseudolife_memory.memory.dream import ExtractorError
        raise ExtractorError("merge judge failed: HTTP Error 401: Unauthorized")


def test_a_failing_second_endpoint_never_starves_first_opinions(svc):
    """With judge_second_url the two opinions fail independently: a second
    endpoint that is down or refuses its key must not stop first opinions
    (review finding, 2026-09-30: the second batch runs first, and its
    exception ended the whole tick while any row awaited a second vote).
    Its share of the batch goes to first opinions for that tick."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = True
    waiting = _propose(svc, "omicron svc", "omicron harness")
    judge = _MergeJudge({("omicron svc", "omicron harness"): ("reject", 0.6),
                         ("pi svc", "pi harness"): ("leave", 0.5)})
    assert svc.deep_dream_judge(judge, limit=1)["judged"] == 1
    fresh = _propose(svc, "pi svc", "pi harness")
    out = svc.deep_dream_judge(judge, limit=1, second_extractor=_DownJudge({}))
    assert "401" in out["second_opinion_error"]
    assert out["second_opinions"] == 0 and out["judged"] == 1
    assert _merge_row(svc, fresh)["judge_verdict"] == "leave"
    assert _merge_row(svc, waiting)["judge2_verdict"] is None      # asked again later


class _DiesAfterFirstCall(_MergeJudge):
    def judge_merges(self, proposals):
        if self.calls:
            self.calls += 1
            from pseudolife_memory.memory.dream import ExtractorError
            raise ExtractorError("merge judge failed: timed out")
        return super().judge_merges(proposals)


def test_a_failing_shared_endpoint_still_ends_the_tick_after_one_call(svc):
    """On the first opinion's own endpoint a second-opinion failure predicts
    the first call's: the tick ends as before instead of waiting out a
    second timeout on the sweep thread."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = True
    _propose(svc, "rho svc", "rho harness")
    judge = _DiesAfterFirstCall({("rho svc", "rho harness"): ("reject", 0.6)})
    assert svc.deep_dream_judge(judge, limit=1)["judged"] == 1
    _propose(svc, "sigma svc", "sigma harness")
    out = svc.deep_dream_judge(judge, limit=2)    # ex2 is ex; one slot left for sigma
    assert out["judged"] == 0 and "timed out" in out["error"]
    assert judge.calls == 2                                    # one failing call


def test_the_skip_holds_on_the_real_judge_url_builder(svc, monkeypatch):
    """The same-model skip read through the real endpoint builder: judge_url
    serving judge_model, and judge_second_model naming that same model."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    calls = []

    def fake_judge_merges(self, proposals):
        calls.append(self.model)
        self.served_model = self.model
        return [{"n": p["n"], "verdict": "reject", "confidence": 0.6, "note": "stub"}
                for p in proposals]

    monkeypatch.setattr(OpenAICompatExtractor, "judge_merges", fake_judge_merges)
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_second_opinion = True
    cfg.judge_url, cfg.judge_model = "https://judge.example.com/v1", "claude-opus-5-5"
    cfg.judge_second_url, cfg.judge_second_model = "", "claude-opus-5-5"
    pid = _propose(svc, "chi svc", "chi harness")
    assert svc.deep_dream_judge()["judged"] == 1
    out = svc.deep_dream_judge()
    assert out["second_opinion_skipped_same_model"] == 1 and calls == ["claude-opus-5-5"]
    # A distinct model is asked. Changing the knob changes the signed
    # judging policy, so the first verdict is re-judged on the next tick and
    # the second opinion follows on the one after.
    cfg.judge_second_model = "claude-sonnet-5-5"
    assert svc.deep_dream_judge()["judged"] == 1 and calls[-1] == "claude-opus-5-5"
    out = svc.deep_dream_judge()
    assert out["second_opinions"] == 1 and calls[-1] == "claude-sonnet-5-5"
    assert _merge_row(svc, pid)["judge2_verdict"] == "reject"


class _KeyedJudge(_MergeJudge):
    base_url = "https://judge.example.com/v1"
    api_key = "sk-first"


class _UnkeyedSecond(_DownJudge):
    base_url = "https://judge.example.com/v1"
    api_key = None


def test_a_second_opinion_on_the_same_url_with_another_key_never_starves_first_opinions(svc):
    """judge_url has its own key since 2026-09-30, so a second endpoint on
    the same URL can fail (401 without the key) where the first call would
    not: that failure must not end the tick, or first opinions stop for
    good (review finding on the follow-up)."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "shadow"
    cfg.judge_second_opinion = True
    waiting = _propose(svc, "psi svc", "psi harness")
    judge = _KeyedJudge({("psi svc", "psi harness"): ("reject", 0.6),
                         ("omega svc", "omega harness"): ("leave", 0.5)})
    assert svc.deep_dream_judge(judge, limit=1)["judged"] == 1
    fresh = _propose(svc, "omega svc", "omega harness")
    out = svc.deep_dream_judge(judge, limit=2, second_extractor=_UnkeyedSecond({}))
    assert "401" in out["second_opinion_error"] and out["judged"] == 1
    assert _merge_row(svc, fresh)["judge_verdict"] == "leave"
    assert _merge_row(svc, waiting)["judge2_verdict"] is None


def test_compose_forwards_the_second_judge_key_to_the_daemon():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    for compose in (root / "ops" / "docker-compose.yml",
                    root / "pseudolife_memory" / "compose" / "docker-compose.yml"):
        env = yaml.safe_load(compose.read_text(encoding="utf-8"))[
            "services"]["pseudolife-daemon"]["environment"]
        for key in ("PSEUDOLIFE_JUDGE_API_KEY", "PSEUDOLIFE_JUDGE_SECOND_API_KEY"):
            assert env.get(key) == "${" + key + ":-}", (compose, key)


def test_an_unset_new_judge_knob_leaves_every_review_fingerprint_as_it_was(svc, monkeypatch):
    """Every pending verdict and automatic decision is fingerprinted over the
    deep_dream config. A knob added at its unset default must leave that
    view, and so every fingerprint, unchanged: otherwise the first tick after
    the deploy clears every recorded verdict and reopens every automatic
    reject (the 2026-09-23 group fix pinned the same continuity)."""
    import dataclasses

    from pseudolife_memory import curation_safety
    from pseudolife_memory.memory import review_judgments as rj
    cfg = svc.config.memory.deep_dream
    before = {k: v for k, v in dataclasses.asdict(cfg).items()
              if k != "judge_second_url"}
    assert rj.signed_deep_dream(cfg) == before
    cfg.judge_second_url = None                     # the Console's clear
    assert rj.signed_deep_dream(cfg) == before
    cfg.judge_second_url = "http://127.0.0.1:8086/v1"
    assert rj.signed_deep_dream(cfg) == {
        **before, "judge_second_url": "http://127.0.0.1:8086/v1"}
    # All three fingerprint sites sign through that view.
    calls = []
    real = rj.signed_deep_dream
    monkeypatch.setattr(rj, "signed_deep_dream",
                        lambda c: calls.append(c) or real(c))
    rj.judging_policy(svc, _MergeJudge(), "prompt")
    with svc._lock:
        rj.candidate_generation(svc)
    curation_safety.curation_policy_fingerprint(svc, _MergeJudge())
    assert len(calls) == 3


def test_auto_mode_accepts_only_two_vote_non_low_differential(svc):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto"
    cfg.judge_second_opinion = True
    cfg.judge_accept_min_confidence = 0.6
    # Distinct evidence per side -> not low-differential.
    svc.store("gamma svc handles the gamma ingest path", source="t")
    svc.store("gamma service is the deployed gamma daemon name", source="t")
    good = _propose(svc, "gamma svc", "gamma service")
    # No evidence at all -> low_differential (empty sides): never auto-accepted.
    thin = _propose(svc, "delta svc", "delta service")
    verdicts = {("gamma svc", "gamma service"): ("accept", 0.7),
                ("delta svc", "delta service"): ("accept", 0.9)}
    judge = _MergeJudge(verdicts)
    second = _SecondJudge(verdicts)                 # a DIFFERENT model
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_accepted"] == 1
    st = svc._storage
    displays = {e["display"] for e in st.load_graph()["entities"]}
    assert "gamma svc" not in displays and "gamma service" in displays  # folded
    assert st.find_entity("gamma-svc")["canonical"] == "gamma-service"  # alias kept
    assert _merge_row(svc, thin)["status"] == "pending"
    dec = [r for r in st.recent_entity_decisions()
           if r["decided_by"] == "dream-judge" and r["status"] == "accepted"]
    assert dec and dec[0]["entity"] == "gamma svc"


def test_auto_mode_refuses_same_model_second_vote_and_name_vetoes(svc):
    """A second vote from the SAME model (temperature 0) mostly repeats the
    first — not independent enough to authorize an irreversible fold; and
    the name vetoes every filing path applies hold at apply time too."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto"
    cfg.judge_second_opinion = True
    svc.store("zeta svc handles the zeta ingest path", source="t")
    svc.store("zeta service is the deployed zeta daemon", source="t")
    svc.store("model E4B is the small extractor variant", source="t")
    svc.store("model E2B is the smaller extractor variant", source="t")
    same = _propose(svc, "zeta svc", "zeta service")
    variant = _propose(svc, "model E4B", "model E2B")
    verdicts = {("zeta svc", "zeta service"): ("accept", 0.9),
                ("model E4B", "model E2B"): ("accept", 0.9)}
    judge = _MergeJudge(verdicts)
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge)                # same model both times
    assert out["auto_accepted"] == 0 and out["auto_accept_refused"] == 2
    assert "distinct second model" in _merge_row(svc, same)["judge_note"]
    second = _SecondJudge(verdicts)
    # Re-run with a distinct model: the variant-conflict pair is still refused.
    for row in (same, variant):
        svc._storage.conn.execute(
            "UPDATE entity_proposals SET judge2_verdict = NULL WHERE id = %s", (row,))
    svc._storage.conn.commit()
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_accepted"] == 1
    # The fold changes differential evidence: defer the remaining row to
    # a fresh first opinion, then exercise its name veto on the second.
    following = [svc.deep_dream_judge(judge, second_extractor=second)
                 for _ in range(2)]
    assert sum(r.get("auto_accept_refused", 0) for r in following) == 1
    note = _merge_row(svc, variant)["judge_note"]
    # merge_veto (numeric-substitution) screens E4B/E2B before the variant
    # check gets its turn; either name veto is a refusal.
    assert "auto-accept refused" in note


def test_auto_accept_refuses_dismissed_pairs_and_analyzer_rows(svc):
    """A pair an earlier verdict settled as distinct (relate / dismiss_pair
    writes dismissed_pairs, never the proposal row) must never be folded
    over that decision; analyzer-filed rows are an unmeasured class."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto"
    cfg.judge_second_opinion = True
    svc.store("theta svc handles the theta path", source="t")
    svc.store("theta service is the theta daemon", source="t")
    svc.store("iota svc handles the iota path", source="t")
    svc.store("iota service is the iota daemon", source="t")
    dismissed = _propose(svc, "theta svc", "theta service")
    assert svc.graph_dismiss_duplicate("theta svc", "theta service")["dismissed"]
    st = svc._storage
    from pseudolife_memory.graph import norm_name
    for n in ("iota svc", "iota service"):
        st.ensure_entity(norm_name(n), display=n)
    analyzer = st.insert_entity_proposal(
        "merge", st.find_entity("iota-svc")["id"], st.find_entity("iota-service")["id"],
        0.75, "analyzer-duplicate: jaccard 0.75", time.time())
    verdicts = {("theta svc", "theta service"): ("accept", 0.9),
                ("iota svc", "iota service"): ("accept", 0.9)}
    judge, second = _MergeJudge(verdicts), _SecondJudge(verdicts)
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_accepted"] == 0 and out["auto_accept_refused"] == 2
    assert st.get_entity_proposal(dismissed)["status"] == "rejected"     # moot row closed
    assert st.get_entity_proposal(dismissed)["decided_by"] == "dream-judge"
    displays = {e["display"] for e in st.load_graph()["entities"]}
    assert {"theta svc", "theta service", "iota svc", "iota service"} <= displays
    row = _merge_row(svc, analyzer)
    assert row["status"] == "pending" and "unmeasured" in row["judge_note"]


def test_auto_accept_same_endpoint_never_distinct_even_if_stamp_differs(svc):
    """A row judged before this build carries the CONFIGURED model name;
    the second opinion stamps the SERVED name. When both come from the same
    extractor object the two strings may differ for one physical model —
    that must not pass as a distinct second model."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto"
    cfg.judge_second_opinion = True
    svc.store("kappa svc handles the kappa path", source="t")
    svc.store("kappa service is the kappa daemon", source="t")
    pid = _propose(svc, "kappa svc", "kappa service")
    judge = _MergeJudge({("kappa svc", "kappa service"): ("accept", 0.9)})
    svc.deep_dream_judge(judge)
    with svc._lock:
        svc._storage.conn.execute(
            "UPDATE entity_proposals SET judge_model = %s WHERE id = %s",
            ("claude-opus-5", pid))                        # legacy configured stamp
        svc._storage.conn.commit()
    judge.served_model = "claude-opus-5-20260901"          # dated served id
    # A changed served identity first invalidates and replaces the old vote.
    assert svc.deep_dream_judge(judge)["judged"] == 1
    out = svc.deep_dream_judge(judge)                       # ex2 is ex
    assert out["auto_accepted"] == 0 and out["auto_accept_refused"] == 1
    assert "distinct second model" in _merge_row(svc, pid)["judge_note"]


def test_auto_accept_guard_keys_on_stored_canonicals(svc):
    """dismissed_pairs is keyed by the entity's STORED canonical — an entity
    minted from a bare name and later display-enriched ('GND (Enshrouded
    server)' over canonical 'gnd') has a canonical norm_name(display) never
    reproduces (graph_dismiss_duplicate's own 2026-08-16 lesson). The
    guard must resolve through the proposal's entity ids, or a dismissed
    pair folds anyway over the human verdict."""
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto"
    cfg.judge_second_opinion = True
    st = svc._storage
    a = st.ensure_entity("gnd", display="gnd")
    b = st.ensure_entity("gnd-box", display="GND box")
    with st._txn():
        st.conn.execute("UPDATE entities SET display = %s WHERE id = %s",
                        ("GND (Enshrouded server)", a))
    svc.store("GND (Enshrouded server) hosts the game world", source="t")
    svc.store("GND box sits in the rack", source="t")
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "test", time.time())
    assert svc.graph_dismiss_duplicate("GND (Enshrouded server)", "GND box")["dismissed"]
    assert ("gnd", "gnd-box") in st.dismissed_pairs()          # canonical keys
    verdicts = {("GND (Enshrouded server)", "GND box"): ("accept", 0.9)}
    judge, second = _MergeJudge(verdicts), _SecondJudge(verdicts)
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_accepted"] == 0
    assert st.get_entity_proposal(pid)["status"] == "rejected"     # closed, not folded
    displays = {e["display"] for e in st.load_graph()["entities"]}
    assert {"GND (Enshrouded server)", "GND box"} <= displays


def test_judges_kill_switch(svc):
    cfg = svc.config.memory.deep_dream
    cfg.judges_enabled = False
    for name in ("deep_dream_judge", "deep_dream_judge_links", "deep_dream_judge_junk",
                 "deep_dream_judge_curation", "deep_dream_judge_candidates"):
        assert getattr(svc, name)()["skipped"] == "judges_disabled", name


def test_auto_reject_mode_never_auto_accepts(svc):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_second_opinion = True
    svc.store("eps svc handles the eps path", source="t")
    svc.store("eps service is the eps daemon", source="t")
    pid = _propose(svc, "eps svc", "eps service")
    judge = _MergeJudge({("eps svc", "eps service"): ("accept", 0.95)},
                        {("eps svc", "eps service"): ("accept", 0.95)})
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge)
    assert out.get("auto_accepted", 0) == 0
    assert _merge_row(svc, pid)["status"] == "pending"


# ── candidate judge ───────────────────────────────────────────────────────

def test_candidate_judge_shadow_files_nothing(svc):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = "shadow"
    st = svc._storage
    for n in ("shadow-a", "shadow-b"):
        st.ensure_entity(n, display=n)
    cands = [{"src_id": st.find_entity("shadow-a")["id"], "dst_id": st.find_entity("shadow-b")["id"],
              "src": "shadow-a", "dst": "shadow-b", "similarity": 0.9,
              "src_snippets": ["s"], "dst_snippets": ["d"]}]
    judge = _CandidateJudge({("shadow-a", "shadow-b"): ("dismiss", 0.9, None, None, None)})
    out = svc.deep_dream_judge_candidates(judge, candidates=cands)
    assert out["judged"] == 1 and out["dismissed"] == 0 and out["mode"] == "shadow"
    assert ("shadow-a", "shadow-b") not in st.dismissed_pairs()


def test_candidate_judge_one_slice_per_call_and_memo(svc):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = "auto"
    cfg.candidate_min_confidence = 0.6
    st = svc._storage
    for n in ("slice-a", "slice-b", "slice-c", "slice-d"):
        st.ensure_entity(n, display=n)
    cands = [{"src_id": st.find_entity("slice-a")["id"], "dst_id": st.find_entity("slice-b")["id"],
              "src": "slice-a", "dst": "slice-b", "similarity": 0.9, "src_snippets": ["s"], "dst_snippets": ["d"]},
             {"src_id": st.find_entity("slice-c")["id"], "dst_id": st.find_entity("slice-d")["id"],
              "src": "slice-c", "dst": "slice-d", "similarity": 0.8, "src_snippets": ["s"], "dst_snippets": ["d"]}]
    judge = _CandidateJudge({("slice-a", "slice-b"): ("dismiss", 0.9, None, None, None),
                             ("slice-c", "slice-d"): ("dismiss", 0.9, None, None, None)})
    out = svc.deep_dream_judge_candidates(judge, candidates=cands, limit=1)
    assert out["judged"] == 1 and out["remaining"] == 1
    out = svc.deep_dream_judge_candidates(judge, candidates=cands, limit=1)
    assert out["judged"] == 1 and out["remaining"] == 0
    assert ("slice-a", "slice-b") in st.dismissed_pairs()
    assert ("slice-c", "slice-d") in st.dismissed_pairs()
    # memoised: nothing re-sent
    out = svc.deep_dream_judge_candidates(judge, candidates=cands, limit=1)
    assert out["judged"] == 0 and out["reason"] == "all_judged"


def test_candidate_judge_files_proposals_and_dismisses(svc):
    cfg = svc.config.memory.deep_dream
    cfg.candidate_judge_mode = "auto"
    cfg.candidate_min_confidence = 0.6
    st = svc._storage
    for n in ("stalker-2", "DLSS 4.5", "Lumen", "video menu"):
        st.ensure_entity(n.lower().replace(" ", "-"), display=n)
    cands = [{"src_id": st.find_entity("stalker-2")["id"], "dst_id": st.find_entity("dlss-4.5")["id"],
              "src": "stalker-2", "dst": "DLSS 4.5", "similarity": 0.9,
              "src_snippets": ["s"], "dst_snippets": ["d"]},
             {"src_id": st.find_entity("lumen")["id"], "dst_id": st.find_entity("video-menu")["id"],
              "src": "Lumen", "dst": "video menu", "similarity": 0.8,
              "src_snippets": ["s"], "dst_snippets": ["d"]}]
    judge = _CandidateJudge({
        ("stalker-2", "DLSS 4.5"): ("propose", 0.7, "uses", "stalker-2", "DLSS 4.5"),
        ("Lumen", "video menu"): ("dismiss", 0.8, None, None, None),
    })
    out = svc.deep_dream_judge_candidates(judge, candidates=cands)
    assert out["judged"] == 2 and out["proposed"] == 1 and out["dismissed"] == 1
    pend = st.pending_proposals()
    assert any(p["src"] == "stalker-2" and p["relation"] == "uses"
               and p["dst"] == "DLSS 4.5" and p["source"] == "deep-dream-judge"
               for p in pend)
    assert ("lumen", "video-menu") in st.dismissed_pairs()


# ── apply-time additions: analyzer duplicates filed, unreachable orphans swept ──

def test_apply_files_analyzer_duplicates_into_the_queues(svc):
    svc.config.memory.deep_dream.analyzer_file_duplicates = True
    # Mint through storage so the WRITE-TIME dedup detector (which files on
    # graph_relate mints) stays out of it: the analyzer pass is the only
    # filer here, as it is for every pair that predates the detector.
    st = svc._storage
    # A NEAR-duplicate pair (jaccard 0.75): a token-set-identical pair would
    # be Step A's exact-duplicate auto-merge, never the analyzer's.
    for name in ("Cortex Console web frontend", "Cortex Console frontend",
                 "band.py", "band", "gemma E4B model", "gemma E2B model",
                 "gemma-4 UD-Q4_K_XL", "gemma-4 Q4_K_M"):
        from pseudolife_memory.graph import norm_name
        st.ensure_entity(norm_name(name), display=name)
    svc.graph_relate("Cortex Console web frontend", "uses", "js-lib", origin="agent")
    svc.graph_relate("Cortex Console frontend", "uses", "css-lib", origin="agent")
    svc.graph_relate("band.py", "part-of", "memory-package", origin="agent")
    svc.graph_relate("band", "stores-data-in", "postgres", origin="agent")
    svc.graph_relate("gemma E4B model", "uses", "gguf-a", origin="agent")
    svc.graph_relate("gemma E2B model", "uses", "gguf-b", origin="agent")
    svc.graph_relate("gemma-4 UD-Q4_K_XL", "uses", "gguf-c", origin="agent")
    svc.graph_relate("gemma-4 Q4_K_M", "uses", "gguf-d", origin="agent")
    out = svc.deep_dream(apply=True, include_snippets=False)
    assert out["applied"] is True
    merges = [p for p in svc._storage.pending_entity_proposals()
              if p["kind"] == "merge" and str(p["reason"]).startswith("analyzer-duplicate")]
    assert any({p["entity"], p["into"]} == {"Cortex Console web frontend",
                                            "Cortex Console frontend"}
               for p in merges)
    # Size/quant/version-conflicting pairs are never filed as merges. The
    # E4B/E2B pair is caught by merge_veto's numeric-substitution rule
    # inside duplicate_candidates; the quant pair below passes merge_veto
    # (no digit-bearing diff tokens) and is stopped ONLY by
    # variant_conflict on the analyzer path — the load-bearing check.
    assert not any({p["entity"], p["into"]} == {"gemma E4B model", "gemma E2B model"}
                   for p in merges)
    assert not any({p["entity"], p["into"]} == {"gemma-4 UD-Q4_K_XL", "gemma-4 Q4_K_M"}
                   for p in merges)
    links = svc._storage.pending_proposals()
    assert any(p["src"] == "band.py" and p["relation"] == "implements"
               and p["dst"] == "band" and p["source"] == "analyzer" for p in links)
    assert out["analyzer_filed"] >= 2
    # idempotent: a second apply files nothing new
    again = svc.deep_dream(apply=True, include_snippets=False)
    assert again["analyzer_filed"] == 0


def test_apply_sweeps_only_old_unreachable_orphans(svc):
    cfg = svc.config.memory.deep_dream
    cfg.orphan_sweep = True
    cfg.orphan_min_age_days = 7
    cfg.orphan_max_per_apply = 1
    st = svc._storage
    old = st.ensure_entity("stale-orphan", display="stale-orphan")
    older = st.ensure_entity("staler-orphan", display="staler-orphan")
    worldly = st.ensure_entity("worldly-orphan", display="worldly-orphan")
    svc.world_write("worldly-orphan", "kind", "a cited external thing",
                    source_url="https://example.com/w")
    young = st.ensure_entity("young-orphan", display="young-orphan")
    mentioned = st.ensure_entity("mentioned-orphan", display="mentioned-orphan")
    svc.store("the mentioned-orphan node is named in this note", source="t")
    facty = st.ensure_entity("facty-orphan", display="facty-orphan")
    svc.cortex_write("facty-orphan", "role", "has a fact", support="user")
    week = 8 * 86400
    with st._txn():
        st.conn.execute("UPDATE entities SET created_at = created_at - %s "
                        "WHERE id IN (%s, %s, %s, %s, %s)",
                        (week, old, older, mentioned, facty, worldly))
    out = svc.deep_dream(apply=True, include_snippets=False)
    assert out["orphans_deleted"] == 1                             # capped
    assert (st.find_entity("stale-orphan") is None) != (st.find_entity("staler-orphan") is None)
    assert st.find_entity("worldly-orphan") is not None            # world fact = evidence
    cfg.orphan_max_per_apply = 0
    out = svc.deep_dream(apply=True, include_snippets=False)
    assert out["orphans_deleted"] == 1                             # the other one
    assert st.find_entity("stale-orphan") is None and st.find_entity("staler-orphan") is None
    assert st.find_entity("young-orphan") is not None
    assert st.find_entity("mentioned-orphan") is not None
    assert st.find_entity("facty-orphan") is not None
    audit = [r for r in st.recent_entity_decisions()
             if r["entity"] == "stale-orphan"]
    assert audit and audit[0]["decided_by"] == "dream-auto"
    assert audit[0]["status"] == "deleted"                           # not a junk tombstone
    assert "stale-orphan" not in st.junk_accepted_displays()


def test_dry_run_reports_the_orphan_census_without_deleting(svc):
    cfg = svc.config.memory.deep_dream
    cfg.orphan_sweep = False                                       # shipped default
    cfg.orphan_min_age_days = 7
    st = svc._storage
    old = st.ensure_entity("census-orphan", display="census-orphan")
    with st._txn():
        st.conn.execute("UPDATE entities SET created_at = created_at - %s WHERE id = %s",
                        (8 * 86400, old))
    out = svc.deep_dream(apply=False)
    assert out["would_orphan_count"] >= 1
    assert any(w["entity"] == "census-orphan" and w["age_days"] >= 7 for w in out["would_orphan"])
    assert st.find_entity("census-orphan") is not None
    applied = svc.deep_dream(apply=True, include_snippets=False)
    assert applied["orphans_deleted"] == 0                          # switch is off
    assert st.find_entity("census-orphan") is not None


def test_zero_evidence_census_is_storage_level(svc):
    st = svc._storage
    a = st.ensure_entity("bare-a", display="bare-a")
    b = st.ensure_entity("edged-b", display="edged-b")
    svc.graph_relate("edged-b", "uses", "bare-c", origin="agent")
    svc.graph_unrelate("edged-b", "uses", "bare-c")                 # superseded still counts
    ids = {r["id"] for r in st.zero_evidence_entities(min_age_seconds=0)}
    assert a in ids and b not in ids


# ── sweep wiring ──────────────────────────────────────────────────────────

def test_sweep_runs_every_judge_stage():
    from pseudolife_memory.memory.dream import run_sweep_once

    calls = []

    class _FakeService:
        class config:  # noqa: D106
            class memory:
                class dream:
                    enabled = True

        def compact_superseded(self):
            return {"total": 0}

        def prune_dream_runs(self):
            return 0

        def dream_status(self):
            return {"would_fire": False, "backlog": 0}

        def analyzer_duplicate_tick(self):
            calls.append("analyzer")
            return {"fired": True, "filed": 1}

        def deep_dream_judge(self):
            calls.append("merge")
            return {"judged": 0}

        def deep_dream_judge_links(self):
            calls.append("links")
            return {"judged": 1, "applied": 1}

        def deep_dream_judge_junk(self):
            calls.append("junk")
            return {"judged": 0}

        def deep_dream_judge_curation(self):
            calls.append("curation")
            return {"judged": 0}

        def deep_dream_judge_candidates(self):
            calls.append("candidates")
            return {"judged": 0}

    out = run_sweep_once(_FakeService())
    assert calls == ["analyzer", "merge", "links", "junk", "curation", "candidates"]
    assert out["analyzer_tick"] == {"fired": True, "filed": 1}
    assert "analyzer_tick" in out["timings"]
    assert out["deep_judge_links"] == {"judged": 1, "applied": 1}
    assert "judge_links" in out["timings"]


# ── the merge judge's "relate" verdict (2026-09-30) ──────────────────────

def _merge_links(svc):
    return [(p["src"], p["relation"], p["dst"], p["source"])
            for p in svc._storage.pending_proposals()]


def _two_vote_auto_reject(svc):
    cfg = svc.config.memory.deep_dream
    cfg.judge_mode = "auto-reject"
    cfg.judge_reject_min_confidence = 0.8
    cfg.judge_second_opinion = True
    cfg.judge_reject_min_confidence_2 = 0.7


def test_two_vote_reject_plus_relate_applies_and_files_the_link(svc):
    """reject + relate from DIFFERENT models is two reject-class votes: the
    merge is rejected and the relate vote's relation is filed as a link."""
    _two_vote_auto_reject(svc)
    pid = _propose(svc, "omicron module", "omicron concept")
    pair = ("omicron module", "omicron concept")
    judge = _MergeJudge({pair: ("reject", 0.6)})
    second = _SecondJudge({pair: ("relate", 0.85, "implements")})
    assert svc.deep_dream_judge(judge)["auto_rejected"] == 0
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["second_opinions"] == 1 and out["auto_rejected"] == 1
    assert out["relate_links_filed"] == 1
    row = svc._storage.get_entity_proposal(pid)
    assert row["status"] == "rejected" and row["decided_by"] == "dream-judge"
    assert row["judge2_verdict"] == "relate"
    # Reject vs relate is agreement on the merge, not a split.
    note = svc._storage.conn.execute(
        "SELECT judge_note FROM entity_proposals WHERE id=%s", (pid,)).fetchone()[0]
    assert "[agree]" in note and "relate:implements" in note
    assert _merge_links(svc) == [
        ("omicron module", "implements", "omicron concept", "merge-judge-relate")]


def test_two_relate_votes_file_the_more_confident_relation(svc):
    _two_vote_auto_reject(svc)
    _propose(svc, "pi module", "pi service")
    pair = ("pi module", "pi service")
    judge = _MergeJudge({pair: ("relate", 0.75, "part-of")})
    second = _SecondJudge({pair: ("relate", 0.9, "uses")})
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_rejected"] == 1 and out["relate_links_filed"] == 1
    assert _merge_links(svc) == [("pi module", "uses", "pi service", "merge-judge-relate")]


def test_accept_vs_relate_is_a_split_and_applies_nothing(svc):
    cfg = svc.config.memory.deep_dream
    _two_vote_auto_reject(svc)
    cfg.judge_mode = "auto"
    pid = _propose(svc, "rho module", "rho concept")
    pair = ("rho module", "rho concept")
    judge = _MergeJudge({pair: ("accept", 0.9)})
    second = _SecondJudge({pair: ("relate", 0.95, "implements")})
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["second_opinions"] == 1
    assert out["auto_rejected"] == 0 and out["auto_accepted"] == 0
    assert out["relate_links_filed"] == 0
    row = _merge_row(svc, pid)
    assert row["status"] == "pending" and row["judge2_verdict"] == "relate"
    assert "[split]" in row["judge_note"]
    assert _merge_links(svc) == []


def test_same_model_reject_class_pair_files_nothing(svc):
    """The distinct-model rule holds for relate too: one model's reject +
    relate is refused, so no reject and no link."""
    _two_vote_auto_reject(svc)
    pid = _propose(svc, "sigma module", "sigma concept")
    pair = ("sigma module", "sigma concept")
    judge = _MergeJudge({pair: ("relate", 0.6, "implements")},
                        {pair: ("relate", 0.9, "implements")})
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=None)
    assert out["auto_rejected"] == 0 and out["auto_reject_refused_same_model"] == 1
    assert out["relate_links_filed"] == 0
    assert _merge_row(svc, pid)["status"] == "pending"
    assert _merge_links(svc) == []


def test_first_opinion_relation_is_recovered_for_the_link(svc):
    """A relate FIRST vote below the single gate, then a reject from another
    model: the relation comes back out of the first opinion's note."""
    _two_vote_auto_reject(svc)
    _propose(svc, "phi module", "phi concept")
    pair = ("phi module", "phi concept")
    judge = _MergeJudge({pair: ("relate", 0.75, "part-of")})
    second = _SecondJudge({pair: ("reject", 0.8)})
    svc.deep_dream_judge(judge)
    out = svc.deep_dream_judge(judge, second_extractor=second)
    assert out["auto_rejected"] == 1 and out["relate_links_filed"] == 1
    assert _merge_links(svc) == [("phi module", "part-of", "phi concept", "merge-judge-relate")]


@pytest.mark.parametrize("result, logged", [
    ({"judged": 0}, False),
    ({"judged": 0, "second_opinions": 0, "auto_reject_refused_same_model": 0,
      "served_model_mismatch": 0, "served_model_mismatches": []}, False),
    ({"judged": 1}, True),
    ({"judged": 0, "reconsideration": {"reopened": 1}}, True),
    ({"judged": 0, "second_opinions": 2}, True),
    ({"judged": 0, "second_opinions": 1, "auto_reject_refused_same_model": 1}, True),
    ({"judged": 0, "served_model_mismatch": 1}, True),
    ({"judged": 0, "second_opinion_error": "HTTP Error 401"}, True),
])
def test_sweep_logs_the_merge_judge_whenever_it_did_or_refused_something(caplog, result, logged):
    """The sweep logged the merge judge only when it judged or reopened
    something, so a tick of second opinions, refused same-model rejects, a
    served-model substitution or a failing second endpoint left no line."""
    import logging

    from pseudolife_memory.memory.dream import run_sweep_once

    class _FakeService:
        class config:  # noqa: D106
            class memory:
                class dream:
                    enabled = True

        def compact_superseded(self):
            return {"total": 0}

        def prune_dream_runs(self):
            return 0

        def dream_status(self):
            return {"would_fire": False, "backlog": 0}

        def deep_dream_judge(self):
            return dict(result)

    with caplog.at_level(logging.INFO, logger="pseudolife_memory.memory.dream"):
        run_sweep_once(_FakeService())
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("deep-dream judge: {")]
    assert bool(lines) is logged, lines
