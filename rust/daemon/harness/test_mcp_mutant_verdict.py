"""Negative controls must distinguish mutations from declared baseline differences."""
import mcp_run


def test_unchanged_failing_baseline_is_not_a_caught_mutant():
    baseline = {"summary": {"diffs": 1}, "diffs": ["body_limit[0]: connection close"]}
    assert not mcp_run.mutant_caught(1, baseline, baseline)


def test_changed_difference_catches_mutant_and_crashes_do_not():
    baseline = {"summary": {"diffs": 1}, "diffs": ["body_limit[0]: connection close"]}
    mutant = {"summary": {"diffs": 2}, "diffs": [*baseline["diffs"], "toolset[0]: wrong tier"]}
    assert mcp_run.mutant_caught(1, mutant, baseline)
    assert not mcp_run.mutant_caught(2, mutant, baseline)
