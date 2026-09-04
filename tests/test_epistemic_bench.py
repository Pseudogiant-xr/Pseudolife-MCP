"""Fixture-level tests for the epistemic bench (spec
docs/superpowers/specs/2026-09-05-epistemic-bench-design.md).

No database, no model, no GPU. Every metric is a pure predicate over a
hand-built served context, so the whole scoring surface is testable
without paying an ingest — which is the point of a judge-free bench.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import epistemic_bench as eb  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────
def _q(**kw):
    """A Question with the fields a test does not care about defaulted."""
    base = dict(question_id="q1", kind="update", entity="ledger-db",
                attribute="engine", question="What is ledger-db's engine?",
                current_value="ENG-2200", superseded_values=("ENG-1100",),
                corrected_from=None, stale_slot=False, decoy_values=())
    base.update(kw)
    return eb.Question(**base)


def _served(text="", facts=(), entries=()):
    return eb.Served(text=text, facts=tuple(facts), entries=tuple(entries))


def _fact(entity="ledger-db", attribute="engine", value="ENG-2200", **kw):
    d = {"entity": entity, "attribute": attribute, "value": value}
    d.update(kw)
    return d


# ── value matching ────────────────────────────────────────────────────────
def test_value_present_is_word_boundary_matched():
    assert eb.value_present("the engine is ENG-2200 today", "ENG-2200")
    # The ladder's own rule: a short value must not match inside a longer one.
    assert not eb.value_present("the port is 24", "4")
    assert not eb.value_present("ENG-22001 is a different thing", "ENG-2200")
    assert not eb.value_present("", "ENG-2200")
    assert not eb.value_present("anything", "")


def test_value_present_matches_a_value_at_the_end_of_a_sentence():
    """ladder_sweep's matcher excludes a trailing '.' outright, so it misses
    every value a turn ends a sentence on — which zeroed the rag arm's
    coverage on the first smoke run before anyone read a number.
    """
    assert eb.value_present("the engine for ledger-db is ENG-2200.",
                            "ENG-2200")
    assert eb.value_present("my personal best was 25:50.", "25:50")


def test_value_present_still_refuses_a_decimal_continuation():
    """The trailing-dot relaxation must not turn '1' into a match for
    '1.5' — that is the case the original exclusion existed for."""
    assert not eb.value_present("the ratio is 1.5 today", "1")
    assert not eb.value_present("the ratio is 1.5 today", "5")
    assert not eb.value_present("version 2.10 shipped", "2")


# ── D1 update_following ───────────────────────────────────────────────────
def test_update_following_true_when_current_value_is_served():
    assert eb.update_following(
        _q(), _served(text="user: engine is now ENG-2200")) is True


def test_update_following_false_when_only_the_old_value_is_served():
    assert eb.update_following(
        _q(), _served(text="user: engine is ENG-1100")) is False


def test_update_following_not_applicable_to_a_stable_slot():
    assert eb.update_following(_q(kind="stable", superseded_values=()),
                               _served(text="ENG-2200")) is None


# ── D2 stale_serving (a defect: True means the arm failed) ────────────────
def test_stale_serving_is_a_defect_when_the_old_value_is_served_alone():
    assert eb.stale_serving(
        _q(), _served(text="user: engine is ENG-1100")) is True


def test_stale_serving_is_clean_when_the_current_value_is_also_served():
    assert eb.stale_serving(
        _q(), _served(text="ENG-1100 -> ENG-2200")) is False


def test_stale_serving_is_clean_when_nothing_at_all_is_served():
    assert eb.stale_serving(_q(), _served(text="")) is False


def test_stale_serving_not_applicable_without_a_superseded_value():
    assert eb.stale_serving(_q(kind="stable", superseded_values=()),
                            _served(text="ENG-1100")) is None


# ── D3 staleness_marking ──────────────────────────────────────────────────
def test_staleness_marking_reads_the_annotate_policy_flag():
    served = _served(text="ledger-db - engine: ENG-2200",
                     facts=[_fact(stale=True)])
    assert eb.staleness_marking(_q(kind="stale", stale_slot=True,
                                   superseded_values=()), served) is True


def test_staleness_marking_reads_the_demote_and_quarantine_shapes():
    q = _q(kind="stale", stale_slot=True, superseded_values=())
    demoted = _served(facts=[_fact(warning=eb.STALE_WARNING)])
    quarantined = _served(facts=[_fact(value=eb.STALE_QUARANTINE_WRAPPER,
                                       last_known_value="ENG-2200")])
    assert eb.staleness_marking(q, demoted) is True
    assert eb.staleness_marking(q, quarantined) is True


def test_staleness_marking_false_when_the_slots_fact_carries_no_marker():
    served = _served(text="ledger-db - engine: ENG-2200",
                     facts=[_fact(stale=False)])
    assert eb.staleness_marking(_q(kind="stale", stale_slot=True,
                                   superseded_values=()), served) is False


def test_staleness_marking_false_for_a_raw_turn_arm():
    """A served turn carries no freshness_class, so there is no marker."""
    served = _served(text="[2026-01-02] user: engine is ENG-2200",
                     entries=[{"text": "engine is ENG-2200"}])
    assert eb.staleness_marking(_q(kind="stale", stale_slot=True,
                                   superseded_values=()), served) is False


def test_staleness_marking_ignores_a_marker_on_another_slot():
    served = _served(facts=[_fact(entity="ledger-cache", stale=True)])
    assert eb.staleness_marking(_q(kind="stale", stale_slot=True,
                                   superseded_values=()), served) is False


# ── D4 abstention_support + answer_coverage ───────────────────────────────
def _unstated(**kw):
    base = dict(kind="unstated", current_value=None, superseded_values=(),
                decoy_values=("ENG-9900", "ENG-8800"))
    base.update(kw)
    return _q(**base)


def test_abstention_support_true_when_nothing_for_the_slot_is_served():
    assert eb.abstention_support(_unstated(), _served(text="")) is True


def test_abstention_support_false_when_a_near_miss_value_leaks_in():
    served = _served(text="ledger-cache - engine: ENG-9900")
    assert eb.abstention_support(_unstated(), served) is False


def test_abstention_support_false_when_a_fact_for_the_slot_is_served():
    served = _served(facts=[_fact(value="ENG-7777")])
    assert eb.abstention_support(_unstated(), served) is False


def test_abstention_support_not_applicable_to_an_answerable_question():
    assert eb.abstention_support(_q(), _served(text="")) is None


def test_answer_coverage_covers_every_answerable_kind_including_stable():
    for kind, sup in (("update", ("ENG-1100",)), ("stable", ()),
                      ("stale", ()), ("correction", ("ENG-1100",))):
        q = _q(kind=kind, superseded_values=sup,
               corrected_from="ENG-1100" if kind == "correction" else None)
        assert eb.answer_coverage(q, _served(text="ENG-2200")) is True
        assert eb.answer_coverage(q, _served(text="")) is False
    # The no-memory floor's other half: an unstated question is not
    # coverable, so it must not dilute the coverage denominator.
    assert eb.answer_coverage(_unstated(), _served(text="")) is None


# ── D5 retraction_handling ────────────────────────────────────────────────
def _corrected(**kw):
    base = dict(kind="correction", corrected_from="ENG-1100",
                superseded_values=("ENG-1100",))
    base.update(kw)
    return _q(**base)


def test_retraction_handling_reads_the_fact_supersession_chain():
    served = _served(
        text="ledger-db - engine: ENG-2200  (earlier values, oldest "
             "first: ENG-1100)",
        facts=[_fact(supersedes_value="ENG-1100")])
    assert eb.retraction_handling(_corrected(), served) is True


def test_retraction_handling_reads_a_served_turns_superseded_by_text():
    """The rag arm's own channel: contradiction detection stamps the
    retracting text onto the entry that stated the wrong value."""
    served = _served(
        text="user: engine is ENG-1100\n\nuser: correction, it is ENG-2200",
        entries=[{"text": "user: engine is ENG-1100",
                  "superseded": True,
                  "superseded_by_text": "correction, engine is ENG-2200"}])
    assert eb.retraction_handling(_corrected(), served) is True


def test_retraction_handling_false_when_both_values_are_served_unmarked():
    served = _served(text="user: engine is ENG-1100\n\nuser: engine ENG-2200",
                     entries=[{"text": "user: engine is ENG-1100",
                               "superseded_by_text": None}])
    assert eb.retraction_handling(_corrected(), served) is False


def test_retraction_handling_false_when_the_correction_is_not_served():
    served = _served(text="user: engine is ENG-1100",
                     facts=[_fact(value="ENG-1100")])
    assert eb.retraction_handling(_corrected(), served) is False


def test_retraction_handling_not_applicable_to_a_plain_update():
    assert eb.retraction_handling(_q(), _served(text="ENG-2200")) is None


# ── the metric registry ───────────────────────────────────────────────────
def test_every_declared_dimension_has_a_metric_and_a_direction():
    assert set(eb.DIMENSIONS) == set(eb.METRICS)
    for name in eb.DIMENSIONS:
        assert name in eb.HIGHER_IS_BETTER
    # stale_serving is the one defect count; everything else is a rate to
    # maximise. A flipped direction would silently invert the verdict.
    assert eb.HIGHER_IS_BETTER["stale_serving"] is False
    assert eb.HIGHER_IS_BETTER["update_following"] is True


def test_scoring_one_arm_reports_counts_not_just_rates():
    questions = [_q(question_id="a"), _q(question_id="b")]
    served = {"a": _served(text="ENG-2200"), "b": _served(text="ENG-1100")}
    scored = eb.score_arm(questions, lambda q: served[q.question_id])
    assert scored["update_following"] == {"n": 2, "hits": 1, "rate": 0.5}
    # A dimension no question in the set scores reports n=0 and a null
    # rate, never a silent 0.0 that reads as a failing arm.
    assert scored["abstention_support"] == {"n": 0, "hits": 0, "rate": None}


# ── the synthetic generator ───────────────────────────────────────────────
_GEN = dict(entities=4, attributes=3, sessions=4, now=1_780_000_000.0)


def test_generator_is_deterministic_under_a_seed():
    a = eb.generate(seed=7, **_GEN)
    b = eb.generate(seed=7, **_GEN)
    assert a.questions == b.questions
    assert a.turns == b.turns
    assert a.fact_writes == b.fact_writes


def test_generator_differs_across_seeds():
    a = eb.generate(seed=7, **_GEN)
    b = eb.generate(seed=8, **_GEN)
    assert a.questions != b.questions


def test_generator_produces_every_question_kind():
    corpus = eb.generate(seed=7, **_GEN)
    kinds = {q.kind for q in corpus.questions}
    assert kinds == {"update", "stable", "stale", "correction", "unstated"}


def test_generator_never_writes_a_fact_or_turn_for_an_unstated_slot():
    corpus = eb.generate(seed=7, **_GEN)
    unstated = {(q.entity, q.attribute) for q in corpus.questions
                if q.kind == "unstated"}
    assert unstated
    for w in corpus.fact_writes:
        assert (w.entity, w.attribute) not in unstated
    for q in corpus.questions:
        if q.kind != "unstated":
            continue
        # No turn may state a value for the slot, and the decoys must be
        # real values other entities hold on the same attribute — that is
        # what makes abstention non-trivial.
        assert q.decoy_values
        for turn in corpus.turns:
            assert not (eb.value_present(turn.text, q.entity)
                        and q.attribute in turn.text.lower()
                        and any(eb.value_present(turn.text, d)
                                for d in q.decoy_values))


def test_generator_marks_stale_slots_volatile_and_old_enough():
    corpus = eb.generate(seed=7, **_GEN)
    stale = {(q.entity, q.attribute) for q in corpus.questions
             if q.kind == "stale"}
    assert stale
    for w in corpus.fact_writes:
        if (w.entity, w.attribute) in stale:
            assert w.freshness_class == "volatile"
            assert _GEN["now"] - w.epoch > 2 * eb.VOLATILE_TTL_SECONDS
        else:
            assert w.freshness_class == "evergreen"


def test_generator_writes_facts_in_chronological_order():
    """cortex_write ticks the HLC per call, so call order IS supersession
    order — a generator that emitted writes out of order would build the
    wrong chain without any error."""
    corpus = eb.generate(seed=7, **_GEN)
    epochs = [w.epoch for w in corpus.fact_writes]
    assert epochs == sorted(epochs)


def test_generator_corrections_carry_the_retracted_value():
    corpus = eb.generate(seed=7, **_GEN)
    corrected = [q for q in corpus.questions if q.kind == "correction"]
    assert corrected
    for q in corrected:
        assert q.corrected_from
        assert q.corrected_from in q.superseded_values
        assert q.corrected_from != q.current_value


# ── the LongMemEval derivation ────────────────────────────────────────────
def _lme_q(qid, answer, early, late, n_evidence=2):
    """A minimal knowledge-update question in the dataset's own shape."""
    sessions = [[{"role": "user", "content": early, "has_answer": True}],
                [{"role": "user", "content": late, "has_answer": True}],
                [{"role": "user", "content": "unrelated chat",
                  "has_answer": False}]]
    ids = ["s_early", "s_late", "s_filler"]
    return {"question_id": qid, "question_type": "knowledge-update",
            "question": "what is it?", "answer": answer,
            "question_date": "2023/06/01 (Thu) 00:58",
            "haystack_dates": ["2023/05/25 (Thu) 20:21",
                               "2023/05/27 (Sat) 10:20",
                               "2023/05/26 (Fri) 08:00"],
            "haystack_session_ids": ids, "haystack_sessions": sessions,
            "answer_session_ids": ids[:n_evidence]}


