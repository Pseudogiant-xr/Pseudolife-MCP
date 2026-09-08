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

``--emulate-tools`` (opt-in, default off) adds OpenAI function calling on top
of that pure completion, for harnesses that cannot work without it. When on:
the request's ``tools`` are rendered into the system prompt with a generic
preamble asking for a bare ``{"tool_call": {"name": ..., "arguments": {...}}}``
object and nothing else; ``assistant``-with-``tool_calls`` and ``tool``-role
messages are folded into the stdin transcript as ``[assistant called tool N
with ARGS]`` / ``[tool N returned: CONTENT]``; and a reply that parses as that
object comes back as ``choices[0].message.tool_calls`` (``content`` null,
``finish_reason`` ``"tool_calls"``, ``arguments`` a JSON **string**, which is
what litellm feeds to ``json.loads``). Prose around the object is tolerated —
models narrate, and a call introduced with "Sure, let me check." is still a
call. A reply that only looks like an attempt is retried once asking for valid
JSON, then served as text — never as an unparsable ``arguments``.
``tool_choice: "none"`` suppresses the rendering; anything else is auto. With
the flag OFF the wire format is unchanged: tools are ignored and no
``tool_calls`` key is ever emitted.

The mode exists for the tau2-bench adapter (``evals/taubench_adapter.py``) and
belongs on an EVAL-ONLY port. It refuses to launch on either deployed shim
port (``PRODUCTION_SHIM_PORTS``): :8082 is the dream primary
(``ops/install-shim-autostart.ps1``) and :8086 the Codex shim
(``ops/install.ps1``), and both sets of callers expect the plain-completion
wire format.

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
    python evals/claude_shim.py [--port 8082] [--model claude-sonnet-5]
        [--cli PATH] [--call-timeout 300] [--emulate-tools]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402

_LOG = logging.getLogger("claude_shim")

# The `claude` CLI from PATH; PSEUDOLIFE_SHIM_CLAUDE_CLI or --cli overrides
# for installs whose binary isn't on PATH.
DEFAULT_CLI = Path(os.environ.get("PSEUDOLIFE_SHIM_CLAUDE_CLI")
                   or shutil.which("claude") or "claude")
# A fenced reply ("```json\n...\n```") would fail the extractor's json.loads.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
# Windows CreateProcess caps the command line at 32767 chars; leave margin.
_MAX_ARGV_SYSTEM = 24000


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out call and its descendants.

    ``Popen.kill()`` on Windows is ``TerminateProcess`` on the DIRECT child
    only. The CLI is a node program behind a wrapper (``claude.cmd`` →
    ``cmd.exe`` → node), so the real claude survives holding the stdout
    pipe — and the reaping ``communicate()`` then blocks forever with the
    serialization lock held, wedging every later call.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        # The child leads its own session (start_new_session in _run), so
        # killing the group takes its descendants too. proc.kill() alone
        # leaves a surviving grandchild holding the stdout pipe.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


# ── tool-call emulation (--emulate-tools; inert without the flag) ─────────
#
# Deliberately generic: the preamble names no domain, no benchmark and no
# tool, so the same text serves any harness. One call per turn is what the
# tau2 loop executes anyway (it runs calls sequentially), and asking for one
# keeps the reply a single small object that either parses or does not.

_TOOL_PREAMBLE = """\
You can call tools. Call AT MOST ONE tool per reply.

To call a tool, reply with ONLY this JSON object and nothing else — no prose
before or after it, no markdown fences, no explanation:

{"tool_call": {"name": "<tool name>", "arguments": {<arguments object>}}}

The arguments object must match that tool's parameter schema. The tool's
result is given back to you on the next turn, after which you can call
another tool or answer. If you do not need a tool, reply to the user in
plain text and do not mention this format.

Earlier turns of this conversation are shown to you with markers such as
[assistant called tool NAME with ARGS] and [tool NAME returned: RESULT].
Those markers are history rendered for you — never write them yourself. A
call is only ever the JSON object above.

Available tools:"""

# The deployed shims' ports: :8082 is the Claude shim the daemon routes dream
# extraction through (ops/install-shim-autostart.ps1) and :8086 the Codex shim
# (ops/install.ps1) — both pinned by tests/test_bench_production_port_guard.py.
# The emulating build serves a different wire format, and on Windows a second
# bind can even "succeed" while the incumbent keeps the traffic, so the
# emulation flag refuses them rather than trusting the bind to fail.
PRODUCTION_SHIM_PORTS = (8082, 8086)


