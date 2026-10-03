"""Repeat pinned Python EOF captures; retain observed order without widening it."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time

from .harness import capture_platform, write_new
from .provenance import ROOT, runtime_metadata
from .stdio import capture
from .stdio_capture import initialize, modern_meta, require_phase1_source
from .stdio_fixture import HangingFixture


def observe(root, home, era, case, *, command=None, validate_oracle=True):
    fixture = HangingFixture()
    result = None
    try:
        def exercise(wire):
            initialize(wire, era)
            if case == "pending-subscription":
                wire.send({"jsonrpc": "2.0", "id": "listener", "method": "subscriptions/listen",
                           "params": {"_meta": modern_meta(), "notifications": {"toolsListChanged": True}}})
                return
            for number in range({"zero": 0, "one": 1, "two": 2}[case]):
                wire.send({"jsonrpc": "2.0", "id": f"hang-{number}", "method": "tools/call",
                           "params": {"name": "hang", "arguments": {},
                                      **({"_meta": modern_meta()} if era == "2026-07-28" else {})}})
                fixture.wait_calls(number + 1)
        result = capture(command or [sys.executable, "-m", "pseudolife_memory.cli"], cwd=root, home=home,
                         url=fixture.url, exercise=exercise)
        frames = result["stdout"]
        result.update(era=era, case=case, order=[
            frame.get("id", frame.get("method")) for frame in frames],
            final_responses=[frame for frame in frames if "error" in frame],
            acknowledgement_count=sum(frame.get("method") == "notifications/subscriptions/acknowledged"
                                      for frame in frames))
        expected_ids = (["listener"] if case == "pending-subscription" else
                        [f"hang-{n}" for n in range({"zero": 0, "one": 1, "two": 2}[case])])
        expected = [{"jsonrpc": "2.0", "id": identifier,
                     "error": {"code": -32000, "message": "Connection closed"}} for identifier in expected_ids]
        if validate_oracle and sorted(result["final_responses"], key=lambda frame: frame["id"]) != expected:
            raise RuntimeError("unexpected EOF terminal response")
        if validate_oracle and result["exit_code"] != 0:
            raise RuntimeError("Python EOF oracle failed")
        return result
    finally:
        cleanup = fixture.close()
        if result is not None:
            result["fixture_cleanup"] = cleanup


def main():
    from evals.rust_baseline.daemon import private_directory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    source = require_phase1_source(args.oracle_root.resolve())
    if args.repeats < 2:
        parser.error("at least two repeats required for ordering evidence")
    cells = []
    for era in ("2025-11-25", "2026-07-28"):
        for case in ("zero", "one", "two", "pending-subscription"):
            if case == "pending-subscription" and era != "2026-07-28":
                continue
            for repeat in range(args.repeats):
                with private_directory() as private:
                    result = observe(args.oracle_root.resolve(), Path(private) / "shim", era, case)
                result["repeat"] = repeat
                cells.append(result)
                print(json.dumps({"era": era, "case": case, "repeat": repeat,
                                  "exit": result["exit_code"], "order": result["order"],
                                  "acknowledgements": result["acknowledgement_count"]}), flush=True)
    groups = []
    for era, case in dict.fromkeys((cell["era"], cell["case"]) for cell in cells):
        selected = [cell for cell in cells if (cell["era"], cell["case"]) == (era, case)]
        orders = Counter(tuple(cell["order"]) for cell in selected)
        groups.append({"era": era, "case": case, "repeats": len(selected),
                       "orders": [{"order": list(order), "count": count} for order, count in orders.items()],
                       "order_varied": len(orders) > 1,
                       "acknowledgement_counts": dict(Counter(cell["acknowledgement_count"] for cell in selected))})
    write_new(args.out, {"schema": 1, "status": "observed", **source, "platform": capture_platform(),
                        "capture_runtime": runtime_metadata(args.oracle_root.resolve()),
                        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "limitation": "Storage-free hanging-response fixture; no durable-state parity claim.",
                        "instrument_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                              for p in sorted(Path(__file__).parent.glob("stdio*.py"))},
                        "groups": groups, "cells": cells})


if __name__ == "__main__":
    main()
