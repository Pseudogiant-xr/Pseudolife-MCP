"""The native installer (ops/install-native.*): the tests of test_installer_client_only_ps.py
that the native client path changed, run against the native installer.

test_installer_client_only_ps.py keeps the default (Python client) installer's tests.
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
from tests.test_installer_client_only import CODE_FORMS, NOT_CODES, _pair_report
from tests.test_installer_client_only_native import CODEX_HOOK_APPROVAL, CODEX_HOOK_REPORTS, CODEX_PENDING_CASES, COMPLETION_CASES, DENIED_CREDENTIAL
from tests.test_installer_existing_upgrade import _between, user_path_unchanged
from tests.test_installer_existing_upgrade_native import _fixture_env


ROOT = Path(__file__).resolve().parents[1]


INSTALL = "ops/install-native.ps1"


REMOTE = "http://100.64.0.2:8765"


FIXTURE_TOKEN = "fixture-remote-token-0123456789"


HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'


HEALTH_OPEN = '{"status": "ok", "version": "0.0.0", "auth": false}'


HELPER = ROOT / "ops" / "client_credentials.py"


PAIRING_CODE = "7KQ2-MX4P-9TZC"


URLSAFE_TOKEN = "Zq3_x9-KfixtureUrlsafeToken0123456789abcdEF"


PAIRED_TOKEN = "fixture-paired-token-0123456789"


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install-native.ps1").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install-native.ps1 has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _q(value: str | Path) -> str:
    return str(value).replace("'", "''")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


_WRAPPED = re.compile(r"\r?\n[ \t]*\|[ \t]?")


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return _WRAPPED.sub("", _ANSI.sub("", proc.stdout + proc.stderr))


class _PowerShell:
    """A disposable home, and pwsh running install-native.ps1 fragments in it."""

    def __init__(self, tmp_path: Path):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("PowerShell 7 (pwsh) is unavailable")
        self.pwsh = pwsh
        self.tmp = tmp_path
        self.env = _fixture_env(tmp_path / "env")
        self.home = Path(self.env["HOME"])
        self.calls = tmp_path / "calls.txt"

    def logged(self) -> list[str]:
        return (self.calls.read_text(encoding="utf-8").splitlines()
                if self.calls.exists() else [])

    def stub_function(self, name: str, body: str = "$global:LASTEXITCODE = 0") -> str:
        return (f"function global:{name} {{\n"
                f"    Add-Content -LiteralPath '{_q(self.calls)}' -Value ('{name}|' + (@($args) -join ' '))\n"
                f"    {body}\n}}\n")

    def run(self, body: str, *, extra_env: dict[str, str] | None = None,
            stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
        prelude = (
            "$ErrorActionPreference = 'Stop'\n"
            # Match the Python pipe codec even on a legacy Windows console.
            "[Console]::InputEncoding = [Console]::OutputEncoding = $OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            # Plain text on every OS, and an uncaught error as one plain line
            # (exit 1, as pwsh gives it): see _output.
            "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
            "trap { [Console]::Error.WriteLine(\"ERROR: $($_.Exception.Message)\"); exit 1 }\n"
            f"$repo = '{_q(ROOT)}'\n"
            "function Step($message) { Write-Output \"STEP: $message\" }\n"
            # The native client's installer steps, answered by the Python
            # helpers they replaced (native_helper_standin.py).
            + native_stub.HELPER_PS
            + _between(INSTALL, "function Get-HelperStatus($output)", "\n}\n") + "\n}\n"
        )
        script = self.tmp / "run.ps1"
        script.write_text(prelude + body, encoding="utf-8")
        env = dict(self.env)
        env.update({"NO_COLOR": "1", "TERM": "dumb"})
        env.update(extra_env or {})
        stdin = {"input": stdin_text} if stdin_text is not None else {
            "stdin": subprocess.DEVNULL}
        installer_sandbox.require(env, self.tmp)
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=120, env=env, check=False, **stdin)


@pytest.mark.parametrize("direction", ["input", "output", "ascii"])
def test_powershell_fragment_transport_is_utf8_with_a_cp1252_parent(tmp_path, monkeypatch, direction):
    ps = object.__new__(_PowerShell)
    ps.pwsh, ps.tmp, ps.env = "fixture-pwsh", tmp_path, installer_sandbox.environment(tmp_path)
    payload = "memory ready" if direction == "ascii" else "memory ─ café"
    stdin = payload if direction == "input" else "ASCII input"
    body = "# memory ─ café\n"

    def cp1252_transport(argv, **kwargs):
        assert kwargs["text"] is True
        assert Path(argv[-1]).read_bytes().decode("utf-8").replace("\r\n", "\n").endswith(body)
        codec = kwargs.get("encoding") or "cp1252"
        assert kwargs["input"].encode(codec).decode("utf-8") == stdin
        return subprocess.CompletedProcess(argv, 0, payload.encode("utf-8").decode(codec), "")

    monkeypatch.setattr(subprocess, "run", cp1252_transport)
    result = ps.run(body, stdin_text=stdin)
    assert result.returncode == 0
    assert result.stdout == payload


def test_powershell_fixture_roundtrips_unicode_stdio(tmp_path):
    ps = _PowerShell(tmp_path)
    payload = "memory ─ café"
    result = ps.run(
        f"$value = [Console]::ReadLine()\n"
        f"if ($value -ne '{payload}') {{ throw 'UTF-8 stdin mismatch' }}\n"
        f"Write-Output '{payload}'\n",
        stdin_text=payload + "\n")
    assert result.returncode == 0, _output(result)
    assert result.stdout.strip() == payload


def _mode(ps: _PowerShell, *, daemon_url: str = "", client_only: bool = False,
          token_file: str = "", extractor: str = "", model: str = "",
          shim_port: int = 0, no_token: bool = False, transport: str = "shim",
          read_token: bool = False, pairing_code: str = "",
          extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return ps.run(
        f"$PairingCode = '{_q(pairing_code)}'\n"
        f"$DaemonUrl = '{_q(daemon_url)}'\n"
        f"$ReadToken = [switch]${'true' if read_token else 'false'}\n"
        f"$ClientOnly = [switch]${'true' if client_only else 'false'}\n"
        f"$TokenFile = '{_q(token_file)}'\n$Extractor = '{extractor}'\n$Model = '{model}'\n"
        f"$ShimPort = {shim_port}\n$NoToken = [switch]${'true' if no_token else 'false'}\n"
        f"$Transport = '{transport}'\n"
        + _block("client-only mode")
        + "\nWrite-Output \"CLIENT_ONLY=$([bool]$ClientOnly)\"\n"
        + "Write-Output \"DAEMON_URL=$DaemonUrl\"\n",
        extra_env=extra_env)


@pytest.mark.parametrize("url", [
    REMOTE, "https://pl.example.invalid", "http://pl.example.invalid:8765/"])
def test_a_daemon_url_off_this_machine_implies_client_only(tmp_path, url):
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout
    assert f"DAEMON_URL={url.rstrip('/')}" in proc.stdout.splitlines()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765", "http://localhost:8765/", "http://LOCALHOST:8765",
    "http://[::1]:8765", "http://127.8.9.10:8765"])
def test_a_loopback_daemon_url_keeps_the_local_install(tmp_path, url):
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=False" in proc.stdout


SHIM_LOOPBACK_FORMS = ["http://127.999.1.1:8765", "http://127.0.0.01:8765",
                       "http://[::ffff:127.0.0.1]:8765", "http://[0:0:0:0:0:0:0:1]:8765",
                       "http://LOCALHOST:8765"]


@pytest.mark.parametrize("url", SHIM_LOOPBACK_FORMS)
def test_loopback_is_the_shims_own_answer(tmp_path, url):
    from pseudolife_memory.daemon_url import _is_loopback_url
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 0, _output(proc)
    assert f"CLIENT_ONLY={not _is_loopback_url(url)}" in proc.stdout.splitlines()


def test_the_installer_asks_the_shims_helper(tmp_path):
    """The decision is the native client's, not a copy of its rule: an
    is-loopback step answering "loopback" (exit 10) keeps even a remote URL
    local."""
    ps = _PowerShell(tmp_path)
    proc = ps.run("function Invoke-NativeHelper {\n"
                  "    $global:LASTEXITCODE = if ($args[0] -eq 'is-loopback') { 10 } else { 2 }\n"
                  "}\n"
                  f"$DaemonUrl = '{REMOTE}'\n$ReadToken = [switch]$false\n"
                  "$ClientOnly = [switch]$false\n$TokenFile = ''\n$Extractor = ''\n"
                  "$Model = ''\n$ShimPort = 0\n$NoToken = [switch]$false\n$Transport = 'shim'\n"
                  + _block("client-only mode")
                  + "\nWrite-Output \"CLIENT_ONLY=$([bool]$ClientOnly)\"\n")
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=False" in proc.stdout.splitlines()


@pytest.mark.parametrize("url,client_only", [
    ("http://127.0.0.1:8765", False), ("http://[::1]:8765", False), (REMOTE, True)])
def test_without_the_clients_answer_the_host_rule_decides(tmp_path, url, client_only):
    """A native step that answers neither 10 nor 11 leaves the decision to
    the installer's own host rule."""
    ps = _PowerShell(tmp_path)
    proc = ps.run("function Invoke-NativeHelper { $global:LASTEXITCODE = 1 }\n"
                  f"$DaemonUrl = '{url}'\n$ReadToken = [switch]$false\n"
                  "$ClientOnly = [switch]$false\n$TokenFile = ''\n$Extractor = ''\n"
                  "$Model = ''\n$ShimPort = 0\n$NoToken = [switch]$false\n$Transport = 'shim'\n"
                  + _block("client-only mode")
                  + "\nWrite-Output \"CLIENT_ONLY=$([bool]$ClientOnly)\"\n")
    assert proc.returncode == 0, _output(proc)
    assert f"CLIENT_ONLY={client_only}" in proc.stdout.splitlines()


