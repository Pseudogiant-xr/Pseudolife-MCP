"""Host CLI commands use Codex's installed connection outside its MCP env."""
import os
import sys

import pytest

from pseudolife_memory import cli, codex_connection, doctor_cli, lease_cli
from pseudolife_memory.credentials import _write_token_file


@pytest.fixture
def connection(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    token = home / "pseudolife" / "token"
    token.parent.mkdir(parents=True)
    _write_token_file(token, "fixture-bearer")
    codex_connection._private_json(home / "pseudolife" / "connection.json",
        codex_connection._connection_values("https://memory.example.com", token))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_SESSION_ID", "fixture-session")
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE",
                "PSEUDOLIFE_MCP_DAEMON_URL"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)
    return home, token


@pytest.mark.parametrize("mode", ["lease", "doctor"])
@pytest.mark.parametrize("ambient_token", [False, True])
def test_host_command_discovers_managed_codex_connection(connection, monkeypatch, mode, ambient_token):
    _, token = connection
    if ambient_token:
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-bearer")
    observed = {}

    def command(*args):
        observed.update(os.environ)
        return 0

    monkeypatch.setattr(lease_cli, "main", command)
    monkeypatch.setattr(doctor_cli, "run_doctor", command)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", mode])
    if mode == "lease":
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 0
    else:
        cli.main()
    assert observed.get("PSEUDOLIFE_MCP_DAEMON_URL") == "https://memory.example.com"
    if not ambient_token:
        assert observed.get("PSEUDOLIFE_MCP_TOKEN_FILE") == str(token)


def test_explicit_origin_keeps_explicit_credentials(connection, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "https://other.example.com")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "other-bearer")
    seen = []
    monkeypatch.setattr(lease_cli, "main", lambda args: seen.append(dict(os.environ)) or 0)
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "lease"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert seen[0]["PSEUDOLIFE_MCP_DAEMON_URL"] == "https://other.example.com"
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in seen[0]


def test_mismatched_ambient_bearer_refuses_before_command(connection, monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "other-bearer")
    called = []
    monkeypatch.setattr(lease_cli, "main", lambda args: called.append(True))
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "lease"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2 and not called
    assert "PSEUDOLIFE_MCP_DAEMON_URL" in capsys.readouterr().err
    assert "PSEUDOLIFE_MCP_DAEMON_URL" not in os.environ


def test_non_codex_host_ignores_saved_codex_connection(connection, monkeypatch):
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert codex_connection.host_command_environment(os.environ) == {}


def test_absent_saved_connection_keeps_existing_fallback(connection):
    home, _ = connection
    (home / "pseudolife" / "connection.json").unlink()
    assert codex_connection.host_command_environment(os.environ) == {}


def test_matching_non_ascii_bearer_can_select_saved_origin(connection, monkeypatch):
    _, token = connection
    _write_token_file(token, "fixture-bearer-é")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-bearer-é")
    assert codex_connection.host_command_environment(os.environ) == {
        "PSEUDOLIFE_MCP_DAEMON_URL": "https://memory.example.com"}


def test_explicit_token_file_can_select_only_its_matching_origin(connection, monkeypatch):
    _, token = connection
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(token))
    assert codex_connection.host_command_environment(os.environ) == {
        "PSEUDOLIFE_MCP_DAEMON_URL": "https://memory.example.com"}


def test_unsafe_connection_refuses_without_traceback_or_running_command(connection, monkeypatch, capsys):
    from pseudolife_memory.private_state import PrivateStateError

    def unsafe(*args):
        raise PrivateStateError("fixture ownership refusal")

    monkeypatch.setattr(codex_connection, "_open_state", unsafe)
    called = []
    monkeypatch.setattr(lease_cli, "main", lambda args: called.append(True))
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "lease"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2 and not called
    assert "unsafe or malformed" in capsys.readouterr().err


@pytest.mark.parametrize("arguments", [
    ["lease", "--help"], ["doctor", "-h"],
    ["lease", "run", "fixture", "--no-board", "--", "echo", "fixture"],
    ["doctor", "--disposable-proof"],
])
def test_offline_commands_bypass_saved_connection(connection, monkeypatch, arguments):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "other-bearer")
    called = []
    monkeypatch.setattr(lease_cli, "main", lambda args: called.append(True) or 0)
    monkeypatch.setattr(doctor_cli, "run_doctor", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", *arguments])
    if arguments[0] == "lease":
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 0
    else:
        cli.main()
    assert called == [True]
    assert "PSEUDOLIFE_MCP_DAEMON_URL" not in os.environ


def test_child_no_board_flag_does_not_disable_host_discovery(connection, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "other-bearer")
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "lease", "run", "fixture",
                                      "--", "child", "--no-board"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
