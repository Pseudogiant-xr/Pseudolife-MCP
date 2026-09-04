"""Sanctioned override for the associative candidate-pool knobs in evals.

``memory.search.candidate_pool_multiplier`` / ``memory.search.fusion`` live
on the ASSOCIATIVE path (``cms.retrieve``), which ``rebuild_contexts.py``
cannot reach: it rebuilds the CORTEX fact ranking offline and copies the
``rag`` context verbatim. So a judged run with these knobs on is only ever a
full ``--phase extract`` re-run, and the knob state has to travel with the
artifact — the same contract PR #165 established for the answerer/judge
thinking/sampler knobs.

Import note: longmemeval_bench imports ladder_sweep/torch at module level,
so this file keeps to module-scoped fixtures.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def ladder():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import ladder_sweep
    return ladder_sweep


@pytest.fixture()
def search_cfg():
    from pseudolife_memory.utils.config import SearchConfig
    return SearchConfig()


def test_no_env_leaves_the_shipped_defaults(ladder, search_cfg, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_BENCH_POOL_MULT", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_BENCH_FUSION", raising=False)
    ladder.apply_pool_env(search_cfg)
    assert search_cfg.candidate_pool_multiplier == 1
    assert search_cfg.fusion == "weighted_sum"
    assert ladder.pool_env_knobs() == {
        "candidate_pool_multiplier": None, "fusion": None}


def test_env_applies_both_knobs_and_stamps_them(ladder, search_cfg,
                                                monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_BENCH_POOL_MULT", "4")
    monkeypatch.setenv("PSEUDOLIFE_BENCH_FUSION", "rrf")
    ladder.apply_pool_env(search_cfg)
    assert search_cfg.candidate_pool_multiplier == 4
    assert search_cfg.fusion == "rrf"
    assert ladder.pool_env_knobs() == {
        "candidate_pool_multiplier": "4", "fusion": "rrf"}


@pytest.mark.parametrize("mult", ["0", "-1", "four", "2.5"])
def test_a_bad_multiplier_aborts_rather_than_serving_the_default(
        ladder, search_cfg, monkeypatch, mult):
    # A typo that quietly served the shipped config would mislabel the whole
    # campaign — the failure mode the judged-run knob discipline exists for.
    monkeypatch.setenv("PSEUDOLIFE_BENCH_POOL_MULT", mult)
    with pytest.raises(SystemExit):
        ladder.apply_pool_env(search_cfg)


def test_a_bad_fusion_mode_aborts(ladder, search_cfg, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_BENCH_POOL_MULT", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_BENCH_FUSION", "reciprocal")
    with pytest.raises(SystemExit):
        ladder.apply_pool_env(search_cfg)


def test_the_knob_state_rides_in_the_bench_summary_stamp(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import longmemeval_bench as B

    monkeypatch.setenv("PSEUDOLIFE_BENCH_POOL_MULT", "4")
    monkeypatch.setenv("PSEUDOLIFE_BENCH_FUSION", "rrf")
    stamp = B.bench_env_knobs()
    assert stamp["candidate_pool"] == {
        "candidate_pool_multiplier": "4", "fusion": "rrf"}


def test_rebuild_contexts_does_not_claim_to_honour_the_pool_knobs():
    """Scope guard. ``rebuild_contexts.py`` is the regression gate's stage 1
    and rebuilds the CORTEX ranking only; it copies the associative context
    verbatim. If someone teaches it ``memory.search``, the lockstep it needs
    is NOT the cortex one (test_cortex_bm25.py) but a new associative
    mirror — so make that a deliberate edit, not a silent one."""
    from pathlib import Path
    path = (Path(__file__).resolve().parents[1]
            / "evals" / "rebuild_contexts.py")
    text = path.read_text(encoding="utf-8")
    # The module docstring EXPLAINS the scope gap and names the knobs, so
    # the guard reads the code below it, not the prose above it. Split on
    # the docstring delimiters rather than ast.get_docstring, whose value
    # is normalised and does not match the file text.
    head, doc, src = text.split('"""', 2)
    assert not head.strip(), "rebuild_contexts.py: expected a module docstring"
    assert "copied verbatim" in doc, (
        "rebuild_contexts.py no longer documents that the rag/associative "
        "context is copied verbatim — the gate's scope note is load-bearing")
    for knob in ("candidate_pool_multiplier", "SearchConfig",
                 "memory.search"):
        assert knob not in src, (
            f"rebuild_contexts.py now references {knob!r} — it rebuilds the "
            "cortex ranking only. Teaching it the associative path needs an "
            "associative lockstep test beside "
            "test_rebuild_fact_ranking_matches_service_fusion.")