def test_the_environment_url_is_honoured_when_the_flag_is_absent(tmp_path):
    proc = _mode(_PowerShell(tmp_path), extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout
    assert f"DAEMON_URL={REMOTE}" in proc.stdout.splitlines()


def test_the_flag_wins_over_the_environment_url(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url="http://127.0.0.1:8765",
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=False" in proc.stdout
    assert "DAEMON_URL=http://127.0.0.1:8765" in proc.stdout.splitlines()


def test_an_explicit_client_only_install_may_use_a_loopback_tunnel(tmp_path):
    proc = _mode(_PowerShell(tmp_path), client_only=True,
                 daemon_url="http://127.0.0.1:18765")
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout


def test_client_only_requires_a_daemon_url(tmp_path):
    proc = _mode(_PowerShell(tmp_path), client_only=True)
    assert proc.returncode == 2
    assert "-ClientOnly needs" in _output(proc)
    assert "-DaemonUrl" in _output(proc)


def test_a_malformed_daemon_url_is_refused(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url="pl.example.invalid:8765")
    assert proc.returncode == 2
    assert "invalid daemon URL" in _output(proc)


@pytest.mark.parametrize("url", [
    "http://user:secret@pl.example.invalid:8765", "http://pl.example.invalid:8765/pl",
    "http://pl.example.invalid:8765/?x=1", "http://pl.example.invalid:8765#frag",
    "http://pl.example.invalid:port", "http://:8765"])
def test_a_daemon_url_that_is_not_an_origin_is_refused(tmp_path, url):
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 2
    assert "origin" in _output(proc)
    assert "secret" not in _output(proc)


def test_refusals_say_when_the_environment_implied_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), extractor="sidecar",
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 2
    assert "PSEUDOLIFE_MCP_DAEMON_URL in the environment" in _output(proc)


def test_read_token_needs_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), read_token=True)
    assert proc.returncode == 2
    assert "-ReadToken" in _output(proc)


@pytest.mark.parametrize("flag,kwargs", [
    ("-Extractor", {"extractor": "sidecar"}),
    ("-Model", {"model": "claude-opus-5"}),
    ("-ShimPort", {"shim_port": 8082}),
    ("-NoToken", {"no_token": True}),
    ("-Transport http", {"transport": "http"}),
])
def test_client_only_refuses_the_flags_of_a_local_daemon(tmp_path, flag, kwargs):
    proc = _mode(_PowerShell(tmp_path), daemon_url=REMOTE, **kwargs)
    assert proc.returncode == 2
    assert flag in _output(proc)


def test_a_token_file_flag_needs_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), token_file=r"C:\fixture\remote.token")
    assert proc.returncode == 2
    assert "-TokenFile" in _output(proc)


def test_a_pairing_code_parameter_needs_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), pairing_code=PAIRING_CODE)
    assert proc.returncode == 2
    assert "-PairingCode" in _output(proc)
    assert "-ClientOnly -DaemonUrl" in _output(proc)
    assert PAIRING_CODE not in _output(proc)


def test_a_pairing_code_with_read_token_is_a_usage_error(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url=REMOTE, read_token=True,
                 pairing_code=PAIRING_CODE)
    assert proc.returncode == 2
    assert "-PairingCode" in _output(proc) and "-ReadToken" in _output(proc)


def test_a_pairing_code_with_a_remote_daemon_url_is_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url=REMOTE, pairing_code=PAIRING_CODE)
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout


def test_the_script_itself_refuses_client_only_without_a_url(tmp_path):
    ps = _PowerShell(tmp_path)
    with user_path_unchanged(ps.home):
        proc = installer_sandbox.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
             "-ClientOnly", "-NoArt"],
            capture_output=True, text=True, timeout=120, env=installer_sandbox.layout(ps.env, tmp_path), root=tmp_path, check=False,
            stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-ClientOnly needs" in _output(proc)


def _preflight(ps: _PowerShell, *, daemon_url: str = REMOTE, token_file: str = "",
               health: str = HEALTH_ON, unreachable: bool = False,
               clients: tuple[str, ...] = ("claude",), missing: tuple[str, ...] = (),
               extra_env: dict[str, str] | None = None, client_only: bool = True,
               read_token: bool = False, feed: str | None = None, before: str = "",
               cwd: Path | None = None, pairing_code: str = "",
               after: str = "") -> subprocess.CompletedProcess[str]:
    """``feed``: text on the preflight's stdin (-ReadToken input); ``before``:
    PowerShell run first, in the same session; ``after``: run next (the
    pairing step)."""
    env = {"FAKE_HEALTH": health, "FAKE_UNREACHABLE": "1" if unreachable else ""}
    env.update(extra_env or {})
    stubs = "".join(ps.stub_function(name) for name in ("claude", "codex", "gemini")
                    if name not in missing)
    client_list = ", ".join(f"'{client}'" for client in clients)
    body = (
        f"$env:PATH = '{_q(ps.tmp)}'\n"
        + (f"Set-Location -LiteralPath '{_q(cwd)}'\n" if cwd else "")
        + stubs
        + ps.stub_function("Invoke-RestMethod",
                           "if ($env:FAKE_UNREACHABLE) { throw 'unreachable' }\n"
                           "    return ($env:FAKE_HEALTH | ConvertFrom-Json)")
        + before
        + f"$PairingCode = '{_q(pairing_code)}'\n"
        f"$DaemonUrl = '{_q(daemon_url)}'\n"
        f"$ClientOnly = [switch]${'true' if client_only else 'false'}\n"
        f"$ReadToken = [switch]${'true' if read_token else 'false'}\n"
        f"$TokenFile = '{_q(token_file)}'\n$clients = @({client_list})\n"
        "$Extractor = ''; $Model = ''; $ShimPort = 0; $NoToken = [switch]$false\n"
        "$Transport = 'shim'\n"
        + _block("bank location") + _block("client-only mode") + _block("client-only preflight")
        + "\nInvoke-ClientOnlyPreflight\nWrite-Output \"TOKEN_FILE=$TokenFile\"\n"
        # Whether a pairing code is held, never the code itself.
        "Write-Output \"PAIRING=$(if ($PairingCode) { 'held' })\"\n" + after)
    return ps.run(body, extra_env=env, stdin_text=feed)


def _token(ps: _PowerShell) -> Path:
    path = ps.tmp / "keys" / "remote.token"
    path.parent.mkdir()
    _write_token_file(path, FIXTURE_TOKEN)
    return path


def test_client_only_never_mints_a_token_it_needs_one_given(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps)
    assert proc.returncode == 2
    assert "-TokenFile" in _output(proc)
    assert "never mints" in _output(proc)
    assert not any(call.startswith("Invoke-RestMethod|") for call in ps.logged())