def production_port_refusal(port: int) -> str | None:
    """Why ``--emulate-tools`` may not run on this port, or None."""
    if port in PRODUCTION_SHIM_PORTS:
        return (f"--emulate-tools is eval-only; :{port} is a deployed shim "
                "port whose callers expect the plain-completion wire format. "
                "Pick another port (the tau2 adapter's default is :8092).")
    return None


_RETRY_INSTRUCTION = (
    "Your previous reply looked like a tool call but was not valid JSON. "
    'Reply with ONLY the JSON object {"tool_call": {"name": ..., '
    '"arguments": {...}}} and nothing else, or with a plain-text answer '
    "containing no JSON at all.")


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def _as_text(content) -> str:
    """OpenAI content is a string or a list of typed parts."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("text"))
    return "" if content is None else str(content)


def _args_text(arguments) -> str:
    """The wire form of a call's arguments: already a JSON string on the way
    in, an object when the model produced one."""
    if isinstance(arguments, str):
        return arguments
    if arguments is None:
        return "{}"
    return json.dumps(arguments, ensure_ascii=False)


def tools_are_enabled(tool_choice) -> bool:
    """``tool_choice`` may be a string or a dict. Only "none" disables the
    rendering; a forced-function dict is treated as auto (the preamble
    already asks for at most one call, and the model sees only that tool's
    siblings — forcing is not worth a second prompt shape)."""
    if isinstance(tool_choice, str):
        return tool_choice.strip().lower() != "none"
    if isinstance(tool_choice, dict):
        return str(tool_choice.get("type", "")).lower() != "none"
    return True


def render_tools(tools) -> str:
    """The system-prompt block describing the request's tools. Empty string
    when there is nothing to render, so the caller can skip the append."""
    blocks = []
    for spec in tools or []:
        fn = spec.get("function") if isinstance(spec, dict) else None
        if not isinstance(fn, dict):
            fn = spec if isinstance(spec, dict) else {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        blocks.append(
            f"- name: {name}\n"
            f"  description: {fn.get('description', '') or ''}\n"
            f"  parameters (JSON Schema): "
            f"{json.dumps(params, ensure_ascii=False)}")
    return f"{_TOOL_PREAMBLE}\n" + "\n\n".join(blocks) if blocks else ""


def fold_messages(msgs) -> tuple[str, str]:
    """Split a tool-carrying message list into (system prompt, transcript).

    The CLI takes one system prompt and one stdin blob, so the tool turns are
    folded into the blob in a fixed textual form. Roles are labelled because
    an agent transcript is multi-turn — without labels the model cannot tell
    its own past replies from the user's."""
    system_parts: list[str] = []
    turns: list[str] = []
    names: dict[str, str] = {}          # tool_call_id -> tool name
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if content:
                system_parts.append(_as_text(content))
            continue
        if role == "tool":
            name = (names.get(m.get("tool_call_id"))
                    or m.get("name") or "tool")
            turns.append(f"[tool {name} returned: {_as_text(content)}]")
            continue
        calls = m.get("tool_calls") if role == "assistant" else None
        if content:
            label = "Assistant" if role == "assistant" else "User"
            turns.append(f"{label}: {_as_text(content)}")
        for call in calls or []:
            fn = call.get("function") if isinstance(call, dict) else None
            fn = fn if isinstance(fn, dict) else {}
            name = fn.get("name") or "tool"
            if isinstance(call, dict) and call.get("id"):
                names[call["id"]] = name
            turns.append(f"[assistant called tool {name} with "
                         f"{_args_text(fn.get('arguments'))}]")
    return "\n\n".join(system_parts), "\n\n".join(turns)


def _tool_call_span(reply: str) -> str | None:
    """The first ``{`` to the last ``}`` of a reply, if that span mentions
    ``tool_call``.

    Models narrate — "Sure, let me check." in front of the object, or a
    sign-off after the fence — and a call that arrives with prose around it
    is still a call. Requiring the reply to START with ``{`` turned every one
    of those into a chat turn, silently costing the episode the call it was
    making. Keyed on ``tool_call`` so an answer that merely quotes some JSON
    stays an answer.
    """
    text = reply or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    span = text[start:end + 1]
    return span if "tool_call" in span else None


# A reply that IS one of the history markers the transcript is folded with:
# the 2026-09-08 tau2 smoke saw the model imitate "[assistant called tool
# KB_search with {...}]" as its whole answer, twice, and the customer
# simulator then reacted to the marker text. Recovered as the call it names.
_MARKER_CALL_RE = re.compile(
    r"^\s*\[assistant called tool\s+([A-Za-z0-9_.-]+)\s+with\s+(\{.*\})\s*\]\s*$",
    re.DOTALL)


