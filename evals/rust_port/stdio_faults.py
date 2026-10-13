"""Public CLI fault and credential-rotation observations over owned HTTP fixtures."""
from pathlib import Path
import sys

from .stdio import capture
from .stdio_capture import initialize, modern_meta
from .stdio_corpus import ERAS
from .stdio_fixture import HangingFixture
from .stdio_judge import StdioPolicy, judge_with_evidence


def run(root, command):
    from evals.rust_baseline.daemon import private_directory
    from pseudolife_memory.credentials import _write_token_file
    cells, differences = [], []
    for fault in ("down", "401", "503", "rotation", "degraded"):
        for era in ERAS:
            fixture = HangingFixture(fault)
            if fault == "down":
                # Stop our own listener so startup and calls see an actually
                # refused endpoint, rather than healthy HTTP with broken MCP.
                fixture.close()
            pair = []
            try:
                with private_directory() as private:
                    token_path = Path(private) / "synthetic-token"
                    for arm, prefix in (("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                                        ("candidate", command)):
                        fixture.token = "synthetic-fixture-before"
                        extras = {}
                        if fault == "rotation":
                            _write_token_file(token_path, fixture.token)
                            extras["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(token_path)
                        def exercise(wire):
                            initialize(wire, era)
                            def request(identifier, method, params):
                                if era == ERAS[1]:
                                    params = {**params, "_meta": modern_meta()}
                                wire.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
                                return wire.response(identifier, timeout=25)
                            request("list", "tools/list", {})
                            request("first", "tools/call", {"name": "fixture_tool", "arguments": {}})
                            if fault == "rotation":
                                fixture.token = "synthetic-fixture-after"
                                _write_token_file(token_path, fixture.token)
                                request("rotated", "tools/call", {"name": "fixture_tool", "arguments": {}})
                            elif fault == "degraded":
                                request("recovered", "tools/list", {})
                        result = capture(prefix, cwd=root, home=Path(private) / arm, url=fixture.url,
                                         exercise=exercise, env_extra=extras, timeout=25)
                        result.update(arm=arm, era=era, case=fault)
                        pair.append(result)
                differences.extend({"era": era, "case": fault, **difference}
                                   for difference in judge_with_evidence(*pair, StdioPolicy()))
            finally:
                cleanup = fixture.close()
                for result in pair:
                    result["fixture_cleanup"] = cleanup
                cells.extend(pair)
            print(__import__("json").dumps({"fault": fault, "era": era, "observed": len(pair)}), flush=True)
    return {"cells": cells, "differences": differences,
            "limitation": "Storage-free fault fixtures; unchanged 8-node tests cover watcher schedules and recovery assertions."}
