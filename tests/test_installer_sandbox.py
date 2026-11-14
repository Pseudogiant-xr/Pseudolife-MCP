"""The installer harnesses refuse to start outside a disposable home.

On 14 November 2026 a test-fixing session started ``ops/install.ps1``
directly from its own shell, with the developer's environment; it stopped
only because that release carried no native archives. Every shared harness
that runs installer code now checks its environment with
``tests/installer_sandbox.py`` first; these tests hold that check to what it
promises.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import installer_sandbox
from tests.test_installer_existing_upgrade import _fixture_env


def test_the_developers_own_environment_is_refused(tmp_path):
    with pytest.raises(installer_sandbox.RealHomeRefused) as refused:
        installer_sandbox.require(dict(os.environ), tmp_path)
    assert "outside" in str(refused.value) or "unset" in str(refused.value)


def test_run_refuses_before_anything_starts(tmp_path):
    marker = tmp_path / "started"
    with pytest.raises(installer_sandbox.RealHomeRefused):
        installer_sandbox.run([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                              env=dict(os.environ), root=tmp_path, check=False)
    assert not marker.exists()


def test_a_disposable_home_is_accepted_and_runs(tmp_path):
    env = installer_sandbox.environment(tmp_path, {k: os.environ[k] for k in ("SystemRoot", "PATH") if k in os.environ})
    env["XDG_CONFIG_HOME"] = str(tmp_path / "home" / "config")
    proc = installer_sandbox.run([sys.executable, "-c", "print('ran')"], env=env, root=tmp_path,
                                 capture_output=True, text=True, check=False)
    assert proc.stdout.strip() == "ran"


@pytest.mark.parametrize("name", installer_sandbox.SANDBOXED)
def test_one_real_folder_is_enough_to_refuse(tmp_path, name):
    env = installer_sandbox.environment(tmp_path)
    env[name] = str(Path.home())
    with pytest.raises(installer_sandbox.RealHomeRefused, match=name):
        installer_sandbox.require(env, tmp_path)


@pytest.mark.parametrize("name", installer_sandbox.SANDBOXED)
def test_an_unset_folder_is_refused(tmp_path, name):
    # On Windows a program that finds LOCALAPPDATA or APPDATA unset asks the
    # system for the real folder.
    env = installer_sandbox.environment(tmp_path)
    del env[name]
    with pytest.raises(installer_sandbox.RealHomeRefused, match=f"{name} is unset"):
        installer_sandbox.require(env, tmp_path)


def test_an_xdg_folder_outside_is_refused(tmp_path):
    env = installer_sandbox.environment(tmp_path)
    env["XDG_DATA_HOME"] = str(Path.home() / ".local" / "share")
    with pytest.raises(installer_sandbox.RealHomeRefused, match="XDG_DATA_HOME"):
        installer_sandbox.require(env, tmp_path)


def test_a_sibling_whose_name_starts_like_the_root_is_outside(tmp_path):
    root = tmp_path / "case"
    env = installer_sandbox.environment(root)
    env["APPDATA"] = str(tmp_path / "case-other" / "Roaming")
    with pytest.raises(installer_sandbox.RealHomeRefused, match="APPDATA"):
        installer_sandbox.require(env, root)


def test_a_root_outside_the_temporary_directory_is_refused():
    root = Path.home()
    env = {name: str(root / "x") for name in installer_sandbox.SANDBOXED}
    with pytest.raises(installer_sandbox.RealHomeRefused, match="temporary directory"):
        installer_sandbox.require(env, root)


def test_the_shared_fixture_environment_passes_its_own_check(tmp_path):
    env = _fixture_env(tmp_path / "env")
    installer_sandbox.require(env, tmp_path)
    for name in installer_sandbox.SANDBOXED:
        assert Path(env[name]).is_relative_to(tmp_path)


def test_the_shell_harness_refuses_a_real_home(tmp_path):
    from tests.test_installer_client_only import _Shell

    shell = object.__new__(_Shell)
    shell.bash, shell.tmp = "fixture-bash", tmp_path
    shell.home, shell.bin, shell.calls = (tmp_path / name for name in ("home", "bin", "calls"))
    shell.path = lambda path: "/fixture"
    shell.env = _fixture_env(tmp_path / "env")
    shell.env["USERPROFILE"] = str(Path.home())
    with pytest.raises(installer_sandbox.RealHomeRefused, match="USERPROFILE"):
        shell.run("true\n")


def test_the_powershell_harness_refuses_a_real_home(tmp_path):
    from tests.test_installer_client_only_ps import _PowerShell

    ps = object.__new__(_PowerShell)
    ps.pwsh, ps.tmp = "fixture-pwsh", tmp_path
    ps.env = _fixture_env(tmp_path / "env")
    ps.env["LOCALAPPDATA"] = str(Path.home() / "AppData" / "Local")
    with pytest.raises(installer_sandbox.RealHomeRefused, match="LOCALAPPDATA"):
        ps.run("Write-Output never\n")
    # Refused before any process: the fixture pwsh name would not start.
    assert not (tmp_path / "calls.txt").exists()


@pytest.mark.parametrize("name", installer_sandbox.REDIRECTS)
def test_a_redirect_outside_is_refused_and_one_inside_is_not(tmp_path, name):
    env = installer_sandbox.environment(tmp_path)
    env[name] = str(tmp_path / "home" / "inside")
    installer_sandbox.require(env, tmp_path)
    env[name] = str(Path.home() / ".pseudolife-mcp")
    with pytest.raises(installer_sandbox.RealHomeRefused, match=name):
        installer_sandbox.require(env, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Git Bash path spelling")
def test_a_git_bash_path_is_read_as_the_windows_path(tmp_path):
    env = installer_sandbox.environment(tmp_path)
    text = tmp_path.as_posix()
    env["HOME"] = "/" + text[0].lower() + text[2:] + "/home"
    installer_sandbox.require(env, tmp_path)
    real = Path.home().as_posix()
    env["HOME"] = "/" + real[0].lower() + real[2:]
    with pytest.raises(installer_sandbox.RealHomeRefused, match="HOME"):
        installer_sandbox.require(env, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Git Bash path spelling")
def test_a_git_bash_tmp_path_is_read_where_git_bash_mounts_tmp(tmp_path):
    # Git Bash mounts the user's own temporary folder at /tmp whatever TEMP
    # says; with TEMP at an 8.3 short-name folder inside it (as the hosted
    # runner reaches its own), /tmp is not tempfile.gettempdir().
    from tests.ops_harness import find_bash

    bash = find_bash()
    if not bash:
        pytest.skip("no Git Bash")
    tmp = subprocess.run([bash, "-c", "cygpath -w /tmp"], capture_output=True, text=True,
                         timeout=60, check=True).stdout.strip()
    try:
        inside = Path(os.path.realpath(tmp_path)).relative_to(os.path.realpath(tmp))
    except ValueError:
        pytest.skip("the test directory is not under Git Bash's /tmp")
    env = installer_sandbox.environment(tmp_path)
    env["HOME"] = "/tmp/" + inside.as_posix() + "/home"
    installer_sandbox.require(env, tmp_path)
    env["HOME"] = "/tmp/" + inside.as_posix() + "-elsewhere/home"
    with pytest.raises(installer_sandbox.RealHomeRefused, match="HOME"):
        installer_sandbox.require(env, tmp_path)

