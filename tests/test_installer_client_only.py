"""Executable contracts for ops/install.sh's client-only mode.

A machine with no daemon of its own is wired to a daemon running elsewhere
(typically over a tailnet): ``--client-only --daemon-url <url> --token-file
<path>``, or a ``--daemon-url`` whose host is not loopback. Such an install
never touches Docker, volumes, ops/.env or a bearer token of its own; it
checks the remote daemon's /health, and registers every client with the
remote URL, the operator's token file and the no-spawn guard. Blocks run
extracted from ops/install.sh with stub CLIs in a disposable home, and one
test runs the whole script against a local stand-in for the daemon.
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

from pseudolife_memory.credentials import _write_token_file
from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env,
)


ROOT = Path(__file__).resolve().parents[1]
BASH = pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
REMOTE = "http://100.64.0.2:8765"
FIXTURE_TOKEN = "fixture-remote-token-0123456789"
HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'
HEALTH_OPEN = '{"status": "ok", "version": "0.0.0", "auth": false}'


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install.sh has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


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
            "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        )
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run([self.bash], input=prelude + body, capture_output=True,
                              text=True, timeout=120, env=env, check=False)


# -- flags and what they imply --------------------------------------------------

def _mode(shell: _Shell, *, daemon_url: str = "", client_only: str = "",
          token_file: str = "", extractor: str = "", model: str = "",
          shim_port: str = "0", no_token: str = "", transport: str = "shim",
          extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return shell.run(
        f"DAEMON_URL='{_q(daemon_url)}'\nCLIENT_ONLY='{client_only}'\n"
        f"TOKEN_FILE='{_q(token_file)}'\nEXTRACTOR='{extractor}'\nMODEL='{model}'\n"
        f"SHIM_PORT='{shim_port}'\nNO_TOKEN='{no_token}'\nTRANSPORT='{transport}'\n"
        + _block("client-only mode")
        + "\nprintf 'CLIENT_ONLY=%s\\nDAEMON_URL=%s\\n' \"$CLIENT_ONLY\" \"$DAEMON_URL\"\n",
        extra_env=extra_env)


@pytest.mark.parametrize("url", [
    REMOTE, "https://pl.example.invalid", "http://pl.example.invalid:8765/"])
@BASH
def test_a_daemon_url_off_this_machine_implies_client_only(bash, tmp_path, url):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url)
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=1\n" in proc.stdout
    assert f"DAEMON_URL={url.rstrip('/')}\n" in proc.stdout


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765", "http://localhost:8765/", "http://LOCALHOST:8765",
    "http://[::1]:8765"])
@BASH
def test_a_loopback_daemon_url_keeps_the_local_install(bash, tmp_path, url):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url)
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=\n" in proc.stdout


@BASH
def test_the_environment_url_is_honoured_when_the_flag_is_absent(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path),
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=1\n" in proc.stdout
    assert f"DAEMON_URL={REMOTE}\n" in proc.stdout


@BASH
def test_the_flag_wins_over_the_environment_url(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url="http://127.0.0.1:8765",
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=\nDAEMON_URL=http://127.0.0.1:8765\n" in proc.stdout


@BASH
def test_an_explicit_client_only_install_may_use_a_loopback_tunnel(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), client_only="1",
                 daemon_url="http://127.0.0.1:18765")
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=1\n" in proc.stdout


@BASH
def test_client_only_requires_a_daemon_url(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), client_only="1")
    assert proc.returncode == 2
    assert "--client-only needs" in proc.stderr
    assert "--daemon-url" in proc.stderr


@BASH
def test_a_malformed_daemon_url_is_refused(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url="pl.example.invalid:8765")
    assert proc.returncode == 2
    assert "invalid daemon URL" in proc.stderr


@pytest.mark.parametrize("flag,kwargs", [
    ("--extractor", {"extractor": "sidecar"}),
    ("--model", {"model": "claude-opus-5"}),
    ("--shim-port", {"shim_port": "8082"}),
    ("--no-token", {"no_token": "1"}),
    ("--transport http", {"transport": "http"}),
])
@BASH
def test_client_only_refuses_the_flags_of_a_local_daemon(bash, tmp_path, flag, kwargs):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=REMOTE, **kwargs)
    assert proc.returncode == 2
    assert flag in proc.stderr


@BASH
def test_a_token_file_flag_needs_client_only(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), token_file="/fixture/remote.token")
    assert proc.returncode == 2
    assert "--token-file" in proc.stderr


@BASH
def test_the_script_itself_refuses_client_only_without_a_url(bash, tmp_path):
    """Refused while parsing arguments: nothing else has run yet."""
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install.sh')}' --client-only --no-art\n")
    assert proc.returncode == 2
    assert "--client-only needs" in proc.stderr
    assert shell.logged() == []


def test_the_new_flags_are_parsed_and_documented():
    sh = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    assert '--daemon-url) DAEMON_URL="$2"; shift 2 ;;' in sh
    assert '--token-file) TOKEN_FILE="$2"; shift 2 ;;' in sh
    assert "--client-only) CLIENT_ONLY=1; shift ;;" in sh
    usage = sh.split("# >>> usage >>>", 1)[1].split("# <<< usage <<<", 1)[0]
    for flag in ("--daemon-url", "--token-file", "--client-only"):
        assert re.search(rf"(?m)^#\s+.*{flag}\b", usage), flag


# -- preflight: the token file and the remote daemon ----------------------------

def _preflight(shell: _Shell, *, daemon_url: str = REMOTE, token_file: str = "",
               health: str = HEALTH_ON, curl_exit: int = 0, clients: str = "claude",
               extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {"FAKE_HEALTH": health, "FAKE_CURL_EXIT": str(curl_exit)}
    env.update(extra_env or {})
    return shell.run(
        f"DAEMON_URL='{_q(daemon_url)}'\nCLIENT_ONLY=1\nTOKEN_FILE='{_q(token_file)}'\n"
        f"CLIENTS='{clients}'\nEXTRACTOR='' MODEL='' SHIM_PORT=0 NO_TOKEN='' TRANSPORT=shim\n"
        + _block("client-only mode") + _block("client-only preflight")
        + "\nclient_only_preflight\nprintf 'TOKEN_FILE=%s\\n' \"$TOKEN_FILE\"\n",
        extra_env=env)


def _token(shell: _Shell) -> Path:
    path = shell.tmp / "keys" / "remote.token"
    path.parent.mkdir()
    _write_token_file(path, FIXTURE_TOKEN)
    return path


@BASH
def test_client_only_never_mints_a_token_it_needs_one_given(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell)
    assert proc.returncode == 2
    assert "--token-file" in proc.stderr
    assert "never mints" in proc.stderr
    assert not any(call.startswith("curl|") for call in shell.logged())


@BASH
def test_a_missing_token_file_is_refused(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    missing = shell.path(tmp_path / "absent.token")
    proc = _preflight(shell, token_file=missing)
    assert proc.returncode == 1
    assert "absent.token" in proc.stderr
    assert "missing or empty" in proc.stderr


@BASH
def test_the_token_file_may_come_from_the_environment(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": str(token)})
    assert proc.returncode == 0, proc.stderr
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@BASH
def test_a_token_file_other_users_can_read_is_refused(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    token.chmod(0o644)
    proc = _preflight(shell, token_file=str(token))
    assert proc.returncode == 1
    assert "chmod 600" in proc.stderr
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_an_unreachable_daemon_is_refused_with_how_to_check(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token), curl_exit=7)
    assert proc.returncode == 1
    assert f"{REMOTE}/health" in proc.stderr
    assert "tailnet" in proc.stderr
    [curl] = [call for call in shell.logged() if call.startswith("curl|")]
    assert f"{REMOTE}/health" in curl
    assert "--max-time" in curl


@BASH
def test_an_open_daemon_is_never_reached_over_a_network(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token), health=HEALTH_OPEN)
    assert proc.returncode == 1
    assert "auth: false" in proc.stderr


@BASH
def test_an_open_daemon_through_a_loopback_tunnel_is_allowed(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, daemon_url="http://127.0.0.1:18765",
                      token_file=shell.path(token), health=HEALTH_OPEN)
    assert proc.returncode == 0, proc.stderr
    assert "unencrypted" not in proc.stderr


@BASH
def test_plain_http_off_this_machine_warns_once_and_continues(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token))
    assert proc.returncode == 0, proc.stderr
    warnings = [line for line in proc.stderr.splitlines() if "unencrypted" in line]
    assert len(warnings) == 1
    assert "tailnet" in warnings[0]
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_https_off_this_machine_does_not_warn(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, daemon_url="https://pl.example.invalid",
                      token_file=shell.path(token))
    assert proc.returncode == 0, proc.stderr
    assert "unencrypted" not in proc.stderr


@BASH
def test_a_relative_token_file_is_made_absolute(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = shell.run(
        f"cd '{shell.path(token.parent.parent)}'\n"
        "DAEMON_URL='" + REMOTE + "'\nCLIENT_ONLY=1\nTOKEN_FILE='keys/remote.token'\n"
        "CLIENTS=claude EXTRACTOR='' MODEL='' SHIM_PORT=0 NO_TOKEN='' TRANSPORT=shim\n"
        + _block("client-only mode") + _block("client-only preflight")
        + "\nclient_only_preflight\nprintf 'TOKEN_FILE=%s\\n' \"$TOKEN_FILE\"\n",
        extra_env={"FAKE_HEALTH": HEALTH_ON})
    assert proc.returncode == 0, proc.stderr
    assert f"TOKEN_FILE={shell.path(token)}\n" in proc.stdout


@BASH
def test_a_selected_cli_that_is_missing_is_refused_up_front(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    (shell.bin / "gemini").unlink()
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token), clients="claude gemini")
    assert proc.returncode == 1
    assert "gemini CLI is not on PATH" in proc.stderr


# -- Codex: the credential helper gets the operator's token and the remote URL --

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
                       r"^# [^\n]*10\. standing", (ROOT / "ops/install.sh").read_text(
                           encoding="utf-8"))[1]
    proc = shell.run(
        f"repo='{shell.path(ROOT)}'\nenv_file='{shell.path(env_file)}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        "CLIENTS=codex CODEX_HOOKS=auto CODEX_HOOK_TRUST=yes INSTRUCTIONS=auto CLAUDE_MD=''\n"
        + _between("ops/install.sh", "get_env() {", "\n") + "\n" + stages
        + "\nprintf 'FILE=%s\\nURL=%s\\n' \"$CODEX_CREDENTIAL_FILE\" \"$CODEX_CREDENTIAL_URL\"\n",
        strict="set -euo pipefail",
        extra_env={"CAPTURED": str(captured), "FIXTURE_URL": REMOTE,
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


@BASH
def test_client_only_briefing_hook_runs_the_host_shim_not_docker(bash, tmp_path):
    text = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    shell = _Shell(bash, tmp_path)
    proc = shell.run(
        "CLIENT_ONLY=1\n"
        + "\n".join(line for line in text.splitlines()
                    if line.startswith("briefing_command=")
                    or re.match(r"^\[ -z \"\$\{CLIENT_ONLY:-\}\" \] \|\| briefing_command=", line))
        + "\nprintf 'CMD=%s\\n' \"$briefing_command\"\n")
    assert proc.returncode == 0, proc.stderr
    assert "CMD=pseudolife-mcp briefing --hook-json\n" in proc.stdout


# -- the whole script, client-only, against a local stand-in daemon ------------

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
    for rel in ("ops/install.sh", "ops/install-hook.sh", "ops/client_credentials.py",
                "ops/.env.example", "examples/CLAUDE.memory.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    (repo / "ops" / "install.sh").chmod(0o755)
    (repo / "ops" / "install-hook.sh").chmod(0o755)
    return repo


@BASH
def test_a_client_only_install_wires_clients_to_the_remote_daemon(bash, tmp_path,
                                                                  board_server):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    repo = _copy_repo(tmp_path)
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    _stub(installed / "pseudolife-mcp", "exit 0\n")
    python = shell.path(sys.executable)
    for name in ("python3", "python"):
        _stub(shell.bin / name, f"exec '{python}' \"$@\"\n")
    _stub(shell.bin / "pipx",
          "printf 'pipx|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1\" = environment ] && [ \"$3\" = PIPX_BIN_DIR ]; then "
          "printf '%s\\n' \"$FAKE_INSTALL_BIN\"; fi\nexit 0\n")
    _stub(shell.bin / "claude",
          "printf 'claude|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then exit 1; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  -e, --env <env...>'; fi\n"
          "exit 0\n")
    _stub(shell.bin / "gemini",
          "printf 'gemini|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  -e, --env <env...>'; fi\n"
          "exit 0\n")
    proc = shell.run(
        f"exec '{shell.path(repo / 'ops' / 'install.sh')}' --client-only "
        f"--daemon-url '{board_server}' --token-file '{shell.path(token)}' "
        "--client claude,gemini --claude-plugin skip --instructions skip --no-art\n",
        extra_env={"FAKE_HEALTH": HEALTH_ON,
                   "FAKE_INSTALL_BIN": _bash_fixture_path(bash, installed, shell.env),
                   "PYTHONPATH": str(ROOT)})
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
    assert "pseudolife-mcp briefing --hook-json" in commands
    assert "docker exec" not in commands
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == board_server
    assert Path(settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token
    # The summary says where the daemon is, and the board was asked with
    # the operator's token.
    assert f"remote at {board_server}" in proc.stdout
    assert re.search(r"\[x\] Agent board\s+on", proc.stdout)
    assert any(seen.endswith(f"Bearer {FIXTURE_TOKEN}") for seen in _Board.seen)
    assert "Waiting for the daemon" not in proc.stdout


# -- parity with install.ps1 ----------------------------------------------------

def test_client_only_notes_are_synced_across_installers():
    """The client-only notes printed at the end are duplicated across
    install.sh and install.ps1 and must stay byte-identical."""
    sh = _heredoc_payload(_marker_block(
        (ROOT / "ops" / "install.sh").read_text(encoding="utf-8"), "client-only notes"))
    ps = _heredoc_payload(_marker_block(
        (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8"), "client-only notes"))
    assert sh == ps
    joined = "\n".join(sh)
    assert "token file" in joined
    assert "~/.codex/pseudolife/token" in joined
    for line in sh:
        assert len(line) <= 78, f"notes line over 78 cols: {line!r}"
        assert all(0x20 <= ord(c) <= 0x7E for c in line), f"non-ASCII: {line!r}"


def _messages(text: str) -> set[str]:
    """The operator-facing refusal and warning sentences of a block, with
    each script's flag spellings normalized to one form."""
    found = set(re.findall(r"client-only install: [^\"$]{12,}", text))
    found |= set(re.findall(r"the link itself is unencrypted[^\"$]*", text))
    found |= set(re.findall(r"An unauthenticated bank must never[^\"$]*", text))
    spellings = {"--daemon-url": "-DaemonUrl", "--token-file": "-TokenFile",
                 "--client-only": "-ClientOnly"}
    normalized = set()
    for message in found:
        for sh_flag, ps_flag in spellings.items():
            message = message.replace(ps_flag, sh_flag)
        normalized.add(message.rstrip(" .'`"))
    return normalized


def test_client_only_messages_match_across_installers():
    sh = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    ps = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    for name in ("client-only mode", "client-only preflight"):
        sh_block = "\n".join(_marker_block(sh, name))
        ps_block = "\n".join(_marker_block(ps, name))
        assert _messages(sh_block), name
        assert _messages(sh_block) == _messages(ps_block), name
