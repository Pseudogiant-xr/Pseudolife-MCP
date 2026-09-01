"""claude_shim /health must reflect real CLI usability (a logged-out CLI
answers 503 so the daemon's fallback probe sees primary-down)."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "claude_shim", REPO / "evals" / "claude_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["claude_shim"] = shim
spec.loader.exec_module(shim)


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


def test_health_ok_when_cli_answers(monkeypatch):
    ok, detail = _cli(monkeypatch, True).health()
    assert ok is True


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

    def _boom(s, u):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(cli, "chat", _boom)
    cli._health_at = time.monotonic() - 301  # expire the cache

    t0 = time.monotonic()
    ok, _ = cli.health()
    assert ok is True                        # stale verdict, served instantly
    assert time.monotonic() - t0 < 0.5

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
