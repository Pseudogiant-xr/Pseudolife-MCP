"""The native installer (ops/install-native.*): the tests of test_installer_client_only.py
that the native client path changed, run against the native installer.

test_installer_client_only.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

import pytest

from tests import native_client_stub as native_stub

from pseudolife_memory.credentials import CredentialProvider, _write_token_file
from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env, user_path_unchanged,
)


ROOT = Path(__file__).resolve().parents[1]


BASH = pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)


REMOTE = "http://100.64.0.2:8765"


FIXTURE_TOKEN = "fixture-remote-token-0123456789"


HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'


def _q(value: str) -> str:
    return value.replace("'", "'\\''")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


class _Shell:
    """A disposable home, a stub bin directory, and the selected Bash."""

    def __init__(self, bash: str, tmp_path: Path):
        self.bash = bash
        self.tmp = tmp_path
        self.env = _fixture_env(tmp_path / "env")
        self.home = Path(self.env["HOME"])
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.calls = tmp_path / "calls.txt"
        # Every stub logs its argv; docker must never appear in the log.
        for name in ("docker", "claude", "codex", "gemini"):
            _stub(self.bin / name,
                  f"printf '{name}|%s\\n' \"$*\" >>\"$CALL_LOG\"\nexit 0\n")
        _stub(self.bin / "curl",
              "printf 'curl|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
              "[ \"${FAKE_CURL_EXIT:-0}\" = 0 ] || exit \"$FAKE_CURL_EXIT\"\n"
              "printf '%s' \"$FAKE_HEALTH\"\n")

    def path(self, path: Path | str) -> str:
        return _q(_bash_fixture_path(self.bash, Path(path), self.env))

    def logged(self) -> list[str]:
        return (self.calls.read_text(encoding="utf-8").splitlines()
                if self.calls.exists() else [])

    def run(self, body: str, *, strict: str = "set -u",
            extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        prelude = (
            f"{strict}\n"
            f"HOME='{self.path(self.home)}'\n"
            f"PATH='{self.path(self.bin)}:/usr/bin:/bin'\n"
            f"export PATH CALL_LOG='{self.path(self.calls)}'\n"
            f"repo='{self.path(ROOT)}'\n"
            f"installer_python() {{ printf '%s\\n' '{self.path(sys.executable)}'; }}\n"
            # The side-by-side shim runtime is never really installed here:
            # ops/shim_runtime.py install fails in the shim-install-failed
            # scenario (FAKE_PIPX_EXIT), otherwise the launcher is the
            # installed shim path and nothing migrates.
            "export PSEUDOLIFE_SHIM_PYTHON=fake_runtime_python\n"
            "fake_runtime_python() {\n"
            "    case \"$1\" in *shim_runtime.py)\n"
            "        [ \"${FAKE_PIPX_EXIT:-0}\" = 0 ] || return 1\n"
            "        if [ \"$2\" = migrate ]; then return 3; fi\n"
            "        printf '%s/pseudolife-mcp\\n' \"${FAKE_INSTALL_BIN:-/fixture/installed-bin}\"; return 0 ;;\n"
            "    esac\n"
            "    return 91\n"
            "}\n"
            "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        )
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run([self.bash], input=prelude + body, capture_output=True,
                              text=True, encoding="utf-8", timeout=120, env=env, check=False)


def _token(shell: _Shell) -> Path:
    path = shell.tmp / "keys" / "remote.token"
    path.parent.mkdir()
    _write_token_file(path, FIXTURE_TOKEN)
    return path


@BASH
def test_codex_credentials_come_from_the_token_file_and_the_remote_url(bash, tmp_path):
    """Section 9 hands the Codex credential helper the token on stdin (never
    an argument, never printed) and the remote URL; the local ops/.env is
    not consulted, and the registration carries what the helper set up."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    captured = tmp_path / "helper.txt"
    _stub(shell.bin / "python3",
          "case \"$1\" in\n"
          "  -c) exec \"$FIXTURE_PYTHON\" \"$@\" ;;\n"
          "  *setup-codex-coordination.py)\n"
          "    { printf 'ARGS=%s\\n' \"$*\"; printf 'STDIN='; cat; printf '\\n'; } >\"$CAPTURED\"\n"
          "    printf '{\"status\":\"ready\",\"credential_file_configured\":true,"
          "\"connection_configured\":true,\"credential_file_path\":\"/fixture/codex/token\","
          "\"daemon_url\":\"%s\"}\\n' \"$FIXTURE_URL\" ;;\n"
          "  *setup-codex-hooks.py)\n"
          "    printf '{\"status\":\"ready\",\"source\":\"manual\","
          "\"instructions\":\"covered-by-hooks\",\"recovery\":null}\\n' ;;\n"
          "esac\n")
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-local-token\n"
                        "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765\n", encoding="utf-8")
    stages = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# [^\n]*10\. standing", (ROOT / "ops/install-native.sh").read_text(
                           encoding="utf-8"))[1]
    proc = shell.run(
        f"repo='{shell.path(ROOT)}'\nenv_file='{shell.path(env_file)}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        "CLIENTS=codex CODEX_HOOKS=auto CODEX_HOOK_TRUST=yes INSTRUCTIONS=auto CLAUDE_MD=''\n"
        + _between("ops/install-native.sh", "get_env() {", "\n") + "\n" + native_stub.BASH + "\n" + stages
        + "\nprintf 'FILE=%s\\nURL=%s\\n' \"$CODEX_CREDENTIAL_FILE\" \"$CODEX_CREDENTIAL_URL\"\n",
        strict="set -euo pipefail",
        extra_env={"CAPTURED": str(captured), "FIXTURE_URL": REMOTE,
                   "FAKE_LAUNCHER": _bash_fixture_path(bash, tmp_path / "native" / "pseudolife-mcp", shell.env),
                   "FIXTURE_PYTHON": _bash_fixture_path(bash, Path(sys.executable),
                                                        shell.env)})
    assert proc.returncode == 0, proc.stderr
    helper = captured.read_text(encoding="utf-8")
    assert "--installer-token-stdin" in helper
    assert f"--installer-daemon-url {REMOTE}" in helper
    assert f"STDIN={FIXTURE_TOKEN}" in helper
    assert "fixture-local-token" not in helper
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr
    assert f"URL={REMOTE}\n" in proc.stdout
    assert "docker exec" not in proc.stdout


