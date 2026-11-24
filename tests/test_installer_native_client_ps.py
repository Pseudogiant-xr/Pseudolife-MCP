"""The PowerShell installer installs the native client from the release.

These dot-source the installer's native-client block and shim helpers
(``ops/install-native.ps1``) in ``pwsh`` with a throwaway home and a fake release
directory (``PSEUDOLIFE_NATIVE_ARCHIVE_URL``). The refusal tests run
everywhere. The install tests need an executable client: the built one
named by ``PSEUDOLIFE_NATIVE_TEST_BINARY`` when it is set (required on
Windows, where a stub cannot be a script; they skip there without one), else
off Windows the shell stub. Tests whose assertions depend on a ``STUB_*``
answer always pack the stub. ``python``, ``py``, ``pip`` and ``pipx`` on PATH
are stubs that fail the test if anything calls them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import tarfile
import zipfile

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.installer_protocol import LEVEL

from tests import installer_sandbox
from tests import native_client_stub


ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.8.7"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="needs pwsh")
WINDOWS = os.name == "nt"
_REAL = native_client_stub.real_binary()
REAL = str(_REAL) if _REAL else ""
# The client the install tests run: the built one when it is named, else the
# shell stub (off Windows). A test whose assertion depends on a STUB_* answer
# packs "stub" instead, whatever is named.
INSTALL = "real" if REAL else "stub"


def _asset(client: str) -> str:
    # Named for the target Get-NativeTarget asks for on this host; the shell
    # stub runs on any of them, a real client only on its own.
    assert client in ("stub", "real"), client
    target = ("x86_64-pc-windows-msvc" if WINDOWS
              else native_client_stub.host_target() or "x86_64-unknown-linux-gnu")
    return f"pseudolife-mcp-{VERSION}-{target}." + ("zip" if WINDOWS else "tar.gz")


ASSET = _asset(INSTALL)
needs_client = pytest.mark.skipif(
    WINDOWS and not (REAL and Path(REAL).is_file()),
    reason="on Windows set PSEUDOLIFE_NATIVE_TEST_BINARY to a built native pseudolife-mcp.exe",
)

STUB = r'''#!/bin/sh
printf '%s\n' "$*" >>"$NATIVE_CALLS"
case "$1" in
    version) [ -z "$STUB_HANG_VERSION" ] || sleep 600; echo "pseudolife-mcp 9.8.7" ;;
    runtime)
        if [ "$2" = install ] && [ "$3" = --check ]; then
            if [ -n "$STUB_REFUSE_TUNNEL" ]; then
                echo "runtime install refused: this machine stays on the Python client: a tunnel is registered to start by itself" >&2
                exit 3
            fi
            exit 0
        fi
        if [ -n "$STUB_REFUSE_TUNNEL_LATE" ]; then
            echo "runtime install refused: this machine stays on the Python client: a tunnel is registered to start by itself" >&2
            exit 3
        fi
        mkdir -p "$(dirname "$PSEUDOLIFE_SHIM_LAUNCHER")"
        cp "$0" "$PSEUDOLIFE_SHIM_LAUNCHER"; chmod 755 "$PSEUDOLIFE_SHIM_LAUNCHER"
        echo "$PSEUDOLIFE_SHIM_LAUNCHER" ;;
    update) [ "$2" = --native-capability ] && echo "pseudolife-mcp native-update-v1" ;;
    installer)
        if [ "$2" = --capability ]; then
            [ -z "$STUB_HANG_CAPABILITY" ] || sleep 600
            if [ -n "$STUB_V0240" ]; then
                printf '%s\n' "installer: expected an installer step: migrate-registrations, codex-credentials, codex-runtime-defaults, codex-hooks, claude-desktop" >&2
                exit 2
            fi
            printf '%s\n' "${STUB_CAPABILITY-pseudolife-mcp installer-protocol $STUB_LEVEL}"
        elif [ "$2" = migrate-registrations ]; then echo "$4 already runs the native client; nothing changed"; fi ;;
esac
'''


def _block(start: str, end: str) -> str:
    text = (ROOT / "ops/install-native.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.count(start) == 1, start
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _release(directory: Path, *, client: str, digest_override: str | None = None, omit: bool = False) -> None:
    """A fake release in ``directory``. Every caller names its ``client``, so
    no test inherits the real client by accident: ``"stub"`` is the shell
    stand-in that answers the STUB_* variables, ``"real"`` the built client
    (``PSEUDOLIFE_NATIVE_TEST_BINARY``), packed as a release ships it. On
    Windows a script cannot stand in, so both pack the named client (or a
    placeholder that never runs), and the stub-only tests skip there."""
    if client not in ("stub", "real"):
        raise ValueError(f"client is 'stub' or 'real', not {client!r}")
    assert client == "stub" or REAL, "a real-client release needs PSEUDOLIFE_NATIVE_TEST_BINARY"
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    if WINDOWS:
        data = Path(REAL).read_bytes() if REAL and Path(REAL).is_file() else b"MZ not a program"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("pseudolife-mcp.exe", data)
    else:
        data = (native_client_stub.release_copy(_REAL, directory.parent / "packed").read_bytes()
                if client == "real" else STUB.encode())
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("pseudolife-mcp")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    asset = _asset(client)
    (directory / asset).write_bytes(buffer.getvalue())
    digest = digest_override or hashlib.sha256(buffer.getvalue()).hexdigest()
    (directory / "SHA256SUMS").write_text("" if omit else f"{digest}  {asset}\n", encoding="utf-8")


def _write_crate(repo: Path, manifest: str | None = None) -> None:
    """The checkout's native client crate, with near misses around its
    version: other tables' `version` keys before and after it, an inline
    table, a capitalised key and a comment after the table header."""
    crate = repo / "rust" / "shim" / "Cargo.toml"
    crate.parent.mkdir(parents=True, exist_ok=True)
    if manifest == "":
        crate.unlink(missing_ok=True)
        return
    default = ('[lints]\nversion = "0.0.2"\n[package] # the native client\nname = "pseudolife-stdio"\n'
               f'Version = "0.0.3"\nrmcp = {{ version = "0.0.4" }}\nversion = "{VERSION}"\n\n'
               '[dependencies]\nversion = "0.0.1"\n')
    crate.write_text(manifest if manifest is not None else default, encoding="utf-8", newline="\n")


def _run(client: str, tmp_path: Path, body: str, *, release: bool = True, host_only: bool = False,
         extra_env: dict[str, str] | None = None, manifest: str | None = None,
         **release_kwargs) -> subprocess.CompletedProcess:
    """``body`` run after the installer's native-client block; ``client``
    ("stub" or "real") is the release ``_release`` packs. A host-only
    control runs the body with the same sandbox setup and no installer code."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    # The checkout's release version is its native client crate's
    # (``manifest`` replaces that file's text; ``""`` removes it);
    # pyproject.toml carries a decoy version the installer must not read.
    _write_crate(repo, manifest)
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.0.9"\n', encoding="utf-8")
    base = tmp_path / "release"
    if release:
        _release(base, client=client, **release_kwargs)
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    forbidden = tmp_path / "forbidden-calls"
    for name in ("python", "python3", "py", "pip", "pipx"):
        if WINDOWS:
            (stubs / f"{name}.cmd").write_text(f'@echo {name} %*>>"{forbidden}"\r\n@exit /b 99\r\n', encoding="utf-8")
        else:
            stub = stubs / name
            stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"{forbidden}"\nexit 99\n', encoding="utf-8")
            stub.chmod(0o755)
    runtimes = home / "runtimes"
    launcher = home / "bin" / ("pseudolife-mcp.exe" if WINDOWS else "pseudolife-mcp")
    if not host_only:
        body = "\n".join([
            '$ErrorActionPreference = "Stop"',
            "function Step($msg) { Write-Host \"==> $msg\" }",
            f"$repo = '{repo}'",
            _block("# >>> native client >>>", "# <<< native client <<<"),
            _block("$script:shimInstallResult = $null\n", "\n# Two env pairs ride each shim registration"),
            body,
        ])
    script_file = tmp_path / "run.ps1"
    script_file.write_text(body, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PSEUDOLIFE_", "XDG_"))}
    env.update({
        "PATH": os.pathsep.join([str(stubs), os.environ.get("PATH", "")]),
        "HOME": str(home), "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "Local"), "APPDATA": str(home / "Roaming"),
        "CODEX_HOME": str(home / "codex"), "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "NATIVE_CALLS": str(tmp_path / "native-calls"),
        "STUB_LEVEL": str(LEVEL),
        "PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(base),
        "PSEUDOLIFE_SHIM_RUNTIMES": str(runtimes),
        "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9",
    })
    env.update(extra_env or {})
    result = installer_sandbox.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script_file)],
                                   env=env, root=tmp_path, capture_output=True, text=True, timeout=180)
    assert not forbidden.exists(), f"the installer ran Python tooling: {forbidden.read_text()}"
    return result


