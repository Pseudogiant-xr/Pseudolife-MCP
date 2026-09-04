"""Console knob registry covers the operator-relevant config added since July.

The Console edits a curated whitelist (config_io.KNOBS); config fields added
after the registry was written silently fall out of the UI. These tests pin
the 2026-08-04 gap-fill so the next drift is caught, and pin the deliberate
ABSENCES: knobs whose capability failed (or has not yet passed) its
preregistered gate must NOT be surfaced — a console switch for a gated-off
capability is the knob lying about what the daemon does.
"""

from __future__ import annotations

from pseudolife_memory.web.config_io import KNOBS

_BY_PATH = {k["path"]: k for k in KNOBS}


def _knob(path):
    assert path in _BY_PATH, f"knob missing from console registry: {path}"
    return _BY_PATH[path]


def test_literal_gate_knobs():
    gate = _knob("memory.dream.literal_gate")
    assert gate["type"] == "enum"
    assert gate["options"] == ["off", "log", "enforce"]
    assert gate["default"] == "enforce"
    assert gate["restart"] is False
    scope = _knob("memory.dream.literal_gate_scope")
    assert scope["options"] == ["batch", "source"]
    assert scope["default"] == "batch"


def test_graph_hygiene_knobs():
    conf = _knob("memory.dream.min_relation_confidence")
    assert conf["type"] == "float" and conf["default"] == 0.2
    quar = _knob("memory.dream.relation_quarantine_below")
    assert quar["type"] == "float" and quar["default"] == 0.5
    retype = _knob("memory.dream.retype_quarantined_max")
    assert retype["type"] == "int" and retype["default"] == 3


def test_runs_keep_knob_in_retention_group():
    knob = _knob("memory.dream.runs_keep")
    assert knob["type"] == "int" and knob["default"] == 50
    assert knob["group"] == "Retention"


def test_bm25_cortex_switch_knob():
    knob = _knob("memory.bm25.cortex_enabled")
    assert knob["type"] == "bool"
    # Shipped OFF by measured evidence (2026-07-30 A/B) — the console must
    # not present a different default.
    assert knob["default"] is False
    assert knob["restart"] is False


def test_reranker_skip_margin_knob():
    knob = _knob("memory.reranker.skip_margin")
    assert knob["type"] == "float" and knob["default"] == 0.0
    # Read per-query in cms.retrieve, not baked at reranker construction.
    assert knob["restart"] is False


def test_lessons_inference_knobs():
    assert _knob("memory.lessons.synthesize_in_dream")["default"] is True
    assert _knob("memory.lessons.infer_outcomes")["default"] is True
    sig = _knob("memory.lessons.infer_outcomes_max_signals")
    assert sig["type"] == "int" and sig["default"] == 3


def test_chronicle_knob_surfaced_default_on():
    # Surfaced 2026-08-12 with the default-on flip: ev2 preregistered
    # gates all passed (evals/results/ev2-separate-pass-verdict.json) and
    # the 08-05..08-12 production soak reviewed clean — the exposure
    # decision the absence list below deliberately waited on.
    knob = _knob("memory.dream.chronicle")
    assert knob["type"] == "bool"
    assert knob["default"] is True
    # chronicle_on is read from service.config on every dream pass.
    assert knob["restart"] is False


def test_session_digest_knobs_surfaced_default_off():
    # Surfaced 2026-08-27 (maintainer-directed): PR #202 shipped the digest
    # stage config-only, so the only way to flip it on a live daemon was
    # hand-editing /data/config.yaml inside the container. Exposure here is
    # about operability (flipping the soak on/off without a container
    # exec), not a gate verdict — the default stays the shipped False and
    # the console must present it.
    knob = _knob("memory.dream.digest_enabled")
    assert knob["type"] == "bool"
    assert knob["default"] is False
    # generate_digests_stage re-reads service.config on every dream cycle.
    assert knob["restart"] is False

    target = _knob("memory.dream.digest_target_chars")
    assert target["type"] == "int" and target["default"] == 1200
    assert target["restart"] is False
    ctx = _knob("memory.dream.digest_context_chars")
    assert ctx["type"] == "int" and ctx["default"] == 24000
    assert ctx["restart"] is False
    # digest_max_per_cycle is deliberately NOT surfaced — a backfill pacing
    # constant, not an operator dial; config.yaml still reaches it.
    assert "memory.dream.digest_max_per_cycle" not in _BY_PATH


def test_deep_dream_judge_mode_knob_surfaced_default_shadow():
    # Surfaced 2026-08-30 (maintainer-directed), alongside the live
    # deployment's opt-in to auto-reject on the 2026-08-21 shadow-vs-triage
    # evidence (evals/results/judge-shadow-live-20260821.json): until now
    # the only way to flip a live daemon was hand-editing /data/config.yaml
    # inside the container. The shipped default stays "shadow" — the
    # 2026-08-16 judge ladder measured auto-reject precision as
    # judge-dependent (Opus-class 0.987+, local Qwen 0.918 with
    # uninformative confidence), so the knob carries that caveat in its
    # help text instead of the default changing.
    knob = _knob("memory.deep_dream.judge_mode")
    assert knob["type"] == "enum"
    # "auto" added 2026-09-02: guarded two-vote auto-accept (see
    # test_review_queue_judge_knobs).
    assert knob["options"] == ["off", "shadow", "auto-reject", "auto"]
    assert knob["default"] == "shadow"
    # deep_dream_judge re-reads service.config on every sweep batch.
    assert knob["restart"] is False
    # The caveat is load-bearing: auto-reject is only measured-safe on an
    # Opus-class judge endpoint.
    assert "Opus-class" in knob["help"]


