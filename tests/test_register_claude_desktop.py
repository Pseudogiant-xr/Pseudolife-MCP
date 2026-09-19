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
from pathlib import Path

import pytest

from pseudolife_memory.credentials import CredentialProvider, _write_token_file

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "register_claude_desktop", ROOT / "ops" / "register_claude_desktop.py")
reg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reg)

TOKEN = "t0k3n-" + "x" * 40
OTHER_TOKEN = "0th3r-" + "y" * 40


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
    assert merged["mcpServers"]["pseudolife-memory"]["command"] == "/abs/pseudolife-mcp"


def test_merge_keeps_user_added_env_keys_and_the_literal_token_by_default():
    """Unmanaged keys survive; the literal token is only dropped when the
    caller says its value now lives in the token file."""
    existing = {"mcpServers": {"pseudolife-memory": {
        "command": "old", "env": {"PSEUDOLIFE_MCP_TOKEN": "keep-me",
                                  "HTTPS_PROXY": "http://proxy:3128",
                                  "PSEUDOLIFE_WRITER_ID": "stale"}}}}
    merged, changed = reg.merge_config(existing, _entry())
    env = merged["mcpServers"]["pseudolife-memory"]["env"]
    assert changed is True
    assert env["PSEUDOLIFE_MCP_TOKEN"] == "keep-me"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["PSEUDOLIFE_WRITER_ID"] == "claude-desktop"
    dropped, _ = reg.merge_config(existing, _entry("/abs/t.token"), drop_literal_token=True)
    assert "PSEUDOLIFE_MCP_TOKEN" not in dropped["mcpServers"]["pseudolife-memory"]["env"]
    assert dropped["mcpServers"]["pseudolife-memory"]["env"]["HTTPS_PROXY"] == "http://proxy:3128"


def test_merge_drops_a_stale_args_list():
    existing = {"mcpServers": {"pseudolife-memory": {
        "command": "old", "args": ["--legacy"], "env": {}}}}
    merged, _ = reg.merge_config(existing, _entry())
    assert "args" not in merged["mcpServers"]["pseudolife-memory"]


def test_merge_is_idempotent():
    merged, changed = reg.merge_config({}, _entry())
    assert changed is True
    again, changed_again = reg.merge_config(merged, _entry())
    assert changed_again is False
    assert again == merged


def test_merge_rejects_non_object_shapes():
    with pytest.raises(reg.ConfigError, match="mcpServers"):
        reg.merge_config({"mcpServers": []}, _entry())
    with pytest.raises(reg.ConfigError, match="mcpServers.pseudolife-memory"):
        reg.merge_config({"mcpServers": {"pseudolife-memory": "not-an-object"}}, _entry())


def test_merge_treats_a_null_mcp_servers_as_empty():
    merged, changed = reg.merge_config({"mcpServers": None}, _entry())
    assert changed is True
    assert merged["mcpServers"]["pseudolife-memory"]["command"] == "/abs/pseudolife-mcp"


# -- main: files, backups, output ---------------------------------------------

def _run(tmp_path, *args, config=None):
    cfg = config or (tmp_path / "claude_desktop_config.json")
    argv = ["--config", str(cfg), "--command", "/abs/pseudolife-mcp", *args]
    return cfg, reg.main(argv)


def test_main_creates_a_missing_config(tmp_path, capsys):
    cfg, rc = _run(tmp_path)
    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["pseudolife-memory"]["command"] == "/abs/pseudolife-mcp"
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
                   "--token-file", str(tmp_path / "claude-desktop.token"),
                   "--token-from-env", "PL_TEST_DESKTOP_TOKEN")
    assert rc == 3  # no token in the env, no file: registration would lack a credential
    assert not cfg.exists()
    out = capsys.readouterr().out
    assert "dry run" in out


def test_main_notes_a_dropped_args_list(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
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


# -- the credential file: never point at a file we did not write or validate --

def test_token_from_env_is_written_owner_only_and_never_printed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "private" / "claude-desktop.token"
    cfg, rc = _run(tmp_path, "--token-file", str(token_file),
                   "--token-from-env", "PL_TEST_DESKTOP_TOKEN")
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
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
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": "/old/pseudolife-mcp",
        "env": {"PSEUDOLIFE_MCP_TOKEN": TOKEN, "PSEUDOLIFE_WRITER_ID": "claude-desktop"}}}}),
        encoding="utf-8")
    token_file = tmp_path / "claude-desktop.token"
    _, rc = _run(tmp_path, "--token-file", str(token_file), config=cfg)
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
    assert env["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(token_file)
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    captured = capsys.readouterr()
    assert "migrated the literal" in captured.out
    assert TOKEN not in captured.out and TOKEN not in captured.err
    # A rotated env token wins over the literal on the next run.


def test_env_token_wins_over_a_stale_literal(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": "/old/pseudolife-mcp", "env": {"PSEUDOLIFE_MCP_TOKEN": OTHER_TOKEN}}}}),
        encoding="utf-8")
    monkeypatch.setenv("PL_TEST_DESKTOP_TOKEN", TOKEN)
    token_file = tmp_path / "claude-desktop.token"
    _, rc = _run(tmp_path, "--token-file", str(token_file),
                 "--token-from-env", "PL_TEST_DESKTOP_TOKEN", config=cfg)
    assert rc == 0
    assert CredentialProvider(path=token_file).snapshot().token == TOKEN
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
    assert "PSEUDOLIFE_MCP_TOKEN" not in env


def test_an_existing_valid_token_file_is_used_as_is(tmp_path, capsys):
    token_file = tmp_path / "claude-desktop.token"
    _write_token_file(token_file, TOKEN)
    cfg, rc = _run(tmp_path, "--token-file", str(token_file))
    assert rc == 0
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
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
    cfg, rc = _run(tmp_path, "--token-file", str(token_file))
    assert rc == 3
    env = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
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


def test_script_loads_without_the_package_and_imports_it_only_for_the_file_writer():
    """Both installers run it with whatever python they find; the package
    (standard-library only itself) is imported lazily, from the checkout,
    only when a token file must be written or validated."""
    import re

    text = (ROOT / "ops" / "register_claude_desktop.py").read_text(encoding="utf-8")
    statements = re.findall(r"^\s*(?:from|import)\s+pseudolife_memory\b.*$", text, re.M)
    assert statements == ["        from pseudolife_memory import credentials"]
    assert text.index(statements[0]) > text.index("def _credentials(")
