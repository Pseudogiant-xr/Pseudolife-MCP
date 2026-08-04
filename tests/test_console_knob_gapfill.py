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


def test_gated_off_capabilities_stay_out_of_console():
    # chronicle + known_facts_window failed their gates; the agg-recall
    # search knobs have not passed theirs. None may appear until a
    # preregistered gate PASSES (update this test in the same change).
    for path in (
        "memory.dream.chronicle",
        "memory.dream.known_facts_window",
        "memory.search.contiguity_neighbors",
        "memory.search.timeline_channel",
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
