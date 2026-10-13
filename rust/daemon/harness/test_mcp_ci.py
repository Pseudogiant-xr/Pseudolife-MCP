"""Only the exact declared refusal differences may accompany MCP controls."""
import copy
import json

import pytest

from mcp_ci import MUTANTS, UNSERVED, check_baseline


def baseline():
    py = {"status": 413, "headers": {"mcp-session-id": None}, "body": {"raw": "Request body too large"}}
    rs = copy.deepcopy(py)
    rs["headers"]["connection"] = "close"
    return {"summary": {"responses": 261, "cases": 16, "diffs": 5, "unserved_checked": list(UNSERVED)},
            "diffs": [f"body_limit[{i}]: python={json.dumps(py)}\n          rust  ={json.dumps(rs)}"
                      for i in (0, 2, 3, 4, 5)]}


def test_exact_declared_baseline_is_retained():
    report = baseline()
    original = copy.deepcopy(report)
    check_baseline(report)
    assert report == original
    assert MUTANTS == ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint")


@pytest.mark.parametrize("change", ["missing", "extra", "status", "body", "duplicate", "incomplete"])
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
    else:
        old, new = ('"status": 413', '"status": 500') if change == "status" else ("Request body too large", "wrong refusal")
        report["diffs"][0] = report["diffs"][0].replace(old, new)
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
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(mcp_ci.subprocess, "run", failed)
    with pytest.raises(FileNotFoundError):
        mcp_ci.main(["--default-bin", "unused", "--mutant-bin", "unused", "--out", str(tmp_path)])
    assert len(calls) == 1