def test_lme_derivation_accepts_a_clean_old_new_pair():
    q = _lme_q("ok1", "25:50",
               "my personal best was 27:12 back then",
               "I want to beat my personal best of 25:50")
    pairs, skips = eb.derive_lme_pairs([q])
    assert [p["question_id"] for p in pairs] == ["ok1"]
    assert pairs[0]["old_value"] == "27:12"
    assert pairs[0]["new_value"] == "25:50"
    assert pairs[0]["family"] == "time"
    assert skips == {}


def test_lme_derivation_skips_a_prose_gold_answer():
    q = _lme_q("prose", "the Nikon D850",
               "I shoot with a Canon", "I switched to the Nikon D850")
    pairs, skips = eb.derive_lme_pairs([q])
    assert pairs == []
    assert skips == {"gold-has-no-value-token": 1}


def test_lme_derivation_skips_an_ambiguous_old_value():
    q = _lme_q("amb", "220",
               "I lifted 200 then 180 that week",
               "I am up to 220 now")
    pairs, skips = eb.derive_lme_pairs([q])
    assert pairs == []
    assert list(skips) == ["ambiguous-old-value(2)"]


def test_lme_derivation_skips_when_the_gold_is_not_in_later_evidence():
    q = _lme_q("para", "220",
               "I lifted 200 that week", "I went up by twenty pounds")
    pairs, skips = eb.derive_lme_pairs([q])
    assert pairs == []
    assert skips == {"gold-not-in-later-evidence": 1}


