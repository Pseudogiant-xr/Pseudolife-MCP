"""Only the exact declared differences may accompany MCP controls."""
import copy
import json

import pytest

from mcp_ci import MUTANTS, UNSERVED, check_baseline

def line(name, py, rs):
    return f"{name}: python={json.dumps(py)}\n          rust  ={json.dumps(rs)}"


def refusal_close(index):
    """A Rust 413 that says it will close where Python's does not."""
    py = {"status": 413, "headers": {"mcp-session-id": None}, "body": {"raw": "Request body too large"}}
    rs = copy.deepcopy(py)
    rs["headers"]["connection"] = "close"
    return line(f"body_limit[{index}]", py, rs)


def baseline():
    """The shapes the strict corpus records on mcp 2.2.0: only the two 413s
    declaring a body past the daemon's discard budget (MCP-REFUSAL-CLEANUP)."""
    return {"summary": {"responses": 319, "cases": 18, "diffs": 2, "unserved_checked": list(UNSERVED)},
            "diffs": [refusal_close(i) for i in (4, 5)]}


def test_exact_declared_baseline_is_retained():
    report = baseline()
    original = copy.deepcopy(report)
    check_baseline(report)
    assert report == original
    assert MUTANTS == ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint",
                       "mcp-binding-open", "mcp-keep-failed-opening", "mcp-delete-keeps-session")


# Each edit replaces text in the named diff: (diff name, old text, new text).
EDITS = {
    "status": ("body_limit[4]", '"status": 413', '"status": 500'),
    "body": ("body_limit[4]", "Request body too large", "wrong refusal"),
    "undeclared_index": ("body_limit[4]", "body_limit[4]", "body_limit[1]"),
    "rust_keeps_connection": ("body_limit[5]", '"connection": "close"', '"connection": "keep-alive"'),
}


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "incomplete",
                                    "old_close_differences", *EDITS])
def test_any_other_difference_or_incomplete_corpus_fails(change):
    report = baseline()
    if change == "missing":
        report["diffs"].pop()
    elif change == "extra":
        report["diffs"].append("lists[0]: additional error")
    elif change == "duplicate":
        report["diffs"][0] = report["diffs"][1]
    elif change == "incomplete":
        report["summary"]["responses"] -= 1
    elif change == "old_close_differences":
        # The five closes this daemon sent before 2026-10-20 must not return.
        report["diffs"] = [refusal_close(i) for i in (0, 2, 3, 4, 5)]
        report["summary"]["diffs"] = 5
    else:
        name, old, new = EDITS[change]
        index = next(i for i, d in enumerate(report["diffs"]) if d.startswith(name + ":"))
        assert old in report["diffs"][index], change
        report["diffs"][index] = report["diffs"][index].replace(old, new, 1)
    with pytest.raises((AssertionError, ValueError, KeyError)):
        check_baseline(report)


def test_every_declared_difference_is_required():
    report = baseline()
    report["diffs"] = []
    report["summary"]["diffs"] = 0
    with pytest.raises(AssertionError):
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
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

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
