"""ops/register_claude_desktop.py — the one JSON merge both installers call.

Claude Desktop has no ``mcp add`` CLI: its servers live in
``claude_desktop_config.json``, whose location differs per OS and, on
Windows, per packaging (the MSIX build keeps its Roaming AppData under the
package cache, invisible to an unpackaged shell's ``%APPDATA%``). Desktop
also launches MCP servers with a sanitized environment, so a bearer token
in the OS environment never reaches the shim — the entry has to carry
``PSEUDOLIFE_MCP_TOKEN_FILE`` itself (the 2026-09-19 incident: every
Desktop session 401'd for four days behind an "unhandled errors in a
TaskGroup" wrapper). And because the shim reads that file first and
unconditionally, the script must never point the entry at a file it did
not write or validate — that would silently disable a literal token that
was working (review finding, 2026-09-20).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider, _write_token_file
from pseudolife_memory.cli import _USAGE as CHECKOUT_USAGE

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "register_claude_desktop", ROOT / "ops" / "register_claude_desktop.py")
reg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reg)

TOKEN = "t0k3n-" + "x" * 40
OTHER_TOKEN = "0th3r-" + "y" * 40


@pytest.mark.parametrize("source", ["token", "tokens"])
def test_checkout_import_wins_over_old_package_with_root_already_on_path(tmp_path, source):
    site = tmp_path / "old-site"
    package = site / "pseudolife_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(site), str(ROOT)])
    env["PL_TEST_SOURCE"] = TOKEN if source == "token" else TOKEN + ":claude-desktop"
    cfg = tmp_path / "config.json"
    token_file = tmp_path / "private.token"
    proc = subprocess.run(
        [sys.executable, "-S", str(ROOT / "ops/register_claude_desktop.py"),
         "--command", str(tmp_path / "pseudolife-mcp"), "--config", str(cfg),
         "--default-token-file", str(token_file),
         f"--{source}-from-env", "PL_TEST_SOURCE"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    assert TOKEN not in proc.stdout + proc.stderr


# -- config path resolution ---------------------------------------------------

def test_windows_prefers_the_msix_package_cache_when_it_exists(tmp_path):
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    cache = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    cache.mkdir(parents=True)
    (appdata / "Claude").mkdir(parents=True)  # a stale unpackaged copy too
    env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}
    got = reg.resolve_config_path("win32", env, tmp_path)
    assert got == cache / "claude_desktop_config.json"


def test_windows_falls_back_to_appdata_without_a_package_cache(tmp_path):
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    env = {"APPDATA": str(appdata), "LOCALAPPDATA": str(local)}
    got = reg.resolve_config_path("win32", env, tmp_path)
    assert got == appdata / "Claude" / "claude_desktop_config.json"


def test_windows_matches_any_claude_package_family(tmp_path):
    """The family-name hash is derived from the publisher; don't pin it."""
    local = tmp_path / "Local"
    cache = local / "Packages" / "Claude_abc123xyz" / "LocalCache" / "Roaming" / "Claude"
    cache.mkdir(parents=True)
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(local)}
    got = reg.resolve_config_path("win32", env, tmp_path)
    assert got == cache / "claude_desktop_config.json"


def test_windows_without_localappdata_still_resolves(tmp_path):
    env = {"APPDATA": str(tmp_path / "Roaming")}
    got = reg.resolve_config_path("win32", env, tmp_path)
    assert got == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