def test_a_missing_token_file_is_refused(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, token_file=str(tmp_path / "absent.token"))
    assert proc.returncode == 1
    assert "absent.token" in _output(proc)
    assert "missing or empty" in _output(proc)


def test_the_token_file_may_come_from_the_environment(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": str(token)})
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_FILE={token}" in proc.stdout.splitlines()
    assert FIXTURE_TOKEN not in _output(proc)


def test_a_token_file_written_with_set_content_is_refused(tmp_path):
    """The shim's own check, on every OS: Set-Content leaves the inherited
    ACL on Windows and the umask's mode on POSIX, neither owner-only."""
    ps = _PowerShell(tmp_path)
    plain = tmp_path / "plain.token"
    proc = _preflight(ps, token_file=str(plain),
                      before=f"Set-Content -LiteralPath '{_q(plain)}' -Value '{FIXTURE_TOKEN}'\n")
    assert plain.exists()
    assert proc.returncode == 1
    assert "owner-only" in _output(proc)
    # -ReadToken refuses an existing file, so the advice deletes it first.
    assert "delete it and re-run with -ReadToken" in _output(proc)
    assert FIXTURE_TOKEN not in _output(proc)
    assert not any(call.startswith("Invoke-RestMethod|") for call in ps.logged())


@pytest.mark.parametrize("target", ["nonempty", "empty", "missing"])
def test_a_symlinked_token_file_is_refused(tmp_path, target):
    ps = _PowerShell(tmp_path)
    token = _token(ps) if target == "nonempty" else tmp_path / "target.token"
    if target == "empty":
        token.touch()
    link = tmp_path / "link.token"
    try:
        os.symlink(token, link)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    proc = _preflight(ps, token_file=str(link))
    assert proc.returncode == 1
    assert "regular file" in _output(proc)
    assert not any(call.startswith("Invoke-RestMethod|") for call in ps.logged())


def test_the_banner_says_when_the_environment_implied_client_only(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="", client_only=False, token_file=str(token),
                      extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, _output(proc)
    [banner] = [line for line in proc.stdout.splitlines() if "Client-only install" in line]
    assert "PSEUDOLIFE_MCP_DAEMON_URL in the environment" in banner


def test_read_token_creates_an_owner_only_token_file_from_stdin(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=FIXTURE_TOKEN + "\n")
    assert proc.returncode == 0, _output(proc)
    assert CredentialProvider(path=target).snapshot().token == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in _output(proc)


def test_read_token_never_replaces_an_existing_file(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    before = token.read_bytes()
    proc = _preflight(ps, token_file=str(token), read_token=True, feed="fixture-other\n")
    assert proc.returncode == 1
    assert "already exists" in _output(proc)
    assert token.read_bytes() == before


def test_read_token_refuses_an_empty_answer(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed="\n")
    assert proc.returncode == 1
    assert not target.exists()


def test_an_unreachable_daemon_is_refused_with_how_to_check(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), unreachable=True)
    assert proc.returncode == 1
    assert f"{REMOTE}/health" in _output(proc)
    assert "tailnet" in _output(proc)
    [call] = [call for call in ps.logged() if call.startswith("Invoke-RestMethod|")]
    assert f"{REMOTE}/health" in call
    assert "-TimeoutSec" in call
    # A redirect is refused, as curl -f and the shim refuse one.
    assert "-MaximumRedirection 0" in call


def test_an_open_daemon_is_never_reached_over_a_network(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), health=HEALTH_OPEN)
    assert proc.returncode == 1
    assert "auth: false" in _output(proc)


def test_an_open_daemon_through_a_loopback_tunnel_is_allowed(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="http://127.0.0.1:18765", token_file=str(token),
                      health=HEALTH_OPEN)
    assert proc.returncode == 0, _output(proc)
    assert "unencrypted" not in _output(proc)


def test_plain_http_off_this_machine_warns_once_and_continues(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token))
    assert proc.returncode == 0, _output(proc)
    warnings = [line for line in _output(proc).splitlines() if "unencrypted" in line]
    assert len(warnings) == 1
    assert "tailnet" in warnings[0]
    assert FIXTURE_TOKEN not in _output(proc)


def test_https_off_this_machine_does_not_warn(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="https://pl.example.invalid", token_file=str(token))
    assert proc.returncode == 0, _output(proc)
    assert "unencrypted" not in _output(proc)


_HINT_AFTER = "Write-Output \"HINT=$script:pairingHintUrl\"\n"


def _pairing_hints(output: str) -> list[str]:
    return [line for line in output.splitlines() if "invite <name>" in line]


def test_a_raw_token_off_this_machine_records_the_pairing_hint(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), after=_HINT_AFTER)
    assert proc.returncode == 0, _output(proc)
    assert f"HINT={REMOTE}" in proc.stdout.splitlines()
    assert _pairing_hints(_output(proc)) == []
    assert FIXTURE_TOKEN not in _output(proc)


def test_a_pasted_raw_token_records_the_pairing_hint(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=FIXTURE_TOKEN + "\n",
                      after=_HINT_AFTER)
    assert proc.returncode == 0, _output(proc)
    assert f"HINT={REMOTE}" in proc.stdout.splitlines()
    assert FIXTURE_TOKEN not in _output(proc)


def test_a_pairing_code_records_no_pairing_hint(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, after=_HINT_AFTER)
    assert proc.returncode == 0, _output(proc)
    assert "HINT=" in proc.stdout.splitlines()


def test_a_pasted_pairing_code_records_no_pairing_hint(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=PAIRING_CODE + "\n",
                      after=_HINT_AFTER)
    assert proc.returncode == 0, _output(proc)
    assert "PAIRING=held" in proc.stdout.splitlines()
    assert "HINT=" in proc.stdout.splitlines()


def test_a_loopback_daemon_records_no_pairing_hint(tmp_path):
    """The daemon's own host, or an SSH tunnel to it: no hint."""
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="http://127.0.0.1:18765", token_file=str(token),
                      after=_HINT_AFTER)
    assert proc.returncode == 0, _output(proc)
    assert "HINT=" in proc.stdout.splitlines()


def test_a_relative_token_file_is_made_absolute(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=os.path.join("keys", "remote.token"), cwd=tmp_path)
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_FILE={token}" in proc.stdout.splitlines()


def test_a_missing_selected_cli_does_not_block_host_preflight(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), clients=("claude", "gemini"),
                      missing=("gemini",))
    assert proc.returncode == 0, _output(proc)
    assert "Daemon answered" in proc.stdout


PS_PAIR_SHIM = """function global:fake-shim {{
    Add-Content -LiteralPath '{calls}' -Value ('shim|' + (@($args) -join ' '))
    $callArgs = @($args)
    if ($callArgs[0] -eq 'pair') {{
        Add-Content -LiteralPath '{calls}.stdin' -Value (@($input) -join "`n")
        $file = $callArgs[[array]::IndexOf($callArgs, '--token-file') + 1]
        if ($env:FAKE_PAIR_EXIT -in '0', '5') {{
            if ($env:FAKE_PAIR_PLAIN) {{
                Set-Content -LiteralPath $file -Value $env:FAKE_PAIR_TOKEN
            }} else {{
                $null = $env:FAKE_PAIR_TOKEN | & '{python}' '{helper}' write-token-file --path $file
            }}
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_PAIR_EXIT
        return $env:FAKE_PAIR_REPORT
    }}
    if (($callArgs[0] -eq 'connect') -and ($callArgs -contains '--dry-run')) {{
        $global:LASTEXITCODE = 3
        return '{{"url": null, "rows": [], "exit": 3, "error": "none"}}'
    }}
    $global:LASTEXITCODE = 0
}}
"""


def _pair_env(*, pair_exit: int = 0, report: str | None = None,
              plain: bool = False) -> dict[str, str]:
    return {"FAKE_PAIR_EXIT": str(pair_exit), "FAKE_PAIR_TOKEN": PAIRED_TOKEN,
            "FAKE_PAIR_PLAIN": "1" if plain else "",
            "FAKE_PAIR_REPORT": report if report is not None else _pair_report("paired", pair_exit)}