@needs_client
def test_fresh_install_and_rerun_use_the_verified_archive(tmp_path):
    body = 'if (Install-ShimOnce) { Write-Host "OK $($script:shimInstallPath)" } else { Write-Host "FAILED" }'
    first = _run(INSTALL, tmp_path, body)
    assert first.returncode == 0, first.stderr + first.stdout
    launcher = tmp_path / "home" / "bin" / ("pseudolife-mcp.exe" if WINDOWS else "pseudolife-mcp")
    assert f"OK {launcher}" in first.stdout, first.stdout
    assert f"Native client {VERSION} installed at" in first.stdout
    downloads = tmp_path / "home" / ".pseudolife-mcp" / "downloads" / VERSION
    assert (downloads / ASSET).is_file() and (downloads / "SHA256SUMS").is_file()
    second = _run(INSTALL, tmp_path, body)
    assert second.returncode == 0, second.stderr + second.stdout
    assert f"Native client {VERSION} already installed at" in second.stdout, second.stdout
    assert "nothing changed" in second.stdout


@pytest.mark.parametrize(("code", "output", "diagnostic"), [
    (-1073741515, "", "Windows could not start the downloaded client because a system library is missing "
     "(exit code -1073741515, 0xC0000135). Install the Microsoft Visual C++ Redistributable for x64 before rerunning "
     "with this archive. To install without the Redistributable, check out a release tag that bundles the Windows C "
     "runtime and rerun that tag's installer with its matching archive."),
    (1, "version probe failed\nmore detail", "the downloaded client does not run on this host "
     "(exit code 1, version probe failed). Check that nothing (an antivirus or an execution policy) blocks it, and rerun."),
])
def test_windows_version_probe_reports_its_exit_and_keeps_only_the_download(tmp_path, code, output, diagnostic):
    control = tmp_path / "host-control"
    control_temp = control / "probe-temp"
    control_temp.mkdir(parents=True)
    probe_temp = tmp_path / "probe-temp"
    probe_temp.mkdir()
    body = '''
function Get-NativeTarget { 'x86_64-pc-windows-msvc' }
function Invoke-NativeBounded([string]$Exe, [string[]]$Arguments) {
    if (($Arguments -join ' ') -ne 'version') { throw "Unexpected probe $Arguments" }
    [pscustomobject]@{ TimedOut = $false; Code = [int]$env:STUB_VERSION_CODE; Out = ''; Err = $env:STUB_VERSION_OUTPUT }
}
Install-ShimOnce | Out-Null
Write-Host 'INSTALLED'
'''
    # Only the process result is stubbed. Stage and verify a real ZIP on
    # both platforms so the refusal also has to remove the staging directory.
    assets = tmp_path / "release"
    assets.mkdir()
    name = f"pseudolife-mcp-{VERSION}-x86_64-pc-windows-msvc.zip"
    archive = assets / name
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("pseudolife-mcp.exe", b"version probe stand-in")
    (assets / "SHA256SUMS").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {name}\n", encoding="utf-8")
    probe_env = {"STUB_VERSION_CODE": str(code), "STUB_VERSION_OUTPUT": output}
    temp_keys = ("TEMP", "TMP", "TMPDIR")
    host = _run("stub", control, "$null = 0", release=False, host_only=True, extra_env={
        **probe_env, **dict.fromkeys(temp_keys, str(control_temp)),
    })
    assert host.returncode == 0, host.stdout + host.stderr
    result = _run("stub", tmp_path, body, release=False, extra_env={
        **probe_env, **dict.fromkeys(temp_keys, str(probe_temp)),
    })
    home = tmp_path / "home"
    downloads = home / ".pseudolife-mcp" / "downloads" / VERSION
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INSTALLED" not in result.stdout
    assert result.stderr.strip() == (
        f"The native pseudolife-mcp client cannot be installed: {diagnostic} "
        f"Nothing else was changed: the download in {downloads} is kept for a rerun (delete it to free the space)."
    )
    # Subtract exact host-only entries. Extra files inside an existing host
    # directory still show up in the installer's remainder.
    host_home = control / "home"
    host_entries = {path.relative_to(host_home) for path in host_home.rglob("*")}
    installed_entries = {path.relative_to(home) for path in home.rglob("*")}
    assert installed_entries - host_entries == {
        path.relative_to(home)
        for path in (downloads / name, downloads / "SHA256SUMS", downloads, downloads.parent, downloads.parent.parent)
    }
    assert {path.relative_to(probe_temp) for path in probe_temp.rglob("*")} <= {
        path.relative_to(control_temp) for path in control_temp.rglob("*")
    }