def test_macos_and_linux_config_homes(tmp_path):
    assert reg.resolve_config_path("darwin", {}, tmp_path) == (
        tmp_path / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json")
    assert reg.resolve_config_path("linux", {}, tmp_path) == (
        tmp_path / ".config" / "Claude" / "claude_desktop_config.json")


def test_linux_honours_xdg_config_home(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert reg.resolve_config_path("linux", env, tmp_path) == (
        tmp_path / "xdg" / "Claude" / "claude_desktop_config.json")


def test_main_names_the_other_windows_config_when_both_exist(tmp_path, monkeypatch, capsys):
    """Package cache wins, but a populated unpackaged copy is named so a
    wrong-file write is visible rather than silent (review finding)."""
    appdata = tmp_path / "Roaming"
    local = tmp_path / "Local"
    cache = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    cache.mkdir(parents=True)
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(reg, "_platform", lambda: "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(reg.Path, "home", classmethod(lambda cls: tmp_path))
    rc = reg.main(["--command", "/abs/pseudolife-mcp"])
    assert rc == 0
    out = capsys.readouterr().out
    assert (cache / "claude_desktop_config.json").exists()
    assert "another Desktop config also exists" in out
    assert str(appdata / "Claude" / "claude_desktop_config.json") in out


# -- the entry ----------------------------------------------------------------

def test_entry_carries_absolute_command_writer_no_spawn_and_daemon_url():
    entry = reg.build_entry(
        command="/opt/venv/bin/pseudolife-mcp", writer_id="claude-desktop",
        daemon_url="http://127.0.0.1:8765", token_file=None)
    assert entry["command"] == "/opt/venv/bin/pseudolife-mcp"
    assert entry["env"] == {
        "PSEUDOLIFE_WRITER_ID": "claude-desktop",
        "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
    }
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in entry["env"]


def test_entry_points_at_a_token_file_never_a_token_value():
    entry = reg.build_entry(
        command="C:\\x\\pseudolife-mcp.exe", writer_id="claude-desktop",
        daemon_url="http://127.0.0.1:8765",
        token_file="D:\\pseudolife\\private\\claude-desktop.token")
    assert entry["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"].endswith("claude-desktop.token")
    assert "PSEUDOLIFE_MCP_TOKEN" not in entry["env"]


def test_command_and_token_file_must_be_absolute():
    """Desktop's sanitized PATH omits pipx/venv bin dirs and its launch CWD
    is arbitrary, so bare names and relative paths would fail at launch."""
    with pytest.raises(ValueError, match="absolute"):
        reg.build_entry(command="pseudolife-mcp", writer_id="claude-desktop",
                        daemon_url="http://127.0.0.1:8765", token_file=None)
    with pytest.raises(ValueError, match="absolute"):
        reg.build_entry(command="/abs/pseudolife-mcp", writer_id="claude-desktop",
                        daemon_url="http://127.0.0.1:8765", token_file="rel/claude.token")


# -- the merge ----------------------------------------------------------------

def _entry(token_file=None):
    return reg.build_entry(
        command="/abs/pseudolife-mcp", writer_id="claude-desktop",
        daemon_url="http://127.0.0.1:8765", token_file=token_file)


def test_merge_preserves_every_other_key_and_server():
    existing = {
        "mcpServers": {
            "pdf-tools": {"command": "/abs/pdf", "env": {"A": "1"}},
        },
        "preferences": {"theme": "dark", "nested": {"list": [], "n": 3}},
    }
    merged, changed = reg.merge_config(existing, _entry())
    assert changed is True
    assert merged["preferences"] == existing["preferences"]
    assert merged["mcpServers"]["pdf-tools"] == existing["mcpServers"]["pdf-tools"]
    assert merged["mcpServers"]["pseudolife-desktop"]["command"] == "/abs/pseudolife-mcp"


def test_merge_keeps_user_added_env_keys_and_the_literal_token_by_default():
    """Unmanaged keys survive; the literal token is only dropped when the
    caller says its value now lives in the token file."""
    existing = {"mcpServers": {"pseudolife-desktop": {
        "command": "old", "env": {"PSEUDOLIFE_MCP_TOKEN": "keep-me",
                                  "HTTPS_PROXY": "http://proxy:3128",
                                  "PSEUDOLIFE_WRITER_ID": "stale"}}}}
    merged, changed = reg.merge_config(existing, _entry())
    env = merged["mcpServers"]["pseudolife-desktop"]["env"]
    assert changed is True
    assert env["PSEUDOLIFE_MCP_TOKEN"] == "keep-me"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["PSEUDOLIFE_WRITER_ID"] == "claude-desktop"
    dropped, _ = reg.merge_config(existing, _entry("/abs/t.token"), drop_literal_token=True)
    assert "PSEUDOLIFE_MCP_TOKEN" not in dropped["mcpServers"]["pseudolife-desktop"]["env"]
    assert dropped["mcpServers"]["pseudolife-desktop"]["env"]["HTTPS_PROXY"] == "http://proxy:3128"


def test_merge_drops_a_stale_args_list():
    existing = {"mcpServers": {"pseudolife-desktop": {
        "command": "old", "args": ["--legacy"], "env": {}}}}
    merged, _ = reg.merge_config(existing, _entry())
    assert "args" not in merged["mcpServers"]["pseudolife-desktop"]


def test_merge_is_idempotent():
    merged, changed = reg.merge_config({}, _entry())
    assert changed is True
    again, changed_again = reg.merge_config(merged, _entry())
    assert changed_again is False
    assert again == merged


def test_merge_rejects_non_object_shapes():
    with pytest.raises(reg.ConfigError, match="mcpServers"):
        reg.merge_config({"mcpServers": []}, _entry())
    with pytest.raises(reg.ConfigError, match="mcpServers.pseudolife-desktop"):
        reg.merge_config({"mcpServers": {"pseudolife-desktop": "not-an-object"}}, _entry())


def test_merge_treats_a_null_mcp_servers_as_empty():
    merged, changed = reg.merge_config({"mcpServers": None}, _entry())
    assert changed is True
    assert merged["mcpServers"]["pseudolife-desktop"]["command"] == "/abs/pseudolife-mcp"


# -- main: files, backups, output ---------------------------------------------

def _run(tmp_path, *args, config=None):
    cfg = config or (tmp_path / "claude_desktop_config.json")
    argv = ["--config", str(cfg), "--command", "/abs/pseudolife-mcp", *args]
    return cfg, reg.main(argv)


def test_main_creates_a_missing_config(tmp_path, capsys):
    cfg, rc = _run(tmp_path)
    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["pseudolife-desktop"]["command"] == "/abs/pseudolife-mcp"
    out = capsys.readouterr().out
    assert str(cfg) in out
    assert "quit Claude Desktop" in out  # the relaunch reminder


def test_main_backs_up_before_changing_and_writes_bom_free_utf8(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('\ufeff{"mcpServers": {}, "keep": "caf\u00e9"}', encoding="utf-8")
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 0
    backups = list(tmp_path.glob("claude_desktop_config.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8-sig"))["mcpServers"] == {}
    raw = cfg.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert json.loads(raw.decode("utf-8"))["keep"] == "caf\u00e9"


def test_main_second_run_changes_nothing_and_takes_no_backup(tmp_path, capsys):
    cfg, _ = _run(tmp_path)
    before = cfg.read_bytes()
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 0
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))
    assert "unchanged" in capsys.readouterr().out


def test_main_refuses_malformed_json_without_touching_it(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{not json", encoding="utf-8")
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 2
    assert cfg.read_text(encoding="utf-8") == "{not json"
    assert not list(tmp_path.glob("*.bak-*"))
    assert "not valid JSON" in capsys.readouterr().err


def test_main_refuses_a_non_utf8_config_with_the_designed_message(tmp_path, capsys):
    """Windows PowerShell 5.1 redirection writes UTF-16; that must be a
    one-line refusal, not a traceback."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_bytes('{"mcpServers": {}}'.encode("utf-16"))
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 2
    assert "could not be read as UTF-8" in capsys.readouterr().err


def test_main_dry_run_prints_the_entry_and_writes_nothing(tmp_path, capsys):
    cfg, rc = _run(tmp_path, "--dry-run",
                   "--default-token-file", str(tmp_path / "claude-desktop.token"),
                   "--token-from-env", "PL_TEST_DESKTOP_TOKEN")
    assert rc == 3  # no token in the env, no file: registration would lack a credential
    assert not cfg.exists()
    out = capsys.readouterr().out
    assert "dry run" in out


def test_main_notes_a_dropped_args_list(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "old", "args": ["--x"]}}}), encoding="utf-8")
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 0
    assert "args" in capsys.readouterr().out


def test_main_resolves_the_config_path_when_not_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reg, "_platform", lambda: "linux")
    monkeypatch.setattr(reg, "resolve_config_path",
                        lambda system, env, home: tmp_path / "resolved.json")
    rc = reg.main(["--command", "/abs/pseudolife-mcp"])
    assert rc == 0
    assert (tmp_path / "resolved.json").exists()
    assert "resolved.json" in capsys.readouterr().out


# -- the entry's name ---------------------------------------------------------
#
# Claude Code registers its per-session stdio shim as pseudolife-memory. While
# Desktop's app-level entry carried the same name, a Desktop Code-tab
# session's mcp__pseudolife-memory__* calls were served by the app-level entry
# and the session's own shim got none (verified live 2026-09-21); renaming the
# Desktop entry gave each session its own server back. The registrar writes
# pseudolife-desktop and moves an entry it wrote under the old name, which it
# recognises by the writer ID it always sets.

LEGACY = "pseudolife-memory"


def _servers(cfg):
    return json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]


def _owned_legacy(**env):
    return {"command": "/old/pseudolife-mcp",
            "env": {"PSEUDOLIFE_WRITER_ID": "claude-desktop", **env}}


def test_the_desktop_entry_is_named_apart_from_the_claude_code_server():
    import re

    assert reg.SERVER == "pseudolife-desktop"
    claude_code_names = set()
    for rel in ("ops/install.sh", "ops/install.ps1"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        claude_code_names |= set(re.findall(r"claude mcp add\b[^\n]*?--scope user (\S+)", text))
    assert claude_code_names == {LEGACY}
    assert reg.SERVER not in claude_code_names


def test_installers_name_the_desktop_entry_in_their_success_line():
    for rel in ("ops/install.sh", "ops/install.ps1"):
        lines = [line for line in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                 if "Wired into Claude Desktop" in line]
        assert lines, rel
        assert all(reg.SERVER in line for line in lines), rel


def test_a_fresh_config_gets_only_the_desktop_name(tmp_path):
    cfg, rc = _run(tmp_path)
    assert rc == 0
    assert list(_servers(cfg)) == ["pseudolife-desktop"]


def test_an_owned_legacy_entry_is_moved_with_its_hand_added_settings(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    legacy = _owned_legacy(PSEUDOLIFE_MCP_NO_SPAWN="1",
                           PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:8765",
                           HTTPS_PROXY="http://proxy:3128")
    legacy["args"] = ["--legacy"]
    legacy["x-note"] = "hand-added"
    original = {"mcpServers": {"pdf-tools": {"command": "/abs/pdf"}, LEGACY: legacy},
                "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original), encoding="utf-8")

    _, rc = _run(tmp_path, config=cfg)

    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert LEGACY not in data["mcpServers"]
    assert data["mcpServers"]["pdf-tools"] == original["mcpServers"]["pdf-tools"]
    assert data["preferences"] == original["preferences"]
    entry = data["mcpServers"]["pseudolife-desktop"]
    assert entry == {
        "command": "/abs/pseudolife-mcp",
        "x-note": "hand-added",
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-desktop",
                "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
                "HTTPS_PROXY": "http://proxy:3128"},
    }
    (backup,) = tmp_path.glob("claude_desktop_config.json.bak-*")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    out = capsys.readouterr().out
    assert f"migrated mcpServers.{LEGACY}" in out and "pseudolife-desktop" in out


def test_an_otherwise_current_legacy_entry_is_still_renamed(tmp_path, capsys):
    """The common upgrade: the installer re-run with the same shim, so the
    old entry differs from what this run writes in its name alone."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: _entry()}}), encoding="utf-8")

    _, rc = _run(tmp_path, config=cfg)

    assert rc == 0
    assert _servers(cfg) == {"pseudolife-desktop": _entry()}
    assert len(list(tmp_path.glob("claude_desktop_config.json.bak-*"))) == 1
    assert "entry: written" in capsys.readouterr().out


def test_an_owned_legacy_literal_token_is_moved_into_the_file(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(PSEUDOLIFE_MCP_TOKEN=TOKEN)}}), encoding="utf-8")
    token_file = tmp_path / "claude-desktop.token"

    _, rc = _run(tmp_path, "--default-token-file", str(token_file), config=cfg)

    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    servers = _servers(cfg)
    assert LEGACY not in servers
    env = servers["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_an_owned_legacy_token_file_path_survives_the_rename(tmp_path):
    custom = tmp_path / "custom" / "desktop.token"
    default = tmp_path / "default.token"
    _write_token_file(custom, TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(PSEUDOLIFE_MCP_TOKEN_FILE=str(custom))}}), encoding="utf-8")

    _, rc = _run(tmp_path, "--default-token-file", str(default), config=cfg)

    assert rc == 0
    env = _servers(cfg)["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(custom)
    assert not default.exists()


@pytest.mark.parametrize("foreign", [
    pytest.param({"command": "/abs/pseudolife-mcp",
                  "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_TOKEN": TOKEN}},
                 id="other-writer"),
    pytest.param({"command": "/abs/pseudolife-mcp"}, id="no-env"),
    pytest.param({"command": "/abs/pseudolife-mcp", "env": "claude-desktop"}, id="env-not-an-object"),
    pytest.param("not-an-object", id="not-an-object"),
])
def test_a_legacy_entry_this_script_did_not_write_is_left_alone_and_reported(
    tmp_path, capsys, foreign,
):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: foreign}}), encoding="utf-8")

    _, rc = _run(tmp_path, config=cfg)

    assert rc == 0
    servers = _servers(cfg)
    assert servers[LEGACY] == foreign
    assert servers["pseudolife-desktop"]["command"] == "/abs/pseudolife-mcp"
    captured = capsys.readouterr()
    assert f"mcpServers.{LEGACY}" in captured.err and "left untouched" in captured.err
    assert "Desktop loads both" in captured.err
    assert TOKEN not in captured.out + captured.err
    # Every run says so, including one with nothing left to change.
    _, rc = _run(tmp_path, config=cfg)
    assert rc == 0
    captured = capsys.readouterr()
    assert "unchanged" in captured.out and "left untouched" in captured.err


