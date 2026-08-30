"""The strict-MC re-score script must reproduce its committed artifacts.

The 2026-08-30 promotion appended rows 57-74 to
``lme-v2-smoke-qwen38-slice.jsonl``, which silently changed what
``rescore_strict_mc.main()``'s paired56 target would compute: an unpinned
re-run would overwrite the canonical
``lme-v2-qwen38-vs-slice2-paired56-rescored-strictmc.json`` with a 74-row
comparison and flip four pinned CHANGELOG claims. These tests hold the
script to its outputs: every committed correction artifact must be exactly
what the committed script computes from the committed inputs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import rescore_strict_mc as R  # noqa: E402

RESULTS = REPO / "evals" / "results"


def _committed(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def test_paired56_stays_pinned_to_the_original_56_rows():
    computed = R.paired("lme-v2-smoke-slice2", "lme-v2-smoke-qwen38-slice",
                        b_limit=56)
    committed = _committed("lme-v2-qwen38-vs-slice2-paired56"
                           "-rescored-strictmc.json")
    assert computed["paired_n"] == committed["paired_n"]
    assert computed["arms"] == committed["arms"]


def test_promoted_summaries_reproduce_the_committed_artifacts():
    for name in ("lme-v2-smoke-qwen38-slice",
                 "lme-v2-smoke-qwen38-slice-compose"):
        computed = R.summary(name, date="2026-08-30")
        committed = _committed(f"{name}-rescored-strictmc.summary.json")
        assert computed == committed, f"{name}: script no longer reproduces " \
                                      f"its committed correction artifact"


def test_paired74_reproduces_the_committed_artifact():
    computed = R.paired("lme-v2-smoke-slice2",
                        "lme-v2-smoke-qwen38-slice-fixjudge")
    committed = _committed("lme-v2-qwen38-vs-slice2-paired74"
                           "-rescored-strictmc.json")
    assert computed["paired_n"] == 74
    assert computed["arms"] == committed["arms"]
    assert computed["flips"] == committed["flips"]
