"""claude_shim /health must reflect real CLI usability (a logged-out CLI
answers 503 so the daemon's fallback probe sees primary-down)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

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


def test_chat_passes_override_model_to_cli(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["model"] = cmd[cmd.index("--model") + 1]

        class R:
            returncode = 0
            stdout = b'{"result": "ok"}'
            stderr = b""
        return R()

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    cli = shim.ClaudeCli(Path("claude.exe"), "claude-opus-5", 30.0)
    cli.chat("sys", "user", model="claude-sonnet-5")
    assert captured["model"] == "claude-sonnet-5"
    cli.chat("sys", "user")
    assert captured["model"] == "claude-opus-5"
