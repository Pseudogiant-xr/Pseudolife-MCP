"""The plugin's SubagentStart / SubagentStop hook (schema v50).

A Claude Code subagent shares its parent's shim and board address, so peers
see it as an entry in the parent's ``children``. subagent-board.sh keeps
that list current: one bounded POST to ``/api/hook/subagent`` per event,
naming the session's board address (the ``<key>.agent`` file the shim writes
beside its digest) and the subagent's ``agent_id`` and ``agent_type`` from
the hook payload. It fails open and prints nothing. Codex loads the same
hooks.json: there (lifecycle.ps1 on Windows, the bash script in Codex
context elsewhere) it is a no-op, since the shim links Codex's native
subagents to their parent itself.
"""
import hashlib
import json
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_codex_hooks import HOOK_PROCESS_TIMEOUT, ROOT, bash_exe, isolated_env, pwsh_run

HOOK = ROOT / "plugin/hooks/subagent-board.sh"
SESSION = "cf225c95-1abe-4d54-8cc8-6f91b707d3ad"
CHILD = "a698026ca4ba524e9"
AGENT = "e190deba1636486e9dade03e481bae59"


def _key(session_id=SESSION):
    return hashlib.sha256(session_id.encode()).hexdigest()


def _payload(event="SubagentStart", **extra):
    """The fields Claude Code 2.1.283 sent a plugin hook (measured
    2026-09-30), less the transcript paths' values."""
    body = {"session_id": SESSION, "transcript_path": "/fixture/parent.jsonl", "cwd": "/fixture",
            "prompt_id": "dc38d93a", "agent_id": CHILD, "agent_type": "general-purpose",
            "hook_event_name": event}
    if event == "SubagentStop":
        body.update(stop_hook_active=False, agent_transcript_path="/fixture/child.jsonl",
                    last_assistant_message="Report delivered to caller.")
    body.update(extra)
    return json.dumps({k: v for k, v in body.items() if v is not None})


class Daemon:
    """A fixture daemon recording each request's method, path, query and
    bearer; ``status`` is what it answers."""

    def __init__(self, status=200):
        self.requests = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                url = urlsplit(self.path)
                daemon.requests.append((self.command, url.path, parse_qs(url.query),
                                        self.headers.get("Authorization")))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"ok\n")

            do_GET = do_POST = _record

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def daemon():
    served = Daemon()
    yield served
    served.close()


def _env(tmp_path, url, **extra):
    env = isolated_env(tmp_path / "home")
    for name in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID",
                 "PSEUDOLIFE_AGENT_COORDINATION"):
        env.pop(name, None)
    env.update({"PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "digests"),
                "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin"),
                "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": SESSION,
                "PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_TOKEN": "fixture-token"})
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


def _agent_file(tmp_path, text=AGENT + "\n", key=None):
    directory = tmp_path / "digests"
    directory.mkdir(exist_ok=True)
    (directory / f"{key or _key()}.agent").write_text(text)
    return directory


def _run(env, payload, *args):
    return subprocess.run([bash_exe(), str(HOOK), *args], input=payload, env=env,
                          capture_output=True, text=True, timeout=HOOK_PROCESS_TIMEOUT)


def _query(child=CHILD, kind="general-purpose", event="start"):
    return {"agent": [AGENT], "event": [event], "child": [child], **({"type": [kind]} if kind else {})}


def test_hooks_json_binds_both_subagent_events_to_the_script():
    hooks = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event, argument, native in (("SubagentStart", "start", "SubagentBoardStart"),
                                    ("SubagentStop", "stop", "SubagentBoardStop")):
        [group] = hooks[event]
        [hook] = group["hooks"]
        assert hook["command"] == f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/subagent-board.sh" {argument}'
        assert hook["commandWindows"].endswith(f"lifecycle.ps1\" -Event {native}")
        # Synchronous, so a stop can never overtake its own start, and short:
        # the script's one request takes at most two seconds.
        assert hook["timeout"] == 5 and "async" not in hook
        assert native in (ROOT / "plugin/hooks/lifecycle.ps1").read_text(encoding="utf-8")


@pytest.mark.parametrize("event,argument", [("SubagentStart", "start"), ("SubagentStop", "stop")])
def test_one_request_names_the_session_address_and_the_subagent(tmp_path, daemon, event, argument):
    _agent_file(tmp_path)
    # SubagentStop's closing message may quote the same keys; only the
    # top-level ones count.
    message = 'done: {"agent_id":"quoted-id","agent_type":"quoted-type","session_id":"x"}'
    payload = _payload(event, last_assistant_message=message)
    result = _run(_env(tmp_path, daemon.url), payload, argument)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert daemon.requests == [("POST", "/api/hook/subagent", _query(event=argument),
                                "Bearer fixture-token")]