def test_a_registered_tunnel_service_refuses_before_anything_changes(tmp_path):
    # On Unix the real bounded probe launches the staged shell fixture, whose
    # runtime install --check exits 3. Windows needs an in-process stand-in
    # because a shell script cannot be the staged Windows executable.
    body = 'Install-ShimOnce | Out-Null; Write-Host "INSTALLED"'
    if WINDOWS:
        body = '''
function Invoke-NativeBounded([string]$Exe, [string[]]$Arguments) {
    switch ($Arguments -join ' ') {
        'version' { @{ Code = 0; Out = 'pseudolife-mcp 9.8.7' } }
        'installer --capability' { @{ Code = 0; Out = "pseudolife-mcp installer-protocol $NativeInstallerProtocol" } }
        'runtime install --check' {
            @{ Code = 3; Err = 'runtime install refused: this machine stays on the Python client' }
        }
        default { throw "Unexpected probe $Arguments" }
    }
}
''' + body
    result = _run("stub", tmp_path, body, extra_env={"STUB_REFUSE_TUNNEL": "1"})
    assert result.returncode == 1, result.stderr + result.stdout
    assert "INSTALLED" not in result.stdout
    assert "runtime install refused: this machine stays on the Python client" in result.stderr
    assert (
        "a tunnel is registered to start by itself through this machine's Python client. "
        "The native client has no tunnel service command to manage that registration after the move."
    ) in result.stderr
    assert "pseudolife-mcp tunnel run" in result.stderr
    assert ("cannot run " "a tunnel yet") not in result.stderr
    assert "the tunnel will no longer start on its own" in result.stderr
    # The verified download is kept for the rerun; nothing else changed.
    assert "Nothing else was changed: the download in" in result.stderr
    assert not (tmp_path / "home" / "runtimes").exists()


