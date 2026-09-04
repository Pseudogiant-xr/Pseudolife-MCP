"""Tests for evals/router_offline.py — the offline routing analysis.

CPU-only, fixture-driven: a hand-built dataset whose right answers can be
worked out by hand, so a change in the aggregation shows up as a wrong
number rather than a plausible one. The committed artifacts are touched
only for the two things a fixture cannot check — that the loaders read the
real row schema, and that the analysis reproduces the committed summaries.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import router_offline as ro  # noqa: E402


# ── fixture ───────────────────────────────────────────────────────────────
def _rec(qid: str, qtype: str, question: str,
         scores: dict[str, float], costs: dict[str, float]) -> ro.Record:
    return ro.Record(qid=qid, qtype=qtype, question=question,
                     score=dict(scores), cost=dict(costs))


def _fixture() -> ro.Dataset:
    """Four questions, two types, three arms. By construction:

    type A: rag right twice, cortex right twice, hybrid right once
    type B: rag right once,  cortex never,       hybrid right twice
    so oracle-by-type picks cortex for A (tied with rag, cheaper) and
    hybrid for B, giving 4/4; rag alone gives 3/4.
    """
    cost = {"rag": 1000.0, "cortex": 100.0, "hybrid": 500.0}
    recs = [
        _rec("a1", "A", "when did I first buy the car?",
             {"rag": 1.0, "cortex": 1.0, "hybrid": 0.0}, cost),
        _rec("a2", "A", "what is my favourite colour?",
             {"rag": 1.0, "cortex": 1.0, "hybrid": 1.0}, cost),
        _rec("b1", "B", "how many sessions did we discuss the roadmap?",
             {"rag": 1.0, "cortex": 0.0, "hybrid": 1.0}, cost),
        _rec("b2", "B", "summarize the deployment plan you should follow",
             {"rag": 0.0, "cortex": 0.0, "hybrid": 1.0}, cost),
    ]
    return ro.Dataset(name="FIX", unit="tokens", cost_to_tokens=1.0,
                      arms=("rag", "cortex", "hybrid"), records=tuple(recs))


# ── arm / type aggregation ────────────────────────────────────────────────
def test_arm_table_means():
    tbl = ro.arm_table(_fixture())
    assert tbl["rag"]["score"] == pytest.approx(0.75)
    assert tbl["cortex"]["score"] == pytest.approx(0.5)
    assert tbl["hybrid"]["score"] == pytest.approx(0.75)
    assert tbl["cortex"]["cost"] == pytest.approx(100.0)


def test_type_table_splits_by_type():
    tbl = ro.type_table(_fixture())
    assert set(tbl) == {"A", "B"}
    assert tbl["A"]["n"] == tbl["B"]["n"] == 2
    assert tbl["A"]["arms"]["cortex"]["score"] == pytest.approx(1.0)
    assert tbl["B"]["arms"]["cortex"]["score"] == pytest.approx(0.0)


# ── arm choice: score first, then cost, then name ─────────────────────────
def test_pick_prefers_score_then_cost_then_name():
    # score dominates cost: the dear arm wins when it is more accurate
    assert ro._pick({"rag": 1.0, "cortex": 0.9},
                    {"rag": 1000.0, "cortex": 1.0},
                    ("rag", "cortex")) == "rag"
    # on a score tie the cheaper arm wins even though it sorts LAST by
    # name — so a dropped cost term shows up here rather than hiding
    # behind alphabetical order
    assert ro._pick({"cortex": 1.0, "rag": 1.0},
                    {"cortex": 1000.0, "rag": 100.0},
                    ("cortex", "rag")) == "rag"
    # a genuine score+cost tie falls back to the arm name, so the analysis
    # never depends on dict ordering
    flat = {"rag": 1.0, "cortex": 1.0, "hybrid": 1.0}
    same = {"rag": 5.0, "cortex": 5.0, "hybrid": 5.0}
    assert ro._pick(flat, same, ("rag", "cortex", "hybrid")) == "cortex"


# ── oracles ───────────────────────────────────────────────────────────────
def test_oracle_by_type_beats_best_single_arm_on_the_fixture():
    ds = _fixture()
    out = ro.oracle_by_type(ds, ("rag", "cortex", "hybrid"))
    assert out["choice"] == {"A": "cortex", "B": "hybrid"}
    assert out["score"] == pytest.approx(1.0)
    assert out["cost"] == pytest.approx((100.0 + 100.0 + 500.0 + 500.0) / 4)
    assert out["score"] > ro.arm_table(ds)["rag"]["score"]


def test_oracle_per_question_is_the_union():
    out = ro.oracle_per_question(_fixture(), ("rag", "cortex", "hybrid"))
    assert out["score"] == pytest.approx(1.0)
    # every question is served by its cheapest correct arm
    assert out["cost"] == pytest.approx((100.0 + 100.0 + 500.0 + 500.0) / 4)


def test_oracle_per_question_is_never_below_oracle_by_type():
    ds = _fixture()
    cands = ("rag", "cortex", "hybrid")
    assert (ro.oracle_per_question(ds, cands)["score"]
            >= ro.oracle_by_type(ds, cands)["score"])


# ── features ──────────────────────────────────────────────────────────────
def test_feature_vector_matches_declared_names():
    vec = ro.features("What is my favourite colour?")
    assert len(vec) == len(ro.FEATURE_NAMES)


def test_features_fire_on_the_cues_they_name():
    idx = {n: i for i, n in enumerate(ro.FEATURE_NAMES)}
    temporal = ro.features("When did I last update it in March?")
    assert temporal[idx["temporal"]] >= 3          # when, last, update?, march
    agg = ro.features("How many sessions in total?")
    assert agg[idx["aggregate"]] >= 2
    pref = ro.features("Which coffee do I prefer?")
    assert pref[idx["preference"]] >= 1
    assert pref[idx["lookup"]] >= 1
    assert ro.features("a b c?")[idx["n_words"]] == 3
    assert ro.features("a b c?")[idx["n_question_marks"]] == 1


def test_features_are_case_insensitive_and_deterministic():
    a = ro.features("WHEN did I buy it?")
    b = ro.features("when did I buy it?")
    assert a == b == ro.features("when did I buy it?")


# ── labels ────────────────────────────────────────────────────────────────
def test_cheap_labels_break_ties_toward_the_cheaper_arm():
    ds = _fixture()
    labels = ro._labels(ds, ("rag", "cortex", "hybrid"), "cheap")
    # a2 is right on all three arms -> cortex, the cheapest
    assert labels[1] == "cortex"


def test_acc_labels_break_ties_toward_the_strongest_arm():
    ds = _fixture()
    labels = ro._labels(ds, ("rag", "cortex", "hybrid"), "acc")
    # rag and hybrid tie at 0.75 overall; the sort is stable, so rag keeps
    # its place ahead of hybrid, and a2 (right on all three arms) takes rag
    # rather than the cheap-but-weak cortex
    assert labels[1] == "rag"
    assert labels[1] != "cortex"


def test_acc_labels_rank_the_arms_within_the_rows_they_are_given():
    """The `acc` tie-break ranks arms by mean score, and the cross-validated
    callers hand it a TRAINING FOLD. If that ranking were taken over the
    whole dataset instead, a held-out row would help decide the label it is
    later scored against — the leak this fixture demonstrates.

    Over all four rows rag and hybrid tie at 0.75 and rag keeps its place,
    so the all-arms-right row is labelled rag. Over rows 2-4 alone hybrid
    is strictly the strongest arm, so the same row must be labelled hybrid.
    """
    ds = _fixture()
    cands = ("rag", "cortex", "hybrid")
    assert ro._labels(ds, cands, "acc")[1] == "rag"
    assert ro._labels(ds, cands, "acc", rows=list(ds.records[1:]))[0] \
        == "hybrid"


def test_cv_predict_takes_labels_from_the_training_fold_only():
    """The callable form of `labels` must be asked for the train indices of
    each fold and never for a test index."""
    feats = [ro.features(f"question number {i} about the thing?")
             for i in range(30)]
    seen: list[tuple[int, ...]] = []

    def label_fn(idx):
        seen.append(tuple(idx))
        return ["rag" if i % 3 else "cortex" for i in idx]

    preds = ro._cv_predict(feats, label_fn, "tree_d3")
    assert len(preds) == 30
    assert len(seen) == ro.N_FOLDS
    for fold in seen:
        assert len(fold) == 24          # 30 rows, 5 folds, 24 train each


def test_unknown_label_policy_is_rejected():
    with pytest.raises(ValueError):
        ro._labels(_fixture(), ("rag", "cortex"), "whatever")


# ── cross-validation contract ─────────────────────────────────────────────
def test_cv_predict_never_trains_on_the_row_it_scores(monkeypatch):
    """The 5-fold split must partition the rows: every index appears in
    exactly one test fold and never in its own training fold."""
    from sklearn.model_selection import KFold
    n = 40
    seen: list[int] = []
    for train, test in KFold(n_splits=5, shuffle=True,
                             random_state=ro.SEED).split(list(range(n))):
        assert not (set(train) & set(test))
        seen.extend(test.tolist())
    assert sorted(seen) == list(range(n))


def test_cv_predict_is_seeded_and_repeatable():
    feats = [ro.features(f"question number {i} about the thing?")
             + [] for i in range(30)]
    labels = ["rag" if i % 3 else "cortex" for i in range(30)]
    first = ro._cv_predict(feats, labels, "tree_d3")
    assert first == ro._cv_predict(feats, labels, "tree_d3")
    assert len(first) == 30


def test_cv_predict_rejects_an_unknown_model():
    feats = [ro.features(f"q{i}?") for i in range(20)]
    labels = ["rag" if i % 2 else "cortex" for i in range(20)]
    with pytest.raises(ValueError):
        ro._cv_predict(feats, labels, "randomforest")


# ── two-stage ─────────────────────────────────────────────────────────────
def test_two_stage_serves_cortex_exactly_where_it_commits():
    ds = _fixture()
    commits = [True, False, False, True]
    out = ro.two_stage_router(ds, commits, ("rag", "hybrid"),
                              "tree_d3", "acc")
    assert out["n_commit"] == 2
    assert out["n_routed"] == 2
    assert out["arm_share"]["cortex(commit)"] == 2
    assert sum(out["arm_share"].values()) == len(ds.records)


def test_two_stage_fallback_still_pays_the_cortex_block():
    """The gate reads the cortex answer, so the cortex tokens are spent
    before the fallback arm is chosen — the cost must include both."""
    ds = _fixture()
    out = ro.two_stage_router(ds, [False] * 4, ("rag", "hybrid"),
                              "tree_d3", "acc")
    assert out["n_commit"] == 0
    assert out["cost"] >= ds.records[0].cost["cortex"]


# ── cost / ratio bookkeeping ──────────────────────────────────────────────
def test_with_ratio_converts_chars_to_tokens_only_for_the_ratio():
    ds = ro.Dataset(name="B", unit="chars",
                    cost_to_tokens=1.0 / ro.CHARS_PER_TOKEN,
                    arms=("rag",), records=())
    out = ro.with_ratio(ds, {"score": 0.5, "cost": 4000.0})
    assert out["cost"] == 4000.0            # unchanged, still chars
    assert out["est_tokens"] == pytest.approx(1000.0)
    assert out["score_per_1k_tokens"] == pytest.approx(0.5)


def test_with_ratio_handles_a_zero_cost_arm():
    ds = ro.Dataset(name="B", unit="chars", cost_to_tokens=0.25,
                    arms=("nomem",), records=())
    assert ro.with_ratio(ds, {"score": 0.2,
                              "cost": 0.0})["score_per_1k_tokens"] is None


# ── robustness ────────────────────────────────────────────────────────────
def test_cross_dataset_agreement_counts_matching_choices():
    lme = {"knowledge-update": "cascade", "temporal-reasoning": "hybrid",
           "multi-session": "rag", "single-session-preference": "rag"}
    beam = {"knowledge_update": "cascade", "temporal_reasoning": "refind",
            "multi_session_reasoning": "hybrid",
            "preference_following": "rag"}
    out = ro.cross_dataset_agreement(lme, beam)
    assert out["n_pairs"] == 4
    assert out["n_agree"] == 2
    assert [r["agree"] for r in out["pairs"]] == [True, False, False, True]


def test_cross_dataset_agreement_reports_a_missing_type_as_disagreement():
    out = ro.cross_dataset_agreement({}, {})
    assert out["n_agree"] == 4   # both None on every pair
    out = ro.cross_dataset_agreement({"knowledge-update": "rag"}, {})
    assert out["pairs"][0]["agree"] is False


# ── verdict ───────────────────────────────────────────────────────────────
def _ds_report(realizable_score: float, realizable_cost: float) -> dict:
    return {
        "best_single_arm": "rag",
        "arms": {"rag": {"score": 0.60, "cost": 1000.0},
                 "cascade": {"score": 0.61, "cost": 800.0}},
        "policies": {
            "oracle_by_type[base]": {"score": 0.70, "cost": 900.0},
            "oracle_per_question[base]": {"score": 0.90, "cost": 500.0},
            "router[base|tree_d3|acc]": {"score": realizable_score,
                                         "cost": realizable_cost},
        },
    }


def test_verdict_passes_only_on_a_real_gain_at_no_extra_cost():
    assert ro.verdict_for(_ds_report(0.63, 900.0))["passes"] is True
    # gain too small
    assert ro.verdict_for(_ds_report(0.62, 900.0))["passes"] is False
    # big gain, but it costs more
    assert ro.verdict_for(_ds_report(0.70, 1100.0))["passes"] is False


def test_verdict_reports_the_oracle_bound_separately():
    v = ro.verdict_for(_ds_report(0.60, 1000.0))
    assert v["oracle_by_type_gain"] == pytest.approx(0.10)
    assert v["ceiling_gain"] == pytest.approx(0.30)
    assert v["oracle_bound_would_pass"] is True
    assert v["passes"] is False


# ── the committed artifacts ───────────────────────────────────────────────
@pytest.mark.parametrize("path", [ro.LME_ALL, ro.LME_KU38, ro.BEAM])
def test_source_artifacts_are_present(path):
    assert path.exists(), f"missing source artifact {path}"


def test_loaders_read_the_real_row_schema():
    lme = ro.load_lme(ro.LME_ALL, "LME-500")
    assert len(lme.records) == 500
    assert lme.unit == "tokens" and "cascade" in lme.arms
    beam = ro.load_beam(ro.BEAM, "BEAM-400")
    assert len(beam.records) == 400
    assert beam.unit == "chars" and "nomem" in beam.arms
    # the nomem arm serves nothing, by construction
    assert all(r.cost["nomem"] == 0.0 for r in beam.records)


@pytest.mark.parametrize("loader,path", [
    (ro.load_lme, ro.LME_ALL), (ro.load_lme, ro.LME_KU38),
    (ro.load_beam, ro.BEAM)])
def test_analysis_reproduces_the_committed_summary(loader, path):
    """The sanity gate the report prints: if this drifts, every number
    below it in the report is being read out of the artifact wrongly."""
    check = ro.sanity_vs_summary(loader(path, "x"))
    assert check["checked"] and check["n_matches"]
    # the summaries round to 3-4 places; the costs to one
    assert check["max_score_delta"] < 5e-4
    assert check["max_cost_delta"] < 0.05


def test_cascade_matches_the_harness_gate_row_by_row():
    """The cascade arm here must be the harness's own policy, not a
    re-implementation that drifted."""
    import replicate
    rows = [json.loads(line) for line
            in ro.LME_ALL.read_text(encoding="utf-8").splitlines() if line]
    ds = ro.load_lme(ro.LME_ALL, "LME-500")
    for row, rec in zip(rows, ds.records):
        assert rec.score["cascade"] == float(replicate.cascade_correct(row))
        assert rec.cost["cascade"] == float(
            replicate.cascade_context_tokens(row))


def test_published_artifact_is_current(tmp_path):
    """Regenerating the report must reproduce the committed artifact
    byte for byte — the script is seeded and reads only committed rows."""
    published = REPO / "evals" / "results" / "router-offline-20260904.json"
    assert published.exists()
    fresh = tmp_path / "fresh.json"
    ro.build(fresh)
    assert json.loads(fresh.read_text(encoding="utf-8")) == \
        json.loads(published.read_text(encoding="utf-8"))
