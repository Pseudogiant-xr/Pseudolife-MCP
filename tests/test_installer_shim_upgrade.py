"""Executable contracts for installing the host shim from the checkout.

The daemon is built from the checkout, so its stdio shim must come from the
same checkout too.  These tests execute only the bounded installer helpers
with fake package-manager commands; they never alter the user's packages.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _bash_variants() -> list[str]:
    variants: list[str] = []
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None,
        shutil.which("bash"),
    ):
        if not candidate or not Path(candidate).is_file() or candidate in variants:
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "exit 0"], capture_output=True,
                check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            variants.append(candidate)
    return variants


def _between(path: str, start: str, end: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _run_bash_helper(
    bash: str, *, manager: str, state: str, create_executable: bool = True,
    windows_manager_path: bool = False, failed_later_probe: bool = False,
) -> tuple[list[str], str, str]:
    helper = _between(
        "ops/install.sh", "resolve_installed_shim() {", "\n}\n\n# Two env pairs"
    ) + "\n}"
    repo = str(ROOT).replace("'", "'\\''")
    fake_setup = r'''
fake_bin="$(mktemp -d)"
installed_bin="$fake_bin/installed dir"
mkdir -p "$installed_bin"
call_log="$fake_bin/calls"
trap 'rm -rf "$fake_bin"' EXIT
export CALL_LOG="$call_log" FAKE_SHIM_STATE="%s" FAKE_INSTALLED_BIN="$installed_bin" FAKE_WINDOWS_PATH="%s"
if [ "%s" = pipx ]; then
    cat >"$fake_bin/pipx" <<'FAKE'
#!/bin/sh
printf 'pipx|%%s\n' "$*" >>"$CALL_LOG"
if [ "$1" = install ] && [ "%s" = yes ]; then
    printf '#!/bin/sh\n' >"$FAKE_INSTALLED_BIN/pseudolife-mcp"
    chmod +x "$FAKE_INSTALLED_BIN/pseudolife-mcp"
elif [ "$1" = environment ]; then
    if [ "${FAKE_RESOLVE_FAIL:-}" = yes ]; then exit 1; fi
    if [ "$FAKE_WINDOWS_PATH" = yes ]; then cygpath -w "$FAKE_INSTALLED_BIN"; else printf '%%s\n' "$FAKE_INSTALLED_BIN"; fi
fi
exit 0
FAKE
else
    cat >"$fake_bin/python3" <<'FAKE'
#!/bin/sh
printf 'python3|%%s\n' "$*" >>"$CALL_LOG"
if [ "$1" = -m ] && [ "%s" = yes ]; then
    printf '#!/bin/sh\n' >"$FAKE_INSTALLED_BIN/pseudolife-mcp"
    chmod +x "$FAKE_INSTALLED_BIN/pseudolife-mcp"
elif [ "$1" = -c ] && printf '%%s' "$2" | grep -q sysconfig; then
    if [ "$FAKE_WINDOWS_PATH" = yes ]; then cygpath -w "$FAKE_INSTALLED_BIN"; else printf '%%s\n' "$FAKE_INSTALLED_BIN"; fi
fi
exit 0
FAKE
fi
printf '#!/bin/sh\nexit 99\n' >"$fake_bin/pseudolife-mcp"
chmod +x "$fake_bin"/*
PATH="$fake_bin:/usr/bin:/bin"
''' % (state, "yes" if windows_manager_path else "no", manager,
       "yes" if create_executable else "no",
       "yes" if create_executable else "no")
    script = f"""set -eu
{fake_setup}
repo='{repo}'
SHIM_TRIED=""
SHIM_OK=""
SHIM_PATH=""
if [ '{manager}' = pip ]; then
    command() {{
        if [ "$1" = -v ] && [ "$2" = pipx ]; then return 1; fi
        builtin command "$@"
    }}
fi
{helper}
ensure_shim
if [ '{"yes" if failed_later_probe else "no"}' = yes ]; then
    export FAKE_RESOLVE_FAIL=yes
    resolve_installed_shim || true
