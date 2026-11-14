"""The PowerShell installer installs the native client from the release.

These dot-source the installer's native-client block and shim helpers
(``ops/install.ps1``) in ``pwsh`` with a throwaway home and a fake release
directory (``PSEUDOLIFE_NATIVE_ARCHIVE_URL``). The refusal tests run
everywhere. The install tests need an executable client: off Windows a
shell stub; on Windows a real native ``pseudolife-mcp.exe`` named by
``PSEUDOLIFE_NATIVE_TEST_BINARY`` (a Windows stub cannot be a script), and
they skip without one. ``python``, ``py``, ``pip`` and ``pipx`` on PATH are
stubs that fail the test if anything calls them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.8.7"
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None, reason="needs pwsh")
WINDOWS = os.name == "nt"
REAL = os.environ.get("PSEUDOLIFE_NATIVE_TEST_BINARY", "")
TARGET = "x86_64-pc-windows-msvc" if WINDOWS else "x86_64-unknown-linux-gnu"
ASSET = f"pseudolife-mcp-{VERSION}-{TARGET}." + ("zip" if WINDOWS else "tar.gz")
needs_client = pytest.mark.skipif(
    WINDOWS and not (REAL and Path(REAL).is_file()),
    reason="on Windows set PSEUDOLIFE_NATIVE_TEST_BINARY to a built native pseudolife-mcp.exe",
)

STUB = r'''#!/bin/sh
printf '%s\n' "$*" >>"$NATIVE_CALLS"
case "$1" in
    version) echo "pseudolife-mcp 9.8.7" ;;
    runtime)
        mkdir -p "$(dirname "$PSEUDOLIFE_SHIM_LAUNCHER")"
        cp "$0" "$PSEUDOLIFE_SHIM_LAUNCHER"; chmod 755 "$PSEUDOLIFE_SHIM_LAUNCHER"
        echo "$PSEUDOLIFE_SHIM_LAUNCHER" ;;
    update) [ "$2" = --native-capability ] && echo "pseudolife-mcp native-update-v1" ;;
    installer) [ "$2" = migrate-registrations ] && echo "$4 already runs the native client; nothing changed" ;;
esac
'''


def _block(start: str, end: str) -> str:
    text = (ROOT / "ops/install.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.count(start) == 1, start
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _release(directory: Path, *, digest_override: str | None = None, omit: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    if WINDOWS:
        data = Path(REAL).read_bytes() if REAL and Path(REAL).is_file() else b"MZ not a program"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("pseudolife-mcp.exe", data)
    else:
        data = STUB.encode()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("pseudolife-mcp")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    (directory / ASSET).write_bytes(buffer.getvalue())
    digest = digest_override or hashlib.sha256(buffer.getvalue()).hexdigest()
    (directory / "SHA256SUMS").write_text("" if omit else f"{digest}  {ASSET}\n", encoding="utf-8")


def _run(tmp_path: Path, body: str, *, release: bool = True, **release_kwargs) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n', encoding="utf-8")
    base = tmp_path / "release"
    if release:
        _release(base, **release_kwargs)
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
    script = "\n".join([
        '$ErrorActionPreference = "Stop"',
        "function Step($msg) { Write-Host \"==> $msg\" }",
        f"$repo = '{repo}'",
        _block("# >>> native client >>>", "# <<< native client <<<"),
        _block("$script:shimInstallResult = $null\n", "\n# Two env pairs ride each shim registration"),
        body,
    ])
    script_file = tmp_path / "run.ps1"
    script_file.write_text(script, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PSEUDOLIFE_")}
    env.update({
        "PATH": os.pathsep.join([str(stubs), os.environ.get("PATH", "")]),
        "HOME": str(home), "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "Local"), "APPDATA": str(home / "Roaming"),
        "CODEX_HOME": str(home / "codex"), "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "NATIVE_CALLS": str(tmp_path / "native-calls"),
        "PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(base),
        "PSEUDOLIFE_SHIM_RUNTIMES": str(runtimes),
        "PSEUDOLIFE_SHIM_LAUNCHER": str(launcher),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9",
    })
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script_file)],
                            capture_output=True, text=True, env=env, timeout=180)
    assert not forbidden.exists(), f"the installer ran Python tooling: {forbidden.read_text()}"
    return result


@needs_client
def test_fresh_install_and_rerun_use_the_verified_archive(tmp_path):
    body = 'if (Install-ShimOnce) { Write-Host "OK $($script:shimInstallPath)" } else { Write-Host "FAILED" }'
    first = _run(tmp_path, body)
    assert first.returncode == 0, first.stderr + first.stdout
    launcher = tmp_path / "home" / "bin" / ("pseudolife-mcp.exe" if WINDOWS else "pseudolife-mcp")
    assert f"OK {launcher}" in first.stdout, first.stdout
    assert f"Native client {VERSION} installed at" in first.stdout
    downloads = tmp_path / "home" / ".pseudolife-mcp" / "downloads" / VERSION
    assert (downloads / ASSET).is_file() and (downloads / "SHA256SUMS").is_file()
    second = _run(tmp_path, body)
    assert second.returncode == 0, second.stderr + second.stdout
    assert f"Native client {VERSION} already installed at" in second.stdout, second.stdout
    assert "nothing changed" in second.stdout


@needs_client
def test_a_python_client_registration_moves_to_the_native_launcher(tmp_path):
    if not WINDOWS:
        pytest.skip("the move itself is the native binary's (cli_installer_contract.rs); the stub only reports")
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
    result = _run(tmp_path, 'if (Test-ShimRegistrationMigrates "claude-code") { Write-Host MOVED } else { Write-Host KEPT }')
    assert result.returncode == 0, result.stderr + result.stdout
    assert "MOVED" in result.stdout, result.stdout
    moved = json.loads((claude / ".claude.json").read_text(encoding="utf-8"))
    assert moved["mcpServers"]["pseudolife-memory"]["command"].endswith("pseudolife-mcp.exe")
    assert "pipx-venv" not in moved["mcpServers"]["pseudolife-memory"]["command"]
    assert "the old Python client stays on disk, unregistered" in result.stdout


@pytest.mark.parametrize("case, expected", [
    ("mismatch", "does not match the release's SHA256SUMS"),
    ("no-release", "has no SHA256SUMS"),
    ("missing", "lists " + ASSET + " 0 times, not once"),
])
def test_a_bad_or_missing_release_refuses_before_anything_changes(tmp_path, case, expected):
    kwargs = {"mismatch": {"digest_override": "f" * 64}, "missing": {"omit": True}}.get(case, {})
    result = _run(tmp_path, 'Initialize-NativeClient; Write-Host STAGED', release=case != "no-release", **kwargs)
    assert result.returncode == 1, result.stdout + result.stderr
    assert expected in result.stderr
    assert "Nothing was changed." in result.stderr
    assert "STAGED" not in result.stdout
    assert not (tmp_path / "home" / "runtimes").exists()
    assert not (tmp_path / "home" / "bin").exists()


def test_an_unsupported_platform_refuses(tmp_path):
    result = _run(tmp_path, 'function Get-NativeTarget { return $null }; Initialize-NativeClient; Write-Host STAGED')
    assert result.returncode == 1
    assert "no native client is published for this platform" in result.stderr
    assert "-ClientOnly" in result.stderr
    assert "STAGED" not in result.stdout


def test_the_installer_no_longer_installs_a_python_client():
    lines = (ROOT / "ops/install.ps1").read_text(encoding="utf-8").splitlines()
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
    (old / "pyvenv.cfg").write_text("home = C:\Python312\n", encoding="utf-8")
    (old / "Scripts" / "pseudolife-mcp.exe").write_bytes(b"python console script")
    (home / "bin").mkdir(parents=True)
    old_launcher = home / "bin" / "pseudolife-mcp.json"
    old_launcher.write_text("{}", encoding="utf-8")
    result = _run(tmp_path, 'if (Install-ShimOnce) { Write-Host "OK" } else { Write-Host "FAILED" }')
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK" in result.stdout, result.stdout
    for path in (old / "pyvenv.cfg", old / "Scripts" / "pseudolife-mcp.exe"):
        assert path.read_bytes(), path


def test_an_archive_url_with_credentials_refuses_without_printing_them(tmp_path):
    body = ('$env:PSEUDOLIFE_NATIVE_ARCHIVE_URL = "https://user:sekrit@mirror.example.com/v9"\n'
            "Initialize-NativeClient; Write-Host STAGED")
    result = _run(tmp_path, body)
    assert result.returncode == 1
    assert "carries a user name or password" in result.stderr
    assert "sekrit" not in result.stdout + result.stderr
    assert "STAGED" not in result.stdout


@needs_client
def test_no_staged_copy_is_left_behind(tmp_path):
    body = ('Initialize-NativeClient; $stage = $script:nativeStageDir\n'
            'Install-ShimOnce | Out-Null\n'
            'Write-Host "STAGED=$([bool]$stage) LEFT=$(Test-Path -LiteralPath $stage)"')
    result = _run(tmp_path, body)
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
