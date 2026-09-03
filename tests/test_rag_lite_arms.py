"""Token-matched rag arms (rag1, rag2, ragb<N>) across both harnesses.

Every comparison published before 2026-09-04 pitted a ~160-token fact
context against a ~1,200-token raw-turn context and reported accuracy and
tokens as two findings. These arms serve the rag control's EXACT
retrieval, ranking and formatting at a narrower budget, so the two read as
one trade-off. That only holds if the arms are strict prefixes of the
control's context — which is what most of this file pins.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import longmemeval_bench as lmb  # noqa: E402
import replicate  # noqa: E402


class _StubSvc:
    """Ranked turns of differing length, so a token budget can bite.

    The texts are deliberately NOT in sorted order: an arm that re-sorted
    or re-ranked its slice would still pass a prefix check over an
    already-sorted list, and the whole claim is that these arms change the
    budget and nothing else.
    """

    _NAMES = ("mango", "apple", "zebra", "kiwi", "cherry", "banana")

    def __init__(self, texts=None):
        self.texts = texts or [f"turn {n} " + "x" * (20 * (i + 1))
                               for i, n in enumerate(self._NAMES)]

    def search(self, q, top_k, **kw):
        return {"entries": [{"text": t} for t in self.texts[:top_k]]}

    def cortex_search(self, q, **kw):
        return {"entries": []}


@pytest.fixture
def rag_lite_off():
    """Restore the module globals: they are bench knobs, read at call time."""
    old = (lmb.RAG_LITE_TOP_KS, lmb.RAG_BUDGET_TOKENS)
    yield
    lmb.RAG_LITE_TOP_KS, lmb.RAG_BUDGET_TOKENS = old


# ── the prefix contract ───────────────────────────────────────────────────
def test_top_k_arms_are_strict_prefixes_of_the_rag_context(rag_lite_off):
    lmb.RAG_LITE_TOP_KS = (1, 2)
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    assert set(ctx) == {"rag", "cortex", "hybrid", "rag1", "rag2"}
    for arm, k in (("rag1", 1), ("rag2", 2)):
        assert ctx["rag"].startswith(ctx[arm])
        assert len(ctx[arm]) < len(ctx["rag"])
        # same ranking, same separator, same formatting — only fewer turns
        assert ctx[arm].count("\n\n") == k - 1
    # the served order is the RANKING, not any re-sort of the slice
    assert ctx["rag1"].startswith("turn mango")
    assert ctx["rag2"].split("\n\n") == ctx["rag"].split("\n\n")[:2]
    assert ctx["rag2"].startswith(ctx["rag1"])


def test_budget_arm_is_a_prefix_that_fits_the_budget(rag_lite_off):
    lmb.RAG_BUDGET_TOKENS = 40
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    served = ctx["ragb40"]
    assert ctx["rag"].startswith(served)
    assert lmb.approx_tokens(served) <= 40
    # ...and it is maximal: adding the next ranked turn would overflow
    turns = ctx["rag"].split("\n\n")
    kept = served.split("\n\n")
    assert len(kept) < len(turns)
    assert lmb.approx_tokens("\n\n".join(turns[:len(kept) + 1])) > 40


def test_budget_arm_always_serves_at_least_one_turn(rag_lite_off):
    """An arm that can serve empty is a second no-memory control wearing a
    budget's name — the delta would then measure abstention, not budget.

    The price of that floor, stated once here so a reader of the artifact
    is not surprised: when the top-ranked turn alone is longer than the
    budget, the arm OVERSHOOTS rather than serving nothing, and the row's
    recorded cost says so.
    """
    lmb.RAG_BUDGET_TOKENS = 1
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    assert ctx["ragb1"] == ctx["rag"].split("\n\n")[0]
    assert lmb.approx_tokens(ctx["ragb1"]) > 1        # the documented floor


def test_budget_arm_bounds_the_number_the_row_records(rag_lite_off):
    """The budget is measured on the JOINED block, because that is the
    string whose approx_tokens the row persists as {arm}_context_tokens.

    Summing per-turn estimates instead loses both the separators and the
    floor division's remainders, so it admits one turn too many and ships
    a row whose recorded cost exceeds the budget it claims. Sized to make
    exactly that difference decide: six 21-char turns sum to 30 per-turn
    tokens but 34 as one block, so a 32-token budget must serve five."""
    texts = ["y" * 21] * 6
    lmb.RAG_BUDGET_TOKENS = 32
    ctx = lmb.build_contexts(_StubSvc(texts), "q?")
    assert sum(lmb.approx_tokens(t) for t in texts) <= 32 < lmb.approx_tokens(
        "\n\n".join(texts))
    row = {"question": "q", "answer": "a", "question_date": "d",
           "question_type": "knowledge-update",
           "contexts": {"ragb32": ctx["ragb32"]}}
    judged = _answer_with_stub(row)
    assert judged["ragb32_context_tokens"] <= 32
    assert ctx["ragb32"].count("\n\n") == 4          # five turns, not six


def test_whole_ranking_fits_when_the_budget_is_generous(rag_lite_off):
    lmb.RAG_BUDGET_TOKENS = 100000
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    assert ctx["ragb100000"] == ctx["rag"]


def test_rag_lite_is_inert_by_default():
    """A vanilla run must stay byte-identical: no new context keys."""
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    assert set(ctx) == {"rag", "cortex", "hybrid"}
    assert lmb.rag_lite_contexts(["a", "b"], (), None) == {}


def test_arms_are_served_in_the_same_order_both_harnesses_name_them():
    assert lmb.rag_lite_arm_names((1, 2), 300) == ("rag1", "rag2", "ragb300")
    assert lmb.rag_lite_arm_names((), None) == ()


# ── config validation ─────────────────────────────────────────────────────
@pytest.mark.parametrize("top_ks,budget,match", [
    ((0,), None, "must be positive"),
    ((6,), None, "not narrower"),
    ((7,), None, "not narrower"),
    ((1, 1), None, "lists a width twice"),
    ((), 0, "must be positive"),
])
def test_bad_configs_are_refused(top_ks, budget, match):
    with pytest.raises(SystemExit, match=match):
        lmb.validate_rag_lite(top_ks, budget, 6)


def test_parse_top_ks():
    assert lmb.parse_rag_lite_top_ks("1,2") == (1, 2)
    assert lmb.parse_rag_lite_top_ks("") == ()
    assert lmb.parse_rag_lite_top_ks(None) == ()
    with pytest.raises(SystemExit, match="comma-separated"):
        lmb.parse_rag_lite_top_ks("1,two")


def test_answer_phase_refuses_the_context_building_flags(monkeypatch):
    """--phase answer replays persisted contexts, so these flags would do
    nothing at all — silently, and the table would read as if they had."""
    monkeypatch.setattr(sys, "argv",
                        ["longmemeval_bench.py", "--phase", "answer",
                         "--rag-lite-top-k", "1"])
    with pytest.raises(SystemExit):
        lmb.main()


# ── the arms survive the answer/report/replicate path ─────────────────────
def _answer_with_stub(row):
    import longmemeval_bench as m
    calls = []

    def fake_chat(system, prompt, max_tokens=256, **_):
        calls.append(prompt)
        return "yes"

    real = m._chat
    m._chat = fake_chat
    try:
        return m.answer_and_judge(row)
    finally:
        m._chat = real


def test_answer_and_judge_judges_every_rag_lite_arm(rag_lite_off):
    lmb.RAG_LITE_TOP_KS = (1,)
    lmb.RAG_BUDGET_TOKENS = 40
    ctx = lmb.build_contexts(_StubSvc(), "q?")
    row = {"question": "q", "answer": "a", "question_date": "d",
           "question_type": "knowledge-update", "contexts": ctx}
    out = _answer_with_stub(row)
    for arm in ("rag", "rag1", "ragb40"):
        assert out[f"{arm}_correct"] is True
        assert out[f"{arm}_context_tokens"] >= 1
    # the budget arm costs strictly less than the control it truncates
    assert out["ragb40_context_tokens"] < out["rag_context_tokens"]
    assert out["rag1_context_tokens"] < out["rag_context_tokens"]


def _judged_row(qid, **arms):
    row = {"question_id": qid, "cortex_response": "the answer",
           "rag_context_tokens": 300, "cortex_context_tokens": 40,
           "hybrid_context_tokens": 340}
    for arm, ok in arms.items():
        row[f"{arm}_correct"] = ok
        row.setdefault(f"{arm}_context_tokens", 100)
        row[f"{arm}_response"] = "x"
    return row


def _rows(**arms):
    return [_judged_row(f"q{i}", **arms) for i in range(3)]


def test_replicate_aggregates_and_strips_the_new_arms():
    """PR #235 fixed exactly this class for refind/nomem: a comparator arm
    whose keys reach agg/compare/strip must not KeyError, and must not be
    left behind as a stale verdict on a stripped replicate."""
    rows = _rows(rag=True, cortex=False, hybrid=True, rag1=True, ragb300=False)
    assert "rag1" in replicate.row_arms(rows[0])
    agg = replicate.aggregate({"t1": rows, "t2": rows})
    for arm in ("rag", "cortex", "hybrid", "cascade", "rag1", "ragb300"):
        assert agg["arms"][arm]["mean"] is not None
    stripped = replicate.strip_judged(rows)
    assert not any(k.startswith(("rag1_", "ragb300_")) for k in stripped[0])
    assert replicate.judged_arms(rows) == (
        "rag", "cortex", "hybrid", "cascade", "rag1", "ragb300")


def test_gate_verdict_ignores_arms_the_baseline_never_carried():
    """A rag-lite run must not fail the gate for carrying EXTRA arms."""
    rows = _rows(rag=True, cortex=True, hybrid=True, rag1=True)
    agg = replicate.aggregate({"t1": rows, "t2": rows})
    baseline = {"arms": {a: {"mean": 0.5, "margin": 0.03}
                         for a in ("rag", "cortex", "hybrid")}}
    assert replicate.gate_verdict(agg, baseline) == []


def test_question_rates_and_permutation_take_a_rag_lite_arm():
    rows_a = _rows(rag=True, cortex=True, hybrid=True, rag1=True)
    rows_b = _rows(rag=True, cortex=True, hybrid=True, rag1=False)
    a = replicate.question_rates({"t": rows_a}, "rag1")
    b = replicate.question_rates({"t": rows_b}, "rag1")
    assert replicate.paired_permutation(a, b, n=200)["delta"] == 1.0


def test_rag_lite_names_collide_with_nothing_but_the_reader_sweep():
    """PR #236's check, re-run over the whole tree: these arm names mint row
    keys (rag1_correct, ragb300_score, ...) and a collision with an unrelated
    key would average two different things into one column.

    The ONE pre-existing user of ``rag<N>`` is beam_reader_sweep.py
    (rag6/rag16/rag48), and it means the same thing these arms do — the
    first N turns of one ranked serve — so the vocabulary is shared, not
    collided. Its artifacts are a separate file family
    (``beam-readersweep-*``) written by a separate harness, so no single
    file ever mixes the two. What this pins is that no LongMemEval or
    beam_adapter artifact carries such a key, and that ``ragb<N>`` is new.
    """
    import re
    pattern = re.compile(r'"(rag\d+|ragb\d+)_')
    hits, budget_hits = set(), set()
    for path in (REPO / "evals" / "results").glob("*.jsonl"):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                for name in pattern.findall(line):
                    hits.add(path.name)
                    if name.startswith("ragb"):
                        budget_hits.add(path.name)
                if hits and path.name in hits:
                    break
    assert budget_hits == set()
    assert all(name.startswith("beam-readersweep-") for name in hits), hits


# ── the offline rebuild onto an already-extracted run ─────────────────────
def test_the_persisted_rag_block_does_not_split_back_into_its_turns():
    """Why rag_lite_rebuild.py re-ingests instead of slicing the string.

    The obvious cheap trick — split the judged rag context on its "\\n\\n"
    separator and take the first K pieces — silently produces the WRONG
    arms, because turn texts contain blank lines of their own. This pins
    the number evals/README quotes for it: 6 of the 78 ceiling-v38 rows.
    """
    path = (REPO / "evals" / "results"
            / "longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl")
    if not path.exists():                          # pragma: no cover
        pytest.skip("ceiling-v38 rows not checked out")
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 78
    recoverable = sum(1 for r in rows
                      if len(r["contexts"]["rag"].split("\n\n")) == lmb.RAG_TOP_K)
    assert recoverable == 6



def _rebuild_fixture(tmp_path, monkeypatch, raw_texts):
    pytest.importorskip("torch")
    import rag_lite_rebuild as rlr

    src = tmp_path / "longmemeval-ku-oracle-qwen-27b-src.jsonl"
    row = {"question_id": "q1", "question": "which bike?",
           "contexts": {"rag": "\n\n".join(raw_texts), "cortex": "f",
                        "hybrid": "h"},
           "rag_correct": True, "rag_response": "x", "rag_context_tokens": 9,
           "sessions": 2}
    src.write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(lmb, "load_questions",
                        lambda *a, **kw: [{"question_id": "q1"}])
    monkeypatch.setattr(rlr, "rederive_raw_texts", lambda q: raw_texts)
    return rlr


def test_rebuild_adds_the_arms_and_strips_every_verdict(tmp_path,
                                                        monkeypatch):
    raw = ["turn one " + "a" * 60, "turn two " + "b" * 60,
           "turn three " + "c" * 60]
    rlr = _rebuild_fixture(tmp_path, monkeypatch, raw)
    rlr.main(["--src-tag", "src", "--out-tag", "out",
              "--rag-lite-top-k", "1,2", "--rag-budget-tokens", "20,40"])
    out = lmb.load_rows(tmp_path / "longmemeval-ku-oracle-qwen-27b-out.jsonl")
    ctx = out[0]["contexts"]
    assert set(ctx) == {"rag", "cortex", "hybrid", "rag1", "rag2",
                        "ragb20", "ragb40"}
    for arm in ("rag1", "rag2", "ragb20", "ragb40"):
        assert ctx["rag"].startswith(ctx[arm])
    # every arm re-judged in one pass, so nothing stale is carried over
    assert not any(k.endswith(("_correct", "_response", "_context_tokens"))
                   for k in out[0])


def test_rebuild_refuses_when_the_rag_context_does_not_re_derive(
        tmp_path, monkeypatch):
    """The whole claim is that these arms are prefixes of the context that
    was actually judged. If the retrieval stack has moved since that run,
    they would measure drift instead of budget — so nothing is written."""
    rlr = _rebuild_fixture(tmp_path, monkeypatch, ["a" * 40, "b" * 40])
    monkeypatch.setattr(rlr, "rederive_raw_texts",
                        lambda q: ["DIFFERENT", "b" * 40])
    with pytest.raises(SystemExit, match="DIFFERENT rag context"):
        rlr.main(["--src-tag", "src", "--out-tag", "out",
                  "--rag-lite-top-k", "1"])
    assert not (tmp_path / "longmemeval-ku-oracle-qwen-27b-out.jsonl").exists()


def test_rebuild_refuses_to_overwrite_its_source(tmp_path, monkeypatch):
    rlr = _rebuild_fixture(tmp_path, monkeypatch, ["a" * 40, "b" * 40])
    with pytest.raises(SystemExit, match="must differ"):
        rlr.main(["--src-tag", "src", "--out-tag", "src",
                  "--rag-lite-top-k", "1"])


# ── BEAM: arm plumbing and token accounting ───────────────────────────────
def _beam():
    pytest.importorskip("torch")
    import beam_adapter
    return beam_adapter


def test_beam_arms_for_appends_the_rag_lite_arms():
    beam = _beam()
    assert beam.arms_for(False, rag_lite=("rag1", "ragb600")) == (
        "rag", "cortex", "hybrid", "rag1", "ragb600")
    # --arms can still select a subset, rag-lite arms included
    assert beam.arms_for(False, only="rag,rag1",
                         rag_lite=("rag1",)) == ("rag", "rag1")
    with pytest.raises(SystemExit, match="ragK/ragbN"):
        beam.arms_for(False, only="rag1")


def test_beam_answer_arm_records_the_served_token_cost():
    """The row shape every downstream reader keys off. Before 2026-09-04
    BEAM recorded no token column at all, so an accuracy-vs-cost read had
    to be eyeballed across two artifacts."""
    beam = _beam()
    row = {"contexts": {}}
    beam.answer_arm(row, "rag1", {"question": "q?", "rubric": ["item"]},
                    "x" * 40, "<question><rubric_item><llm_response>",
                    chat=lambda system, prompt, max_tokens=256, **_:
                        '{"score": 1.0}')
    assert row["rag1_context_tokens"] == 10          # len//4
    assert row["contexts"]["rag1"] == "x" * 40
    assert row["rag1_score"] == 1.0
    # the served-nothing arm records the harness's floored estimate, so
    # its column reads in the same units as every other arm
    beam.answer_arm(row, "nomem", {"question": "q?", "rubric": ["item"]},
                    "", "<question><rubric_item><llm_response>",
                    chat=lambda system, prompt, max_tokens=256, **_:
                        '{"score": 0.0}')
    assert row["nomem_context_tokens"] == 1


def test_beam_rag_lite_validation_fires_before_any_global_moves():
    """A bad rag-lite width must die before the bench globals are touched —
    the contract every other budget knob on this adapter carries."""
    beam = _beam()
    import longmemeval_bench as lme
    before = (lme.RAG_TOP_K, lme.HYBRID_TOP_K, lme.RAG_LITE_TOP_KS,
              lme.RAG_BUDGET_TOKENS, lme.CHRONICLE)
    for kwargs, match in ((({"rag_lite_top_ks": (0,)}), "positive"),
                          # 16 is the EFFECTIVE width here (--rag-top-k 16),
                          # so a 16-turn rag-lite arm copies the control
                          (({"rag_lite_top_ks": (16,)}), "not narrower"),
                          (({"rag_budget_tokens": 0}), "positive")):
        with pytest.raises(SystemExit, match=match):
            beam.run(Path("nowhere"), "100K", "qwen-27b", "t", None, None,
                     rag_top_k=16, chronicle=True, **kwargs)
    assert (lme.RAG_TOP_K, lme.HYBRID_TOP_K, lme.RAG_LITE_TOP_KS,
            lme.RAG_BUDGET_TOKENS, lme.CHRONICLE) == before


def test_beam_row_context_tokens_prefers_the_recorded_field():
    beam = _beam()
    row = {"contexts": {"rag": "abcdefgh", "nomem": ""},
           "rag_context_tokens": 99}
    assert beam.row_context_tokens(row, "rag") == 99       # recorded wins
    assert beam.row_context_tokens(row, "nomem") == 1      # legacy estimate
    assert beam.row_context_tokens(row, "absent") is None
    assert beam.mean_context_tokens([row, row], "rag") == 99


def test_beam_report_carries_context_tokens_per_arm_and_type(tmp_path,
                                                             monkeypatch):
    beam = _beam()
    rows = [
        {"chat_id": "1", "type": "a", "index": 0,
         "rag_score": 1.0, "rag_score_intfaithful": 1.0,
         "rag_context_tokens": 300,
         "rag1_score": 0.5, "rag1_score_intfaithful": 0.0,
         "rag1_context_tokens": 50,
         "cortex_score": 0.0, "cortex_score_intfaithful": 0.0,
         "cortex_context_tokens": 40,
         "hybrid_score": 1.0, "hybrid_score_intfaithful": 1.0,
         "hybrid_context_tokens": 340,
         "contexts": {}},
        {"chat_id": "1", "type": "b", "index": 0,
         "rag_score": 0.0, "rag_score_intfaithful": 0.0,
         "rag_context_tokens": 100,
         "rag1_score": 0.0, "rag1_score_intfaithful": 0.0,
         "rag1_context_tokens": 20,
         "cortex_score": 1.0, "cortex_score_intfaithful": 1.0,
         "cortex_context_tokens": 60,
         "hybrid_score": 0.5, "hybrid_score_intfaithful": 0.0,
         "hybrid_context_tokens": 160,
         "contexts": {}},
    ]
    out = tmp_path / "beam-100K-qwen-27b-t.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows),
                   encoding="utf-8")
    monkeypatch.setattr(beam, "RESULTS_DIR", tmp_path)
    beam.report("100K", "qwen-27b", "t")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-t.summary.json")
        .read_text(encoding="utf-8"))
    # the knob-minted arm is reported, not silently dropped
    assert set(summary["arms"]) == {"rag", "cortex", "hybrid", "rag1"}
    assert summary["arms"]["rag1"]["context_tokens"] == 35
    assert summary["arms"]["rag"]["context_tokens"] == 200
    assert summary["types"]["a"]["context_tokens"]["rag1"] == 50
    assert summary["types"]["b"]["context_tokens"]["rag"] == 100


def test_beam_within_run_pairs_reports_tokens_beside_chars():
    sys.path.insert(0, str(REPO / "evals"))
    from beam_within_run_pairs import pair_run
    rows = [
        # recorded field present: used verbatim
        {"type": "t", "rag_score": 1.0, "x_score": 0.0,
         "contexts": {"rag": "abcd" * 10, "x": "ab" * 10},
         "rag_context_tokens": 10, "x_context_tokens": 5},
        # legacy row: estimated from the persisted characters (len//4)
        {"type": "t", "rag_score": 0.0, "x_score": 1.0,
         "contexts": {"rag": "a" * 40, "x": ""}},
    ]
    d = pair_run(rows, ["x"], perms=200, seed=0)
    assert d["control_context_tokens_mean"] == 10        # (10 + 10) / 2
    # 5 and max(1, 0) — the estimator floors at 1, so a served-nothing arm
    # reads 1 token beside 0 characters
    assert d["arms"]["x"]["context_tokens_mean"] == 3
    assert d["arms"]["x"]["context_chars_mean"] == 10