def test_a_foreign_entry_s_literal_token_is_not_harvested(tmp_path, capsys):
    """Ownership also gates credentials: a token in someone else's entry is
    never copied into the file this script writes."""
    foreign = {"command": "/abs/pseudolife-mcp",
               "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_TOKEN": TOKEN}}
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: foreign}}), encoding="utf-8")
    token_file = tmp_path / "claude-desktop.token"

    _, rc = _run(tmp_path, "--default-token-file", str(token_file), config=cfg)

    assert rc == reg.EXIT_NO_CREDENTIAL
    assert not token_file.exists()
    servers = _servers(cfg)
    assert servers[LEGACY] == foreign
    assert "PSEUDOLIFE_MCP_TOKEN" not in servers["pseudolife-desktop"]["env"]
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_when_both_names_exist_the_desktop_entry_wins_and_the_old_one_fills_gaps(
    tmp_path, capsys,
):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(HTTPS_PROXY="http://old-proxy:3128", NO_PROXY="localhost"),
        "pseudolife-desktop": {"command": "/renamed/pseudolife-mcp", "env": {
            "PSEUDOLIFE_WRITER_ID": "claude-desktop",
            "HTTPS_PROXY": "http://new-proxy:3128"}},
    }}), encoding="utf-8")

    _, rc = _run(tmp_path, config=cfg)

    assert rc == 0
    servers = _servers(cfg)
    assert LEGACY not in servers
    env = servers["pseudolife-desktop"]["env"]
    assert env["HTTPS_PROXY"] == "http://new-proxy:3128"
    assert env["NO_PROXY"] == "localhost"
    captured = capsys.readouterr()
    assert "kept the pseudolife-desktop value of HTTPS_PROXY" in captured.out
    assert "old-proxy" not in captured.out + captured.err  # key names, never values


