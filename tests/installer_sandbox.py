"""Installer code runs in tests only inside a disposable home.

The installers install the native client into the home they are given and
re-point that home's client registrations. A test that started one with the
developer's own environment would do that to the developer's machine, so every
shared harness that runs installer code (a fragment or the whole script) calls
``require`` with the environment it passes and the test's temporary directory
before it starts the process, and ``run`` does both for a direct start.

``require`` refuses unless HOME, USERPROFILE, LOCALAPPDATA and APPDATA are all
set and inside that directory, every XDG_* variable and every other variable
that redirects writes (``REDIRECTS``) that is set is inside it, and the
directory itself is inside the system temporary directory. Git Bash paths
(``/c/...``, ``/tmp/...``) are read as the Windows paths they name. A missing
LOCALAPPDATA or APPDATA is refused too: on Windows a program that finds it
unset asks the system for the real folder instead.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

SANDBOXED = ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA")
# Each of these, when set, also sends writes somewhere: it must be inside too.
REDIRECTS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PSEUDOLIFE_MCP_DATA_DIR",
             "PSEUDOLIFE_SHIM_LAUNCHER", "PSEUDOLIFE_SHIM_RUNTIMES")


class RealHomeRefused(RuntimeError):
    """An installer was about to start outside a disposable home."""


def _resolved(value: str | os.PathLike[str]) -> Path:
    text = os.fspath(value)
    # Git Bash spells C:\x as /c/x; read it as the Windows path it names.
    if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[1].isalpha() and text[2] == "/":
        text = f"{text[1]}:{text[2:]}"
    elif os.name == "nt" and (text == "/tmp" or text.startswith("/tmp/")):
        # Git Bash's /tmp is the Windows temporary directory.
        text = tempfile.gettempdir() + text[4:]
    return Path(os.path.realpath(text))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def environment(root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: empty) with the four home variables inside ``root``."""
    home = Path(root) / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(base or {})
    env.update(HOME=str(home), USERPROFILE=str(home), LOCALAPPDATA=str(home / "AppData" / "Local"),
               APPDATA=str(home / "AppData" / "Roaming"))
    return env


def require(env: Mapping[str, str], root: Path) -> None:
    """Refuse (``RealHomeRefused``) unless ``env`` keeps every home inside ``root``."""
    root = _resolved(root)
    temp = _resolved(tempfile.gettempdir())
    if not _inside(root, temp):
        raise RealHomeRefused(f"the test directory {root} is not inside the temporary directory {temp}")
    problems = []
    for name in SANDBOXED:
        value = env.get(name)
        if not value:
            problems.append(f"{name} is unset")
        elif not _inside(_resolved(value), root):
            problems.append(f"{name}={value} is outside {root}")
    for name, value in env.items():
        if (name.startswith("XDG_") or name in REDIRECTS) and value and not _inside(_resolved(value), root):
            problems.append(f"{name}={value} is outside {root}")
    if problems:
        raise RealHomeRefused("refusing to start installer code outside a disposable home: " + "; ".join(problems))


def run(argv, *, env: Mapping[str, str], root: Path, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, env=env, ...)`` after ``require(env, root)``."""
    require(env, root)
    return subprocess.run(argv, env=dict(env), **kwargs)
