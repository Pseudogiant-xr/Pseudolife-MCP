"""Which client the installers install: the Python client unless asked.

In 0.24.x the move to the native client is opt-in: ``ops/install.sh`` and
``ops/install.ps1`` install the Python client (``ops/shim_runtime.py install``,
then pipx and pip) unless ``PSEUDOLIFE_SHIM_ENGINE=native`` asks for the native
client, which ``ops/install-native.sh`` / ``ops/install-native.ps1`` install.
``PSEUDOLIFE_SHIM_ENGINE=python`` is the default spelled out, and the way back
the 0.24.0 release notes name; any other value is refused before anything
changes. Each case runs a copy of the real script whose native installer is a
stub that logs its arguments.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ops_harness import find_bash

ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
BASH = find_bash() if os.name == "nt" else shutil.which("bash")
REFUSAL = "PSEUDOLIFE_SHIM_ENGINE"


def _bash_path(path: Path) -> str:
    text = path.as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _repo(tmp_path: Path, script: str, stub: str, stub_name: str) -> Path:
    """A copy of the real installer, beside a stub native installer."""
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copyfile(ROOT / "ops" / script, repo / "ops" / script)
    (repo / "ops" / stub_name).write_text(stub, encoding="utf-8", newline="\n")
    (repo / "ops" / stub_name).chmod(0o755)
    return repo


def _env(tmp_path: Path, engine: str | None) -> dict[str, str]:
    base = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ}
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(base, HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"),
               APPDATA=str(home / "AppData" / "Roaming"), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local" / "share"), CODEX_HOME=str(home / ".codex"),
               CLAUDE_CONFIG_DIR=str(home / ".claude"))
    # Never the real User PATH: a layout override inside the home.
    env["PSEUDOLIFE_SHIM_RUNTIMES"] = str(home / "runtimes")
    env["PSEUDOLIFE_SHIM_LAUNCHER"] = str(home / "bin" / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"))
    env["NATIVE_ARGS"] = str(tmp_path / "native-args")
    env["NO_COLOR"] = "1"
    if engine is not None:
        env["PSEUDOLIFE_SHIM_ENGINE"] = engine
    return env


SH_STUB = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >"$NATIVE_ARGS"\nexit 7\n'


def _sh(tmp_path: Path, engine: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("needs bash")
    repo = _repo(tmp_path, "install.sh", SH_STUB, "install-native.sh")
    env = _env(tmp_path, engine)
    return subprocess.run([BASH, _bash_path(repo / "ops" / "install.sh"), *args], env=env,
                          capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)


def _ps_stub() -> str:
    # A native installer with the real one's parameter block, that logs how
    # it was bound: what reaches it is what the real one would receive.
    text = (ROOT / "ops" / "install-native.ps1").read_text(encoding="utf-8").replace("\r\n", "\n") \
        if (ROOT / "ops" / "install-native.ps1").exists() else ""
    params = text[text.index("\nparam(") + 1:].split("\n)\n", 1)[0] + "\n)\n" if "\nparam(" in text else "param()\n"
    return (params
            + "Set-Content -LiteralPath $env:NATIVE_ARGS -Value 'started'\n"
            + "foreach ($key in ($PSBoundParameters.Keys | Sort-Object)) { Add-Content -LiteralPath $env:NATIVE_ARGS -Value \"$key=$($PSBoundParameters[$key])\" }\n"
            + "exit 7\n")


def _ps(tmp_path: Path, engine: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    if PWSH is None:
        pytest.skip("needs pwsh")
    repo = _repo(tmp_path, "install.ps1", _ps_stub(), "install-native.ps1")
    env = _env(tmp_path, engine)
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "install.ps1"), *args],
                          env=env, capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)


def _native_args(tmp_path: Path) -> list[str] | None:
    log = tmp_path / "native-args"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else None


# -- ops/install.sh ------------------------------------------------------------

@pytest.mark.parametrize("engine", [None, "", "python"])
def test_sh_default_and_python_run_the_python_installer(tmp_path, engine):
    proc = _sh(tmp_path, engine, "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None
    assert "--client-only" in proc.stdout


def test_sh_native_hands_every_argument_to_the_native_installer(tmp_path):
    proc = _sh(tmp_path, "native", "--client-only", "--daemon-url", "http://pl.example.invalid:8765", "--yes")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["--client-only", "--daemon-url", "http://pl.example.invalid:8765", "--yes"]


@pytest.mark.parametrize("engine", ["rust", "Native", "pip"])
def test_sh_any_other_engine_is_refused_before_anything_changes(tmp_path, engine):
    proc = _sh(tmp_path, engine, "--client-only", "--daemon-url", "http://pl.example.invalid:8765")
    assert proc.returncode == 2
    assert _native_args(tmp_path) is None
    [line] = [text for text in proc.stderr.splitlines() if REFUSAL in text]
    assert f"PSEUDOLIFE_SHIM_ENGINE={engine}" in line
    assert "python" in line and "native" in line and "Nothing was changed" in line
    assert proc.stdout == ""


# -- ops/install.ps1 -----------------------------------------------------------

@pytest.mark.parametrize("engine", [None, "", "python"])
def test_ps_default_and_python_run_the_python_installer(tmp_path, engine):
    proc = _ps(tmp_path, engine, "-ClientOnly", "-NoArt")
    # Master's own refusal, from the Python installer's parameter checks.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "-ClientOnly needs" in proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None


def test_ps_native_hands_every_parameter_to_the_native_installer(tmp_path):
    proc = _ps(tmp_path, "native", "-ClientOnly", "-DaemonUrl", "http://pl.example.invalid:8765", "-Yes", "-NoArt")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["started", "ClientOnly=True", "DaemonUrl=http://pl.example.invalid:8765",
                                      "NoArt=True", "Yes=True"]


@pytest.mark.parametrize("engine", ["rust", "Native", "pip"])
def test_ps_any_other_engine_is_refused_before_anything_changes(tmp_path, engine):
    proc = _ps(tmp_path, engine, "-ClientOnly", "-DaemonUrl", "http://pl.example.invalid:8765", "-NoArt")
    assert proc.returncode == 2
    assert _native_args(tmp_path) is None
    [line] = [text for text in (proc.stdout + proc.stderr).splitlines() if REFUSAL in text]
    assert f"PSEUDOLIFE_SHIM_ENGINE={engine}" in line
    assert "python" in line and "native" in line and "Nothing was changed" in line


# -- the default path is master's: Python, and nothing moves to native ---------

@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_the_default_installer_installs_the_python_client_and_moves_nothing_to_native(script):
    text = (ROOT / "ops" / script).read_text(encoding="utf-8")
    engine = text.split("# >>> client engine >>>", 1)[1].split("# <<< client engine <<<", 1)
    assert len(engine) == 2, "the client engine block is missing"
    rest = text.split("# <<< client engine <<<", 1)[1]
    assert "shim_runtime.py" in rest
    # No registration is moved to a native launcher, and no native client is
    # downloaded, on the default path.
    for native_only in ("migrate-registrations", "native_install", "Install-NativeClient",
                        "PSEUDOLIFE_NATIVE_ARCHIVE_URL"):
        assert native_only not in rest, native_only


@pytest.mark.parametrize("script,native", [("install.sh", "install-native.sh"), ("install.ps1", "install-native.ps1")])
def test_the_native_installer_is_the_one_that_moves_registrations(script, native):
    text = (ROOT / "ops" / native).read_text(encoding="utf-8")
    assert "migrate-registrations" in text and "shim_runtime.py" not in text