def test_gated_off_capabilities_stay_out_of_console():
    # known_facts_window failed its gate; the agg-recall search knobs
    # have not passed theirs. None may appear until a preregistered gate
    # PASSES and exposure is deliberately decided (update this test in
    # the same change — chronicle graduated out of this list 2026-08-12).
    for path in (
        "memory.dream.known_facts_window",
        "memory.search.contiguity_neighbors",
        "memory.search.timeline_channel",
        # stale_policy ships default-"annotate"; console exposure waits on
        # the H3 verdict AND a deliberate exposure decision (the chronicle
        # precedent: a passed gate alone does not surface a knob).
        "memory.search.stale_policy",
        # Two-man-rule quarantine ships default-off; console exposure is a
        # separate decision after the gate-3 friction estimate.
        "memory.dream.quarantine_low_trust",
        "memory.dream.trusted_sources",
        # Candidate-pool shape (2026-09-04). Both ship at today's behaviour
        # and both went through a judged run and FAILED it: multiplier 4
        # costs naive RAG 0.115 under rrf and 0.077 under weighted_sum on
        # the LongMemEval knowledge-update oracle slice, while serving a
        # third to a half more context tokens (table in evals/README.md,
        # "Judged verdict (2026-09-04)"). A measured loser is a stronger
        # reason to keep a live retrieval-ranking switch off the Console
        # than an unmeasured one, not a weaker one.
        "memory.search.candidate_pool_multiplier",
        "memory.search.fusion",
    ):
        assert path not in _BY_PATH, f"gated-off knob surfaced: {path}"


def test_registry_paths_all_resolve_against_appconfig():
    # Every registered knob must reach a real AppConfig attribute — a typo'd
    # or removed path renders as a control that silently 400s on save.
    from pseudolife_memory.utils.config import AppConfig
    cfg = AppConfig()
    for knob in KNOBS:
        cur = cfg
        for part in knob["path"].split("."):
            assert hasattr(cur, part), (
                f"{knob['path']}: AppConfig has no attribute {part!r}")
            cur = getattr(cur, part)


def test_extractor_reasoning_effort_knob():
    # Applies at the next dream (build_extractor constructs fresh from
    # config per invocation), hence restart False; provider-specific extras
    # (codex "minimal", claude "max") ride the suggestions, not an enum.
    k = _knob("memory.dream.extractor_reasoning_effort")
    assert k["group"] == "Extractor"
    assert k["type"] == "string"
    assert k["default"] is None
    assert k["restart"] is False
    for v in ("low", "medium", "high", "xhigh"):
        assert v in k["suggestions"]


def test_review_queue_judge_knobs():
    # 2026-09-02 review-queue autonomy: every queue's judge gets a mode
    # switch in the Console (the gates stay config-file knobs like
    # judge_reject_min_confidence). Defaults are the shipped ones: shadow
    # everywhere a verdict deletes or writes, off for the candidate judge,
    # on for the two mechanical apply additions.
    for path, options, default in (
            ("memory.deep_dream.link_judge_mode", ["off", "shadow", "auto"], "shadow"),
            ("memory.deep_dream.junk_judge_mode", ["off", "shadow", "auto"], "shadow"),
            ("memory.deep_dream.curation_judge_mode",
             ["off", "shadow", "auto-distinct", "auto"], "shadow"),
            ("memory.deep_dream.candidate_judge_mode", ["off", "shadow", "auto"], "off")):
        knob = _knob(path)
        assert knob["type"] == "enum" and knob["options"] == options, path
        assert knob["default"] == default and knob["restart"] is False, path
    for path in ("memory.deep_dream.judges_enabled",
                 "memory.deep_dream.judge_second_opinion",
                 "memory.deep_dream.analyzer_file_duplicates"):
        knob = _knob(path)
        assert knob["type"] == "bool" and knob["default"] is True, path
        assert knob["restart"] is False
    # The one destructive switch ships OFF (review finding, 2026-09-02).
    orphan = _knob("memory.deep_dream.orphan_sweep")
    assert orphan["type"] == "bool" and orphan["default"] is False


def test_judge_second_model_knob():
    # 2026-09-03: the merge judge's second-opinion model was a config-file
    # field only, so the one flip that makes the two-vote gates a
    # two-MODEL check (judge_mode "auto" refuses same-model accepts by
    # design) needed a container edit and a restart. The judge reads it
    # from service.config on every batch, so it is a live string knob like
    # extractor_model_override, with the same suggestion list shape.
    knob = _knob("memory.deep_dream.judge_second_model")
    assert knob["type"] == "string" and knob["default"] is None
    assert knob["restart"] is False and knob["group"] == "Deep dream"
    assert "claude-fable-5" in knob["suggestions"]
    assert "different" in knob["help"].lower()   # says why it exists