def _pairing_step(ps: _PowerShell, *, shim_ok: bool = True, connect: bool = True) -> str:
    ensure = ("function Install-ShimOnce { $script:shimInstallPath = 'fake-shim'; return $true }\n"
              if shim_ok else
              "function Install-ShimOnce { $script:shimInstallPath = $null; return $false }\n")
    body = (PS_PAIR_SHIM.format(calls=_q(ps.calls), python=_q(sys.executable), helper=_q(HELPER))
            + ensure + _block("client-only pair") + "Invoke-ClientOnlyPair\n"
            "Write-Output \"PAIRED=$TokenFile|$(if ($PairingCode) { 'held' })\"\n")
    if connect:
        body += _block("client-only connect") + "Invoke-ClientOnlyConnect\n"
    return body


def _shim_calls(ps: _PowerShell) -> list[str]:
    return [call for call in ps.logged() if call.startswith("shim|")]


def _pair_stdin(ps: _PowerShell) -> str:
    path = Path(str(ps.calls) + ".stdin")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _assert_code_unseen(ps: _PowerShell, proc, code: str = PAIRING_CODE) -> None:
    canonical = code.strip().upper().replace("-", "")
    for text in [*ps.logged(), _output(proc)]:
        assert code.strip() not in text and canonical not in text.upper().replace("-", ""), text


def _is_code_ps(ps: _PowerShell, values: list[str], helper_answers: bool = True) -> list[bool]:
    """Test-PairingCode on each value; without ``helper_answers`` the native
    step answers neither 10 nor 11."""
    feed = ps.tmp / "values.txt"
    feed.write_bytes("".join(value + "\n" for value in values).encode("utf-8"))
    prelude = ("" if helper_answers else
               "function Invoke-NativeHelper { $global:LASTEXITCODE = 1 }\n")
    proc = ps.run(prelude + _block("client-only preflight")
                  + f"foreach ($value in [IO.File]::ReadAllLines('{_q(feed)}')) {{\n"
                    "    if (Test-PairingCode $value) { Write-Output CODE } else { Write-Output TOKEN }\n}\n")
    assert proc.returncode == 0, _output(proc)
    return [line == "CODE" for line in proc.stdout.splitlines()]


@pytest.mark.parametrize("helper_answers", [True, False], ids=["client", "own-rule"])
def test_a_pairing_code_is_recognised_exactly_as_pair_reads_one(tmp_path, helper_answers):
    from pseudolife_memory.principals import normalize_pairing_code
    values = CODE_FORMS + NOT_CODES
    assert _is_code_ps(_PowerShell(tmp_path), values, helper_answers) == [
        normalize_pairing_code(value) is not None for value in values]


@pytest.mark.parametrize("form", CODE_FORMS)
def test_a_pasted_pairing_code_is_kept_for_pairing_not_written(tmp_path, form):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=form + "\n")
    assert proc.returncode == 0, _output(proc)
    assert "PAIRING=held" in proc.stdout.splitlines()
    assert not target.exists()
    assert target.parent.is_dir()
    assert any(call.startswith("Invoke-RestMethod|") for call in ps.logged())
    assert "pairing code" in _output(proc)
    _assert_code_unseen(ps, proc, form)


@pytest.mark.parametrize("token", [URLSAFE_TOKEN, FIXTURE_TOKEN])
def test_a_pasted_token_is_still_written_as_the_token(tmp_path, token):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=token + "\n")
    assert proc.returncode == 0, _output(proc)
    assert "PAIRING=" in proc.stdout.splitlines()
    assert CredentialProvider(path=target).snapshot().token == token


@pytest.mark.parametrize("clients,name", [(("claude",), "claude-code"), (("codex",), "codex"),
                                          (("claude", "codex"), "shared")])
