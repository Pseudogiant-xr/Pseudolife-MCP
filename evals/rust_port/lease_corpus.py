"""Additive native lease leaves; no existing in-process test is relabelled."""
from __future__ import annotations

import base64
import os
import sys


def cases():
    """Stable local-state cells for the reviewed public-process fixture.

    Board FIFO, renewals and signal cleanup require owned live preparation;
    these cells do not claim coverage of those concurrency boundaries.
    """
    absent_board = {"PSEUDOLIFE_MCP_TOKEN_FILE": None, "PSEUDOLIFE_MCP_TOKEN": None}
    instance = {".pseudolife-mcp/locks/instance.id": base64.b64encode(b"0123456789ab\n").decode()}
    result = []

    def add(identifier, action, tail, *, files=None, deltas=None):
        result.append({"id": identifier, "mode": "lease-" + action,
                       "argv": ["lease", action, *tail], "stdin_b64": "",
                       "environment_deltas": {**absent_board, **(deltas or {})},
                       "pre_files_b64": {**instance, **(files or {})}, "normalizations": []})

    for action in ("check", "list", "run"):
        add("lease-" + action + "-help", action, ["--help"])
    for identifier, argv in (
        ("leading-unknown", ["--unknown", "check", "sample"]),
        ("top-help-cluster", ["-hh"]),
        ("check-help-cluster", ["check", "-hh"]),
        ("top-help-explicit", ["--help=x"]),
    ):
        result.append({"id": "lease-parser-" + identifier, "mode": "lease-parser",
                       "argv": ["lease", *argv], "stdin_b64": "",
                       "environment_deltas": absent_board.copy(),
                       "pre_files_b64": instance.copy(), "normalizations": []})
    for flag in ([], ["--json"]):
        suffix = "json" if flag else "text"
        add("lease-check-absent-" + suffix, "check", ["sample", *flag])
        add("lease-check-free-" + suffix, "check", ["sample", *flag],
            files={".pseudolife-mcp/locks/lease-sample.lock": ""})
        add("lease-check-suite-remote-" + suffix, "check", ["full-suite@peer", *flag])
        add("lease-check-negative-number-name-" + suffix, "check", ["-1.5", *flag])
        add("lease-list-empty-" + suffix, "list", flag)
        add("lease-list-free-" + suffix, "list", flag,
            files={".pseudolife-mcp/locks/lease-a.lock": "", ".pseudolife-mcp/locks/lease-z.lock": "",
                   ".pseudolife-mcp/locks/full-suite.lock": ""})
    child = ("import os,pathlib,sys; os.write(1,b'child\\x00\\xff'); "
             "os.write(2,b'stderr\\x00'); "
             "pathlib.Path(os.environ['HOME'],'sentinel').write_bytes("
             "os.environ['PSEUDOLIFE_LEASES_HELD'].encode('utf-8')); sys.exit(3)")
    command = ["--", sys.executable, "-c", child]
    add("lease-run-local-zero-timeout", "run", ["sample", "--no-board", "--timeout", "0", *command])
    add("lease-run-held-env-appended", "run", ["sample", "--no-board", "--expect", "7d", "--ttl", "30", *command],
        deltas={"PSEUDOLIFE_LEASES_HELD": "outer"})
    add("lease-run-name-hash", "run", ["mémoire/claim", "--no-board", *command])
    add("lease-run-missing-executable", "run", ["sample", "--no-board", "--", "missing-lease-corpus-program"])
    for identifier, value in (("above-u64", "18446744073709551616"),
                              ("above-u64-days", "18446744073709551616d"),
                              ("above-instant-range", "9223372036854775808")):
        add("lease-run-timeout-" + identifier, "run",
            ["sample", "--timeout", value, "--no-board", "--", "missing-boundary-child"])
    add("lease-run-bad-lock-directory", "run", ["sample", "--no-board", *command],
        files={"blocked": ""}, deltas={"PSEUDOLIFE_LEASE_LOCK_DIR": "{home}/blocked"})
    for option in ("timeout", "expect", "ttl"):
        add("lease-run-digit-limit-" + option, "run",
            ["sample", "--" + option, "0" * 4300 + "1", *command])
    add("lease-run-cannot-execute-file", "run", ["sample", "--no-board", "--", "{home}/blocked"], files={"blocked": ""})
    add("lease-check-nonascii-bearer", "check", ["sample", "--json"],
        deltas={"PSEUDOLIFE_MCP_TOKEN": "bearer-é", "PSEUDOLIFE_MCP_TOKEN_FILE": None})
    if os.name == "nt":
        add("lease-run-command-script", "run", ["sample", "--no-board", "--", "{home}/child.cmd"],
            files={"child.cmd": base64.b64encode(b"@echo off\r\nexit /b 3\r\n").decode()})
    for name, options in (
        ("decimal", ["--expect", "1.5h"]), ("bad-unit", ["--timeout", "7days"]),
        ("expect-upper-bound", ["--expect", "8d"]), ("ttl-lower-bound", ["--ttl", "29"]),
        ("negative-timeout", ["--timeout", "-1"]), ("delegate-only-for", ["--for", "7d"]),
        ("negative-decimal-timeout", ["--timeout", "-1.5"]),
    ):
        add("lease-run-usage-" + name, "run", ["sample", *options, *command])
    add("lease-run-missing-command", "run", ["sample"])
    add("lease-check-rejects-command", "check", ["sample", *command])
    return result
