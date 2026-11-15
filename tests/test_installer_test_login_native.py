"""The native installer (ops/install-native.*): the tests of test_installer_test_login.py
that the native client path changed, run against the native installer.

test_installer_test_login.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests import installer_sandbox
from tests.test_installer_client_only import BASH, REMOTE, ROOT, _mode, _stub
from tests.test_installer_client_only_native import _Shell
from tests.test_installer_client_only_ps import _output
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_client_only_ps import _block as _ps_block

COMMAND = "test-login create"


def _text(name: str) -> str:
    return (ROOT / "ops" / name).read_text(encoding="utf-8")


def _block(name: str, script: str) -> str:
    text = _text(script)
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


@BASH
def test_sh_takes_the_flag_and_documents_it(bash, tmp_path):
    env = installer_sandbox.environment(tmp_path, {k: v for k, v in os.environ.items()
                                                   if k in ("PATH", "SystemRoot", "TEMP", "TMP", "TMPDIR")})
    proc = installer_sandbox.run([bash, str(ROOT / "ops" / "install-native.sh"), "--test-login", "--help"],
                                 env=installer_sandbox.layout(env, tmp_path), root=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    usage = _text("install-native.sh").split("# <<< usage <<<", 1)[0]
    assert "--test-login" in usage


def _sh(shell: _Shell, *, shim_exit: int = 0, shim: bool = True):
    _stub(shell.bin / "fakeshim",
          "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "exit \"$FAKE_SHIM_EXIT\"\n")
    ensure = ("ensure_shim() { SHIM_OK=1; SHIM_PATH=fakeshim; }\n" if shim
              else "ensure_shim() { SHIM_OK=''; SHIM_PATH=''; }\n")
    return shell.run(
        "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        + ensure
        + _block("test login", "install-native.sh")
        + "\nsetup_test_login\necho DONE\n",
        extra_env={"FAKE_SHIM_EXIT": str(shim_exit)})


@BASH
def test_sh_calls_the_native_client(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell)
    assert proc.returncode == 0, proc.stderr
    assert [line for line in shell.logged() if line.startswith("shim|")] == ["shim|" + COMMAND]
    assert not [line for line in shell.logged() if "python" in line]
    assert "WARNING" not in proc.stderr
    assert "DONE" in proc.stdout


@BASH
def test_sh_a_failure_warns_and_the_install_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, shim_exit=4)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and COMMAND in proc.stderr
    assert "DONE" in proc.stdout


@BASH
def test_sh_without_the_native_client_it_says_so_and_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, shim=False)
    assert proc.returncode == 0, proc.stderr
    assert not [line for line in shell.logged() if line.startswith("shim|")]
    assert "WARNING" in proc.stderr and COMMAND in proc.stderr
    assert "DONE" in proc.stdout


def _ps(ps: _PowerShell, *, shim_exit: int = 0, shim: bool = True):
    ensure = ("function Install-ShimOnce { $script:shimInstallPath = 'fakeshim'; return $true }\n" if shim
              else "function Install-ShimOnce { $script:shimInstallPath = $null; return $false }\n")
    return ps.run(
        ps.stub_function("fakeshim", f"$global:LASTEXITCODE = {shim_exit}")
        + ensure
        + _block("test login", "install-native.ps1")
        + "\nInvoke-TestLoginSetup\nWrite-Output DONE\n")


def test_ps_calls_the_native_client(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps)
    assert proc.returncode == 0, _output(proc)
    [call] = [line for line in ps.logged() if line.startswith("fakeshim|")]
    assert call == "fakeshim|" + COMMAND
    assert "WARNING" not in _output(proc)
    assert "DONE" in proc.stdout


def test_ps_a_failure_warns_and_the_install_goes_on(tmp_path):
    proc = _ps(_PowerShell(tmp_path), shim_exit=4)
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and COMMAND in _output(proc)
    assert "DONE" in proc.stdout


def test_ps_without_the_native_client_it_says_so_and_goes_on(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, shim=False)
    assert proc.returncode == 0, _output(proc)
    assert not [line for line in ps.logged() if line.startswith("fakeshim|")]
    assert "WARNING" in _output(proc) and COMMAND in _output(proc)
    assert "DONE" in proc.stdout
