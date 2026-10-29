"""claude_shim /health must reflect real CLI usability (a logged-out CLI
answers 503 so the daemon's fallback probe sees primary-down)."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "claude_shim", REPO / "evals" / "claude_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["claude_shim"] = shim
spec.loader.exec_module(shim)

from pseudolife_memory.memory.dream import _SYSTEM_PROMPT  # noqa: E402


def _cli(monkeypatch, chat_ok: bool):
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 30.0)
    if chat_ok:
        monkeypatch.setattr(cli, "chat", lambda s, u: "OK")
    else:
        def _fail(s, u):
            raise RuntimeError("claude -p error result: Not logged in")
        monkeypatch.setattr(cli, "chat", _fail)
    return cli


def test_parse_args_host_defaults_to_loopback():
    # --host exists for Linux host-gateway reachability (issue #11): the
    # container reaches the host via the docker bridge IP, so a 127.0.0.1
    # bind is invisible to it. Default stays loopback-only.
    assert shim._parse_args([]).host == "127.0.0.1"
    assert shim._parse_args(["--host", "172.17.0.1"]).host == "172.17.0.1"


def test_health_fails_when_cli_errors(monkeypatch):
    ok, detail = _cli(monkeypatch, False).health()
    assert ok is False and "Not logged in" in detail


def test_health_result_is_cached(monkeypatch):
    cli = _cli(monkeypatch, True)
    assert cli.health()[0] is True
    calls = {"n": 0}

    def _boom(s, u):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    assert cli.health()[0] is True          # served from cache
    assert calls["n"] == 0


def test_health_stale_cache_served_while_revalidating(monkeypatch):
    # 2026-07-19: the health check runs a REAL completion (seconds) while the
    # daemon probes /health with a 3s timeout — a BLOCKING refresh on cache
    # expiry made every post-idle probe time out, so dreams silently fell
    # back on a healthy shim (3/3 live dreams that day). A stale cache must
    # answer instantly with the last verdict and refresh in the background;
    # the refreshed verdict serves the NEXT probe.
    cli = _cli(monkeypatch, True)
    assert cli.health()[0] is True           # warm
    calls = {"n": 0}
    # The refresh is held until the test releases it, so the stale answer
    # must arrive while the refresh is still running: an ordering check, not
    # a wall-clock budget a stalled runner can overrun.
    release, finished = threading.Event(), threading.Event()

    def _boom(s, u):
        calls["n"] += 1
        release.wait(30)
        finished.set()
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    cli._health_at = time.monotonic() - 301  # expire the cache

    try:
        ok, _ = cli.health()
        assert ok is True                    # stale verdict, served instantly
        assert not finished.is_set()         # ... before the refresh finished
    finally:
        release.set()

    deadline = time.monotonic() + 5.0        # background refresh lands
    while calls["n"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    while cli._health_ok is not False and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cli.health()[0] is False
    assert calls["n"] == 1                   # exactly one refresh, no stampede


# ── timeout kill path (mirrors test_codex_shim_health) ─────────────────────


def test_timeout_kills_the_whole_process_tree(monkeypatch):
    # subprocess timeout kills only the DIRECT child, then reaps with an
    # unbounded communicate(). The CLI is a node program behind a wrapper
    # (claude.cmd -> cmd.exe -> node on Windows; a shell shim on POSIX), so
    # the real claude survives holding the stdout pipe — the reap blocks
    # forever with the serialization lock held, wedging every later call.
    # The ORDER is the contract: kill the tree first, THEN reap — a reap
    # before the kill is exactly the wedge being fixed.
    seq = []

    class _Proc:
        pid = 4321

        def communicate(self, payload=None, timeout=None):
            if timeout is not None:
                raise shim.subprocess.TimeoutExpired("claude", timeout)
            seq.append("reap")
            return b"", b""

    monkeypatch.setattr(shim.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(shim, "_kill_tree",
                        lambda p: seq.append(("kill", p.pid)))
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 0.01)
    with pytest.raises(shim.subprocess.TimeoutExpired):
        cli.chat("sys", "hi")
    assert seq == [("kill", 4321), "reap"]


def test_kill_tree_on_windows_taskkills_the_whole_pid_tree(monkeypatch):
    # taskkill /F /T is the load-bearing kill on the production platform
    # (the dream primary on :8082 runs on a Windows host), and its failures
    # are swallowed (check=False) — so the argv is pinned here, where a
    # wrong flag is a test failure instead of a silent re-wedge.
    calls = []
    monkeypatch.setattr(shim.os, "name", "nt")
    monkeypatch.setattr(shim.subprocess, "run",
                        lambda argv, **k: calls.append(argv))

    class _Proc:
        pid = 4321

    shim._kill_tree(_Proc())
    assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]]


def test_run_detaches_the_child_into_its_own_session_on_posix(monkeypatch):
    # The POSIX kill path is os.killpg on the child's process group, which
    # only takes the descendants if the child LEADS its own session —
    # start_new_session is the enabling condition, and this repo develops
    # on Windows where that branch otherwise never executes.
    seen = {}

    class _Proc:
        pid = 1
        returncode = 0

        def communicate(self, payload=None, timeout=None):
            return b"", b""

    def _popen(*a, **k):
        seen.update(k)
        return _Proc()

    monkeypatch.setattr(shim.subprocess, "Popen", _popen)
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 30.0)
    cli._run(["claude", "-p"], b"hi")
    assert seen["start_new_session"] == (os.name != "nt")


# ── per-request model override (2026-08-02 dashboard switcher) ─────────────


def test_resolve_model_claude_name_wins():
    assert shim.resolve_model("claude-sonnet-5", "claude-opus-5") == "claude-sonnet-5"
    assert shim.resolve_model("claude-haiku-4-5", "claude-opus-5") == "claude-haiku-4-5"


def test_resolve_model_aliases_keep_launch_default():
    # The compose default PSEUDOLIFE_DREAM_MODEL=extractor and the bench
    # alias must keep hitting the launch-time model, not error.
    assert shim.resolve_model("extractor", "claude-opus-5") == "claude-opus-5"
    assert shim.resolve_model("bench", "claude-opus-5") == "claude-opus-5"
    assert shim.resolve_model(None, "claude-opus-5") == "claude-opus-5"
    assert shim.resolve_model("", "claude-opus-5") == "claude-opus-5"


def test_chat_passes_override_model_to_cli():
    captured = {}
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0)

    def fake_run(cmd, payload):
        captured["model"] = cmd[cmd.index("--model") + 1]
        return 0, b'{"result": "ok"}', b""

    cli._run = fake_run
    cli.chat("sys", "user", model="claude-sonnet-5")
    assert captured["model"] == "claude-sonnet-5"
    cli.chat("sys", "user")
    assert captured["model"] == "claude-opus-5"


# ── per-request reasoning effort (2026-09-01 dreamer effort knob) ──────────


def test_chat_threads_reasoning_effort_into_argv():
    # Request effort wins over the launch default; maps to the claude CLI's
    # --effort flag.
    captured = {}
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0,
                         reasoning_effort="medium")

    def fake_run(cmd, payload):
        captured["cmd"] = list(cmd)
        return 0, b'{"result": "ok"}', b""

    cli._run = fake_run
    cli.chat("sys", "user", effort="high")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "high"
    cli.chat("sys", "user")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "medium"


def test_no_effort_omits_the_flag():
    # Unset everywhere == pre-knob behavior: the CLI's own per-model default
    # serves, exactly as before.
    captured = {}
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0)

    def fake_run(cmd, payload):
        captured["cmd"] = list(cmd)
        return 0, b'{"result": "ok"}', b""

    cli._run = fake_run
    cli.chat("sys", "user")
    assert "--effort" not in captured["cmd"]


def test_chat_completions_thread_reasoning_effort(monkeypatch):
    # Mirror of the codex shim's handler pin: the daemon's extra_body lands
    # the field in the request JSON; the handler must hand it to chat() or
    # the knob silently does nothing on the DEPLOYED primary (:8082).
    import json
    import threading
    import urllib.request

    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0)
    seen = {}

    def _chat(system, user, model=None, effort=None):
        seen["effort"] = effort
        return "pong"
    monkeypatch.setattr(cli, "chat", _chat)
    srv = shim.ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(cli))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": "extractor", "reasoning_effort": "low",
                             "messages": [{"role": "user", "content": "hi"}]
                             }).encode(),
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req):
            pass
    finally:
        srv.shutdown()
    assert seen["effort"] == "low"


# ── --system-prompt-file misses are audible (2026-09-05 merge review) ─────
#
# The override applies only when the incoming system prompt starts with
# `dream._SYSTEM_PROMPT`. On a miss it silently vanishes and the shipped
# text goes to the model — which is exactly the shape of the bug that made
# `sonnet_extractor_v4.md` necessary in the first place, seen from the other
# side. A miss is NORMAL for the relations and lessons prompts (the override
# targets claims extraction only), so the log line is deduped per distinct
# prompt rather than fired per call.


def _echo_cli(system_prompt):
    """A ClaudeCli whose `_run` records the --system-prompt it was handed."""
    seen = {}
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0,
                         system_override="OVERRIDE BODY")

    def fake_run(cmd, payload):
        seen["system"] = (cmd[cmd.index("--system-prompt") + 1]
                          if "--system-prompt" in cmd else None)
        return 0, b'{"result": "ok"}', b""

    cli._run = fake_run
    return cli, seen, system_prompt


def test_an_override_miss_is_logged_once_per_distinct_prompt(caplog):
    cli, seen, prompt = _echo_cli("Extract the RELATIONS between entities.")
    with caplog.at_level(logging.WARNING, logger="claude_shim"):
        cli.chat(prompt, "notes")
        cli.chat(prompt, "more notes")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"expected one deduped warning, got {[r.message for r in warnings]}")
    assert "--system-prompt-file did not apply" in warnings[0].getMessage()
    # No behaviour change: the prompt still goes through untouched.
    assert seen["system"] == prompt


def test_a_second_distinct_missed_prompt_warns_again(caplog):
    cli, _, _ = _echo_cli("")
    with caplog.at_level(logging.WARNING, logger="claude_shim"):
        cli.chat("Extract the RELATIONS between entities.", "n")
        cli.chat("Distil the LESSONS from these outcomes.", "n")
    assert len([r for r in caplog.records
                if r.levelno >= logging.WARNING]) == 2


def test_an_applied_override_logs_nothing(caplog):
    cli, seen, _ = _echo_cli("")
    with caplog.at_level(logging.WARNING, logger="claude_shim"):
        cli.chat(_SYSTEM_PROMPT + "\n\nvocab hint", "notes")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert seen["system"].startswith("OVERRIDE BODY")


def test_no_override_configured_never_warns(caplog):
    """The warning is about a configured override failing to land, not
    about the ordinary no-override install."""
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0)
    cli._run = lambda cmd, payload: (0, b'{"result": "ok"}', b"")
    with caplog.at_level(logging.WARNING, logger="claude_shim"):
        cli.chat("anything at all", "notes")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_health_probe_does_not_trip_the_override_warning(caplog):
    """`_health_refresh` calls `chat("", ...)` on every /health cache miss,
    and `main()` warms that cache before `serve_forever`. Warning on it put
    the loudest, earliest and wrongest line at the top of every shim's log —
    for a call that passes no system prompt at all."""
    cli, seen, _ = _echo_cli("")
    with caplog.at_level(logging.WARNING, logger="claude_shim"):
        cli.chat("", "Reply with exactly: OK")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
        "the empty health-probe prompt must not report an override miss")
    assert seen["system"] is None      # nothing was passed to the CLI


# --- bind semantics -------------------------------------------------------

def test_server_refuses_to_bind_beside_a_running_shim():
    """A second shim launched over a live one must FAIL at bind, not start
    "successfully" and serve nothing. ``HTTPServer.allow_reuse_address``
    sets SO_REUSEADDR, and on Windows that lets a second socket bind a port
    already in LISTEN while the first keeps the traffic — which is why the
    2026-09-06 re-install over the live :8082 shim produced no traceback
    (probed 2026-09-07, Python 3.11.9: a plain ThreadingHTTPServer bound
    beside another; with allow_reuse_address=False the bind fails with
    WinError 10048). On POSIX SO_REUSEADDR never permits a bind over a
    LISTEN socket, so this is a real check on both platforms."""
    first = shim.ShimHTTPServer(("127.0.0.1", 0), shim.BaseHTTPRequestHandler)
    port = first.server_address[1]
    try:
        with pytest.raises(OSError):
            shim.ShimHTTPServer(("127.0.0.1", port), shim.BaseHTTPRequestHandler)
    finally:
        first.server_close()


def test_reuse_address_is_dropped_only_on_windows():
    # POSIX keeps SO_REUSEADDR: a restart must rebind through TIME_WAIT.
    # Windows does not need it for that, and with it a duplicate bind never
    # fails. main() must construct this class, not the stock server.
    assert shim.ShimHTTPServer.allow_reuse_address is (os.name != "nt")
    src = Path(shim.__file__).read_text(encoding="utf-8")
    assert "ShimHTTPServer((args.host, args.port)" in src


# ── a failed CLI call keeps the CLI's own words (2026-10-27) ──────────────
#
# Live: the CLI's OAuth session expired and `claude -p` printed "Failed to
# authenticate: OAuth session expired and could not be refreshed" and exited
# 1, but /health answered `{"status": "cli_error", "detail": "claude -p
# rc=1: "}` because only stderr was read and the message was on stdout.
# Nobody could tell from it that the fix was to sign in again.

_EXPIRED = b"Failed to authenticate: OAuth session expired and could not be refreshed\n"


def _failing_cli(stdout=b"", stderr=b"", rc=1):
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 30.0)
    cli._run = lambda cmd, payload: (rc, stdout, stderr)
    return cli


def test_a_failed_call_reports_the_message_the_cli_printed_on_stdout():
    with pytest.raises(RuntimeError, match="OAuth session expired") as e:
        _failing_cli(stdout=_EXPIRED).chat("sys", "hi")
    assert "rc=1" in str(e.value)


def test_a_failed_call_reads_a_json_error_result_on_stdout():
    out = (b'{"type":"result","subtype":"error","is_error":true,'
           b'"result":"Failed to authenticate: OAuth session expired"}')
    with pytest.raises(RuntimeError) as e:
        _failing_cli(stdout=out).chat("sys", "hi")
    assert "Failed to authenticate: OAuth session expired" in str(e.value)
    assert '"is_error"' not in str(e.value)


def test_a_failed_call_keeps_both_streams():
    with pytest.raises(RuntimeError) as e:
        _failing_cli(stdout=b"on stdout", stderr=b"on stderr").chat("sys", "hi")
    assert "on stdout" in str(e.value) and "on stderr" in str(e.value)


def test_a_failed_call_with_no_output_says_so():
    with pytest.raises(RuntimeError, match="no output on stdout or stderr"):
        _failing_cli().chat("sys", "hi")


def test_a_failed_calls_detail_is_bounded_per_stream():
    with pytest.raises(RuntimeError) as e:
        _failing_cli(stdout=b"x" * 5000, stderr=b"the real cause").chat("sys", "hi")
    # A flood on one stream must not push the other out of the detail.
    assert "the real cause" in str(e.value)
    assert len(str(e.value)) < 700


def _health_answer(cli):
    import json
    import urllib.error
    import urllib.request

    srv = shim.ThreadingHTTPServer(("127.0.0.1", 0), shim.make_handler(cli))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/health"
        try:
            with urllib.request.urlopen(url) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())
    finally:
        srv.shutdown()
        srv.server_close()


def test_health_names_the_sign_in_fix_when_the_cli_login_expired():
    code, body = _health_answer(_failing_cli(stdout=_EXPIRED))
    assert code == 503
    assert body["status"] == "login_required"
    assert "OAuth session expired" in body["detail"]
    assert "/login" in body["fix"] and "shim host" in body["fix"]


@pytest.mark.parametrize("message", [
    b"Invalid API key \xc2\xb7 Please run /login",
    b"Not logged in",
])
def test_health_maps_each_sign_in_message(message):
    code, body = _health_answer(_failing_cli(stdout=message))
    assert (code, body["status"]) == (503, "login_required")


@pytest.mark.parametrize("message", [
    # Near misses: an error that mentions neither a login nor credentials,
    # and one whose words only resemble them, stay a plain CLI error.
    b"API Error: 529 overloaded",
    b"error: unknown option '--catalog-in'",
    b"Credit balance is too low",
])
def test_health_keeps_other_cli_failures_as_cli_error(message):
    code, body = _health_answer(_failing_cli(stdout=message))
    assert (code, body["status"]) == (503, "cli_error")
    assert message.decode()[:20] in body["detail"]
    assert "fix" not in body


def test_a_sign_in_failure_after_long_warnings_is_still_named():
    # Codex review of bc1176d8: classifying the bounded text lost a sign-in
    # message that followed more than one stream's worth of warnings.
    noise = b"warning: something unrelated happened. " * 20
    code, body = _health_answer(_failing_cli(stdout=noise + _EXPIRED))
    assert (code, body["status"]) == (503, "login_required")
    # The bound keeps the stream's end, where the CLI prints its error.
    assert "could not be refreshed" in body["detail"]


@pytest.mark.parametrize("message", [
    b"authentication_failed",
    b"Your organization does not have access to Claude. Please login again.",
    b"OAuth token has expired. Please obtain a new token or refresh your existing token.",
    b"API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}",
])
def test_health_maps_more_sign_in_messages(message):
    code, body = _health_answer(_failing_cli(stdout=message))
    assert (code, body["status"]) == (503, "login_required")


@pytest.mark.parametrize("message", [
    # Reviewer near misses on bc1176d8: a number field equal to 401 in a
    # JSON result with no message, and a 403 that names OAuth but that no
    # sign-in fixes.
    b'{"type":"result","subtype":"error_during_execution","is_error":true,"duration_ms":401}',
    b'API Error: 403 {"type":"error","error":{"type":"permission_error",'
    b'"message":"OAuth authentication is currently not supported."}}',
])
def test_health_does_not_call_these_a_sign_in_failure(message):
    code, body = _health_answer(_failing_cli(stdout=message))
    assert (code, body["status"]) == (503, "cli_error")


def test_a_lone_stream_gets_both_streams_budget():
    with pytest.raises(RuntimeError) as e:
        _failing_cli(stderr=b"a" * 400 + b"END").chat("sys", "hi")
    assert "a" * 400 + "END" in str(e.value)


def test_a_refresh_publishes_verdict_and_body_together(monkeypatch):
    # Codex review of f8582a7c: the refresh wrote the body and the verdict
    # one after the other, so a /health landing between them answered 503
    # with {"status": "ok"}. Pause the refresh after its first write to the
    # health cache and read it from another thread.
    first_write, release = threading.Event(), threading.Event()

    class _Paused(shim.ClaudeCli):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if (name.startswith("_health") and name != "_health_refreshing"
                    and getattr(self, "_pause", False)):
                self._pause = False
                first_write.set()
                release.wait(10)

    cli = _Paused(Path("claude.exe"), "m", 30.0)
    cli._run = lambda cmd, payload: (1, _EXPIRED, b"")
    assert cli.health_answer()[1]["status"] == "login_required"   # warm, down
    cli._run = lambda cmd, payload: (0, b'{"result": "OK"}', b"")
    cli._pause = True
    t = threading.Thread(target=cli._health_refresh)
    t.start()
    try:
        assert first_write.wait(10)
        ok, body = cli.health_answer()
        assert (ok, body["status"] == "ok") in ((True, True), (False, False))
    finally:
        release.set()
        t.join(10)
    assert cli.health_answer() == (True, {"status": "ok"})


# ── who to sign in, and a fast recovery (2026-10-27 maintainer request) ──
#
# The notice must say which provider is signed out and as which OS user to
# sign it in, and it must clear soon after the user signs in: with the
# 5-minute health TTL the shim kept answering the stale 503 for up to five
# minutes after a live re-login on 2026-10-27.


def test_a_sign_in_failure_names_the_provider_and_the_shims_user(monkeypatch):
    monkeypatch.setattr(shim.getpass, "getuser", lambda: "svc-user")
    code, body = _health_answer(_failing_cli(stdout=_EXPIRED))
    assert (code, body["status"]) == (503, "login_required")
    assert body["provider"] == "claude" and body["user"] == "svc-user"
    assert "as svc-user" in body["fix"]
    assert "`claude`" in body["fix"] and "/login" in body["fix"]


def test_an_unknown_user_is_left_out(monkeypatch):
    def _no_user():
        raise OSError("no login name")
    monkeypatch.setattr(shim.getpass, "getuser", _no_user)
    code, body = _health_answer(_failing_cli(stdout=_EXPIRED))
    assert body["provider"] == "claude" and "user" not in body


def test_other_failures_carry_no_sign_in_fields():
    code, body = _health_answer(_failing_cli(stdout=b"API Error: 529 overloaded"))
    assert body["status"] == "cli_error"
    assert "provider" not in body and "user" not in body


@pytest.mark.parametrize("status, age, refreshes", [
    ("login_required", 31.0, True),   # a sign-in failure is re-checked after 30 s
    ("login_required", 29.0, False),
    # Opus review of 1f782865: any other failure (a rate limit, a slow CLI)
    # keeps the 5-minute TTL, or a polled /health would spend a real call
    # every 30 s.
    ("cli_error", 31.0, False),
    ("cli_error", 301.0, True),
    ("ok", 31.0, False),
    ("ok", 301.0, True),
])
def test_a_sign_in_failure_is_rechecked_sooner(monkeypatch, status, age, refreshes):
    cli = shim.ClaudeCli(Path("claude.exe"), "m", 30.0)
    cli._health_snapshot = (status == "ok", {"status": status})
    cli._health_at = time.monotonic() - age
    started = []
    monkeypatch.setattr(shim.threading, "Thread",
                        lambda target, daemon: type("T", (), {
                            "start": lambda self: started.append(target)})())
    cli.health_answer()
    assert bool(started) is refreshes