def test_the_session_record_names_the_shims_key(tmp_path, daemon):
    """After /clear the shim keeps its launch-time key; the SessionStart
    record confirmed for this session names it, as stop-wake.sh reads it."""
    shim_key = "b" * 64
    directory = _agent_file(tmp_path, key=shim_key)
    # LF, as the SessionStart hook writes it: a CRLF record is refused.
    (directory / "claude-4242.host").write_bytes(f"{shim_key}\n{_key()}\n".encode())
    _run(_env(tmp_path, daemon.url, CLAUDE_PID="4242"), _payload(), "start")
    assert [r[2] for r in daemon.requests] == [_query()]


def test_a_type_the_daemon_would_refuse_is_left_out(tmp_path, daemon):
    _agent_file(tmp_path)
    _run(_env(tmp_path, daemon.url), _payload(agent_type="bad type&x=1"), "start")
    _run(_env(tmp_path, daemon.url), _payload(agent_type=None), "start")
    assert [r[2] for r in daemon.requests] == [_query(kind=None), _query(kind=None)]


@pytest.mark.parametrize("case", [
    "codex-marker", "codex-plugin-root", "not-claude", "another-session", "coordination-off",
    "no-address", "malformed-address", "odd-child", "long-child", "odd-session", "no-argument",
    "unknown-argument",
])
def test_nothing_is_sent_where_the_hook_has_no_business(tmp_path, daemon, case):
    env = _env(tmp_path, daemon.url)
    payload, args = _payload(), ["start"]
    _agent_file(tmp_path)
    if case == "codex-marker":
        env["PSEUDOLIFE_CODEX_HOOK"] = "1"
    elif case == "codex-plugin-root":
        env["PLUGIN_ROOT"] = env["CLAUDE_PLUGIN_ROOT"]
    elif case == "not-claude":
        env.pop("CLAUDECODE")
    elif case == "another-session":
        # A Codex run inside a Claude Bash tool inherits the outer session.
        env["CLAUDE_CODE_SESSION_ID"] = "the-outer-session"
    elif case == "coordination-off":
        env["PSEUDOLIFE_AGENT_COORDINATION"] = " Off "
    elif case == "no-address":
        (tmp_path / "digests" / f"{_key()}.agent").unlink()
    elif case == "malformed-address":
        _agent_file(tmp_path, "not-an-agent-id\n")
    elif case == "odd-child":
        payload = _payload(agent_id="../../x")
    elif case == "long-child":
        payload = _payload(agent_id="a" * 65)
    elif case == "odd-session":
        payload = _payload(session_id="x y")
        env["CLAUDE_CODE_SESSION_ID"] = "x y"
    elif case == "no-argument":
        args = []
    elif case == "unknown-argument":
        args = ["restart"]
    result = _run(env, payload, *args)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert daemon.requests == []


def test_it_fails_open_when_the_daemon_is_down_or_refuses(tmp_path):
    _agent_file(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = _run(_env(tmp_path, f"http://127.0.0.1:{port}"), _payload(), "start")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    refusing = Daemon(status=500)
    try:
        result = _run(_env(tmp_path, refusing.url), _payload(), "stop")
    finally:
        refusing.close()
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert len(refusing.requests) == 1


@pytest.mark.parametrize("url", ["http://127.0.0.1:1/path", "ftp://127.0.0.1", "http://u@host"])
def test_an_unsafe_daemon_url_sends_nothing(tmp_path, url):
    _agent_file(tmp_path)
    result = _run(_env(tmp_path, url), _payload(), "start")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("native", ["SubagentBoardStart", "SubagentBoardStop"])
def test_lifecycle_ps1_subagent_events_are_a_silent_no_op(tmp_path, daemon, native):
    """Claude never runs commandWindows; Codex does, and its native
    subagents are linked by the shim. Falling through would reach the
    SessionEnd request at the bottom of lifecycle.ps1 and close the episode,
    so the daemon is what this watches."""
    _agent_file(tmp_path)
    event = "SubagentStart" if native.endswith("Start") else "SubagentStop"
    result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", native,
                      input=_payload(event), env=_env(tmp_path, daemon.url))
    assert (result.stdout, result.stderr, daemon.requests) == ("", "", [])
