"""Which client the installers install: the native client unless told.

Since 0.26.0 ``ops/install.sh`` and ``ops/install.ps1`` install the native
client (``ops/install-native.sh`` / ``ops/install-native.ps1``) by default and
with ``PSEUDOLIFE_SHIM_ENGINE=native``. ``PSEUDOLIFE_SHIM_ENGINE=python``
installs the Python client (``ops/shim_runtime.py install``, then pipx and
pip) and records that choice in ``~/.pseudolife-mcp/client-engine``, which a
later run without the variable honours. A defaulted run whose native installer
hands back (the native client cannot serve the machine) carries on with the
Python client and records nothing. Any other value is refused before anything
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


SH_STUB = ('#!/usr/bin/env bash\nprintf "%s\\n" "$@" >"$NATIVE_ARGS"\n'
           # Says whether the wrapper marked the run as defaulted.
           'printf "%s\\n" "${PSEUDOLIFE_INSTALLER_DEFAULTED-unset}" >"$NATIVE_ARGS.defaulted"\n'
           'exit 7\n')


def _sh(tmp_path: Path, engine: str | None, *args: str,
        extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("needs bash")
    repo = _repo(tmp_path, "install.sh", SH_STUB, "install-native.sh")
    env = dict(_env(tmp_path, engine), **(extra or {}))
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

def test_sh_python_runs_the_python_installer(tmp_path):
    proc = _sh(tmp_path, "python", "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None
    assert "--client-only" in proc.stdout
    # Help installs nothing, so nothing is recorded.
    assert not (tmp_path / "home" / ".pseudolife-mcp" / "client-engine").exists()


@pytest.mark.parametrize("engine", [None, ""])
def test_sh_default_runs_the_native_installer_marked_as_defaulted(tmp_path, engine):
    proc = _sh(tmp_path, engine, "--client-only", "--daemon-url", "http://pl.example.invalid:8765", "--yes")
    # It replaces the wrapper (exec), so its exit is the run's and the
    # Python installer never starts.
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["--client-only", "--daemon-url", "http://pl.example.invalid:8765", "--yes"]
    assert (tmp_path / "native-args.defaulted").read_text(encoding="utf-8").strip() == "1"
    assert "==>" not in proc.stdout


def test_sh_explicit_native_is_never_marked_as_defaulted(tmp_path):
    # Not even when the caller's environment carries the mark.
    proc = _sh(tmp_path, "native", "--yes", extra={"PSEUDOLIFE_INSTALLER_DEFAULTED": "1"})
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert (tmp_path / "native-args.defaulted").read_text(encoding="utf-8").strip() == ""


HAND_BACK = ("the native client cannot serve this machine (no native client is published for this platform), so "
             "this install uses the Python client; PSEUDOLIFE_SHIM_ENGINE=native makes this a refusal, and "
             "PSEUDOLIFE_SHIM_ENGINE=python chooses the Python client for later runs too")


def test_sh_a_hand_back_runs_the_python_installer_and_says_its_line(tmp_path):
    # What the native installer's hand-back execs: this script with =python.
    proc = _sh(tmp_path, "python", "--help", extra={"PSEUDOLIFE_INSTALLER_FALLBACK": HAND_BACK})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None
    assert f"==> {HAND_BACK}" in proc.stdout.splitlines()
    assert "--client-only" in proc.stdout, "the Python installer's usage"


def _engine_block_then_record(tmp_path: Path, engine: str | None, extra: dict[str, str]) -> subprocess.CompletedProcess:
    # The real client engine block, then the record step the install's end
    # runs: which runs record a Python choice.
    if BASH is None:
        pytest.skip("needs bash")
    text = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    head = text.split("# <<< client engine <<<", 1)[0]
    script = tmp_path / "engine.sh"
    script.write_text(head + "\nrecord_client_engine\necho END\n", encoding="utf-8", newline="\n")
    env = dict(_env(tmp_path, engine), **extra)
    return subprocess.run([BASH, _bash_path(script), "--yes"], env=env, capture_output=True, text=True, timeout=120,
                          check=False, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("case, engine, extra, recorded", [
    ("explicit-python", "python", {}, True),
    ("hand-back", "python", {"PSEUDOLIFE_INSTALLER_FALLBACK": HAND_BACK}, False),
])
def test_sh_only_an_explicit_python_choice_is_recorded(tmp_path, case, engine, extra, recorded):
    proc = _engine_block_then_record(tmp_path, engine, extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "END" in proc.stdout
    record = tmp_path / "home" / ".pseudolife-mcp" / "client-engine"
    assert record.exists() is recorded
    if recorded:
        assert record.read_text(encoding="utf-8") == "python\n"


def test_sh_a_recorded_choice_is_kept_and_not_rewritten(tmp_path):
    record = _record(tmp_path, "python\r\n")
    proc = _engine_block_then_record(tmp_path, None, {})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "records an earlier" in proc.stdout and "Python client chosen" not in proc.stdout
    assert record.read_bytes() == b"python\r\n"


def _record(tmp_path: Path, text: str) -> Path:
    record = tmp_path / "home" / ".pseudolife-mcp" / "client-engine"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(text, encoding="utf-8", newline="\n")
    return record


def test_sh_a_recorded_python_choice_is_honoured_without_the_variable(tmp_path):
    _record(tmp_path, "python\n")
    proc = _sh(tmp_path, None, "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None
    assert "--client-only" in proc.stdout
    [line] = [text for text in proc.stdout.splitlines() if "records an earlier" in text]
    assert line.startswith("Python client install: ") and "PSEUDOLIFE_SHIM_ENGINE=native" in line


@pytest.mark.parametrize("text", ["python\r\n", "  python \n", "\npython\n", "python"])
def test_sh_a_record_reads_as_python_and_strip_read_it(tmp_path, text):
    # The same reading as the update bridge (str.strip()) and install.ps1.
    assert text.strip() == "python"
    _record(tmp_path, text)
    proc = _sh(tmp_path, None, "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None


@pytest.mark.parametrize("text", ["native\n", "", "Python\n", "python python\n", "py thon\n", "python\npython\n"])
def test_sh_a_record_that_does_not_say_exactly_python_is_no_choice(tmp_path, text):
    assert text.strip() != "python"
    _record(tmp_path, text)
    proc = _sh(tmp_path, None, "--yes")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["--yes"]


def test_sh_explicit_native_overrides_a_recorded_python_choice(tmp_path):
    _record(tmp_path, "python\n")
    proc = _sh(tmp_path, "native", "--yes")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["--yes"]


def _record_function(shell: str) -> str:
    text = (ROOT / "ops" / ("install.sh" if shell == "bash" else "install.ps1")).read_text(encoding="utf-8")
    if shell == "bash":
        # record_client_engine reads the record through
        # recorded_client_engine, defined just before it.
        start = "recorded_client_engine() {"
        assert text.count(start) == 1
        reader, rest = text.split(start, 1)[1].split("\n}\n", 1)
        writer = rest.split("\n}\n", 1)[0]
        assert writer.startswith("record_client_engine() {"), writer[:60]
        return start + reader + "\n}\n" + writer + "\n}\n"
    start = "function Save-ClientEngineChoice {"
    assert text.count(start) == 1
    return start + text.split(start, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_an_explicit_python_install_records_its_choice_at_its_end(tmp_path, shell):
    record = tmp_path / "home" / ".pseudolife-mcp" / "client-engine"
    if shell == "bash":
        if BASH is None:
            pytest.skip("needs bash")
        body = (f"CLIENT_ENGINE_RECORD='{_bash_path(record)}'\n" + _record_function("bash")
                + "RECORD_PYTHON_CHOICE=\nrecord_client_engine; echo ONE\n"
                + "RECORD_PYTHON_CHOICE=1\nrecord_client_engine; record_client_engine; echo TWO\n")
        script = tmp_path / "record.sh"
        script.write_text(body, encoding="utf-8", newline="\n")
        command = [BASH, _bash_path(script)]
    else:
        if PWSH is None:
            pytest.skip("needs pwsh")
        body = (f"$clientEngineRecord = '{str(record)}'\n" + _record_function("pwsh")
                + "$script:recordPythonChoice = $false\nSave-ClientEngineChoice; 'ONE'\n"
                + "$script:recordPythonChoice = $true\nSave-ClientEngineChoice; Save-ClientEngineChoice; 'TWO'\n")
        script = tmp_path / "record.ps1"
        script.write_text(body, encoding="utf-8")
        command = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script)]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False,
                          env=_env(tmp_path, None), stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout.splitlines()
    # Not chosen: nothing written. Chosen: written once, said once.
    assert out.index("ONE") == 0
    assert record.read_text(encoding="utf-8").strip() == "python"
    said = [line for line in out if line.startswith("Python client chosen: recorded in ")]
    assert len(said) == 1 and out.index(said[0]) < out.index("TWO")


@pytest.mark.parametrize("script, call, before, after", [
    ("install.sh", "\nrecord_client_engine\n", "\nprint_update_line\n", 'echo "Done.'),
    ("install.ps1", "\nSave-ClientEngineChoice\n", "\nWrite-UpdateLine ", 'Write-Host "Done.'),
])
def test_the_python_choice_is_recorded_once_after_the_install_steps(script, call, before, after):
    text = (ROOT / "ops" / script).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert text.count(call) == 1
    assert text.index(before) < text.index(call) < text.index(after)


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

def test_ps_python_runs_the_python_installer(tmp_path):
    proc = _ps(tmp_path, "python", "-ClientOnly", "-NoArt")
    # Master's own refusal, from the Python installer's parameter checks.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "-ClientOnly needs" in proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None


PS_LOG_DEFAULTED = "Add-Content -LiteralPath $env:NATIVE_ARGS -Value \"defaulted=$($global:PseudolifeClientDefaulted)\"\n"


@pytest.mark.parametrize("engine, defaulted", [(None, "True"), ("", "True"), ("native", "False")])
def test_ps_default_and_native_run_the_native_installer(tmp_path, engine, defaulted):
    proc = _ps(tmp_path, engine, "-ClientOnly", "-NoArt", ending=PS_LOG_DEFAULTED + "exit 7\n")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["started", "ClientOnly=True", "NoArt=True", f"defaulted={defaulted}"]


@pytest.mark.parametrize("text", ["python\r\n", "  python \n", "\npython\n"])
def test_ps_a_record_reads_as_python_and_strip_read_it(tmp_path, text):
    _record(tmp_path, text)
    proc = _ps(tmp_path, None, "-ClientOnly", "-NoArt")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None


def test_ps_a_hand_back_carries_on_with_the_python_installer_and_records_nothing(tmp_path):
    hand_back = HAND_BACK.replace("PSEUDOLIFE_SHIM_ENGINE=native", "$env:PSEUDOLIFE_SHIM_ENGINE = 'native'")
    ending = f"$global:PseudolifeClientFallback = '{hand_back.replace(chr(39), chr(39) * 2)}'\nexit 0\n"
    proc = _ps(tmp_path, None, "-ClientOnly", "-NoArt", ending=ending)
    # The Python installer's own refusal follows the hand-back line.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"==> {hand_back}" in proc.stdout.splitlines()
    assert "-ClientOnly needs" in proc.stdout + proc.stderr
    assert not (tmp_path / "home" / ".pseudolife-mcp" / "client-engine").exists()


def test_ps_the_shared_globals_do_not_outlive_the_run(tmp_path):
    # Run as from an open pwsh session: the wrapper's globals are gone after.
    if PWSH is None:
        pytest.skip("needs pwsh")
    repo = _repo(tmp_path, "install.ps1", _ps_stub("exit 7\n"), "install-native.ps1")
    command = (f"& '{str(repo / 'ops' / 'install.ps1')}' -NoArt; "
               "'left=' + (@(Get-Variable -Name PseudolifeClient* -Scope Global -ErrorAction SilentlyContinue).Count)")
    with user_path_unchanged(tmp_path):
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command], env=_env(tmp_path, None),
                              capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    assert "left=0" in proc.stdout.splitlines(), proc.stdout + proc.stderr


def test_ps_a_recorded_python_choice_is_honoured_without_the_variable(tmp_path):
    _record(tmp_path, "python\n")
    proc = _ps(tmp_path, None, "-ClientOnly", "-NoArt")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "-ClientOnly needs" in proc.stdout + proc.stderr
    assert _native_args(tmp_path) is None
    [line] = [text for text in proc.stdout.splitlines() if "records an earlier" in text]
    assert line.startswith("Python client install: ") and "'native'" in line


def test_ps_explicit_native_overrides_a_recorded_python_choice(tmp_path):
    _record(tmp_path, "python\n")
    proc = _ps(tmp_path, "native", "-NoArt")
    assert proc.returncode == 7, proc.stdout + proc.stderr


def test_ps_native_hands_every_parameter_to_the_native_installer(tmp_path):
    proc = _ps(tmp_path, "native", "-ClientOnly", "-DaemonUrl", "http://pl.example.invalid:8765", "-Yes", "-NoArt")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert _native_args(tmp_path) == ["started", "ClientOnly=True", "DaemonUrl=http://pl.example.invalid:8765",
                                      "NoArt=True", "Yes=True"]


@pytest.mark.parametrize("engine, tunnel", [
    ("native", True), (None, True), ("native", False), ("python", True),
], ids=["native-with-tunnel", "default-with-tunnel", "native-without-tunnel", "python-with-tunnel"])
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
    if engine in ("native", None) and tunnel:
        assert proc.returncode == 1, output
        assert _native_args(tmp_path) is None
        # PowerShell can create its own cache before the script is entered.
        home = tmp_path / "home"
        for state in (".pseudolife-mcp", "runtimes", "bin", ".claude", ".codex"):
            assert not (home / state).exists(), state
        assert not (repo / "ops" / ".env").exists()
        assert "The native client has no guided tunnel setup yet" in output
        assert "Nothing was changed" in output
        assert f"Rerun without {switch}" in output
        # The route that works: the Python client's guided setup.
        assert ("PSEUDOLIFE_SHIM_ENGINE=python bash ops/install.sh --tunnel" if shell == "bash"
                else "$env:PSEUDOLIFE_SHIM_ENGINE = 'python' and rerun .\\ops\\install.ps1 -Tunnel") in output
        assert "unset PSEUDOLIFE_SHIM_ENGINE" not in output and "Remove-Item Env:" not in output
        assert "pseudolife-mcp tunnel setup --daemon-url" in output
        assert "--token-file" in output and "--accept-access" in output
    else:
        expected = "native install reached" if engine in ("native", None) else "tunnel setup"
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


# -- the Python client's path moves nothing to native --------------------------

@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_the_python_path_installs_the_python_client_and_moves_nothing_to_native(script):
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
    ("install-native.sh", 'echo "Native client install (the default client; PSEUDOLIFE_SHIM_ENGINE=python installs the Python client instead)."'),
    ("install-native.ps1", 'Write-Host "Native client install (the default client; PSEUDOLIFE_SHIM_ENGINE=python installs the Python client instead)."'),
])
def test_the_native_installer_says_it_is_the_default_and_names_the_other_client(native, line):
    # Every native run says, once, near its start, that it is the default
    # and how to get the Python client instead; its usage names the variable.
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
