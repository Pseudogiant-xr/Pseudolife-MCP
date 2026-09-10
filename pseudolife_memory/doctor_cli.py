"""Read-only runtime diagnostics: never spawn a daemon or call a bank tool."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys


async def _handshake() -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "pseudolife_memory.cli"],
        env={**os.environ, "PSEUDOLIFE_MCP_NO_SPAWN": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            result = await client.initialize()
            manifest = (await client.list_tools()).tools
            return {
                "instructions_present": bool(result.instructions),
                "tool_count": len(manifest),
                "tools_missing_annotations": [t.name for t in manifest if t.annotations is None],
            }


def run_doctor() -> None:
    from pseudolife_memory.shim import _daemon_url, _require_mcp_sdk_v2, probe_health

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20,
                        help="total MCP handshake budget in seconds (default: 20)")
    args = parser.parse_args(sys.argv[2:])
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    report = {"ok": False, "interpreter": sys.executable,
              "source": str(Path(__file__).resolve().parent)}
    for package in ("pseudolife-mcp", "mcp"):
        try:
            report[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report[package] = "not installed"
    try:
        _require_mcp_sdk_v2()
        health = probe_health(_daemon_url(), timeout=min(args.timeout, 2))
        report["daemon_status"] = health.get("status") if health else "unreachable"
        if not health or health.get("status") != "ok":
            report["error"] = "DaemonUnavailable"
            report["recovery"] = "Start the intended daemon, then retry; doctor never starts one."
        else:
            report.update(asyncio.run(asyncio.wait_for(_handshake(), timeout=args.timeout)))
            report["ok"] = bool(report["instructions_present"] and report["tool_count"]
                                and not report["tools_missing_annotations"])
            if not report["ok"]:
                report["recovery"] = "Check shim stderr and daemon MCP access, then compare daemon and shim versions; update the component missing instructions or annotations and reconnect."
    except TimeoutError:
        report["error"] = "TimeoutError"
        report["recovery"] = "Check daemon health and MCP access; if startup is slow, retry doctor with a larger --timeout budget."
    except (Exception, SystemExit) as exc:
        # Do not serialize transport exceptions: they can contain auth headers
        # or URL credentials. The exception type plus recovery is sufficient.
        report["error"] = type(exc).__name__
        report["recovery"] = "Check daemon health and the exact registered interpreter. Run that interpreter with -m pip check and -m pip show pseudolife-mcp mcp; reinstall there if stale, then retry."
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
