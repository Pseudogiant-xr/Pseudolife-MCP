"""Run strict MCP parity and controls, retaining the named declared divergences."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from mcp_compare import UNSERVED
from mcp_run import mutant_caught
from mcp_validation_sweep import EXPECTED_COERCION_CASES

HERE = Path(__file__).resolve().parent
MUTANTS = ("mcp-tier-flip", "mcp-toolset-floor", "mcp-sse-no-session", "mcp-no-hint",
           "mcp-binding-open")


# Declared differences (divergences.md), by name, as the strict corpus reports them.
# MCP-REFUSAL-CLEANUP: Rust closes the connection after a 413.
REFUSAL_CLEANUP = {f"body_limit[{index}]" for index in (0, 2, 3, 4, 5)}
# MCP-SESSION-FAILED-FIRST-REQUEST (port pending): mcp 2.2.0 drops a session
# whose first request was refused or failed; Rust keeps it, so the follow-up
# ping (id 2) or uninitialized tools/list (id 3) is still answered.
FAILED_FIRST_REQUEST = {6: 2, 7: 3, 9: 2, 10: 3, 13: 2, 14: 3, 16: 2, 18: 2, 20: 2}
# MCP-TERMINATED-SESSION-ID (port pending): mcp 2.2.0 forgets a DELETEd id.
TERMINATED_SESSION_ID = (12, 13)
SESSION_NOT_FOUND = {"status": 404, "headers": {"content-type": "application/json", "mcp-session-id": None},
                     "body": {"raw": '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                                     '"message":"Session not found"}}'}}
SSE_HEADERS = {"content-type": "text/event-stream", "cache-control": "no-cache, no-transform",
               "connection": "keep-alive", "x-accel-buffering": "no", "mcp-session-id": "<request>"}


def _kept_session_answer(request_id):
    """Rust's answer on a session mcp 2.1 kept: ping succeeds, tools/list is refused."""
    message = ({"jsonrpc": "2.0", "id": 2, "result": {}} if request_id == 2 else
               {"jsonrpc": "2.0", "id": 3,
                "error": {"code": -32602, "message": "Invalid request parameters", "data": ""}})
    text = json.dumps(message, separators=(",", ":"))
    return {"status": 200, "headers": SSE_HEADERS,
            "body": {"sse": [text], "events": [f"event: message\r\ndata: {text}"]}}


def _expected_pair(name):
    """The exact (python, rust) pair declared for one difference."""
    if name in REFUSAL_CLEANUP:
        py = {"status": 413, "headers": {"mcp-session-id": None}, "body": {"raw": "Request body too large"}}
        return py, {**py, "headers": {**py["headers"], "connection": "close"}}
    for index, request_id in FAILED_FIRST_REQUEST.items():
        if name == f"init_gate[{index}]":
            return SESSION_NOT_FOUND, _kept_session_answer(request_id)
    if name in {f"transport[{index}]" for index in TERMINATED_SESSION_ID}:
        return SESSION_NOT_FOUND, {
            "status": 404, "headers": {"content-type": "application/json", "mcp-session-id": "<request>"},
            "body": {"raw": '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,'
                            '"message":"Not Found: Session has been terminated"}}'}}
    raise AssertionError(f"undeclared difference: {name}")


def check_baseline(report):
    """Accept exactly the declared differences; the strict report stays red."""
    summary = report["summary"]
    assert (summary["responses"], summary["cases"], summary["diffs"]) == (347, 18, 16)
    assert set(summary["unserved_checked"]) == set(UNSERVED)
    expected = (REFUSAL_CLEANUP | {f"init_gate[{index}]" for index in FAILED_FIRST_REQUEST}
                | {f"transport[{index}]" for index in TERMINATED_SESSION_ID})
    seen = set()
    for diff in report["diffs"]:
        name, pair = diff.split(": python=", 1)
        py, rs = pair.split("\n          rust  =", 1)
        py, rs = json.loads(py), json.loads(rs)
        assert name in expected and name not in seen, name
        seen.add(name)
        want_py, want_rs = _expected_pair(name)
        assert py == want_py, name
        assert rs == want_rs, name
        if name not in REFUSAL_CLEANUP:
            # Python dropped or forgot the session; Rust still knows it.
            assert py["status"] == 404 and py["headers"]["mcp-session-id"] is None, name
            assert rs != py and rs["headers"]["mcp-session-id"] == "<request>", name
    assert seen == expected


def check_coercion_report(report):
    assert report['mode'] == 'coercions'
    assert report['generated_cases'] == EXPECTED_COERCION_CASES
    assert report['expected_observations'] == report['completed_observations'] == 2 * EXPECTED_COERCION_CASES
    assert not report['diffs']
    assert report['source']['pydantic-core'] == '2.50.0'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-bin", type=Path, required=True)
    parser.add_argument("--mutant-bin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = {}

    def run(label, binary, flags, expected):
        target = out / label
        report_name = (f"mutant-{flags[flags.index('--mutant') + 1]}.json"
                       if "--mutant" in flags else "compare.json")
        (target / report_name).unlink(missing_ok=True)
        if "--validation-coercions" in flags:
            (target / "validation-sweep.json").unlink(missing_ok=True)
        if "--mount-probe" in flags:
            (target / "foundation-mount.json").unlink(missing_ok=True)
        command = [sys.executable, str(HERE / "mcp_run.py"), str(binary.resolve()), str(target), *flags]
        with (out / f"{label}.log").open("w", encoding="utf-8") as log:
            code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
        results[label] = code
        print(f"{label}: exit {code}", flush=True)
        assert code == expected, f"{label}: expected exit {expected}, got {code}; see {out / (label + '.log')}"
        return target

    baseline_dir = run("baseline", args.default_bin, ["--mount-probe", "--validation-coercions"], 1)
    baseline_file = baseline_dir / "compare.json"
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    check_baseline(baseline)
    check_coercion_report(json.loads((baseline_dir / 'validation-sweep.json').read_text(encoding='utf-8')))
    print(f'SDK coercion gate: {2 * EXPECTED_COERCION_CASES} observations, zero differences', flush=True)
    mount = json.loads((baseline_dir / "foundation-mount.json").read_text(encoding="utf-8"))
    assert mount["case"] == "mcp path, auth" and not mount["diffs"]
    print("strict baseline: 16 declared differences (five 413 close headers, nine dropped-session"
          " follow-ups, two terminated ids); foundation mount matches", flush=True)
    for mutant in MUTANTS:
        target = run(mutant, args.mutant_bin, ["--mutant", mutant, "--baseline", str(baseline_file)], 0)
        report = json.loads((target / f"mutant-{mutant}.json").read_text(encoding="utf-8"))
        assert mutant_caught(1, report, baseline), mutant
    # A production binary must not be credited for the baseline's declared diffs.
    target = run("without-mutants", args.default_bin,
                 ["--mutant", "mcp-no-hint", "--baseline", str(baseline_file)], 1)
    report = json.loads((target / "mutant-mcp-no-hint.json").read_text(encoding="utf-8"))
    check_baseline(report)
    assert not mutant_caught(1, report, baseline)
    results["seconds"] = round(time.monotonic() - started, 1)
    (out / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