fi
ensure_shim
cat "$call_log"
printf 'resolved|%s\n' "$SHIM_PATH"
"""
    proc = subprocess.run(
        [bash], input=script.encode(), capture_output=True, check=False,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    lines = proc.stdout.decode().splitlines()
    resolved = lines.pop().split("|", 1)[1]
    diagnostics = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    return lines, resolved, diagnostics


@pytest.mark.parametrize("state", ["fresh", "stale"])
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_pipx_replaces_with_checkout_once(bash: str, state: str) -> None:
    calls, resolved, _ = _run_bash_helper(bash, manager="pipx", state=state)
    assert calls == [f"pipx|install --force {ROOT}",
                     "pipx|environment --value PIPX_BIN_DIR"]
    assert Path(resolved).name == "pseudolife-mcp"
    assert Path(resolved).parent.name == "installed dir"


@pytest.mark.parametrize("state", ["fresh", "stale"])
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_pip_uses_upgrade_from_checkout_once(bash: str, state: str) -> None:
    calls, resolved, _ = _run_bash_helper(bash, manager="pip", state=state)
    assert calls == ["python3|-c import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)",
                     f"python3|-m pip install --user --upgrade {ROOT}",
                     ("python3|-c import sysconfig; "
                      "print(sysconfig.get_path('scripts', "
                      "scheme=sysconfig.get_preferred_scheme('user')))")]
    assert Path(resolved).name == "pseudolife-mcp"
    assert Path(resolved).parent.name == "installed dir"


@pytest.mark.parametrize(
    "bash", [p for p in _bash_variants() if "Git" in p],
    ids=lambda p: Path(p).parent.name,
)
def test_install_sh_normalizes_windows_manager_path_with_spaces(bash: str) -> None:
    _, resolved, _ = _run_bash_helper(
        bash, manager="pipx", state="fresh", windows_manager_path=True,
    )
    assert Path(resolved).name == "pseudolife-mcp"
    assert Path(resolved).parent.name == "installed dir"


@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_failed_later_probe_preserves_memoized_shim_path(
    bash: str,
) -> None:
    calls, resolved, _ = _run_bash_helper(
        bash, manager="pipx", state="fresh", failed_later_probe=True,
    )
    assert calls == [
        f"pipx|install --force {ROOT}",
        "pipx|environment --value PIPX_BIN_DIR",
        "pipx|environment --value PIPX_BIN_DIR",
    ]
    assert Path(resolved).name == "pseudolife-mcp"
    assert Path(resolved).parent.name == "installed dir"


@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_does_not_report_success_without_installed_executable(
    bash: str,
) -> None:
    _, resolved, stderr = _run_bash_helper(
        bash, manager="pipx", state="fresh", create_executable=False,
    )
    assert resolved == ""
    assert "installed executable" in stderr


def _run_powershell_helper(
    tmp_path: Path, *, manager: str, state: str, create_executable: bool = True,
) -> tuple[list[str], str, str]:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is unavailable")
    helper = _between(
        "ops/install.ps1", "function Resolve-InstalledShimPath", "\n}\n\n# Two env pairs"
    ) + "\n}"
    log = tmp_path / "calls.txt"
    command = "pipx" if manager == "pipx" else "python"
    escaped_repo = str(ROOT).replace("'", "''")
    escaped_log = str(log).replace("'", "''")
    installed_bin = tmp_path / "installed"
    installed_bin.mkdir()
    escaped_installed_bin = str(installed_bin).replace("'", "''")
    script = f"""$ErrorActionPreference = 'Stop'
