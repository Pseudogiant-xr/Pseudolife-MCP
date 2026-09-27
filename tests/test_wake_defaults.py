"""Wake is on by default and policy-gated (maintainer decision 2026-09-28,
superseding the 2026-09-25 opt-in): the Claude Code Stop hook rings unless
``PSEUDOLIFE_AGENT_WAKE_HOOK=0``, and the Codex doorbell rings when a
``codex`` CLI is found unless ``PSEUDOLIFE_CODEX_DOORBELL=0``;
``PSEUDOLIFE_AGENT_COORDINATION=0`` stays the master off switch. The caps the
daemon applies to every ring are operator-configurable under
``coordination.wake`` in config.yaml, served on ``/health``, and
``pseudolife-mcp doctor`` reports each registered client's wake path and
the caps in force.

The Stop hook's own default-on tests live in ``test_stop_wake_hook.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from unittest.mock import AsyncMock

import pytest

from pseudolife_memory import doctor_cli, shim
from pseudolife_memory.codex_doorbell import CodexDoorbell, resolve_codex_command
from pseudolife_memory.daemon import _build_health_payload
from pseudolife_memory.utils.config import (
    AppConfig, CoordinationConfig, WakeConfig, load_config)

PATHEXT = ".COM;.EXE;.BAT;.CMD"
CAPS = {"per_recipient_per_hour": 20, "urgent_per_sender_per_hour": 6,
        "nightly_total": 200, "fan_out_stagger_seconds": 30}
PLUGIN = "pseudolife-memory@pseudolife-mcp"


def _fake_cli(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("codex.exe" if os.name == "nt" else "codex")
    path.write_bytes(b"")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _same(command, path):
    return command is not None and [os.path.normcase(part) for part in command] == [
        os.path.normcase(str(path))]


# --- caps: config.yaml `coordination.wake` ---------------------------------

def test_wake_caps_default_to_the_decided_figures():
    """Per recipient 20 an hour counting every wake, ``urgent`` 6 an hour per
    sender, 200 a night, 30 s between rings of one fan-out (design decision,
    2026-09-28)."""
    wake = CoordinationConfig().wake
    assert isinstance(wake, WakeConfig)
    assert {k: getattr(wake, k) for k in CAPS} == CAPS
    assert AppConfig().coordination.wake == WakeConfig()


def test_wake_caps_are_read_from_config_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("coordination:\n  wake:\n    per_recipient_per_hour: 5\n"
                 "    nightly_total: 40\n    fan_out_stagger_seconds: 5\n")
    wake = load_config(p).coordination.wake
    assert (wake.per_recipient_per_hour, wake.nightly_total,
            wake.fan_out_stagger_seconds) == (5, 40, 5)
    # Omitted keys keep the decided defaults, as the rest of the block does.
    assert wake.urgent_per_sender_per_hour == 6


def test_a_zero_nightly_total_rings_nobody_and_the_rate_caps_need_one():
    """``nightly_total: 0`` stops every ring from the daemon side; a
    per-recipient or urgent cap of 0 is refused, so a typo cannot pass for
    that switch. Whole numbers only: the stagger is whole seconds."""
    wake = WakeConfig(nightly_total=0, fan_out_stagger_seconds=0)
    assert (wake.nightly_total, wake.fan_out_stagger_seconds) == (0, 0)
    for name in ("per_recipient_per_hour", "urgent_per_sender_per_hour"):
        with pytest.raises(ValueError, match=f"coordination.wake.{name}"):
            WakeConfig(**{name: 0})
    for name, value in (("fan_out_stagger_seconds", 2.5), ("nightly_total", -1),
                        ("per_recipient_per_hour", True)):
        with pytest.raises(ValueError, match=f"coordination.wake.{name}"):
            WakeConfig(**{name: value})


# --- caps on /health ---------------------------------------------------------

class _Svc:
    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None

    def __init__(self, config=None):
        if config is not None:
            self.config = config


def test_health_serves_the_wake_caps_in_force():
    config = AppConfig()
    config.coordination.wake.per_recipient_per_hour = 7
    payload = _build_health_payload(_Svc(config), token_present=True)
    assert payload["coordination"]["enabled"] is True
    # Every cap, by its config name; the dataclass may carry more knobs.
    assert payload["coordination"]["wake"].items() >= {**CAPS, "per_recipient_per_hour": 7}.items()


def test_health_without_a_config_carries_no_coordination_block():
    """Stand-ins and partial services never break the liveness probe."""
    assert "coordination" not in _build_health_payload(_Svc(), token_present=False)


# --- Codex CLI lookup: the desktop app's bin directory ------------------------

@pytest.mark.skipif(os.name != "nt", reason="the desktop app's bin directory is a Windows path")
def test_codex_lookup_falls_back_to_the_desktop_apps_bin_directory(tmp_path):
    """A desktop-only Codex install keeps its CLI under
    ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe`` and puts nothing on
    PATH; the newest build wins, as in ops/setup-codex-hooks.py."""
    root = tmp_path / "OpenAI" / "Codex" / "bin"
    older = _fake_cli(root / "0ddb895c950eaeba")
    newer = _fake_cli(root / "faa963e871dd422c")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    env = {"PATH": "", "PATHEXT": PATHEXT, "LOCALAPPDATA": str(tmp_path)}
    assert _same(resolve_codex_command(env), newer)
    # PATH still wins over the desktop directory, and the configured path
    # over both; a relative LOCALAPPDATA is refused like a relative PATH entry.
    on_path = _fake_cli(tmp_path / "on-path")
    assert _same(resolve_codex_command({**env, "PATH": str(tmp_path / "on-path")}), on_path)
    assert _same(resolve_codex_command({**env, "PSEUDOLIFE_CODEX_BIN": str(older)}), older)
    assert resolve_codex_command({**env, "LOCALAPPDATA": "OpenAI"}) is None
    assert resolve_codex_command({**env, "LOCALAPPDATA": str(tmp_path / "missing")}) is None


# --- the doorbell default in the Codex shim ------------------------------------

@pytest.fixture
def codex_shim(monkeypatch, tmp_path, capsys):
    from pseudolife_memory import codex_coordination

    seen = []

    class Registry:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)

        async def aclose(self):
            pass

    async def proxy(*args, **kwargs):
        pass

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    for key in ("PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_WAKE",
                "PSEUDOLIFE_CODEX_DOORBELL", "PSEUDOLIFE_CODEX_BIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-desktop-app"))

    def run():
        asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))
        return capsys.readouterr().err

    return seen, run


def test_the_doorbell_is_on_by_default_when_a_codex_cli_is_found(codex_shim, monkeypatch, tmp_path):
    seen, run = codex_shim
    binary = _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert "doorbell" not in run()                       # on, and quiet about it
    assert isinstance(seen[-1]["doorbell"], CodexDoorbell)
    assert _same(seen[-1]["doorbell"]._command, binary)
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("PSEUDOLIFE_CODEX_DOORBELL", value)
        run()
        assert isinstance(seen[-1]["doorbell"], CodexDoorbell)


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "maybe"])
def test_the_doorbell_opts_out_with_any_value_that_is_not_a_yes(codex_shim, monkeypatch, tmp_path, value):
    seen, run = codex_shim
    _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("PSEUDOLIFE_CODEX_DOORBELL", value)
    assert "doorbell" not in run()
    assert "doorbell" not in seen[-1]


def test_without_a_codex_cli_the_default_stays_quiet_but_an_explicit_yes_says_why(codex_shim, monkeypatch):
    """A stripped MCP environment finds no CLI: the default falls back to
    pull delivery without a stderr line on every launch (doctor names it);
    an operator who set =1 is told."""
    seen, run = codex_shim
    assert "doorbell" not in run()
    assert "doorbell" not in seen[-1]
    monkeypatch.setenv("PSEUDOLIFE_CODEX_DOORBELL", "1")
    assert "no codex CLI found on PATH" in run()
    assert "doorbell" not in seen[-1]


def test_coordination_off_is_the_master_switch_for_the_doorbell(codex_shim, monkeypatch, tmp_path):
    seen, run = codex_shim
    _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "0")
    before = len(seen)
    assert "doorbell" not in run()                       # default: quiet
    monkeypatch.setenv("PSEUDOLIFE_CODEX_DOORBELL", "1")
    assert "PSEUDOLIFE_CODEX_DOORBELL needs PSEUDOLIFE_AGENT_COORDINATION=1" in run()
    assert len(seen) == before                           # no registry at all


# --- doctor: wake path per registered client, caps in force ----------------------

@pytest.fixture
def doctor_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for key in ("PSEUDOLIFE_AGENT_COORDINATION", "PSEUDOLIFE_AGENT_WAKE_HOOK",
                "PSEUDOLIFE_CODEX_DOORBELL", "PSEUDOLIFE_CODEX_BIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-desktop-app"))
    return home


def _register_claude(home: Path, env: dict | None = None, *, plugin: bool | None = True,
                     config_dir: Path | None = None) -> None:
    """The MCP registration, plus the plugin that carries the Stop hook:
    ``plugin`` True installs and enables it, False installs it disabled, None
    leaves it out. With ``config_dir`` (CLAUDE_CONFIG_DIR) everything,
    ``.claude.json`` included, lives in that directory."""
    registration = (config_dir or home) / ".claude.json"
    config_dir = config_dir or home / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    registration.write_text(json.dumps(
        {"mcpServers": {"pseudolife-memory": {"command": "pseudolife-mcp"}}}))
    settings = {}
    if env is not None:
        settings["env"] = env
    if plugin is not None:
        (config_dir / "plugins").mkdir(exist_ok=True)
        (config_dir / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {PLUGIN: [{"scope": "user"}]}}))
        settings["enabledPlugins"] = {PLUGIN: plugin}
    if settings:
        (config_dir / "settings.json").write_text(json.dumps(settings))


def _register_codex(home: Path, env: dict | None = None, forwarded: list | None = None,
                    *, codex_home: Path | None = None) -> None:
    codex_home = codex_home or home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    lines = ['[mcp_servers.pseudolife-memory]', 'command = "pseudolife-mcp"']
    if forwarded:
        lines.append("env_vars = " + json.dumps(forwarded))
    if env:
        lines.append("[mcp_servers.pseudolife-memory.env]")
        lines += [f'{key} = "{value}"' for key, value in env.items()]
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n")


def _doctor(monkeypatch, capsys, health):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: health)
    monkeypatch.setattr(doctor_cli, "_handshake", AsyncMock(return_value={
        "instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}))
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    return json.loads(capsys.readouterr().out)


HEALTH = {"status": "ok", "version": "0.0.0-fixture",
          "coordination": {"enabled": True, "wake": CAPS}}


def test_doctor_reports_wake_on_for_both_registered_clients(monkeypatch, capsys, doctor_home, tmp_path):
    _register_claude(doctor_home)
    _register_codex(doctor_home)
    _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"] == {
        "claude_code": {"registered": True, "stop_hook": "on"},
        "codex": {"registered": True, "doorbell": "on"},
        "caps": CAPS}


def test_doctor_names_the_opt_out_each_client_carries(monkeypatch, capsys, doctor_home, tmp_path):
    """Claude Code's hooks read the settings.json env block; the Codex shim
    reads its MCP server's env table. Each is reported from where it is set."""
    _register_claude(doctor_home, {"PSEUDOLIFE_AGENT_WAKE_HOOK": "0"})
    _register_codex(doctor_home, {"PSEUDOLIFE_CODEX_DOORBELL": "0"})
    _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"]["stop_hook"] == "off (PSEUDOLIFE_AGENT_WAKE_HOOK=0)"
    assert report["wake"]["codex"]["doorbell"] == "off (PSEUDOLIFE_CODEX_DOORBELL=0)"