def test_a_pairing_code_defaults_to_the_token_file_named_for_the_client(tmp_path, clients, name):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, clients=clients,
                      extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": str(tmp_path / "other.token")})
    assert proc.returncode == 0, _output(proc)
    target = ps.home / ".pseudolife-mcp" / f"{name}.token"
    [line] = [line for line in proc.stdout.splitlines() if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == target
    assert "PAIRING=held" in proc.stdout.splitlines()
    assert target.parent.is_dir() and not target.exists()
    assert any(call.startswith("Invoke-RestMethod|") and f"{REMOTE}/health" in call
               for call in ps.logged())
    _assert_code_unseen(ps, proc)


def test_a_given_token_file_overrides_the_pairing_default(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "laptop.token"
    proc = _preflight(ps, pairing_code=PAIRING_CODE, token_file=str(target))
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_FILE={target}" in proc.stdout.splitlines()
    assert target.parent.is_dir() and not target.exists()


def test_a_pairing_code_refuses_an_existing_token_file_before_pair_runs(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    before = token.read_bytes()
    proc = _preflight(ps, pairing_code=PAIRING_CODE, token_file=str(token),
                      after=_pairing_step(ps), extra_env=_pair_env())
    assert proc.returncode == 1
    assert "already exists" in _output(proc) and "remote.token" in _output(proc)
    assert token.read_bytes() == before
    assert _shim_calls(ps) == []
    assert not any(call.startswith("Invoke-RestMethod|") for call in ps.logged())
    _assert_code_unseen(ps, proc)


def test_a_pairing_code_is_redeemed_on_stdin_and_then_used_as_the_token_file(tmp_path):
    """-DaemonUrl <remote> -PairingCode <code>, non-interactive: the mode
    block, the preflight, then pair with the code on stdin and never in an
    argument list, then the token file checked and handed to connect."""
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, client_only=False, clients=("codex",),
                      after=_pairing_step(ps), extra_env=_pair_env())
    assert proc.returncode == 0, _output(proc)
    target = ps.home / ".pseudolife-mcp" / "codex.token"
    assert _shim_calls(ps) == [
        f"shim|pair {REMOTE} --read-code --token-file {target} --json",
        f"shim|connect {REMOTE} --token-file {target} --client codex --dry-run --json"]
    assert _pair_stdin(ps).strip() == PAIRING_CODE
    assert CredentialProvider(path=target).snapshot().token == PAIRED_TOKEN
    assert f"PAIRED={target}|" in proc.stdout.splitlines()
    assert "fixture-laptop" in _output(proc)
    _assert_code_unseen(ps, proc)
    assert PAIRED_TOKEN not in _output(proc)


def test_a_pasted_pairing_code_reaches_pair_as_it_was_typed(tmp_path):
    ps = _PowerShell(tmp_path)
    target = tmp_path / "keys" / "new.token"
    proc = _preflight(ps, token_file=str(target), read_token=True, feed=" oil2-mx4p-9tzc \n",
                      after=_pairing_step(ps, connect=False), extra_env=_pair_env())
    assert proc.returncode == 0, _output(proc)
    assert _shim_calls(ps) == [f"shim|pair {REMOTE} --read-code --token-file {target} --json"]
    assert _pair_stdin(ps).strip() == "oil2-mx4p-9tzc"
    assert CredentialProvider(path=target).snapshot().token == PAIRED_TOKEN
    _assert_code_unseen(ps, proc, "oil2-mx4p-9tzc")


def test_pairing_runs_even_with_no_client_for_connect(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, clients=("generic",),
                      after=_pairing_step(ps), extra_env=_pair_env())
    assert proc.returncode == 0, _output(proc)
    assert [call.split()[0] for call in _shim_calls(ps)] == ["shim|pair"]


def test_a_refused_pairing_stops_the_install(tmp_path):
    ps = _PowerShell(tmp_path)
    report = _pair_report("refused", 4, "fixture-refusal: the daemon refused the pairing code")
    proc = _preflight(ps, pairing_code=PAIRING_CODE, after=_pairing_step(ps),
                      extra_env=_pair_env(pair_exit=4, report=report))
    assert proc.returncode == 1
    assert "pseudolife-mcp pair exited 4" in _output(proc)
    assert "fixture-refusal: the daemon refused the pairing code" in _output(proc)
    assert not any(line.startswith("PAIRED=") for line in proc.stdout.splitlines())
    assert [call.split()[0] for call in _shim_calls(ps)] == ["shim|pair"]
    assert not (ps.home / ".pseudolife-mcp" / "claude-code.token").exists()
    _assert_code_unseen(ps, proc)


def test_an_unknown_pairing_outcome_names_the_kept_token_file(tmp_path):
    ps = _PowerShell(tmp_path)
    report = _pair_report("unknown", 5, "fixture-unknown: no answer after the retries")
    proc = _preflight(ps, pairing_code=PAIRING_CODE, after=_pairing_step(ps),
                      extra_env=_pair_env(pair_exit=5, report=report))
    assert proc.returncode == 1
    kept = ps.home / ".pseudolife-mcp" / "claude-code.token"
    assert "pseudolife-mcp pair exited 5" in _output(proc)
    assert "fixture-unknown: no answer after the retries" in _output(proc)
    assert f"kept at {kept}" in _output(proc)
    assert "may hold a token the daemon now accepts" in _output(proc)
    assert [call.split()[0] for call in _shim_calls(ps)] == ["shim|pair"]
    _assert_code_unseen(ps, proc)


def test_pairing_without_a_shim_stops_the_install(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, after=_pairing_step(ps, shim_ok=False),
                      extra_env=_pair_env())
    assert proc.returncode == 1
    assert "pairing needs the pseudolife-mcp shim" in _output(proc)
    assert _shim_calls(ps) == []
    _assert_code_unseen(ps, proc)


def test_the_paired_token_file_is_checked_as_a_given_one_is(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, pairing_code=PAIRING_CODE, after=_pairing_step(ps),
                      extra_env=_pair_env(plain=True))
    assert proc.returncode == 1
    assert "the shim cannot use the token file" in _output(proc)
    assert [call.split()[0] for call in _shim_calls(ps)] == ["shim|pair"]


def _codex_steps_ps(credentials: str) -> str:
    """The native client's Codex steps, canned, after native_stub.POWERSHELL;
    every other step goes to the stand-in. ``credentials`` is the body of the
    codex-credentials step, which the installer always pipes the token to
    here."""
    return (native_stub.HELPER_PS.replace("function Invoke-NativeHelper",
                                          "function Invoke-StandinHelper")
            + "function Invoke-NativeHelper {\n"
              "    $global:LASTEXITCODE = 0\n"
              "    if ($args[0] -eq 'codex-credentials') {\n"
              f"        {credentials}\n"
              "    }\n"
              "    if ($args[0] -eq 'codex-runtime-defaults') {\n"
              "        return '{\"status\":\"ready\",\"runtime_defaults\":\"configured\"}'\n"
              "    }\n"
              "    if ($args[0] -eq 'codex-hooks') {\n"
              "        return '{\"status\":\"ready\",\"source\":\"manual\",\"instructions\":\"covered-by-hooks\",\"recovery\":null}'\n"
              "    }\n"
              "    if ($MyInvocation.ExpectingInput) { $input | Invoke-StandinHelper @args }\n"
              "    else { Invoke-StandinHelper @args }\n"
              "}\n")


def test_codex_credentials_come_from_the_token_file_and_the_remote_url(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    captured = tmp_path / "helper.txt"
    stages = re.search(r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# -- 10\. standing", (ROOT / INSTALL).read_text(encoding="utf-8"))[1]
    steps = _codex_steps_ps(
        "$stdin = @($input) -join ''\n"
        f"        Set-Content -LiteralPath '{_q(captured)}' -Value (\"ARGS=\" + (@($args) -join ' ') + \"`nSTDIN=\" + $stdin)\n"
        "        return ('{\"status\":\"ready\",\"credential_file_configured\":true,\"connection_configured\":true,' +\n"
        f"                '\"credential_file_path\":\"/fixture/codex/token\",\"daemon_url\":\"{REMOTE}\"}}')")
    body = f"""$clients = @('codex'); $CodexHooks = 'auto'; $CodexHookTrust = 'yes'
$Instructions = 'auto'; $ClaudeMd = ''; $interactive = $false
$ClientOnly = [switch]$true; $DaemonUrl = '{REMOTE}'; $TokenFile = '{_q(token)}'
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_TOKEN') {{ return 'fixture-local-token' }}
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return 'http://127.0.0.1:8765' }}
    return $null
}}
$env:PATH = '{_q(tmp_path)}'
$env:FAKE_LAUNCHER = '{_q(tmp_path / "native" / ("pseudolife-mcp.cmd" if os.name == "nt" else "pseudolife-mcp"))}'
$env:FAKE_LAUNCHER_SH = @'
{native_stub.LAUNCHER_SH}
'@
{native_stub.POWERSHELL}
{steps}
{stages}
Write-Output "URL=$codexCredentialUrl"
"""
    proc = ps.run(body)
    assert proc.returncode == 0, _output(proc)
    helper = captured.read_text(encoding="utf-8")
    assert "--installer-token-stdin" in helper
    assert f"--installer-daemon-url {REMOTE}" in helper
    assert f"STDIN={FIXTURE_TOKEN}" in helper
    assert "fixture-local-token" not in helper
    assert FIXTURE_TOKEN not in _output(proc)
    assert f"URL={REMOTE}" in proc.stdout.splitlines()


CRED_READY = ('{"status":"ready","credential_file_configured":true,"connection_configured":true,'
              '"credential_file_path":"/fixture/codex/token","daemon_url":"%s"}')


CODEX_STUB = """$global:LASTEXITCODE = 0
    $callArgs = @($args)
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq 'get')) {
        if ($env:FAKE_EXISTING) { return $env:FAKE_EXISTING }
        $global:LASTEXITCODE = 1
        return
    }
    if ($callArgs -contains '--help') { return '  --env <KEY=VALUE>' }"""


def _codex_wiring(ps: _PowerShell, *, cred: str | None = None,
                  cred_exit: int = 0, native_exit: int = 0, existing: str = "",
                  requested: str | None = None, add_exit: int = 0, missing: bool = False,
                  runtime_failed: bool = False, gemini_list: str = "",
                  show_ladder: bool = False, transport: str = "shim", hook_report: str = "",
                  hook_trust: str = "yes", hook_interactive: bool = False, hook_exit: int = 0):
    """Sections 9 to 11 for a Codex-only client-only install, with canned
    Codex steps, the native client (``native_exit`` is its install result)
    and a stub codex. Returns the run and the codex adds."""
    token = _token(ps)
    installed = ps.tmp / "installed-bin"
    installed.mkdir()
    launcher = installed / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    launcher.write_text("placeholder\n", encoding="utf-8")
    if "FAKE_INSTALL_BIN" in ps.env:  # a test's own launcher that answers connect
        launcher = Path(ps.env["FAKE_INSTALL_BIN"]) / "pseudolife-mcp.ps1"
    source = (ROOT / INSTALL).read_text(encoding="utf-8")
    stages = re.search(r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health", source)[1]
    ladder = re.search(r"(?ms)^# -- 13\. per-provider wiring ladder[^\n]*\n(.*?)"
                       r"^# One line for the agent board", source)[1] if show_ladder else ""
    steps = _codex_steps_ps(
        "$null = @($input); $global:LASTEXITCODE = [int]$env:FAKE_CRED_EXIT; return $env:FAKE_CRED")
    if runtime_failed:
        steps = steps.replace(
            """return '{"status":"ready","runtime_defaults":"configured"}'""",
            """$global:LASTEXITCODE = 1; return '{"status":"needs-configuration"}'""",
        )
    if hook_report:
        steps = steps.replace(
            """return '{"status":"ready","source":"manual","instructions":"covered-by-hooks","recovery":null}'""",
            f"Add-Content -LiteralPath '{_q(ps.calls)}' -Value ('hook-options|' + ($args -join ' ')); "
            f"$global:LASTEXITCODE = {hook_exit}; "
            "if ($LASTEXITCODE -and $PSNativeCommandUseErrorActionPreference) { throw 'fixture native error promotion' }; "
            "return $env:FAKE_HOOK_REPORT", 1,
        )
    codex = "" if missing else ps.stub_function("codex", CODEX_STUB + "\n$global:LASTEXITCODE = [int]$env:FAKE_MCP_ADD_EXIT")
    if native_exit and not gemini_list:
        gemini_list = "pseudolife-memory: http://fixture.example/mcp (http)"
    gemini = "" if requested is None else ps.stub_function(
        "gemini", "$global:LASTEXITCODE = 0\n"
        "if ($args -contains 'list') { $env:FAKE_GEMINI_LIST }\n"
        "if ($args -contains '--help') { '  -e, --env' }")
    providers = "'codex'" if requested in (None, "codex") else "'codex','gemini'"
    finish_fixture = "" if requested is None else (
        "Write-Output ('MCP_GEMINI=' + $mcpState['gemini'])\n"
        "Write-Output 'REMAINING_STEPS_FINISHED'\n"
        + _block("client setup result")
    )
    body = f"""$env:PATH = '{_q(ps.tmp)}'
$clients = @({providers}); $CodexHooks = 'auto'; $CodexHookTrust = '{hook_trust}'
$requestedClients = '{requested or ''}' -split ' '
$Instructions = '{'skip' if hook_interactive else 'auto'}'; $ClaudeMd = ''; $AgentsFile = ''
$interactive = {'$true' if hook_interactive else '$false'}
$PSNativeCommandUseErrorActionPreference = {'$true' if hook_exit else '$false'}
$Transport = '{transport}'
$ClientOnly = [switch]$true; $DaemonUrl = '{REMOTE}'; $TokenFile = '{_q(token)}'
function Get-EnvValue($name) {{ return $null }}
{native_stub.POWERSHELL}
{steps}
{codex}
{gemini}
{stages}
{ladder}
Write-Output ("MCP_CODEX=" + $mcpState['codex'])
{finish_fixture}
"""
    proc = ps.run(body, extra_env={
        "FAKE_CRED": cred if cred is not None else CRED_READY % REMOTE,
        "FAKE_CRED_EXIT": str(cred_exit), "FAKE_NATIVE_EXIT": str(native_exit),
        "FAKE_EXISTING": existing, "FAKE_MCP_ADD_EXIT": str(add_exit),
        "FAKE_GEMINI_LIST": gemini_list, "FAKE_HOOK_REPORT": hook_report,
        "FAKE_LAUNCHER": str(launcher), "FAKE_KEEP_LAUNCHER": "1"})
    adds = [call for call in ps.logged()
            if call.startswith("codex|mcp add") and "--help" not in call]
    return proc, adds


def test_codex_registers_with_the_remote_url_and_its_credential_file(tmp_path):
    proc, adds = _codex_wiring(_PowerShell(tmp_path))
    assert proc.returncode == 0, _output(proc)
    [add] = adds
    assert f"PSEUDOLIFE_MCP_DAEMON_URL={REMOTE}" in add
    assert "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/codex/token" in add
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in add
    assert "MCP_CODEX=shim-env" in proc.stdout.splitlines()


@pytest.mark.parametrize("kwargs,reason", COMPLETION_CASES)
@pytest.mark.parametrize("requested,status", [("codex gemini", 5), ("", 0)])
def test_incomplete_client_leaves_other_steps_running(tmp_path, kwargs, reason, requested, status):
    proc, _ = _codex_wiring(_PowerShell(tmp_path), requested=requested, **kwargs)
    assert proc.returncode == status, _output(proc)
    other_state = "present" if kwargs.get("native_exit") else "shim-env"
    assert f"MCP_GEMINI={other_state}" in proc.stdout
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    label = "Requested" if requested else "Detected"
    reason = reason.replace("drop it from --client", "drop it from -Client")
    reason = reason.replace("the existing Codex registration was preserved", "The existing Codex registration was preserved")
    assert proc.stderr.splitlines()[-1] == f"{label} client setup incomplete (Codex). {reason}"
    assert FIXTURE_TOKEN not in _output(proc)


def test_malformed_gemini_listing_reports_only_that_client(tmp_path):
    proc, _ = _codex_wiring(
        _PowerShell(tmp_path), requested="codex gemini", gemini_list="pseudolife-memory")
    assert proc.returncode == 5, _output(proc)
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
             "Rerun the installer with -Transport shim."),
])
def test_bearer_transport_failure_names_the_right_repair(tmp_path, transport, repair):
    proc = _PowerShell(tmp_path).run(
        f"$Transport = '{transport}'\n"
        "$clients = @('claude'); $requestedClients = $clients\n"
        "$hookState = @{}; $instrState = @{}; $mcpState = @{}\n"
        + _block("client registration state") + _block("client registration helpers")
        + 'Write-ClientHttpAuthResult "claude"\n' + _block("client setup result"))
    assert proc.returncode == 5, _output(proc)
    assert proc.stderr.splitlines()[-1] == f"Requested client setup incomplete (Claude Code). {repair}"


def test_codex_refuses_explicit_http_when_authenticated(tmp_path):
    proc, adds = _codex_wiring(
        _PowerShell(tmp_path), requested="codex", transport="http", native_exit=7)
    assert proc.returncode == 5, _output(proc)
    assert not adds
    assert proc.stderr.splitlines()[-1] == (
        "Requested client setup incomplete (Codex). Codex authentication requires the stdio shim. "
        "HTTP transport was not registered. Rerun the installer with -Transport shim."
    )


@pytest.mark.parametrize("kwargs", [{}, {"runtime_failed": True},
                                    {"existing": "pseudolife-memory\ncommand: custom-shim\n{PSEUDOLIFE_MCP_NO_SPAWN=1}"}])
def test_successful_client_setup_and_rerun_exit_zero(tmp_path, kwargs):
    proc, _ = _codex_wiring(_PowerShell(tmp_path), requested="codex gemini", show_ladder=True, **kwargs)
    assert proc.returncode == 0, _output(proc)
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
@pytest.mark.parametrize("failure", ['throw "fixture hook write failure"', "exit 19"])
@pytest.mark.parametrize("requested", [True, False], ids=["requested", "detected"])
def test_a_claude_hook_write_failure_is_reported_at_completion(tmp_path, deferred, failure, requested):
    ps = _PowerShell(tmp_path)
    (tmp_path / "install-hook.ps1").write_text(failure + "\n", encoding="utf-8")
    block = _block("client-only claude hook") if deferred else _between(
        INSTALL, '    Step "Installing $selectedClient session hook..."', "\n}")
    proc = ps.run(
        f"$clients = @('claude'); $requestedClients = {'$clients' if requested else '@()'}\n"
        "$hookState = @{claude = 'deferred'}; $instrState = @{}\n"
        "$selectedClient = 'claude'; $briefingCommand = 'fixture'\n"
        "$script:shimInstallPath = '/fixture/shim.exe'\n"
        "function Install-ShimOnce { return $true }\n"
        + _block("client registration state") + block
        + "\nWrite-Output 'CLIENT_SETUP_CONTINUED'\n" + _block("client setup result")
    )
    assert proc.returncode == (5 if requested else 0), _output(proc)
    assert "CLIENT_SETUP_CONTINUED" in proc.stdout
    lines = proc.stderr.splitlines()
    label = "Requested" if requested else "Detected"
    assert lines[-1] == (
        f"{label} client setup incomplete (Claude Code). "
        "Hooks were not installed. Fix the hook error above and rerun the installer."
    )
    if not requested:
        assert lines[-2] == "Warnings"
    assert "fixture hook write failure" not in _output(proc)


@pytest.mark.parametrize("hook_status,recovery,reason", CODEX_HOOK_REPORTS)
@pytest.mark.parametrize("requested", ["codex gemini", ""], ids=["requested", "detected"])
def test_codex_hook_failure_is_reported_at_completion(tmp_path, hook_status, recovery, reason, requested):
    report = json.dumps(dict(status=hook_status, source="manual", instructions="present", recovery=recovery))
    proc, adds = _codex_wiring(_PowerShell(tmp_path), requested=requested, hook_report=report)
    assert proc.returncode == (5 if requested else 0), _output(proc)
    assert adds and "MCP_GEMINI=shim-env" in proc.stdout
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert recovery in proc.stdout
    label = "Requested" if requested else "Detected"
    assert proc.stderr.splitlines()[-1] == f"{label} client setup incomplete (Codex). {reason}"
    if not requested:
        assert "Warnings" in proc.stderr.splitlines()


@pytest.mark.parametrize("trust,interactive,recovery", CODEX_PENDING_CASES)
def test_pending_codex_hooks_warn_without_failing(tmp_path, trust, interactive, recovery):
    ps = _PowerShell(tmp_path)
    report = json.dumps(dict(status="pending", source="manual", instructions="present", recovery=recovery))
    proc, adds = _codex_wiring(ps, requested="codex gemini", hook_report=report,
                               hook_trust=trust, hook_interactive=interactive, hook_exit=1)
    assert proc.returncode == 0, _output(proc)
    assert adds and "REMAINING_STEPS_FINISHED" in proc.stdout
    [hook_args] = [line for line in ps.logged() if line.startswith("hook-options|")]
    assert f" --trust {trust} " in hook_args
    assert ("--non-interactive" in hook_args) is not interactive
    reason = CODEX_HOOK_APPROVAL if recovery.startswith("PseudoLife") else (
        "Hooks are not installed. Rerun the installer with -CodexHookTrust yes."
    )
    assert proc.stderr.splitlines()[-2:] == ["Warnings", "Codex hook setup needs attention. " + reason]


def test_codex_hook_and_connection_failures_are_reported_separately(tmp_path):
    report = json.dumps(dict(status="unavailable", source="manual", instructions="present",
                             recovery="The fixture could not write hooks."))
    proc, _ = _codex_wiring(_PowerShell(tmp_path), requested="codex gemini", hook_report=report, add_exit=17)
    assert proc.returncode == 5, _output(proc)
    assert proc.stderr.splitlines()[-2:] == [
        "Requested client setup incomplete (Codex). Hooks could not be set up. "
        "Fix the hook problem reported above and rerun the installer.",
        "Requested client setup incomplete (Codex). MCP registration failed (exit 17). "
        "Fix the CLI error above and rerun the installer.",
    ]


def test_requested_codex_instruction_failure_is_a_warning(tmp_path):
    report = json.dumps({
        "status": "ready", "source": "manual", "instructions": "skipped",
        "instructions_error": {"file": "fixture-AGENTS.md", "cause": "fixture write failed"},
        "recovery": "Standing instructions could not be written: fixture write failed",
    })
    proc, adds = _codex_wiring(_PowerShell(tmp_path), requested="codex gemini", hook_report=report)
    assert proc.returncode == 0, _output(proc)
    assert adds and "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
    assert proc.stderr.splitlines()[-2:] == [
        "Warnings", "Codex standing instructions could not be written. Check the Codex home and file permissions.",
    ]


@pytest.mark.parametrize("accepted", [True, False], ids=["accepted", "rejected"])
def test_current_credential_probe_in_native_powershell(tmp_path, accepted):
    ps = _PowerShell(tmp_path)
    recovery = ("The installer credential was not accepted by the configured daemon. "
                "Supply a token accepted by that daemon, then rerun the installer.")
    report = json.dumps({"status": "ready"} if accepted else {"status": "failed", "recovery": recovery})
    plan = json.dumps({"exit": 0, "rows": [
        {"client": "codex", "place": "registration", "state": "current"}]})
    body = f"""$clients = @('codex', 'gemini')
$requestedClients = $clients
$DaemonUrl = '{REMOTE}'
$TokenFile = 'fixture.token'
$hookState = @{{}}
$instrState = @{{}}
$mcpState = @{{}}
function codex {{ }}
function gemini {{ }}
function Step($message) {{ Write-Output $message | Out-Host }}
function Install-ShimOnce {{ $script:shimInstallPath = 'fake-shim'; return $true }}
function fake-shim {{
    Add-Content -LiteralPath '{_q(ps.calls)}' -Value ('shim|' + ($args -join ' '))
    if ($args -contains '--yes') {{ throw 'A current registration must not be rewritten.' }}
    if ($args -contains 'gemini') {{
        $global:LASTEXITCODE = 3
        return '{{"exit":3,"rows":[]}}'
    }}
    $global:LASTEXITCODE = 0
    return '{plan}'
}}
function Invoke-NativeHelper {{
    if ($args[0] -eq 'connect-report' -and $args[1] -eq 'current') {{
        return 'current'
    }}
    Add-Content -LiteralPath '{_q(ps.calls)}' -Value ('installer|' + ($args -join ' '))
    $global:LASTEXITCODE = {0 if accepted else 1}
    return '{report}'
}}
{_block("client-only connect")}
Invoke-ClientOnlyConnect
if (-not $script:clientSetupFailures['gemini']) {{ Write-Output 'GEMINI_REGISTRAR_ELIGIBLE' }}
{_block("client setup result")}
"""
    proc = ps.run(body)
    assert proc.returncode == (0 if accepted else 5), _output(proc)
    assert proc.stdout.count(
        "Verifying the current credential for codex,gemini. "
        "If the daemon is unreachable, this can take up to 180 seconds."
    ) == 1
    assert "GEMINI_REGISTRAR_ELIGIBLE" in proc.stdout
    assert not any("--yes" in call for call in ps.logged())
    assert [call for call in ps.logged() if call.startswith("installer|")] == [
        f"installer|credentials verify-token-file --daemon-url {REMOTE} --token-file fixture.token"]
    if accepted:
        assert "Nothing to write." in proc.stdout
    else:
        assert proc.stderr.splitlines()[-1] == f"Requested client setup incomplete (Codex). {recovery}"


@pytest.mark.parametrize("outcome,recovery", [
    ("refused", "The installer credential was not accepted by the configured daemon. "
     "Supply a token accepted by that daemon, then rerun the installer."),
    ("not-ready", "The authenticated check at https://daemon.example.com did not succeed "
     "within 180 seconds. The last attempt returned HTTP 503. Check that the daemon is "
     "reachable, then rerun the installer."),
])
def test_codex_credential_bootstrap_reports_failure(tmp_path, outcome, recovery):
    cred = json.dumps({
        "status": "needs-configuration", "credential_check": outcome, "recovery": recovery,
    })
    proc, adds = _codex_wiring(_PowerShell(tmp_path), cred=cred, cred_exit=1)
    assert proc.returncode == 0, _output(proc)
    assert adds == []
    assert "MCP_CODEX=failed" in proc.stdout.splitlines()
    assert recovery in _output(proc)
    assert FIXTURE_TOKEN not in _output(proc)


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
def test_codex_is_never_left_on_another_daemon(tmp_path, scenario, kwargs, says):
    proc, adds = _codex_wiring(_PowerShell(tmp_path), **kwargs)
    assert proc.returncode == 0, _output(proc)
    assert adds == [], scenario
    assert "MCP_CODEX=failed" in proc.stdout.splitlines(), scenario
    assert says in _output(proc), scenario


class _Daemon(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        _Daemon.seen.append(f"{self.path}|{self.headers.get('Authorization', '')}")
        if self.path == "/health":
            body = HEALTH_ON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"check-in"
            self.send_response(200)
            self.send_header("X-PL-Board", "on")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test log
        pass


@pytest.fixture
def daemon_server():
    _Daemon.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Daemon)
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
    # ops/scheduled_task_lock.ps1: the native installer dot-sources it at stage 12a
    # (the scheduled-task owner helpers) on every run. The installer only
    # runs from a checkout, where the file is always beside it.
    for rel in ("ops/install-native.ps1", "ops/install-hook.ps1", "ops/scheduled_task_lock.ps1",
                "ops/client_credentials.py", "ops/.env.example", "examples/CLAUDE.memory.md",
                "pyproject.toml"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    return repo


def _script_stub(bin_dir: Path, calls: Path, name: str, body: str = "exit 0") -> None:
    (bin_dir / f"{name}.ps1").write_text(
        f"Add-Content -LiteralPath '{_q(calls)}' -Value ('{name}|' + (@($args) -join ' '))\n"
        + body + "\n", encoding="utf-8")


FRESH_CONNECT = '{"url": null, "rows": [], "exit": 3, "error": "none"}'


def _whole_client_only_run(tmp_path, daemon_server, *, connect_report: str = FRESH_CONNECT, connect_exit: int = 3,
                           gemini_body: str | None = None, current_recovery: str = ""):
    """The whole client-only run, with a configurable Claude Code connect result.
    ``gemini_body`` replaces the gemini CLI stub's answers."""
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    repo = _copy_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    shim = installed / "pseudolife-mcp"
    # The release's client answers connect's dry run as the real one does on
    # a fresh machine (its report of no registration, exit 3), and its
    # installer steps through the stand-in, which runs this checkout's
    # helpers as the native client carries its own.
    standin = ROOT / "tests" / "native_helper_standin.py"
    verify = ""
    if current_recovery:
        report = json.dumps({"status": "failed", "recovery": current_recovery})
        verify = (
            "if [ \"$1 $2 $3\" = 'installer connect-report current' ]; then cat >/dev/null; echo current; exit 0; fi\n"
            "if [ \"$1 $2 $3\" = 'installer credentials verify-token-file' ]; then\n"
            f"  printf 'shim|%s\\n' \"$*\" >>'{ps.calls}'; echo '{report}'; exit 1\nfi\n"
        )
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    release_env = native_stub.write_release(
        tmp_path / "release", version,
        verify + "if [ \"$1\" = installer ] && [ \"$2\" != migrate-registrations ]; then\n"
        f"  shift; exec '{sys.executable}' '{standin}' '{ROOT}' \"$@\"\nfi\n"
        f"printf 'shim|%s\\n' \"$*\" >>'{ps.calls}'\n"
        "if [ \"$1\" = connect ]; then\n"
        f"  case \" $* \" in *' --client gemini '*) echo '{FRESH_CONNECT}'; exit 3 ;; esac\n"
        f"  echo '{connect_report}'; exit {connect_exit}\nfi\n"
        "exit 0\n")
    calls = ps.calls
    # docker and codex must not run; neither may any Python tooling (the
    # installer runs every step through the native client).
    for name in ("docker", "codex", "python", "python3", "py", "pip", "pip3", "pipx"):
        _script_stub(fake_bin, calls, name, "exit 99")
    _script_stub(fake_bin, calls, "claude",
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'get')) { exit 1 }\n"
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0")
    _script_stub(fake_bin, calls, "gemini", gemini_body or (
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0"))
    # An earlier local install left its own file and loopback URL for the hooks.
    settings_path = ps.home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {
        "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/local/claude-code.token",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}}), encoding="utf-8")
    env = dict(ps.env)
    system_dirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] \
        if os.name == "nt" else ["/usr/bin", "/bin"]
    env.update(release_env)
    env.update({"PSEUDOLIFE_SHIM_RUNTIMES": str(installed / "runtimes"),
                "PSEUDOLIFE_SHIM_LAUNCHER": str(shim)})
    env.update({"PATH": os.pathsep.join([str(fake_bin), *system_dirs]),
                "NO_COLOR": "1", "TERM": "dumb",
                # The installer's own environment carries an older credential.
                "PSEUDOLIFE_MCP_TOKEN": "fixture-local-literal"})
    with user_path_unchanged(ps.home):
        proc = installer_sandbox.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "install-native.ps1"),
             "-ClientOnly", "-DaemonUrl", daemon_server, "-TokenFile", str(token),
             "-Client", "claude,gemini", "-ClaudePlugin", "skip", "-Instructions", "skip",
             "-NoArt"],
            capture_output=True, text=True, timeout=300, env=env, root=tmp_path, check=False,
            stdin=subprocess.DEVNULL)
    return env, installed, proc, ps, repo, shim, token


