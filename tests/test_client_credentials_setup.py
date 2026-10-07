"""ops/client_credentials.py: the installers' shared board-token helper.

Both installers call it, so minting, per-client token files, the in-place
Claude Code registration update and the plugin-hook credential behave the
same on every platform. Every case runs the script as the installers do,
in a subprocess, and asserts the secret never reaches its output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "ops" / "client_credentials.py"
TOKEN_KEYS = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS", "PSEUDOLIFE_MCP_TOKEN_FILE",
              "PSEUDOLIFE_INSTALLER_TOKEN", "PSEUDOLIFE_INSTALLER_TOKENS")


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {key: value for key, value in os.environ.items() if key not in TOKEN_KEYS}
    base.update(env or {})
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True,
                          text=True, timeout=60, env=base, check=False)


def _active(text: str, key: str) -> list[str]:
    return re.findall(rf"(?m)^{key}=(.*)$", text)


def _owner_only(path: Path) -> None:
    """The credential reader's own check: owner-only DACL on Windows, 0600
    and owned by this user elsewhere."""
    from pseudolife_memory.credentials import _validate_file
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        _validate_file(fd)
    finally:
        os.close(fd)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- mint ---------------------------------------------------------------------

def test_mint_writes_a_random_token_to_a_tokenless_env_file(tmp_path):
    env_file = tmp_path / ".env"
    example = (ROOT / "ops" / ".env.example").read_text(encoding="utf-8")
    env_file.write_text(example, encoding="utf-8")
    first = _run("mint", "--env-file", str(env_file))
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"status": "minted"}
    text = env_file.read_text(encoding="utf-8")
    [token] = _active(text, "PSEUDOLIFE_MCP_TOKEN")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", token)
    assert token not in first.stdout + first.stderr
    assert text.startswith(example)  # nothing else in the file changed
    _owner_only(env_file)

    other = tmp_path / "other.env"
    other.write_text(example, encoding="utf-8")
    _run("mint", "--env-file", str(other))
    assert _active(other.read_text(encoding="utf-8"), "PSEUDOLIFE_MCP_TOKEN") != [token]


@pytest.mark.parametrize("line", [
    "PSEUDOLIFE_MCP_TOKEN=already-set",
    "PSEUDOLIFE_MCP_TOKENS=aLongRandomA:claude-code",
])
def test_mint_keeps_an_existing_token(tmp_path, line):
    env_file = tmp_path / ".env"
    env_file.write_text(f"#PSEUDOLIFE_MCP_TOKEN=\n{line}\n", encoding="utf-8")
    before = env_file.read_bytes()
    result = _run("mint", "--env-file", str(env_file))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "present"}
    assert env_file.read_bytes() == before


def test_mint_defers_to_a_token_in_the_installers_environment(tmp_path):
    """Compose takes the shell's value over ops/.env, so a token there is
    the daemon's token: minting another would split the two."""
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    result = _run("mint", "--env-file", str(env_file),
                  env={"PSEUDOLIFE_MCP_TOKEN": "from-the-shell"})
    assert json.loads(result.stdout) == {"status": "present"}
    assert _active(env_file.read_text(encoding="utf-8"), "PSEUDOLIFE_MCP_TOKEN") == []


