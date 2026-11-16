"""The native installer (ops/install-native.*): the tests of test_installer_board_token.py
that the native client path changed, run against the native installer.

test_installer_board_token.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from tests import native_client_stub as native_stub

from pseudolife_memory.credentials import CredentialProvider
from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env,
)


ROOT = Path(__file__).resolve().parents[1]


BASH = pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)


def _q(value: str) -> str:
    return value.replace("'", "'\\''")


class _Shell:
    """A disposable home plus the path spellings the selected Bash reads."""

    def __init__(self, bash: str, tmp_path: Path):
        self.bash = bash
        self.tmp = tmp_path
        self.env = _fixture_env(tmp_path / "env")
        self.home = Path(self.env["HOME"])

    def path(self, path: Path | str) -> str:
        return _q(_bash_fixture_path(self.bash, Path(path), self.env))

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        prelude = (
            "set -u\n"
            f"HOME='{self.path(self.home)}'\n"
            f"repo='{self.path(ROOT)}'\n"
            f"installer_python() {{ printf '%s\\n' '{self.path(sys.executable)}'; }}\n"
            "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        )
        return subprocess.run([self.bash], input=prelude + body, capture_output=True,
                              text=True, timeout=120, env=self.env, check=False)


def _wire(shell: _Shell, client: str, *, token_file: str, existing: str | None = None,
          claude_json: Path | None = None, state_dir: str = "/fixture/claude-code-agents",
          migrate_exit: int | None = None,
          ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the client-wiring loop with fake claude/gemini/pipx CLIs."""
    fake_bin = shell.tmp / "bin"
    fake_bin.mkdir()
    installed_bin = shell.tmp / "installed-bin"
    installed_bin.mkdir()
    (installed_bin / "pseudolife-mcp").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (installed_bin / "pseudolife-mcp").chmod(0o755)
    call_log = shell.tmp / "calls.txt"
    for name, query in (("claude", "get"), ("gemini", "list")):
        (fake_bin / name).write_text(
            "#!/bin/sh\n"
            "printf '%s|%s\\n' \"$(basename \"$0\")\" \"$*\" >>\"$CALL_LOG\"\n"
            "if [ \"$1\" = mcp ] && [ \"$2\" = add ] && [ \"$3\" = --help ]; then\n"
            "  printf '  -e, --env <env...>\\n'; exit 0\nfi\n"
            f"if [ \"$1\" = mcp ] && [ \"$2\" = {query} ]; then\n"
            "  if [ -n \"$FAKE_CONFIG\" ]; then printf '%s\\n' \"$FAKE_CONFIG\"; exit 0; fi\n"
            "  exit 1\nfi\n"
            "if [ \"$1\" = mcp ] && [ \"$2\" = add ]; then exit 0; fi\n"
            "exit 91\n", encoding="utf-8")
        (fake_bin / name).chmod(0o755)
    helper = _between("ops/install-native.sh", "resolve_installed_shim() {",
                      "\n}\n\n# Two env pairs") + "\n}"
    loop = _between("ops/install-native.sh",
                    'for selected_client in $CLIENTS; do\n    if [ "$selected_client" = claude-desktop ]; then',
                    "\ndone\n\n# ── 12. health") + "\ndone"
    config = ("pseudolife-memory\n" + existing) if existing else ""
    body = f"""PATH='{shell.path(installed_bin)}:{shell.path(fake_bin)}:/usr/bin:/bin'
export PATH CALL_LOG='{shell.path(call_log)}' FAKE_CONFIG='{_q(config)}'
export FAKE_LAUNCHER='{shell.path(installed_bin / "pseudolife-mcp")}' {"FAKE_KEEP_LAUNCHER=1" if migrate_exit is None else f"FAKE_MIGRATE_EXIT={migrate_exit}"}
CLIENTS='{client}'
TRANSPORT=shim
CODEX_CREDENTIAL_BOOTSTRAP_FAILED='' CODEX_CONNECTION_CONFIGURED='' CODEX_CREDENTIAL_FILE=''
CODEX_CREDENTIAL_URL='' CODEX_RUNTIME_DEFAULTS=''
MCP_CODEX='' MCP_CLAUDE='' MCP_GEMINI=''
SHIM_TRIED='' SHIM_OK='' SHIM_PATH='{shell.path(installed_bin / "pseudolife-mcp")}'
CLAUDE_TOKEN_FILE='{_q(token_file)}'
GEMINI_TOKEN_FILE='{_q(token_file.replace("claude-code", "gemini") if token_file else "")}'
CLIENT_DAEMON_URL='http://127.0.0.1:8765'
CLAUDE_AGENT_STATE_DIR='{_q(state_dir)}'
CLAUDE_JSON='{shell.path(claude_json) if claude_json else ""}'
configure_codex_runtime_defaults() {{ CODEX_RUNTIME_DEFAULTS=preserved; }}
{helper}
{native_stub.BASH}
{loop}
printf 'STATE=%s/%s\\n' "$MCP_CLAUDE" "$MCP_GEMINI"
"""
    proc = shell.run(body)
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return proc, calls


