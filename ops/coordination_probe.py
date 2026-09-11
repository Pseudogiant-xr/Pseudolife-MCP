"""Manual, synthetic Claude channel probe; never opens a memory bank.

Register this script as a disposable stdio MCP server and explicitly enable that
server using Claude Code's interactive development-channel launch option. A human
must confirm the host's registration notice; SDK initialization is not that proof.
The required output path is a new private JSONL trace outside the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from pseudolife_memory.channel import ChannelEvent, serve_channel
from pseudolife_memory.coordination_adapter import AdapterError, _open_state


class ProbeError(RuntimeError):
    """Safe probe diagnostic, without supplied values or paths."""


class Probe:
    def __init__(self, out: Path, *, nonce: str | None = None, delay=2):
        out = Path(out)
        if not out.is_absolute() or out.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise ProbeError("probe trace requires an absolute private path outside the repository")
        self._nonce = nonce or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self._nonce):
            raise ProbeError("probe nonce must be a short synthetic identifier")
        try:
            fd = _open_state(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            self._trace = os.fdopen(fd, "w", encoding="utf-8")
        except (OSError, AdapterError):
            raise ProbeError("cannot create a new private probe trace") from None
        self.message_id = uuid.uuid4().hex
        self.delay = delay
        self.acknowledged = asyncio.Event()
        self._started = time.monotonic()
        self._attempted = False
        self._protocol = None
        self.server = Server(
            "pseudolife-coordination-probe",
            instructions=("This server emits one synthetic coordination probe. "
                          "Acknowledge only that event using coordination_probe_ack with its "
                          "message_id and nonce. No code, deployment, permission or other work "
                          "is requested. SDK readiness does not establish host registration."),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        self.record("started")

    def record(self, event):
        record = {"event": event, "elapsed_seconds": round(time.monotonic() - self._started, 3),
                  "message_id": self.message_id}
        if self._protocol:
            record["protocol"] = self._protocol
        self._trace.write(json.dumps(record) + "\n")
        self._trace.flush()
        os.fsync(self._trace.fileno())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.record("closed")
        finally:
            self._trace.close()

    async def _list_tools(self, ctx, params):
        self._protocol = ctx.protocol_version
        return types.ListToolsResult(tools=[types.Tool(
            name="coordination_probe_ack",
            description="Acknowledge the one synthetic channel event; no other action.",
            input_schema={"type": "object", "properties": {
                "message_id": {"type": "string"}, "nonce": {"type": "string"}},
                "required": ["message_id", "nonce"], "additionalProperties": False},
        )])

    async def _call_tool(self, ctx, params):
        self._protocol = ctx.protocol_version
        arguments = params.arguments or {}
        valid = (params.name == "coordination_probe_ack" and self._attempted
                 and set(arguments) == {"message_id", "nonce"}
                 and isinstance(arguments["message_id"], str) and isinstance(arguments["nonce"], str)
                 and hmac.compare_digest(arguments["message_id"].encode(), self.message_id.encode())
                 and hmac.compare_digest(arguments["nonce"].encode(), self._nonce.encode()))
        if not valid:
            self.record("ack_rejected")
            return types.CallToolResult(is_error=True, content=[types.TextContent(
                type="text", text="Acknowledgment rejected; use only the emitted probe event.")])
        if not self.acknowledged.is_set():
            self.record("explicit_ack")
            self.acknowledged.set()
            print("coordination probe: explicit agent acknowledgment recorded.", file=sys.stderr)
        return types.CallToolResult(content=[types.TextContent(
            type="text", text="Synthetic probe acknowledged. No further action requested.")])

    async def inbox(self):
        self.record("sdk_initialized")
        print("coordination probe: SDK initialized; host registration remains unverified.", file=sys.stderr)
        await asyncio.sleep(self.delay)
        self._attempted = True
        self.record("event_attempted")
        yield ChannelEvent(
            "Acknowledge only this synthetic event using coordination_probe_ack. "
            "Use the message_id and nonce from this event's metadata. "
            "No code, deployment, permissions or other work is requested.",
            {"message_id": self.message_id, "nonce": self._nonce, "origin": "synthetic_probe"},
        )


async def run_probe(probe, read, write, *, timeout):
    try:
        await asyncio.wait_for(serve_channel(probe.server, read, write, probe.inbox), timeout)
    except asyncio.TimeoutError:
        if not probe.acknowledged.is_set():
            probe.record("timeout")
            print("coordination probe: observation timed out; agent receipt unverified.", file=sys.stderr)
            return 1
    return 0 if probe.acknowledged.is_set() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--nonce", help="Optional synthetic correlation identifier; never logged")
    parser.add_argument("--delay", type=float, default=2, help="Seconds after SDK initialization (2–120)")
    parser.add_argument("--timeout", type=float, default=180,
                        help="Total observation seconds (greater than delay; at most 600)")
    args = parser.parse_args(argv)
    if not 2 <= args.delay <= 120 or not args.delay < args.timeout <= 600:
        parser.error("delay must be 2–120 seconds and timeout must exceed delay, at most 600 seconds")

    async def drive(probe):
        async with stdio_server() as (read, write):
            return await run_probe(probe, read, write, timeout=args.timeout)

    try:
        with Probe(args.out, nonce=args.nonce, delay=args.delay) as probe:
            print("coordination probe: started; waiting for SDK initialization.", file=sys.stderr)
            return asyncio.run(drive(probe))
    except ProbeError as error:
        print(f"coordination probe: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