@pytest.mark.skipif(os.name == "nt", reason=(
    "the installer runs the staged native client before anything else, and a "
    "Windows stub cannot be a script; test_installer_native_client_ps.py covers "
    "that stage with a real binary"))
def test_a_client_only_install_wires_clients_to_the_remote_daemon(tmp_path, daemon_server):
    env, installed, proc, ps, repo, shim, token = _whole_client_only_run(tmp_path, daemon_server)
    output = _output(proc)
    assert proc.returncode == 0, output
    logged = ps.logged()
    assert not any(call.startswith(("docker|", "codex|")) for call in logged)
    assert not [call for call in logged
                if call.split("|", 1)[0] in ("python", "python3", "py", "pip", "pip3", "pipx")], logged
    assert not (repo / "ops" / ".env").exists()
    assert not (repo / "ops" / "docker-compose.override.yml").exists()
    assert FIXTURE_TOKEN not in output
    [claude] = [call for call in logged if call.startswith("claude|mcp add --scope user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=claude-code", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={daemon_server}"):
        assert f" {pair} " in claude, pair
    [gemini] = [call for call in logged if call.startswith("gemini|mcp add -s user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=gemini", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={daemon_server}"):
        assert f"-e {pair} " in gemini, pair
    settings = json.loads((ps.home / ".claude" / "settings.json").read_text(
        encoding="utf-8-sig"))
    commands = json.dumps(settings.get("hooks", {}))
    # The installed shim by its path: a hook's PATH need not include pipx's.
    briefings = [hook["command"] for group in settings["hooks"]["SessionStart"]
                 for hook in group["hooks"]
                 if hook["command"].endswith(" briefing --hook-json")]
    assert [Path(command.rsplit(" briefing", 1)[0]) for command in briefings] == [
        installed / "pseudolife-mcp"]
    assert "docker exec" not in commands
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == daemon_server
    assert Path(settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token
    assert "fixture-local-literal" not in output
    assert list((ps.home / ".claude").glob("settings.json.bak-pseudolife-*"))
    assert f"remote at {daemon_server}" in proc.stdout
    assert re.search(r"\[x\] Agent board\s+on", proc.stdout)
    assert any(seen.endswith(f"Bearer {FIXTURE_TOKEN}") for seen in _Daemon.seen)
    assert "Waiting for the daemon" not in proc.stdout
    connects = [call for call in ps.logged() if call.startswith("shim|connect")]
    if os.name == "nt":
        assert connects == [] and "not checked" in output
    else:
        assert [call.split("--client ", 1)[1] for call in connects] == ["claude-code,gemini --dry-run --json"]
        assert "not checked" not in output


@pytest.mark.skipif(os.name == "nt", reason=(
    "the installer runs the staged native client before anything else, and a "
    "Windows stub cannot be a script; test_installer_native_client_ps.py covers "
    "that stage with a real binary"))
def test_client_only_connect_failure_keeps_the_native_client_and_reports_partial_setup(tmp_path, daemon_server):
    env, installed, proc, ps, repo, shim, token = _whole_client_only_run(tmp_path, daemon_server, connect_report='{"url": null, "rows": [], "exit": 1, "error": "the fixture refuses"}', connect_exit=1)
    output = _output(proc)
    assert proc.returncode == 5, output
    assert "could not check the existing registration" in output and "the fixture refuses" in output
    assert "Nothing was changed" not in output and "nothing was changed" not in output
    assert "stopped part way" not in output
    assert proc.stderr.splitlines()[-1] == (
        "Requested client setup incomplete (Claude Code). pseudolife-mcp connect could not check the existing "
        "registration (exit 1: the fixture refuses). Fix the reported problem and rerun the installer."
    )
    assert any(call.startswith("gemini|mcp add -s user") for call in ps.logged())
    assert shim.is_file()
    settings = json.loads((ps.home / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:8765"
    assert settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] == "/fixture/local/claude-code.token"


@pytest.mark.skipif(os.name == "nt", reason="the staged native fixture is a POSIX launcher script")
def test_client_only_current_registration_with_refused_token_is_incomplete(tmp_path, daemon_server):
    plan = json.dumps({"rows": [{"place": "registration", "client": "claude-code", "state": "current"}], "exit": 0})
    _, _, proc, ps, _, _, _ = _whole_client_only_run(
        tmp_path, daemon_server, connect_report=plan, connect_exit=0, current_recovery=DENIED_CREDENTIAL)
    assert proc.returncode == 5, _output(proc)
    assert proc.stderr.splitlines()[-1] == "Requested client setup incomplete (Claude Code). " + DENIED_CREDENTIAL
    calls = ps.logged()
    assert len([call for call in calls if call.startswith("shim|installer credentials verify-token-file")]) == 1
    assert not any(call.startswith("shim|connect") and "--yes" in call for call in calls)
    assert any(call.startswith("gemini|mcp add -s user") for call in calls)


@pytest.mark.skipif(os.name == "nt", reason=(
    "the installer runs the staged native client before anything else, and a "
    "Windows stub cannot be a script; test_installer_native_client_ps.py covers "
    "that stage with a real binary"))
def test_a_cli_exception_is_reported_as_an_incomplete_requested_client(tmp_path, daemon_server):
    env, installed, proc, ps, repo, shim, token = _whole_client_only_run(
        tmp_path, daemon_server,
        gemini_body="if ($args -contains '--help') { throw 'the fixture gemini CLI broke' }\nexit 0")
    output = _output(proc)
    assert proc.returncode == 5, output
    assert "the fixture gemini CLI broke" in output
    assert "Nothing was changed" not in output and "nothing was changed" not in output
    assert "stopped part way" not in output
    assert proc.stderr.splitlines()[-1] == (
        "Requested client setup incomplete (Gemini CLI). MCP setup failed. "
        "the fixture gemini CLI broke Fix the reported problem and rerun the installer."
    )