CRED_READY = ('{"status":"ready","credential_file_configured":true,"connection_configured":true,'
              '"credential_file_path":"/fixture/codex/token","daemon_url":"%s"}')


def _codex_wiring(shell: _Shell, *, python_ok: bool = True, cred: str | None = None,
                  cred_exit: int = 0, pipx_exit: int = 0, existing: str = ""):
    """Sections 9 to 11 for a Codex-only client-only install, with stub
    python, the native client (``pipx_exit`` is its install result) and
    codex. Returns the run and the codex calls."""
    token = _token(shell)
    installed = shell.tmp / "installed-bin"
    installed.mkdir()
    _stub(installed / "pseudolife-mcp", "exit 0\n")
    for name in ("python3", "python"):
        _stub(shell.bin / name,
              "case \"$1\" in\n"
              "  -c) [ \"$FAKE_PY_OK\" = 1 ] || exit 1; exec \"$FIXTURE_PYTHON\" \"$@\" ;;\n"
              "  *setup-codex-coordination.py)\n"
              "    if [ \"$2\" = --runtime-defaults ]; then\n"
              "      printf '{\"status\":\"ready\",\"runtime_defaults\":\"configured\"}\\n'; exit 0; fi\n"
              "    cat >/dev/null; printf '%s\\n' \"$FAKE_CRED\"; exit \"$FAKE_CRED_EXIT\" ;;\n"
              "  *setup-codex-hooks.py)\n"
              "    printf '{\"status\":\"ready\",\"source\":\"manual\","
              "\"instructions\":\"covered-by-hooks\",\"recovery\":null}\\n' ;;\n"
              "esac\n")
    _stub(shell.bin / "codex",
          "printf 'codex|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then\n"
          "  [ -n \"$FAKE_EXISTING\" ] || exit 1; printf '%s\\n' \"$FAKE_EXISTING\"; exit 0; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  --env <KEY=VALUE>'; exit 0; fi\n"
          "exit 0\n")
    source = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    stages = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# [^\n]*12\. health", source)[1]
    proc = shell.run(
        f"env_file='{shell.path(shell.tmp / 'absent.env')}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        "CLIENTS=codex CODEX_HOOKS=auto CODEX_HOOK_TRUST=yes INSTRUCTIONS=auto CLAUDE_MD=''\n"
        "AGENTS_FILE='' TRANSPORT=shim\n"
        + _between("ops/install-native.sh", "get_env() {", "\n") + "\n" + native_stub.BASH + "\n" + stages
        + "\nprintf 'MCP_CODEX=%s\\n' \"$MCP_CODEX\"\n",
        strict="set -euo pipefail",
        extra_env={"FAKE_PY_OK": "1" if python_ok else "0",
                   "FIXTURE_PYTHON": _bash_fixture_path(shell.bash, Path(sys.executable),
                                                        shell.env),
                   "FAKE_CRED": cred if cred is not None else CRED_READY % REMOTE,
                   "FAKE_CRED_EXIT": str(cred_exit), "FAKE_NATIVE_EXIT": str(pipx_exit),
                   "FAKE_EXISTING": existing,
                   "FAKE_LAUNCHER": _bash_fixture_path(shell.bash, installed / "pseudolife-mcp", shell.env),
                   "FAKE_KEEP_LAUNCHER": "1"})
    adds = [call for call in shell.logged()
            if call.startswith("codex|mcp add") and "--help" not in call]
    return proc, adds


