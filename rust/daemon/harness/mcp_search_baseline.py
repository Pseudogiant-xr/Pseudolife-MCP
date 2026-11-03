"""Compare explain=false response bytes against an unmodified daemon binary.

Both daemons hydrate independent copies of the same offline fixture bank.
The comparison retains the entire SSE body, including both MCP payload
representations, and the HTTP status. Session header values are transport
identifiers and do not appear in the body.
"""

import argparse
import json
from pathlib import Path

import run as harness
from mcp_wire import Session


class PlainSearch(harness.SCENARIOS["mcp-search-explain"]):
    name = "mcp-search-baseline"

    def timeline(self, procs, holders):
        sessions = {side: Session(proc.port, harness.T_DEFAULT) for side, proc in procs.items()}
        try:
            for args in ({"query": "memory"}, {"query": "memory", "verbose": True},
                         {"query": "memory", "explain": False, "min_score": 0.9},
                         {"query": "   ", "explain": False}):
                answers = {}
                for side, session in sessions.items():
                    response = session.tool("memory_search", args)
                    answers[side] = {"status": response["status"], "headers": {},
                                     "bytes": response["body"].decode("utf-8")}
                c = harness.case(json.dumps(args), "POST", "/mcp")
                c["_answers"] = (answers["python"], answers["rust"])
                yield c
        finally:
            for session in sessions.values():
                session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-bin", required=True, type=Path)
    parser.add_argument("--rust-bin", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    baseline = args.baseline_bin.resolve()
    original = harness.daemons.python_daemon
    # Use the runner's first arm for the baseline binary; Python still
    # seeds the bank but does not supply these response expectations.
    harness.daemons.python_daemon = lambda home, port, env: harness.daemons.rust_daemon(
        baseline, home, port, harness.rust_env(env))
    try:
        result = harness.run_scenario(PlainSearch(), args.rust_bin.resolve(),
                                      harness.daemons.scratch_root(), "live", False)
    finally:
        harness.daemons.python_daemon = original
    result["reference"] = "baseline Rust binary"
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    bad = sum(bool(c["diffs"]) for c in result["cases"])
    print(f"baseline comparison: {len(result['cases'])} cases, {bad} response diffs, "
          f"{len(result['db_diffs'])} bank diffs", flush=True)
    return int(bool(bad or result["db_diffs"]))


if __name__ == "__main__":
    raise SystemExit(main())
