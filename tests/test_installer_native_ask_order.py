"""A first native install refuses what it can detect before it asks anything.

Run at a terminal (a pty here), ``ops/install-native.sh`` / ``.ps1`` used to
ask the bank, agent and token questions and only then download the release
and find it too old for the installer. Now the first question waits for the
staged release: downloaded, verified and probed. A release that predates the
installer is refused before any question, so no answer is wasted.

POSIX only (a pty); a copy of the checkout, a throwaway home, and a release
whose client answers ``installer --capability`` as 0.24.0 does.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import select
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "x86_64-unknown-linux-gnu"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="a pty is POSIX-only")

# What 0.24.0's client answers: it runs, and has no `installer --capability`.
OLD_CLIENT = r'''#!/bin/sh
case "$1" in
    version) echo "pseudolife-mcp 0.24.0" ;;
    installer) echo "installer: expected an installer step: migrate-registrations, codex-credentials, codex-runtime-defaults, codex-hooks, claude-desktop" >&2; exit 2 ;;
esac
'''
QUESTIONS = ("Where does the memory bank live", "Choose 1-3", "Select one or more", "The daemon's URL")


def _version() -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert match
    return match.group(1)


def _copy(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "ops", repo / "ops", ignore=shutil.ignore_patterns(".env", "__pycache__", "*.log"))
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    if (ROOT / "examples").is_dir():
        shutil.copytree(ROOT / "examples", repo / "examples")
    return repo


def _release(directory: Path) -> None:
    directory.mkdir(parents=True)
    data = OLD_CLIENT.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("pseudolife-mcp")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    name = f"pseudolife-mcp-{_version()}-{TARGET}.tar.gz"
    (directory / name).write_bytes(buffer.getvalue())
    (directory / "SHA256SUMS").write_text(f"{hashlib.sha256(buffer.getvalue()).hexdigest()}  {name}\n", encoding="utf-8")


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "tmp").mkdir()
    return {
        "PATH": "/usr/bin:/bin" + (os.pathsep + str(Path(shutil.which("pwsh")).parent) if shutil.which("pwsh") else ""),
        "HOME": str(home), "USERPROFILE": str(home), "TERM": "dumb", "NO_COLOR": "1",
        "LOCALAPPDATA": str(home / "AppData" / "Local"), "APPDATA": str(home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local" / "share"),
        "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PSEUDOLIFE_SHIM_RUNTIMES": str(home / "runtimes"),
        "PSEUDOLIFE_SHIM_LAUNCHER": str(home / "bin" / "pseudolife-mcp"),
        "PSEUDOLIFE_NATIVE_ARCHIVE_URL": str(tmp_path / "release"),
        "PSEUDOLIFE_NATIVE_TARGET": TARGET,
        "PSEUDOLIFE_SHIM_ENGINE": "native",
    }


def _at_a_terminal(argv: list[str], env: dict[str, str], cwd: Path, timeout: float = 120) -> tuple[int | None, str]:
    """Run with a pty for stdin/stdout/stderr; answer any question with Enter."""
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, close_fds=True)
    os.close(slave)
    output = b""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if output.rstrip().endswith((b":", b"?", b"]")):
                    os.write(master, b"\n")
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        os.close(master)
    return proc.returncode, output.decode("utf-8", "replace").replace("\r\n", "\n")


@pytest.mark.parametrize("shell", ["sh", "ps1"])
def test_a_first_native_install_refuses_an_old_release_before_asking_anything(tmp_path, shell):
    if shell == "ps1" and not shutil.which("pwsh"):
        pytest.skip("needs pwsh")
    repo = _copy(tmp_path)
    _release(tmp_path / "release")
    env = _env(tmp_path)
    argv = (["bash", str(repo / "ops" / "install-native.sh")] if shell == "sh"
            else [shutil.which("pwsh"), "-NoProfile", "-File", str(repo / "ops" / "install-native.ps1"), "-NoArt"])
    code, output = _at_a_terminal(argv, env, repo)
    assert "predates this installer" in output, output[-3000:]
    before = output.split("predates this installer", 1)[0]
    asked = [question for question in QUESTIONS if question in before]
    assert not asked, f"asked {asked} before the refusal:\n{before[-2000:]}"
    assert code == 1
    assert not (repo / "ops" / ".env").exists()
