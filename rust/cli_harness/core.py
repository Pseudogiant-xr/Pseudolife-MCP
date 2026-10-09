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
import hashlib
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
    stderr_closed: bool = False  # the arm's stderr is a pipe whose reader closed
    daemon: Callable[[], Any] | None = None
    skip_if: Callable[[], bool] | None = None  # e.g. root makes a chmod case vacuous
    # Needs real oracle daemons on disposable banks: live local acceptance
    # only, never golden replay (the candidate still needs the oracle daemon).
    bank: bool = False
    # False: the observation shows state the row seeds afresh in every harness
    # process (wall-clock stamps, random codes), so the case runs live only;
    # --record and --golden leave it out. The row's .md names the reason.
    golden: bool = True
    # The case exists to show both arms succeed (a trickle, an https daemon):
    # an arm with empty stdout makes the case differ instead of matching
    # vacuously when both arms fail the same quiet way.
    expect_output: bool = False
    # External programs the oracle may look up on this case's paths (docker,
    # pg_dump, tailscale, ...). Before each arm, check_programs proves none of
    # them resolves outside the disposable home. real_programs names the ones
    # the case deliberately runs from the host (shown in every run's listing).
    programs: tuple[str, ...] = ()
    real_programs: tuple[str, ...] = ()
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
    home = str(arm.home)
    # A host reporting the same home with a lowercase drive letter.
    value = value.replace("{HOME_LOWER_DRIVE}", home[:1].lower() + home[1:])
    value = value.replace("{HOME}", home).replace("{CWD}", str(arm.cwd))
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
    if WINDOWS:
        # Default install locations (Program Files) resolve inside the home.
        # A 64-bit child derives ProgramFiles from ProgramW6432 when it is
        # set; with neither set, lookups fall back to the real C:\Program Files.
        program_files = str(arm.home / "Program Files")
        env.update({"ProgramW6432": program_files, "ProgramFiles": program_files})
    for key, value in case.env.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = _expand(value, arm)
    return env


class ProgramLeak(RuntimeError):
    """A case's environment lets a declared program resolve outside its home."""


# What the preflight child reports: shutil.which (current directory first on
# Windows, PATHEXT) for each name, and the lookup inputs as the child sees
# them (Windows derives some at process start: ProgramFiles from ProgramW6432).
_PROBE = """\
import json, os, shutil, sys
names = json.loads(sys.argv[1])
keys = ("PATH", "PATHEXT", "ProgramW6432", "ProgramFiles", "ProgramFiles(x86)",
        "LOCALAPPDATA", "APPDATA", "SystemRoot", "HOME", "USERPROFILE")
print(json.dumps({"which": {n: shutil.which(n) for n in names},
                  "env": {k: os.environ.get(k) for k in keys}}))
"""
_PROBED: dict[str, dict] = {}


def _inside(path: str, home: Path) -> bool:
    try:
        Path(path).resolve().relative_to(home.resolve())
        return True
    except ValueError:
        return False


def check_programs(case: Case, env: dict[str, str], cwd: Path, home: Path,
                   python: str) -> dict:
    """Refuse ``case`` unless every program it declares resolves inside
    ``home`` or nowhere (``real_programs`` excepted) and, on Windows, the
    Program Files variables are set inside ``home``: asked of a child of
    ``python`` given ``env`` and ``cwd`` exactly. Runs only the interpreter;
    one child per distinct environment, cwd and name list."""
    names = sorted(set(case.programs) | set(case.real_programs))
    key = hashlib.sha256(json.dumps([python, sorted(env.items()), str(cwd), names])
                         .encode()).hexdigest()
    if key not in _PROBED:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if WINDOWS else 0
        done = subprocess.run([python, "-I", "-c", _PROBE, json.dumps(names)], env=env,
                              cwd=cwd, capture_output=True, text=True, timeout=60,
                              creationflags=creation)
        if done.returncode != 0:
            raise ProgramLeak(f"{case.id}: the program preflight failed: {done.stderr[-400:]}")
        _PROBED[key] = json.loads(done.stdout)
    seen = _PROBED[key]
    problems = [f"{name} resolves to {found}" for name, found in seen["which"].items()
                if found and name not in case.real_programs and not _inside(found, home)]
    if WINDOWS:
        problems += [f"{var} is {seen['env'][var]!r}" for var in ("ProgramW6432", "ProgramFiles")
                     if not seen["env"][var] or not _inside(seen["env"][var], home)]
    if problems:
        raise ProgramLeak(f"{case.id}: an external program could resolve outside the home "
                          f"{home}: {'; '.join(problems)}; the child sees {seen['env']}")
    return seen


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


