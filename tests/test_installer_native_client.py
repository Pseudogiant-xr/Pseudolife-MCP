"""The bash installer installs the native client from the release, never Python.

These run the installer's native-client block (``ops/install-native.sh``) and its
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
import re
import subprocess
import tarfile
import zipfile

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.installer_protocol import LEVEL, PS_LEVEL, RUST_LEVEL

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
    text = (ROOT / "ops/install-native.sh").read_text(encoding="utf-8")
    assert text.count(start) == 1, start
    return start + text.split(start, 1)[1].split(end, 1)[0] + end


# The stub client: what the installer asks of the real one, logged.
STUB = r'''#!/bin/sh
printf '%s\n' "$*" >>"$NATIVE_CALLS"
case "$1" in
    version) [ -z "$STUB_HANG_VERSION" ] || sleep 600; echo "pseudolife-mcp 9.8.7"; [ -z "$STUB_BROKEN" ] || { echo "GLIBC_2.35 not found" >&2; exit 1; } ;;
    runtime)
        if [ "$2" = install ] && [ "$3" = --check ]; then
            if [ -n "$STUB_REFUSE_TUNNEL" ]; then
                echo "runtime install refused: this machine stays on the Python client: a tunnel is registered to start by itself" >&2
                exit 3
            fi
            exit 0
        fi
        if [ -n "$STUB_REFUSE_TUNNEL_LATE" ]; then
            echo "runtime install refused: this machine stays on the Python client: a tunnel is registered to start by itself" >&2
            exit 3
        fi
        if [ -z "$STUB_NO_LAUNCHER" ]; then
            mkdir -p "$(dirname "$PSEUDOLIFE_SHIM_LAUNCHER")"
            cp "$0" "$PSEUDOLIFE_SHIM_LAUNCHER"; chmod 755 "$PSEUDOLIFE_SHIM_LAUNCHER"
        fi
        echo "$PSEUDOLIFE_SHIM_LAUNCHER" ;;
    update) [ "$2" = --native-capability ] && echo "pseudolife-mcp native-update-v1" ;;
    installer)
        if [ "$2" = --capability ]; then
            [ -z "$STUB_HANG_CAPABILITY" ] || sleep 600
            if [ -n "$STUB_V0240" ]; then
                # What v0.24.0's client answers: a usage error, exit 2.
                printf '%s\n' "installer: expected an installer step: migrate-registrations, codex-credentials, codex-runtime-defaults, codex-hooks, claude-desktop" >&2
                exit 2
            fi
            printf '%s\n' "${STUB_CAPABILITY-pseudolife-mcp installer-protocol $STUB_LEVEL}"
        fi
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
                data = native_client_stub.release_copy(REAL, directory.parent / "packed").read_bytes()
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


def _run(tmp_path: Path, body: str, *, env: dict[str, str] | None = None, release: bool = True, host_only: bool = False,
         version: str = VERSION, **release_kwargs) -> subprocess.CompletedProcess:
    """Use the same sandbox for either the installer or its host-only control."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    base = tmp_path / "release"
    if release:
        _release(base, **release_kwargs)
    forbidden = tmp_path / "bin"
    forbidden.mkdir(exist_ok=True)
    for name in ("python", "python3", "py", "pip", "pip3", "pipx"):
        stub = forbidden / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >>"$FORBIDDEN_CALLS"\nexit 99\n', encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    if not host_only:
        body = "\n".join([
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
        "STUB_LEVEL": str(LEVEL),
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
    script_file.write_text(body + "\n", encoding="utf-8", newline="\n")
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
    install = next(c for c in calls if c.startswith("runtime install --archive"))
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


@pytest.mark.parametrize(("code", "output", "diagnostic"), [
    (-1073741515, "", "Windows could not start the downloaded client because a system library is missing "
     "(exit code -1073741515, 0xC0000135). Install the Microsoft Visual C++ Redistributable for x64 before rerunning "
     "with this archive. To install without the Redistributable, check out a release tag that bundles the Windows C "
     "runtime and rerun that tag's installer with its matching archive."),
    (1, "version probe failed\nmore detail", "the downloaded client does not run on this host "
     "(exit code 1, version probe failed). Check that nothing (an antivirus or an execution policy) blocks it, and rerun."),
    (53, "", "the downloaded client does not run on this host (exit code 53). "
     "Check that nothing (an antivirus or an execution policy) blocks it, and rerun."),
    (29, "direct probe failed", "the downloaded client does not run on this host (exit code 29, direct probe failed). "
     "Check that nothing (an antivirus or an execution policy) blocks it, and rerun."),
    (124, "", "the downloaded client did not answer version within 30 s. "
     "Rerun; if it persists, report it at https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues."),
])
def test_windows_version_probe_reports_its_exit_and_keeps_only_the_download(tmp_path, code, output, diagnostic):
    control = tmp_path / "host-control"
    control.mkdir()
    probe_env = {"STUB_VERSION_CODE": str(code), "STUB_VERSION_OUTPUT": output}
    host = _run(control, ":", release=False, host_only=True, env=probe_env)
    assert host.returncode == 0, host.stdout + host.stderr
    assets = tmp_path / "release"
    assets.mkdir()
    name = f"pseudolife-mcp-{VERSION}-x86_64-pc-windows-msvc.zip"
    archive = assets / name
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("pseudolife-mcp.exe", "#!/bin/sh\nexit 53\n")
    (assets / "SHA256SUMS").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {name}\n", encoding="utf-8")
    result = _run(tmp_path, '''
native_target() { echo x86_64-pc-windows-msvc; }
cygpath() { printf '%s\\n' "$2"; }
unzip() {
    # Some Linux test hosts have no unzip. The real checksum still covers the ZIP.
    [ "$1 $2 $4 $5" = '-q -o pseudolife-mcp.exe -d' ] || return 1
    printf '%s\\n' '#!/bin/sh' 'printf "%s\\n" "direct probe failed"' 'exit 29' >"$6/$4"
}
powershell.exe() {
    [ -f "$PSEUDOLIFE_NATIVE_PROBE_EXE" ] || return 1
    case "$STUB_VERSION_CODE" in
        29) printf '%s\\n' 'Method invocation is not permitted in this language mode' >&2; return 1 ;;
        124) return 124 ;;
    esac
    printf '%s\\r\\n%s' "$STUB_VERSION_CODE" "$STUB_VERSION_OUTPUT"
}
native_stage
echo STAGED
''', release=False, env=probe_env)
    home = tmp_path / "home"
    downloads = home / ".pseudolife-mcp" / "downloads" / VERSION
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert result.stderr.strip() == (
        f"The native pseudolife-mcp client cannot be installed: {diagnostic} "
        f"Nothing else was changed: the download in {_p(downloads)} is kept for a rerun (delete it to free the space)."
    )
    host_home = control / "home"
    host_entries = {path.relative_to(host_home) for path in host_home.rglob("*")}
    installed_entries = {path.relative_to(home) for path in home.rglob("*")}
    assert installed_entries - host_entries == {
        path.relative_to(home)
        for path in (downloads / name, downloads / "SHA256SUMS", downloads, downloads.parent, downloads.parent.parent)
    }
    probe_temp = tmp_path / "tmp"
    control_temp = control / "tmp"
    assert {path.relative_to(probe_temp) for path in probe_temp.rglob("*")} <= {
        path.relative_to(control_temp) for path in control_temp.rglob("*")
    }


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
    # Nothing on the host; a download already verified is named as kept.
    assert re.search(r"Nothing (else )?was changed", result.stderr)
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


@pytest.mark.parametrize("answer", [
    {"STUB_V0240": "1"},                                              # v0.24.0: a usage error
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL - 1}"},  # an older protocol
    {"STUB_CAPABILITY": ""},                                          # says nothing
    {"STUB_CAPABILITY": "pseudolife-mcp installer-protocol one"},     # not the documented form
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL}\npseudolife-mcp installer-protocol 9"},  # two lines
    {"STUB_CAPABILITY": "pseudolife-mcp installer-protocol 99999999999999999999"},  # past any level
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol 0{LEVEL}"},  # a leading zero
    {"STUB_CAPABILITY": f"installer: pseudolife-mcp installer-protocol {LEVEL}"},
    {"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {LEVEL} "},  # trailing space
])
def test_a_release_older_than_this_installer_is_refused_before_anything_changes(tmp_path, answer):
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "REACHED"', env=answer)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "REACHED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    # The true cause and the one fix: which release, from which checkout.
    assert f"the native client {VERSION} predates this installer" in line
    assert "installer --capability" in line and f"installer-protocol {LEVEL} or newer" in line
    # The route that works today: the Python client, then update moves it.
    assert "rerunning the installer with PSEUDOLIFE_SHIM_ENGINE=python" in line
    assert "without PSEUDOLIFE_SHIM_ENGINE" not in line
    assert "PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only" in line
    assert "release_assets" not in line and "PSEUDOLIFE_NATIVE_ARCHIVE_URL" not in line
    # What a refusal after the download leaves: the verified download only.
    assert "Nothing else was changed" in line and "kept for a rerun" in line
    assert ".pseudolife-mcp" in line and "downloads" in line
    calls = _calls(tmp_path)
    assert "installer --capability" in calls
    assert not any(call.startswith(("runtime", "installer expose", "installer migrate")) for call in calls)
    assert not (tmp_path / "home/share/pseudolife-mcp/bin/pseudolife-mcp").exists()


