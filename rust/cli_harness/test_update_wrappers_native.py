"""Native wrapper opt-in, without Docker, a bank or an installed package."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


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
    env.update(HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
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
