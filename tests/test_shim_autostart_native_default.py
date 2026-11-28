"""The standalone shim autostart wrappers register the shim through the
installed native client on a host without Python.

`ops/install-shim-autostart.*` and `ops/install-codex-shim-autostart.*` used
the native client only when `--native` / `-NativeExe` named it; otherwise
they needed Python to pick the unit's interpreter and refused without it,
telling the user to install Python. Where no Python is on PATH they now find
the native launcher where the installers put it (or
`PSEUDOLIFE_SHIM_LAUNCHER`), check that it answers as a native client, and
run its `extractor-shim install-autostart`, as the installers do. A host
with Python keeps the Python runner and is told the `--native` command,
since switching a registered runner is the user's choice.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else None
PWSH = shutil.which("pwsh")
CAPABILITY = "pseudolife-mcp native-update-v1"
FAMILIES = {"claude": "install-shim-autostart", "codex": "install-codex-shim-autostart"}


def _checkout(tmp_path: Path, script: str) -> Path:
    root = tmp_path / "checkout"
    (root / "ops").mkdir(parents=True)
    (root / "evals" / "prompts").mkdir(parents=True)
    (root / "evals" / "prompts" / "sonnet_extractor_v5.md").write_text("prompt\n", encoding="utf-8")
    for name in (script, "scheduled_task_lock.ps1"):
        if (ROOT / "ops" / name).exists():
            shutil.copy2(ROOT / "ops" / name, root / "ops" / name)
    return root


def _stub(path: Path, answer: str = CAPABILITY) -> Path:
    """A native launcher: it answers the capability probe and records any
    other call's arguments, one per line, next to itself."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = path.with_suffix(".cmd")
        path.write_text(
            "@echo off\r\n"
            f'if "%~1 %~2"=="update --native-capability" (echo {answer}& exit /b 0)\r\n'
            'echo %* > "%~dp0launcher-args"\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        path.write_text(
            f"#!{shutil.which('bash')}\n"
            f'if [ "$1 $2" = "update --native-capability" ]; then echo "{answer}"; exit 0; fi\n'
            'printf "%s\\n" "$@" > "${0%/*}/launcher-args"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    return path


def _tool_dir(tmp_path: Path) -> Path:
    """A PATH with the shell tools the wrappers use, the family CLIs and
    systemctl as stubs, and no Python."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "sed", "head", "mkdir", "cat"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    for name in ("claude", "codex", "systemctl"):
        (tools / name).write_text(f"#!{shutil.which('bash')}\nexit 0\n", encoding="utf-8")
        (tools / name).chmod(0o755)
    return tools


def _run_sh(tmp_path, family, *args, env_extra=None):
    root = _checkout(tmp_path, FAMILIES[family] + ".sh")
    env = {"HOME": str(tmp_path / "home"), "PATH": str(_tool_dir(tmp_path))}
    env.update(env_extra or {})
    proc = subprocess.run([BASH, str(root / "ops" / (FAMILIES[family] + ".sh")), *args],
                          capture_output=True, text=True, timeout=60, env=env)
    return root, proc


def _default_launcher(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".local" / "share" / "pseudolife-mcp" / "bin" / "pseudolife-mcp"


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_registers_through_the_installed_native_client_without_python(tmp_path, family):
    launcher = _stub(_default_launcher(tmp_path))
    root, proc = _run_sh(tmp_path, family)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = (launcher.parent / "launcher-args").read_text(encoding="utf-8").splitlines()
    assert recorded[:3] == ["extractor-shim", "install-autostart", family], recorded
    assert recorded[recorded.index("--repo") + 1] == str(root)
    assert recorded[recorded.index("--native-exe") + 1] == str(launcher)
    # No docker0 here (no `ip` at all): no bridge address is guessed, so the
    # native client binds its loopback default.
    assert "--host" not in recorded, recorded
    assert "registering through the installed native client" in proc.stdout + proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_honours_the_launcher_override(tmp_path, family):
    launcher = _stub(tmp_path / "elsewhere" / "pseudolife-mcp")
    _, proc = _run_sh(tmp_path, family, env_extra={"PSEUDOLIFE_SHIM_LAUNCHER": str(launcher)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = (launcher.parent / "launcher-args").read_text(encoding="utf-8").splitlines()
    assert recorded[recorded.index("--native-exe") + 1] == str(launcher)


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_python_flag_keeps_the_python_runner(tmp_path, family):
    # The near miss: a named interpreter is the Python runner's, so the
    # installed native client is not used.
    launcher = _stub(_default_launcher(tmp_path))
    _, proc = _run_sh(tmp_path, family, "--python", str(tmp_path / "no-such-python"))
    assert proc.returncode == 1
    assert not (launcher.parent / "launcher-args").exists()
    assert "drop --python" in proc.stderr, proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_on_a_host_with_python_keeps_the_python_runner_and_names_the_switch(tmp_path, family):
    # The other near miss: Python on PATH keeps the runner the host has; the
    # wrapper prints the one command that switches it instead of switching.
    launcher = _stub(_default_launcher(tmp_path))
    root = _checkout(tmp_path, FAMILIES[family] + ".sh")
    tools = _tool_dir(tmp_path)
    (tools / "python3").write_text(f"#!{shutil.which('bash')}\nexit 1\n", encoding="utf-8")
    (tools / "python3").chmod(0o755)
    script = root / "ops" / (FAMILIES[family] + ".sh")
    proc = subprocess.run([BASH, str(script)], capture_output=True, text=True, timeout=60,
                          env={"HOME": str(tmp_path / "home"), "PATH": str(tools)})
    assert proc.returncode == 1
    assert not (launcher.parent / "launcher-args").exists()
    assert f"{script} --native {launcher}" in proc.stdout, proc.stdout


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_without_a_native_client_or_python_names_the_python_free_way(tmp_path, family):
    # A file that does not answer as a native client is not one.
    launcher = _stub(_default_launcher(tmp_path), answer="something else")
    _, proc = _run_sh(tmp_path, family)
    assert proc.returncode == 1
    assert not (launcher.parent / "launcher-args").exists()
    assert "ops/install.sh" in proc.stderr and "--native" in proc.stderr, proc.stderr
    assert "install Python" not in proc.stderr


@pytest.mark.skipif(BASH is None, reason="bash wrappers run on Linux and macOS")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_sh_wrapper_without_systemd_names_the_native_shim(tmp_path, family):
    _stub(_default_launcher(tmp_path))
    root = _checkout(tmp_path, FAMILIES[family] + ".sh")
    tools = _tool_dir(tmp_path)
    (tools / "systemctl").unlink()
    proc = subprocess.run([BASH, str(root / "ops" / (FAMILIES[family] + ".sh"))], capture_output=True,
                          text=True, timeout=60, env={"HOME": str(tmp_path / "home"), "PATH": str(tools)})
    assert proc.returncode == 1
    assert f"extractor-shim {family} --repo {root}" in proc.stderr, proc.stderr
    assert "_shim.py" not in proc.stderr


def _recorded(launcher: Path) -> str:
    """The stub's recorded call as one space-separated line (the bash stub
    writes one argument per line, the Windows one a single line)."""
    return " ".join((launcher.parent / "launcher-args").read_text(encoding="utf-8").split())


def _run_ps1(tmp_path, family, *args, launcher_env=None, python=False):
    root = _checkout(tmp_path, FAMILIES[family] + ".ps1")
    env = dict(os.environ)
    env.pop("PSEUDOLIFE_SHIM_LAUNCHER", None)
    # No Python on PATH, unless the case gives one that fails as an
    # interpreter would for a missing picker.
    bin_dir = tmp_path / "ps-bin"
    bin_dir.mkdir(exist_ok=True)
    env["PATH"] = os.pathsep.join([str(bin_dir)] + ([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
                                                     if os.name == "nt" else []))
    if python:
        if os.name == "nt":
            (bin_dir / "python.cmd").write_text("@exit /b 1\r\n", encoding="utf-8")
        else:
            (bin_dir / "python").write_text(f"#!{shutil.which('bash')}\nexit 1\n", encoding="utf-8")
            (bin_dir / "python").chmod(0o755)
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    env["USERPROFILE"] = str(tmp_path / "home")
    if launcher_env:
        env["PSEUDOLIFE_SHIM_LAUNCHER"] = launcher_env
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "ops" / (FAMILIES[family] + ".ps1")), *args],
                          capture_output=True, text=True, timeout=120, env=env)
    return root, proc


@pytest.mark.skipif(PWSH is None, reason="pwsh not available")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_ps1_wrapper_registers_through_the_native_client_it_finds(tmp_path, family):
    launcher = _stub(tmp_path / "elsewhere" / "pseudolife-mcp")
    root, proc = _run_ps1(tmp_path, family, launcher_env=str(launcher))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = _recorded(launcher)
    assert f"extractor-shim install-autostart {family}" in recorded, recorded
    assert str(launcher) in recorded and str(root) in recorded


@pytest.mark.skipif(PWSH is None or os.name == "nt",
                    reason="the default-path stub must be an executable named .exe")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_ps1_wrapper_finds_the_launcher_where_the_installer_puts_it(tmp_path, family):
    launcher = _stub(tmp_path / "local" / "pseudolife-mcp" / "bin" / "pseudolife-mcp.exe")
    _, proc = _run_ps1(tmp_path, family)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"extractor-shim install-autostart {family}" in _recorded(launcher)


@pytest.mark.skipif(PWSH is None, reason="pwsh not available")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_ps1_wrapper_python_flag_keeps_the_python_runner(tmp_path, family):
    launcher = _stub(tmp_path / "elsewhere" / "pseudolife-mcp")
    _, proc = _run_ps1(tmp_path, family, "-PythonExe", str(tmp_path / "no-such-python"), launcher_env=str(launcher))
    # Where it stops depends on the host's own scheduled tasks (read only);
    # what matters is that the native client was not used.
    assert proc.returncode != 0
    assert not (launcher.parent / "launcher-args").exists()
    text = (ROOT / "ops" / (FAMILIES[family] + ".ps1")).read_text(encoding="utf-8")
    assert "drop -PythonExe to register through the native client" in text


@pytest.mark.skipif(PWSH is None, reason="pwsh not available")
@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_ps1_wrapper_on_a_host_with_python_keeps_the_python_runner_and_names_the_switch(tmp_path, family):
    launcher = _stub(tmp_path / "elsewhere" / "pseudolife-mcp")
    _, proc = _run_ps1(tmp_path, family, launcher_env=str(launcher), python=True)
    assert proc.returncode != 0
    assert not (launcher.parent / "launcher-args").exists()
    assert f"-NativeExe '{launcher}'" in proc.stdout, proc.stdout + proc.stderr


def test_the_guides_no_longer_tell_users_to_run_the_python_shims():
    for rel in ("docs/guide/dreaming.md", "docs/guide/install-and-move.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "python evals/" not in text, rel
        assert "python $repo/evals/" not in text, rel
