"""The strict MCP corpus must match exactly; no difference is declared."""
import copy
import json

import pytest

from mcp_ci import MUTANTS, UNSERVED, check_baseline

def line(name, py, rs):
    return f"{name}: python={json.dumps(py)}\n          rust  ={json.dumps(rs)}"


def baseline():
    """The shape the strict corpus records on mcp 2.2.0: complete, no differences."""
    return {"summary": {"responses": 325, "cases": 18, "diffs": 0, "unserved_checked": list(UNSERVED)},
            "diffs": []}


def test_exact_baseline_is_retained():
    report = baseline()
    original = copy.deepcopy(report)
    check_baseline(report)
    assert report == original
    assert MUTANTS == ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint",
                       "mcp-binding-open", "mcp-keep-failed-opening", "mcp-delete-keeps-session")


def refusal_close(index):
    """The difference MCP-REFUSAL-CLEANUP used to declare: Rust closing after a 413."""
    py = {"status": 413, "headers": {"mcp-session-id": None}, "body": {"raw": "Request body too large"}}
    rs = copy.deepcopy(py)
    rs["headers"]["connection"] = "close"
    return line(f"body_limit[{index}]", py, rs)


@pytest.mark.parametrize("change", ["refusal_close", "extra", "incomplete", "unserved", "count_only"])
def test_any_difference_or_incomplete_corpus_fails(change):
    report = baseline()
    if change == "refusal_close":
        report["diffs"] = [refusal_close(i) for i in (0, 2, 3, 4, 5)]
        report["summary"]["diffs"] = 5
    elif change == "extra":
        report["diffs"].append("lists[0]: additional error")
        report["summary"]["diffs"] = 1
    elif change == "incomplete":
        report["summary"]["responses"] -= 1
    elif change == "unserved":
        report["summary"]["unserved_checked"] = report["summary"]["unserved_checked"][1:]
    else:
        report["diffs"].append(refusal_close(0))
    with pytest.raises((AssertionError, ValueError, KeyError)):
        check_baseline(report)


def test_rerun_cannot_reuse_reports_when_child_writes_nothing(tmp_path, monkeypatch):
    import subprocess
    import mcp_ci

    target = tmp_path / "baseline"
    target.mkdir()
    (target / "compare.json").write_text(json.dumps(baseline()), encoding="utf-8")
    (target / "foundation-mount.json").write_text(
        json.dumps({"case": "mcp path, auth", "diffs": []}), encoding="utf-8")
    calls = []

    def failed(command, **kwargs):
        # The baseline's expected exit, so only the missing report can fail it.
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(mcp_ci.subprocess, "run", failed)
    with pytest.raises(FileNotFoundError):
        mcp_ci.main(["--default-bin", "unused", "--mutant-bin", "unused", "--out", str(tmp_path)])
    assert len(calls) == 1


def test_ci_baseline_requests_live_coercion_gate(tmp_path, monkeypatch):
    import mcp_ci

    def stopped(command, **kwargs):
        assert '--validation-coercions' in command
        raise RuntimeError('verified live coercion gate')

    monkeypatch.setattr(mcp_ci.subprocess, 'run', stopped)
    with pytest.raises(RuntimeError, match='verified live coercion gate'):
        mcp_ci.main(['--default-bin', 'unused', '--mutant-bin', 'unused', '--out', str(tmp_path)])


@pytest.mark.parametrize('failure', ['missing', 'incomplete', 'different', 'version'])
def test_ci_rejects_missing_or_failed_coercion_evidence(failure):
    import mcp_ci
    count = mcp_ci.EXPECTED_COERCION_CASES
    report = {'mode': 'coercions', 'generated_cases': count,
              'expected_observations': 2 * count, 'completed_observations': 2 * count,
              'diffs': [], 'source': {'pydantic-core': '2.50.0'}}
    mcp_ci.check_coercion_report(report)
    if failure == 'missing':
        report.pop('completed_observations')
    elif failure == 'incomplete':
        report['completed_observations'] -= 1
    elif failure == 'different':
        report['diffs'] = [{'python': {'tier': 'core'}, 'rust': {'tier': 'full'}}]
    else:
        report['source']['pydantic-core'] = '99.0.0'
    with pytest.raises((AssertionError, KeyError)):
        mcp_ci.check_coercion_report(report)