def _marker_call(reply: str) -> str | None:
    """The JSON tool-call text equivalent to an imitated history marker."""
    m = _MARKER_CALL_RE.match(_strip_fence(reply))
    if not m:
        return None
    return json.dumps({"tool_call": {"name": m.group(1),
                                     "arguments": m.group(2)}})


def looks_like_tool_call(reply: str) -> bool:
    """A reply that was TRYING to be a tool call — worth one retry."""
    text = _strip_fence(reply)
    if text.startswith("{") and "tool_call" in text:
        return True
    if _marker_call(reply) is not None:
        return True
    return _tool_call_span(reply) is not None


def _balance_braces(text: str) -> str:
    """Append the closing braces a JSON object is short of, counting only
    braces outside string literals.

    The 2026-09-09 full baseline lost five of its first 37 episodes to one
    reply shape: a wrapper tool whose own ``arguments`` field is a
    JSON-encoded string. The model wrote the escaped inner object correctly
    and then closed one brace short (713 chars, "Expecting ',' delimiter"
    at the very end); the retry closed one short again; the raw JSON went
    back to the customer as prose and the customer played along to
    max_steps. Balanced or over-closed text is returned unchanged — this
    never removes anything, and a reply that is wrong in any other way
    still fails ``json.loads`` afterwards.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return text + "}" * depth if depth > 0 else text


def parse_tool_reply(reply: str):
    """Turn a tool-call reply into OpenAI ``tool_calls`` entries, or None.

    ``arguments`` comes back as a JSON **string**: litellm calls
    ``json.loads`` on it and an episode dies on a parse failure, so anything
    that is not an object becomes ``{}`` rather than reaching the harness."""
    marker = _marker_call(reply)
    text = marker if marker is not None else _strip_fence(reply)
    if not text.startswith("{"):
        # A call the model introduced with prose, fenced or not.
        text = _tool_call_span(reply)
        if text is None:
            return None
    try:
        obj = json.loads(text)
    except ValueError:
        try:
            obj = json.loads(_balance_braces(text))
        except ValueError:
            return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("tool_call")
    raw = [raw] if raw is not None else obj.get("tool_calls")
    if not isinstance(raw, list):
        return None
    calls = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        fn = fn if isinstance(fn, dict) else item
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({
            "id": f"call_{secrets.token_hex(4)}",
            "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls or None


def resolve_model(requested: str | None, default: str) -> str:
    """Per-request model override (2026-08-02): a request naming a concrete
    Claude model wins over the launch default, so the daemon's Console
    Extractor panel can switch the dreamer model live without a shim
    restart. Anything else — the compose default "extractor", "bench",
    empty — keeps the launch default, preserving existing deploys."""
    if requested and requested.startswith("claude-"):
        return requested
    return default


class ClaudeCli:
    """One ``claude -p`` subprocess per call, serialized."""

    def __init__(self, cli: Path, model: str, call_timeout: float,
                 system_override: str | None = None,
                 reasoning_effort: str | None = None,
                 emulate_tools: bool = False):
        self.cli = cli
        self.model = model
        self.call_timeout = call_timeout
        self.system_override = system_override
        # --emulate-tools: read by the handler, which owns the prompt
        # rendering and reply parsing. Off = the pre-emulation wire format.
        self.emulate_tools = emulate_tools
        # Launch-default reasoning effort (claude --effort); a per-request
        # value wins. None = never pass the flag — the pre-knob behavior.
        self.reasoning_effort = reasoning_effort
        self.lock = threading.Lock()
        # Prompt heads already reported as an override miss, so the warning
        # below is one line per distinct prompt rather than one per call.
        self._override_miss_seen: set[str] = set()
        self.calls = 0
        self._health_ok: bool | None = None
        self._health_detail = ""
        self._health_at = 0.0
        self._health_refreshing = False

    def _run(self, cmd: list[str], payload: bytes) -> tuple[int, bytes, bytes]:
        """Spawn one call. Seam for tests, and the place the timeout kill-tree
        lives (``subprocess.run``'s timeout kills only the direct child)."""
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
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

    def _warn_override_missed(self, system: str) -> None:
        """Say so when ``--system-prompt-file`` did not apply to a call.

        A miss is NORMAL for every non-claims prompt on this endpoint: the
        override targets claims extraction only, and the daemon also sends
        events, relations, lessons, digest, outcome-inference and — when
        ``judge_url`` is unset — the four review-queue judge prompts down
        the same wire. It is not normal for the claims prompt, and from the
        outside the two are indistinguishable: the override just vanishes
        and the model gets the shipped text while the operator believes it
        got the variant. That is the same class of silent substitution that
        made ``sonnet_extractor_v4.md`` necessary, seen from the other side,
        so it gets a line in the log.

        Deduped per distinct prompt head: about ten prompts legitimately
        miss, and a per-call warning on each would teach an operator to
        ignore the one that matters. The EMPTY system prompt is filtered by
        the caller — ``_health_refresh`` sends one on every /health cache
        miss, and ``main()`` warms that cache before ``serve_forever``, so
        warning on it would put the loudest, earliest and wrongest line at
        the top of every shim's log.
        """
        head = system[:60]
        if head in self._override_miss_seen:
            return
        self._override_miss_seen.add(head)
        _LOG.warning(
            "--system-prompt-file did not apply: this prompt does not start "
            "with dream._SYSTEM_PROMPT, so the shipped text was sent "
            "unchanged (prompt begins %r). Expected for every non-claims "
            "prompt (events, relations, lessons, digest, outcome inference, "
            "the review-queue judges); for claims extraction it means the "
            "variant never reached the model.", head)

    def chat(self, system: str, user: str, model: str | None = None,
             effort: str | None = None) -> str:
        if self.system_override and system.startswith(_SYSTEM_PROMPT):
            # Swap the claims-extraction prompt prefix for the variant,
            # PRESERVING whatever the harness appended after it (vocab hint
            # etc.). Other prompts (relations, lessons) pass through
            # untouched — the override targets claims extraction only.
            system = self.system_override + system[len(_SYSTEM_PROMPT):]
        elif self.system_override and system:
            # `and system`: the /health probe sends an EMPTY system prompt
            # (see _health_refresh), which is not an override miss worth
            # reporting — it is a call that deliberately carries no prompt.
            self._warn_override_missed(system)
        cmd = [str(self.cli), "-p", "--model", model or self.model,
               "--output-format", "json",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--tools", ""]
        if effort or self.reasoning_effort:
            # Per-request effort wins over the launch default (mirrors the
            # model precedence); unset everywhere = no flag, the CLI's own
            # per-model default serves.
            cmd += ["--effort", effort or self.reasoning_effort]
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
            rc, stdout, stderr = self._run(cmd, user.encode("utf-8"))
        if rc != 0:
            raise RuntimeError(
                f"claude -p rc={rc}: "
                f"{stderr.decode('utf-8', 'replace')[:400]}")
        out = json.loads(stdout.decode("utf-8", "replace"))
        if out.get("is_error"):
            raise RuntimeError(
                f"claude -p error result: {str(out.get('result'))[:400]}")
        reply = (out.get("result") or "").strip()
        m = _FENCE_RE.match(reply)
        if m:
            reply = m.group(1).strip()
        print(f"claude_shim: call {n} ok "
              f"({time.monotonic() - t0:.1f}s, {len(reply)} chars)",
              flush=True)
        return reply

    _HEALTH_TTL = 300.0  # one trivial CLI call per 5 min keeps /health honest

    def health(self) -> tuple[bool, str]:
        """Real usability check: a trivial completion so a logged-out or
        broken CLI turns /health into 503 (the daemon's fallback probe treats
        that as primary-down). Stale-while-revalidate (2026-07-19): the check
        takes SECONDS while the daemon probes with a 3s timeout — a blocking
        refresh on cache expiry made every post-idle probe time out, so
        dreams silently fell back on a healthy shim (3/3 that day). A stale
        cache answers instantly with the last verdict and refreshes in a
        background thread; only an empty cache blocks (startup warms it)."""
        now = time.monotonic()
        if self._health_ok is None:
            return self._health_refresh()
        ok, detail = self._health_ok, self._health_detail  # pre-refresh verdict
        if (now - self._health_at >= self._HEALTH_TTL
                and not self._health_refreshing):
            self._health_refreshing = True
            threading.Thread(target=self._health_refresh, daemon=True).start()
        return ok, detail

    def _health_refresh(self) -> tuple[bool, str]:
        try:
            try:
                self.chat("", "Reply with exactly: OK")
                self._health_ok, self._health_detail = True, ""
            except Exception as e:  # noqa: BLE001 — any failure means unusable
                self._health_ok, self._health_detail = False, str(e)[:300]
            self._health_at = time.monotonic()
            return self._health_ok, self._health_detail
        finally:
            self._health_refreshing = False


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
                    {"id": m, "object": "model"}
                    for m in dict.fromkeys([
                        cli.model, "claude-opus-5", "claude-sonnet-5",
                        "claude-haiku-4-5", "claude-fable-5",
                        "extractor", "bench"])]})
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
                emulate = bool(getattr(cli, "emulate_tools", False))
                tools = req.get("tools")
                tools = tools if isinstance(tools, list) else []
                if emulate:
                    system, user = fold_messages(msgs)
                    rendered = (render_tools(tools)
                                if tools_are_enabled(req.get("tool_choice"))
                                else "")
                    if rendered:
                        system = f"{system}\n\n{rendered}" if system \
                            else rendered
                else:
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
                calls = parse_tool_reply(reply) if emulate else None
                retried = False
                if emulate and calls is None and looks_like_tool_call(reply):
                    # Near-JSON: one more attempt, then serve the ORIGINAL
                    # reply as text. Never raise, and never hand the harness
                    # an `arguments` string it cannot json.loads.
                    retried = True
                    calls = parse_tool_reply(
                        cli.chat(system, f"{user}\n\n{_RETRY_INSTRUCTION}",
                                 model=model, effort=effort))
                _LOG.info("chat.completions: emulate=%s n_tools=%d "
                          "tool_call=%s retried=%s",
                          "on" if emulate else "off", len(tools),
                          "yes" if calls else "no", "yes" if retried else "no")
                if calls:
                    message = {"role": "assistant", "content": None,
                               "tool_calls": calls}
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": reply}
                    finish = "stop"
                self._json(200, {
                    "id": f"claude-shim-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": finish,
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0},
                })
            except Exception as e:  # noqa: BLE001 - surface anything as a 500
                print(f"claude_shim: request failed: {e}", file=sys.stderr,
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
    ap.add_argument("--reasoning-effort", default=None,
                    help="launch-default claude --effort level "
                         "(low/medium/high/xhigh/max); a request's "
                         "reasoning_effort wins per call. Unset = the CLI's "
                         "own per-model default")
    ap.add_argument("--emulate-tools", action="store_true",
                    help="emulate OpenAI function calling in the prompt for "
                         "harnesses that require it (the tau2-bench adapter). "
                         "Off by default and EVAL-ONLY: refused on the "
                         "deployed shim ports :8082 and :8086")
    ap.add_argument("--system-prompt-file", type=Path, default=None,
                    help="replace the production _SYSTEM_PROMPT prefix with "
                         "this file's body (text after the first '---' line, "
                         "or the whole file if no separator); the harness's "
                         "appended vocab hint is preserved")
    return ap.parse_args(argv)


class ShimHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that fails loudly when the port is taken.

    ``HTTPServer.allow_reuse_address`` sets SO_REUSEADDR. On POSIX that only
    lets a restart rebind through TIME_WAIT. On Windows it ALSO lets a second
    socket bind a port that is already in LISTEN, and the first socket keeps
    the traffic — so a duplicate shim launched over a live one started
    "successfully", served nothing, and had no error to log (the 2026-09-06
    re-install over the live :8082 shim; probed 2026-09-07 on Python 3.11.9:
    with ``allow_reuse_address = False`` the second bind fails with
    WinError 10048). Windows does not need SO_REUSEADDR to rebind after a
    restart, so it is dropped there and kept everywhere else.
    """

    allow_reuse_address = os.name != "nt"


def main():
    args = _parse_args()

    if not args.cli.exists():
        sys.exit(f"claude CLI not found at {args.cli}")
    override = None
    if args.system_prompt_file:
        raw = args.system_prompt_file.read_text(encoding="utf-8")
        override = raw.split("\n---\n", 1)[-1].strip()
        print(f"claude_shim: system prompt override from "
              f"{args.system_prompt_file} ({len(override)} chars)", flush=True)
    if args.emulate_tools:
        # The per-request line is INFO; without a handler the emulation path
        # would run silently. Configured ONLY under the flag so an ordinary
        # launch keeps exactly the output it had.
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        refusal = production_port_refusal(args.port)
        if refusal:
            sys.exit(refusal)
        print("claude_shim: tool-call emulation ON (OpenAI function calling "
              "rendered into the prompt)", flush=True)
    cli = ClaudeCli(args.cli, args.model, args.call_timeout,
                    system_override=override,
                    reasoning_effort=args.reasoning_effort,
                    emulate_tools=args.emulate_tools)
    # Warm the health cache before serving: the only blocking health path is
    # an empty cache, and this guarantees no request ever hits it.
    ok, detail = cli.health()
    print(f"claude_shim: health warm -> {'ok' if ok else detail}", flush=True)
    srv = ShimHTTPServer((args.host, args.port), make_handler(cli))
    print(f"claude_shim: serving {args.model} on "
          f"http://{args.host}:{args.port}/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
