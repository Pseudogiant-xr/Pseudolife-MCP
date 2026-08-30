"""The judge ladder's --caution arm: computes ``low_differential`` on the
frozen fixture's SHOWN snippets (the two defect classes of
``evals/results/judge-shadow-live-20260821.json``) and passes it through the
proposal dicts so ``format_judge_proposal`` renders the production caution
line. Unflagged rows must serialize byte-identically to the frozen baseline.
"""
from __future__ import annotations

import json
from pathlib import Path

from evals import judge_ladder as JL
from pseudolife_memory.memory.dream import format_judge_proposal


def _row(src, dst):
    return {"from": {"display": "a", "snippets": src},
            "into": {"display": "b", "snippets": dst},
            "reason": "cosine", "score": 0.9}


def test_caution_flag_empty_side():
    assert JL.caution_flag(_row([], ["x"])) is True
    assert JL.caution_flag(_row(["x"], [])) is True


def test_caution_flag_disjoint_is_clean():
    assert JL.caution_flag(_row(["x", "y"], ["p", "q"])) is False


def test_caution_flag_half_shared():
    # 1 shared of min-side 2 -> 0.5, at the production threshold.
    assert JL.caution_flag(_row(["x", "y"], ["x", "q"])) is True


def test_caution_flag_containment():
    # src wholly contained in dst -> overlap 1.0 on the shown sets.
    assert JL.caution_flag(_row(["x"], ["x", "q"])) is True


def test_build_proposals_key_only_on_flagged_rows():
    rows = [_row(["x", "y"], ["p", "q"]), _row([], ["x"])]
    plain = JL.build_proposals(rows, caution=False)
    assert all("low_differential" not in p for p in plain)
    flagged = JL.build_proposals(rows, caution=True)
    assert "low_differential" not in flagged[0]
    assert flagged[1]["low_differential"] is True
    # The unflagged row's prompt is byte-identical to the baseline arm's.
    assert (format_judge_proposal(flagged[0])
            == format_judge_proposal(plain[0]))
    assert "caution: LOW-DIFFERENTIAL" in format_judge_proposal(flagged[1])


def test_fixture_flag_count_is_stable():
    rows = json.loads(Path(JL.DATA).read_text(encoding="utf-8"))["rows"]
    n = sum(JL.caution_flag(r) for r in rows)
    # Frozen fixture (built 2026-08-16 under the pre-fix attachment code) --
    # the flagged share must be substantial but not total, or the caution
    # arm measures nothing.
    assert 0 < n < len(rows)
