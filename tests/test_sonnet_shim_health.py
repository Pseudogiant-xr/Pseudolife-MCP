"""sonnet_shim /health must reflect real CLI usability (a logged-out CLI
answers 503 so the daemon's fallback probe sees primary-down)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sonnet_shim", REPO / "evals" / "sonnet_shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules["sonnet_shim"] = shim
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
    cli._health_at = time.monotonic() - 301  # expire the cache
    assert cli.health()[0] is False
    assert calls["n"] == 1