@BASH
def test_codex_registers_with_the_remote_url_and_its_credential_file(bash, tmp_path):
    proc, adds = _codex_wiring(_Shell(bash, tmp_path))
    assert proc.returncode == 0, proc.stderr
    [add] = adds
    assert f"PSEUDOLIFE_MCP_DAEMON_URL={REMOTE}" in add
    assert "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/codex/token" in add
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in add
    assert "MCP_CODEX=shim-env" in proc.stdout


@pytest.mark.parametrize("scenario,kwargs,says", [
    ("no python", {"python_ok": False}, "Python 3.10"),
    ("credential setup failed",
     {"cred": '{"status":"needs-configuration","recovery":"fixture"}', "cred_exit": 1},
     "credential setup failed"),
    ("shim install failed", {"pipx_exit": 1}, "stdio shim"),
    ("existing registration elsewhere",
     {"cred": CRED_READY % "http://127.0.0.1:8765",
      "existing": "pseudolife-memory\n  command: pseudolife-mcp\n"
                  "  env: PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765"},
     f"not the daemon at {REMOTE}"),
])
@BASH
def test_codex_is_never_left_on_another_daemon(bash, tmp_path, scenario, kwargs, says):
    proc, adds = _codex_wiring(_Shell(bash, tmp_path), **kwargs)
    assert proc.returncode == 0, proc.stderr
    assert adds == [], scenario
    assert "MCP_CODEX=failed" in proc.stdout, scenario
    assert says in proc.stdout + proc.stderr, scenario


