"""OpenAI-compatible shim serving GPT-5.6 via headless ``codex exec``.

The OpenAI-side twin of ``claude_shim.py``: bridges the bench harness and the
daemon's dream pass (both speak ``/v1/chat/completions``) to the Codex CLI
running on the user's ChatGPT plan — included usage, no API key. Each POST
spawns one ``codex exec`` subprocess.

Three contracts differ from the Claude CLI and drive the design here:

  * **No per-call system-prompt flag.** Codex takes base instructions from a
    FILE (``model_instructions_file``), which *replaces* its built-in
    coding-agent instructions rather than appending to them. That is exactly
    what a pure-completion shim wants — the persona goes away and the
    extractor contract lands in the slot the model weights as instructions.
    Calls are serialized, so one temp file rewritten per call is safe.
  * **Reply comes from the event stream, not bare stdout.** ``--json`` emits
    JSONL; the answer is the last ``item.completed`` whose ``item.type`` is
    ``agent_message``. Parsing that beats trusting stdout to stay banner-free,
    and it lets ``turn.failed`` surface as an error instead of masquerading as
    an empty extraction (the dream advances its cursor on empty results, so a
    silent failure would skip those memories permanently).
  * **It is an agent, not a completion endpoint.** Tools are disabled
    explicitly (``web_search=disabled``, ``features.shell_tool=false``), the
    sandbox stays read-only, and ``--ephemeral`` keeps session files off disk.

Registered as the ``terra`` rung/extractor. Like ``sonnet-5`` it stays OUT of
LADDER_ORDER — the default sweep remains sovereign-only; invoke explicitly
with ``--rung terra`` / ``--extractor terra``.

Notes:
  * Calls are serialized with a lock (one in-flight subscription call at a
    time is deliberate, and the bench is sequential anyway).
  * ``response_format``/``temperature`` are ignored — the CLI exposes neither.
    Markdown code fences are stripped so a fenced JSON answer still parses.
  * Requires a signed-in CLI (``codex login`` once, interactively); a
    logged-out CLI surfaces as HTTP 500 / 503 on ``/health``.

Endpoints: POST /v1/chat/completions, GET /health, GET /v1/models.

Usage:
    python evals/codex_shim.py [--port 8086] [--model gpt-5.6-terra]
        [--cli PATH] [--call-timeout 300]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402

# The `codex` CLI from PATH; PSEUDOLIFE_SHIM_CODEX_CLI or --cli overrides
# for installs whose binary isn't on PATH.
DEFAULT_CLI = Path(os.environ.get("PSEUDOLIFE_SHIM_CODEX_CLI")
                   or shutil.which("codex") or "codex")
# A fenced reply ("```json\n...\n```") would fail the extractor's json.loads.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def resolve_model(requested: str | None, default: str) -> str:
    """Per-request model override, mirroring claude_shim.resolve_model: a
    request naming a concrete OpenAI model (``gpt-*``/``codex-*``) wins over
    the launch default, so the daemon's Console Dreamer card can switch the
    dreamer model live without a shim restart. Anything else — the compose
    default "extractor", "bench", empty — keeps the launch default,
    preserving existing deploys."""
    if requested and (requested.startswith("gpt-")
                      or requested.startswith("codex-")):
        return requested
    return default


class CodexTurnFailed(RuntimeError):
    """The stream carried a structured reason (``turn.failed`` / ``error``)."""


class CodexNoReply(RuntimeError):
    """The stream carried no ``agent_message`` — i.e. NO reason at all.

    Distinct from :class:`CodexTurnFailed` because it is the shape of every
    failure that dies before the turn starts (logged out, bad ``--model``,
    rejected ``-c`` key): no JSON events, real cause on stderr. Conflating
    the two lets an empty stream overwrite the only useful diagnostic.
    """


def _resolve_cli(cli: Path) -> Path | None:
    """Resolve to a path ``subprocess`` can actually launch, or None.

    Windows ``CreateProcess`` appends only ``.exe`` — it does NOT consult
    ``PATHEXT`` — so a bare ``codex`` that ``which`` finds as ``codex.cmd``
    (the npm install shape) must be resolved before it reaches argv[0].
    """
    if cli.exists():
        return cli
    found = shutil.which(str(cli))
    if found:
        return Path(found)
    # Official Windows installer: codex.exe lives in a rotating
    # %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ dir and is NOT on PATH
    # (verified live 2026-08-31). Globbing the newest at every startup keeps
    # an at-logon task working across auto-updates, where a baked path dies
    # with the old hash dir. Bare-name lookups only — an explicit path the
    # caller passed is never second-guessed.
    if os.name == "nt" and str(cli) in ("codex", "codex.exe"):
        return _official_install_glob()
    return None


def _official_install_glob() -> Path | None:
    """Newest codex.exe under the official installer layout, or None.

    Split from :func:`_resolve_cli` so tests can exercise the newest-wins
    glob on any platform — patching ``os.name`` to fake Windows makes
    ``pathlib.Path`` construct ``WindowsPath`` on POSIX and explode."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    hits = sorted(Path(base, "OpenAI", "Codex", "bin").glob("*/codex.exe"),
                  key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out call and its descendants.

    ``Popen.kill()`` on Windows is ``TerminateProcess`` on the DIRECT child
    only. When the CLI is ``codex.cmd`` that child is ``cmd.exe`` and the
    real (node) codex survives, holding an in-flight subscription call —
    breaking the one-at-a-time invariant the lock exists to enforce.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        # The child leads its own session (start_new_session in _run), so
        # killing the group takes its descendants too. proc.kill() alone
        # leaves a surviving grandchild holding the stdout pipe, and the
        # reaping communicate() then blocks forever with the lock held.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _parse_reply(stdout: str) -> str:
    """Pull the final assistant text out of a ``codex exec --json`` stream.

    Raises on a failed turn or a stream carrying no ``agent_message`` — the
    caller must be able to tell breakage from a genuinely empty extraction.
    """
    reply: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue                      # non-JSON banner/progress line
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind in ("turn.failed", "error"):
            err = ev.get("error") or {}
            msg = (err.get("message") if isinstance(err, dict) else None) \
                or ev.get("message") or json.dumps(ev)[:200]
            raise CodexTurnFailed(f"codex exec failed: {msg}")
        if kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                reply = item.get("text") or ""
    if reply is None:
        raise CodexNoReply(
            f"codex exec produced no agent_message (stream: {stdout[:200]!r})")
    reply = reply.strip()
    m = _FENCE_RE.match(reply)
    return m.group(1).strip() if m else reply