def test_doctor_reports_the_master_switch_and_a_missing_codex_cli(monkeypatch, capsys, doctor_home):
    _register_claude(doctor_home, {"PSEUDOLIFE_AGENT_COORDINATION": "0"})
    _register_codex(doctor_home)
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"]["stop_hook"] == "off (PSEUDOLIFE_AGENT_COORDINATION=0)"
    assert report["wake"]["codex"]["doorbell"] == "off (no codex CLI)"


def test_doctor_reads_a_codex_opt_out_only_where_codex_forwards_it(monkeypatch, capsys, doctor_home, tmp_path):
    """Codex hands an MCP server its ``env`` table plus the ``env_vars`` it
    forwards from the launching environment, nothing else: a shell variable
    counts only when it is listed there."""
    _fake_cli(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "0")
    _register_codex(doctor_home)
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["codex"]["doorbell"] == "on"
    assert report["wake"]["claude_code"] == {"registered": False}
    _register_codex(doctor_home, forwarded=["PSEUDOLIFE_AGENT_COORDINATION"])
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["codex"]["doorbell"] == "off (PSEUDOLIFE_AGENT_COORDINATION=0)"


def test_doctor_says_when_the_daemon_predates_the_caps_and_when_it_is_down(monkeypatch, capsys, doctor_home):
    _register_claude(doctor_home)
    report = _doctor(monkeypatch, capsys, {"status": "ok", "version": "0.0.0-fixture"})
    assert report["wake"]["caps"] == "unknown (the daemon does not report them; update it)"
    report = _doctor(monkeypatch, capsys, None)
    assert report["wake"]["claude_code"]["stop_hook"] == "on"
    assert report["wake"]["caps"] == "unknown (daemon unreachable)"


