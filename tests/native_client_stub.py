"""A fake native client for installer tests that run sliced installer code.

The installers' shim helpers (``ensure_shim`` / ``Install-ShimOnce`` and the
registration move) call the native-client block's ``native_install`` /
``Install-NativeClient`` and ``native_installed_launcher`` /
``Get-NativeInstalledLauncher``. A test that slices the helpers without the
block defines these stand-ins after the slice. Environment:

- ``FAKE_NATIVE_EXIT``: ``0`` (default) installs; anything else fails.
- ``FAKE_LAUNCHER``: where the stub launcher is written and found.
- ``FAKE_MIGRATE_EXIT``: the launcher's ``installer migrate-registrations
  --client X`` reports ``moved X ...`` for ``0``, ``X already runs the native
  client ...`` for ``4``, and otherwise (default ``3``) that it left the
  registration alone, as the real step does for a bare or custom command.
- ``FAKE_KEEP_LAUNCHER``: set, a launcher the test already wrote at
  ``FAKE_LAUNCHER`` is kept (a test's own stub that answers ``connect`` or
  ``pair``) instead of being replaced by the stub launcher.
- ``CALL_LOG``: ``native|install`` and ``launcher|<args>`` lines go here.

The real block is tested in ``test_installer_native_client.py`` and
``test_installer_native_client_ps.py``; the real moves in
``rust/shim/tests/cli_installer_contract.rs``.
"""
from __future__ import annotations

# The launcher the stub install writes (POSIX shell).
LAUNCHER_SH = r'''#!/bin/sh
printf 'launcher|%s\n' "$*" >>"${CALL_LOG:-/dev/null}"
if [ "$1" = installer ] && [ "$2" = migrate-registrations ]; then
    case "${FAKE_MIGRATE_EXIT:-3}" in
        0) echo "moved $4 to the native client at $0 (each changed file has a private backup)" ;;
        4) echo "$4 already runs the native client; nothing changed" ;;
        *) echo "no registration moved; nothing changed ($4 still runs another command: left as it is)" ;;
    esac
fi
if [ "$1" = update ] && [ "$2" = --native-capability ]; then echo "pseudolife-mcp native-update-v1"; fi
exit 0
'''

BASH = r'''
NATIVE_ARCHIVE="${NATIVE_ARCHIVE:-the verified fake archive}"
native_install() {
    printf 'native|install\n' >>"${CALL_LOG:-/dev/null}"
    [ "${FAKE_NATIVE_EXIT:-0}" = 0 ] || return 1
    native_fake_launcher="${FAKE_LAUNCHER:-$SHIM_PATH}"
    mkdir -p "$(dirname "$native_fake_launcher")"
    if [ -n "${FAKE_KEEP_LAUNCHER:-}" ] && [ -f "$native_fake_launcher" ]; then
        SHIM_PATH="$native_fake_launcher"
        return 0
    fi
    cat >"$native_fake_launcher" <<'NATIVE_STUB_LAUNCHER'
''' + LAUNCHER_SH + r'''NATIVE_STUB_LAUNCHER
    chmod 755 "$native_fake_launcher"
    SHIM_PATH="$native_fake_launcher"
    step "Native client fake installed at $SHIM_PATH"
}
native_installed_launcher() {
    native_fake_launcher="${FAKE_LAUNCHER:-}"
    if [ -n "$native_fake_launcher" ] && [ -f "$native_fake_launcher" ]; then printf '%s\n' "$native_fake_launcher"; fi
}
'''

