"""Wrapper runtime selection, without Docker, a bank or an installed package."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE_HELP = (
    "usage: pseudolife-mcp runtime install --archive <archive> "
    "--sha256sums <SUMS-file> --version <version> [--json]"
)


def fixture(tmp_path, code):
    repo = tmp_path / "checkout"
    (repo / "ops").mkdir(parents=True)
    for name in ("update.sh", "update.ps1", "update.py"):
        shutil.copyfile(ROOT / "ops" / name, repo / "ops" / name)
    driver = tmp_path / "native.py"
    output = tmp_path / "arguments.json"
    driver.write_text("import json,sys,pathlib\n"
                      f"pathlib.Path({str(output)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                      f"raise SystemExit({code})\n", encoding="utf-8")
    native = tmp_path / ("native.cmd" if os.name == "nt" else "native")
    if os.name == "nt":
        native.write_text(f'@echo off\r\n"{sys.executable}" "{driver}" %*\r\nexit /b %errorlevel%\r\n')
    else:
        import shlex
        native.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} \"$@\"\n")
        native.chmod(0o755)
    # Deliberately no inherited connection, credential or client configuration.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH") if key in os.environ}
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    env.update(HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
               TEMP=str(temporary), TMP=str(temporary), TMPDIR=temporary.as_posix(),
               PSEUDOLIFE_NATIVE_UPDATE="1", PSEUDOLIFE_NATIVE_CLI=str(native))
    return repo, env, output


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5, 7])
@pytest.mark.parametrize("shell", ["pwsh", "bash", "python"])
def test_native_opt_in_maps_flags_and_preserves_exact_exit(tmp_path, code, shell):
    if shell == "bash" and os.name == "nt":
        program = next((str(path) for path in (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe",
                                               Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/bin/bash.exe") if path.is_file()), None)
    else:
        program = sys.executable if shell == "python" else shutil.which(shell)
    if not program:
        pytest.skip(f"{shell} unavailable")
    repo, env, output = fixture(tmp_path, code)
    if shell == "bash" and os.name == "nt":
        import shlex
        native = tmp_path / "native-bash"
        native.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable.replace("\\", "/"))
                          + " " + shlex.quote(str(tmp_path / "native.py").replace("\\", "/")) + ' "$@"\n')
        native.chmod(0o755)
        env["PSEUDOLIFE_NATIVE_CLI"] = str(native).replace("\\", "/")
    if shell == "pwsh":
        argv = [program, "-NoProfile", "-File", str(repo / "ops/update.ps1"),
                "-Engine", "rust", "-Shadow", "-Tag", "pre-fixture", "-NoTestLogin"]
    elif shell == "bash":
        argv = [program, str(repo / "ops/update.sh").replace("\\", "/"), "--engine", "rust", "--shadow", "--tag", "pre-fixture", "--no-test-login"]
    else:
        argv = [program, str(repo / "ops/update.py"), "--engine", "rust", "--shadow", "--rollback-tag", "pre-fixture", "--no-test-login"]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=30)
    assert result.returncode == code, result.stderr.decode(errors="replace")
    args = json.loads(output.read_text())
    assert args[:3] == ["update", "--checkout", str(repo)] or (
        shell == "bash" and args[:2] == ["update", "--checkout"] and Path(args[2]).name == "checkout")
    assert args[args.index("--engine") + 1] == "rust"
    assert args[args.index("--rollback-tag") + 1] == "pre-fixture"
    assert "--shadow" in args and "--no-test-login" in args


def test_native_missing_binary_never_falls_back_to_python(tmp_path):
    program = shutil.which("pwsh")
    if not program:
        pytest.skip("pwsh unavailable")
    repo, env, output = fixture(tmp_path, 0)
    env["PSEUDOLIFE_NATIVE_CLI"] = str(tmp_path / "absent-native")
    result = subprocess.run([program, "-NoProfile", "-File", str(repo / "ops/update.ps1")],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 2
    assert not output.exists()
    assert b"native" in (result.stdout + result.stderr).lower()


def _shell_program(shell):
    if shell == "bash" and os.name == "nt":
        paths = (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/bin/bash.exe")
        return next((str(path) for path in paths if path.is_file()), None)
    return shutil.which(shell)


class Wrapper:
    """A closed PATH with Python oracle drivers standing in for both runtimes."""

    def __init__(self, tmp_path, shell):
        self.shell = shell
        self.program = _shell_program(shell)
        if not self.program:
            pytest.skip(f"{shell} unavailable")
        self.root = tmp_path
        self.repo, self.env, self.native_args = fixture(tmp_path, 7)
        self.env.pop("PSEUDOLIFE_NATIVE_UPDATE")
        self.env.update(LOCALAPPDATA=str(tmp_path / "local data"),
                        XDG_DATA_HOME=str(tmp_path / "local data"))
        self.bin = tmp_path / "path with spaces"
        self.bin.mkdir()
        self.env["PATH"] = str(self.bin)
        self.python_args = tmp_path / "python-arguments.json"
        self.python_probes = tmp_path / "python-probes.json"
        self.native_probes = tmp_path / "native-probes.json"
        driver = tmp_path / "native-capability.py"
        driver.write_text(
            "import json, os, pathlib, sys\n"
            "if sys.argv[1:] == ['runtime', '--help']:\n"
            f"    pathlib.Path({str(self.native_probes)!r}).write_text('probed')\n"
            f"    print(os.environ.get('FIXTURE_HELP', {NATIVE_HELP!r}))\n"
            "    raise SystemExit(int(os.environ.get('FIXTURE_HELP_EXIT', '0')))\n"
            f"pathlib.Path({str(self.native_args)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(7)\n", encoding="utf-8")
        self.native = self._driver(self.bin / "native", driver)
        self.env["PSEUDOLIFE_NATIVE_CLI"] = str(self.native)
        # update.py is an oracle fixture, never a package import or live update.
        (self.repo / "ops/update.py").write_text(
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(self.python_args)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(5)\n", encoding="utf-8")
        if shell == "bash":
            import shlex
            # Keep discovery deterministic without removing shell utilities.
            for name in ("dirname", "grep", "cp", "tr", "date", "basename"):
                executable = (Path(self.program).parents[1] / "usr/bin" / f"{name}.exe"
                              if os.name == "nt" else Path(shutil.which(name)))
                path = self.bin / name
                path.write_text("#!/bin/sh\nexec " + shlex.quote(str(executable).replace("\\", "/"))
                                + ' "$@"\n', encoding="utf-8", newline="\n")
                path.chmod(0o755)

    def _driver(self, path, driver):
        if self.shell == "pwsh" and os.name == "nt":
            path = path.with_suffix(".cmd")
            path.write_text(f'@echo off\r\n"{sys.executable}" "{driver}" %*\r\nexit /b %errorlevel%\r\n',
                            encoding="utf-8", newline="")
        else:
            import shlex
            path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable.replace("\\", "/"))
                            + " " + shlex.quote(str(driver).replace("\\", "/")) + ' "$@"\n',
                            encoding="utf-8", newline="\n")
            path.chmod(0o755)
        return path

    def python(self, usable=True):
        driver = self.root / "python-oracle.py"
        driver.write_text(
            "import pathlib, runpy, sys\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['-3']: args.pop(0)\n"
            "if args[:1] == ['-c']:\n"
            f"    pathlib.Path({str(self.python_probes)!r}).write_text('probed')\n"
            f"    raise SystemExit({0 if usable else 1})\n"
            "sys.argv = args\n"
            "runpy.run_path(args[0], run_name='__main__')\n", encoding="utf-8")
        for name in ("python", "python3", "py"):
            self._driver(self.bin / name, driver)

    def run(self, *args):
        argv = ([self.program, "-NoProfile", "-File", str(self.repo / "ops/update.ps1")]
                if self.shell == "pwsh" else
                [self.program, "--noprofile", "--norc", str(self.repo / "ops/update.sh").replace("\\", "/")])
        return subprocess.run([*argv, *args], env=self.env, capture_output=True, text=True, timeout=30)

    def crlf(self):
        path = self.repo / "ops/.env"
        path.write_bytes(b"FIXTURE_VALUE=unchanged\r\n")
        return path, path.stat().st_mtime_ns

    def unchanged(self, path, mtime):
        assert path.read_bytes() == b"FIXTURE_VALUE=unchanged\r\n"
        assert path.stat().st_mtime_ns == mtime
        assert not list(path.parent.glob(".env.crlf-*"))


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("selection", ["flag", "env"])
def test_explicit_native_skips_python_probe(tmp_path, shell, selection):
    wrapper = Wrapper(tmp_path, shell)
    wrapper.python()
    args = []
    if selection == "env":
        wrapper.env["PSEUDOLIFE_NATIVE_UPDATE"] = "1"
    else:
        args = ["--native" if shell == "bash" else "-Native"]
    result = wrapper.run(*args)
    assert result.returncode == 7, result.stderr
    assert json.loads(wrapper.native_args.read_text())[:2] == ["update", "--checkout"]
    assert not wrapper.python_args.exists() and not wrapper.python_probes.exists()
    assert not wrapper.native_probes.exists()  # explicit selection needs no capability probe


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("setting", [None, "0", "true"])
def test_installed_native_alone_does_not_opt_in_with_usable_python(tmp_path, shell, setting):
    wrapper = Wrapper(tmp_path, shell)
    wrapper.python()
    if setting is not None:
        wrapper.env["PSEUDOLIFE_NATIVE_UPDATE"] = setting
    result = wrapper.run()
    assert result.returncode == 5, result.stderr
    assert json.loads(wrapper.python_args.read_text())[:1] == ["--checkout"]
    assert not wrapper.native_args.exists() and not wrapper.native_probes.exists()
    assert result.stderr == ""


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("python", ["absent", "old"])
@pytest.mark.parametrize("resolution", ["cli", "shim", "stable"])
def test_no_usable_python_automatically_uses_verified_native(tmp_path, shell, python, resolution):
    if resolution == "stable" and shell == "pwsh" and os.name == "nt":
        pytest.skip("stable .exe execution requires a real installed-native integration fixture")
    wrapper = Wrapper(tmp_path, shell)
    if python == "old":
        wrapper.python(usable=False)
    if resolution != "cli":
        wrapper.env.pop("PSEUDOLIFE_NATIVE_CLI")
    if resolution == "shim":
        wrapper.env["PSEUDOLIFE_SHIM_LAUNCHER"] = str(wrapper.native)
    elif resolution == "stable":
        suffix = ".exe" if shell == "pwsh" else ""
        stable = Path(wrapper.env["LOCALAPPDATA"]) / "pseudolife-mcp/bin" / f"pseudolife-mcp{suffix}"
        stable.parent.mkdir(parents=True)
        shutil.copyfile(wrapper.native, stable)
        stable.chmod(0o755)
    result = wrapper.run()
    assert result.returncode == 7, result.stderr
    assert wrapper.native_probes.exists() and not wrapper.python_args.exists()
    assert len(result.stderr.splitlines()) == 1 and "verified native" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("invalid", ["missing", "directory", "generic-help", "near-help", "extra-help", "failed-help"])
def test_no_usable_runtime_refuses_without_changing_crlf_env(tmp_path, shell, invalid):
    wrapper = Wrapper(tmp_path, shell)
    path, mtime = wrapper.crlf()
    if invalid == "missing":
        wrapper.env["PSEUDOLIFE_NATIVE_CLI"] = str(tmp_path / "absent")
    elif invalid == "directory":
        wrapper.env["PSEUDOLIFE_NATIVE_CLI"] = str(tmp_path)
    elif invalid == "generic-help":
        wrapper.env["FIXTURE_HELP"] = "usage: pseudolife-mcp [shim|update]"
    elif invalid == "near-help":
        wrapper.env["FIXTURE_HELP"] = "prefix " + NATIVE_HELP
    elif invalid == "extra-help":
        wrapper.env["FIXTURE_HELP"] = NATIVE_HELP + "\nextra output"
    else:
        wrapper.env["FIXTURE_HELP_EXIT"] = "7"
    result = wrapper.run()
    assert result.returncode == 1, result.stderr
    assert "install the native runtime" in result.stderr and "Python >= 3.10" in result.stderr
    assert "nothing was changed" in result.stderr
    assert not wrapper.native_args.exists() and not wrapper.python_args.exists()
    wrapper.unchanged(path, mtime)


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("refusal", ["flag", "missing-value", "clients-only", "native-missing", "native-directory"])
def test_wrapper_refusals_precede_crlf_normalization(tmp_path, shell, refusal):
    wrapper = Wrapper(tmp_path, shell)
    wrapper.python()
    path, mtime = wrapper.crlf()
    if refusal.startswith("native-"):
        wrapper.env["PSEUDOLIFE_NATIVE_UPDATE"] = "1"
        wrapper.env["PSEUDOLIFE_NATIVE_CLI"] = str(tmp_path if refusal == "native-directory" else tmp_path / "absent")
        args = []
    elif refusal == "clients-only":
        args = ["--clients-only" if shell == "bash" else "-ClientsOnly"]
    elif refusal == "missing-value":
        args = ["--tag" if shell == "bash" else "-Tag"]
    else:
        args = ["--bogus" if shell == "bash" else "-Bogus"]
        if shell == "pwsh":
            wrapper.env["PSEUDOLIFE_NATIVE_UPDATE"] = "1"
    result = wrapper.run(*args)
    assert result.returncode != 0, result.stderr
    assert not wrapper.native_args.exists() and not wrapper.python_args.exists()
    assert not wrapper.python_probes.exists() and not wrapper.native_probes.exists()
    wrapper.unchanged(path, mtime)


def test_powershell_python_path_keeps_legacy_extra_argument_behavior(tmp_path):
    wrapper = Wrapper(tmp_path, "pwsh")
    wrapper.python()
    result = wrapper.run("-Bogus")
    assert result.returncode == 5, result.stderr
    assert wrapper.python_args.exists()
    assert not wrapper.native_args.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission contract")
@pytest.mark.parametrize("shell", ["bash", "pwsh"])
@pytest.mark.parametrize("explicit", [False, True])
def test_nonexecutable_native_refuses_without_env_mutation(tmp_path, shell, explicit):
    wrapper = Wrapper(tmp_path, shell)
    wrapper.native.chmod(0o600)
    path, mtime = wrapper.crlf()
    if explicit:
        wrapper.env["PSEUDOLIFE_NATIVE_UPDATE"] = "1"
    result = wrapper.run()
    assert result.returncode == (2 if explicit else 1), result.stderr
    assert not wrapper.native_args.exists()
    wrapper.unchanged(path, mtime)


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_invalid_explicit_cli_path_does_not_fall_through_to_shim(tmp_path, shell):
    wrapper = Wrapper(tmp_path, shell)
    wrapper.env["PSEUDOLIFE_SHIM_LAUNCHER"] = str(wrapper.native)
    wrapper.env["PSEUDOLIFE_NATIVE_CLI"] = str(tmp_path / "missing-cli")
    path, mtime = wrapper.crlf()
    result = wrapper.run()
    assert result.returncode == 1, result.stderr
    assert not wrapper.native_args.exists() and not wrapper.native_probes.exists()
    wrapper.unchanged(path, mtime)


@pytest.mark.skipif(os.name == "nt", reason="Git Bash reads CRLF files as LF in MSYS text mode")
@pytest.mark.parametrize("native", [False, True])
def test_bash_selected_update_still_normalizes_crlf_env(tmp_path, native):
    wrapper = Wrapper(tmp_path, "bash")
    if not native:
        wrapper.python()
    path, _ = wrapper.crlf()
    result = wrapper.run()
    assert result.returncode == (7 if native else 5), result.stderr
    assert path.read_bytes() == b"FIXTURE_VALUE=unchanged\n"
    [backup] = list(path.parent.glob(".env.crlf-*"))
    assert backup.read_bytes() == b"FIXTURE_VALUE=unchanged\r\n"


def test_capability_probe_matches_shipped_native_runtime_help():
    source = (ROOT / "rust/shim/src/runtime/cli.rs").read_text(encoding="utf-8")
    assert f'const HELP: &str = "{NATIVE_HELP}\\n";' in source


@pytest.mark.skipif(os.name != "nt", reason="Windows installed .exe fallback contract")
@pytest.mark.parametrize("python", ["absent", "old"])
def test_windows_python_free_wrapper_finds_verified_installed_native_runtime(tmp_path, python):
    import hashlib
    import zipfile

    binary = os.environ.get("PSEUDOLIFE_UPDATE_TEST_BINARY")
    if not binary:
        pytest.skip("set PSEUDOLIFE_UPDATE_TEST_BINARY to the built native CLI")
    wrapper = Wrapper(tmp_path, "pwsh")
    wrapper.env.pop("PSEUDOLIFE_NATIVE_CLI")
    wrapper.env["PSEUDOLIFE_MCP_DATA_DIR"] = str(tmp_path / "update-data")
    # Native validation stops at this deliberately incomplete checkout, before
    # any Docker operation, download, bank access or client movement.
    if python == "old":
        wrapper.python(usable=False)
    archive = tmp_path / "pseudolife-mcp-1.2.3-x86_64-pc-windows-msvc.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(binary, "pseudolife-mcp.exe")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="ascii")
    install = subprocess.run([binary, "runtime", "install", "--archive", str(archive),
                              "--sha256sums", str(sums), "--version", "1.2.3", "--json"],
                             env=wrapper.env, capture_output=True, text=True, timeout=60)
    assert install.returncode == 0, install.stdout + install.stderr
    assert json.loads(install.stdout)["state"] == "installed"
    result = wrapper.run()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "using the verified native update CLI" in result.stderr
    assert "--checkout must name a Pseudolife-MCP checkout" in result.stderr
    assert not wrapper.python_args.exists()