def test_mint_fills_an_empty_assignment_in_place(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nPSEUDOLIFE_MCP_TOKEN=\nB=2\n", encoding="utf-8")
    result = _run("mint", "--env-file", str(env_file))
    assert json.loads(result.stdout) == {"status": "minted"}
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "A=1" and lines[2] == "B=2"
    assert re.fullmatch(r"PSEUDOLIFE_MCP_TOKEN=[A-Za-z0-9_-]{43,}", lines[1])


# -- token-file ---------------------------------------------------------------

def test_token_file_writes_the_singular_token_owner_only(tmp_path):
    target = tmp_path / "creds" / "claude-code.token"
    result = _run("token-file", "--principal", "claude-code", "--path", str(target),
                  env={"PSEUDOLIFE_INSTALLER_TOKEN": "fixture-singular"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "ready", "path": str(target)}
    assert CredentialProvider(path=target).snapshot().token == "fixture-singular"
    assert "fixture-singular" not in result.stdout + result.stderr
    _owner_only(target)


def test_token_file_prefers_the_principals_own_map_entry(tmp_path):
    target = tmp_path / "claude-code.token"
    result = _run("token-file", "--principal", "claude-code", "--path", str(target),
                  env={"PSEUDOLIFE_INSTALLER_TOKEN": "fixture-singular",
                       "PSEUDOLIFE_INSTALLER_TOKENS": "fixture-desk:claude-desktop,"
                                                      "fixture-code:claude-code"})
    assert json.loads(result.stdout)["status"] == "ready"
    assert CredentialProvider(path=target).snapshot().token == "fixture-code"


def test_token_file_without_a_usable_source_writes_nothing(tmp_path):
    target = tmp_path / "gemini.token"
    result = _run("token-file", "--principal", "gemini", "--path", str(target),
                  env={"PSEUDOLIFE_INSTALLER_TOKENS": "fixture-code:claude-code"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "tokenless"}
    assert not target.exists()


def test_token_file_refuses_an_ambiguous_map(tmp_path):
    target = tmp_path / "claude-code.token"
    result = _run("token-file", "--principal", "claude-code", "--path", str(target),
                  env={"PSEUDOLIFE_INSTALLER_TOKENS": "fixture-one:claude-code,"
                                                      "fixture-two:claude-code"})
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "failed" and "exactly one" in report["recovery"]
    assert "fixture-one" not in result.stdout + result.stderr
    assert not target.exists()


def test_token_file_rotates_a_stale_file(tmp_path):
    target = tmp_path / "claude-code.token"
    _run("token-file", "--principal", "claude-code", "--path", str(target),
         env={"PSEUDOLIFE_INSTALLER_TOKEN": "fixture-old"})
    _run("token-file", "--principal", "claude-code", "--path", str(target),
         env={"PSEUDOLIFE_INSTALLER_TOKEN": "fixture-new"})
    assert CredentialProvider(path=target).snapshot().token == "fixture-new"


# -- registration-env ---------------------------------------------------------

def _claude_config(tmp_path: Path, entry: dict | None) -> Path:
    config = tmp_path / ".claude.json"
    data = {"numStartups": 7, "projects": {"/work": {"allowedTools": []}}}
    if entry is not None:
        data["mcpServers"] = {"pseudolife-memory": entry, "other": {"command": "x"}}
    config.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return config


WANTED = ("--set", "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/claude-code.token",
          "--set", "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765",
          "--set", "PSEUDOLIFE_AGENT_STATE_DIR=/fixture/agents")


def test_claude_code_env_adds_missing_keys_in_place(tmp_path):
    entry = {"type": "stdio", "command": "pseudolife-mcp", "args": [],
             "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                     "PSEUDOLIFE_AGENT_COORDINATION": "0"}}
    config = _claude_config(tmp_path, entry)
    result = _run("registration-env", "--config", str(config), *WANTED)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "updated" and Path(report["backup"]).is_file()
    data = json.loads(config.read_text(encoding="utf-8"))
    env = data["mcpServers"]["pseudolife-memory"]["env"]
    assert env == {**entry["env"],
                   "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/claude-code.token",
                   "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
                   "PSEUDOLIFE_AGENT_STATE_DIR": "/fixture/agents"}
    assert data["numStartups"] == 7 and data["mcpServers"]["other"] == {"command": "x"}
    again = _run("registration-env", "--config", str(config), *WANTED)
    assert json.loads(again.stdout) == {"status": "unchanged", "backup": None}


@pytest.mark.parametrize("credential", [
    {"PSEUDOLIFE_MCP_TOKEN": "fixture-literal"},
    {"PSEUDOLIFE_MCP_TOKEN_FILE": "/elsewhere/own.token"},
])
def test_claude_code_env_never_replaces_a_configured_credential(tmp_path, credential):
    entry = {"command": "pseudolife-mcp", "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1", **credential}}
    config = _claude_config(tmp_path, entry)
    result = _run("registration-env", "--config", str(config), *WANTED)
    env = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["pseudolife-memory"]["env"]
    assert {k: env[k] for k in credential} == credential
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in env or credential.get("PSEUDOLIFE_MCP_TOKEN_FILE")
    assert env["PSEUDOLIFE_AGENT_STATE_DIR"] == "/fixture/agents"
    assert "fixture-literal" not in result.stdout + result.stderr


@pytest.mark.parametrize("entry,status", [
    (None, "absent"),
    ({"type": "http", "url": "http://127.0.0.1:8765/mcp"}, "unsupported"),
])
def test_claude_code_env_leaves_other_shapes_alone(tmp_path, entry, status):
    config = _claude_config(tmp_path, entry)
    before = config.read_bytes()
    result = _run("registration-env", "--config", str(config), *WANTED)
    assert json.loads(result.stdout)["status"] == status
    assert config.read_bytes() == before


# -- claude-settings-env ------------------------------------------------------

def test_claude_settings_env_gives_plugin_hooks_the_token_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"KEEP": "1"}, "hooks": {}}), encoding="utf-8")
    result = _run("claude-settings-env", "--settings", str(settings),
                  "--token-file", "/fixture/claude-code.token",
                  "--daemon-url", "http://127.0.0.1:8765")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "updated" and Path(report["backup"]).is_file()
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"env": {"KEEP": "1",
                            "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/claude-code.token",
                            "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"},
                    "hooks": {}}