@pytest.mark.skipif(WINDOWS, reason="the refusing stand-in is a shell script; on Windows the fixture is a real client")
def test_a_refused_move_keeps_the_python_client_and_says_so(tmp_path):
    # Should runtime install itself refuse after the preflight passed, the
    # installer keeps the Python client and says so (issue #1632).
    body = 'if (Install-ShimOnce) { Write-Host "OK" } else { Write-Host "KEPT changing=$($script:nativeHostChanging)" }'
    result = _run("stub", tmp_path, body, extra_env={"STUB_REFUSE_TUNNEL_LATE": "1"})
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout + result.stderr
    assert "KEPT changing=" in result.stdout and "changing=True" not in result.stdout, out
    assert "this machine keeps the Python client" in out, out
    assert "rerun the installer, which reuses" not in out, out
    assert not (tmp_path / "home" / "runtimes").exists()


@needs_client
def test_a_python_client_registration_moves_to_the_native_launcher(tmp_path):
    if not WINDOWS:
        # The fixture is a Windows pipx layout; the move itself is the native
        # binary's (cli_installer_contract.rs), and the stub only reports.
        pytest.skip("a Windows pipx layout")
    venv = tmp_path / "home" / "pipx-venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages" / "pseudolife_mcp-0.23.1.dist-info").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")
    script = venv / "Scripts" / "pseudolife-mcp.exe"
    script.write_bytes(b"python console script")
    claude = tmp_path / "home" / "claude"
    claude.mkdir(parents=True)
    (claude / ".claude.json").write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": str(script), "args": [], "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    result = _run(INSTALL, tmp_path, 'if (Test-ShimRegistrationMigrates "claude-code") { Write-Host MOVED } else { Write-Host KEPT }')
    assert result.returncode == 0, result.stderr + result.stdout
    assert "MOVED" in result.stdout, result.stdout
    moved = json.loads((claude / ".claude.json").read_text(encoding="utf-8"))
    assert moved["mcpServers"]["pseudolife-memory"]["command"].endswith("pseudolife-mcp.exe")
    assert "pipx-venv" not in moved["mcpServers"]["pseudolife-memory"]["command"]
    assert "the old Python client stays on disk, unregistered" in result.stdout


