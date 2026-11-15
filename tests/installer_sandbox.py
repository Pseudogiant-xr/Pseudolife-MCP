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
directory itself is inside the system temporary directory. ``run`` (a whole
installer start) also refuses unless PSEUDOLIFE_SHIM_RUNTIMES and
PSEUDOLIFE_SHIM_LAUNCHER are both set inside that directory (``layout`` sets
them): without that override an installer puts its launcher's directory first
on the Windows user PATH, which lives in HKCU and which no home variable
redirects. Git Bash paths
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
# A whole installer run needs both: the layout override is what keeps the
# installer off the real Windows user PATH.
LAYOUT = ("PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER")
# Each of these, when set, also sends writes somewhere: it must be inside too.
REDIRECTS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PSEUDOLIFE_MCP_DATA_DIR",
             "PSEUDOLIFE_SHIM_LAUNCHER", "PSEUDOLIFE_SHIM_RUNTIMES")


class RealHomeRefused(RuntimeError):
    """An installer was about to start outside a disposable home."""


_GIT_BASH_TMP: list[str] = []


def _git_bash_tmp() -> str | None:
    """The Windows folder Git Bash mounts at ``/tmp``, as Git Bash answers it.

    Git mounts the user's own temporary folder there whatever ``TEMP`` says,
    so with ``TEMP`` pointed elsewhere (an 8.3 short-name folder, as the
    hosted runner reaches its own) ``tempfile.gettempdir()`` is a different
    folder. ``None`` when Git Bash cannot be asked: a ``/tmp`` path is then
    not resolved, and so refused. Only an answer is kept, so one slow start
    under load is asked again rather than refusing for the rest of the run.
    """
    if _GIT_BASH_TMP:
        return _GIT_BASH_TMP[0]
    from tests.ops_harness import find_bash

    bash = find_bash()
    if not bash:
        return None
    try:
        answer = subprocess.run([bash, "-c", "cygpath -w /tmp"], capture_output=True, text=True,
                                timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    folder = answer.stdout.strip()
    if answer.returncode != 0 or not folder:
        return None
    _GIT_BASH_TMP.append(folder)
    return folder


def _resolved(value: str | os.PathLike[str]) -> Path:
    text = os.fspath(value)
    # Git Bash spells C:\x as /c/x; read it as the Windows path it names.
    if os.name == "nt" and len(text) > 2 and text[0] == "/" and text[1].isalpha() and text[2] == "/":
        text = f"{text[1]}:{text[2:]}"
    elif os.name == "nt" and (text == "/tmp" or text.startswith("/tmp/")):
        tmp = _git_bash_tmp()
        if tmp is None:
            return Path(text)
        text = tmp + text[4:]
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


def layout(env: Mapping[str, str], root: Path) -> dict[str, str]:
    """``env`` with the shim layout overridden inside ``root``'s home."""
    home = Path(root) / "home"
    launcher = "pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"
    out = dict(env)
    out["PSEUDOLIFE_SHIM_RUNTIMES"] = str(home / "shim" / "runtimes")
    out["PSEUDOLIFE_SHIM_LAUNCHER"] = str(home / "shim" / "bin" / launcher)
    return out


def run(argv, *, env: Mapping[str, str], root: Path, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, env=env, ...)`` after ``require(env, root)``, and
    only with both layout overrides set (``LAYOUT``; ``require`` checks they
    are inside ``root``)."""
    require(env, root)
    missing = [name for name in LAYOUT if not env.get(name)]
    if missing:
        raise RealHomeRefused(
            "refusing to start a whole installer run without " + " and ".join(missing)
            + " inside the disposable home: without the layout override the installer puts its"
            " launcher's directory first on the real Windows user PATH (installer_sandbox.layout sets both)")
    return subprocess.run(argv, env=dict(env), **kwargs)
