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


def test_threshold_locksteps_with_production():
    # The ladder mirrors service_dream.DreamOps._LOW_DIFFERENTIAL_SHARE as
    # a literal (importing the service stack would drag its heavy deps into
    # the harness), so pin the mirror by source scan -- the same pattern
    # the service-discipline tests use. A retuned production threshold must
    # turn this red, not silently diverge the bench.
    import re
    src = (Path(JL.__file__).resolve().parents[1] / "pseudolife_memory"
           / "service_dream.py").read_text(encoding="utf-8")
    m = re.search(r"_LOW_DIFFERENTIAL_SHARE\s*=\s*([0-9.]+)", src)
    assert m, "production threshold constant not found in service_dream.py"
    assert float(m.group(1)) == JL.LOW_DIFFERENTIAL_SHARE


def test_subset_scores_omitted_under_only_flagged():
    # Under --only-flagged every row is flagged: flagged_subset would
    # duplicate the top-level score and clean_subset would be the
    # degenerate empty-list block, so the split is not emitted at all.
    rows = [{**_row([], ["x"]), "label": "reject"},
            {**_row(["x", "y"], ["p", "q"]), "label": "accept"}]
    final = [("reject", 0.9), ("accept", 0.7)]
    flags = [JL.caution_flag(r) for r in rows]          # [True, False]
    assert JL.subset_scores(rows, final, flags, only_flagged=True) == {}
    both = JL.subset_scores(rows, final, flags, only_flagged=False)
    assert set(both) == {"flagged_subset", "clean_subset"}
    assert both["flagged_subset"]["rows"] == 1
    assert both["clean_subset"]["rows"] == 1
