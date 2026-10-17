"""Negative controls must distinguish mutations from declared baseline differences."""
import mcp_run


def report(diffs):
    return {"summary": {**mcp_run.expected_summary([]), "diffs": len(diffs)}, "diffs": diffs}


def test_unchanged_failing_baseline_is_not_a_caught_mutant():
    baseline = report(["body_limit[0]: connection close"])
    assert not mcp_run.mutant_caught(1, baseline, baseline)


def test_changed_difference_catches_mutant_and_crashes_do_not():
    baseline = report(["body_limit[0]: connection close"])
    mutant = report([*baseline["diffs"], "toolset[0]: wrong tier"])
    assert mcp_run.mutant_caught(1, mutant, baseline)
    assert not mcp_run.mutant_caught(2, mutant, baseline)


def test_execution_failures_and_incomplete_reports_are_not_kills():
    import copy
    from mcp_compare import UNSERVED

    baseline = {"summary": {"responses": 342, "cases": 18, "diffs": 0,
                            "unserved_checked": sorted(UNSERVED)}, "diffs": []}
    for failure in [
        'handshake[0]: python={"exception": "TimeoutError: timed out"}\n          rust  ={}',
        'stub calls raised: RuntimeError: aborted',
        'streams: 10 vs 1 responses',
    ]:
        report = copy.deepcopy(baseline)
        report["summary"]["diffs"] = 1
        report["diffs"] = [failure]
        assert not mcp_run.mutant_caught(1, report, baseline)
    for key, value in [("responses", 260), ("cases", 15), ("unserved_checked", [])]:
        report = copy.deepcopy(baseline)
        report["summary"]["diffs"] = 1
        report["diffs"] = ["toolset[0]: wrong tier"]
        report["summary"][key] = value
        assert not mcp_run.mutant_caught(1, report, baseline)
