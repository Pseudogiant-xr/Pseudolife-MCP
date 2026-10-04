"""The installers set up the test suite's own Postgres login, on request.

With `--test-login` / `-TestLogin`, an install (and a re-run) calls
`pseudolife-mcp test-login create`'s module from the checkout once the stack
is healthy, so the checkout's tests log in as a role that cannot open the
bank instead of the bank owner through ops/.env (2026-10-04). It is off by
default: an end user's server gets no CREATEDB password login it has no use
for (review, 2026-10-04); contributors who run the suite against the bundled
server opt in. It is idempotent, and a failure there leaves the install
working: a warning names the command to run later. Blocks run extracted from
ops/install.sh and ops/install.ps1 with stub commands.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from tests.test_installer_client_only import BASH, REMOTE, ROOT, _Shell, _mode, _stub
from tests.test_installer_client_only_ps import _PowerShell, _output
from tests.test_installer_client_only_ps import _block as _ps_block

MODULE = "-m pseudolife_memory.test_login_cli create"


def _text(name: str) -> str:
    return (ROOT / "ops" / name).read_text(encoding="utf-8")


def _block(name: str, script: str) -> str:
    text = _text(script)
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


@pytest.mark.parametrize("script,call,health", [
    ("install.sh", '\n    if [ -n "${TEST_LOGIN:-}" ]; then\n        setup_test_login\n    fi\n',
     'step "Healthy: http://127.0.0.1:8765/health'),
    ("install.ps1", "\n    if ($TestLogin) { Invoke-TestLoginSetup }\n",
     'Step "Healthy: http://127.0.0.1:8765/health'),
])
def test_it_runs_once_the_local_stack_is_healthy_and_only_when_asked(script, call, health):
    text = _text(script)
    assert text.count(call) == 1, f"{script} does not call the setup behind its flag"
    name = "setup_test_login" if script.endswith(".sh") else "Invoke-TestLoginSetup"
    calls = [line for line in text.splitlines()
             if name in line and not line.lstrip().startswith(("#", "function ", f"{name}()"))]
    assert len(calls) == 1, calls  # no unguarded call anywhere else
    healthy = text.index(health)
    called = text.index(call)
    assert healthy < called < text.index("# >>> client-only claude hook >>>")


@BASH
def test_sh_takes_the_flag_and_documents_it(bash):
    proc = subprocess.run([bash, str(ROOT / "ops" / "install.sh"), "--test-login", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    usage = _text("install.sh").split("# <<< usage <<<", 1)[0]
    assert "--test-login" in usage


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not available")
def test_ps_takes_the_switch_and_documents_it():
    script = str(ROOT / "ops" / "install.ps1").replace("'", "''")
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-Command '{script}').Parameters['TestLogin'].SwitchParameter"],
        capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "True", proc.stdout + proc.stderr
    header = _text("install.ps1").split("param(", 1)[0]
    assert "-TestLogin" in header


@BASH
def test_sh_client_only_refuses_the_flag(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=REMOTE, prelude="TEST_LOGIN=1\n")
    assert proc.returncode == 2
    assert "--test-login" in proc.stderr


def test_ps_client_only_refuses_the_switch(tmp_path):
    proc = _PowerShell(tmp_path).run(
        "$PairingCode = ''\n"
        f"$DaemonUrl = '{REMOTE}'\n$ReadToken = [switch]$false\n$ClientOnly = [switch]$false\n"
        "$TokenFile = ''\n$Extractor = ''\n$Model = ''\n$ShimPort = 0\n"
        "$NoToken = [switch]$false\n$Transport = 'shim'\n$TestLogin = [switch]$true\n"
        + _ps_block("client-only mode"))
    assert proc.returncode == 2
    assert "-TestLogin" in _output(proc)


# -- install.sh ----------------------------------------------------------------

def _sh(shell: _Shell, *, python_exit: int = 0, python: bool = True):
    _stub(shell.bin / "fakepy",
          "printf 'python|%s|PYTHONPATH=%s\\n' \"$*\" \"$PYTHONPATH\" >>\"$CALL_LOG\"\n"
          "exit \"$FAKE_PY_EXIT\"\n")
    return shell.run(
        "step() { printf 'STEP: %s\\n' \"$*\"; }\n"
        f"installer_python() {{ {'echo fakepy' if python else ':'}; }}\n"
        + _block("test login", "install.sh")
        + "\nsetup_test_login\necho DONE\n",
        extra_env={"FAKE_PY_EXIT": str(python_exit)})


@BASH
def test_sh_calls_the_checkouts_module(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell)
    assert proc.returncode == 0, proc.stderr
    [call] = [line for line in shell.logged() if line.startswith("python|")]
    assert MODULE in call
    assert "WARNING" not in proc.stderr
    assert "DONE" in proc.stdout


@BASH
def test_sh_a_failure_warns_and_the_install_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, python_exit=4)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and "test_login_cli create" in proc.stderr
    assert "DONE" in proc.stdout


@BASH
def test_sh_without_python_it_says_so_and_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, python=False)
    assert proc.returncode == 0, proc.stderr
    assert not [line for line in shell.logged() if line.startswith("python|")]
    assert "WARNING" in proc.stderr and "test-login create" in proc.stderr
    assert "DONE" in proc.stdout


# -- install.ps1 ---------------------------------------------------------------

def _ps(ps: _PowerShell, *, python_exit: int = 0, python: bool = True):
    return ps.run(
        ps.stub_function("fakepy", f"$global:LASTEXITCODE = {python_exit}")
        + ("function Get-InstallerPython { 'fakepy' }\n" if python
           else "function Get-InstallerPython { $null }\n")
        + _block("test login", "install.ps1")
        + "\nInvoke-TestLoginSetup\nWrite-Output DONE\n")


def test_ps_calls_the_checkouts_module(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps)
    assert proc.returncode == 0, _output(proc)
    [call] = [line for line in ps.logged() if line.startswith("fakepy|")]
    assert MODULE in call
    assert "WARNING" not in _output(proc)
    assert "DONE" in proc.stdout


def test_ps_a_failure_warns_and_the_install_goes_on(tmp_path):
    proc = _ps(_PowerShell(tmp_path), python_exit=4)
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and "test_login_cli create" in _output(proc)
    assert "DONE" in proc.stdout


def test_ps_without_python_it_says_so_and_goes_on(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, python=False)
    assert proc.returncode == 0, _output(proc)
    assert not [line for line in ps.logged() if line.startswith("fakepy|")]
    assert "WARNING" in _output(proc) and "test-login create" in _output(proc)
    assert "DONE" in proc.stdout