@pytest.mark.parametrize("case, expected", [
    ("mismatch", "does not match the release's SHA256SUMS"),
    ("no-release", "has no SHA256SUMS"),
    ("missing", "lists " + _asset("stub") + " 0 times, not once"),
])
def test_a_bad_or_missing_release_refuses_before_anything_changes(tmp_path, case, expected):
    kwargs = {"mismatch": {"digest_override": "f" * 64}, "missing": {"omit": True}}.get(case, {})
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', release=case != "no-release", **kwargs)
    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stderr
    # Nothing on the host; a download already verified is named as kept.
    assert re.search(r"Nothing (else )?was changed", result.stderr)
    assert "STAGED" not in result.stdout
    assert not (tmp_path / "home" / "runtimes").exists()
    assert not (tmp_path / "home" / "bin").exists()


@pytest.mark.parametrize("manifest", [
    f'﻿[package]\r\nname = "pseudolife-stdio"\r\nversion = "{VERSION}"\r\n',
    f"[package]\nversion = '{VERSION}' # cut\n",
], ids=["byte-order-mark-crlf", "literal-string"])
def test_every_one_line_string_version_is_read(tmp_path, manifest):
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host "VERSION=$($script:nativeVersion)"',
                  manifest=manifest)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"VERSION={VERSION}" in result.stdout


@pytest.mark.parametrize("manifest", ["", '[package]\nname = "pseudolife-stdio"\nversion.workspace = true\n'],
                         ids=["no-crate", "no-literal-version"])
def test_a_checkout_without_a_native_release_version_is_refused_naming_the_crate(tmp_path, manifest):
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', manifest=manifest)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert ("this checkout's rust/shim/Cargo.toml names no version. "
            "Check out a release tag (git checkout v<version>) and rerun.") in line
    assert not (tmp_path / "home" / "runtimes").exists()


@pytest.mark.skipif(WINDOWS, reason="the v0.24.0 stand-in is a shell script; on Windows the fixture is a real client")
@pytest.mark.parametrize("answer", [
    {"STUB_V0240": "1"},                                              # v0.24.0: a usage error
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL - 1}"},  # an older protocol
    {"STUB_CAPABILITY": ""},                                          # says nothing
    {"STUB_CAPABILITY": "pseudolife-mcp installer-protocol one"},     # not the documented form
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL}\npseudolife-mcp installer-protocol 9"},  # two lines
    {"STUB_CAPABILITY": "pseudolife-mcp installer-protocol 99999999999999999999"},  # past any level
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol 0{LEVEL}"},  # a leading zero
])
def test_a_release_older_than_this_installer_is_refused_before_anything_changes(tmp_path, answer):
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', extra_env=answer)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert f"the native client {VERSION} predates this installer" in line
    assert "installer --capability" in line and f"installer-protocol {LEVEL} or newer" in line
    # The route that works today: the default client, then update moves it.
    assert "without PSEUDOLIFE_SHIM_ENGINE" in line
    assert "PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only" in line
    assert "release_assets" not in line and "PSEUDOLIFE_NATIVE_ARCHIVE_URL" not in line
    # What a refusal after the download leaves: the verified download only.
    assert "Nothing else was changed" in line and "kept for a rerun" in line
    assert ".pseudolife-mcp" in line and "downloads" in line
    calls = (tmp_path / "native-calls").read_text().splitlines()
    assert "installer --capability" in calls
    assert not any(call.startswith(("runtime", "installer expose", "installer migrate")) for call in calls)
    assert not (tmp_path / "home" / "runtimes").exists()
    assert not (tmp_path / "home" / "bin").exists()