def test_lme_derivation_skips_a_question_without_two_evidence_sessions():
    q = _lme_q("one", "220", "I lifted 200", "I am up to 220", n_evidence=1)
    pairs, skips = eb.derive_lme_pairs([q])
    assert pairs == []
    assert skips == {"not-two-evidence-sessions": 1}


def test_lme_derivation_ignores_non_knowledge_update_types():
    q = _lme_q("ok1", "25:50", "was 27:12", "now 25:50")
    q["question_type"] = "multi-session"
    pairs, skips = eb.derive_lme_pairs([q])
    assert pairs == [] and skips == {}


def test_lme_pair_becomes_a_question_that_scores_the_shared_dimensions():
    q = _lme_q("ok1", "25:50", "was 27:12", "now 25:50")
    pairs, _ = eb.derive_lme_pairs([q])
    question = eb.lme_question(pairs[0])
    assert question.current_value == "25:50"
    assert question.superseded_values == ("27:12",)
    assert eb.update_following(question, _served(text="25:50")) is True
    # Section 4 of the spec: the LME slice cannot score staleness or
    # abstention, so those dimensions must report not-applicable.
    assert eb.staleness_marking(question, _served()) is None
    assert eb.abstention_support(question, _served()) is None


# ── the artifact ──────────────────────────────────────────────────────────
def test_write_artifact_refuses_to_overwrite(tmp_path):
    path = tmp_path / "epistemic-bench-x.json"
    eb.write_artifact(path, {"meta": {"tag": "x"}}, rows=[{"a": 1}])
    with pytest.raises(SystemExit) as exc:
        eb.write_artifact(path, {"meta": {"tag": "x"}}, rows=[])
    assert "--force" in str(exc.value)


