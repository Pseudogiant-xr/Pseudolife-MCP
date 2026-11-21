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

from tests.test_installer_existing_upgrade import user_path_unchanged

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
    # Without PATHEXT, pwsh on Windows starts no native command at all.
    base = {key: os.environ[key] for key in ("PATH", "PATHEXT", "ComSpec", "SystemRoot", "TEMP", "TMP")
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
    with user_path_unchanged(tmp_path):
        return subprocess.run([BASH, _bash_path(repo / "ops" / "install.sh"), *args], env=env,
                              capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)


def _ps_stub(ending: str = "exit 7\n") -> str:
    # A native installer with the real one's parameter block, that logs how
    # it was bound: what reaches it is what the real one would receive.
    text = (ROOT / "ops" / "install-native.ps1").read_text(encoding="utf-8").replace("\r\n", "\n") \
        if (ROOT / "ops" / "install-native.ps1").exists() else ""
    params = text[text.index("\nparam(") + 1:].split("\n)\n", 1)[0] + "\n)\n" if "\nparam(" in text else "param()\n"
    return (params
            + "Set-Content -LiteralPath $env:NATIVE_ARGS -Value 'started'\n"
            + "foreach ($key in ($PSBoundParameters.Keys | Sort-Object)) { Add-Content -LiteralPath $env:NATIVE_ARGS -Value \"$key=$($PSBoundParameters[$key])\" }\n"
            + ending)


def _ps(tmp_path: Path, engine: str | None, *args: str, ending: str = "exit 7\n") -> subprocess.CompletedProcess[str]:
    if PWSH is None:
        pytest.skip("needs pwsh")
    repo = _repo(tmp_path, "install.ps1", _ps_stub(ending), "install-native.ps1")
    env = _env(tmp_path, engine)
    with user_path_unchanged(tmp_path):
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


@pytest.mark.parametrize("engine, tunnel", [
    ("native", True), ("native", False), ("python", True),
], ids=["native-with-tunnel", "native-without-tunnel", "python-with-tunnel"])
@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_tunnel_switch_is_only_for_the_python_installer(tmp_path, engine, tunnel, shell):
    # Exercise the actual routing and argument prologues, stopping the native
    # installer at a stand-in for its first write. The Python tail gets a fake
    # installed shim in place of a real guided setup.
    program = BASH if shell == "bash" else PWSH
    if program is None:
        pytest.skip(f"needs {shell}")
    suffix = ".sh" if shell == "bash" else ".ps1"
    name = "install" + suffix
    native_name = "install-native" + suffix
    early = "# >>> extractor modes >>>"
    python = (ROOT / "ops" / name).read_text(encoding="utf-8")
    native = (ROOT / "ops" / native_name).read_text(encoding="utf-8").split(early, 1)[0]
    if shell == "bash":
        native += 'printf "%s\\n" "native install reached" >"$NATIVE_ARGS"\n'
        tail = '\nif [ -n "$TUNNEL" ] && [ -n "${INSTALLER_UNATTENDED:-}" ]; then'
        setup = ('\nINSTALLER_UNATTENDED=""\nSHIM_PATH=tunnel_fixture\n'
                 'tunnel_fixture() { printf "%s\\n" "$*" >"$NATIVE_ARGS"; }\n')
        switch = "--tunnel"
    else:
        native += "Set-Content -LiteralPath $env:NATIVE_ARGS -Value 'native install reached'\n"
        tail = "\nif ($Tunnel -and -not $interactive) {"
        setup = ("\n$interactive = $true\n$boardFile = $null\n$script:shimInstallPath = 'Invoke-TunnelFixture'\n"
                 'function Invoke-TunnelFixture { Set-Content -LiteralPath $env:NATIVE_ARGS -Value ($args -join " "); $global:LASTEXITCODE = 0 }\n')
        switch = "-Tunnel"
    repo = _repo(tmp_path, name, native, native_name)
    entry = repo / "ops" / name
    entry.write_text(python.split(early, 1)[0] + setup + python[python.index(tail):], encoding="utf-8", newline="\n")
    env = _env(tmp_path, engine)
    command = ([program, _bash_path(entry)] if shell == "bash" else
               [program, "-NoProfile", "-NonInteractive", "-File", str(entry)])
    with user_path_unchanged(tmp_path):
        proc = subprocess.run([*command, *([switch] if tunnel else [])], env=env,
                              capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    output = proc.stdout + proc.stderr
    if engine == "native" and tunnel:
        assert proc.returncode == 1, output
        assert _native_args(tmp_path) is None
        assert not list((tmp_path / "home").iterdir())
        assert "The native client has no guided tunnel setup yet" in output
        assert "Nothing was changed" in output
        assert f"Rerun without {switch}" in output
        assert "pseudolife-mcp tunnel setup --daemon-url" in output
        assert "--token-file" in output and "--accept-access" in output
    else:
        expected = "native install reached" if engine == "native" else "tunnel setup"
        assert proc.returncode == 0, output
        assert _native_args(tmp_path) == [expected]


# A native command that fails along the way (a probe the native installer
# expects to fail, such as schtasks /Query for a task that is not there)
# leaves $LASTEXITCODE non-zero; how the native installer ends decides.
FAILED_PROBE = ("if ($IsWindows) { & (Join-Path $env:SystemRoot 'System32' 'cmd.exe') /c 'exit 3' } "
                "else { & /bin/sh -c 'exit 3' }\n")


@pytest.mark.parametrize("ending, status", [
    (FAILED_PROBE + "'installed'\n", 0),          # ends normally: success, as when run itself
    (FAILED_PROBE + "exit 0\n", 0),
    (FAILED_PROBE + "exit 5\n", 5),
    ("$ErrorActionPreference = 'Stop'\nthrow 'boom'\n", 1),
])
def test_ps_native_exits_as_the_native_installer_run_itself_would(tmp_path, ending, status):
    proc = _ps(tmp_path, "native", "-NoArt", ending=ending)
    assert proc.returncode == status, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["started", "NoArt=True"]


def test_ps_native_without_the_native_installer_refuses(tmp_path):
    if PWSH is None:
        pytest.skip("needs pwsh")
    repo = _repo(tmp_path, "install.ps1", _ps_stub(), "install-native.ps1")
    (repo / "ops" / "install-native.ps1").unlink()
    with user_path_unchanged(tmp_path):
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "install.ps1"), "-NoArt"],
                              env=_env(tmp_path, "native"), capture_output=True, text=True, timeout=120, check=False,
                              stdin=subprocess.DEVNULL)
    assert proc.returncode == 1
    assert "install-native.ps1" in proc.stderr and "Nothing was changed" in proc.stderr


