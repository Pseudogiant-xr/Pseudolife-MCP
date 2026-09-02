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

    def __init__(self, *rounds):
        self._rounds = list(rounds)   # [{(from, into): (verdict, conf)}, ...]
        self.calls = 0

    def judge_merges(self, proposals):
        verdicts = self._rounds[min(self.calls, len(self._rounds) - 1)]
        self.calls += 1
        out = []
        for p in proposals:
            v = verdicts.get((p["from"]["display"], p["into"]["display"]))
            if v:
                out.append({"n": p["n"], "verdict": v[0], "confidence": v[1],
                            "note": "stub"})
        return out


class _CandidateJudge:
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
    assert out["judged"] == 3 and out["applied"] == 2
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
    # auto mode applies the memoised duplicate: loser forgotten, fold carried
    cfg.curation_judge_mode = "auto"
    cfg.curation_rejudge_days = 0                                    # memo expired
    out = svc.deep_dream_judge_curation(judge)
    assert out["applied"] == 1
    recs = {r.key: r for r in svc._lessons.current_records()}
    keys = {"|".join(k) for k in recs}
    assert "deploy-daemon-to-homelab-host|approach" in keys
    assert "deploy-the-daemon-to-the-host|pitfall" not in keys
    survivor = recs[("deploy-daemon-to-homelab-host", "approach")]
    assert "carry the backup step" in survivor.value


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
         ("beta svc", "beta harness"): ("reject", 0.6)},
        {("alpha svc", "alpha harness"): ("reject", 0.85),
         ("beta svc", "beta harness"): ("accept", 0.7)})
    first = svc.deep_dream_judge(judge)
    assert first["judged"] == 2 and first["auto_rejected"] == 0     # both below 0.8
    assert _merge_row(svc, agree)["judge_verdict"] == "reject"
    second = svc.deep_dream_judge(judge)                             # second opinion round
    assert second["second_opinions"] == 2 and second["auto_rejected"] == 1
    assert _merge_row(svc, agree) is None
    assert svc._storage.get_entity_proposal(agree)["status"] == "rejected"
    assert svc._storage.get_entity_proposal(agree)["decided_by"] == "dream-judge"
    row = _merge_row(svc, split)
    assert row["status"] == "pending" and row["judge2_verdict"] == "accept"
    assert "split" in (row["judge_note"] or "")
    # a third call re-sends nothing: both opinions are on the row
    third = svc.deep_dream_judge(judge)
    assert third["judged"] == 0 and third.get("second_opinions", 0) == 0


class _SecondJudge(_MergeJudge):
    model = "stub-merge-judge-2"


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
    assert out["auto_accepted"] == 1 and out["auto_accept_refused"] == 1
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
    assert calls == ["merge", "links", "junk", "curation", "candidates"]
    assert out["deep_judge_links"] == {"judged": 1, "applied": 1}
    assert "judge_links" in out["timings"]
