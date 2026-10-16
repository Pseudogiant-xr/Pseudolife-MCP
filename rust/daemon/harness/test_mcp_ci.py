"""Only the exact declared differences may accompany MCP controls."""
import copy
import json

import pytest

from mcp_ci import MUTANTS, UNSERVED, check_baseline

NOT_FOUND = {"status": 404, "headers": {"content-type": "application/json", "mcp-session-id": None},
             "body": {"raw": '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Session not found"}}'}}
TERMINATED = {"status": 404, "headers": {"content-type": "application/json", "mcp-session-id": "<request>"},
              "body": {"raw": '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                              '"message":"Not Found: Session has been terminated"}}'}}
PING = '{"jsonrpc":"2.0","id":2,"result":{}}'
UNINITIALIZED = '{"jsonrpc":"2.0","id":3,"error":{"code":-32602,"message":"Invalid request parameters","data":""}}'


def sse(text):
    return {"status": 200,
            "headers": {"content-type": "text/event-stream", "cache-control": "no-cache, no-transform",
                        "connection": "keep-alive", "x-accel-buffering": "no", "mcp-session-id": "<request>"},
            "body": {"sse": [text], "events": [f"event: message\r\ndata: {text}"]}}


def line(name, py, rs):
    return f"{name}: python={json.dumps(py)}\n          rust  ={json.dumps(rs)}"


def baseline():
    """The shapes the strict corpus recorded on mcp 2.2.0 (CI run of #780, 2026-10-16)."""
    py = {"status": 413, "headers": {"mcp-session-id": None}, "body": {"raw": "Request body too large"}}
    rs = copy.deepcopy(py)
    rs["headers"]["connection"] = "close"
    follow_ups = {6: PING, 7: UNINITIALIZED, 9: PING, 10: UNINITIALIZED, 13: PING,
                  14: UNINITIALIZED, 16: PING, 18: PING, 20: PING}
    diffs = [line(f"init_gate[{i}]", NOT_FOUND, sse(text)) for i, text in follow_ups.items()]
    diffs += [line(f"body_limit[{i}]", py, rs) for i in (0, 2, 3, 4, 5)]
    diffs += [line(f"transport[{i}]", NOT_FOUND, TERMINATED) for i in (12, 13)]
    return {"summary": {"responses": 345, "cases": 18, "diffs": 16, "unserved_checked": list(UNSERVED)},
            "diffs": diffs}


def test_exact_declared_baseline_is_retained():
    report = baseline()
    original = copy.deepcopy(report)
    check_baseline(report)
    assert report == original
    assert MUTANTS == ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint",
                       "mcp-binding-open")


# Each edit replaces text in the named diff: (diff name, old text, new text).
EDITS = {
    "status": ("body_limit[0]", '"status": 413', '"status": 500'),
    "body": ("body_limit[0]", "Request body too large", "wrong refusal"),
    "undeclared_index": ("init_gate[6]", "init_gate[6]", "init_gate[8]"),
    "rust_drops_too": ("init_gate[6]", '"status": 200', '"status": 404'),
    "python_keeps_id": ("init_gate[7]", '"mcp-session-id": null', '"mcp-session-id": "<request>"'),
    "python_other_error": ("init_gate[9]", "Session not found", "Session expired"),
    "rust_other_answer": ("init_gate[10]", "Invalid request parameters", "Session not initialized"),
    "python_terminated": ("transport[12]", "Session not found", "Not Found: Session has been terminated"),
    "rust_not_terminated": ("transport[13]", "Not Found: Session has been terminated", "Gone"),
    "rust_no_id": ("transport[13]", '"mcp-session-id": "<request>"', '"mcp-session-id": null'),
}


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "incomplete", *EDITS])
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
        name, old, new = EDITS[change]
        index = next(i for i, d in enumerate(report["diffs"]) if d.startswith(name + ":"))
        assert old in report["diffs"][index], change
        report["diffs"][index] = report["diffs"][index].replace(old, new, 1)
    with pytest.raises((AssertionError, ValueError, KeyError)):
        check_baseline(report)


@pytest.mark.parametrize("family", ["init_gate[", "body_limit[", "transport["])
def test_every_declared_family_is_required(family):
    report = baseline()
    report["diffs"] = [d for d in report["diffs"] if not d.startswith(family)]
    report["summary"]["diffs"] = len(report["diffs"])
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