class CodexCli:
    """One ``codex exec`` subprocess per call, serialized."""

    def __init__(self, cli: Path, model: str, call_timeout: float,
                 system_override: str | None = None,
                 health_ttl: float = 300.0,
                 reasoning_effort: str | None = None):
        self.cli = cli
        self.model = model
        self.call_timeout = call_timeout
        self.system_override = system_override
        # Launch-default reasoning effort; a per-request value wins (mirrors
        # resolve_model). None = never pass the -c key, so the CLI inherits
        # the host's ~/.codex/config.toml — the pre-knob behavior.
        self.reasoning_effort = reasoning_effort
        # Refresh cadence for /health — every refresh is a REAL CLI call, so
        # on a metered/free ChatGPT tier the default (300s ≈ 288 calls/day)
        # is real spend; the autostart installer raises it via --health-ttl.
        self._health_ttl = health_ttl
        self.lock = threading.Lock()
        self.calls = 0
        self._instr: Path | None = None
        self._health_ok: bool | None = None
        self._health_detail = ""
        self._health_at = 0.0
        self._health_refreshing = False

    def _argv(self, instructions: Path | None,
              model: str | None = None,
              effort: str | None = None) -> list[str]:
        """Argv for one pure completion. ``instructions`` (when given) replaces
        Codex's built-in agent instructions with the request's system message."""
        argv = [str(self.cli), "exec",
                "--model", model or self.model,
                "--json",
                "--sandbox", "read-only",
                "--ephemeral",
                "-c", "web_search=disabled",
                "-c", "features.shell_tool=false"]
        if effort or self.reasoning_effort:
            argv += ["-c", "model_reasoning_effort="
                     f"{effort or self.reasoning_effort}"]
        if instructions is not None:
            argv += ["-c", f"model_instructions_file={instructions}"]
        argv.append("-")                  # prompt arrives on stdin
        return argv

    def _instructions_path(self) -> Path:
        if self._instr is None:
            fd, p = tempfile.mkstemp(prefix="codex-shim-instr-", suffix=".md")
            os.close(fd)
            self._instr = Path(p)
        return self._instr

    def _run(self, argv: list[str], payload: bytes) -> tuple[int, bytes, bytes]:
        """Spawn one call. Seam for tests, and the place the timeout kill-tree
        lives (``subprocess.run``'s timeout kills only the direct child)."""
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                start_new_session=(os.name != "nt"))
        try:
            out, err = proc.communicate(payload, timeout=self.call_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()          # reap, so no zombie holds the pipes
            raise
        return proc.returncode, out, err

    def chat(self, system: str, user: str, model: str | None = None,
             effort: str | None = None) -> str:
        if self.system_override and system.startswith(_SYSTEM_PROMPT):
            # Swap the claims-extraction prompt prefix for the variant,
            # PRESERVING whatever the harness appended after it (vocab hint
            # etc.). Other prompts (relations, lessons) pass through
            # untouched — the override targets claims extraction only.
            system = self.system_override + system[len(_SYSTEM_PROMPT):]
        with self.lock:
            instructions = None
            if system:
                instructions = self._instructions_path()
                instructions.write_text(system, encoding="utf-8")
            self.calls += 1
            n = self.calls
            t0 = time.monotonic()
            rc, out, err = self._run(self._argv(instructions, model, effort),
                                     user.encode("utf-8"))
        if rc != 0:
            # Prefer a STRUCTURED reason; a stream with no agent_message
            # carries no reason at all, so stderr stays the diagnostic.
            detail = err.decode("utf-8", "replace")[:400]
            try:
                _parse_reply(out.decode("utf-8", "replace"))
            except CodexTurnFailed as e:
                detail = str(e)
            except CodexNoReply:
                pass
            raise RuntimeError(f"codex exec rc={rc}: {detail}")
        reply = _parse_reply(out.decode("utf-8", "replace"))
        print(f"codex_shim: call {n} ok "
              f"({time.monotonic() - t0:.1f}s, {len(reply)} chars)",
              flush=True)
        return reply

    def health(self) -> tuple[bool, str]:
        """Real usability check: a trivial completion so a logged-out or
        broken CLI turns /health into 503 (the daemon's fallback probe treats
        that as primary-down). Stale-while-revalidate, inherited from
        claude_shim (2026-07-19): the check takes SECONDS while the daemon
        probes with a 3s timeout, so a blocking refresh on cache expiry makes
        every post-idle probe time out and dreams silently fall back on a
        healthy shim. A stale cache answers instantly with the last verdict
        and refreshes in a background thread; only an empty cache blocks
        (startup warms it)."""
        now = time.monotonic()
        if self._health_ok is None:
            return self._health_refresh()
        ok, detail = self._health_ok, self._health_detail  # pre-refresh verdict
        if (now - self._health_at >= self._health_ttl
                and not self._health_refreshing):
            self._health_refreshing = True
            threading.Thread(target=self._health_refresh, daemon=True).start()
        return ok, detail

    def _health_refresh(self) -> tuple[bool, str]:
        try:
            try:
                # NON-EMPTY system on purpose: an empty one omits
                # -c model_instructions_file entirely, so /health would stay
                # green while every real extraction ran with Codex's built-in
                # coding-agent persona (or died on a rejected -c key). The
                # probe must load-bear the mechanism the shim exists for.
                self.chat("Reply with exactly: OK", "ping")
                self._health_ok, self._health_detail = True, ""
            except Exception as e:  # noqa: BLE001 — any failure means unusable
                self._health_ok, self._health_detail = False, str(e)[:300]
            self._health_at = time.monotonic()
            return self._health_ok, self._health_detail
        finally:
            self._health_refreshing = False


def make_handler(cli: CodexCli):
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
                    {"id": m, "object": "model"}
                    for m in dict.fromkeys([
                        cli.model, "gpt-5.6-sol", "gpt-5.6-terra",
                        "gpt-5.6-luna", "extractor", "bench"])]})
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
                model = resolve_model(req.get("model"), cli.model)
                # Per-request effort (the daemon's effort knob rides the
                # request body); non-string/blank means unset.
                effort = req.get("reasoning_effort")
                effort = (effort.strip()
                          if isinstance(effort, str) and effort.strip()
                          else None)
                reply = cli.chat(system, user, model=model, effort=effort)
                self._json(200, {
                    "id": f"codex-shim-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })
            except Exception as e:  # noqa: BLE001 - surface anything as a 500
                print(f"codex_shim: request failed: {e}", file=sys.stderr,
                      flush=True)
                self._json(500, {"error": str(e)})

    return Handler


def _parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    ap.add_argument("--model", default="gpt-5.6-terra")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; on Linux Docker Engine the daemon "
                         "container reaches the host via the docker bridge "
                         "IP (host-gateway), so bind that (e.g. 172.17.0.1) "
                         "instead of loopback — 0.0.0.0 exposes the "
                         "unauthenticated shim to the LAN")
    ap.add_argument("--port", type=int, default=8086,
                    help="8082 is claude_shim's, 8083/8084 its opus-5/fable-5 "
                         "ceiling-rung instances, 8085 the events-teacher "
                         "shim default (distill_datagen_events.py); "
                         "side-by-side needs a free port")
    ap.add_argument("--call-timeout", type=float, default=300.0)
    ap.add_argument("--health-ttl", type=float, default=300.0,
                    help="seconds between /health refreshes; every refresh "
                         "is a real CLI call (metered spend on a free "
                         "ChatGPT tier), so autostart installs raise this")
    ap.add_argument("--system-prompt-file", type=Path, default=None,
                    help="replace the production _SYSTEM_PROMPT prefix with "
                         "this file's body (text after the first '---' line, "
                         "or the whole file if no separator); the harness's "
                         "appended vocab hint is preserved")
    ap.add_argument("--reasoning-effort", default=None,
                    help="launch-default model_reasoning_effort "
                         "(minimal/low/medium/high/xhigh); a request's "
                         "reasoning_effort wins per call. Unset = the CLI "
                         "inherits the host's ~/.codex/config.toml")
    return ap.parse_args(argv)


def main():
    args = _parse_args()

    resolved = _resolve_cli(args.cli)
    if resolved is None:
        sys.exit(f"codex CLI not found at {args.cli}")
    args.cli = resolved          # argv[0] must be launchable, not just found
    override = None
    if args.system_prompt_file:
        raw = args.system_prompt_file.read_text(encoding="utf-8")
        override = raw.split("\n---\n", 1)[-1].strip()
        print(f"codex_shim: system prompt override from "
              f"{args.system_prompt_file} ({len(override)} chars)", flush=True)
    cli = CodexCli(args.cli, args.model, args.call_timeout,
                   system_override=override, health_ttl=args.health_ttl,
                   reasoning_effort=args.reasoning_effort)
    # Warm the health cache before serving: the only blocking health path is
    # an empty cache, and this guarantees no request ever hits it.
    ok, detail = cli.health()
    print(f"codex_shim: health warm -> {'ok' if ok else detail}", flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(cli))
    print(f"codex_shim: serving {args.model} on "
          f"http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