def test_doctor_reports_a_board_the_daemon_turned_off(monkeypatch, capsys, doctor_home):
    _register_claude(doctor_home)
    _register_codex(doctor_home)
    health = {**HEALTH, "coordination": {"enabled": False, "wake": CAPS}}
    report = _doctor(monkeypatch, capsys, health)
    assert report["wake"]["claude_code"]["stop_hook"] == "off (coordination disabled on the daemon)"
    assert report["wake"]["codex"]["doorbell"] == "off (coordination disabled on the daemon)"


def test_doctor_never_lets_a_broken_client_config_hide_the_report(monkeypatch, capsys, doctor_home):
    (doctor_home / ".claude.json").write_text("{not json")
    (doctor_home / ".codex" / "config.toml").write_text("[mcp_servers.pseudolife-memory\n")
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"] == {"registered": "unknown (unreadable ~/.claude.json)"}
    assert report["wake"]["codex"] == {"registered": "unknown (unreadable config.toml)"}
    assert report["wake"]["caps"] == CAPS


def test_doctor_reports_the_stop_hook_off_without_the_plugin(monkeypatch, capsys, doctor_home):
    """The Stop hook ships only in the plugin: an MCP registration from the
    installer or ops/install-hook.* alone has no wake path, and a plugin
    disabled in enabledPlugins runs no hooks."""
    _register_claude(doctor_home, plugin=None)
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"] == {"registered": True,
                                             "stop_hook": "off (plugin not installed)"}
    _register_claude(doctor_home, plugin=False)
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"]["stop_hook"] == "off (plugin disabled)"


def test_doctor_follows_claude_config_dir_and_codex_home(monkeypatch, capsys, doctor_home, tmp_path):
    claude_dir, codex_home = tmp_path / "claude-config", tmp_path / "codex-home"
    _register_claude(doctor_home, {"PSEUDOLIFE_AGENT_WAKE_HOOK": "0"}, config_dir=claude_dir)
    _register_codex(doctor_home, {"PSEUDOLIFE_CODEX_DOORBELL": "0"}, codex_home=codex_home)
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"] == {"registered": False}
    assert report["wake"]["codex"] == {"registered": False}
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    report = _doctor(monkeypatch, capsys, HEALTH)
    assert report["wake"]["claude_code"]["stop_hook"] == "off (PSEUDOLIFE_AGENT_WAKE_HOOK=0)"
    assert report["wake"]["codex"]["doorbell"] == "off (PSEUDOLIFE_CODEX_DOORBELL=0)"