$env:PATH = ''
$repo = '{escaped_repo}'
$script:shimInstallResult = $null
$script:shimInstallPath = $null
function global:{command} {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('{command}|' + ($callArgs -join ' '))
    if (($callArgs[0] -eq 'install') -and ({'$true' if create_executable else '$false'})) {{
        Set-Content -LiteralPath (Join-Path '{escaped_installed_bin}' 'pseudolife-mcp.exe') -Value 'fake'
    }} elseif ($callArgs[0] -eq 'environment') {{
        Write-Output '{escaped_installed_bin}'
    }} elseif (($callArgs[0] -eq '-m') -and ({'$true' if create_executable else '$false'})) {{
        Set-Content -LiteralPath (Join-Path '{escaped_installed_bin}' 'pseudolife-mcp.exe') -Value 'fake'
    }} elseif (($callArgs[0] -eq '-c') -and ($callArgs[1] -match 'sysconfig')) {{
        Write-Output '{escaped_installed_bin}'
    }}
    $global:LASTEXITCODE = 0
}}
{helper}
Install-ShimOnce | Out-Null
Install-ShimOnce | Out-Null
Add-Content -LiteralPath '{escaped_log}' -Value ('resolved|' + $script:shimInstallPath)
"""
    env = os.environ.copy()
    env["CALL_LOG"] = str(log)
    env["FAKE_SHIM_STATE"] = state
    runner = tmp_path / "run-helper.ps1"
    runner.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
        capture_output=True,
        env=env,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    lines = log.read_text(encoding="utf-8").splitlines()
    resolved = lines.pop().split("|", 1)[1]
    diagnostics = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    return lines, resolved, diagnostics


@pytest.mark.parametrize("state", ["fresh", "stale"])
def test_install_ps1_pipx_replaces_with_checkout_once(tmp_path: Path, state: str) -> None:
    calls, resolved, _ = _run_powershell_helper(
        tmp_path, manager="pipx", state=state,
    )
    assert calls == [f"pipx|install --force {ROOT}",
                     "pipx|environment --value PIPX_BIN_DIR"]
    assert Path(resolved).name == "pseudolife-mcp.exe"
    assert Path(resolved).parent.name == "installed"


@pytest.mark.parametrize("state", ["fresh", "stale"])
def test_install_ps1_pip_uses_upgrade_from_checkout_once(tmp_path: Path, state: str) -> None:
    calls, resolved, _ = _run_powershell_helper(tmp_path, manager="pip", state=state)
    assert calls == ["python|-c import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)",
                     f"python|-m pip install --user --upgrade {ROOT}",
                     ("python|-c import sysconfig; "
                      "print(sysconfig.get_path('scripts', "
                      "scheme=sysconfig.get_preferred_scheme('user')))")]
    assert Path(resolved).name == "pseudolife-mcp.exe"
    assert Path(resolved).parent.name == "installed"


def test_install_ps1_does_not_report_success_without_installed_executable(
    tmp_path: Path,
) -> None:
    _, resolved, stderr = _run_powershell_helper(
        tmp_path, manager="pipx", state="fresh", create_executable=False,
    )
    assert resolved == ""
    assert "installed executable" in stderr


def test_pip_local_upgrade_replaces_same_version_in_disposable_userbase(
    tmp_path: Path,
) -> None:
    """Prove pip's direct-local requirement refreshes same-version code.

    The synthetic package has no dependencies and PYTHONUSERBASE points into
    pytest's temporary directory, so this never reads or writes user packages.
    """
    package = tmp_path / "package"
    package.mkdir()
    (package / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = 'backend'\n"
        "backend-path = ['.']\n",
        encoding="utf-8",
    )
    (package / "backend.py").write_text(
        "from pathlib import Path\n"
        "from zipfile import ZipFile\n"
        "def build_wheel(wheel_directory, config_settings=None, "
        "metadata_directory=None):\n"
        "    name = 'pseudolife_shim_upgrade_probe-0.15.0-py3-none-any.whl'\n"
        "    dist = 'pseudolife_shim_upgrade_probe-0.15.0.dist-info'\n"
        "    with ZipFile(Path(wheel_directory) / name, 'w') as wheel:\n"
        "        wheel.write('shim_upgrade_probe.py', 'shim_upgrade_probe.py')\n"
        "        wheel.writestr(dist + '/METADATA', "
        "'Metadata-Version: 2.1\\nName: pseudolife-shim-upgrade-probe\\nVersion: 0.15.0\\n')\n"
        "        wheel.writestr(dist + '/WHEEL', "
        "'Wheel-Version: 1.0\\nGenerator: contract-probe\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
        "        wheel.writestr(dist + '/RECORD', '')\n"
        "    return name\n",
        encoding="utf-8",
    )
    module = package / "shim_upgrade_probe.py"
    userbase = tmp_path / "userbase"
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(userbase)
    # A venv rejects --user even when its base interpreter supports it. Probe
    # with the base executable, which matches the installers' host-Python path.
    probe_python = str(getattr(sys, "_base_executable", sys.executable))
    pip_probe = subprocess.run(
        [probe_python, "-m", "pip", "--version"], capture_output=True,
        text=True, check=False, timeout=15,
    )
    assert pip_probe.returncode == 0, (
        f"base interpreter {probe_python!r} has no usable pip: {pip_probe.stderr}")
    command = [
        probe_python, "-m", "pip", "install", "--user", "--upgrade",
        "--no-deps", "--no-build-isolation", str(package),
    ]

    module.write_text("MARKER = 'stale'\n", encoding="utf-8")
    first = subprocess.run(command, capture_output=True, text=True, env=env,
                           check=False, timeout=60)
    assert first.returncode == 0, first.stderr
    installed = list(userbase.glob("**/shim_upgrade_probe.py"))
    assert len(installed) == 1
    assert "stale" in installed[0].read_text(encoding="utf-8")

    module.write_text("MARKER = 'checkout'\n", encoding="utf-8")
    second = subprocess.run(command, capture_output=True, text=True, env=env,
                            check=False, timeout=60)
    assert second.returncode == 0, second.stderr
    assert "checkout" in installed[0].read_text(encoding="utf-8")
