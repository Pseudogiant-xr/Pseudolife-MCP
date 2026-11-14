"""The bash installer installs the native client from the release, never Python.

These run the installer's native-client block (``ops/install.sh``) and its
shim helpers in a throwaway home, against a fake release directory
(``PSEUDOLIFE_NATIVE_ARCHIVE_URL``) whose archive holds a stub
``pseudolife-mcp``. ``python``, ``python3``, ``py``, ``pip`` and ``pipx`` on
PATH are stubs that record any call, and every test asserts none was made.
With ``PSEUDOLIFE_NATIVE_TEST_BINARY`` set (a built native client from the
same commit), ``test_the_real_client_installs_and_answers_the_installer_steps``
runs the real one instead of the stub.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from tests import installer_sandbox
from tests import native_client_stub


ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.8.7"
TARGET = "x86_64-unknown-linux-gnu"
ASSET = f"pseudolife-mcp-{VERSION}-{TARGET}.tar.gz"


def _bash() -> str | None:
    for candidate in (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None, shutil.which("bash")):
        if candidate and Path(candidate).is_file():
            try:
                if subprocess.run([candidate, "-c", "exit 0"], capture_output=True, timeout=10).returncode == 0:
                    return candidate
            except (OSError, subprocess.TimeoutExpired):
                continue
    return None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="needs a working bash")


def _p(path: Path) -> str:
    """A path as bash sees it (MSYS form under Git Bash, where tar reads
    ``C:`` as a remote host)."""
    text = path.as_posix()
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _between(start: str, end: str) -> str:
    text = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
    assert text.count(start) == 1, start
    return start + text.split(start, 1)[1].split(end, 1)[0] + end


# The stub client: what the installer asks of the real one, logged.
STUB = r'''#!/bin/sh
printf '%s\n' "$*" >>"$NATIVE_CALLS"
case "$1" in
    version) echo "pseudolife-mcp 9.8.7"; [ -z "$STUB_BROKEN" ] || { echo "GLIBC_2.35 not found" >&2; exit 1; } ;;
    runtime)
        if [ -z "$STUB_NO_LAUNCHER" ]; then
            mkdir -p "$(dirname "$PSEUDOLIFE_SHIM_LAUNCHER")"
            cp "$0" "$PSEUDOLIFE_SHIM_LAUNCHER"; chmod 755 "$PSEUDOLIFE_SHIM_LAUNCHER"
        fi
        echo "$PSEUDOLIFE_SHIM_LAUNCHER" ;;
    update) [ "$2" = --native-capability ] && echo "pseudolife-mcp native-update-v1" ;;
    installer)
        if [ "$2" = expose ] && [ -n "${STUB_EXPOSE:-}" ]; then printf '%s\n' "$STUB_EXPOSE"; fi
        # A flat JSON object's string field, as the real step reads it.
        if [ "$2" = json-field ]; then sed -n "s/.*\"$3\": \"\([^\"]*\)\".*/\1/p"; fi
        if [ "$2" = migrate-registrations ]; then
            case "$STUB_MOVES" in
                moved) echo "moved $4 to the native client at $PSEUDOLIFE_SHIM_LAUNCHER (each changed file has a private backup)" ;;
                current) echo "$4 already runs the native client; nothing changed" ;;
                *) echo "no registration moved; nothing changed ($4 still runs /opt/x: left as it is)" ;;
            esac
        fi ;;
esac
'''


REAL = native_client_stub.real_binary()


def _release(directory: Path, *, sums_override: str | None = None, extra_line: bool = False,
             omit: bool = False, real: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    asset = ASSET
    if real:
        # The built client, packed as the release packs it for this host.
        target = native_client_stub.host_target()
        if target and target.endswith("windows-msvc"):
            asset = f"pseudolife-mcp-{VERSION}-{target}.zip"
            import zipfile
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("pseudolife-mcp.exe", REAL.read_bytes())
        else:
            asset = f"pseudolife-mcp-{VERSION}-{target}.tar.gz"
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                info = tarfile.TarInfo("pseudolife-mcp")
                data = REAL.read_bytes()
                info.size = len(data)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(data))
    else:
        data = STUB.encode()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("pseudolife-mcp")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    (directory / asset).write_bytes(buffer.getvalue())
    digest = hashlib.sha256(buffer.getvalue()).hexdigest()
    lines = [] if omit else [f"{sums_override or digest}  {asset}"]
    if extra_line:
        lines.append(f"{digest}  {asset}")
    lines.append(f"{'0' * 64}  pseudolife-compose-{VERSION}.tar.gz")
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(tmp_path: Path, body: str, *, env: dict[str, str] | None = None, release: bool = True,
         **release_kwargs) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{VERSION}"\n', encoding="utf-8")
    base = tmp_path / "release"
    if release:
        _release(base, **release_kwargs)
    forbidden = tmp_path / "bin"
    forbidden.mkdir(exist_ok=True)
    for name in ("python", "python3", "py", "pip", "pip3", "pipx"):
        stub = forbidden / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"$FORBIDDEN_CALLS"\nexit 99\n', encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    script = "\n".join([
        "set -euo pipefail",
        'step() { echo "==> $*"; }',
        f"repo='{_p(repo)}'",
        _between("# >>> native client >>>", "# <<< native client <<<"),
        f"native_target() {{ echo {native_client_stub.host_target() if release_kwargs.get('real') else TARGET}; }}",
        _between('SHIM_TRIED=""\nSHIM_OK=""', "\ncli_env_flag() {").removesuffix("\ncli_env_flag() {"),
        body,
    ])
    launcher = home / "share" / "pseudolife-mcp" / "bin" / (
        "pseudolife-mcp.exe" if release_kwargs.get("real") and os.name == "nt" else "pseudolife-mcp")
    run_env = {
        "PATH": f"{_p(forbidden)}:/usr/bin:/bin",
        "HOME": _p(home),
        # Set inside the throwaway home: a Windows client finding these unset
        # would ask the system for the real folders.
        "USERPROFILE": str(home), "LOCALAPPDATA": str(home / "Local"), "APPDATA": str(home / "Roaming"),
        "TMPDIR": _p((tmp_path / "tmp")),
        "NATIVE_CALLS": _p((tmp_path / "native-calls")),
        "FORBIDDEN_CALLS": _p((tmp_path / "forbidden-calls")),
        "PSEUDOLIFE_NATIVE_ARCHIVE_URL": _p(base),
        "PSEUDOLIFE_SHIM_LAUNCHER": _p(launcher),
        # The real client takes both layout overrides or neither.
        "PSEUDOLIFE_SHIM_RUNTIMES": _p(home / "share" / "pseudolife-mcp" / "runtimes"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    (tmp_path / "tmp").mkdir(exist_ok=True)
    run_env.update(env or {})
    script_file = tmp_path / "run.sh"
    script_file.write_text(script + "\n", encoding="utf-8", newline="\n")
    installer_sandbox.require(run_env, tmp_path)
    result = subprocess.run([BASH, _p(script_file)], capture_output=True, text=True, env=run_env, timeout=120)
    forbidden_log = tmp_path / "forbidden-calls"
    assert not forbidden_log.exists(), f"the installer ran Python tooling: {forbidden_log.read_text()}"
    return result


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "native-calls"
    return log.read_text().splitlines() if log.exists() else []


def test_fresh_install_verifies_the_archive_and_installs_it_natively(tmp_path):
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=$SHIM_OK PATH=$SHIM_PATH"')
    assert result.returncode == 0, result.stderr
    launcher = tmp_path / "home/share/pseudolife-mcp/bin/pseudolife-mcp"
    assert f"OK=1 PATH={_p(launcher)}" in result.stdout
    assert f"Native client {VERSION} installed at" in result.stdout
    calls = _calls(tmp_path)
    assert calls[0] == "version"
    install = next(c for c in calls if c.startswith("runtime install"))
    downloads = tmp_path / "home/.pseudolife-mcp/downloads" / VERSION
    assert install == (f"runtime install --archive {_p(downloads / ASSET)} "
                       f"--sha256sums {_p(downloads / 'SHA256SUMS')} --version {VERSION}")
    assert "installer expose --json" in calls


def test_rerun_on_a_native_host_says_nothing_changed(tmp_path):
    first = _run(tmp_path, "native_stage; ensure_shim")
    assert first.returncode == 0, first.stderr
    second = _run(tmp_path, "native_stage; ensure_shim")
    assert second.returncode == 0, second.stderr
    assert f"Native client {VERSION} already installed at" in second.stdout
    assert "nothing changed" in second.stdout


@pytest.mark.parametrize("case, expected", [
    ("mismatch", "does not match the release's SHA256SUMS"),
    ("duplicate", "lists " + ASSET + " 2 times, not once"),
    ("missing", "lists " + ASSET + " 0 times, not once"),
    ("no-release", "has no SHA256SUMS"),
])
def test_a_bad_or_missing_release_refuses_before_anything_changes(tmp_path, case, expected):
    kwargs = {"mismatch": {"sums_override": "f" * 64}, "duplicate": {"extra_line": True},
              "missing": {"omit": True}}.get(case, {})
    result = _run(tmp_path, "native_stage; echo STAGED", release=case != "no-release", **kwargs)
    assert result.returncode == 1
    assert expected in result.stderr
    assert "Nothing was changed." in result.stderr
    assert "STAGED" not in result.stdout
    assert not (tmp_path / "home/share").exists()
    assert not any(c.startswith("runtime") for c in _calls(tmp_path))


def test_an_unsupported_platform_refuses_naming_the_supported_ones(tmp_path):
    result = _run(tmp_path, "native_target() { :; }; native_stage; echo STAGED")
    assert result.returncode == 1
    assert "no native client is published for this platform" in result.stderr
    assert "--client-only" in result.stderr
    assert "STAGED" not in result.stdout
    assert _calls(tmp_path) == []


def test_a_binary_that_cannot_run_here_refuses_before_install(tmp_path):
    result = _run(tmp_path, "native_stage; echo STAGED", env={"STUB_BROKEN": "1"})
    assert result.returncode == 1
    assert "does not run on this host" in result.stderr
    assert "glibc 2.35" in result.stderr
    assert _calls(tmp_path) == ["version"]


@pytest.mark.parametrize("moves, status", [("moved", 0), ("current", 0), ("left", 1)])
def test_registration_move_succeeds_once_the_client_runs_the_launcher(tmp_path, moves, status):
    result = _run(tmp_path, 'native_stage; if shim_registration_migrates codex; then echo MOVED; else echo KEPT; fi',
                  env={"STUB_MOVES": moves})
    assert result.returncode == 0, result.stderr
    assert ("MOVED" if status == 0 else "KEPT") in result.stdout
    assert "installer migrate-registrations --client codex" in _calls(tmp_path)


def test_resolve_installed_shim_accepts_only_a_native_launcher(tmp_path):
    launcher = tmp_path / "home/share/pseudolife-mcp/bin/pseudolife-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho python launcher\n", encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    body = 'if resolve_installed_shim; then echo "FOUND $SHIM_PATH"; else echo NONE; fi'
    assert "NONE" in _run(tmp_path, body).stdout
    _run(tmp_path, "native_stage; ensure_shim")
    assert f"FOUND {_p(launcher)}" in _run(tmp_path, body).stdout


def test_the_installer_no_longer_installs_a_python_client():
    lines = (ROOT / "ops/install.sh").read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    for gone in ("pipx install", "pip install --user", "ops/shim_runtime.py", "shim_runtime_python"):
        assert gone not in text, gone


def test_a_reported_launcher_that_does_not_exist_is_not_success(tmp_path):
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=${SHIM_OK:-unset}"', env={"STUB_NO_LAUNCHER": "1"})
    assert result.returncode == 0, result.stderr
    assert "OK=unset" in result.stdout
    assert "the native client could not be installed" in result.stderr


def test_one_run_installs_the_client_once(tmp_path):
    result = _run(tmp_path, "native_stage; ensure_shim; ensure_shim; shim_registration_migrates codex || true")
    assert result.returncode == 0, result.stderr
    assert sum(c.startswith("runtime install") for c in _calls(tmp_path)) == 1


def test_an_archive_url_with_credentials_refuses_without_printing_them(tmp_path):
    result = _run(tmp_path, "native_stage; echo STAGED",
                  env={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": "https://user:sekrit@mirror.example.com/v9"})
    assert result.returncode == 1
    assert "carries a user name or password" in result.stderr
    assert "sekrit" not in result.stdout + result.stderr
    assert "STAGED" not in result.stdout


def test_no_staged_copy_is_left_behind(tmp_path):
    result = _run(tmp_path, "native_stage; ensure_shim")
    assert result.returncode == 0, result.stderr
    assert not list((tmp_path / "tmp").glob("pseudolife-native.*"))
    assert (tmp_path / "home/.pseudolife-mcp/downloads" / VERSION / ASSET).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_an_existing_state_directory_keeps_its_mode(tmp_path):
    state = tmp_path / "home" / ".pseudolife-mcp"
    state.mkdir(parents=True)
    state.chmod(0o755)
    result = _run(tmp_path, "native_stage; ensure_shim")
    assert result.returncode == 0, result.stderr
    assert (state.stat().st_mode & 0o777) == 0o755
    assert ((state / "downloads").stat().st_mode & 0o777) == 0o700


@pytest.mark.skipif(REAL is None, reason="set PSEUDOLIFE_NATIVE_TEST_BINARY to a built native client")
def test_the_real_client_installs_and_answers_the_installer_steps(tmp_path):
    # The installer's own steps run the staged client: the client's loopback
    # rule (10 = this machine) and a JSON field read, then the install.
    body = (
        "native_stage\n"
        "rc=0; native_helper is-loopback 'http://[::ffff:127.0.0.1]:8765' || rc=$?; echo \"LOOPBACK=$rc\"\n"
        "rc=0; native_helper is-loopback http://127.0.0.01:8765 || rc=$?; echo \"LEADING_ZERO=$rc\"\n"
        "echo \"FIELD=$(printf '%s' '{\"action\": \"start\"}' | native_helper json-field action)\"\n"
        "ensure_shim; echo \"OK=$SHIM_OK\"\n"
    )
    result = _run(tmp_path, body, real=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "LOOPBACK=10" in result.stdout, result.stdout
    # As Python's ipaddress reads it: a leading zero is not this machine.
    assert "LEADING_ZERO=11" in result.stdout, result.stdout
    assert "FIELD=start" in result.stdout, result.stdout
    assert "OK=1" in result.stdout, result.stdout
    assert f"Native client {VERSION} installed at" in result.stdout


@pytest.mark.parametrize("report, shown", [
    ('{"state": "left", "detail": "user bin entry is already occupied; run the stable launcher by its full path"}',
     "pseudolife-mcp on PATH: user bin entry is already occupied; run the stable launcher by its full path"),
    ('{"state": "failed", "detail": "could not create the user bin link; run the stable launcher by its full path"}',
     "pseudolife-mcp on PATH: could not create the user bin link"),
    ('{"state": "linked", "detail": "linked user bin to the stable launcher", "hint": "/x is not on PATH: add it"}',
     "pseudolife-mcp on PATH: /x is not on PATH: add it"),
    ('{"state": "linked", "detail": "linked user bin to the stable launcher"}', None),
    ('{"state": "current", "detail": "user bin already reaches the stable launcher"}', None),
])
def test_the_launcher_exposure_is_said_unless_it_simply_worked(tmp_path, report, shown):
    # The installer steps of the stub client read JSON as the real one does.
    result = _run(tmp_path, "native_stage; ensure_shim", env={"STUB_EXPOSE": report})
    assert result.returncode == 0, result.stderr
    exposure = [line for line in result.stdout.splitlines() if "pseudolife-mcp on PATH" in line]
    if shown is None:
        assert exposure == [], result.stdout
    else:
        assert len(exposure) == 1 and shown in exposure[0], result.stdout