def test_when_both_names_carry_credentials_the_desktop_entry_s_file_is_used(
    tmp_path, capsys,
):
    legacy_file = tmp_path / "legacy" / "desktop.token"
    target_file = tmp_path / "target" / "desktop.token"
    _write_token_file(legacy_file, OTHER_TOKEN)
    _write_token_file(target_file, TOKEN)
    legacy_before = legacy_file.read_bytes()
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(PSEUDOLIFE_MCP_TOKEN_FILE=str(legacy_file),
                              PSEUDOLIFE_MCP_TOKEN=OTHER_TOKEN),
        "pseudolife-desktop": {"command": "/renamed/pseudolife-mcp", "env": {
            "PSEUDOLIFE_WRITER_ID": "claude-desktop",
            "PSEUDOLIFE_MCP_TOKEN_FILE": str(target_file),
            "PSEUDOLIFE_MCP_TOKEN": TOKEN}},
    }}), encoding="utf-8")

    _, rc = _run(tmp_path, "--default-token-file", str(tmp_path / "default.token"),
                 config=cfg)

    assert rc == 0
    servers = _servers(cfg)
    assert LEGACY not in servers
    env = servers["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(target_file)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env  # superseded by the validated file
    assert legacy_file.read_bytes() == legacy_before
    assert CredentialProvider(path=target_file).snapshot().token == TOKEN
    captured = capsys.readouterr()
    assert "value of PSEUDOLIFE_MCP_TOKEN, PSEUDOLIFE_MCP_TOKEN_FILE" in captured.out
    for value in (TOKEN, OTHER_TOKEN, str(legacy_file)):
        assert value not in captured.out + captured.err


def test_a_dry_run_with_both_names_says_what_it_would_keep(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(HTTPS_PROXY="http://old-proxy:3128"),
        "pseudolife-desktop": {"command": "/renamed/pseudolife-mcp", "env": {
            "PSEUDOLIFE_WRITER_ID": "claude-desktop",
            "HTTPS_PROXY": "http://new-proxy:3128"}},
    }}), encoding="utf-8")
    before = cfg.read_bytes()

    _, rc = _run(tmp_path, "--dry-run", config=cfg)

    assert rc == 0
    assert cfg.read_bytes() == before
    assert "would keep the pseudolife-desktop value of HTTPS_PROXY" in capsys.readouterr().out


def test_a_migrated_config_is_stable_on_rerun(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: _owned_legacy()}}), encoding="utf-8")
    _run(tmp_path, config=cfg)
    capsys.readouterr()
    before = cfg.read_bytes()
    backups = sorted(tmp_path.glob("*.bak-*"))

    _, rc = _run(tmp_path, config=cfg)

    assert rc == 0
    assert cfg.read_bytes() == before
    assert sorted(tmp_path.glob("*.bak-*")) == backups
    assert "unchanged" in capsys.readouterr().out


def test_dry_run_reports_the_migration_and_writes_nothing(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: _owned_legacy()}}), encoding="utf-8")
    before = cfg.read_bytes()

    _, rc = _run(tmp_path, "--dry-run", config=cfg)

    assert rc == 0
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))
    assert f"would migrate mcpServers.{LEGACY}" in capsys.readouterr().out


@pytest.mark.parametrize("credential", ["token-file", "literal"])
def test_a_refused_run_leaves_the_legacy_entry_in_place(tmp_path, monkeypatch, credential):
    if credential == "token-file":
        custom = tmp_path / "custom.token"
        _write_token_file(custom, TOKEN)
        legacy = _owned_legacy(PSEUDOLIFE_MCP_TOKEN_FILE=str(custom))
    else:
        legacy = _owned_legacy(PSEUDOLIFE_MCP_TOKEN=TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {LEGACY: legacy}}), encoding="utf-8")
    before = cfg.read_bytes()
    default = tmp_path / "d.token"
    _probe_returning(monkeypatch, RELEASED_HELP)

    _, rc = _run(tmp_path, "--default-token-file", str(default), config=cfg)

    assert rc == reg.EXIT_OLD_SHIM
    assert cfg.read_bytes() == before
    assert not default.exists()
    assert not list(tmp_path.glob("*.bak-*"))


@pytest.mark.parametrize("target", [
    pytest.param("junk", id="entry"),
    pytest.param({"command": "/abs/pseudolife-mcp", "env": "junk"}, id="env"),
])
def test_migration_refuses_a_desktop_entry_that_is_not_an_object(tmp_path, capsys, target):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        LEGACY: _owned_legacy(), "pseudolife-desktop": target}}), encoding="utf-8")
    before = cfg.read_bytes()

    _, rc = _run(tmp_path, config=cfg)

    assert rc == reg.EXIT_REFUSED
    assert cfg.read_bytes() == before
    assert "mcpServers.pseudolife-desktop" in capsys.readouterr().err


# -- the credential file: never point at a file we did not write or validate --

def test_token_from_env_is_written_owner_only_and_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "private" / "claude-desktop.token"
    cfg, rc = _run(tmp_path, "--token-file", str(token_file),
                   "--token-from-env", "PL_TEST_DESKTOP_TOKEN")
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert "token file: written" in captured.out