def test_claude_settings_env_creates_a_missing_settings_file(tmp_path):
    settings = tmp_path / "claude" / "settings.json"
    result = _run("claude-settings-env", "--settings", str(settings),
                  "--token-file", "/fixture/claude-code.token",
                  "--daemon-url", "http://127.0.0.1:8765")
    assert json.loads(result.stdout) == {"status": "updated", "backup": None}
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "env": {"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/claude-code.token",
                "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}}


@pytest.mark.parametrize("settings_env,process_env", [
    ({"PSEUDOLIFE_MCP_TOKEN": "fixture-literal"}, {}),
    ({"PSEUDOLIFE_MCP_TOKEN_FILE": "/own.token"}, {}),
    ({}, {"PSEUDOLIFE_MCP_TOKEN": "fixture-user-env"}),
    ({}, {"PSEUDOLIFE_MCP_TOKEN_FILE": "/own.token"}),
])
def test_claude_settings_env_keeps_a_credential_the_user_manages(tmp_path, settings_env,
                                                                 process_env):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": settings_env}), encoding="utf-8")
    before = settings.read_bytes()
    result = _run("claude-settings-env", "--settings", str(settings),
                  "--token-file", "/fixture/claude-code.token",
                  "--daemon-url", "http://127.0.0.1:8765", env=process_env)
    assert json.loads(result.stdout) == {"status": "kept", "backup": None}
    assert settings.read_bytes() == before


# -- board --------------------------------------------------------------------

def test_board_line_without_a_token_is_off_and_names_why(tmp_path):
    result = _run("board", "--daemon-url", "http://127.0.0.1:1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("off - no bearer token")


def test_board_line_reads_the_token_file(tmp_path):
    target = tmp_path / "claude-code.token"
    _run("token-file", "--principal", "claude-code", "--path", str(target),
         env={"PSEUDOLIFE_INSTALLER_TOKEN": "fixture-singular"})
    result = _run("board", "--daemon-url", "http://127.0.0.1:1", "--token-file", str(target))
    assert result.stdout.strip() == "off - daemon unreachable"
    assert "fixture-singular" not in result.stdout + result.stderr


# -- review follow-ups --------------------------------------------------------

def test_settings_env_keeps_plugin_hooks_working_beside_a_codex_connection(tmp_path):
    """Codex setup writes a managed connection.json, and lifecycle.ps1 then
    refuses an explicit token file without the matching daemon URL. The
    settings env therefore carries both, and the hook authenticates with the
    Claude Code token."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from pseudolife_memory.credentials import _write_token_file
    from tests.test_codex_hooks import (
        connection_payload, isolated_env, pwsh_run, write_connection)

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    codex_home = tmp_path / "codex"
    codex_token = codex_home / "pseudolife" / "token"
    _write_token_file(codex_token, "codex-fixture")
    write_connection(codex_token.with_name("connection.json"),
                     connection_payload(url, codex_token))
    claude_token = tmp_path / "claude-code.token"
    _write_token_file(claude_token, "claude-fixture")
    settings = tmp_path / "settings.json"
    try:
        result = _run("claude-settings-env", "--settings", str(settings),
                      "--token-file", str(claude_token), "--daemon-url", url)
        assert json.loads(result.stdout)["status"] == "updated", result.stderr
        env = isolated_env(codex_home)
        env.update(json.loads(settings.read_text(encoding="utf-8"))["env"])
        command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
        out = pwsh_run("-Command", command,
                       input='{"session_id":"fixture","source":"startup"}', env=env)
        assert "briefing unavailable" not in out.stdout
        assert seen and set(seen) == {"Bearer claude-fixture"}
    finally:
        server.shutdown()
        server.server_close()


def test_settings_env_never_replaces_a_daemon_url(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"PSEUDOLIFE_MCP_DAEMON_URL": "http://own:1"}}),
                        encoding="utf-8")
    _run("claude-settings-env", "--settings", str(settings),
         "--token-file", "/fixture/claude-code.token", "--daemon-url", "http://127.0.0.1:8765")
    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert env == {"PSEUDOLIFE_MCP_DAEMON_URL": "http://own:1",
                   "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/claude-code.token"}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_writes_go_through_a_symlinked_config(tmp_path):
    """A settings.json kept as a symlink into a dotfiles repo stays a link."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    _run("claude-settings-env", "--settings", str(link),
         "--token-file", "/fixture/claude-code.token", "--daemon-url", "http://127.0.0.1:8765")
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8"))["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]