@needs_client
def test_a_release_with_this_installers_protocol_installs(tmp_path):
    result = _run("real" if WINDOWS else "stub", tmp_path, 'if (Install-ShimOnce) { Write-Host "OK" } else { Write-Host "FAILED" }',
                  extra_env={"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL + 1}"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout.splitlines()


def test_an_unsupported_platform_refuses(tmp_path):
    result = _run("stub", tmp_path, 'function Get-NativeTarget { return $null }; Initialize-NativeClient; Write-Host STAGED')
    assert result.returncode == 1
    assert "no native client is published for this platform" in result.stderr
    assert "-ClientOnly" in result.stderr
    assert "STAGED" not in result.stdout


def test_the_installer_no_longer_installs_a_python_client():
    lines = (ROOT / "ops/install-native.ps1").read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    for gone in ("pipx install", "pip install --user", "shim_runtime.py", "Get-ShimRuntimePython"):
        assert gone not in text, gone


@needs_client
def test_a_rerun_over_a_python_client_keeps_the_old_client_readable(tmp_path):
    # The old Python client stays on disk for rollback, so the native install
    # must not leave its files unreadable. Files made here inherit their
    # access from the temporary directory only, as an installer-made Python
    # runtime under %LOCALAPPDATA% does.
    home = tmp_path / "home"
    old = home / "runtimes" / "000001"
    (old / "Scripts").mkdir(parents=True)
    (old / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")
    (old / "Scripts" / "pseudolife-mcp.exe").write_bytes(b"python console script")
    (home / "bin").mkdir(parents=True)
    old_launcher = home / "bin" / "pseudolife-mcp.json"
    old_launcher.write_text("{}", encoding="utf-8")
    result = _run(INSTALL, tmp_path, 'if (Install-ShimOnce) { Write-Host "OK" } else { Write-Host "FAILED" }')
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout, result.stdout
    for path in (old / "pyvenv.cfg", old / "Scripts" / "pseudolife-mcp.exe"):
        assert path.read_bytes(), path


def test_an_archive_url_with_credentials_refuses_without_printing_them(tmp_path):
    body = ('$env:PSEUDOLIFE_NATIVE_ARCHIVE_URL = "https://user:sekrit@mirror.example.com/v9"\n'
            "Initialize-NativeClient; Write-Host STAGED")
    result = _run("stub", tmp_path, body)
    assert result.returncode == 1
    assert "carries a user name or password" in result.stderr
    assert "sekrit" not in result.stdout + result.stderr
    assert "STAGED" not in result.stdout


@needs_client
def test_no_staged_copy_is_left_behind(tmp_path):
    body = ('Initialize-NativeClient; $stage = $script:nativeStageDir\n'
            'Install-ShimOnce | Out-Null\n'
            'Write-Host "STAGED=$([bool]$stage) LEFT=$(Test-Path -LiteralPath $stage)"')
    result = _run(INSTALL, tmp_path, body)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "STAGED=True LEFT=False" in result.stdout, result.stdout
    downloads = tmp_path / "home" / ".pseudolife-mcp" / "downloads" / VERSION
    assert (downloads / ASSET).is_file()



@pytest.mark.skipif(not WINDOWS, reason="the user PATH lives in the Windows registry")
def test_the_user_path_helper_keeps_expandable_entries_and_reruns_quietly(tmp_path):
    import uuid
    import winreg
    subkey = f"Software\\PseudolifeTest-{uuid.uuid4().hex}"
    raw = "%USERPROFILE%\\tools;C:\\Other"
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey)
    try:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, raw)
        target = str(tmp_path / "launcher-bin")
        helper = str(ROOT / "ops" / "add-user-path.ps1")
        run = lambda: subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", helper,
                                      "-Dir", target, "-KeyPath", subkey],
                                     capture_output=True, text=True, timeout=60)
        first = run()
        assert first.returncode == 0, first.stderr
        assert first.stdout.strip().splitlines()[-1] == "changed"
        value, kind = winreg.QueryValueEx(key, "Path")
        assert kind == winreg.REG_EXPAND_SZ
        assert value == target + ";" + raw
        second = run()
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip().splitlines()[-1] == "current"
        assert winreg.QueryValueEx(key, "Path")[0] == target + ";" + raw
    finally:
        winreg.CloseKey(key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)


def test_a_download_folder_that_cannot_be_made_is_named_not_the_network(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".pseudolife-mcp").write_text("a file, not a folder\n", encoding="utf-8")
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert ".pseudolife-mcp" in line and "downloads" in line and "network" not in line
    assert line.endswith("Nothing was changed.")