def _adds(calls: list[str]) -> list[str]:
    return [call for call in calls if "|mcp add" in call and "--help" not in call]


@BASH
def test_fresh_claude_code_registration_carries_the_board_credential(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc, calls = _wire(shell, "claude", token_file="/fixture/claude-code.token")
    assert proc.returncode == 0, proc.stderr
    [add] = _adds(calls)
    assert add.startswith("claude|mcp add --scope user pseudolife-memory --env ")
    for pair in ("PSEUDOLIFE_WRITER_ID=claude-code", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/claude-code.token",
                 "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765",
                 "PSEUDOLIFE_AGENT_STATE_DIR=/fixture/claude-code-agents"):
        assert f" {pair} " in add
    assert "STATE=shim-env/" in proc.stdout


@BASH
def test_tokenless_claude_code_registration_is_unchanged(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc, calls = _wire(shell, "claude", token_file="")
    assert proc.returncode == 0, proc.stderr
    [add] = _adds(calls)
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in add
    assert "PSEUDOLIFE_AGENT_STATE_DIR" not in add


@BASH
def test_fresh_gemini_registration_carries_the_token_file(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc, calls = _wire(shell, "gemini", token_file="/fixture/claude-code.token")
    assert proc.returncode == 0, proc.stderr
    [add] = _adds(calls)
    assert "-e PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/gemini.token " in add
    assert "-e PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765 " in add
    assert "-e PSEUDOLIFE_MCP_NO_SPAWN=1 " in add


@BASH
def test_existing_claude_code_registration_gains_the_credential_in_place(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    claude_json = shell.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": "pseudolife-mcp", "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    existing = "Type: stdio\nCommand: pseudolife-mcp\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN=1"
    # Real paths: Git Bash converts path arguments for the native Python,
    # as a real install needs, so the helper records the host spelling.
    token_file = shell.home / ".pseudolife-mcp" / "claude-code.token"
    state_dir = shell.home / ".pseudolife-mcp" / "claude-code-agents"
    proc, calls = _wire(shell, "claude", token_file=shell.path(token_file),
                        existing=existing, claude_json=claude_json,
                        state_dir=shell.path(state_dir))
    assert proc.returncode == 0, proc.stderr
    assert _adds(calls) == []
    assert not any("mcp remove" in call for call in calls)
    assert "STATE=present-upgraded/" in proc.stdout
    env = json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-memory"]["env"]
    assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token_file
    assert Path(env["PSEUDOLIFE_AGENT_STATE_DIR"]) == state_dir
    assert env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
    assert "restart" in proc.stdout.lower()


@BASH
def test_a_registration_already_on_the_native_launcher_still_gains_the_credential(bash, tmp_path):
    """The native step reports a registration that already runs the launcher
    as current; the credential repair must still run for it (Codex review,
    2026-11-14)."""
    shell = _Shell(bash, tmp_path)
    claude_json = shell.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": "/opt/tools/pseudolife-mcp", "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    existing = "Type: stdio\nCommand: /opt/tools/pseudolife-mcp\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN=1"
    token_file = shell.home / ".pseudolife-mcp" / "claude-code.token"
    state_dir = shell.home / ".pseudolife-mcp" / "claude-code-agents"
    proc, calls = _wire(shell, "claude", token_file=shell.path(token_file),
                        existing=existing, claude_json=claude_json,
                        state_dir=shell.path(state_dir), migrate_exit=4)
    assert proc.returncode == 0, proc.stderr
    assert "STATE=present-current/" in proc.stdout
    assert "registration preserved; it already runs the native client" in proc.stdout
    env = json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-memory"]["env"]
    assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token_file


@BASH
def test_existing_gemini_registration_gets_the_manual_fix(bash, tmp_path):
    """`gemini mcp list` shows no env, so the installer cannot tell whether
    the registration carries a token (maintainer decision 2026-09-28: warn,
    never edit it)."""
    shell = _Shell(bash, tmp_path)
    settings = shell.home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"mcpServers": {}}', encoding="utf-8")
    proc, calls = _wire(shell, "gemini", token_file="/fixture/claude-code.token",
                        existing="pseudolife-memory: pseudolife-mcp (stdio) - Connected")
    assert proc.returncode == 0, proc.stderr
    assert _adds(calls) == []
    assert settings.read_text(encoding="utf-8") == '{"mcpServers": {}}'
    assert "~/.gemini/settings.json" in proc.stderr
    assert "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/gemini.token" in proc.stderr


@BASH
def test_existing_http_registration_is_warned_about_not_broken_silently(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc, calls = _wire(shell, "claude", token_file="/fixture/claude-code.token",
                        existing="Type: http\nURL: http://127.0.0.1:8765/mcp")
    assert proc.returncode == 0, proc.stderr
    assert _adds(calls) == []
    assert "requires a bearer token" in proc.stderr
    assert "register the stdio shim instead" in proc.stderr