# PowerShell: the launcher is a .cmd on Windows (an .exe cannot be a
# script) and a shell script elsewhere; calls go to $env:CALL_LOG.
POWERSHELL = r'''
$script:nativeArchive = "the verified fake archive"
$script:fakeLauncherSh = @'
''' + LAUNCHER_SH + r''''@
function Install-NativeClient {
    if ($env:CALL_LOG) { Add-Content -LiteralPath $env:CALL_LOG -Value 'native|install' }
    if ("$env:FAKE_NATIVE_EXIT" -and "$env:FAKE_NATIVE_EXIT" -ne "0") { return $null }
    $launcher = $env:FAKE_LAUNCHER
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $launcher) | Out-Null
    if ($env:FAKE_KEEP_LAUNCHER -and (Test-Path -LiteralPath $launcher -PathType Leaf)) { return $launcher }
    if ($IsWindows -or ($null -eq $IsWindows)) {
        $cmd = @(
            '@echo off',
            'if defined CALL_LOG echo launcher^|%*>>"%CALL_LOG%"',
            'if "%1 %2"=="installer migrate-registrations" (',
            '  if "%FAKE_MIGRATE_EXIT%"=="0" (echo moved %4 to the native client at %~f0 ^(each changed file has a private backup^)) else if "%FAKE_MIGRATE_EXIT%"=="4" (echo %4 already runs the native client; nothing changed) else (echo no registration moved; nothing changed)',
            ')',
            'if "%1 %2"=="update --native-capability" echo pseudolife-mcp native-update-v1',
            'exit /b 0'
        ) -join "`r`n"
        Set-Content -LiteralPath $launcher -Value $cmd -Encoding ascii
    } else {
        $script = if ($env:FAKE_LAUNCHER_SH) { $env:FAKE_LAUNCHER_SH } else { $script:fakeLauncherSh }
        Set-Content -LiteralPath $launcher -Value $script -NoNewline
        # No chmod lookup: some harnesses run with a PATH holding only their stubs.
        [System.IO.File]::SetUnixFileMode($launcher, [System.IO.UnixFileMode]"UserRead, UserWrite, UserExecute, GroupRead, GroupExecute, OtherRead, OtherExecute")
    }
    Step "Native client fake installed at $launcher"
    return $launcher
}
function Get-NativeInstalledLauncher {
    if ($env:FAKE_LAUNCHER -and (Test-Path -LiteralPath $env:FAKE_LAUNCHER -PathType Leaf)) { return $env:FAKE_LAUNCHER }
    return $null
}
'''


def powershell_env(launcher: str) -> dict[str, str]:
    """Environment the PowerShell stand-ins read; ``launcher`` should end in
    ``.cmd`` on Windows."""
    return {"FAKE_LAUNCHER": launcher, "FAKE_LAUNCHER_SH": LAUNCHER_SH}


# -- A fake release, for tests that run a whole installer ---------------------

RELEASE_TARGET = "x86_64-unknown-linux-gnu"

# Prepended to a test's own launcher body: what the installer's native block
# asks of the staged binary (its version, the runtime install, the native
# capability probe) is answered here, and everything else falls through.
RELEASE_PRELUDE = r'''#!/bin/sh
case "$1" in
    version) echo "pseudolife-mcp fake"; exit 0 ;;
    runtime)
        mkdir -p "$(dirname "$PSEUDOLIFE_SHIM_LAUNCHER")"
        cp "$0" "$PSEUDOLIFE_SHIM_LAUNCHER"; chmod 755 "$PSEUDOLIFE_SHIM_LAUNCHER"
        echo "$PSEUDOLIFE_SHIM_LAUNCHER"; exit 0 ;;
esac
if [ "$1" = update ] && [ "$2" = --native-capability ]; then echo "pseudolife-mcp native-update-v1"; exit 0; fi
'''


def write_release(directory, version: str, launcher_body: str) -> dict[str, str]:
    """A release directory holding the archive for ``RELEASE_TARGET`` (a
    shell script answering the native block, then ``launcher_body``) and its
    ``SHA256SUMS``. Returns the installer environment that selects it; the
    caller adds ``PSEUDOLIFE_SHIM_RUNTIMES`` / ``PSEUDOLIFE_SHIM_LAUNCHER``."""
    import hashlib
    import io
    import tarfile
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = (RELEASE_PRELUDE + launcher_body).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("pseudolife-mcp")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    name = f"pseudolife-mcp-{version}-{RELEASE_TARGET}.tar.gz"
    (directory / name).write_bytes(buffer.getvalue())
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()
    (directory / "SHA256SUMS").write_text(f"{digest}  {name}\n", encoding="utf-8", newline="\n")
    return {"PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(directory), "PSEUDOLIFE_NATIVE_TARGET": RELEASE_TARGET}


# Harnesses whose fake shim sits in $FIXTURE_SHIM_BIN (test_codex_hooks.py).
NATIVE_FROM_SHIM_BIN_SH = 'FAKE_LAUNCHER="$FIXTURE_SHIM_BIN/pseudolife-mcp"\nFAKE_KEEP_LAUNCHER=1\n' + BASH
NATIVE_UNAVAILABLE_SH = (
    'NATIVE_ARCHIVE="the verified fake archive"\n'
    "native_install() { return 1; }\n"
    "native_installed_launcher() { :; }\n")
NATIVE_FROM_SHIM_BIN_PS = (
    "$env:FAKE_LAUNCHER = Join-Path $env:FIXTURE_SHIM_BIN "
    "$(if ($IsWindows -or ($null -eq $IsWindows)) { 'pseudolife-mcp.exe' } else { 'pseudolife-mcp' })\n"
    "$env:FAKE_KEEP_LAUNCHER = '1'\n" + POWERSHELL)