@pytest.mark.parametrize("level", [str(LEVEL), str(LEVEL + 1), "17"])
def test_a_release_with_this_installers_protocol_or_newer_installs(tmp_path, level):
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=$SHIM_OK"',
                  env={"STUB_CAPABILITY": f"pseudolife-mcp installer-protocol {level}"})
    assert result.returncode == 0, result.stderr
    assert "OK=1" in result.stdout
    calls = _calls(tmp_path)
    assert calls.index("installer --capability") < calls.index("runtime install --check") < next(
        i for i, c in enumerate(calls) if c.startswith("runtime install --archive"))


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
    lines = (ROOT / "ops/install-native.sh").read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    for gone in ("pipx install", "pip install --user", "ops/shim_runtime.py", "shim_runtime_python"):
        assert gone not in text, gone


def test_a_reported_launcher_that_does_not_exist_is_not_success(tmp_path):
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=${SHIM_OK:-unset}"', env={"STUB_NO_LAUNCHER": "1"})
    assert result.returncode == 0, result.stderr
    assert "OK=unset" in result.stdout
    assert "the native client could not be installed" in result.stderr


def test_a_registered_tunnel_service_refuses_before_anything_changes(tmp_path):
    # The staged client's runtime install --check exits 3 when a tunnel
    # registered to start by itself runs this machine's Python client
    # (issue #1632): the preflight refuses, and nothing was changed.
    result = _run(tmp_path, "native_stage; echo STAGED", env={"STUB_REFUSE_TUNNEL": "1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert "runtime install refused: this machine stays on the Python client" in result.stderr
    assert (
        "a tunnel is registered to start by itself through this machine's Python client. "
        "The native client has no tunnel service command to manage that registration after the move."
    ) in result.stderr
    assert "pseudolife-mcp tunnel run" in result.stderr
    assert ("cannot run " "a tunnel yet") not in result.stderr
    assert "the tunnel will no longer start on its own" in result.stderr
    # The verified download is kept for the rerun; nothing else changed.
    assert "Nothing else was changed: the download in" in result.stderr
    assert not any(c.startswith("runtime install --archive") for c in _calls(tmp_path))
    assert not (tmp_path / "home" / "runtimes").exists()


FALLBACK_CAUSES = [
    ("platform", "native_target() { :; }; ", {}, "no native client is published for this platform"),
    ("does-not-run", "", {"STUB_BROKEN": "1"}, "the downloaded client does not run on this host"),
    ("old-release", "", {"STUB_V0240": "1"}, f"the native client {VERSION} predates this installer"),
    ("tunnel", "", {"STUB_REFUSE_TUNNEL": "1"}, "a tunnel is registered to start by itself"),
]


@pytest.mark.parametrize("case, prefix, env, cause", FALLBACK_CAUSES, ids=[c[0] for c in FALLBACK_CAUSES])
def test_a_defaulted_run_the_native_client_cannot_serve_hands_back_to_the_python_client(tmp_path, case, prefix, env,
                                                                                        cause):
    # ops/install.sh chose the native client by default and made the file:
    # the cause goes there, the run ends 0 before anything changed, and the
    # wrapper carries on with the Python client.
    handback = tmp_path / "fallback"
    handback.write_text("", encoding="utf-8")
    result = _run(tmp_path, prefix + 'native_stage; echo "STAGED"',
                  env={**env, "PSEUDOLIFE_INSTALLER_FALLBACK_FILE": _p(handback)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert "cannot be installed" not in result.stderr
    line = handback.read_text(encoding="utf-8")
    assert line.startswith(f"the native client cannot serve this machine ({cause}"), line
    assert line.rstrip("\n").endswith(
        "so this install uses the Python client; PSEUDOLIFE_SHIM_ENGINE=native makes this a refusal, and "
        "PSEUDOLIFE_SHIM_ENGINE=python chooses the Python client for later runs too"), line
    assert line.count("\n") == 1
    assert not any(c.startswith("runtime install --archive") for c in _calls(tmp_path))
    assert not (tmp_path / "home" / "share").exists()
    assert not list((tmp_path / "tmp").glob("pseudolife-native.*")), "the staged copy is removed"


@pytest.mark.parametrize("case, prefix, env, cause", FALLBACK_CAUSES, ids=[c[0] for c in FALLBACK_CAUSES])
def test_an_explicit_native_run_still_refuses_what_it_cannot_serve(tmp_path, case, prefix, env, cause):
    # No hand-back file: PSEUDOLIFE_SHIM_ENGINE=native asked for this client.
    result = _run(tmp_path, prefix + 'native_stage; echo "STAGED"', env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert cause in line
    assert "PSEUDOLIFE_SHIM_ENGINE=python" in line, line
    assert "without PSEUDOLIFE_SHIM_ENGINE" not in line


def test_no_hand_back_once_the_host_has_changed(tmp_path):
    handback = tmp_path / "fallback"
    handback.write_text("", encoding="utf-8")
    result = _run(tmp_path, 'NATIVE_HOST_CHANGING=1; native_fallback "a cause" "A fix."; echo "GONE ON"',
                  env={"PSEUDOLIFE_INSTALLER_FALLBACK_FILE": _p(handback)})
    assert result.returncode == 1
    assert "The install stopped part way: a cause. A fix." in result.stderr
    assert handback.read_text(encoding="utf-8") == ""


def test_a_hand_back_file_that_is_not_there_is_no_hand_back(tmp_path):
    # Only the file the wrapper made counts; a variable naming nothing refuses.
    result = _run(tmp_path, 'native_target() { :; }; native_stage',
                  env={"PSEUDOLIFE_INSTALLER_FALLBACK_FILE": _p(tmp_path / "never-made")})
    assert result.returncode == 1
    assert "cannot be installed: no native client is published for this platform" in result.stderr
    assert not (tmp_path / "never-made").exists()


def test_installing_the_native_client_ends_a_recorded_python_choice(tmp_path):
    record = tmp_path / "home" / ".pseudolife-mcp" / "client-engine"
    record.parent.mkdir(parents=True)
    record.write_text("python\n", encoding="utf-8")
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=$SHIM_OK"')
    assert result.returncode == 0, result.stderr
    assert "OK=1" in result.stdout
    assert not record.exists()
    assert f"The recorded Python client choice ({_p(record)}) is removed" in result.stdout
    # A rerun says nothing more about it.
    again = _run(tmp_path, "native_stage; ensure_shim")
    assert "recorded Python client choice" not in again.stdout + again.stderr


def test_a_refused_move_keeps_the_python_client_and_says_so(tmp_path):
    # Should runtime install itself refuse after the preflight passed, the
    # installer keeps the Python client and says so (issue #1632).
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=${SHIM_OK:-unset} CHANGING=${NATIVE_HOST_CHANGING:-unset}"',
                  env={"STUB_REFUSE_TUNNEL_LATE": "1"})
    assert result.returncode == 0, result.stderr
    assert "OK=unset CHANGING=unset" in result.stdout
    assert "runtime install refused: this machine stays on the Python client" in result.stderr
    assert "this machine keeps the Python client" in result.stderr
    assert "rerun the installer, which reuses" not in result.stderr
    assert not (tmp_path / "home" / "runtimes").exists()


def test_one_run_installs_the_client_once(tmp_path):
    result = _run(tmp_path, "native_stage; ensure_shim; ensure_shim; shim_registration_migrates codex || true")
    assert result.returncode == 0, result.stderr
    assert sum(c.startswith("runtime install --archive") for c in _calls(tmp_path)) == 1


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


def test_the_installers_and_the_binary_name_one_protocol_level():
    # The level the installers require is the level this checkout's binary
    # answers: raising one without the others refuses every release.
    assert LEVEL == PS_LEVEL == RUST_LEVEL


def test_a_download_folder_that_cannot_be_made_is_named_not_the_network(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".pseudolife-mcp").write_text("a file, not a folder\n", encoding="utf-8")
    result = _run(tmp_path, 'native_stage; echo STAGED')
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "/.pseudolife-mcp/downloads/" in line and "network" not in line
    assert line.endswith("Nothing was changed.")
    assert not any(call.startswith(("runtime", "version")) for call in _calls(tmp_path))


class _Status(BaseHTTPRequestHandler):
    """A release server that answers SHA256SUMS with one status."""
    status = 404

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve(status: int) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_Status,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.mark.parametrize("status", [403, 500])
def test_a_sums_file_that_cannot_be_fetched_names_the_status_not_a_missing_file(tmp_path, status):
    server, url = _serve(status)
    try:
        result = _run(tmp_path, 'native_stage; echo STAGED', release=False, env={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": url})
    finally:
        server.shutdown()
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert f"HTTP {status}" in line and "could not be fetched" in line and "retry" in line.lower()
    assert "has no SHA256SUMS" not in line


def test_a_sums_file_the_server_does_not_have_says_so(tmp_path):
    server, url = _serve(404)
    try:
        result = _run(tmp_path, 'native_stage; echo STAGED', release=False, env={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": url})
    finally:
        server.shutdown()
    assert result.returncode == 1
    assert "has no SHA256SUMS" in result.stderr


@pytest.mark.parametrize("hang", ["STUB_HANG_VERSION", "STUB_HANG_CAPABILITY"])
def test_a_staged_client_that_does_not_answer_is_refused_in_time(tmp_path, hang):
    result = _run(tmp_path, 'native_stage; echo STAGED', env={hang: "1", "PSEUDOLIFE_NATIVE_PROBE_SECONDS": "2"})
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    step = "version" if hang == "STUB_HANG_VERSION" else "installer --capability"
    assert f"did not answer {step} within 2 s" in line


# -- round 4 ------------------------------------------------------------------

def _rewrite_sums(tmp_path: Path, separator: str) -> None:
    """The release's SHA256SUMS line for the archive, written with another separator."""
    _release(tmp_path / "release")
    digest = hashlib.sha256((tmp_path / "release" / ASSET).read_bytes()).hexdigest()
    (tmp_path / "release" / "SHA256SUMS").write_text(f"{digest}{separator}{ASSET}\n", encoding="utf-8")


def test_a_binary_mode_checksum_line_is_accepted(tmp_path):
    # `sha256sum` under Git Bash writes `<hash> *<name>` (binary mode): the
    # other standard form of a checksum line.
    _rewrite_sums(tmp_path, " *")
    result = _run(tmp_path, 'native_stage; ensure_shim; echo "OK=$SHIM_OK"', release=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK=1" in result.stdout


@pytest.mark.parametrize("separator", [" ", "\t", "   ", " * "])
def test_a_checksum_line_in_no_standard_form_is_refused(tmp_path, separator):
    # The near miss: only two spaces or space-asterisk.
    _rewrite_sums(tmp_path, separator)
    result = _run(tmp_path, 'native_stage; echo STAGED', release=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert f"lists {ASSET} 0 times, not once" in result.stderr


@pytest.mark.parametrize("value, used", [
    ("08", "8"), ("09", "9"), ("010", "10"), ("7", "7"), ("00", "30"), ("0", "30"),
    ("", "30"), ("abc", "30"), ("1.5", "30"), ("-3", "30"), ("123456", "30"),
])
def test_the_probe_deadline_is_read_as_a_decimal_number_of_seconds(tmp_path, value, used):
    result = _run(tmp_path, 'echo "SECONDS=$NATIVE_PROBE_SECONDS"', env={"PSEUDOLIFE_NATIVE_PROBE_SECONDS": value})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SECONDS={used}" in result.stdout
    # A value that cannot be used is named, with the deadline used instead.
    named = [line for line in result.stderr.splitlines() if "PSEUDOLIFE_NATIVE_PROBE_SECONDS" in line]
    if value and used == "30" and value not in ("30",):
        assert named and f"'{value}'" in named[0] and "30 s" in named[0], result.stderr
    else:
        assert not named, result.stderr


def test_a_leading_zero_deadline_still_refuses_with_the_timeout_cause(tmp_path):
    result = _run(tmp_path, 'native_stage; echo STAGED', env={"STUB_HANG_VERSION": "1", "PSEUDOLIFE_NATIVE_PROBE_SECONDS": "02"})
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "did not answer version within 2 s" in line and "does not run on this host" not in line


def _fake_curl(tmp_path: Path, script: str) -> str:
    """A curl stub, and the script line that puts it first on PATH. It is set
    inside the script, since a Git Bash login puts its own directories
    (/ucrt64/bin, /usr/bin) before a PATH it is given."""
    stubs = tmp_path / "curl-stub"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text("#!/bin/sh\n" + script, encoding="utf-8", newline="\n")
    curl.chmod(0o755)
    return f'PATH="{_p(stubs)}:$PATH"; hash -r'


def test_a_download_that_cannot_be_written_names_the_write_not_an_http_status(tmp_path):
    # curl's own exit 23 is a write error (a full disk): -w still printed 200.
    first = _fake_curl(tmp_path, "printf 200\nexit 23\n")
    result = _run(tmp_path, f"{first}; native_stage; echo STAGED", release=False,
                  env={"PSEUDOLIFE_NATIVE_ARCHIVE_URL": "https://release.example.invalid/v9.8.7"})
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "HTTP 200" not in line and "proxy" not in line
    assert "could not be written" in line and "curl exit 23" in line and ".pseudolife-mcp" in line


@pytest.mark.skipif(os.name == "nt", reason="builds a PATH without curl from /usr/bin")
def test_a_missing_downloader_is_named_not_the_network(tmp_path):
    tools = tmp_path / "no-curl"
    tools.mkdir()
    for directory in ("/usr/bin", "/bin"):
        for name in os.listdir(directory):
            if name in ("curl", "wget") or (tools / name).exists():
                continue
            try:
                (tools / name).symlink_to(Path(directory) / name)
            except OSError:
                pass
    result = _run(tmp_path, 'native_stage; echo STAGED', release=False,
                  env={"PATH": f"{_p(tmp_path / 'bin')}:{tools}",
                       "PSEUDOLIFE_NATIVE_ARCHIVE_URL": "https://release.example.invalid/v9.8.7"})
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "curl" in line and "network" not in line and "proxy" not in line
    assert "install curl" in line.lower()


def test_a_mismatched_archive_refusal_names_only_what_is_kept(tmp_path):
    result = _run(tmp_path, "native_stage; echo STAGED", sums_override="f" * 64)
    assert result.returncode == 1
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "was removed" in line
    # The archive is gone; only SHA256SUMS stays, and the refusal says so.
    assert "the download in" not in line
    assert "SHA256SUMS in" in line and "kept for a rerun" in line


# The archive name is matched literally: a version may carry regex
# metacharacters (a PEP 440 local version, `9.8.7+meta`), and `+` read as a
# quantifier both refused the real line and let another name's line apply.
LOCAL = "9.8.7+meta"


def _local_release(tmp_path: Path, named: str) -> None:
    """A release for LOCAL whose SHA256SUMS lists the archive's digest under ``named``."""
    _release(tmp_path / "release")
    data = (tmp_path / "release" / ASSET).read_bytes()
    (tmp_path / "release" / ASSET).unlink()
    real = f"pseudolife-mcp-{LOCAL}-{TARGET}.tar.gz"
    (tmp_path / "release" / real).write_bytes(data)
    (tmp_path / "release" / "SHA256SUMS").write_text(f"{hashlib.sha256(data).hexdigest()}  {named}\n", encoding="utf-8")


def test_a_version_with_a_plus_sign_matches_its_own_line(tmp_path):
    _local_release(tmp_path, f"pseudolife-mcp-{LOCAL}-{TARGET}.tar.gz")
    result = _run(tmp_path, 'native_stage; echo STAGED', release=False, version=LOCAL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STAGED" in result.stdout


def test_another_names_line_never_supplies_the_digest(tmp_path):
    # `9[.]8[.]7+meta` as a regex matches `9.8.77meta`: that line must not count.
    _local_release(tmp_path, f"pseudolife-mcp-9.8.77meta-{TARGET}.tar.gz")
    result = _run(tmp_path, 'native_stage; echo STAGED', release=False, version=LOCAL)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STAGED" not in result.stdout
    assert f"lists pseudolife-mcp-{LOCAL}-{TARGET}.tar.gz 0 times, not once" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="an unreadable file needs POSIX permissions")
def test_a_local_release_file_that_cannot_be_copied_names_the_copy_not_curl(tmp_path):
    _release(tmp_path / "release")
    (tmp_path / "release" / "SHA256SUMS").chmod(0)
    try:
        result = _run(tmp_path, "native_stage; echo STAGED", release=False)
    finally:
        (tmp_path / "release" / "SHA256SUMS").chmod(0o644)
    assert result.returncode == 1, result.stdout + result.stderr
    [line] = [text for text in result.stderr.splitlines() if "cannot be installed" in text]
    assert "curl" not in line
    assert "could not be copied" in line and "release" in line


# -- the real client packed as a release ships it (native_client_stub.release_copy)


def _built_copy(tmp_path: Path) -> Path:
    """A stand-in for the built client (this interpreter's executable)."""
    import sys
    built = tmp_path / "built" / Path(sys.executable).name
    built.parent.mkdir()
    shutil.copyfile(sys.executable, built)
    return built


def test_the_packed_client_is_checked_against_the_release_binary_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(native_client_stub, "archive_binary_limit", lambda: 10)
    built = _built_copy(tmp_path)
    before = hashlib.sha256(built.read_bytes()).hexdigest()
    with pytest.raises(AssertionError, match=r"bytes \(built: \d+ bytes\), past runtime install's 10-byte limit"):
        native_client_stub.release_copy(built, tmp_path / "packed")
    assert hashlib.sha256(built.read_bytes()).hexdigest() == before, "the built binary is never touched"


def test_the_release_binary_limit_is_read_from_the_rust_source():
    limit = native_client_stub.archive_binary_limit()
    assert isinstance(limit, int) and limit > 0


@pytest.mark.parametrize("required, outcome", [("1", "failed"), ("", "skipped")])
def test_a_missing_strip_fails_a_required_real_client_and_skips_otherwise(tmp_path, monkeypatch, required, outcome):
    import sys
    if not sys.platform.startswith("linux"):
        pytest.skip("only a Linux debug build embeds its debug info, so only Linux strips the packed copy")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None)
    monkeypatch.setenv("PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED", required)
    # Both outcomes are caught, so a skip where a failure is due is a failure here.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception), match="strip") as raised:
        native_client_stub.release_copy(_built_copy(tmp_path), tmp_path / "packed")
    assert raised.type is (pytest.fail.Exception if outcome == "failed" else pytest.skip.Exception)


def test_on_linux_the_packed_copy_is_stripped_and_the_built_binary_is_not(tmp_path, monkeypatch):
    import subprocess as subprocesses
    import sys
    if not sys.platform.startswith("linux") or shutil.which("strip") is None:
        pytest.skip("only Linux strips the packed copy, with binutils' strip")
    calls = []
    real_run = subprocesses.run

    def run(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)
    monkeypatch.setattr(subprocesses, "run", run)
    built = _built_copy(tmp_path)
    before = hashlib.sha256(built.read_bytes()).hexdigest()
    packed = native_client_stub.release_copy(built, tmp_path / "packed")
    assert calls == [[shutil.which("strip"), "--strip-debug", str(packed)]]
    assert packed.parent == tmp_path / "packed" and packed != built
    assert hashlib.sha256(built.read_bytes()).hexdigest() == before


@pytest.mark.skipif(REAL is None, reason="set PSEUDOLIFE_NATIVE_TEST_BINARY to a built native client")
def test_the_packed_real_client_fits_the_release_limit(tmp_path):
    import sys
    packed = native_client_stub.release_copy(REAL, tmp_path / "packed")
    assert packed.stat().st_size <= native_client_stub.archive_binary_limit()
    if sys.platform.startswith("linux") and REAL.stat().st_size > native_client_stub.archive_binary_limit():
        # The hosted lane's debug build: only stripping makes it fit.
        assert packed.stat().st_size < REAL.stat().st_size
