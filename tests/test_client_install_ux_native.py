"""The native installer (ops/install-native.*): the tests of test_client_install_ux.py
that the native client path changed, run against the native installer.

test_client_install_ux.py keeps the default (Python client) installer's tests.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_install_sh_shim_failure_falls_back_instead_of_aborting() -> None:
    """A failed shim install must leave SHIM_OK unset so the HTTP fallback
    fires, not kill the run via errexit (issue #176). The native client
    install is the condition of an `if`, so errexit is suspended around it,
    and its own failure paths return a status instead of exiting."""
    sh = _read("ops/install-native.sh")
    ensure = sh[sh.index("ensure_shim() {"):]
    ensure = ensure[:ensure.index("\n}\n")]
    assert "    if native_install; then" in ensure
    install = sh[sh.index("native_install() {"):]
    install = install[:install.index("\n}\n")]
    assert '--version "$NATIVE_VERSION" | tail -1)" || return 1' in install
    assert "|| return 1" in install.split("SHIM_PATH=", 1)[0]


def test_install_ps1_puts_the_launcher_on_the_user_path_and_says_so_in_the_summary() -> None:
    """Once the native client installs, the launcher directory goes first on
    the user PATH (HKCU only), so `pseudolife-mcp` in a new terminal is the
    launcher rather than an older pipx copy (2026-09-29); the outcome is
    repeated in the summary, before the board line, marked [!] when the step
    failed."""
    ps = _read("ops/install-native.ps1")
    native = ps[ps.index("function Install-NativeClient"):]
    native = native[:native.index("\n}\n")]
    assert "Add-NativeLauncherToUserPath $launcher" in native
    assert native.index("Add-NativeLauncherToUserPath") < native.index("return $launcher")
    add_path = ps[ps.index("function Add-NativeLauncherToUserPath"):]
    add_path = add_path[:add_path.index("\n}\n")]
    assert '& (Join-Path $repo "ops/add-user-path.ps1") -Dir $dir' in add_path
    # a test or custom layout (PSEUDOLIFE_SHIM_LAUNCHER) never touches PATH
    assert "if ($env:PSEUDOLIFE_SHIM_LAUNCHER) { return $null }" in add_path
    helper = _read("ops/add-user-path.ps1")
    # %VAR% entries stay as written: the raw value, written back with its kind
    assert "DoNotExpandEnvironmentNames" in helper
    assert '$key.SetValue("Path", ((@($dir) + $rest) -join ";"), $kind)' in helper
    assert "Registry]::CurrentUser" in helper and "LocalMachine" not in helper
    assert "add-user-path.ps1" in _read("ops/install-native.sh")
    once = ps[ps.index("function Install-ShimOnce"):]
    once = once[:once.index("\n}\n")]
    assert once.index("Install-NativeClient") < once.index("$script:shimInstallResult = $true")
    summary = ps[ps.index('if ($script:shimPathNotes.Count -gt 0) {'):]
    assert summary.index("Launcher on PATH") < summary.index("# >>> board line >>>")
    assert '$pathMarker = if ($script:shimPathState -eq "failed") { "[!]" } else { "[x]" }' in summary
    # the machine PATH is never written
    assert "'Machine'" not in ps and '"Machine"' not in ps
