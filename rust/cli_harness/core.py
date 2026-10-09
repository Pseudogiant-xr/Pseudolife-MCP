"""Differential runner: one case, two arms, one comparison.

Each arm runs in the same disposable home path, reset between arms, with a
minimal allowlisted environment: no live token, daemon URL, digest directory
or lock directory from the caller can leak in. The Python arm runs the oracle
checkout's ``pseudolife_memory.cli``; the Rust arm runs the candidate binary.
An observation is the exit code, both raw streams, every file under the home
(bytes, or a typed marker for directories and links), the HTTP requests a
fixture daemon received and any database rows a case dumps. Comparison is
exact except for the named rules a case opts into (``normalize.py``).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

WINDOWS = os.name == "nt"
PLATFORM = "windows" if WINDOWS else "linux"

# Variables an arm may inherit: process plumbing only. Everything a CLI under
# test reads (HOME, tokens, daemon URL, digest and lock dirs) is set per case.
_INHERIT = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "TZ", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE")

# A closed loopback port: a case that forgets to name a daemon still never
# reaches the caller's real one (127.0.0.1:8765 by default).
DEAD_DAEMON_URL = "http://127.0.0.1:9"


@dataclasses.dataclass
class Arm:
    """What a case's hooks see: the shared home and which side is running."""

    name: str            # "python" or "rust"
    home: Path
    cwd: Path
    daemon: Any = None   # fixture daemon, when the case uses one
    started: float = 0.0  # wall clock at launch (time.time())
    state: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Case:
    id: str
    argv: list[str]
    env: dict[str, str | None] = dataclasses.field(default_factory=dict)
    stdin: bytes = b""
    # JSON stdin with {HOME}/{CWD} expanded in its strings, then serialized
    # the way a host hook sends it (json.dumps defaults).
    stdin_json: Any = None
    setup: Callable[[Arm], None] | None = None
    during: Callable[[Arm, subprocess.Popen], None] | None = None
    before_capture: Callable[[Arm, subprocess.Popen], bytes] | None = None
    after: Callable[[Arm, dict], None] | None = None
    timeout: float = 30.0
    rules: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("windows", "linux")
    stdout_closed: bool = False
    daemon: Callable[[], Any] | None = None
    skip_if: Callable[[], bool] | None = None  # e.g. root makes a chmod case vacuous
    # Needs real oracle daemons on disposable banks: live local acceptance
    # only, never golden replay (the candidate still needs the oracle daemon).
    bank: bool = False
    # The case exists to show both arms succeed (a trickle, an https daemon):
    # an arm with empty stdout makes the case differ instead of matching
    # vacuously when both arms fail the same quiet way.
    expect_output: bool = False
    note: str = ""

    def runs_here(self) -> bool:
        return PLATFORM in self.platforms and not (self.skip_if and self.skip_if())


@dataclasses.dataclass
class Target:
    """How to start one arm's CLI."""

    name: str
    command: list[str]
    env: dict[str, str] = dataclasses.field(default_factory=dict)


def python_target(python: str, source: Path) -> Target:
    # -P keeps the arm's cwd off sys.path; PYTHONPATH selects the oracle source.
    return Target("python", [python, "-P", "-m", "pseudolife_memory.cli"],
                  {"PYTHONPATH": str(source), "PYTHONIOENCODING": "utf-8",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})


def rust_target(binary: Path) -> Target:
    # Absolute: each arm runs with its disposable home's cwd.
    return Target("rust", [str(Path(binary).resolve())])


def _expand(value: str, arm: Arm) -> str:
    value = value.replace("{HOME}", str(arm.home)).replace("{CWD}", str(arm.cwd))
    if arm.daemon is not None:
        value = value.replace("{DAEMON_PORT}", arm.daemon.url.rsplit(":", 1)[1])
    return value


def _expand_json(value, arm: Arm):
    if isinstance(value, str):
        return _expand(value, arm)
    if isinstance(value, dict):
        return {k: _expand_json(v, arm) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_json(v, arm) for v in value]
    return value