class _Status(BaseHTTPRequestHandler):
    """A release server that answers SHA256SUMS with one status."""
    status = 404

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(status: int) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_Status,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize("status", [403, 500])
def test_a_sums_file_that_cannot_be_fetched_names_the_status_not_a_missing_file(tmp_path, status):
    server, url = _serve(status)
    try:
        result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', release=False,
                      extra_env={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": url})
    finally:
        server.shutdown()
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert f"HTTP {status}" in line and "could not be fetched" in line and "retry" in line.lower()
    assert "has no SHA256SUMS" not in line


@pytest.mark.skipif(WINDOWS, reason="the hanging stand-in is a shell script; on Windows the fixture is a real client")
@pytest.mark.parametrize("hang", ["STUB_HANG_VERSION", "STUB_HANG_CAPABILITY"])
def test_a_staged_client_that_does_not_answer_is_refused_in_time(tmp_path, hang):
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED',
                  extra_env={hang: "1", "PSEUDOLIFE_NATIVE_PROBE_SECONDS": "2"})
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    step = "version" if hang == "STUB_HANG_VERSION" else "installer --capability"
    assert f"did not answer {step} within 2 s" in line


@needs_client



# -- round 4 ------------------------------------------------------------------

@needs_client
def test_a_question_after_the_install_does_not_stage_the_release_again(tmp_path):
    # After Install-ShimOnce the staged copy is gone and the installed client
    # answers; a later question (the passkey, expose or schedule offer) must
    # not download SHA256SUMS again, nor refuse a half-finished install.
    # The hook every question calls (the bank-location block).
    hook = _block("function Read-NativeBeforeAsking", "\nfunction Read-BankAnswer")
    body = "\n".join([
        hook,
        'if (-not (Install-ShimOnce)) { Write-Host "INSTALL FAILED"; exit 3 }',
        f"Remove-Item -LiteralPath '{tmp_path / 'release' / 'SHA256SUMS'}'",
        "Read-NativeBeforeAsking",
        'Write-Host "ASKED"',
    ])
    result = _run(INSTALL, tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ASKED" in result.stdout
    assert "cannot be installed" not in result.stderr


@needs_client
def test_a_refusal_after_the_install_began_does_not_claim_nothing_changed(tmp_path):
    body = 'if (-not (Install-ShimOnce)) { Write-Host "INSTALL FAILED"; exit 3 }\nStop-NativeInstall "a cause" "A fix."'
    result = _run(INSTALL, tmp_path, body)
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "a cause" in text]
    assert "Nothing" not in line and "was changed" not in line
    assert "part way" in line and "rerun" in line.lower()


def _rewrite_sums(tmp_path: Path, separator: str, client: str) -> None:
    _release(tmp_path / "release", client=client)
    asset = _asset(client)
    digest = hashlib.sha256((tmp_path / "release" / asset).read_bytes()).hexdigest()
    (tmp_path / "release" / "SHA256SUMS").write_text(f"{digest}{separator}{asset}\n", encoding="utf-8")


