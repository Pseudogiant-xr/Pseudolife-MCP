"""Real forwarding candidate executions, separate from observation mutations."""
from pathlib import Path
import sys
from .stdio import capture
from .stdio_fixture import HangingFixture
from .stdio_judge import StdioPolicy, judge


def run(root):
    from evals.rust_baseline.daemon import private_directory
    policy = StdioPolicy()
    fixture = HangingFixture("healthy")
    try:
        with private_directory() as private:
            def exercise(wire):
                wire.send({"jsonrpc": "2.0", "id": "open", "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "graded-forwarding", "version": "1"}}})
                # finish closes stdin and drains the response, including a
                # deliberately malformed frame, before the judge parses it.
            expected = capture([sys.executable, "-m", "pseudolife_memory.cli"], cwd=root,
                home=Path(private) / "oracle", url=fixture.url, exercise=exercise)
            expected.update(arm="oracle", era="2025-11-25")
            results = {}
            for control in ("identity", "wrong-protocol", "duplicate-key", "stderr"):
                actual = capture([sys.executable, "-m", "evals.rust_port.broken_stdio", control],
                    cwd=root, home=Path(private) / control, url=fixture.url, exercise=exercise)
                actual.update(arm="candidate", era="2025-11-25")
                differences = judge(expected, actual, policy)
                if control == "identity":
                    passed = not differences
                elif control == "wrong-protocol":
                    passed = any(difference["path"] == "/json/0/result/protocolVersion"
                                 and difference["reason"] == "value"
                                 for difference in differences)
                elif control == "duplicate-key":
                    passed = any(difference["reason"] == "duplicate_json_key"
                                 for difference in differences)
                else:
                    passed = any(difference["path"] == "/stderr" and difference["reason"] == "value"
                                 for difference in differences)
                if not passed:
                    raise RuntimeError("forwarding candidate control failed: " + control)
                results[control] = {"passed": passed, "policy": policy.name,
                                    "differences": differences, "observed": actual}
    finally:
        cleanup = fixture.close()
    return {"controls": results, "oracle_observed": expected, "fixture_cleanup": cleanup,
            "scope": "Real public Python CLI forwarding controls; not a Rust parity claim or bank identity proof."}