def test_a_literal_token_already_in_the_entry_is_migrated_into_the_file(tmp_path, capsys):
    """The 2026-09-19 hand fix put the token literal in the entry. A re-run
    must move it into the file and drop it from the config — never leave a
    working literal shadowed by a path to a file nobody created."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN": TOKEN, "PSEUDOLIFE_WRITER_ID": "claude-desktop"}}}}),
        encoding="utf-8")
    token_file = tmp_path / "claude-desktop.token"
    _, rc = _run(tmp_path, "--token-file", str(token_file), config=cfg)
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    captured = capsys.readouterr()
    assert "migrated the literal" in captured.out
    assert TOKEN not in captured.out and TOKEN not in captured.err
    # A rotated env token wins over the literal on the next run.


def test_env_token_wins_over_a_stale_literal(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp", "env": {"PSEUDOLIFE_MCP_TOKEN": OTHER_TOKEN}}}}),
        encoding="utf-8")
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "claude-desktop.token"
    _, rc = _run(tmp_path, "--token-file", str(token_file),
                 "--token-from-env", "PL_TEST_DESKTOP_TOKEN", config=cfg)
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert "PSEUDOLIFE_MCP_TOKEN" not in env


def test_an_existing_valid_token_file_is_used_as_is(tmp_path, capsys):
    token_file = tmp_path / "claude-desktop.token"
    _write_token_file(token_file, TOKEN)
    cfg, rc = _run(tmp_path, "--token-file", str(token_file))
    assert rc == 0
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    assert "using the existing" in capsys.readouterr().out


def test_an_unusable_existing_token_file_is_refused_and_the_config_untouched(tmp_path, capsys):
    """A plain world-readable file fails the shim's owner-only check on
    every OS; pointing the entry at it would just move the failure."""
    token_file = tmp_path / "claude-desktop.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{"mcpServers": {}}', encoding="utf-8")
    _, rc = _run(tmp_path, "--token-file", str(token_file), config=cfg)
    assert rc == 2
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"mcpServers": {}}
    err = capsys.readouterr().err
    assert "cannot be used" in err
    assert TOKEN not in err


def test_no_token_anywhere_registers_without_a_credential_and_exits_3(tmp_path, capsys):
    """Nothing to write, nothing to validate: the entry is still written
    (one less step for the user) but it carries NO token-file path, and
    the exit code + warning say the daemon will refuse it."""
    token_file = tmp_path / "claude-desktop.token"
    cfg, rc = _run(tmp_path, "--default-token-file", str(token_file))
    assert rc == 3
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in env
    assert not token_file.exists()
    err = capsys.readouterr().err
    assert "WITHOUT a credential" in err and "PSEUDOLIFE_MCP_TOKEN=" in err


def test_relative_token_file_is_refused_before_anything_is_touched(tmp_path, capsys):
    cfg, rc = _run(tmp_path, "--token-file", "relative.token")
    assert rc == 2
    assert not cfg.exists()
    assert "absolute" in capsys.readouterr().err


def test_token_from_env_without_a_token_file_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    cfg, rc = _run(tmp_path, "--token-from-env", "PL_TEST_DESKTOP_TOKEN")
    assert rc == 2
    assert not cfg.exists()
    assert "--token-file" in capsys.readouterr().err


def test_default_token_file_preserves_and_validates_existing_custom_file(
    tmp_path, capsys,
):
    custom = tmp_path / "custom" / "desktop.token"
    default = tmp_path / "default" / "desktop.token"
    _write_token_file(custom, TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(custom)},
    }}}), encoding="utf-8")

    _, rc = _run(tmp_path, "--default-token-file", str(default), config=cfg)

    assert rc == 0
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(custom)
    assert not default.exists()
    assert "using the existing" in capsys.readouterr().out


def test_missing_explicit_replacement_refuses_without_rewriting_working_config(
    tmp_path, capsys,
):
    old_file = tmp_path / "old.token"
    replacement = tmp_path / "missing.token"
    _write_token_file(old_file, TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(old_file)},
    }}}
    cfg.write_text(json.dumps(original), encoding="utf-8")

    _, rc = _run(tmp_path, "--token-file", str(replacement), config=cfg)

    assert rc == reg.EXIT_REFUSED
    assert json.loads(cfg.read_text(encoding="utf-8")) == original
    assert "explicit token file" in capsys.readouterr().err


def test_missing_existing_custom_file_is_refused_on_ordinary_rerun(
    tmp_path, capsys,
):
    missing = tmp_path / "missing-custom.token"
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(missing)},
    }}}
    cfg.write_text(json.dumps(original), encoding="utf-8")

    _, rc = _run(
        tmp_path, "--default-token-file", str(tmp_path / "default.token"),
        config=cfg,
    )

    assert rc == reg.EXIT_REFUSED
    assert json.loads(cfg.read_text(encoding="utf-8")) == original
    assert "configured token file" in capsys.readouterr().err


@pytest.mark.parametrize("source_kind", ["singular", "ambiguous-map"])
def test_existing_custom_file_is_not_rotated_by_installer_sources(
    tmp_path, monkeypatch, capsys, source_kind,
):
    custom = tmp_path / "custom.token"
    _write_token_file(custom, TOKEN)
    before = custom.read_bytes()
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(custom)},
    }}}), encoding="utf-8")
    if source_kind == "singular":
        monkeypatch.setenv("PL_TEST_TOKEN", OTHER_TOKEN)
        source_args = ("--token-from-env", "PL_TEST_TOKEN")
    else:
        token_map = f"{OTHER_TOKEN}:claude-desktop,{TOKEN}:claude-desktop"
        monkeypatch.setenv("PL_TEST_TOKEN_MAP", token_map)
        source_args = ("--tokens-from-env", "PL_TEST_TOKEN_MAP")

    _, rc = _run(
        tmp_path, "--default-token-file", str(tmp_path / "default.token"),
        *source_args, config=cfg,
    )

    assert rc == 0
    assert custom.read_bytes() == before
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(custom)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out and TOKEN not in captured.err
    assert OTHER_TOKEN not in captured.out and OTHER_TOKEN not in captured.err


def test_explicit_existing_file_is_validated_not_overwritten_by_auto_source(
    tmp_path, monkeypatch,
):
    old_file = tmp_path / "old.token"
    chosen = tmp_path / "chosen.token"
    _write_token_file(old_file, TOKEN)
    _write_token_file(chosen, OTHER_TOKEN)
    before = chosen.read_bytes()
    monkeypatch.setenv("PL_TEST_TOKEN", TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(old_file)},
    }}}), encoding="utf-8")

    _, rc = _run(
        tmp_path, "--token-file", str(chosen),
        "--token-from-env", "PL_TEST_TOKEN", config=cfg,
    )

    assert rc == 0
    assert chosen.read_bytes() == before
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(chosen)


@pytest.mark.parametrize("explicit", [False, True])
def test_validated_existing_file_removes_superseded_literal(tmp_path, capsys, explicit):
    token_file = tmp_path / "private.token"
    _write_token_file(token_file, TOKEN)
    before = token_file.read_bytes()
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
                "PSEUDOLIFE_MCP_TOKEN": OTHER_TOKEN},
    }}}), encoding="utf-8")
    args = ["--token-file", str(token_file)] if explicit else []
    _, rc = _run(tmp_path, *args, config=cfg)
    assert rc == 0
    assert token_file.read_bytes() == before
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-desktop"]["env"]
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert OTHER_TOKEN not in captured.out + captured.err


def test_map_only_auth_writes_unique_desktop_principal_without_printing_secret(
    tmp_path, monkeypatch, capsys,
):
    desktop_token = "desktop-secret-" + "d" * 32
    monkeypatch.setenv(
        "PL_TEST_TOKEN_MAP",
        f"other-secret:codex,{desktop_token}:claude-desktop",
    )
    token_file = tmp_path / "desktop.token"

    cfg, rc = _run(
        tmp_path, "--default-token-file", str(token_file),
        "--tokens-from-env", "PL_TEST_TOKEN_MAP",
    )

    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == desktop_token
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    captured = capsys.readouterr()
    assert desktop_token not in captured.out and desktop_token not in captured.err
    assert "other-secret" not in captured.out and "other-secret" not in captured.err


@pytest.mark.parametrize(
    "token_map",
    [
        "codex-secret:codex",
        "first-secret:claude-desktop,second-secret:claude-desktop",
        "malformed",
    ],
)
def test_map_without_one_desktop_credential_refuses_without_config_rewrite(
    tmp_path, monkeypatch, capsys, token_map,
):
    monkeypatch.setenv("PL_TEST_TOKEN_MAP", token_map)
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{"keep": true}', encoding="utf-8")

    _, rc = _run(
        tmp_path, "--default-token-file", str(tmp_path / "desktop.token"),
        "--tokens-from-env", "PL_TEST_TOKEN_MAP", config=cfg,
    )

    assert rc == reg.EXIT_REFUSED
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"keep": True}
    captured = capsys.readouterr()
    for secret in ("codex-secret", "first-secret", "second-secret"):
        assert secret not in captured.out and secret not in captured.err


def test_map_dry_run_selects_path_but_writes_nothing_and_hides_secret(
    tmp_path, monkeypatch, capsys,
):
    secret = "dry-run-secret-" + "z" * 32
    monkeypatch.setenv("PL_TEST_TOKEN_MAP", f"{secret}:claude-desktop")
    token_file = tmp_path / "desktop.token"

    cfg, rc = _run(
        tmp_path, "--dry-run", "--default-token-file", str(token_file),
        "--tokens-from-env", "PL_TEST_TOKEN_MAP",
    )

    assert rc == 0
    assert not cfg.exists() and not token_file.exists()
    captured = capsys.readouterr()
    assert str(token_file) in captured.out
    assert secret not in captured.out and secret not in captured.err


def test_installers_pass_default_and_map_sources_by_environment_name_only():
    sh = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    for text in (sh, ps):
        assert "--default-token-file" in text
        assert "--tokens-from-env" in text
        assert "PSEUDOLIFE_DESKTOP_TOKENS_SOURCE" in text
    assert 'PSEUDOLIFE_DESKTOP_TOKENS_SOURCE="$tokens_source"' in sh
    assert '$env:PSEUDOLIFE_DESKTOP_TOKENS_SOURCE = Get-DesktopTokensSource' in ps


def test_script_loads_without_package_and_imports_only_for_credentials():
    """Both installers run it with whatever python they find; the package
    (standard-library only itself) is imported lazily, from the checkout,
    only when a token file must be written or validated."""
    import re

    text = (ROOT / "ops" / "register_claude_desktop.py").read_text(encoding="utf-8")
    statements = re.findall(r"^\s*(?:from|import)\s+pseudolife_memory\b.*$", text, re.M)
    assert statements == [
        "        from pseudolife_memory import credentials",
        "        from pseudolife_memory.principals import parse_token_map",
    ]
    assert text.index(statements[0]) > text.index("def _credentials(")
    assert text.index(statements[1]) > text.index("def _desktop_token_from_map(")


# -- the shim must be able to READ the file the entry points at ---------------
#
# The installers take the shim from PyPI, and every release through 0.15.0
# reads only the literal PSEUDOLIFE_MCP_TOKEN — Desktop never delivers that,
# so an entry carrying PSEUDOLIFE_MCP_TOKEN_FILE against such a shim fails
# exactly like the 2026-09-19 incident, with no hint. The registrar probes
# `<command> --help` for the capability marker before it writes anything;
# refusal needs positive evidence (a help text that answers and lacks it, or
# the "unknown mode" of a shim from before --help existed), while a probe
# that yields no evidence is noted and not blocking.

# The real thing: `pseudolife-mcp --help` from pseudolife-mcp==0.15.0 as
# published on PyPI, captured 2026-09-20. The negative direction of the
# guard is pinned to this, not to a stripped copy of today's text.
RELEASED_HELP = (ROOT / "tests" / "fixtures" / "pseudolife-mcp-0.15.0-help.txt").read_text(encoding="utf-8")
# The most confusable text there is: today's usage with only the marker
# line removed (the rest of the credentials paragraph survives).
STRIPPED_USAGE = "\n".join(
    line for line in CHECKOUT_USAGE.splitlines()
    if "PSEUDOLIFE_MCP_TOKEN_FILE" not in line) + "\n"
PRE_HELP_ANSWER = "unknown mode '--help'; see: pseudolife-mcp --help\n"


class _Probe:
    """Records every subprocess.run the registrar makes."""

    def __init__(self):
        self.argv: list[list[str]] = []
        self.kwargs: list[dict] = []


def _probe_returning(monkeypatch, stdout: str, returncode: int = 0, stderr: str = ""):
    probe = _Probe()

    def fake_run(argv, **kwargs):
        probe.argv.append(list(argv))
        probe.kwargs.append(kwargs)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(reg.subprocess, "run", fake_run)
    return probe


def _probe_raising(monkeypatch, exc: BaseException):
    def fake_run(argv, **kwargs):
        raise exc

    monkeypatch.setattr(reg.subprocess, "run", fake_run)


def _gated_run(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "private" / "claude-desktop.token"
    cfg, rc = _run(tmp_path, "--token-file", str(token_file),
                   "--token-from-env", "PL_TEST_DESKTOP_TOKEN", *extra)
    return cfg, token_file, rc


def test_registrar_refuses_the_released_shim_before_writing_anything(tmp_path, monkeypatch, capsys):
    probe = _probe_returning(monkeypatch, RELEASED_HELP)
    cfg, token_file, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 4
    assert probe.argv == [["/abs/pseudolife-mcp", "--help"]]
    assert not cfg.exists(), "config must be untouched"
    assert not token_file.exists(), "the credential file must not be written for a shim that cannot read it"
    captured = capsys.readouterr()
    assert "/abs/pseudolife-mcp" in captured.err
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in captured.err
    assert "upgrade" in captured.err.lower()
    assert "--skip-shim-check" in captured.err
    assert TOKEN not in captured.out and TOKEN not in captured.err


def test_registrar_probes_preserved_configured_token_path_before_writing(
    tmp_path, monkeypatch,
):
    custom = tmp_path / "custom.token"
    default = tmp_path / "default.token"
    _write_token_file(custom, TOKEN)
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {"pseudolife-desktop": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": str(custom)},
    }}}
    cfg.write_text(json.dumps(original), encoding="utf-8")
    _probe_returning(monkeypatch, RELEASED_HELP)

    _, rc = _run(
        tmp_path, "--default-token-file", str(default), config=cfg,
    )

    assert rc == reg.EXIT_OLD_SHIM
    assert json.loads(cfg.read_text(encoding="utf-8")) == original
    assert custom.exists() and not default.exists()


def test_registrar_probes_fresh_default_token_path_before_writing(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "default.token"
    _probe_returning(monkeypatch, RELEASED_HELP)

    cfg, rc = _run(
        tmp_path, "--default-token-file", str(token_file),
        "--token-from-env", "PL_TEST_DESKTOP_TOKEN",
    )

    assert rc == reg.EXIT_OLD_SHIM
    assert not cfg.exists() and not token_file.exists()


def test_registrar_refuses_a_help_that_mentions_everything_but_the_file(tmp_path, monkeypatch):
    _probe_returning(monkeypatch, STRIPPED_USAGE)
    cfg, token_file, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 4
    assert not cfg.exists() and not token_file.exists()


def test_registrar_refuses_a_shim_from_before_help_existed(tmp_path, monkeypatch, capsys):
    """Releases before 2026-07-16 answer --help with "unknown mode" and
    exit 2: positive evidence of a shim that cannot read a token file."""
    _probe_returning(monkeypatch, "", returncode=2, stderr=PRE_HELP_ANSWER)
    cfg, token_file, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 4
    assert not cfg.exists() and not token_file.exists()
    assert "unknown mode" in capsys.readouterr().err


def test_registrar_accepts_a_shim_whose_help_lists_the_token_file(tmp_path, monkeypatch, capsys):
    probe = _probe_returning(monkeypatch, CHECKOUT_USAGE)
    cfg, token_file, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 0
    assert probe.argv == [["/abs/pseudolife-mcp", "--help"]]
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-desktop"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    out = capsys.readouterr().out
    assert "shim check: /abs/pseudolife-mcp reads PSEUDOLIFE_MCP_TOKEN_FILE" in out
    assert "could not verify" not in out


def test_a_marker_printed_on_stderr_counts(tmp_path, monkeypatch):
    """A wrapper that prints its help on stderr is still a capable shim."""
    _probe_returning(monkeypatch, "", stderr=CHECKOUT_USAGE)
    cfg, _, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 0 and cfg.exists()


def test_the_probe_gets_no_stdin_and_not_the_token_variable(tmp_path, monkeypatch):
    """A wrapper that drops its arguments would start the real stdio shim:
    with no stdin it exits on EOF instead of holding the installer's
    terminal, and it must not inherit the bearer the registrar is about
    to write. The UTF-8 pin keeps the usage text's em dashes from failing
    the child on a narrow console code page."""
    monkeypatch.setenv("PL_UNRELATED", "kept")
    probe = _probe_returning(monkeypatch, CHECKOUT_USAGE)
    _, _, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 0
    (kwargs,) = probe.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == reg.SHIM_PROBE_TIMEOUT
    assert "PL_TEST_DESKTOP_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["PL_UNRELATED"] == "kept"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert os.environ["PL_TEST_DESKTOP_TOKEN"] == TOKEN, "the registrar's own environment is untouched"


def test_probe_scrubs_canonical_credentials_and_installer_source(
    tmp_path, monkeypatch, capsys,
):
    canonical_token = "canonical-" + "c" * 40
    mapped_token = "mapped-" + "m" * 40
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", canonical_token)
    monkeypatch.setenv(
        "PSEUDOLIFE_MCP_TOKENS", f"{mapped_token}:claude-desktop",
    )
    monkeypatch.setenv(
        "PSEUDOLIFE_MCP_TOKEN_FILE", str(tmp_path / "canonical.token"),
    )
    monkeypatch.setenv("PSEUDOLIFE_DESKTOP_TOKEN_SOURCE", TOKEN)
    monkeypatch.setenv("PL_UNRELATED", "kept")
    probe = _probe_returning(monkeypatch, CHECKOUT_USAGE)
    selected_file = tmp_path / "selected.token"

    cfg, rc = _run(
        tmp_path, "--default-token-file", str(selected_file),
        "--token-from-env", "PSEUDOLIFE_DESKTOP_TOKEN_SOURCE",
    )

    assert rc == 0 and cfg.exists()
    assert CredentialProvider(path=selected_file).snapshot().token == TOKEN
    sensitive_names = {
        "PSEUDOLIFE_MCP_TOKEN",
        "PSEUDOLIFE_MCP_TOKENS",
        "PSEUDOLIFE_MCP_TOKEN_FILE",
        "PSEUDOLIFE_DESKTOP_TOKEN_SOURCE",
    }
    inherited_sensitive_names = sensitive_names.intersection(
        probe.kwargs[0]["env"]
    )
    assert inherited_sensitive_names == set()
    assert probe.kwargs[0]["env"]["PL_UNRELATED"] == "kept"
    captured = capsys.readouterr()
    for secret in (TOKEN, canonical_token, mapped_token):
        assert secret not in captured.out and secret not in captured.err


@pytest.mark.parametrize("failure", [
    pytest.param(("raise", FileNotFoundError(2, "no such file")), id="not-found"),
    pytest.param(("raise", subprocess.TimeoutExpired(["x"], 30)), id="timeout"),
    pytest.param(("raise", PermissionError(13, "not executable")), id="not-executable"),
    pytest.param(("return", 1, "", "Traceback: something else broke"), id="nonzero-exit"),
    pytest.param(("return", 0, "", ""), id="exit-zero-no-output"),
    pytest.param(("return", 0, "  \n", ""), id="exit-zero-blank-output"),
])
def test_registrar_proceeds_with_a_note_when_the_probe_yields_no_evidence(tmp_path, monkeypatch, capsys, failure):
    """No positive evidence either way: keep the pre-guard behaviour (the
    installer already verified the command exists) and say the check did
    not run, rather than block a wrapper script or a slow disk. A wrong
    None costs a note; a wrong False would block the install."""
    if failure[0] == "raise":
        _probe_raising(monkeypatch, failure[1])
    else:
        _, rc_, out_, err_ = failure
        _probe_returning(monkeypatch, out_, returncode=rc_, stderr=err_)
    cfg, _, rc = _gated_run(tmp_path, monkeypatch)
    assert rc == 0
    assert cfg.exists()
    out = capsys.readouterr().out
    assert "could not verify" in out
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in out


def test_shim_probe_is_skipped_without_a_token_file(tmp_path, monkeypatch):
    """An open daemon needs no credential, so there is nothing to verify —
    and the probe must not add a process spawn to every registration."""
    def never(argv, **kwargs):
        raise AssertionError(f"probe ran without a token file: {argv}")

    monkeypatch.setattr(reg.subprocess, "run", never)
    cfg, rc = _run(tmp_path)
    assert rc == 0
    assert cfg.exists()


def test_skip_shim_check_flag_bypasses_the_probe(tmp_path, monkeypatch, capsys):
    probe = _probe_returning(monkeypatch, RELEASED_HELP)
    cfg, _, rc = _gated_run(tmp_path, monkeypatch, "--skip-shim-check")
    assert rc == 0
    assert probe.argv == []
    assert cfg.exists()
    assert "skipped" in capsys.readouterr().out


def test_dry_run_still_reports_an_old_shim(tmp_path, monkeypatch, capsys):
    """A dry run exists to show what a real run would do; an old shim is
    the most useful thing it can report."""
    _probe_returning(monkeypatch, RELEASED_HELP)
    cfg, token_file, rc = _gated_run(tmp_path, monkeypatch, "--dry-run")
    assert rc == 4
    assert not cfg.exists() and not token_file.exists()
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in capsys.readouterr().err


def test_the_checkout_cli_advertises_token_file_support_in_its_help():
    """The marker the registrar looks for and the help the shim prints are
    two files; this pins them together, and pins the marker to the exact
    variable the credential provider reads, so a capable-looking shim is
    a capable one. The released and stripped texts prove the classifier
    is load-bearing: the same function rejects a help without it."""
    assert reg.TOKEN_FILE_MARKER == "PSEUDOLIFE_MCP_TOKEN_FILE"
    assert reg.help_shows_token_file_support(CHECKOUT_USAGE) is True
    assert reg.help_shows_token_file_support(RELEASED_HELP) is False
    assert reg.help_shows_token_file_support(STRIPPED_USAGE) is False
    assert reg.help_shows_token_file_support("") is False


def test_the_marker_is_the_variable_the_credential_provider_reads(tmp_path, monkeypatch):
    token_file = tmp_path / "claude-desktop.token"
    _write_token_file(str(token_file), TOKEN)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setenv(reg.TOKEN_FILE_MARKER, str(token_file))
    provider = CredentialProvider.from_environment()
    assert provider.path == token_file
    assert provider.snapshot().token == TOKEN


def test_the_released_help_fixture_is_the_real_thing():
    """Guards the fixture itself: a real usage text (so the negative tests
    exercise the same substring search the positive one does), from a
    release with no token-file support anywhere in it."""
    assert RELEASED_HELP.startswith("pseudolife-mcp — persistent long-term memory")
    assert "usage: pseudolife-mcp [mode]" in RELEASED_HELP
    assert "help           show this message" in RELEASED_HELP
    assert "PSEUDOLIFE_MCP_TOKEN" not in RELEASED_HELP


# -- through a real process ---------------------------------------------------

def _launcher(tmp_path, name: str, body: str) -> str:
    """An executable that forwards its arguments to a python snippet —
    the shape of a forwarding wrapper script — on either OS."""
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        command = tmp_path / f"{name}.cmd"
        command.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        command = tmp_path / name
        command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
    return str(command)


def test_a_real_subprocess_probe_of_this_checkout_s_cli_passes(tmp_path):
    """The console script beside the interpreter is deliberately not used:
    an editable install may point it at a different checkout."""
    command = _launcher(tmp_path, "capable", (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pseudolife_memory.cli import main\n"
        "sys.argv = ['pseudolife-mcp'] + sys.argv[1:]\n"
        "main()\n"))
    assert reg.shim_reads_token_file(command) is True


def test_a_real_subprocess_probe_of_the_released_help_is_refused(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "pseudolife-mcp-0.15.0-help.txt"
    command = _launcher(tmp_path, "released", (
        "import sys\n"
        f"sys.stdout.write(open({str(fixture)!r}, encoding='utf-8').read())\n"))
    assert reg.shim_reads_token_file(command) is False


def test_a_real_subprocess_probe_of_a_pre_help_shim_is_refused(tmp_path):
    command = _launcher(tmp_path, "prehelp", (
        "import sys\n"
        f"sys.stderr.write({PRE_HELP_ANSWER!r})\n"
        "sys.exit(2)\n"))
    assert reg.shim_reads_token_file(command) is False


def test_a_real_subprocess_probe_that_swallows_its_arguments_does_not_hang(tmp_path):
    """A wrapper that drops its arguments and reads stdin (what the real
    stdio shim would do) must see EOF at once, not the installer's
    terminal — and yields no evidence."""
    command = _launcher(tmp_path, "swallow", (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.exit(0 if data == '' else 9)\n"))
    assert reg.shim_reads_token_file(command, timeout=20) is None