def test_write_artifact_force_overwrites_both_files(tmp_path):
    path = tmp_path / "epistemic-bench-x.json"
    eb.write_artifact(path, {"meta": {"tag": "x"}}, rows=[{"a": 1}])
    eb.write_artifact(path, {"meta": {"tag": "y"}}, rows=[{"a": 2}],
                      force=True)
    assert json.loads(path.read_text(encoding="utf-8"))["meta"]["tag"] == "y"
    rows = [json.loads(line) for line
            in path.with_suffix(".jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    assert rows == [{"a": 2}]


def test_write_artifact_refuses_when_only_the_rows_file_exists(tmp_path):
    """A half-written run must not be completable by a silent overwrite."""
    path = tmp_path / "epistemic-bench-x.json"
    path.with_suffix(".jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        eb.write_artifact(path, {"meta": {}}, rows=[])


# ── the LongMemEval source path ───────────────────────────────────────────
# Fixture-level like everything above: no database, no extractor, no model.
# The bank lifecycle itself is exercised by the CPU plumbing smoke
# (--extractor floor), which is a run, not a test.
def _pair(qid="q1", old="27:12", new="25:50"):
    """One row of the committed derivation artifact
    (evals/results/epistemic-bench-lme-derivation-20260905.jsonl)."""
    return {"question_id": qid, "family": "time", "old_value": old,
            "new_value": new, "old_date": "2023/05/25 (Thu) 20:21",
            "new_date": "2023/05/27 (Sat) 10:20",
            "question": "What was my personal best time in the 5K?"}


def test_lme_scoring_credits_the_new_value_as_update_following():
    q = eb.lme_question(_pair())
    served = _served(text="[2023/05/27] user: my new PB is 25:50.")
    assert eb.update_following(q, served) is True
    assert eb.stale_serving(q, served) is False


def test_lme_scoring_flags_the_old_value_alone_as_stale_serving():
    q = eb.lme_question(_pair())
    served = _served(text="[2023/05/25] user: my PB is 27:12.")
    assert eb.update_following(q, served) is False
    assert eb.stale_serving(q, served) is True


def test_lme_scoring_is_clean_when_both_values_are_served():
    """An agent can adjudicate two values; it cannot adjudicate one."""
    q = eb.lme_question(_pair())
    served = _served(text="[05/25] PB 27:12 ... [05/27] PB now 25:50.")
    assert eb.stale_serving(q, served) is False
    assert eb.update_following(q, served) is True


def test_lme_questions_report_the_ungradable_dimensions_with_a_zero_count():
    """Spec section 4: the slice grounds D1/D2/D5 and cannot ground D3/D4.
    Those must report n=0 and a NULL rate, never a 0.0 a reader would take
    for a failing arm."""
    qs = [eb.lme_question(_pair(qid=f"q{i}")) for i in range(3)]
    scored = eb.score_arm(qs, lambda q: _served(text=q.current_value))
    assert scored["update_following"]["n"] == 3
    assert scored["stale_serving"]["n"] == 3
    assert scored["retraction_handling"]["n"] == 3
    assert scored["staleness_marking"] == {"n": 0, "hits": 0, "rate": None}
    assert scored["abstention_support"] == {"n": 0, "hits": 0, "rate": None}


def test_lme_artifact_name_carries_the_source_without_stuttering():
    assert (eb.lme_out_path("plumbing").name
            == "epistemic-bench-lme-plumbing.json")
    assert (eb.lme_out_path("lme-plumbing").name
            == "epistemic-bench-lme-plumbing.json")


def test_lme_resume_reads_the_question_ids_already_in_the_rows_file(tmp_path):
    rows_path = tmp_path / "epistemic-bench-lme-x.jsonl"
    rows_path.write_text('{"question_id": "a"}\n\n{"question_id": "b"}\n',
                         encoding="utf-8")
    assert eb.done_question_ids(rows_path) == {"a", "b"}
    assert eb.done_question_ids(tmp_path / "missing.jsonl") == set()


def test_lme_limit_selects_the_same_slice_across_a_resume():
    """--limit counts questions in the SLICE, not questions still pending,
    so an interrupted run resumes onto the same slice instead of walking
    further down the derivation."""
    pairs = [_pair(qid=f"q{i}") for i in range(5)]
    assert [p["question_id"]
            for p in eb.lme_pending(pairs, set(), limit=3)] == ["q0", "q1",
                                                               "q2"]
    assert [p["question_id"]
            for p in eb.lme_pending(pairs, {"q0"}, limit=3)] == ["q1", "q2"]
    assert len(eb.lme_pending(pairs, set(), limit=None)) == 5


def test_lme_refuses_to_start_when_the_summary_already_exists(tmp_path):
    out = tmp_path / "epistemic-bench-lme-x.json"
    out.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        eb.guard_lme_artifact(out, force=False)
    assert "--force" in str(exc.value)


def test_lme_rows_file_alone_is_a_resume_not_a_refusal(tmp_path):
    """The synthetic path treats an orphaned rows file as a half-written
    run and blocks. The LME path shares the GPU and MUST resume from one."""
    out = tmp_path / "epistemic-bench-lme-x.json"
    out.with_suffix(".jsonl").write_text('{"question_id": "a"}\n',
                                         encoding="utf-8")
    eb.guard_lme_artifact(out, force=False)


def test_lme_aborts_when_the_extractor_endpoint_is_unreachable(monkeypatch):
    monkeypatch.setattr(eb, "extractor_url",
                        lambda name: "http://127.0.0.1:9/v1")
    monkeypatch.setattr(eb, "_probe", lambda url: False)
    with pytest.raises(SystemExit) as exc:
        eb.require_extractor("qwen-27b")
    msg = str(exc.value)
    assert "http://127.0.0.1:9/v1" in msg and "qwen-27b" in msg


def test_the_no_llm_floor_rung_needs_no_endpoint(monkeypatch):
    monkeypatch.setattr(eb, "_probe", lambda url: pytest.fail(
        "probed an endpoint for the no-LLM floor rung"))
    assert eb.require_extractor(eb.FLOOR_EXTRACTOR) is None


def test_source_lme_aborts_on_a_dead_endpoint_before_any_bank_work(tmp_path):
    """The probe is the first thing the run does: a dead endpoint must not
    cost a database, a dataset load, or a partial artifact."""
    import argparse

    args = argparse.Namespace(
        tag="never", extractor="qwen-27b", limit=None, force=False,
        derivation=str(tmp_path / "does-not-exist.json"), lme_path=None)
    orig_url, orig_probe = eb.extractor_url, eb._probe
    eb.extractor_url = lambda name: "http://127.0.0.1:9/v1"
    eb._probe = lambda url: False
    try:
        with pytest.raises(SystemExit) as exc:
            eb.run_lme(args)
    finally:
        eb.extractor_url, eb._probe = orig_url, orig_probe
    assert "extractor" in str(exc.value)


def test_scores_aggregate_from_persisted_rows_so_a_resume_totals_correctly():
    """A resumed run scores from the rows file, not from re-served
    contexts, so questions carried over from an earlier process still
    count in the summary."""
    rows = [{"rag_update_following": True, "rag_stale_serving": False,
             "rag_staleness_marking": None, "rag_context_chars": 100},
            {"rag_update_following": False, "rag_stale_serving": True,
             "rag_context_chars": 300}]
    scored = eb.score_from_rows(rows, "rag")
    assert scored["update_following"] == {"n": 2, "hits": 1, "rate": 0.5}
    assert scored["stale_serving"] == {"n": 2, "hits": 1, "rate": 0.5}
    assert scored["staleness_marking"] == {"n": 0, "hits": 0, "rate": None}
    assert scored["context_chars_mean"] == 200.0
