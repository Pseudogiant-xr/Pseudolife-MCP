"""Unit tests for the memory-only answerability probe + pathway evidence.

AWM (arXiv 2608.25618) asks: can the question be answered from the
agent's persisted memory ALONE, source context removed? Their headline —
42.5% of correct answers could not be reproduced from memory alone — is
exactly the failure end-to-end QA cannot see. PAST-Bench (arXiv
2608.04003) asks the sibling question per row: does a correct answer's
gold actually sit in a served context entry, so the save → retrieve → use
pathway is evidenced rather than assumed?

The probe is CPU-only re-parsing of committed artifacts, in both harness
shapes (LongMemEval ``*_correct`` booleans, BEAM ``*_score`` floats), and
must classify what it cannot test — no gold string, trivial gold, or a
row that predates context persistence — with reasons, never silently.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import answerability_probe as ap  # noqa: E402


# ── context blocks: each arm's served context, split back into the ──────
#    entries it was composed from (build_contexts / refind_search)

def test_rag_context_splits_on_blank_lines():
    ctx = "[2023/05/01] user: I bought a Trek Domane\n\n[2023/05/02] user: hi"
    blocks = ap.context_blocks("rag", ctx)
    assert len(blocks) == 2
    assert "Trek Domane" in blocks[0] and blocks[1].endswith("hi")


def test_refind_context_splits_like_rag():
    ctx = "turn one\n\nturn two\n\nturn three"
    assert len(ap.context_blocks("refind", ctx)) == 3


def test_cortex_context_splits_per_fact_line():
    ctx = ("user — bike: Trek Domane  (earlier values, oldest first: BMC)\n"
           "user — city: Sydney")
    blocks = ap.context_blocks("cortex", ctx)
    assert len(blocks) == 2
    assert blocks[1] == "user — city: Sydney"


def test_cortex_enum_render_keeps_continuation_lines_with_their_fact():
    """The enum fact render is multi-line with two-space continuations —
    they belong to the fact above, not to a block of their own."""
    ctx = ("user — bike: Trek Domane\n"
           "  earlier values, oldest first:\n"
           "  1. BMC Roadmachine (2023-01-05)\n"
           "user — city: Sydney")
    blocks = ap.context_blocks("cortex", ctx)
    assert len(blocks) == 2
    assert "BMC Roadmachine" in blocks[0]


def test_cortex_variant_arms_split_per_fact_line_too():
    """The cortex splitter was dispatched on an exact ``arm == "cortex"``
    match while the hybrid one beside it used a prefix, so any cortex
    VARIANT arm would fall through to the blank-line splitter and
    collapse to one block, silently gutting its pathway attribution. No
    such arm exists in a probed artifact today (``cortex_orig`` in the
    rejudge summary is a display key, not an arm), so this pins the
    symmetry for the next variant rather than a live miscount
    (2026-09-01 review of PR #236, scope corrected in its fix-pass)."""
    ctx = "fact one line\nfact two line\nfact three line"
    assert len(ap.context_blocks("cortex", ctx)) == 3
    assert len(ap.context_blocks("cortex_orig", ctx)) == 3


def test_hybrid_context_splits_facts_and_memories_sections():
    ctx = ("Known facts:\nuser — bike: Trek Domane\nuser — city: Sydney"
           "\n\nRelevant memories:\n[d1] user: mem one\n\n[d2] user: mem two")
    blocks = ap.context_blocks("hybrid", ctx)
    assert len(blocks) == 4
    assert blocks[0] == "user — bike: Trek Domane"
    assert blocks[2] == "[d1] user: mem one"


def test_context_blocks_splits_what_the_canonical_hybrid_join_produces():
    """The splitter and the producers must agree by construction, not by
    two people typing the same literal (2026-09-01 post-merge review of
    PR #236: the header literals had five uncoordinated copies)."""
    import context_format
    facts = ["user — bike: Trek Domane", "user — city: Sydney"]
    mems = ["[t1] user: I bought a Trek Domane",
            "[t2] user: I moved to Sydney"]
    ctx = context_format.hybrid_context(facts, mems)
    assert ap.context_blocks("hybrid", ctx) == facts + mems


def test_hybrid_join_is_byte_identical_to_the_literals_it_replaced():
    """The extraction has to be serving-inert: the producers must emit the
    same string the five copied literals built, character for character.
    Anything else shifts every persisted context and makes the committed
    accuracies incomparable — which is why this is pinned rather than
    checked by eye (the judged regression gate cannot run on this chip)."""
    import context_format
    facts, mems = ["user — bike: Trek", "user — city: Sydney"], ["m1", "m2"]
    assert context_format.FACTS_HEADER == "Known facts:\n"
    assert context_format.MEMS_HEADER == "\n\nRelevant memories:\n"
    assert context_format.hybrid_context(facts, mems) == (
        "Known facts:\n" + "\n".join(facts)
        + "\n\nRelevant memories:\n" + "\n\n".join(mems))


def test_hybrid_header_literals_have_exactly_one_home():
    """No module under ``evals/`` re-types the headers — the drift guard
    the five copies needed. Scoped to production modules on purpose:
    TESTS that hardcode the literal (this file's byte-identity pin,
    test_beam_adapter's served-format assertion) are independent checks
    of the format and would be worth less written against the constant
    they are checking."""
    import context_format
    evals_dir = Path(__file__).resolve().parents[1] / "evals"
    offenders = sorted(
        p.name for p in evals_dir.glob("*.py")
        if p.name != "context_format.py"
        and any(h.strip() in p.read_text(encoding="utf-8")
                for h in (context_format.FACTS_HEADER,
                          context_format.MEMS_HEADER)))
    assert offenders == []


def test_empty_context_has_no_blocks():
    assert ap.context_blocks("nomem", "") == []


# ── the containment ladder ──────────────────────────────────────────────
#    Whole-gold verbatim containment is a leak test, not an answerability
#    test: LongMemEval golds carry parenthetical variants and number
#    words, and BEAM golds are full sentences. Naive containment marks a
#    context that plainly says "25:50" unanswerable for the gold
#    "25 minutes and 50 seconds (or 25:50)" — inflating the AWM red-flag
#    cell with surface mismatches instead of missing memory.

def test_answerable_in_matches_a_parenthetical_gold_variant():
    ctx = "user — charity-5k-run-personal-best-time: 25:50"
    assert ap.answerable_in(ctx, "25 minutes and 50 seconds (or 25:50)") \
        == "span"


def test_answerable_in_normalizes_number_words():
    assert ap.answerable_in("user — short-stories-completed: 7",
                            "seven") == "span"


def test_answerable_in_tolerates_a_plural_mismatch():
    assert ap.answerable_in("cocktail-making class on Fridays",
                            "Friday") == "span"


def test_answerable_in_falls_back_to_content_token_coverage():
    """BEAM golds are sentences; the information can be present while the
    sentence never is. Full content-token coverage (stopwords dropped)
    answers the coverage question; the method is reported so the strict
    span level stays distinguishable."""
    ctx = ("[t1] user: my first sprint is planned\n\n"
           "[t2] user: the sprint ends on March 29")
    assert ap.answerable_in(ctx, "My first sprint ends on March 29.") \
        == "tokens"


def test_answerable_in_is_none_when_content_tokens_are_missing():
    assert ap.answerable_in("user: nothing relevant",
                            "My first sprint ends on March 29.") is None
    assert ap.answerable_in("", "Trek Domane") is None


def test_gold_variants_are_screened_for_triviality():
    """A gold like "Yes. (You have a road bike too.)" passes the whole-
    gold triviality screen, but stripping the parens yields the bare
    "Yes." — exactly the string TRIVIAL_ANSWERS exists to exclude. An
    unscreened variant lets any context containing the token "yes" score
    answerable (live in the committed corpus at 89941a94; 2026-09-01
    review)."""
    gold = "Yes. (You have a road bike too.)"
    assert ap.answerable_in("user: yes I did say that", gold) is None
    # the substantive parenthetical variant still matches
    assert ap.answerable_in("you have a road bike too", gold) == "span"


def test_stopwords_are_dropped_from_the_content_token_set():
    """The tokens rung requires only CONTENT tokens, so a gold's function
    words must not be demanded of the context."""
    assert ap.answerable_in("user — trip-booking-month: March, booked",
                            "This trip was booked in March.") == "tokens"


def test_stopword_exclusion_cannot_swallow_a_content_word():
    """The stopword set is matched against the RAW token, before folding.
    Folding it first collides function words with real nouns — "does"
    folds to "doe", which then excludes the genuine noun "doe" from the
    required-coverage set and scores a context that never mentions it
    answerable (2026-09-01 post-merge review of PR #236)."""
    assert ap.answerable_in(
        "user: I saw something on the trail yesterday",
        "You saw a doe on the trail") is None
    # the same context DOES answer a gold whose content it carries: only
    # the noun is missing above, not the function words around it
    assert ap.answerable_in(
        "user: I saw something on the trail yesterday",
        "You saw something on the trail") == "tokens"


@pytest.mark.parametrize("word, gold, ctx", [
    # "does" -> "doe": the folded stopword is itself a common noun
    ("doe", "A doe crossed the road", "user: something crossed the road"),
    # "this" -> "thi": guards the whole collision class, not one word
    ("thi", "The thi rating was high", "user: the rating was high"),
    # "always" is NOT a stopword, so its fold ("alway") must stay required
    ("alway", "The gate is always locked", "user: the gate is locked"),
])
def test_folded_stopword_collisions_do_not_relax_coverage(word, gold, ctx):
    assert word not in ap._STOPWORDS
    assert ap.answerable_in(ctx, gold) is None


def test_stopword_set_holds_raw_words_not_folded_ones():
    """A structural guard over the whole class: every entry is the word as
    written, so a future addition cannot re-introduce the collision."""
    assert {"does", "this", "has"} <= ap._STOPWORDS
    assert not ({"doe", "thi"} & ap._STOPWORDS)


@pytest.mark.parametrize("gold, ctx", [
    ("The decision is theirs", "user: they made the decision"),
    ("The choice was yours", "user: you made the choice"),
    ("The fault is ours", "user: we caused the fault"),
])
def test_inflected_function_words_stay_out_of_the_content_set(gold, ctx):
    """Matching the stopword set on the raw token fixes the "doe"
    collision, but the folded set it replaced was also absorbing
    INFLECTED function words — "theirs" folded onto the stopword "their".
    Requiring those instead moves rows into `unanswerable`, which inflates
    the AWM red-flag cell with exactly the surface mismatches the ladder
    exists to keep out of it. So the exclusion tests the raw token AND its
    depluralized stem (2026-09-01 review of the chip 2.1 fix-pass)."""
    assert ap.answerable_in(ctx, gold) == "tokens"


def test_the_stem_test_still_keeps_real_content_words():
    """The stem test must not become a second swallowing mechanism: a word
    is only dropped when the STEM is a function word, so "doe"/"thi"
    (whose stems are themselves) and "always" (stem "alway") all stay
    required."""
    assert ap.answerable_in("user: something crossed the road",
                            "A doe crossed the road") is None
    assert ap.answerable_in("user: the gate is locked",
                            "The gate is always locked") is None


def test_number_words_cover_the_full_dream_table():
    """The probe mirrors dream.py's _SPELLED_NUMBERS rather than importing
    it (importing pseudolife_memory.memory.dream pulls torch through the
    package __init__, and the probe must stay CPU-only). Re-typed, it
    stopped at twenty while dream.py reaches ninety plus hundred/thousand
    — so "thirty minutes" scored unanswerable against a context saying
    "30 minutes" (2026-09-01 post-merge review of PR #236). This is the
    sync guard the mirror needs."""
    from pseudolife_memory.memory.dream import _SPELLED_NUMBERS
    assert ap._NUMBER_WORDS == _SPELLED_NUMBERS


def test_answerable_in_normalizes_number_words_above_twenty():
    assert ap.answerable_in("note: the ride took 30 minutes",
                            "thirty minutes") == "span"
    assert ap.answerable_in("note: a 1000 word draft",
                            "thousand word draft") == "span"


def test_answerable_in_folds_plural_number_words():
    """The plural-s strip must run BEFORE the number-word fold: folded
    first, "sevens" becomes "seven" while a bare "seven" becomes "7", so
    the two spellings of the same number stopped matching each other
    (2026-09-01 post-merge review of PR #236)."""
    assert ap._tokens("sevens") == ap._tokens("seven") == ["7"]
    assert ap.answerable_in("he rolled two sevens in a row",
                            "Seven") == "span"


def test_gold_variants_strip_punctuated_ie_and_eg():
    """The variant lead-in strip runs on raw paren text, so it must
    match "i.e." and "e.g." WITH their dots (2026-09-01 review)."""
    assert ap.answerable_in("we meet at 6:30 sharp",
                            "half past six (i.e. 6:30)") == "span"
    assert ap.answerable_in("bring a Trek Domane",
                            "a road bike (e.g. Trek Domane)") == "span"


def test_classify_records_the_answerable_method():
    row = _lme_row("a", "user: I bought a Trek Domane", "Trek Domane", True)
    assert ap.classify(row, "rag")["answerable_method"] == "span"


def test_probe_rows_counts_answerable_by_method():
    rows = [_lme_row("a", "user: I bought a Trek Domane", "Trek Domane",
                     True),
            _lme_row("b", "user: a Trek\n\nuser: the Domane", "Trek Domane",
                     True)]
    summary = ap.probe_rows(rows)
    assert summary["arms"]["rag"]["answerable_by"] == {"span": 1,
                                                       "tokens": 1}


# ── correctness in both harness shapes ──────────────────────────────────

def test_row_correct_reads_lme_booleans_and_beam_scores():
    assert ap.row_correct({"rag_correct": True}, "rag") is True
    assert ap.row_correct({"rag_correct": False}, "rag") is False
    assert ap.row_correct({"rag_score": 1.0}, "rag") is True
    # Partial rubric credit is NOT a correct answer for the cross-tab —
    # the threshold is recorded in the summary so the binarization is
    # auditable.
    assert ap.row_correct({"rag_score": 0.5}, "rag") is False
    assert ap.row_correct({"rag_score": 0.0}, "rag") is False
    assert ap.row_correct({"question": "q"}, "rag") is None


# ── per-row classification ──────────────────────────────────────────────

def _lme_row(qid, ctx, gold, correct, question="Which bike did I buy?"):
    return {"question_id": qid, "question": question, "answer": gold,
            "contexts": {"rag": ctx}, "rag_correct": correct,
            "gold_in_question": False}


def test_classify_answerable_cells():
    good = ap.classify(_lme_row("a", "user: I bought a Trek Domane",
                                "Trek Domane", True), "rag")
    assert good["testable"] and good["answerable"] is True
    assert good["cell"] == "answerable_correct"
    bad = ap.classify(_lme_row("b", "user: I bought a Trek Domane",
                               "Trek Domane", False), "rag")
    assert bad["cell"] == "answerable_wrong"


def test_classify_red_flag_cell_is_unanswerable_but_correct():
    """The AWM red flag: the arm answered correctly while its served
    context does not contain the gold — the answer has no memory
    support."""
    v = ap.classify(_lme_row("c", "user: nothing relevant here",
                             "Trek Domane", True), "rag")
    assert v["cell"] == "unanswerable_correct"


def test_classify_untestable_reasons_gold_first():
    """A missing/trivial gold makes the row untestable regardless of the
    context, so the gold reasons take precedence (the leak_check
    taxonomy); only a testable gold with no persisted context reports
    no_context."""
    no_gold = ap.classify({"question_id": "a", "question": "summarise",
                           "answer": "", "rag_score": 1.0}, "rag")
    assert not no_gold["testable"] and no_gold["reason"] == "no_gold"
    trivial = ap.classify({"question_id": "b", "question": "did you?",
                           "answer": "yes", "rag_score": 1.0}, "rag")
    assert trivial["reason"] == "trivial_gold"
    no_ctx = ap.classify({"question_id": "c", "question": "which bike?",
                          "answer": "Trek Domane", "rag_score": 1.0}, "rag")
    assert no_ctx["reason"] == "no_context"


def test_classify_abstention_rows_are_untestable():
    """An abstention question's gold names an ABSENCE ("the information
    provided is not enough") and correct means abstaining — a right
    abstention with no memory support is the designed outcome, not the
    AWM red flag. Containment cannot test an absence, so these rows are
    classified out with their own reason instead of polluting the
    red-flag cell."""
    row = _lme_row("q_abs", "user: filler",
                   "The information provided is not enough. You mentioned "
                   "tennis but not table tennis.", True)
    row["abstention"] = True
    v = ap.classify(row, "rag")
    assert not v["testable"] and v["reason"] == "abstention"
    assert ap.pathway(row, "rag") is None


def test_classify_context_free_arms_are_untestable():
    """A context-free arm (nomem) is unanswerable BY CONSTRUCTION — a
    correct nomem answer is the arm's accuracy, not an AWM red flag.
    Counting it in unanswerable_correct would publish the memory-off
    arm's score as a memory-support failure (2026-09-01 review). Same
    treatment as abstention: classified out with its own reason."""
    row = {"question_id": "n", "question": "which bike?",
           "answer": "Trek Domane", "contexts": {"nomem": ""},
           "nomem_correct": True}
    v = ap.classify(row, "nomem")
    assert not v["testable"] and v["reason"] == "context_free_arm"
    assert ap.pathway(row, "nomem") is None


def test_classify_empty_context_on_a_memory_arm_is_unanswerable():
    """An empty string served by a MEMORY arm (e.g. cortex extracted
    nothing) is a real served context and a genuine storage failure —
    testable, unanswerable."""
    row = {"question_id": "e", "question": "which bike?",
           "answer": "Trek Domane", "contexts": {"cortex": ""},
           "cortex_correct": False}
    v = ap.classify(row, "cortex")
    assert v["testable"] and v["answerable"] is False
    assert v["cell"] == "unanswerable_wrong"


# ── pathway evidence (PAST-Bench) ───────────────────────────────────────

def test_pathway_supported_records_the_gold_bearing_entries():
    row = _lme_row("a", "user: filler turn\n\nuser: I bought a Trek Domane",
                   "Trek Domane", True)
    ev = ap.pathway(row, "rag")
    assert ev["verdict"] == "supported"
    assert ev["n_context_entries"] == 2 and ev["gold_entries"] == [1]


def test_pathway_unsupported_when_no_entry_carries_the_gold():
    row = _lme_row("b", "user: filler\n\nuser: more filler",
                   "Trek Domane", True)
    ev = ap.pathway(row, "rag")
    assert ev["verdict"] == "unsupported" and ev["gold_entries"] == []


def test_pathway_spanning_when_the_gold_straddles_a_block_boundary():
    """Gold in the full context but in no single block: either the gold
    genuinely spans entries or the splitter over-split a block. Reported
    as its own verdict, excluded from both supported and unsupported —
    never silently miscounted as a pathway failure."""
    row = _lme_row("c", "user: it was a Trek\n\nDomane, yes",
                   "Trek Domane", True)
    ev = ap.pathway(row, "rag")
    assert ev["verdict"] == "spanning"


def test_pathway_is_only_computed_for_correct_answers():
    assert ap.pathway(_lme_row("d", "ctx", "Trek Domane", False),
                      "rag") is None


# ── the summary over rows ───────────────────────────────────────────────

def _rows():
    return [
        # answerable + correct, pathway supported
        _lme_row("a", "user: I bought a Trek Domane", "Trek Domane", True),
        # answerable + wrong (answering failure)
        _lme_row("b", "user: I moved to Sydney", "Sydney", False,
                 question="Where did I move?"),
        # unanswerable + correct (the AWM red flag)
        _lme_row("c", "user: filler", "Nikon Zf", True,
                 question="Which camera did I buy?"),
        # unanswerable + wrong (storage/retrieval failure)
        _lme_row("d", "user: filler", "Fuji X100", False,
                 question="Which compact did I buy?"),
    ]


def test_probe_rows_cross_tab_counts_all_four_cells():
    summary = ap.probe_rows(_rows())
    arm = summary["arms"]["rag"]
    assert arm["cells"] == {"answerable_correct": 1, "answerable_wrong": 1,
                            "unanswerable_correct": 1,
                            "unanswerable_wrong": 1}
    assert arm["n_testable"] == 4
    assert arm["answerable_share"] == 0.5
    assert arm["unanswerable_correct_ids"] == ["c"]
    assert summary["arms"]["rag"]["pathway"]["supported"] == 1
    assert summary["arms"]["rag"]["pathway"]["supported_share"] == 0.5


def test_probe_rows_red_flag_reconciles_with_the_leak_flag():
    """One way the red-flag cell fills is the question naming its own
    gold (the SR-TTT leak): the arm echoes the question with no memory
    support. Those rows are counted within the cell, not beside it."""
    rows = [
        _lme_row("a", "user: filler", "Nikon Zf", True,
                 question="Since the Nikon Zf arrived, happy?"),
        _lme_row("b", "user: filler", "Fuji X100", True),
    ]
    rows[0]["gold_in_question"] = True
    summary = ap.probe_rows(rows)
    arm = summary["arms"]["rag"]
    assert arm["cells"]["unanswerable_correct"] == 2
    assert arm["red_flag_leak_explained"] == 1


def test_probe_rows_classifies_context_less_rows_untestable_with_reasons():
    """The committed 2026-08-21 BEAM artifact persists no contexts at all
    — 400 rows the probe must report as untestable, never skip or count."""
    rows = [{"chat_id": "1", "type": "recall", "index": 0,
             "question": "which bike?", "reference_answer": "Trek Domane",
             "rag_score": 1.0},
            {"chat_id": "1", "type": "summarization", "index": 0,
             "question": "summarise", "reference_answer": "",
             "rag_score": 0.5}]
    summary = ap.probe_rows(rows)
    arm = summary["arms"]["rag"]
    assert arm["n_testable"] == 0
    assert arm["untestable_reasons"] == {"no_context": 1, "no_gold": 1}
    assert summary["beam_correct_threshold"] == 1.0
    # n_partial counts over ALL score rows, testable or not — the pathway
    # block's n_correct is also unconditioned, and a binarization count
    # scoped narrower than the binarized count it audits reports 0 while
    # partial credit is being floored (2026-09-01 review).
    assert arm["n_partial"] == 1


def test_probe_rows_counts_partial_scores_and_unjudged_rows():
    rows = [
        {"question_id": "a", "question": "which bike?",
         "answer": "Trek Domane",
         "contexts": {"rag": "user: a Trek Domane"}, "rag_score": 0.5},
        # extract-phase row: context persisted, not yet judged
        {"question_id": "b", "question": "which bike?",
         "answer": "Trek Domane",
         "contexts": {"rag": "user: a Trek Domane"}},
    ]
    summary = ap.probe_rows(rows)
    arm = summary["arms"]["rag"]
    assert arm["n_partial"] == 1
    assert arm["n_unjudged"] == 1
    assert arm["cells"]["answerable_wrong"] == 1     # partial != correct
    assert arm["answerable_share"] == 1.0            # answerability needs no verdict


def test_probe_rows_tolerates_rows_that_disagree_about_contexts():
    """A resumed artifact may mix pre- and post-persistence rows; each row
    classifies on its own instead of the probe dying or skipping."""
    rows = [_lme_row("a", "user: a Trek Domane", "Trek Domane", True),
            {"question_id": "old", "question": "which bike?",
             "answer": "Trek Domane", "rag_correct": True}]
    summary = ap.probe_rows(rows)
    arm = summary["arms"]["rag"]
    assert arm["n_testable"] == 1
    assert arm["untestable_reasons"] == {"no_context": 1}


def test_probe_rows_emits_per_row_pathway_evidence():
    summary = ap.probe_rows(_rows())
    ev = {(e["id"], e["arm"]): e for e in summary["pathway_evidence"]}
    assert ev[("a", "rag")]["verdict"] == "supported"
    assert ev[("a", "rag")]["gold_entries"] == [0]
    assert ev[("c", "rag")]["verdict"] == "unsupported"
    assert ("b", "rag") not in ev                    # wrong answers carry none


def test_report_block_summarises_without_the_per_row_evidence():
    block = ap.report_block(_rows())
    assert block["arms"]["rag"]["cells"]["unanswerable_correct"] == 1
    assert "pathway_evidence" not in block


def test_report_block_is_none_when_no_row_has_contexts():
    rows = [{"question_id": "a", "question": "q", "answer": "Trek Domane",
             "rag_correct": True}]
    assert ap.report_block(rows) is None


def test_report_block_fails_loudly_when_the_arm_set_disagrees_with_row_0():
    """report_block discovers arms by unioning across ALL rows, while both
    harnesses' accuracy tables derive theirs from rows[0]. A file resumed
    with different arm flags would then publish an answerability block
    covering arms the accuracy table beside it omits, silently. report()
    already exits on that disagreement; the block must not be quieter
    (2026-09-01 post-merge review of PR #236)."""
    rows = [_lme_row("a", "user: a Trek Domane", "Trek Domane", True),
            _lme_row("b", "user: a Trek Domane", "Trek Domane", True)]
    rows[1]["hybrid_correct"] = True
    rows[1]["contexts"]["hybrid"] = "Known facts:\nuser — bike: Trek Domane"
    with pytest.raises(SystemExit) as excinfo:
        ap.report_block(rows)
    assert "hybrid" in str(excinfo.value)


def test_report_block_accepts_an_artifact_whose_rows_agree_on_arms():
    """The guard must not fire on the legitimate resumed-artifact shape.
    The context-less row goes FIRST, which is the case that could trip
    the guard: rows[0] carries no `contexts` at all, so the arm set has
    to be recovered from its `rag_correct` verdict alone."""
    rows = [{"question_id": "old", "question": "which bike?",
             "answer": "Trek Domane", "rag_correct": True},
            _lme_row("a", "user: a Trek Domane", "Trek Domane", True)]
    assert ap.report_block(rows)["arms"]["rag"]["n_testable"] == 1


# ── the judge-based level: wired, never run here ────────────────────────

def test_judge_fields_are_registered_judged_fields():
    """`{arm}_answerable_judge` is written by a judge call, so replicate
    and rebuild_contexts must strip it like any other verdict."""
    import replicate
    assert replicate.is_judge_field("rag_answerable_judge")
    assert replicate.is_judge_field("hybrid_ctg_answerable_judge")
    assert not replicate.is_judge_field("answerable_share")


def test_judge_suffix_is_the_one_replicate_registered():
    """The suffix was hardcoded independently in the probe, in
    replicate._JUDGE_SUFFIXES and in the tests, tied only by a comment —
    so a rename in one place would leave the strippers blind to the field
    they are supposed to clear (2026-09-01 post-merge review of PR #236).
    This is the assertion that links them."""
    import replicate
    assert ap.JUDGE_SUFFIX in replicate._JUDGE_SUFFIXES
    assert replicate.is_judge_field(f"rag_{ap.JUDGE_SUFFIX}")


def test_annotate_judge_writes_verdicts_and_is_resumable():
    calls = []

    def fake_chat(system, user):
        calls.append(user)
        return "yes" if "Trek" in user else "no"

    rows = [_lme_row("a", "user: a Trek Domane", "Trek Domane", True),
            _lme_row("b", "user: filler", "Nikon Zf", True)]
    rows[0]["rag_answerable_judge"] = True            # already judged: skipped
    done = ap.annotate_judge(rows, chat=fake_chat)
    assert done == 1                                  # only row b was judged
    assert rows[1]["rag_answerable_judge"] is False
    assert len(calls) == 1


def test_annotate_judge_skips_untestable_rows():
    rows = [{"question_id": "a", "question": "summarise", "answer": "",
             "contexts": {"rag": "ctx"}, "rag_correct": True}]
    assert ap.annotate_judge(rows, chat=lambda s, u: "yes") == 0
    assert "rag_answerable_judge" not in rows[0]


def test_probe_rows_reports_judge_cells_when_rows_carry_them():
    rows = _rows()
    for r in rows:
        r["rag_answerable_judge"] = True
    summary = ap.probe_rows(rows)
    judge = summary["arms"]["rag"]["judge"]
    assert judge["n_judged"] == 4
    assert judge["cells"]["answerable_correct"] == 2


def test_main_with_judge_fails_fast_when_the_server_is_absent(
        tmp_path, monkeypatch):
    """Chip-1 discipline: probe the server BEFORE any work, so a judged
    pass cannot die mid-run. No server, no row touched."""
    src = tmp_path / "rows.jsonl"
    src.write_text(json.dumps(_lme_row("a", "ctx", "Trek Domane", True)),
                   encoding="utf-8")
    before = src.read_text(encoding="utf-8")
    monkeypatch.setattr(ap, "_server_alive", lambda url: False)
    with pytest.raises(SystemExit):
        ap.main(["--in", str(src), "--judge"])
    assert src.read_text(encoding="utf-8") == before


def test_main_with_judge_rejects_a_json_list_input(tmp_path, monkeypatch):
    """annotate_judge persists via rewrite_rows, which writes JSONL —
    running it against a .json LIST artifact would silently rewrite a
    canonical file in a different format (2026-09-01 review)."""
    src = tmp_path / "rows.json"
    src.write_text(json.dumps([_lme_row("a", "ctx", "Trek Domane", True)]),
                   encoding="utf-8")
    monkeypatch.setattr(ap, "_server_alive", lambda url: True)
    with pytest.raises(SystemExit):
        ap.main(["--in", str(src), "--judge"])


# ── CLI ─────────────────────────────────────────────────────────────────

def test_main_writes_the_probe_artifact(tmp_path):
    src = tmp_path / "rows.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in _rows()),
                   encoding="utf-8")
    out = tmp_path / "probe.json"
    assert ap.main(["--in", str(src), "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["source"] == "rows.jsonl"
    assert report["arms"]["rag"]["cells"]["unanswerable_correct"] == 1
    assert report["arms"]["rag"]["pathway"]["supported_share"] == 0.5


def test_main_defaults_the_artifact_beside_the_input(tmp_path):
    src = tmp_path / "longmemeval-ku-oracle-qwen-27b-x.jsonl"
    src.write_text(json.dumps(_rows()[0]), encoding="utf-8")
    assert ap.main(["--in", str(src)]) == 0
    assert (tmp_path
            / "longmemeval-ku-oracle-qwen-27b-x.answerability.json").exists()


# ── committed artifacts: the probe must reproduce its own artifacts ─────

REPO = Path(__file__).resolve().parents[1]
COMMITTED = [
    "evals/results/beam-100K-qwen-27b-beam100k-qwen38",
    "evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-e2e",
    "evals/results/beam-100K-qwen-27b-refind-smoke",
    "evals/results/longmemeval-ku-oracle-qwen-27b-refind-smoke",
]


@pytest.mark.parametrize("stem", COMMITTED)
def test_committed_probe_artifacts_regenerate_exactly(stem):
    """CPU-only re-parsing: the committed probe artifact and a fresh run
    over its source rows must agree byte-for-byte on content. A drift in
    either goes red (the leak_check regeneration discipline)."""
    src = REPO / f"{stem}.jsonl"
    committed = REPO / f"{stem}.answerability.json"
    rows = ap.load_rows(src)
    fresh = ap.probe_rows(rows, source=src.name)
    assert fresh == json.loads(committed.read_text(encoding="utf-8"))


def test_redflag_audit_stays_in_sync_with_the_probe_artifact():
    """The committed manual audit covers exactly the probe artifact's
    unanswerable_correct arm-rows — a regenerated probe that changes the
    red-flag set without a re-audit goes red here instead of shipping an
    audit that no longer covers what it claims to."""
    stem = REPO / "evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-e2e"
    probe = json.loads(
        (stem.parent / (stem.name + ".answerability.json"))
        .read_text(encoding="utf-8"))
    audit = json.loads(
        (stem.parent / (stem.name + ".redflag-audit.json"))
        .read_text(encoding="utf-8"))
    flagged = {(qid, arm) for arm, a in probe["arms"].items()
               for qid in a["unanswerable_correct_ids"]}
    audited = {(e["id"], e["arm"]) for e in audit["entries"]}
    assert audited == flagged
    assert audit["n_arm_rows"] == len(audit["entries"])
    assert audit["n_questions"] == len({e["id"] for e in audit["entries"]})
    assert all(e["verdict"] == "inference_gap" and e["served_evidence"]
               for e in audit["entries"])


# ── harness report wiring: one implementation, both harnesses ───────────

def test_beam_report_carries_the_answerability_block(tmp_path, monkeypatch):
    import beam_adapter

    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "recall", "index": i,
             "question": "which bike?", "reference_answer": "Trek Domane",
             "contexts": {"rag": "user: a Trek Domane" if i else "filler"},
             "rag_score": 1.0, "rag_score_intfaithful": 1.0}
            for i in range(2)]
    out = tmp_path / "beam-100K-qwen-27b-ans.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "ans")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-ans.summary.json").read_text(
            encoding="utf-8"))
    arm = summary["answerability"]["arms"]["rag"]
    assert arm["cells"]["answerable_correct"] == 1
    assert arm["cells"]["unanswerable_correct"] == 1