# Windows: a file a scanner or indexer opened a moment ago refuses deletion
# with a sharing violation (WinError 32) or access denied (5) until it lets
# go; observed on hosted runners. Every arm's process and helper thread has
# exited before a reset, so these are retried for a bounded time, not ignored.
_SHARING = (32, 5)
_RETRY_SECONDS = 10.0


def _transient(error: OSError) -> bool:
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in _SHARING


def _remove(home: Path) -> None:
    def retry(func, path, exc_info):
        error = exc_info[1]
        if isinstance(error, FileNotFoundError):
            return
        if func not in (os.unlink, os.remove, os.rmdir):
            # A failed directory scan or open cannot be resumed by calling it
            # again: unlock the directory and remove that subtree afresh.
            os.chmod(path, 0o700)
            shutil.rmtree(path, onerror=retry)
            return
        deadline = time.monotonic() + _RETRY_SECONDS
        while True:
            try:
                os.chmod(path, 0o700)
                func(path)
                return
            except FileNotFoundError:
                return
            except OSError as again:
                if not _transient(again) or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
    # chmod-000 stat cases leave a directory a plain rmtree cannot enter.
    for dirpath, dirnames, _ in os.walk(home):
        for name in dirnames:
            try:
                os.chmod(os.path.join(dirpath, name), 0o700)
            except OSError:
                pass
    shutil.rmtree(home, onerror=retry)


def _reset(home: Path) -> None:
    if home.exists():
        _remove(home)
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
        checked = None
        if case.programs or case.real_programs:
            # On the empty home, before setup: whatever resolves now lies
            # outside the home, whatever setup installs later.
            checked = _environment(case, arm, target)
            python = target.command[0] if target.name == "python" else sys.executable
            check_programs(case, checked, arm.cwd, home, python)
        if case.setup:
            case.setup(arm)
        env = _environment(case, arm, target)
        if checked is not None and env != checked:
            raise ProgramLeak(f"{case.id}: setup changed the environment the preflight checked")
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
        stderr_target = subprocess.PIPE
        if case.stderr_closed:
            closed_reader, writer = os.pipe()
            os.close(closed_reader)
            stderr_target = writer
        arm.started = time.time()
        proc = subprocess.Popen(target.command + argv, cwd=arm.cwd, env=env,
                                stdin=subprocess.PIPE, stdout=stdout_target,
                                stderr=stderr_target, creationflags=creation,
                                bufsize=0 if case.before_capture else -1)
        if case.stdout_closed:
            os.close(stdout_target)
        if case.stderr_closed:
            os.close(stderr_target)
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
                               f"stderr={(err or b'')[-400:]!r}") from None
        ended = time.time()
        err = stderr_prefix + (err or b"")
        if worker:
            worker.join(10)
            if worker.is_alive():
                # A helper still touching the home would race the snapshot
                # and the next arm's reset; fail the case instead.
                raise RuntimeError(f"{case.id}/{target.name}: helper thread still running")
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
    finished = False
    try:
        python = run_arm(case, oracle, home)
        rust = run_arm(case, candidate, home)
        finished = True
    finally:
        if root.exists():
            try:
                _remove(root)
            except OSError:
                # Never let teardown replace the case's own failure.
                if finished:
                    raise
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
