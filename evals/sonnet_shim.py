"""OpenAI-compatible shim serving Claude models via headless ``claude -p``.

Bridges the bench harness (which speaks ``/v1/chat/completions``) to the
Claude Code CLI on the user's Max plan — no API key. Each POST spawns one
``claude -p`` subprocess: the request's system message goes to
``--system-prompt``, the user messages go to stdin, and the reply is parsed
from ``--output-format json``. Tools and MCP servers are disabled so each
call is a single pure completion.

Registered as the ``sonnet-5`` rung/extractor (ceiling probe, 2026-07-11).
Cloud rung stays OUT of LADDER_ORDER — the default sweep remains
sovereign-only; invoke explicitly with ``--rung sonnet-5`` /
``--extractor sonnet-5``.

Notes:
  * Calls are serialized with a lock (the bench is sequential anyway, and one
    in-flight Max call at a time is deliberate).
  * ``response_format``/``temperature`` in the request are ignored — the CLI
    exposes neither. Markdown code fences around the reply are stripped so a
    fenced JSON answer still parses downstream.
  * Requires a logged-in CLI (``/login`` once, interactively); a
    "Not logged in" result surfaces as HTTP 500 with that message.

Endpoints: POST /v1/chat/completions, GET /health, GET /v1/models.

Usage:
    python evals/sonnet_shim.py [--port 8082] [--model claude-sonnet-5]
        [--cli PATH] [--call-timeout 300]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_CLI = Path(
    r"C:\Users\HAMO9\AppData\Local\Packages\Claude_pzs8sxrjxfjjc"
    r"\LocalCache\Roaming\Claude\claude-code\2.1.205\claude.exe")
# A fenced reply ("```json\n...\n```") would fail the extractor's json.loads.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Windows CreateProcess caps the command line at 32767 chars; leave margin.
_MAX_ARGV_SYSTEM = 24000


class ClaudeCli:
    """One ``claude -p`` subprocess per call, serialized."""

    def __init__(self, cli: Path, model: str, call_timeout: float):
        self.cli = cli
        self.model = model
        self.call_timeout = call_timeout
        self.lock = threading.Lock()
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        cmd = [str(self.cli), "-p", "--model", self.model,
               "--output-format", "json",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--tools", ""]
        if system and len(system) <= _MAX_ARGV_SYSTEM:
            cmd += ["--system-prompt", system]
        elif system:
            # Too long for argv — prepend to stdin instead (rare; vocab hints
            # keep the system message a few KB).
            user = f"{system}\n\n{user}"
        with self.lock:
            self.calls += 1
            n = self.calls
            t0 = time.monotonic()
            proc = subprocess.run(cmd, input=user.encode("utf-8"),
                                  capture_output=True,
                                  timeout=self.call_timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude -p rc={proc.returncode}: "
                f"{proc.stderr.decode('utf-8', 'replace')[:400]}")
        out = json.loads(proc.stdout.decode("utf-8", "replace"))
        if out.get("is_error"):
            raise RuntimeError(
                f"claude -p error result: {str(out.get('result'))[:400]}")
        reply = (out.get("result") or "").strip()
        m = _FENCE_RE.match(reply)
        if m:
            reply = m.group(1).strip()
        print(f"sonnet_shim: call {n} ok "
              f"({time.monotonic() - t0:.1f}s, {len(reply)} chars)",
              flush=True)
        return reply


def make_handler(cli: ClaudeCli):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet per-request noise
            pass

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {"status": "ok"})
            elif self.path in ("/v1/models", "/models"):
                self._json(200, {"object": "list", "data": [
                    {"id": cli.model, "object": "model"},
                    {"id": "extractor", "object": "model"},
                    {"id": "bench", "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/v1/chat/completions", "/chat/completions"):
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("content-length", 0))
                req = json.loads(self.rfile.read(n))
                msgs = req.get("messages", [])
                system = "\n\n".join(m.get("content", "") for m in msgs
                                     if m.get("role") == "system"
                                     and m.get("content"))
                user = "\n\n".join(m.get("content", "") for m in msgs
                                   if m.get("role") != "system"
                                   and m.get("content"))
                reply = cli.chat(system, user)
                self._json(200, {
                    "id": f"sonnet-shim-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "model": cli.model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })
            except Exception as e:  # noqa: BLE001 - surface anything as a 500
                print(f"sonnet_shim: request failed: {e}", file=sys.stderr,
                      flush=True)
                self._json(500, {"error": str(e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--call-timeout", type=float, default=300.0)
    args = ap.parse_args()

    if not args.cli.exists():
        sys.exit(f"claude CLI not found at {args.cli}")
    cli = ClaudeCli(args.cli, args.model, args.call_timeout)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(cli))
    print(f"sonnet_shim: serving {args.model} on "
          f"http://127.0.0.1:{args.port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