def test_beam_report_omits_the_block_for_context_less_rows(
        tmp_path, monkeypatch):
    import beam_adapter

    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "recall", "index": 0,
             "question": "q", "reference_answer": "Trek Domane",
             "rag_score": 1.0, "rag_score_intfaithful": 1.0}]
    out = tmp_path / "beam-100K-qwen-27b-old.jsonl"
    out.write_text(json.dumps(rows[0]), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "old")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-old.summary.json").read_text(
            encoding="utf-8"))
    assert "answerability" not in summary


def test_lme_report_carries_the_answerability_block(tmp_path, monkeypatch):
    import longmemeval_bench as lme

    monkeypatch.setattr(lme, "RESULTS_DIR", tmp_path)
    rows = []
    for i, (ctx, correct) in enumerate(
            [("user: a Trek Domane", True), ("filler", True)]):
        r = _lme_row(f"q{i}", ctx, "Trek Domane", correct)
        r.update({"question_date": "2023/05/27", "rag_context_tokens": 10,
                  "cortex_correct": False, "cortex_context_tokens": 1,
                  "hybrid_correct": correct, "hybrid_context_tokens": 5,
                  "consolidation": {"superseded": 0}})
        r["contexts"].update({"cortex": "", "hybrid": ""})
        rows.append(r)
    out = tmp_path / "longmemeval-ku-oracle-qwen-27b-ans.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    lme.report("oracle", "qwen-27b", "ans")
    summary = json.loads(
        (tmp_path / "longmemeval-ku-oracle-qwen-27b-ans.summary.json")
        .read_text(encoding="utf-8"))
    arm = summary["answerability"]["arms"]["rag"]
    assert arm["cells"]["answerable_correct"] == 1
    assert arm["cells"]["unanswerable_correct"] == 1
    assert summary["answerability"]["arms"]["rag"]["pathway"]["supported"] == 1