def _environment(case: Case, arm: Arm, target: Target) -> dict[str, str]:
    env = {k: os.environ[k] for k in _INHERIT if k in os.environ}
    env.update(target.env)
    env.update({
        "HOME": str(arm.home),
        "USERPROFILE": str(arm.home),
        "APPDATA": str(arm.home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(arm.home / "AppData" / "Local"),
        "PSEUDOLIFE_MCP_DAEMON_URL": arm.daemon.url if arm.daemon else DEAD_DAEMON_URL,
        "PSEUDOLIFE_RELEASE_CHECK": "0",
        "COLUMNS": "80",
    })
    for key, value in case.env.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = _expand(value, arm)
    return env


def modes(root: Path) -> dict[str, str]:
    """POSIX permission bits of every entry under ``root`` (empty on Windows,
    where private files are judged by ACLs the CLIs check themselves)."""
    if WINDOWS or not root.exists():
        return {}
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = oct(os.lstat(path).st_mode & 0o7777)
    return dict(sorted(out.items()))


def snapshot(root: Path) -> dict[str, str]:
    """Every entry under ``root``: base64 file bytes, or a typed marker."""
    files: dict[str, str] = {}
    if not root.exists():
        return files
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in sorted(dirnames + filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                files[rel] = "link:" + os.readlink(path)
            elif path.is_dir():
                files[rel] = "dir"
            else:
                try:
                    files[rel] = "file:" + base64.b64encode(path.read_bytes()).decode()
                except OSError as error:
                    files[rel] = f"unreadable:{error.__class__.__name__}"
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
    return files


def _reset(home: Path) -> None:
    if home.exists():
        def unlock(func, path, _exc):
            os.chmod(path, 0o700)
            func(path)
        # chmod-000 stat cases leave a directory a plain rmtree cannot enter.
        for dirpath, dirnames, _ in os.walk(home):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(home, onerror=unlock)
    home.mkdir(parents=True)


def run_arm(case: Case, target: Target, home: Path) -> dict:
    _reset(home)
    arm = Arm(target.name, home, home / "cwd")
    arm.cwd.mkdir()
    daemon = None
    if case.daemon:
        daemon = (case.daemon(target.name) if getattr(case.daemon, "per_arm", False)
                  else case.daemon())
    arm.daemon = daemon
    try:
        if case.setup:
            case.setup(arm)
        env = _environment(case, arm, target)
        argv = [_expand(a, arm) for a in case.argv]
        stdin = case.stdin
        if case.stdin_json is not None:
            stdin = json.dumps(_expand_json(case.stdin_json, arm)).encode()
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if WINDOWS else 0
        stdout_target = subprocess.PIPE
        closed_reader = None
        if case.stdout_closed:
            closed_reader, writer = os.pipe()
            os.close(closed_reader)
            stdout_target = writer
        arm.started = time.time()
        proc = subprocess.Popen(target.command + argv, cwd=arm.cwd, env=env,
                                stdin=subprocess.PIPE, stdout=stdout_target,
                                stderr=subprocess.PIPE, creationflags=creation,
                                bufsize=0 if case.before_capture else -1)
        if case.stdout_closed:
            os.close(stdout_target)
        deadline = time.monotonic() + case.timeout
        stderr_prefix = b""
        if case.before_capture:
            ready = threading.Event()
            handshake = {}
            def before_capture():
                try:
                    handshake["prefix"] = case.before_capture(arm, proc)
                except Exception as error:
                    handshake["error"] = error
                finally:
                    ready.set()
            thread = threading.Thread(target=before_capture, daemon=True)
            thread.start()
            if not ready.wait(max(0, deadline - time.monotonic())):
                if proc.poll() is None:
                    proc.kill()
                thread.join(5)
                proc.communicate()
                raise RuntimeError(f"{case.id}/{target.name}: no capture handshake within {case.timeout}s")
            if "error" in handshake:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
                raise handshake["error"]
            stderr_prefix = handshake["prefix"]
        worker = None
        if case.during:
            worker = threading.Thread(target=case.during, args=(arm, proc), daemon=True)
            worker.start()
        try:
            out, err = proc.communicate(stdin, timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise RuntimeError(f"{case.id}/{target.name}: no exit within {case.timeout}s; "
                               f"stderr={err[-400:]!r}") from None
        ended = time.time()
        err = stderr_prefix + err
        if worker:
            worker.join(10)
        observation = {
            "home": str(home),
            # Local clock offset, so a golden's clocks validate where recorded.
            "utc_offset": time.localtime(arm.started).tm_gmtoff,
            "exit": proc.returncode,
            "stdout": base64.b64encode(out or b"").decode(),
            "stderr": base64.b64encode(err).decode(),
            "window": [arm.started, ended],
        }
        if daemon is not None:
            observation["daemon_url"] = daemon.url
            observation["requests"] = daemon.requests()
        if case.expect_output and not out:
            observation["vacuous"] = "no stdout where the case requires output"
        if case.after:
            case.after(arm, observation)
        if "listener" in arm.state:
            observation["listener"] = arm.state["listener"]
        observation["files"] = snapshot(home)
        observation["modes"] = modes(home)
        return observation
    finally:
        if daemon is not None:
            daemon.close()


def decode(observation: dict, field: str) -> bytes:
    return base64.b64decode(observation[field])


def _home_root() -> Path:
    # Default TEMP on purpose: owner-only credential checks need its ACLs.
    return Path(tempfile.gettempdir()) / f"pl-cli-diff-{os.getpid()}"


def run_case(case: Case, oracle: Target, candidate: Target) -> tuple[dict, dict]:
    root = _home_root()
    home = root / "h"
    try:
        python = run_arm(case, oracle, home)
        rust = run_arm(case, candidate, home)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return python, rust


def describe(observation: dict) -> dict:
    """A readable view of an observation for diff reports."""
    view = dict(observation)
    for field in ("stdout", "stderr"):
        view[field] = decode(observation, field).decode("utf-8", "backslashreplace")
    view["files"] = {k: (base64.b64decode(v[5:]).decode("utf-8", "backslashreplace")
                         if v.startswith("file:") else v)
                     for k, v in observation.get("files", {}).items()}
    return view


def python_executable() -> str:
    return sys.executable