def test_sh_native_without_the_native_installer_refuses(tmp_path):
    if BASH is None:
        pytest.skip("needs bash")
    repo = _repo(tmp_path, "install.sh", SH_STUB, "install-native.sh")
    (repo / "ops" / "install-native.sh").unlink()
    with user_path_unchanged(tmp_path):
        proc = subprocess.run([BASH, _bash_path(repo / "ops" / "install.sh"), "--yes"], env=_env(tmp_path, "native"),
                              capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert proc.returncode == 1
    assert "install-native.sh" in proc.stderr and "Nothing was changed" in proc.stderr


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


@pytest.mark.parametrize("native,line", [
    ("install-native.sh", 'echo "Native client install (PSEUDOLIFE_SHIM_ENGINE=native): rerun it with that variable set;'),
    ("install-native.ps1", 'Write-Host "Native client install (PSEUDOLIFE_SHIM_ENGINE=native): rerun it with that variable set;'),
])
def test_the_native_installer_says_a_rerun_needs_the_variable(native, line):
    # A rerun without the variable takes the Python client's path, so every
    # native run says how to rerun it, once, near its start, and its usage
    # names the variable.
    text = (ROOT / "ops" / native).read_text(encoding="utf-8")
    assert text.count(line) == 1
    assert text.index(line) < text.index("# >>> extractor modes >>>")
    usage = text.split("# >>> usage >>>", 1)[1].split("# <<< usage <<<", 1)[0] if "# >>> usage >>>" in text \
        else text.split("param(", 1)[0]
    assert "PSEUDOLIFE_SHIM_ENGINE=native" in usage


@pytest.mark.parametrize("rel", sorted(str(path.relative_to(ROOT)).replace("\\", "/")
                                      for path in (ROOT / "ops").glob("*-native.ps1")))
def test_the_native_powershell_scripts_parse(rel):
    # tests/test_client_install_ux.py::test_installer_ps1_parses parses the
    # default installer's scripts; the native ones are only run, so a parse
    # error in a branch no test reaches would ship.
    if PWSH is None:
        pytest.skip("needs pwsh")
    path = str(ROOT / rel).replace("'", "''")
    script = ("$e=$null;"
              "[System.Management.Automation.Language.Parser]::ParseFile("
              f"'{path}',[ref]$null,[ref]$e)|Out-Null;"
              "if($e){$e|ForEach-Object{[Console]::Error.WriteLine($_.Message)};exit 1}")
    proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"{rel} failed to parse: {proc.stderr}"
