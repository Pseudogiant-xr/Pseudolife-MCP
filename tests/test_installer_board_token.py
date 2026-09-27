"""Executable contracts for the installers' board token.

A default install mints a bearer token so the agent board is on, and every
client the installer wires then carries it: the Claude Code and Gemini shim
registrations get a token file (plus, for Claude Code, a resumable board
address), an existing Claude Code registration gains them in place, and the
plugin's hooks get the file through ~/.claude/settings.json. Each block runs
extracted from ops/install.sh against fake CLIs in a disposable home; the
real ops/client_credentials.py does the file work.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider
from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env,
)


ROOT = Path(__file__).resolve().parents[1]
BASH = pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


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


# -- minting ------------------------------------------------------------------

def _mint(shell: _Shell, env_file: Path, *, no_token: str = "", transport: str = "shim",
          extra: str = ""):
    return shell.run(
        extra + f"env_file='{shell.path(env_file)}'\nNO_TOKEN='{no_token}'\n"
        f"TRANSPORT='{transport}'\n"
        + _block("mint token") + "\nprintf 'TOKEN_STATE=%s\\n' \"$TOKEN_STATE\"\n")


def _minted(env_file: Path) -> list[str]:
    return re.findall(r"(?m)^PSEUDOLIFE_MCP_TOKEN=(\S+)\r?$",
                      env_file.read_text(encoding="utf-8"))


@BASH
def test_default_install_mints_a_token_and_never_prints_it(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(shell, env_file)
    assert proc.returncode == 0, proc.stderr
    [token] = _minted(env_file)
    assert "TOKEN_STATE=minted" in proc.stdout
    assert token not in proc.stdout + proc.stderr
    again = _mint(shell, env_file)
    assert "TOKEN_STATE=present" in again.stdout
    assert _minted(env_file) == [token]


@pytest.mark.parametrize("no_token,transport,state", [
    ("1", "shim", "opted-out"),
    ("", "http", "http"),
])
@BASH
def test_open_loopback_installs_mint_nothing(bash, tmp_path, no_token, transport, state):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(shell, env_file, no_token=no_token, transport=transport)
    assert proc.returncode == 0, proc.stderr
    assert f"TOKEN_STATE={state}" in proc.stdout
    assert _minted(env_file) == []


@pytest.mark.parametrize("no_token,transport,extra", [
    ("", "http", ""),
    ("", "shim", "installer_python() { :; }\n"),
    ("1", "shim", ""),
])
@BASH
def test_an_existing_token_is_reported_present_whatever_else_holds(bash, tmp_path, no_token,
                                                                   transport, extra):
    """The daemon is gated by a token already in ops/.env whether or not
    this run could have minted one, so the HTTP-registration warning and
    the board hint must see it (final review, 2026-09-28)."""
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-existing\n", encoding="utf-8")
    getter = _between("ops/install.sh", "get_env() {", "\n") + "\n"
    proc = _mint(shell, env_file, no_token=no_token, transport=transport,
                 extra=getter + extra)
    assert proc.returncode == 0, proc.stderr
    assert "TOKEN_STATE=present" in proc.stdout


@BASH
def test_http_registrations_against_a_gated_daemon_are_warned_about(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(
        "CLAUDE_TOKEN_FILE='' GEMINI_TOKEN_FILE='' CODEX_CREDENTIAL_FILE=''\n"
        "CLIENT_DAEMON_URL='http://127.0.0.1:1' TOKEN_STATE=present\n"
        "MCP_CLAUDE=http MCP_GEMINI='' MCP_CODEX=''\n"
        "desktop_token_source() { printf 'fixture-existing\\n'; }\n" + _block("board line"))
    assert proc.returncode == 0, proc.stderr
    assert "HTTP registration sends no bearer token" in proc.stderr
    assert "fixture-existing" not in proc.stdout + proc.stderr


@BASH
def test_a_host_that_cannot_install_the_shim_mints_nothing(bash, tmp_path):
    """Without a shim the registrations fall back to HTTP, which cannot
    carry a token file: minting there would lock memory out."""
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(shell, env_file, extra="installer_python() { :; }\n")
    assert proc.returncode == 0, proc.stderr
    assert "TOKEN_STATE=no-shim" in proc.stdout
    assert _minted(env_file) == []


def test_installers_document_the_no_token_opt_out():
    sh = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    assert "--no-token) NO_TOKEN=1; shift ;;" in sh
    assert re.search(r"#\s+--no-token\s", sh)
    assert "[switch]$NoToken" in ps
    assert re.search(r"-NoToken\s", ps)


# -- client token files -------------------------------------------------------

def _client_files(shell: _Shell, env_file: Path, clients: str, *, hook: str = "",
                  process_env: str = "") -> subprocess.CompletedProcess[str]:
    getters = (_between("ops/install.sh", "get_env() {", "\n")
               + "\n" + _between("ops/install.sh", "desktop_token_source() {",
                                 "\nconfigure_codex_runtime_defaults()"))
    return shell.run(
        process_env
        + f"env_file='{shell.path(env_file)}'\nCLIENTS='{clients}'\nTRANSPORT=shim\n"
        f"HOOK_CLAUDE='{hook}'\n" + getters + "\n" + _block("client token files")
        + "\nprintf 'CLAUDE=%s\\nGEMINI=%s\\n' \"$CLAUDE_TOKEN_FILE\" \"$GEMINI_TOKEN_FILE\"\n")


@BASH
def test_claude_and_gemini_get_owner_only_token_files(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    proc = _client_files(shell, env_file, "claude gemini")
    assert proc.returncode == 0, proc.stderr
    assert "fixture-singular" not in proc.stdout + proc.stderr
    for principal in ("claude-code", "gemini"):
        target = shell.home / ".pseudolife-mcp" / f"{principal}.token"
        assert CredentialProvider(path=target).snapshot().token == "fixture-singular"
    assert re.search(r"(?m)^CLAUDE=.*/\.pseudolife-mcp/claude-code\.token$", proc.stdout)
    assert re.search(r"(?m)^GEMINI=.*/\.pseudolife-mcp/gemini\.token$", proc.stdout)


@BASH
def test_tokenless_install_writes_no_client_files(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _client_files(shell, env_file, "claude gemini", hook="plugin")
    assert proc.returncode == 0, proc.stderr
    assert "CLAUDE=\nGEMINI=\n" in proc.stdout
    assert not (shell.home / ".pseudolife-mcp").exists()
    assert not (shell.home / ".claude" / "settings.json").exists()


@BASH
def test_plugin_hooks_get_the_token_file_through_claude_settings(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    settings = shell.home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"enabledPlugins": {"x": True}}), encoding="utf-8")
    proc = _client_files(shell, env_file, "claude", hook="plugin")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == {"x": True}
    assert data["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"].endswith("claude-code.token")


@BASH
def test_plugin_hooks_keep_a_token_the_user_environment_supplies(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    proc = _client_files(shell, env_file, "claude", hook="plugin",
                         process_env="export PSEUDOLIFE_MCP_TOKEN=fixture-singular\n")
    assert proc.returncode == 0, proc.stderr
    assert not (shell.home / ".claude" / "settings.json").exists()


# -- registrations ------------------------------------------------------------

def _wire(shell: _Shell, client: str, *, token_file: str, existing: str | None = None,
          claude_json: Path | None = None, state_dir: str = "/fixture/claude-code-agents",
          ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the client-wiring loop with fake claude/gemini/pipx CLIs."""
    fake_bin = shell.tmp / "bin"
    fake_bin.mkdir()
    installed_bin = shell.tmp / "installed-bin"
    installed_bin.mkdir()
    (installed_bin / "pseudolife-mcp").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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
    (fake_bin / "pipx").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = environment ]; then printf '%s\\n' \"$FAKE_INSTALL_BIN\"; fi\n"
        "exit 0\n", encoding="utf-8")
    helper = _between("ops/install.sh", "resolve_installed_shim() {",
                      "\n}\n\n# Two env pairs") + "\n}"
    loop = _between("ops/install.sh",
                    'for selected_client in $CLIENTS; do\n    if [ "$selected_client" = claude-desktop ]; then',
                    "\ndone\n\n# ── 12. health") + "\ndone"
    config = ("pseudolife-memory\n" + existing) if existing else ""
    body = f"""PATH='{shell.path(installed_bin)}:{shell.path(fake_bin)}:/usr/bin:/bin'
export PATH CALL_LOG='{shell.path(call_log)}' FAKE_CONFIG='{_q(config)}'
export FAKE_INSTALL_BIN='{shell.path(installed_bin)}'
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
shim_process_table() {{ return 2; }}
{helper}
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


# -- the board line -----------------------------------------------------------

@BASH
def test_final_ladder_prints_one_board_line(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(
        "CLAUDE_TOKEN_FILE='' GEMINI_TOKEN_FILE='' CODEX_CREDENTIAL_FILE=''\n"
        "CLIENT_DAEMON_URL='http://127.0.0.1:1' TOKEN_STATE=opted-out\n"
        "desktop_token_source() { :; }\n" + _block("board line"))
    assert proc.returncode == 0, proc.stderr
    [line] = [line for line in proc.stdout.splitlines() if "Agent board" in line]
    assert re.fullmatch(r"\s*\[!\] Agent board\s+off - no bearer token.*--no-token.*", line)
