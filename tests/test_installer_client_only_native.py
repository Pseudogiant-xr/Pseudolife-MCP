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

from tests import installer_sandbox
from tests import native_client_stub as native_stub

from pseudolife_memory.credentials import CredentialProvider, _write_token_file
from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_existing_upgrade import _bash_fixture_path, _bash_variants, _between, user_path_unchanged
from tests.test_installer_existing_upgrade_native import _fixture_env


ROOT = Path(__file__).resolve().parents[1]


BASH = pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)


REMOTE = "http://100.64.0.2:8765"


HELPER = ROOT / "ops" / "client_credentials.py"


FIXTURE_TOKEN = "fixture-remote-token-0123456789"


HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'


HEALTH_OPEN = '{"status": "ok", "version": "0.0.0", "auth": false}'


PAIRING_CODE = "7KQ2-MX4P-9TZC"


URLSAFE_TOKEN = "Zq3_x9-KfixtureUrlsafeToken0123456789abcdEF"


PAIRED_TOKEN = "fixture-paired-token-0123456789"


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install-native.sh has no '{name}' marker block"
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
            f"repo='{self.path(ROOT)}'\n"
            # The native client's installer steps, answered by the Python
            # helpers they replaced (native_helper_standin.py).
            + native_stub.HELPER_SH +
            "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        )
        env = dict(self.env)
        env.update(extra_env or {})
        installer_sandbox.require(env, self.tmp)
        return subprocess.run([self.bash], input=prelude + body, capture_output=True,
                              text=True, encoding="utf-8", timeout=120, env=env, check=False)


@pytest.mark.parametrize("direction", ["input", "output", "ascii"])
def test_shell_fragment_transport_is_utf8_with_a_cp1252_parent(tmp_path, monkeypatch, direction):
    shell = object.__new__(_Shell)
    shell.bash = "fixture-bash"
    shell.env = installer_sandbox.environment(tmp_path)
    shell.tmp = tmp_path
    shell.home, shell.bin, shell.calls = (tmp_path / name for name in ("home", "bin", "calls"))
    monkeypatch.setattr(shell, "path", lambda path: "/fixture")
    payload = "memory ready" if direction == "ascii" else "memory ─ café"
    body = f"# {payload}\n" if direction == "input" else "# ASCII source\n"

    def cp1252_transport(argv, **kwargs):
        # Exercise both codec boundaries without starting a process. The
        # controlled shell emits UTF-8; an unspecified parent codec is cp1252.
        assert kwargs["text"] is True
        codec = kwargs.get("encoding") or "cp1252"
        received = kwargs["input"].encode(codec).decode("utf-8")
        assert received.endswith(body)
        return subprocess.CompletedProcess(argv, 0, payload.encode("utf-8").decode(codec), "")

    monkeypatch.setattr(subprocess, "run", cp1252_transport)
    result = shell.run(body)
    assert result.returncode == 0
    assert result.stdout == payload


def _mode(shell: _Shell, *, daemon_url: str = "", client_only: str = "",
          token_file: str = "", extractor: str = "", model: str = "",
          shim_port: str = "0", no_token: str = "", transport: str = "shim",
          read_token: str = "", prelude: str = "", pairing_code: str | None = None,
          extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """``pairing_code``: None leaves PAIRING_CODE unset, as a run of the
    blocks from before it does (they must hold under ``set -u`` either way)."""
    pairing = "" if pairing_code is None else f"PAIRING_CODE='{_q(pairing_code)}'\n"
    return shell.run(
        prelude + pairing + f"DAEMON_URL='{_q(daemon_url)}'\nCLIENT_ONLY='{client_only}'\nREAD_TOKEN='{read_token}'\n"
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
    "http://[::1]:8765", "http://127.8.9.10:8765"])
@BASH
def test_a_loopback_daemon_url_keeps_the_local_install(bash, tmp_path, url):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url)
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=\n" in proc.stdout


SHIM_LOOPBACK_FORMS = ["http://127.999.1.1:8765", "http://127.0.0.01:8765",
                       "http://[::ffff:127.0.0.1]:8765", "http://[0:0:0:0:0:0:0:1]:8765",
                       "http://LOCALHOST:8765"]


@pytest.mark.parametrize("url", SHIM_LOOPBACK_FORMS)
@BASH
def test_loopback_is_the_shims_own_answer(bash, tmp_path, url):
    from pseudolife_memory.daemon_url import _is_loopback_url
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url)
    assert proc.returncode == 0, proc.stderr
    expected = "" if _is_loopback_url(url) else "1"
    assert f"CLIENT_ONLY={expected}\n" in proc.stdout


@BASH
def test_the_installer_asks_the_shims_helper(bash, tmp_path):
    """The decision is the native client's, not a copy of its rule: an
    is-loopback step answering "loopback" (exit 10) keeps even a remote URL
    local."""
    shell = _Shell(bash, tmp_path)
    proc = _mode(shell, daemon_url=REMOTE,
                 prelude='native_helper() { [ "$1" = is-loopback ] && return 10; return 2; }\n')
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=\n" in proc.stdout


@pytest.mark.parametrize("url,client_only", [
    ("http://127.0.0.1:8765", ""), ("http://[::1]:8765", ""), (REMOTE, "1")])
@BASH
def test_without_the_clients_answer_the_host_rule_decides(bash, tmp_path, url, client_only):
    """A native step that answers neither 10 nor 11 leaves the decision to
    the installer's own host rule."""
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url,
                 prelude="native_helper() { return 1; }\n")
    assert proc.returncode == 0, proc.stderr
    assert f"CLIENT_ONLY={client_only}\n" in proc.stdout


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


@pytest.mark.parametrize("url", [
    "http://user:secret@pl.example.invalid:8765", "http://pl.example.invalid:8765/pl",
    "http://pl.example.invalid:8765/?x=1", "http://pl.example.invalid:8765#frag",
    "http://pl.example.invalid:port", "http://:8765"])
@BASH
def test_a_daemon_url_that_is_not_an_origin_is_refused(bash, tmp_path, url):
    """The shim's own rule (daemon_url._validated_daemon_url): an http(s)
    origin with no credentials, path, query or fragment."""
    proc = _mode(_Shell(bash, tmp_path), daemon_url=url)
    assert proc.returncode == 2
    assert "origin" in proc.stderr
    assert "secret" not in proc.stderr


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
def test_refusals_say_when_the_environment_implied_client_only(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), extractor="sidecar",
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 2
    assert "PSEUDOLIFE_MCP_DAEMON_URL in the environment" in proc.stderr


@BASH
def test_read_token_needs_client_only(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), read_token="1")
    assert proc.returncode == 2
    assert "--read-token" in proc.stderr


@BASH
def test_a_token_file_flag_needs_client_only(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), token_file="/fixture/remote.token")
    assert proc.returncode == 2
    assert "--token-file" in proc.stderr


@BASH
def test_a_pairing_code_flag_needs_client_only(bash, tmp_path):
    """Like --token-file, a pairing code names a remote daemon's credential."""
    proc = _mode(_Shell(bash, tmp_path), pairing_code=PAIRING_CODE)
    assert proc.returncode == 2
    assert "--pairing-code" in proc.stderr
    assert "--client-only --daemon-url" in proc.stderr
    assert PAIRING_CODE not in proc.stdout + proc.stderr


