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
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402

# The `claude` CLI from PATH; PSEUDOLIFE_SHIM_CLAUDE_CLI or --cli overrides
# for installs whose binary isn't on PATH.
DEFAULT_CLI = Path(os.environ.get("PSEUDOLIFE_SHIM_CLAUDE_CLI")
                   or shutil.which("claude") or "claude")
# A fenced reply ("```json\n...\n```") would fail the extractor's json.loads.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Windows CreateProcess caps the command line at 32767 chars; leave margin.
_MAX_ARGV_SYSTEM = 24000


class ClaudeCli:
    """One ``claude -p`` subprocess per call, serialized."""

    def __init__(self, cli: Path, model: str, call_timeout: float,
                 system_override: str | None = None):
        self.cli = cli
        self.model = model
        self.call_timeout = call_timeout
        self.system_override = system_override
        self.lock = threading.Lock()
        self.calls = 0
        self._health_ok: bool | None = None
        self._health_detail = ""
        self._health_at = 0.0

    def chat(self, system: str, user: str) -> str:
        if self.system_override and system.startswith(_SYSTEM_PROMPT):
            # Swap the claims-extraction prompt prefix for the variant,
            # PRESERVING whatever the harness appended after it (vocab hint
            # etc.). Other prompts (relations, lessons) pass through
            # untouched — the override targets claims extraction only.
            system = self.system_override + system[len(_SYSTEM_PROMPT):]
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

    _HEALTH_TTL = 300.0  # one trivial CLI call per 5 min keeps /health honest

    def health(self) -> tuple[bool, str]:
        """Real usability check: run a trivial completion so a logged-out or
        broken CLI turns /health into 503 (the daemon's fallback probe treats
        that as primary-down). Cached for _HEALTH_TTL seconds."""
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_at < self._HEALTH_TTL:
            return self._health_ok, self._health_detail
        try:
            self.chat("", "Reply with exactly: OK")
            self._health_ok, self._health_detail = True, ""
        except Exception as e:  # noqa: BLE001 — any failure means unusable
            self._health_ok, self._health_detail = False, str(e)[:300]
        self._health_at = now
        return self._health_ok, self._health_detail


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
                ok, detail = cli.health()
                if ok:
                    self._json(200, {"status": "ok"})
                else:
                    self._json(503, {"status": "cli_error", "detail": detail})
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


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; on Linux Docker Engine the daemon "
                         "container reaches the host via the docker bridge "
                         "IP (host-gateway), so bind that (e.g. 172.17.0.1) "
                         "instead of loopback — 0.0.0.0 exposes the "
                         "unauthenticated shim to the LAN")
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--call-timeout", type=float, default=300.0)
    ap.add_argument("--system-prompt-file", type=Path, default=None,
                    help="replace the production _SYSTEM_PROMPT prefix with "
                         "this file's body (text after the first '---' line, "
                         "or the whole file if no separator); the harness's "
                         "appended vocab hint is preserved")
    return ap.parse_args(argv)


def main():
    args = _parse_args()

    if not args.cli.exists():
        sys.exit(f"claude CLI not found at {args.cli}")
    override = None
    if args.system_prompt_file:
        raw = args.system_prompt_file.read_text(encoding="utf-8")
        override = raw.split("\n---\n", 1)[-1].strip()
        print(f"sonnet_shim: system prompt override from "
              f"{args.system_prompt_file} ({len(override)} chars)", flush=True)
    cli = ClaudeCli(args.cli, args.model, args.call_timeout,
                    system_override=override)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(cli))
    print(f"sonnet_shim: serving {args.model} on "
          f"http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