class _Board(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        _Board.seen.append(f"{self.path}|{self.headers.get('Authorization', '')}")
        self.send_response(200)
        self.send_header("X-PL-Board", "on")
        self.end_headers()
        self.wfile.write(b"check-in")

    def log_message(self, *args):  # silence the test log
        pass


@pytest.fixture
def board_server():
    _Board.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _copy_repo(tmp_path: Path) -> Path:
    """The files a client-only run reads, in a scratch checkout: a bug that
    wrote ops/.env or the compose override must not touch the real tree."""
    repo = tmp_path / "repo"
    for rel in ("ops/install-native.sh", "ops/install-hook.sh", "ops/client_credentials.py",
                "ops/.env.example", "examples/CLAUDE.memory.md", "pyproject.toml"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    (repo / "ops" / "install-native.sh").chmod(0o755)
    (repo / "ops" / "install-hook.sh").chmod(0o755)
    return repo


FRESH_CONNECT = '{"url": null, "rows": [], "exit": 3, "error": "none"}'


def _whole_client_only_run(bash, tmp_path, board_server, *, connect_report: str = FRESH_CONNECT, connect_exit: int = 3):
    """The whole client-only run; the staged client answers connect's dry run with
    ``connect_report`` and exit ``connect_exit`` (a fresh machine's: no registration, 3)."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    repo = _copy_repo(tmp_path)
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    release_env = native_stub.write_release(
        tmp_path / "release", version,
        "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
        "if [ \"$1\" = connect ]; then\n"
        f"  echo '{connect_report}'; exit {connect_exit}\nfi\n"
        "exit 0\n")
    release_env["PSEUDOLIFE_NATIVE_ARCHIVE_URL"] = shell.path(tmp_path / "release")
    python = shell.path(sys.executable)
    for name in ("python3", "python"):
        _stub(shell.bin / name, f"exec '{python}' \"$@\"\n")
    _stub(shell.bin / "claude",
          "printf 'claude|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then exit 1; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  -e, --env <env...>'; fi\n"
          "exit 0\n")
    _stub(shell.bin / "gemini",
          "printf 'gemini|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  -e, --env <env...>'; fi\n"
          "exit 0\n")
    # An earlier local install left its own file and loopback URL for the hooks.
    settings_path = shell.home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {
        "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/local/claude-code.token",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}}), encoding="utf-8")
    with user_path_unchanged(shell.home):
        proc = shell.run(
            f"exec '{shell.path(repo / 'ops' / 'install-native.sh')}' --client-only "
            f"--daemon-url '{board_server}' --token-file '{shell.path(token)}' "
            "--client claude,gemini --claude-plugin skip --instructions skip --no-art\n",
            extra_env={"FAKE_HEALTH": HEALTH_ON,
                       **release_env,
                       "PSEUDOLIFE_SHIM_RUNTIMES": shell.path(installed / "runtimes"),
                       "PSEUDOLIFE_SHIM_LAUNCHER": shell.path(installed / "pseudolife-mcp"),
                       "PYTHONPATH": str(ROOT),
                       # The installer's own environment carries an older credential.
                       "PSEUDOLIFE_MCP_TOKEN": "fixture-local-literal"})
    return installed, proc, repo, shell, token


@BASH
def test_a_client_only_install_wires_clients_to_the_remote_daemon(bash, tmp_path,
                                                                  board_server):
    installed, proc, repo, shell, token = _whole_client_only_run(bash, tmp_path, board_server)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = shell.logged()
    assert not any(call.startswith("docker|") for call in calls)
    assert not (repo / "ops" / ".env").exists()
    assert not (repo / "ops" / "docker-compose.override.yml").exists()
    output = proc.stdout + proc.stderr
    assert FIXTURE_TOKEN not in output
    token_path = shell.path(token)
    [claude] = [call for call in calls if call.startswith("claude|mcp add --scope user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=claude-code", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token_path}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={board_server}"):
        assert f" {pair} " in claude, pair
    [gemini] = [call for call in calls if call.startswith("gemini|mcp add -s user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=gemini", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token_path}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={board_server}"):
        assert f"-e {pair} " in gemini, pair
    # The Claude Code hooks run the host shim, which reads the daemon's
    # address and the token file from settings.json.
    settings = json.loads((shell.home / ".claude" / "settings.json").read_text(
        encoding="utf-8"))
    commands = json.dumps(settings.get("hooks", {}))
    # The installed shim by its path: a hook's PATH need not include pipx's.
    # (Git Bash hands the path to Python in its Windows spelling.)
    briefings = [hook["command"] for group in settings["hooks"]["SessionStart"]
                 for hook in group["hooks"]
                 if hook["command"].endswith(" briefing --hook-json")]
    assert [Path(command.rsplit(" briefing", 1)[0]) for command in briefings] == [
        installed / "pseudolife-mcp"]
    assert "docker exec" not in commands
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == board_server
    assert Path(settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token
    assert "fixture-local-literal" not in output
    assert list((shell.home / ".claude").glob("settings.json.bak-pseudolife-*"))
    # The summary says where the daemon is, and the board was asked with
    # the operator's token.
    assert f"remote at {board_server}" in proc.stdout
    assert re.search(r"\[x\] Agent board\s+on", proc.stdout)
    assert any(seen.endswith(f"Bearer {FIXTURE_TOKEN}") for seen in _Board.seen)
    assert "Waiting for the daemon" not in proc.stdout
    # A fresh machine: connect's dry run found nothing, so it was not run again.
    connects = [call for call in calls if call.startswith("shim|connect")]
    assert len(connects) == 1 and "--dry-run" in connects[0]
    assert "not checked" not in output


@BASH
def test_an_exit_after_the_native_client_is_installed_says_the_run_stopped_part_way(bash, tmp_path, board_server):
    # connect's dry run refuses after the native client is installed: that exit
    # must not claim that nothing changed, and says what this run already did.
    proc, *_ = _whole_client_only_run(bash, tmp_path, board_server, connect_report='{"url": null, "rows": [], "exit": 1, "error": "the fixture refuses"}', connect_exit=1)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "could not check this machine's existing registrations" in output
    assert "Nothing was changed" not in output and "nothing was changed" not in output
    assert "stopped part way" in output and "the native client" in output and "Rerun the installer" in output