@BASH
def test_a_pairing_code_with_read_token_is_a_usage_error(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=REMOTE, read_token="1",
                 pairing_code=PAIRING_CODE)
    assert proc.returncode == 2
    assert "--pairing-code" in proc.stderr and "--read-token" in proc.stderr


@BASH
def test_a_pairing_code_with_a_remote_daemon_url_is_client_only(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=REMOTE, pairing_code=PAIRING_CODE)
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=1\n" in proc.stdout


@BASH
def test_the_script_itself_refuses_client_only_without_a_url(bash, tmp_path):
    """Refused while parsing arguments: nothing else has run yet."""
    shell = _Shell(bash, tmp_path)
    with user_path_unchanged(shell.home):
        proc = shell.run(f"exec '{shell.path(ROOT / 'ops' / 'install-native.sh')}' --client-only --no-art\n")
    assert proc.returncode == 2
    assert "--client-only needs" in proc.stderr
    assert shell.logged() == []


def _preflight(shell: _Shell, *, daemon_url: str = REMOTE, token_file: str = "",
               health: str = HEALTH_ON, curl_exit: int = 0, clients: str = "claude",
               client_only: str = "1", read_token: str = "", feed: str | None = None,
               pairing_code: str | None = None, after: str = "",
               extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """``feed``: a line on the preflight's stdin (--read-token input), from
    a file so the preflight runs in this shell and ``after`` (Bash run next,
    such as the pairing step) sees what it set."""
    env = {"FAKE_HEALTH": health, "FAKE_CURL_EXIT": str(curl_exit),
           "FIXTURE_FEED": feed or ""}
    env.update(extra_env or {})
    call = ("printf '%s\\n' \"$FIXTURE_FEED\" >\"$CALL_LOG.feed\"\n"
            "client_only_preflight <\"$CALL_LOG.feed\"\n" if feed is not None
            else "client_only_preflight\n")
    # Whether a pairing code is held, never the code itself: tests assert it
    # appears in no output.
    call += ("printf 'TOKEN_FILE=%s\\nPAIRING=%s\\n' \"$TOKEN_FILE\" \"${PAIRING_CODE:+held}\"\n"
             + after)
    pairing = "" if pairing_code is None else f"PAIRING_CODE='{_q(pairing_code)}'\n"
    return shell.run(
        pairing + f"DAEMON_URL='{_q(daemon_url)}'\nCLIENT_ONLY='{client_only}'\n"
        f"TOKEN_FILE='{_q(token_file)}'\nREAD_TOKEN='{read_token}'\n"
        f"CLIENTS='{clients}'\nEXTRACTOR='' MODEL='' SHIM_PORT=0 NO_TOKEN='' TRANSPORT=shim\n"
        + _block("bank location") + _block("client-only mode") + _block("client-only preflight")
        + "\n" + call,
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


@BASH
def test_a_token_file_without_owner_only_protection_is_refused(bash, tmp_path):
    """The shim's own check, on every OS: POSIX mode bits, or on Windows the
    inherited ACL a file written the ordinary way gets."""
    shell = _Shell(bash, tmp_path)
    token = tmp_path / "plain.token"
    token.write_text(FIXTURE_TOKEN + "\n", encoding="utf-8")
    if os.name != "nt":
        token.chmod(0o644)
    proc = _preflight(shell, token_file=shell.path(token))
    assert proc.returncode == 1
    assert "owner-only" in proc.stderr
    # --read-token refuses an existing file, so the advice deletes it first.
    assert "delete it and re-run with --read-token" in proc.stderr
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr
    assert not any(call.startswith("curl|") for call in shell.logged())


@pytest.mark.parametrize("target", ["nonempty", "empty", "missing"])
@BASH
def test_a_symlinked_token_file_is_refused(bash, tmp_path, target):
    shell = _Shell(bash, tmp_path)
    token = _token(shell) if target == "nonempty" else tmp_path / "target.token"
    if target == "empty":
        token.touch()
    link = tmp_path / "link.token"
    try:
        os.symlink(token, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    proc = _preflight(shell, token_file=shell.path(link))
    assert proc.returncode == 1
    assert "regular file" in proc.stderr
    assert not any(call.startswith("curl|") for call in shell.logged())


@BASH
def test_the_banner_says_when_the_environment_implied_client_only(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, daemon_url="", client_only="", token_file=shell.path(token),
                      extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, proc.stderr
    [banner] = [line for line in proc.stdout.splitlines() if "Client-only install" in line]
    assert "PSEUDOLIFE_MCP_DAEMON_URL in the environment" in banner


@BASH
def test_read_token_creates_an_owner_only_token_file_from_stdin(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1",
                      feed=FIXTURE_TOKEN)
    assert proc.returncode == 0, proc.stderr
    assert CredentialProvider(path=target).snapshot().token == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr
    assert any(call.startswith("curl|") for call in shell.logged())


@BASH
def test_read_token_never_replaces_an_existing_file(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    before = token.read_bytes()
    proc = _preflight(shell, token_file=shell.path(token), read_token="1",
                      feed="fixture-other-token")
    assert proc.returncode == 1
    assert "already exists" in proc.stderr
    assert token.read_bytes() == before


@BASH
def test_read_token_refuses_an_empty_answer(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1", feed="")
    assert proc.returncode == 1
    assert not target.exists()


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


_HINT_AFTER = "printf 'HINT=%s\\n' \"${PAIRING_HINT_URL:-}\"\n"


def _pairing_hints(output: str) -> list[str]:
    return [line for line in output.splitlines() if "invite <name>" in line]


@BASH
def test_a_raw_token_off_this_machine_records_the_pairing_hint(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token), after=_HINT_AFTER)
    assert proc.returncode == 0, proc.stderr
    assert f"HINT={REMOTE}\n" in proc.stdout
    # Printed at the end of the run, not here, before the shim exists.
    assert _pairing_hints(proc.stdout + proc.stderr) == []
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_a_pasted_raw_token_records_the_pairing_hint(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1",
                      feed=FIXTURE_TOKEN, after=_HINT_AFTER)
    assert proc.returncode == 0, proc.stderr
    assert f"HINT={REMOTE}\n" in proc.stdout
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_a_pairing_code_records_no_pairing_hint(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, after=_HINT_AFTER)
    assert proc.returncode == 0, proc.stderr
    assert "HINT=\n" in proc.stdout


@BASH
def test_a_pasted_pairing_code_records_no_pairing_hint(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1",
                      feed=PAIRING_CODE, after=_HINT_AFTER)
    assert proc.returncode == 0, proc.stderr
    assert "PAIRING=held\n" in proc.stdout
    assert "HINT=\n" in proc.stdout


@BASH
def test_a_loopback_daemon_records_no_pairing_hint(bash, tmp_path):
    """The daemon's own host, or an SSH tunnel to it: no hint."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    proc = _preflight(shell, daemon_url="http://127.0.0.1:18765",
                      token_file=shell.path(token), after=_HINT_AFTER)
    assert proc.returncode == 0, proc.stderr
    assert "HINT=\n" in proc.stdout


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
def test_a_missing_selected_cli_does_not_block_host_preflight(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    (shell.bin / "gemini").unlink()
    token = _token(shell)
    proc = _preflight(shell, token_file=shell.path(token), clients="claude gemini")
    assert proc.returncode == 0, proc.stderr
    assert "Daemon answered" in proc.stdout


CODE_FORMS = [PAIRING_CODE, "7kq2mx4p9tzc", "  7KQ2-MX4P-9TZC  ", "\t7kq2-mx4p-9tzc",
              "OIL2-MX4P-9TZC", "oil2mx4p9tzc", "7-K-Q-2MX4P9TZC", "--7KQ2MX4P9TZC--"]


NOT_CODES = [URLSAFE_TOKEN, FIXTURE_TOKEN, "", "7KQ2-MX4P-9TZU", "7KQ2-MX4P-9TZ",
             "7KQ2-MX4P-9TZC1", "7KQ2 MX4P 9TZC", "7KQ2_MX4P_9TZC", "'7KQ2-MX4P-9TZC'"]


def _pair_report(state: str, exit_code: int, error: str | None = None,
                 token_file: str = "/fixture/token") -> str:
    """pair's --json report: never the token or the code."""
    return json.dumps({"url": REMOTE, "state": state, "principal": "fixture-laptop",
                       "tier": "full", "bank": "fixture-bank", "token_file": token_file,
                       "warnings": [], "error": error, "exit": exit_code})


PAIR_SHIM = (
    "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
    "case \"$1\" in\n"
    "  pair)\n"
    "    cat >>\"$CALL_LOG.stdin\"\n"
    "    pair_file=''\n"
    "    while [ $# -gt 0 ]; do [ \"$1\" = --token-file ] && pair_file=\"$2\"; shift; done\n"
    "    case \"${FAKE_PAIR_EXIT:-0}\" in 0|5)\n"
    "      if [ -n \"${FAKE_PAIR_PLAIN:-}\" ]; then\n"
    "        printf '%s\\n' \"$FAKE_PAIR_TOKEN\" >\"$pair_file\"; chmod 644 \"$pair_file\"\n"
    "      else printf '%s\\n' \"$FAKE_PAIR_TOKEN\" |\n"
    "        \"$FIXTURE_PYTHON\" \"$FIXTURE_HELPER\" write-token-file --path \"$pair_file\" >/dev/null; fi ;;\n"
    "    esac\n"
    "    printf '%s\\n' \"$FAKE_PAIR_REPORT\"; exit \"${FAKE_PAIR_EXIT:-0}\" ;;\n"
    "  connect)\n"
    "    case \" $* \" in *' --dry-run '*)\n"
    "      echo '{\"url\": null, \"rows\": [], \"exit\": 3, \"error\": \"none\"}'; exit 3 ;; esac\n"
    "    exit 0 ;;\n"
    "  expose) exit \"${FAKE_EXPOSE_EXIT:-0}\" ;;\n"
    "esac\n"
    "exit 0\n")


def _pair_env(shell: _Shell, *, pair_exit: int = 0, report: str | None = None,
              plain: bool = False) -> dict[str, str]:
    return {"FAKE_PAIR_EXIT": str(pair_exit), "FAKE_PAIR_TOKEN": PAIRED_TOKEN,
            "FAKE_PAIR_PLAIN": "1" if plain else "",
            "FAKE_PAIR_REPORT": report if report is not None else _pair_report("paired", pair_exit),
            "FIXTURE_PYTHON": _bash_fixture_path(shell.bash, Path(sys.executable), shell.env),
            "FIXTURE_HELPER": _bash_fixture_path(shell.bash, HELPER, shell.env)}


def _pairing_step(shell: _Shell, *, shim_ok: bool = True, connect: bool = True) -> str:
    """The pairing step, then (``connect``) the connect step, as section 9
    runs them, with the fake shim installed."""
    shim = shell.bin / "fake-shim"
    _stub(shim, PAIR_SHIM)
    ensure = (f"ensure_shim() {{ SHIM_OK=1; SHIM_PATH='{shell.path(shim)}'; }}\n" if shim_ok
              else "ensure_shim() { SHIM_OK=''; SHIM_PATH=''; }\n")
    body = (ensure + "SHIM_HELD=''\n" + _block("client-only pair")
            + "pair_with_code\nprintf 'PAIRED=%s|%s\\n' \"$TOKEN_FILE\" \"${PAIRING_CODE:+held}\"\n")
    if connect:
        body += _block("client-only connect") + "connect_existing_registrations\n"
    return body


def _shim_calls(shell: _Shell) -> list[str]:
    return [call for call in shell.logged() if call.startswith("shim|")]


def _pair_stdin(shell: _Shell) -> str:
    path = Path(str(shell.calls) + ".stdin")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _assert_code_unseen(shell: _Shell, proc, code: str = PAIRING_CODE) -> None:
    """The code never reaches an argument list a stub recorded, or the output."""
    canonical = code.strip().upper().replace("-", "")
    for text in [*shell.logged(), proc.stdout, proc.stderr]:
        assert code.strip() not in text and canonical not in text.upper().replace("-", ""), text


def _is_code_sh(shell: _Shell, values: list[str], helper_answers: bool = True) -> list[bool]:
    """is_pairing_code on each value, fed on stdin as the preflight feeds it;
    without ``helper_answers`` the native step answers neither 10 nor 11."""
    feed = shell.tmp / "values.txt"
    feed.write_bytes("".join(value + "\n" for value in values).encode("utf-8"))
    prelude = "" if helper_answers else "native_helper() { cat >/dev/null; return 1; }\n"
    proc = shell.run(
        prelude + _block("client-only preflight")
        + f"while IFS= read -r value; do\n"
          "    if printf '%s\\n' \"$value\" | is_pairing_code; then echo CODE; else echo TOKEN; fi\n"
          f"done <'{shell.path(feed)}'\n")
    assert proc.returncode == 0, proc.stderr
    return [line == "CODE" for line in proc.stdout.splitlines()]


@pytest.mark.parametrize("fallback", [False, True], ids=["client", "shell-rule"])
@BASH
def test_a_pairing_code_is_recognised_exactly_as_pair_reads_one(bash, tmp_path, fallback):
    """The shape is the shim's own rule (principals.normalize_pairing_code),
    and the shell rule used when that cannot be asked gives the same answer."""
    from pseudolife_memory.principals import normalize_pairing_code
    shell = _Shell(bash, tmp_path)
    values = CODE_FORMS + NOT_CODES
    assert _is_code_sh(shell, values, helper_answers=not fallback) == [
        normalize_pairing_code(value) is not None for value in values]


@pytest.mark.parametrize("form", CODE_FORMS)
@BASH
def test_a_pasted_pairing_code_is_kept_for_pairing_not_written(bash, tmp_path, form):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1", feed=form)
    assert proc.returncode == 0, proc.stderr
    assert "PAIRING=held\n" in proc.stdout
    assert not target.exists()
    assert target.parent.is_dir()
    assert any(call.startswith("curl|") for call in shell.logged())
    assert "pairing code" in proc.stdout
    _assert_code_unseen(shell, proc, form)


@pytest.mark.parametrize("token", [URLSAFE_TOKEN, FIXTURE_TOKEN])
@BASH
def test_a_pasted_token_is_still_written_as_the_token(bash, tmp_path, token):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1", feed=token)
    assert proc.returncode == 0, proc.stderr
    assert "PAIRING=\n" in proc.stdout
    assert CredentialProvider(path=target).snapshot().token == token


@pytest.mark.parametrize("clients,name", [("claude", "claude-code"), ("codex", "codex"),
                                          ("claude codex", "shared")])
@BASH
def test_a_pairing_code_defaults_to_the_token_file_named_for_the_client(bash, tmp_path,
                                                                        clients, name):
    """Non-interactive (--daemon-url ... --pairing-code ...): nothing asks
    where the token goes, so it is the file the paste path offers, in a
    directory made for it; the environment's token file is someone else's."""
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, clients=clients,
                      extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/other.token"})
    assert proc.returncode == 0, proc.stderr
    target = shell.home / ".pseudolife-mcp" / f"{name}.token"
    assert f"TOKEN_FILE={shell.path(target)}\nPAIRING=held\n" in proc.stdout
    assert target.parent.is_dir() and not target.exists()
    # The daemon is still checked, exactly as for a token file.
    assert f"curl|-fsS --max-time 5 {REMOTE}/health" in shell.logged()
    _assert_code_unseen(shell, proc)


@BASH
def test_a_given_token_file_overrides_the_pairing_default(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "laptop.token"
    proc = _preflight(shell, pairing_code=PAIRING_CODE, token_file=shell.path(target))
    assert proc.returncode == 0, proc.stderr
    assert f"TOKEN_FILE={shell.path(target)}\n" in proc.stdout
    assert target.parent.is_dir() and not target.exists()


@BASH
def test_a_pairing_code_refuses_an_existing_token_file_before_pair_runs(bash, tmp_path):
    """pair never overwrites: an existing file is refused at the preflight,
    before the daemon is asked and before any code is spent."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    before = token.read_bytes()
    proc = _preflight(shell, pairing_code=PAIRING_CODE, token_file=shell.path(token),
                      after=_pairing_step(shell), extra_env=_pair_env(shell))
    assert proc.returncode == 1
    assert "already exists" in proc.stderr and "remote.token" in proc.stderr
    assert token.read_bytes() == before
    assert _shim_calls(shell) == []
    assert not any(call.startswith("curl|") for call in shell.logged())
    _assert_code_unseen(shell, proc)


@BASH
def test_a_pairing_code_is_redeemed_on_stdin_and_then_used_as_the_token_file(bash, tmp_path):
    """--daemon-url <remote> --pairing-code <code>, non-interactive: the mode
    block, the preflight, then pair with the code on stdin and never in an
    argument list, then the token file checked and handed to connect."""
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, client_only="", clients="codex",
                      after=_pairing_step(shell), extra_env=_pair_env(shell))
    assert proc.returncode == 0, proc.stderr
    target = shell.path(shell.home / ".pseudolife-mcp" / "codex.token")
    assert _shim_calls(shell) == [
        f"shim|pair {REMOTE} --read-code --token-file {target} --json",
        f"shim|connect {REMOTE} --token-file {target} --client codex --dry-run --json"]
    assert _pair_stdin(shell) == PAIRING_CODE + "\n"
    assert CredentialProvider(path=shell.home / ".pseudolife-mcp" / "codex.token"
                              ).snapshot().token == PAIRED_TOKEN
    # The code is forgotten once pair has it.
    assert f"PAIRED={target}|\n" in proc.stdout
    assert "fixture-laptop" in proc.stdout
    _assert_code_unseen(shell, proc)
    assert PAIRED_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_a_pasted_pairing_code_reaches_pair_as_it_was_typed(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(shell, token_file=shell.path(target), read_token="1",
                      feed=" oil2-mx4p-9tzc ", after=_pairing_step(shell, connect=False),
                      extra_env=_pair_env(shell))
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [
        f"shim|pair {REMOTE} --read-code --token-file {shell.path(target)} --json"]
    assert _pair_stdin(shell).strip() == "oil2-mx4p-9tzc"
    assert CredentialProvider(path=target).snapshot().token == PAIRED_TOKEN
    _assert_code_unseen(shell, proc, "oil2-mx4p-9tzc")


@BASH
def test_pairing_runs_even_with_no_client_for_connect(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, clients="generic",
                      after=_pairing_step(shell), extra_env=_pair_env(shell))
    assert proc.returncode == 0, proc.stderr
    assert [call.split()[0] for call in _shim_calls(shell)] == ["shim|pair"]


@BASH
def test_a_refused_pairing_stops_the_install(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    report = _pair_report("refused", 4, "fixture-refusal: the daemon refused the pairing code")
    proc = _preflight(shell, pairing_code=PAIRING_CODE, after=_pairing_step(shell),
                      extra_env=_pair_env(shell, pair_exit=4, report=report))
    assert proc.returncode == 1
    assert "pseudolife-mcp pair exited 4" in proc.stderr
    assert "fixture-refusal: the daemon refused the pairing code" in proc.stderr
    assert "PAIRED=" not in proc.stdout
    assert [call.split()[0] for call in _shim_calls(shell)] == ["shim|pair"]
    assert not (shell.home / ".pseudolife-mcp" / "claude-code.token").exists()
    _assert_code_unseen(shell, proc)


@BASH
def test_an_unknown_pairing_outcome_names_the_kept_token_file(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    report = _pair_report("unknown", 5, "fixture-unknown: no answer after the retries")
    proc = _preflight(shell, pairing_code=PAIRING_CODE, after=_pairing_step(shell),
                      extra_env=_pair_env(shell, pair_exit=5, report=report))
    assert proc.returncode == 1
    kept = shell.path(shell.home / ".pseudolife-mcp" / "claude-code.token")
    assert "pseudolife-mcp pair exited 5" in proc.stderr
    assert "fixture-unknown: no answer after the retries" in proc.stderr
    assert f"kept at {kept}" in proc.stderr
    assert "may hold a token the daemon now accepts" in proc.stderr
    assert [call.split()[0] for call in _shim_calls(shell)] == ["shim|pair"]
    _assert_code_unseen(shell, proc)


@BASH
def test_pairing_without_a_shim_stops_the_install(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, after=_pairing_step(shell, shim_ok=False),
                      extra_env=_pair_env(shell))
    assert proc.returncode == 1
    assert "pairing needs the pseudolife-mcp shim" in proc.stderr
    assert _shim_calls(shell) == []
    _assert_code_unseen(shell, proc)


@BASH
def test_the_paired_token_file_is_checked_as_a_given_one_is(bash, tmp_path):
    """A file the shim could not read (here: not owner-only) stops the
    install at the same check a --token-file meets."""
    shell = _Shell(bash, tmp_path)
    proc = _preflight(shell, pairing_code=PAIRING_CODE, after=_pairing_step(shell),
                      extra_env=_pair_env(shell, plain=True))
    assert proc.returncode == 1
    assert "the shim cannot use the token file" in proc.stderr
    assert [call.split()[0] for call in _shim_calls(shell)] == ["shim|pair"]


_HOOKS_READY = ("printf '{\"status\":\"ready\",\"source\":\"manual\","
                "\"instructions\":\"covered-by-hooks\",\"recovery\":null}\\n'")


def _codex_steps_sh(credentials: str) -> str:
    return (native_stub.HELPER_SH.replace("native_helper()", "standin_helper()")
            + "native_helper() {\n"
              "    case \"$1\" in\n"
              f"        codex-credentials) {credentials} ;;\n"
              "        codex-runtime-defaults)\n"
              "            printf '{\"status\":\"ready\",\"runtime_defaults\":\"configured\"}\\n' ;;\n"
              f"        codex-hooks) {_HOOKS_READY} ;;\n"
              "        *) standin_helper \"$@\" ;;\n"
              "    esac\n"
              "}\n")


@BASH
def test_codex_credentials_come_from_the_token_file_and_the_remote_url(bash, tmp_path):
    """Section 9 hands the Codex credential helper the token on stdin (never
    an argument, never printed) and the remote URL; the local ops/.env is
    not consulted, and the registration carries what the helper set up."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    captured = tmp_path / "helper.txt"
    steps = _codex_steps_sh(
        "{ printf 'ARGS=%s\\n' \"$*\"; printf 'STDIN='; cat; printf '\\n'; } >\"$CAPTURED\"\n"
        "            printf '{\"status\":\"ready\",\"credential_file_configured\":true,"
        "\"connection_configured\":true,\"credential_file_path\":\"/fixture/codex/token\","
        "\"daemon_url\":\"%s\"}\\n' \"$FIXTURE_URL\"")
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
        + _between("ops/install-native.sh", "get_env() {", "\n") + "\n" + native_stub.BASH + "\n"
        + steps + stages
        + "\nprintf 'FILE=%s\\nURL=%s\\n' \"$CODEX_CREDENTIAL_FILE\" \"$CODEX_CREDENTIAL_URL\"\n",
        strict="set -euo pipefail",
        extra_env={"CAPTURED": str(captured), "FIXTURE_URL": REMOTE,
                   "FAKE_LAUNCHER": _bash_fixture_path(bash, tmp_path / "native" / "pseudolife-mcp", shell.env)})
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


def _codex_wiring(shell: _Shell, *, cred: str | None = None,
                  cred_exit: int = 0, native_exit: int = 0, existing: str = "",
                  requested: str | None = None, add_exit: int = 0, missing: bool = False,
                  runtime_failed: bool = False, gemini_list: str = "", show_ladder: bool = False,
                  hook_report: str = "", hook_trust: str = "yes",
                  hook_interactive: bool = False):
    """Sections 9 to 11 for a Codex-only client-only install, with canned
    Codex steps, the native client (``native_exit`` is its install result)
    and a stub codex. Returns the run and the codex calls."""
    token = _token(shell)
    installed = shell.tmp / "installed-bin"
    installed.mkdir()
    _stub(installed / "pseudolife-mcp", "exit 0\n")
    steps = _codex_steps_sh(
        "cat >/dev/null; printf '%s\\n' \"$FAKE_CRED\"; return \"$FAKE_CRED_EXIT\"")
    _stub(shell.bin / "codex",
          "printf 'codex|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then\n"
          "  [ -n \"$FAKE_EXISTING\" ] || exit 1; printf '%s\\n' \"$FAKE_EXISTING\"; exit 0; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  --env <KEY=VALUE>'; exit 0; fi\n"
          'exit "$FAKE_MCP_ADD_EXIT"\n')
    if missing:
        (shell.bin / "codex").unlink()
    if runtime_failed:
        steps = steps.replace("codex-runtime-defaults)\n", "codex-runtime-defaults) return 1\n", 1)
    if hook_report:
        steps = steps.replace(
            _HOOKS_READY,
            'printf "hook-options|%s\\n" "$*" >> "$CALL_LOG"; printf "%s\\n" "$FAKE_HOOK_REPORT"', 1)
    terminal = (
        "function [ { if [[ $* == '! -t 0 ]' ]]; then return 1; fi; builtin [ \"$@\"; }\n"
        if hook_interactive else ""
    )
    finish_fixture = ""
    if requested is not None:
        if native_exit and not gemini_list:
            gemini_list = "pseudolife-memory: http://fixture.example/mcp (http)"
        _stub(shell.bin / "gemini",
              "printf 'gemini|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
              "if [ \"$*\" = 'mcp list' ]; then printf '%s\\n' \"$FAKE_GEMINI_LIST\"; fi\n"
              "if [ \"$*\" = 'mcp add --help' ]; then echo '  -e, --env'; fi\n"
              "exit 0\n")
        finish_fixture = ("\nprintf 'MCP_GEMINI=%s\\n' \"$MCP_GEMINI\"\n"
                          "echo REMAINING_STEPS_FINISHED\n"
                          + _block("client setup result"))
    source = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    stages = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# [^\n]*12\. health", source)[1]
    ladder = re.search(r"(?ms)^# [^\n]*13\. per-provider wiring ladder[^\n]*\n(.*?)"
                       r"^# One line for the agent board", source)[1] if show_ladder else ""
    proc = shell.run(
        f"env_file='{shell.path(shell.tmp / 'absent.env')}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        f"CLIENTS='codex{' gemini' if requested is not None else ''}'\n"
        f"REQUESTED_CLIENTS='{requested or ''}'\n"
        f"CODEX_HOOKS=auto CODEX_HOOK_TRUST={hook_trust} INSTRUCTIONS={'skip' if hook_interactive else 'auto'} "
        "CLAUDE_MD='' EXPLICIT_ARGS=''\n"
        "AGENTS_FILE='' TRANSPORT=shim\n"
        + _between("ops/install-native.sh", "get_env() {", "\n") + "\n" + native_stub.BASH + "\n"
        + terminal + steps + stages + ladder
        + "\nprintf 'MCP_CODEX=%s\\n' \"$MCP_CODEX\"\n" + finish_fixture,
        strict="set -euo pipefail",
        extra_env={"FAKE_CRED": cred if cred is not None else CRED_READY % REMOTE,
                   "FAKE_CRED_EXIT": str(cred_exit), "FAKE_NATIVE_EXIT": str(native_exit),
                   "FAKE_EXISTING": existing, "FAKE_MCP_ADD_EXIT": str(add_exit),
                   "FAKE_HOOK_REPORT": hook_report,
                   "FAKE_GEMINI_LIST": gemini_list,
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


DENIED_CREDENTIAL = "The fixture credential was refused. Supply an accepted token and rerun the installer."
COMPLETION_CASES = [
    pytest.param({"cred": json.dumps({"status": "needs-configuration", "recovery": DENIED_CREDENTIAL}),
                  "cred_exit": 1}, DENIED_CREDENTIAL, id="credential-refused"),
    pytest.param({"cred": '{"status":"tokenless","credential_file_configured":false,'
                           '"connection_configured":true,"daemon_url":"%s"}' % REMOTE},
                 f"Codex was not registered: credential setup for the daemon at {REMOTE} "
                 "did not produce a token file. Fix the error above and rerun the installer.",
                 id="credential-missing"),
    pytest.param({"missing": True}, "The codex CLI is not on PATH. Install it and rerun, or drop it from --client.",
                 id="cli-missing"),
    pytest.param({"add_exit": 17}, "MCP registration failed (exit 17). Fix the CLI error above and rerun the installer.",
                 id="registration"),
    pytest.param({"native_exit": 7}, "Codex authentication requires the stdio shim. HTTP fallback was not registered. "
                 "Fix the native client install (see above), then rerun the installer.", id="native-install"),
    pytest.param({"existing": "pseudolife-memory\ncommand: pseudolife-mcp\n{PSEUDOLIFE_MCP_NO_SPAWN=1}"},
                 "the existing Codex registration was preserved and the checkout shim installed, but bare "
                 "pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from "
                 "PATH or put the installed scripts directory first, then re-run.", id="migration"),
    pytest.param({"existing": "pseudolife-memory\ncommand: custom-shim"},
                 "The existing stdio registration has no verifiable no-spawn guard. "
                 "Set PSEUDOLIFE_MCP_NO_SPAWN=1 in the existing entry, then rerun.", id="verification"),
]


@pytest.mark.parametrize("kwargs,reason", COMPLETION_CASES)
@pytest.mark.parametrize("requested,status", [("codex gemini", 5), ("", 0)])
@BASH
def test_incomplete_client_leaves_other_steps_running(bash, tmp_path, kwargs, reason, requested, status):
    proc, _ = _codex_wiring(_Shell(bash, tmp_path), requested=requested, **kwargs)
    assert proc.returncode == status, proc.stdout + proc.stderr
    other_state = "present" if kwargs.get("native_exit") else "shim-env"
    assert f"MCP_GEMINI={other_state}" in proc.stdout
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    label = "Requested" if requested else "Detected"
    assert proc.stderr.splitlines()[-1] == f"{label} client setup incomplete (Codex). {reason}"
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@BASH
def test_malformed_gemini_listing_reports_only_that_client(bash, tmp_path):
    proc, _ = _codex_wiring(
        _Shell(bash, tmp_path), requested="codex gemini", gemini_list="pseudolife-memory")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "MCP_CODEX=shim-env" in proc.stdout
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.stderr.splitlines()[-1] == (
        "Requested client setup incomplete (Gemini CLI). Gemini CLI did not return a usable Pseudolife registration. "
        "Check gemini mcp list, then rerun the installer."
    )


@pytest.mark.parametrize("transport,repair", [
    ("shim", "The native client could not be installed (see above), and the daemon requires a bearer token "
             "for this connection. Fix the native client install, then rerun the installer."),
    ("http", "The installer cannot add the daemon's required bearer token to an HTTP registration. "
             "Rerun the installer with --transport shim."),
])
@BASH
def test_bearer_transport_failure_names_the_right_repair(bash, tmp_path, transport, repair):
    proc = _Shell(bash, tmp_path).run(
        f"TRANSPORT={transport} CLIENTS=claude REQUESTED_CLIENTS=claude\n"
        + _block("client registration state") + _block("client registration helpers")
        + "check_client_http_auth claude\n" + _block("client setup result"))
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert proc.stderr.splitlines()[-1] == f"Requested client setup incomplete (Claude Code). {repair}"


@pytest.mark.parametrize("kwargs", [{}, {"runtime_failed": True},
                                    {"existing": "pseudolife-memory\ncommand: custom-shim\n{PSEUDOLIFE_MCP_NO_SPAWN=1}"}])
@BASH
def test_successful_client_setup_and_rerun_exit_zero(bash, tmp_path, kwargs):
    proc, _ = _codex_wiring(_Shell(bash, tmp_path), requested="codex gemini", show_ladder=True, **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
    codex_ladder = proc.stdout.split("  OpenAI Codex\n", 1)[1]
    if kwargs.get("runtime_failed"):
        assert "Warnings\nCodex runtime defaults were not confirmed." in proc.stderr
        assert codex_ladder.startswith("    [!] MCP transport        stdio shim")
        assert "    [!] Runtime defaults     not confirmed -" in codex_ladder
    elif kwargs.get("existing"):
        assert codex_ladder.startswith("    [x] MCP transport        already wired with custom command (unchanged)")
    else:
        assert codex_ladder.startswith("    [x] MCP transport        stdio shim")


@pytest.mark.parametrize("deferred", [False, True], ids=["full", "client-only"])
@pytest.mark.parametrize("requested", [True, False], ids=["requested", "detected"])
@BASH
def test_a_claude_hook_write_failure_is_reported_at_completion(bash, tmp_path, deferred, requested):
    shell = _Shell(bash, tmp_path)
    ops = tmp_path / "ops"
    ops.mkdir()
    _stub(ops / "install-hook-native.sh", "exit 19\n")
    block = _block("client-only claude hook") if deferred else _between(
        "ops/install-native.sh", '    step "Installing $selected_client session hook..."', "\ndone")
    proc = shell.run(
        f"repo='{shell.path(tmp_path)}'\n"
        f"CLIENTS='claude' REQUESTED_CLIENTS='{'claude' if requested else ''}' HOOK_CLAUDE=deferred\n"
        "selected_client=claude briefing_command=fixture SHIM_OK=1 SHIM_PATH=/fixture/shim\n"
        "ensure_shim() { :; }\n"
        + _block("client registration state") + block
        + "\nprintf 'CLIENT_SETUP_CONTINUED\\n'\n" + _block("client setup result"),
        strict="set -eu",
    )
    assert proc.returncode == (5 if requested else 0), proc.stdout + proc.stderr
    assert "CLIENT_SETUP_CONTINUED" in proc.stdout
    lines = proc.stderr.splitlines()
    label = "Requested" if requested else "Detected"
    assert lines[-1] == (
        f"{label} client setup incomplete (Claude Code). "
        "Hooks were not installed. Fix the hook error above and rerun the installer."
    )
    if not requested:
        assert lines[-2] == "Warnings"


CODEX_HOOK_APPROVAL = (
    "Hooks await approval. Open /hooks in the Codex CLI or Settings > Hooks in the Codex app. "
    "Review and trust the Pseudolife handlers, then start a new session."
)
CODEX_HOOK_REPORTS = [
    pytest.param("unavailable", "The fixture could not write hooks.",
                 "Hooks could not be set up. Fix the hook problem reported above and rerun the installer.",
                 id="failure"),
    pytest.param("unavailable", "Codex hooks are disabled in configuration (features.hooks = false); setup preserves that choice.",
                 "Hooks are disabled in Codex. Set features.hooks = true in the active Codex configuration and rerun the installer.",
                 id="disabled"),
]


@pytest.mark.parametrize("hook_status,recovery,reason", CODEX_HOOK_REPORTS)
@pytest.mark.parametrize("requested", ["codex gemini", ""], ids=["requested", "detected"])
@BASH
def test_codex_hook_failure_is_reported_at_completion(bash, tmp_path, hook_status, recovery, reason, requested):
    report = json.dumps(dict(status=hook_status, source="manual", instructions="present", recovery=recovery))
    proc, adds = _codex_wiring(_Shell(bash, tmp_path), requested=requested, hook_report=report)
    assert proc.returncode == (5 if requested else 0), proc.stdout + proc.stderr
    assert adds and "MCP_GEMINI=shim-env" in proc.stdout
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert recovery in proc.stdout
    label = "Requested" if requested else "Detected"
    assert proc.stderr.splitlines()[-1] == f"{label} client setup incomplete (Codex). {reason}"
    if not requested:
        assert "Warnings" in proc.stderr.splitlines()


CODEX_PENDING_CASES = [
    pytest.param("ask", False, "No hooks are installed.", id="unattended"),
    pytest.param("ask", True, "PseudoLife hooks await approval. Use Codex /hooks.", id="declined"),
    pytest.param("no", False, "PseudoLife hooks await approval. Use Codex /hooks.", id="opt-out"),
]


@pytest.mark.parametrize("trust,interactive,recovery", CODEX_PENDING_CASES)
@BASH
def test_pending_codex_hooks_warn_without_failing(bash, tmp_path, trust, interactive, recovery):
    shell = _Shell(bash, tmp_path)
    report = json.dumps(dict(status="pending", source="manual", instructions="present", recovery=recovery))
    proc, adds = _codex_wiring(shell, requested="codex gemini", hook_report=report,
                               hook_trust=trust, hook_interactive=interactive)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert adds and "REMAINING_STEPS_FINISHED" in proc.stdout
    [hook_args] = [line for line in shell.logged() if line.startswith("hook-options|")]
    assert f" --trust {trust} " in hook_args
    assert ("--non-interactive" in hook_args) is not interactive
    reason = CODEX_HOOK_APPROVAL if recovery.startswith("PseudoLife") else (
        "Hooks are not installed. Rerun the installer with --codex-hook-trust yes."
    )
    assert proc.stderr.splitlines()[-2:] == ["Warnings", "Codex hook setup needs attention. " + reason]


@BASH
def test_requested_codex_instruction_failure_is_a_warning(bash, tmp_path):
    report = json.dumps({
        "status": "ready", "source": "manual", "instructions": "skipped",
        "instructions_error": {"file": "fixture-AGENTS.md", "cause": "fixture write failed"},
        "recovery": "Standing instructions could not be written: fixture write failed",
    })
    proc, adds = _codex_wiring(_Shell(bash, tmp_path), requested="codex gemini", hook_report=report)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert adds and "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
    assert proc.stderr.splitlines()[-2:] == [
        "Warnings", "Codex standing instructions could not be written. Check the Codex home and file permissions.",
    ]


@pytest.mark.parametrize("outcome,recovery", [
    ("refused", "The installer credential was not accepted by the configured daemon. "
     "Supply a token accepted by that daemon, then rerun the installer."),
    ("not-ready", "The authenticated check at https://daemon.example.com did not succeed "
     "within 180 seconds. The last attempt returned HTTP 503. Check that the daemon is "
     "reachable, then rerun the installer."),
])
@BASH
def test_codex_credential_bootstrap_reports_failure(bash, tmp_path, outcome, recovery):
    cred = json.dumps({
        "status": "needs-configuration", "credential_check": outcome, "recovery": recovery,
    })
    proc, adds = _codex_wiring(_Shell(bash, tmp_path), cred=cred, cred_exit=1)
    assert proc.returncode == 0, proc.stderr
    assert adds == []
    assert "MCP_CODEX=failed" in proc.stdout
    assert recovery in proc.stdout + proc.stderr
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr


@pytest.mark.parametrize("scenario,kwargs,says", [
    ("credential setup failed",
     {"cred": '{"status":"needs-configuration","recovery":"fixture"}', "cred_exit": 1},
     "credential setup failed"),
    ("shim install failed", {"native_exit": 1}, "stdio shim"),
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
    for rel in ("ops/install-native.sh", "ops/install-hook-native.sh", "ops/client_credentials.py",
                "ops/.env.example", "examples/CLAUDE.memory.md", "pyproject.toml"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    (repo / "ops" / "install-native.sh").chmod(0o755)
    (repo / "ops" / "install-hook-native.sh").chmod(0o755)
    return repo


FRESH_CONNECT = '{"url": null, "rows": [], "exit": 3, "error": "none"}'


def _whole_client_only_run(bash, tmp_path, board_server, *, connect_report: str = FRESH_CONNECT,
                           connect_exit: int = 3, current_recovery: str = "",
                           native_exit: int = 0, requested: bool = True):
    """The whole client-only run, with a configurable Claude Code connect result."""
    shell = _Shell(bash, tmp_path)
    token = _token(shell)
    repo = _copy_repo(tmp_path)
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    # The staged client's installer steps are answered by the stand-in, which
    # runs this checkout's helpers as the native client carries its own.
    standin = shell.path(ROOT / "tests" / "native_helper_standin.py")
    verify = ""
    if current_recovery:
        report = json.dumps({"status": "failed", "recovery": current_recovery})
        verify = (
            "if [ \"$1 $2 $3\" = 'installer connect-report current' ]; then cat >/dev/null; echo current; exit 0; fi\n"
            "if [ \"$1 $2 $3\" = 'installer credentials verify-token-file' ]; then\n"
            f"  printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"; echo '{report}'; exit 1\nfi\n"
        )
    release_env = native_stub.write_release(
        tmp_path / "release", version,
        verify + "if [ \"$1\" = installer ] && [ \"$2\" != migrate-registrations ]; then\n"
        f"  shift; exec '{shell.path(sys.executable)}' '{standin}' '{shell.path(ROOT)}' \"$@\"\nfi\n"
        "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
        "if [ \"$1\" = connect ]; then\n"
        f"  case \" $* \" in *' --client gemini '*) echo '{FRESH_CONNECT}'; exit 3 ;; esac\n"
        f"  echo '{connect_report}'; exit {connect_exit}\nfi\n"
        "exit 0\n")
    release_env["PSEUDOLIFE_NATIVE_ARCHIVE_URL"] = shell.path(tmp_path / "release")
    # No Python anywhere: the installer and its hook writer run every step
    # through the native client (a client-only install runs no Docker
    # preflight; tests/test_installer_no_python.py scans that script), so
    # each of these records the call and fails it: any line fails the test.
    python_calls = tmp_path / "python-calls"
    for name in ("python", "python3", "py", "pip", "pip3", "pipx"):
        _stub(shell.bin / name, f"echo {name} \"$*\" >>'{shell.path(python_calls)}'\nexit 99\n")
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
            + ("--client claude,gemini " if requested else "--yes ")
            + "--claude-plugin skip --instructions skip --no-art\n",
            extra_env={"FAKE_HEALTH": HEALTH_ON,
                       "FAKE_NATIVE_EXIT": str(native_exit),
                       **release_env,
                       "PSEUDOLIFE_SHIM_RUNTIMES": shell.path(installed / "runtimes"),
                       "PSEUDOLIFE_SHIM_LAUNCHER": shell.path(installed / "pseudolife-mcp"),
                       # The installer's own environment carries an older credential.
                       "PSEUDOLIFE_MCP_TOKEN": "fixture-local-literal"})
    return installed, proc, python_calls, repo, shell, token


@BASH
def test_unattended_detected_clients_keep_success_when_native_install_fails(bash, tmp_path, board_server):
    _, proc, _, _, _, _ = _whole_client_only_run(
        bash, tmp_path, board_server, native_exit=7, requested=False)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "the native client could not be installed" in output
    lines = proc.stderr.splitlines()
    assert "Warnings" in lines
    assert (
        "Detected client setup incomplete (Claude Code). Hooks were not installed because the native client is "
        "unavailable. Fix the native client install and rerun the installer."
    ) in lines
    assert (
        "Detected client setup incomplete (Claude Code). the pseudolife-mcp shim is unavailable (see the message above), "
        "and a remote daemon needs it: an HTTP registration cannot carry the token file. Fix that and re-run."
    ) in lines
    assert lines[-1] == (
        "Detected client setup incomplete (Gemini CLI). shim unavailable for Gemini CLI (see warnings above), "
        "and a remote daemon needs it: an HTTP registration cannot carry the token file. Fix that and re-run."
    )
    assert "Requested client setup incomplete" not in output
    assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."


@BASH
def test_a_client_only_install_wires_clients_to_the_remote_daemon(bash, tmp_path,
                                                                  board_server):
    installed, proc, python_calls, repo, shell, token = _whole_client_only_run(bash, tmp_path, board_server)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not python_calls.exists(), "the installer ran Python tooling: " + python_calls.read_text()
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
    assert [call.split("--client ", 1)[1] for call in connects] == ["claude-code,gemini --dry-run --json"]
    assert "not checked" not in output


@BASH
def test_preflight_looks_for_no_python_pip_or_pipx(bash, tmp_path):
    """The client and every installer step are the native client now: the
    preflight neither checks for Python, venv or pipx nor starts them, and
    its Docker ownership check is the native client's."""
    shell = _Shell(bash, tmp_path)
    calls = tmp_path / "python-calls"
    for name in ("python", "python3", "py", "pip", "pip3", "pipx"):
        _stub(shell.bin / name, f"echo {name} >>'{shell.path(calls)}'\nexit 99\n")
    _stub(shell.bin / "git", "exit 0\n")
    client = tmp_path / "native" / "pseudolife-mcp"
    client.parent.mkdir()
    client.write_text(
        '#!/bin/sh\n[ "$1 $2" = "installer preflight-check" ] || exit 2\n'
        "echo '  OK   Docker stack, recorded volumes and ports belong to this install (or are free)'\n",
        encoding="utf-8", newline="\n")
    client.chmod(0o755)
    script = (ROOT / "ops" / "preflight-native.sh").read_text(encoding="utf-8")
    proc = shell.run("set -- --client generic\n" + script,
                     extra_env={"PSEUDOLIFE_NATIVE_CLI": shell.path(client)})
    assert not calls.exists(), calls.read_text()
    assert "python" not in proc.stdout.lower().replace("pseudolife", "")
    assert "pipx" not in proc.stdout
    assert "Docker stack, recorded volumes and ports belong to this install" in proc.stdout


@BASH
def test_preflight_run_by_hand_before_an_install_warns_and_carries_on(bash, tmp_path):
    """Before an install there is no native client yet: the ownership check
    runs when ops/install-native.sh runs the preflight, so a hand run says so and
    still checks everything else."""
    shell = _Shell(bash, tmp_path)
    _stub(shell.bin / "git", "exit 0\n")
    _stub(shell.bin / "docker", "exit 0\n")
    script = (ROOT / "ops" / "preflight-native.sh").read_text(encoding="utf-8")
    proc = shell.run("set -- --client generic\n" + script,
                     extra_env={"PSEUDOLIFE_NATIVE_CLI": shell.path(tmp_path / "absent" / "pseudolife-mcp")})
    assert "the Docker ownership check needs the native pseudolife-mcp client" in proc.stdout
    assert "PSEUDOLIFE_SHIM_ENGINE=native ops/install.sh downloads and verifies it" in proc.stdout
    assert re.search(r"OK\S*\s+git", proc.stdout), proc.stdout
    assert proc.returncode == 0, proc.stdout


@BASH
def test_preflight_names_a_client_too_old_for_the_check(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    _stub(shell.bin / "git", "exit 0\n")
    _stub(shell.bin / "docker", "exit 0\n")
    old = tmp_path / "old" / "pseudolife-mcp"
    old.parent.mkdir()
    old.write_text("#!/bin/sh\necho 'pseudolife-mcp installer: expected an installer step' >&2\nexit 2\n",
                   encoding="utf-8", newline="\n")
    old.chmod(0o755)
    script = (ROOT / "ops" / "preflight-native.sh").read_text(encoding="utf-8")
    proc = shell.run("set -- --client generic\n" + script,
                     extra_env={"PSEUDOLIFE_NATIVE_CLI": shell.path(old)})
    assert proc.returncode == 1, proc.stdout
    assert "is older than this checkout and has no Docker ownership check" in proc.stdout


@pytest.mark.parametrize("buildx_exit", [0, 1])
@BASH
def test_preflight_fails_without_the_buildx_plugin_the_image_build_needs(bash, tmp_path, buildx_exit):
    """The daemon's Dockerfile uses ``RUN --mount`` caches, which only
    BuildKit runs; without the buildx plugin compose falls back to the
    classic builder, and the install died mid-build with "the --mount
    option requires BuildKit" after preflight said all checks passed
    (CachyOS with Docker Engine and no docker-buildx, 2026-10-25)."""
    shell = _Shell(bash, tmp_path)
    _stub(shell.bin / "docker",
          'case "$*" in "buildx version") exit "$FAKE_BUILDX_EXIT" ;; esac\nexit 0\n')
    # The fix line asserted below is the Linux one, whatever this host runs.
    _stub(shell.bin / "uname", "printf 'Linux\\n'\n")
    # The stub PATH keeps only /usr/bin:/bin, which on Git for Windows has
    # no git: stub it, and give the ownership check a client that passes, so
    # the exit code is buildx's alone.
    _stub(shell.bin / "git", "exit 0\n")
    client = tmp_path / "native" / "pseudolife-mcp"
    client.parent.mkdir()
    client.write_text('#!/bin/sh\necho "  OK   Docker stack"\n', encoding="utf-8", newline="\n")
    client.chmod(0o755)
    script = (ROOT / "ops" / "preflight-native.sh").read_text(encoding="utf-8")
    proc = shell.run("set -- --client generic\n" + script,
                     extra_env={"FAKE_BUILDX_EXIT": str(buildx_exit),
                                "PSEUDOLIFE_NATIVE_CLI": shell.path(client)})
    lines = proc.stdout.splitlines()
    if buildx_exit == 0:
        assert any(re.search(r"OK\S*\s+docker buildx", line) for line in lines), proc.stdout
        assert proc.returncode == 0, proc.stdout
    else:
        [failed] = [i for i, line in enumerate(lines) if re.search(r"FAIL\S*\s+docker buildx", line)]
        assert "BuildKit" in lines[failed]
        assert "sudo pacman -S docker-buildx" in lines[failed + 1]
        assert "docker-buildx-plugin" in lines[failed + 1]
        assert proc.returncode == 1, proc.stdout


@BASH
def test_client_only_connect_failure_keeps_the_native_client_and_reports_partial_setup(bash, tmp_path, board_server):
    installed, proc, python_calls, repo, shell, token = _whole_client_only_run(bash, tmp_path, board_server, connect_report='{"url": null, "rows": [], "exit": 1, "error": "the fixture refuses"}', connect_exit=1)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 5, output
    assert "could not check the existing registration" in output and "the fixture refuses" in output
    assert "Nothing was changed" not in output and "nothing was changed" not in output
    assert "stopped part way" not in output
    assert proc.stderr.splitlines()[-1] == (
        "Requested client setup incomplete (Claude Code). pseudolife-mcp connect could not check the existing "
        "registration (exit 1: the fixture refuses). Fix the reported problem and rerun the installer."
    )
    assert any(call.startswith("gemini|mcp add -s user") for call in shell.logged())
    assert (installed / "pseudolife-mcp").is_file()
    settings = json.loads((shell.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:8765"
    assert settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] == "/fixture/local/claude-code.token"


@BASH
def test_client_only_current_registration_with_refused_token_is_incomplete(bash, tmp_path, board_server):
    plan = json.dumps({"rows": [{"place": "registration", "client": "claude-code", "state": "current"}], "exit": 0})
    _, proc, _, _, shell, _ = _whole_client_only_run(
        bash, tmp_path, board_server, connect_report=plan, connect_exit=0, current_recovery=DENIED_CREDENTIAL)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert proc.stdout.count("If the daemon is unreachable, this can take up to 180 seconds.") == 1
    assert proc.stderr.splitlines()[-1] == "Requested client setup incomplete (Claude Code). " + DENIED_CREDENTIAL
    calls = shell.logged()
    assert len([call for call in calls if call.startswith("shim|installer credentials verify-token-file")]) == 1
    assert not any(call.startswith("shim|connect") and "--yes" in call for call in calls)
    assert any(call.startswith("gemini|mcp add -s user") for call in calls)