@needs_client
def test_a_binary_mode_checksum_line_is_accepted(tmp_path):
    _rewrite_sums(tmp_path, " *", INSTALL)
    result = _run(INSTALL, tmp_path, 'if (Install-ShimOnce) { Write-Host "OK" } else { Write-Host "FAILED" }', release=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize("separator", [" ", "\t", "   ", " * "])
def test_a_checksum_line_in_no_standard_form_is_refused(tmp_path, separator):
    _rewrite_sums(tmp_path, separator, "stub")
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', release=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert f"lists {_asset('stub')} 0 times, not once" in result.stderr


@pytest.mark.parametrize("value, used", [
    ("08", "8"), ("09", "9"), ("010", "10"), ("7", "7"), ("00", "30"), ("0", "30"),
    ("", "30"), ("abc", "30"), ("1.5", "30"), ("-3", "30"), ("123456", "30"),
])
def test_the_probe_deadline_is_read_as_a_decimal_number_of_seconds(tmp_path, value, used):
    result = _run("stub", tmp_path, 'Write-Host "SECONDS=$NativeProbeSeconds"', extra_env={"PSEUDOLIFE_NATIVE_PROBE_SECONDS": value})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SECONDS={used}" in result.stdout
    named = [line for line in (result.stdout + result.stderr).splitlines() if "PSEUDOLIFE_NATIVE_PROBE_SECONDS" in line]
    if value and used == "30":
        assert named and f"'{value}'" in named[0] and "30 s" in named[0], result.stdout + result.stderr
    else:
        assert not named, result.stdout + result.stderr


@pytest.mark.skipif(WINDOWS, reason="the v0.24.0 stand-in is a shell script; on Windows the fixture is a real client")
def test_the_refusal_says_how_to_clear_the_variable_in_powershell(tmp_path):
    result = _run("stub", tmp_path, 'Initialize-NativeClient; Write-Host STAGED', extra_env={"STUB_V0240": "1"})
    assert result.returncode == 1
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE" in line


def test_a_mismatched_archive_refusal_names_only_what_is_kept(tmp_path):
    result = _run("stub", tmp_path, "Initialize-NativeClient; Write-Host STAGED", digest_override="f" * 64)
    assert result.returncode == 1
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "was removed" in line
    assert "the download in" not in line
    assert "SHA256SUMS in" in line and "kept for a rerun" in line


def _whole_repo(tmp_path: Path) -> Path:
    """A copy of ops/ with this module's release version, for a whole-installer run."""
    repo = tmp_path / "whole"
    shutil.copytree(ROOT / "ops", repo / "ops", ignore=shutil.ignore_patterns(".env", "__pycache__", "*.log"))
    if (ROOT / "examples").is_dir():
        shutil.copytree(ROOT / "examples", repo / "examples")
    _write_crate(repo)
    return repo


@needs_client
def test_the_staging_folder_is_gone_when_the_installer_returns_to_an_open_session(tmp_path):
    # As a user runs it: `ops\install.ps1` at an open pwsh prompt, which stays
    # open after the script exits. A client-only run stages, then stops at the
    # daemon health check (exit 1, not a native refusal); the staged copy must
    # be gone while the session is still open, after each of two runs.
    from tests.test_installer_existing_upgrade import user_path_unchanged

    repo = _whole_repo(tmp_path)
    _release(tmp_path / "release", client=INSTALL)
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    installer = repo / "ops" / "install.ps1"
    # A pairing code: the token file is made only after the daemon answers.
    # The first run stops on a thrown error (a missing token file); the
    # second reaches the health check and exits 1 (port 9 answers nothing).
    # A throw is caught, so the check after it runs in the same session.
    def run(client: str, credential: str) -> str:
        return (f"try {{ & '{installer}' -ClientOnly -DaemonUrl http://127.0.0.1:9 {credential} "
                f"-Client {client} -ClaudePlugin skip -Instructions skip -NoArt -ClientUpdateSchedule no }} "
                "catch { Write-Host \"THREW: $_\" }")
    command = "; ".join([
        run("claude", f"-TokenFile '{tmp_path / 'absent.token'}'"),
        "Write-Host \"FIRST=$LASTEXITCODE LEFT=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter 'pseudolife-native-*').Count)\"",
        run("generic", "-PairingCode ABCD-EFGH-JKMN"),
        "Write-Host \"SECOND=$LASTEXITCODE LEFT=$(@(Get-ChildItem -LiteralPath $env:TEMP -Filter 'pseudolife-native-*').Count)\"",
        "Write-Host \"SUBSCRIBERS=$(@(Get-EventSubscriber).Count)\"",
    ])
    env = {key: value for key, value in os.environ.items() if not key.startswith("PSEUDOLIFE_")}
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "Local"), "APPDATA": str(home / "Roaming"),
        "CODEX_HOME": str(home / "codex"), "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
        "NATIVE_CALLS": str(tmp_path / "native-calls"), "STUB_LEVEL": str(LEVEL),
        "PSEUDOLIFE_SHIM_ENGINE": "native",
        "PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(tmp_path / "release"),
        "PSEUDOLIFE_SHIM_RUNTIMES": str(home / "runtimes"),
        "PSEUDOLIFE_SHIM_LAUNCHER": str(home / "bin" / ("pseudolife-mcp.exe" if WINDOWS else "pseudolife-mcp")),
        "NO_COLOR": "1", "TERM": "dumb",
    })
    with user_path_unchanged(home):
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, env=env, timeout=300, stdin=subprocess.DEVNULL)
    output = result.stdout + result.stderr
    assert (home / ".pseudolife-mcp" / "downloads" / VERSION / ASSET).is_file(), output
    # Each run stops after staging (an exit or a thrown error, not a native
    # refusal), and the session it returns to holds no staged copy.
    assert "THREW" in output and re.search(r"FIRST=[0-9]+ LEFT=0", output), output
    assert re.search(r"SECOND=[1-9][0-9]* LEFT=0", output), output
    assert "SUBSCRIBERS=0" in output, output
