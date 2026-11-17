"""ops/wsl-suite.sh: its guard for runs dispatched to a second machine, its
sweep of the processes a run leaves behind, and its pruning of the test
copies and environments earlier runs left on disk.

ops/remote-suite.ps1 runs a commit's own ops/wsl-suite.sh on another
machine with PSEUDOLIFE_SUITE_DISPATCHED=1. On the maintainer's homelab box
127.0.0.1:5433 is the live bank's server, and a fixed
PSEUDOLIFE_TEST_DATABASE_URL covers only the fixtures: default paths (the
admin URL, the dev-server probe) still go to 5433 (review of #528,
2026-10-03). So a dispatched run starts only with PSEUDOLIFE_TEST_PG_HOST_PORT
naming another server, and only under that machine's own suite lease
(full-suite@<host>), or the first machine's gates would hold off for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.ops_harness import BASH, hermetic_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "wsl-suite.sh"
REFUSAL = "wsl-suite: a dispatched run"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")


@pytest.fixture(autouse=True)
def _not_profiled(monkeypatch):
    """A profiled suite exports these; a launcher under test must not inherit
    them, or it profiles itself (seen in the first profiled run, 2026-10-20)."""
    monkeypatch.delenv("PSEUDOLIFE_SUITE_PROFILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_SUITE_PROFILE_DIR", raising=False)
    # Likewise a suite run with the real native client (#1634): a launcher
    # under test that inherited the switch would start a real cargo build.
    for name in ("PSEUDOLIFE_SUITE_NATIVE_CLIENT", "PSEUDOLIFE_SUITE_CARGO_TARGET_DIR",
                 "PSEUDOLIFE_NATIVE_TEST_BINARY", "PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED",
                 "CARGO_TARGET_DIR", "PSEUDOLIFE_SUITE_PC_UNAVAILABLE", "PSEUDOLIFE_SUITE_CHOSEN",
                 "PSEUDOLIFE_SUITE_REMOTE", "PSEUDOLIFE_SUITE_WHERE"):
        monkeypatch.delenv(name, raising=False)


def _run(tmp_path: Path, *, lease_file: str | None = None, **env: str | None):
    home = tmp_path / "home"
    (home / ".pseudolife-mcp" / "locks").mkdir(parents=True, exist_ok=True)
    if lease_file is not None:
        (home / ".pseudolife-mcp" / "locks" / "full-suite.lease").write_text(
            lease_file, encoding="utf-8")
    base = {"HOME": str(home), "PSEUDOLIFE_TEST_PG_HOST_PORT": None,
            "PSEUDOLIFE_TEST_DATABASE_URL": None, "PSEUDOLIFE_SUITE_LEASE": None,
            "PSEUDOLIFE_SUITE_DISPATCHED": None,
            # Past the guard the script must stop cheaply: a commit with no
            # repository behind it fails at the mirror, before any venv.
            "PSEUDOLIFE_SUITE_COMMIT": "0" * 40,
            "PSEUDOLIFE_SUITE_GIT_COMMON": str(tmp_path / "no-such-repo")}
    base.update(env)
    proc = subprocess.run([BASH, str(SCRIPT), "--version"], capture_output=True,
                          text=True, timeout=120, env=hermetic_env(**base))
    return proc


@pytest.mark.parametrize("env", [
    {"PSEUDOLIFE_TEST_DATABASE_URL": "postgresql://u:p@127.0.0.1:5434/fixed"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:5433"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "localhost:5433"},
    # pg_defaults strips the value and int()s the port, so both reach 5433
    # (re-review of #528, 2026-10-03): the guard compares the number.
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:05433"},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:5433 "},
    {"PSEUDOLIFE_TEST_PG_HOST_PORT": "127.0.0.1:"},
    {},
], ids=["url-only", "default-port", "default-port-by-name", "zero-padded-port",
        "trailing-space", "no-port", "nothing"])
def test_a_dispatched_run_needs_a_test_server_off_5433(tmp_path, env):
    proc = _run(tmp_path, lease_file="full-suite@box\n",
                PSEUDOLIFE_SUITE_DISPATCHED="1", **env)
    assert proc.returncode == 2, proc.stderr
    assert REFUSAL in proc.stderr and "PSEUDOLIFE_TEST_PG_HOST_PORT" in proc.stderr


@pytest.mark.parametrize("lease_file, env", [
    (None, {}),
    ("full-suite\n", {}),
    ("full-suite@box\n", {"PSEUDOLIFE_SUITE_LEASE": "full-suite"}),
], ids=["no-lease-file", "plain-lease-file", "env-overrides-to-plain"])
def test_a_dispatched_run_needs_the_machines_own_lease(tmp_path, lease_file, env):
    proc = _run(tmp_path, lease_file=lease_file, PSEUDOLIFE_SUITE_DISPATCHED="1",
                PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:5434", **env)
    assert proc.returncode == 2, proc.stderr
    assert REFUSAL in proc.stderr and "full-suite@" in proc.stderr


def test_a_dispatched_run_with_both_passes_the_guard(tmp_path):
    proc = _run(tmp_path, lease_file="full-suite@box\n", PSEUDOLIFE_SUITE_DISPATCHED="1",
                PSEUDOLIFE_TEST_PG_HOST_PORT="127.0.0.1:5434")
    assert REFUSAL not in proc.stderr, proc.stderr


def test_an_ordinary_run_has_no_such_guard(tmp_path):
    proc = _run(tmp_path)
    assert REFUSAL not in proc.stderr, proc.stderr


def _alive(pid: int) -> bool:
    """Whether ``pid`` runs (a zombie, already dead, does not count)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _start_launcher(tmp_path: Path, stub_tail: str) -> tuple[subprocess.Popen, Path]:
    """Start ops/wsl-suite.sh with pytest's stand-in: a stub that detaches a
    child as the shim detaches its daemon, records the child's pid, then
    runs ``stub_tail``. Returns the launcher and the pid file."""
    venv_bin = tmp_path / "venv" / "bin"
    stubs = tmp_path / "stubs"
    venv_bin.mkdir(parents=True)
    stubs.mkdir()
    left = tmp_path / "left.pid"
    (venv_bin / "python").write_text(
        "#!/bin/bash\n"
        "setsid sleep 300 </dev/null >/dev/null 2>&1 &\n"
        f"echo $! > '{left}'\n"
        f"{stub_tail}\n", encoding="utf-8")
    (stubs / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in (venv_bin / "python", stubs / "uv"):
        stub.chmod(0o755)
    launcher = subprocess.Popen(
        [BASH, str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        env=hermetic_env(HOME=str(tmp_path / "home"),
                         PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                         PSEUDOLIFE_SUITE_VENV=str(tmp_path / "venv"),
                         PSEUDOLIFE_SUITE_COMMIT=None,
                         PSEUDOLIFE_SUITE_DISPATCHED=None))
    return launcher, left


def _left_pid(left: Path) -> int:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        text = left.read_text(encoding="ascii").strip() if left.exists() else ""
        if text:
            return int(text)
        time.sleep(0.1)
    raise AssertionError("pytest's stand-in never started")


_LINUX_ONLY = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="the sweep reads /proc; setsid is util-linux")


@_LINUX_ONLY
def test_the_launcher_stops_what_the_run_left_behind(tmp_path):
    """A detached process the run started dies when the launcher returns.

    The shim's autostarted daemon is detached on purpose (its own session),
    so neither pytest's exit nor a signal to its process group reaches it.
    Off Windows one outlived every full run for months (2026-10-03: 14 in
    WSL, 5 on the second machine, ~2.8 GB each, which exhausted its memory).
    A process the run did not start, here this test's own, is never touched,
    and the launcher still exits with pytest's own code.
    """
    outsider = subprocess.Popen(["sleep", "300"])
    leftover = None
    try:
        launcher, left = _start_launcher(tmp_path, "exit 3")
        _, stderr = launcher.communicate(timeout=120)
        assert launcher.returncode == 3, stderr
        leftover = _left_pid(left)
        assert not _alive(leftover), (
            f"the run's detached child {leftover} outlived the launcher")
        assert "left 1 process(es) behind" in stderr, stderr
        assert outsider.poll() is None, "stopped a process outside the run"
    finally:
        outsider.kill()
        outsider.wait(timeout=10)
        if leftover is not None and _alive(leftover):
            os.kill(leftover, signal.SIGKILL)


@_LINUX_ONLY
def test_a_signal_to_the_launcher_stops_the_run_and_its_leftovers(tmp_path):
    """A hangup or TERM sent to the launcher alone acts at once.

    Before the sweep the launcher exec'd pytest, so such a signal reached
    pytest directly. Now pytest is a child, and bash defers a trap until a
    foreground child exits: the signal would wait out the whole run. The
    launcher waits on pytest in the background instead and passes the
    signal on, then sweeps.
    """
    launcher = leftover = None
    try:
        launcher, left = _start_launcher(tmp_path, "exec sleep 300")
        leftover = _left_pid(left)
        started = time.monotonic()
        launcher.send_signal(signal.SIGTERM)
        _, stderr = launcher.communicate(timeout=60)
        assert time.monotonic() - started < 30, "the signal waited for the run"
        assert launcher.returncode == 143, stderr
        assert not _alive(leftover), (
            f"the run's detached child {leftover} outlived the launcher")
    finally:
        if launcher is not None and launcher.poll() is None:
            launcher.kill()  # pytest's stand-in still holds its pipes
            launcher.wait(timeout=10)
        if leftover is not None and _alive(leftover):
            os.kill(leftover, signal.SIGKILL)


@_LINUX_ONLY
def test_ctrl_c_on_a_terminal_interrupts_pytest_once(tmp_path):
    """Ctrl+C on the launcher's terminal reaches pytest exactly once.

    The terminal sends SIGINT to its whole foreground process group, and the
    launcher passes the interrupt on as well. Were pytest in that group, it
    would take two: the second aborted its session-finish cleanup (private
    banks undropped, the suite lease unreleased) in 5 of 5 reproductions
    (review of #541, 2026-10-03). pytest's stand-in counts the interrupts it
    takes during a 2 s cleanup.
    """
    import fcntl
    import termios
    import threading

    venv_bin = tmp_path / "venv" / "bin"
    stubs = tmp_path / "stubs"
    venv_bin.mkdir(parents=True)
    stubs.mkdir()
    ints, ready = tmp_path / "ints", tmp_path / "ready"
    (venv_bin / "python").write_text(
        "#!/bin/bash\n"
        f"trap 'echo int >> {ints}' INT\n"
        f"touch {ready}\n"
        "sleep 60 & wait $!\n"
        "end=$((SECONDS + 2))\n"
        "while (( SECONDS < end )); do sleep 0.1 & wait $!; done\n"
        "exit 0\n", encoding="utf-8")
    (stubs / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in (venv_bin / "python", stubs / "uv"):
        stub.chmod(0o755)
    master, slave = os.openpty()

    def own_terminal():
        # start_new_session has already made this child a session leader;
        # the pty becomes its controlling terminal, its group the foreground.
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    launcher = subprocess.Popen(
        [BASH, str(SCRIPT)], stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, preexec_fn=own_terminal,
        env=hermetic_env(HOME=str(tmp_path / "home"),
                         PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                         PSEUDOLIFE_SUITE_VENV=str(tmp_path / "venv"),
                         PSEUDOLIFE_SUITE_COMMIT=None,
                         PSEUDOLIFE_SUITE_DISPATCHED=None))
    os.close(slave)
    output = bytearray()

    def drain():
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                return
            if not chunk:
                return
            output.extend(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert time.monotonic() < deadline, output.decode(errors="replace")
            time.sleep(0.1)
        os.write(master, b"\x03")  # Ctrl+C
        launcher.wait(timeout=60)
        taken = ints.read_text(encoding="ascii").split() if ints.exists() else []
        assert taken == ["int"], (taken, output.decode(errors="replace"))
        assert launcher.returncode == 130, output.decode(errors="replace")
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        os.close(master)
        reader.join(timeout=10)


# --- the launcher prunes the test copies and environments runs leave behind
#
# Nothing removed them until 2026-10-05, when the second machine's disk had
# gone from 62% to 88% in one day (13 copies of ~600 MB, 2.9 GB of
# environments) and WSL here held 18 copies (11 GB). Each run now removes
# the stale ones itself, under the locks the runs already take.

DAY = 86400
WORK = Path(".cache") / "pseudolife-suite" / "work"
VENVS = Path(".venvs") / "pseudolife"


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _age_tree(path: Path, days: float) -> None:
    for p in sorted(path.rglob("*"), reverse=True):
        _age(p, days)
    _age(path, days)


def _copy(home: Path, entry: str, days: float) -> Path:
    """A test copy as a run leaves it: a .git, some files, and its lock."""
    path = home / WORK / entry
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")
    # Random, so a compressing filesystem still reports its size.
    (path / "payload").write_bytes(os.urandom(300_000))
    lock = home / WORK / f"{entry}.lock"
    lock.touch()
    _age_tree(path, days)
    _age(lock, days)
    return path


def _env(home: Path, key: str, days: float, lock: bool = False) -> Path:
    """An environment as uv leaves it, with a lock file if a run made one."""
    path = home / VENVS / key
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="ascii")
    _stub(path / "bin" / "python")
    _age_tree(path, days)
    if lock:
        (home / VENVS / f"{key}.lock").touch()
        _age(home / VENVS / f"{key}.lock", days)
    return path


def _env_of(home: Path, entry: str) -> str:
    """The environment key a run of copy ``entry`` uses (ops/wsl-suite.sh)."""
    name = entry.split("-", 1)[1]
    return f"{name}-{_sha8(f'{home}/{WORK.as_posix()}/{entry}')}"


def _stub(path: Path, body: str = "exit 0") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="ascii")
    path.chmod(0o755)


_SCRUBBED = {"PSEUDOLIFE_SUITE_COMMIT": None, "PSEUDOLIFE_SUITE_DISPATCHED": None,
             "PSEUDOLIFE_SUITE_ENV_FILE": None, "PSEUDOLIFE_SUITE_PRUNE": None,
             "PSEUDOLIFE_SUITE_PRUNE_DAYS": None, "PSEUDOLIFE_SUITE_KEEP": None}


def _launch(tmp_path: Path, **env: str | None) -> subprocess.CompletedProcess:
    """A worktree-mode run whose pytest and uv are stubs that exit 0, with
    its own environment outside the pruned root."""
    _stub(tmp_path / "venv" / "bin" / "python")
    _stub(tmp_path / "stubs" / "uv")
    base = {**_SCRUBBED, "HOME": str(tmp_path / "home"),
            "PATH": f"{tmp_path / 'stubs'}{os.pathsep}{os.environ['PATH']}",
            "PSEUDOLIFE_SUITE_VENV": str(tmp_path / "venv")}
    base.update(env)
    return subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True,
                          timeout=120, env=hermetic_env(**base))


def _pruned_lines(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith("wsl-suite: pruned")]


@_LINUX_ONLY
def test_a_run_prunes_stale_copies_and_environments(tmp_path):
    """Stale copies go with their environments and locks; a recent copy, a
    copy whose lock is held (a run is using it), and their environments
    stay, and one line says what went and how much it freed."""
    import fcntl

    home = tmp_path / "home"
    stale = _copy(home, "e36d1995-stale", days=10)
    stale_env = _env(home, _env_of(home, "e36d1995-stale"), days=10)
    fresh = _copy(home, "e36d1995-fresh", days=0.5)
    # A recent copy keeps its environment even when that looks stale.
    fresh_env = _env(home, _env_of(home, "e36d1995-fresh"), days=10)
    busy = _copy(home, "e36d1995-busy", days=10)
    busy_env = _env(home, _env_of(home, "e36d1995-busy"), days=10)
    loose_env = _env(home, "gone-checkout-0badf00d", days=10, lock=True)
    recent_env = _env(home, "other-checkout-12345678", days=1)
    with open(home / WORK / "e36d1995-busy.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    for gone in (stale, stale_env, loose_env):
        assert not gone.exists(), f"{gone.name} was not pruned: {proc.stderr}"
    for kept in (fresh, fresh_env, busy, busy_env, recent_env):
        assert kept.exists(), f"{kept.name} was pruned: {proc.stderr}"
    assert not (home / WORK / "e36d1995-stale.lock").exists()
    assert not (home / VENVS / "gone-checkout-0badf00d.lock").exists()
    assert (home / WORK / "e36d1995-busy.lock").exists()
    assert not list((home / WORK).glob(".trash-*")), "a renamed copy was left"
    lines = _pruned_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert "pruned 1 test copy and 2 environments" in lines[0], lines[0]
    assert "removed 1 orphaned lock file" in lines[0], lines[0]
    assert "freeing 0.0 MB" not in lines[0], lines[0]  # 300 KB of payload


@_LINUX_ONLY
def test_the_freed_space_leaves_out_files_linked_from_elsewhere(tmp_path):
    """uv hardlinks an environment's packages from its cache, so deleting the
    environment frees only what it alone holds. Counting those links, the
    first prune in WSL reported 20.8 GB where the disk gained 7 GB."""
    home = tmp_path / "home"
    env = _env(home, "gone-checkout-0badf00d", days=10)
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    (cache / "package.so").write_bytes(os.urandom(4 << 20))
    os.link(cache / "package.so", env / "package.so")
    (env / "own").write_bytes(os.urandom(2 << 20))
    _age_tree(env, 10)
    proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not env.exists() and (cache / "package.so").exists(), proc.stderr
    assert "freeing 2.0 MB" in proc.stderr, proc.stderr


@_LINUX_ONLY
def test_pruning_keeps_the_newest_copies_past_the_cap(tmp_path):
    """Past PSEUDOLIFE_SUITE_KEEP the least recently used copies go, even
    when none is stale; a recent copy's lock file stays behind it."""
    home = tmp_path / "home"
    copies = [_copy(home, f"e36d1995-c{i}", days=i * 0.1) for i in range(5)]
    proc = _launch(tmp_path, PSEUDOLIFE_SUITE_KEEP="2")
    assert proc.returncode == 0, proc.stderr
    assert [c.exists() for c in copies] == [True, True, False, False, False], proc.stderr
    # A run from before the pruner may be waiting on it (see the script).
    assert (home / WORK / "e36d1995-c4.lock").exists()


@_LINUX_ONLY
def test_pruning_removes_old_orphaned_locks_but_not_held_or_recent_ones(tmp_path):
    """A lock file whose copy is gone is removed once it is as old as a
    stale copy, unless a run holds it: a run takes the lock before it clones
    the copy, and that run's environment stays too."""
    import fcntl

    home = tmp_path / "home"
    (home / WORK).mkdir(parents=True)
    (home / VENVS).mkdir(parents=True)
    orphans = [home / WORK / "e36d1995-gone.lock", home / VENVS / "gone-0badf00d.lock"]
    recent = home / WORK / "e36d1995-recent.lock"
    held_path = home / WORK / "e36d1995-cloning.lock"
    for lock in (*orphans, recent, held_path):
        lock.touch()
    for lock in (*orphans, held_path):
        _age(lock, 10)
    cloning_env = _env(home, _env_of(home, "e36d1995-cloning"), days=10)
    with open(held_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not any(o.exists() for o in orphans), proc.stderr
    assert held_path.exists() and recent.exists(), proc.stderr
    assert cloning_env.exists(), proc.stderr
    assert "removed 2 orphaned lock files" in proc.stderr, proc.stderr


@_LINUX_ONLY
def test_pruning_spares_an_environment_a_process_runs_from(tmp_path):
    """An environment with no lock (a run from before the pruner) stays
    while any process's argv[0] lies inside it, and goes once none does."""
    home = tmp_path / "home"
    env = _env(home, "old-run-0badf00d", days=10)
    runner = subprocess.Popen(
        [BASH, "-c", f'exec -a "{env}/bin/python" "{BASH}" -c "sleep 300; :"'],
        start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not Path(f"/proc/{runner.pid}/cmdline").read_bytes().startswith(
                str(env).encode()):
            assert time.monotonic() < deadline, "the stand-in run never started"
            time.sleep(0.05)
        proc = _launch(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert env.exists(), proc.stderr
    finally:
        os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)
    proc = _launch(tmp_path)
    assert not env.exists(), proc.stderr


@_LINUX_ONLY
def test_pruning_spares_a_copy_a_process_works_in(tmp_path):
    """A copy with no lock held stays while a process's working directory
    lies inside it (a shell cd'd into it by hand), and goes once none does."""
    home = tmp_path / "home"
    copy = _copy(home, "e36d1995-debugging", days=10)
    (copy / "sub").mkdir()
    _age_tree(copy, 10)
    shell = subprocess.Popen(["sleep", "300"], cwd=copy / "sub")
    try:
        proc = _launch(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert copy.exists(), proc.stderr
    finally:
        shell.kill()
        shell.wait(timeout=10)
    proc = _launch(tmp_path)
    assert not copy.exists(), proc.stderr


@_LINUX_ONLY
def test_pruning_spares_an_environment_whose_lock_a_run_holds(tmp_path):
    """A run holds its environment's lock shared for its whole length; an
    environment so held stays, though old and with no copy of its own."""
    import fcntl

    home = tmp_path / "home"
    env = _env(home, "worktree-run-0badf00d", days=10, lock=True)
    with open(home / VENVS / "worktree-run-0badf00d.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_SH | fcntl.LOCK_NB)
        proc = _launch(tmp_path, PSEUDOLIFE_SUITE_PRUNE_DAYS="0", PSEUDOLIFE_SUITE_KEEP="0")
    assert proc.returncode == 0, proc.stderr
    assert env.exists(), proc.stderr


@_LINUX_ONLY
def test_pruning_takes_names_with_spaces_and_glob_characters_literally(tmp_path):
    """A stale copy named with a space and glob characters goes with its
    environment; a recent copy its name would match as a pattern stays."""
    home = tmp_path / "home"
    odd = _copy(home, "e36d1995-a b[1]*", days=10)
    odd_env = _env(home, _env_of(home, "e36d1995-a b[1]*"), days=10)
    match = _copy(home, "e36d1995-a b1x", days=0)
    match_env = _env(home, _env_of(home, "e36d1995-a b1x"), days=10)
    proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not odd.exists() and not odd_env.exists(), proc.stderr
    assert match.exists() and match_env.exists(), proc.stderr
    assert "pruned 1 test copy and 1 environment " in proc.stderr, proc.stderr


@_LINUX_ONLY
def test_pruning_refuses_a_symlinked_root(tmp_path):
    """A root that is itself a symlink could point at something broad, so
    nothing is pruned through it, and one line says why."""
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    _env(elsewhere, "old-0badf00d", days=10)
    real = elsewhere / VENVS
    (home / VENVS).parent.mkdir(parents=True)
    (home / VENVS).symlink_to(real, target_is_directory=True)
    proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (real / "old-0badf00d").exists(), proc.stderr
    assert "not pruning" in proc.stderr and "is a symlink" in proc.stderr, proc.stderr


@_LINUX_ONLY
def test_pruning_never_follows_a_symlink_out_of_its_root(tmp_path):
    """A stale entry or lock file that is a symlink stays, and so does what
    it points at: only real directories inside the two roots are removed."""
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (outside / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="ascii")
    (outside / "keep").write_text("data", encoding="ascii")
    links = []
    for root, name in ((WORK, "e36d1995-link"), (VENVS, "link-0badf00d")):
        (home / root).mkdir(parents=True, exist_ok=True)
        link = home / root / name
        link.symlink_to(outside, target_is_directory=True)
        _age(link, 10)
        links.append(link)
    # A dangling lock link would otherwise be created, outside the root.
    (home / WORK / "e36d1995-link.lock").symlink_to(tmp_path / "made-outside")
    _age_tree(outside, 10)
    proc = _launch(tmp_path, PSEUDOLIFE_SUITE_PRUNE_DAYS="0", PSEUDOLIFE_SUITE_KEEP="0")
    assert proc.returncode == 0, proc.stderr
    assert (outside / "keep").read_text(encoding="ascii") == "data"
    assert all(link.is_symlink() for link in links), proc.stderr
    assert not (tmp_path / "made-outside").exists(), proc.stderr
    assert not _pruned_lines(proc.stderr), proc.stderr


@_LINUX_ONLY
def test_pruning_finishes_what_an_interrupted_prune_left(tmp_path):
    """A copy renamed for deletion by a prune that was stopped is deleted by
    the next run, whatever its age; one whose pruner still runs stays."""
    home = tmp_path / "home"
    (home / WORK).mkdir(parents=True)
    dead = home / WORK / ".trash-999999999-e36d1995-old"
    live = home / WORK / f".trash-{os.getpid()}-e36d1995-busy"
    for trash in (dead, live):
        (trash / ".git").mkdir(parents=True)
    proc = _launch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not dead.exists() and live.exists(), proc.stderr


@_LINUX_ONLY
@pytest.mark.parametrize("value", ["off", "OFF", "0", "False", "no"])
def test_pruning_can_be_turned_off(tmp_path, value):
    stale = _copy(tmp_path / "home", "e36d1995-stale", days=10)
    proc = _launch(tmp_path, PSEUDOLIFE_SUITE_PRUNE=value)
    assert proc.returncode == 0, proc.stderr
    assert stale.exists() and not _pruned_lines(proc.stderr), proc.stderr


# --- copy mode: the run's own copy and environment, and its locks


def _copy_mode(tmp_path: Path) -> tuple[Path, str, dict[str, str | None]]:
    """A repository with one commit, and the settings for a copy-mode run of
    it named "mine". Returns the copy's entry name, the environment's path
    and the environment variables; pytest's stand-in is that environment's
    python, which exits 0."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", str(repo)], check=True)
    (repo / "README").write_text("x", encoding="ascii")
    subprocess.run([*git, "-C", str(repo), "add", "README"], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "init"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    common = str(repo / ".git")
    entry = f"{_sha8(common)}-mine"
    env_path = _env(home, _env_of(home, entry), days=0, lock=True)
    _stub(tmp_path / "stubs" / "uv")
    env = {**_SCRUBBED, "HOME": str(home),
           "PATH": f"{tmp_path / 'stubs'}{os.pathsep}{os.environ['PATH']}",
           "PSEUDOLIFE_SUITE_COMMIT": sha, "PSEUDOLIFE_SUITE_GIT_COMMON": common,
           "PSEUDOLIFE_SUITE_NAME": "mine", "PSEUDOLIFE_SUITE_VENV": None}
    return home / WORK / entry, env_path, env


_NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")


@_LINUX_ONLY
@_NEEDS_GIT
def test_a_copy_mode_run_never_prunes_its_own_copy_or_environment(tmp_path):
    """A run's own copy and environment stay however old they are, and count
    toward the kept number: with a cap of 1 the other, recent copy goes."""
    own, own_env, env = _copy_mode(tmp_path)
    first = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True,
                           timeout=120, env=hermetic_env(**env))
    assert first.returncode == 0 and (own / "README").exists(), first.stderr
    home = tmp_path / "home"
    _age_tree(own, 10)
    _age_tree(own_env, 10)
    for lock in (own.with_name(own.name + ".lock"), own_env.with_name(own_env.name + ".lock")):
        _age(lock, 10)
    other = _copy(home, "e36d1995-other", days=0)
    other_env = _env(home, _env_of(home, "e36d1995-other"), days=0)
    proc = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True, timeout=120,
                          env=hermetic_env(**{**env, "PSEUDOLIFE_SUITE_KEEP": "1"}))
    assert proc.returncode == 0, proc.stderr
    assert (own / "README").exists(), proc.stderr
    assert (own_env / "bin" / "python").exists(), proc.stderr
    assert not other.exists() and not other_env.exists(), proc.stderr
    assert "pruned 1 test copy and 1 environment " in proc.stderr, proc.stderr


def _held(path: Path) -> bool:
    """Whether some process holds a lock on the file now at ``path``."""
    import fcntl

    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@_LINUX_ONLY
@_NEEDS_GIT
@pytest.mark.parametrize("which", ["copy", "environment"])
def test_a_run_reopens_a_lock_file_removed_while_it_waited(tmp_path, which):
    """A run waiting on a lock file that a pruner then deletes takes the new
    file at that path, not the deleted one: otherwise a later run would
    take the new file at once and both would use the same copy."""
    import fcntl

    own, own_env, env = _copy_mode(tmp_path)
    first = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True,
                           timeout=120, env=hermetic_env(**env))
    assert first.returncode == 0, first.stderr
    ready = tmp_path / "ready"
    _stub(own_env / "bin" / "python",
          f'touch "{ready}"\nwhile [ -e "{ready}" ]; do sleep 0.1; done\nexit 0')
    lock = (own.with_name(own.name + ".lock") if which == "copy"
            else own_env.with_name(own_env.name + ".lock"))
    log = tmp_path / "launcher.log"
    holder = open(lock, "a")
    launcher = None
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(log, "w") as out:
            launcher = subprocess.Popen([BASH, str(SCRIPT)], stdout=out, stderr=out,
                                        env=hermetic_env(**env))
        fd = 9 if which == "copy" else 7
        deadline = time.monotonic() + 60
        # The launcher has opened the lock and is about to wait on it.
        while not (Path(f"/proc/{launcher.pid}/fd/{fd}").exists()
                   and os.readlink(f"/proc/{launcher.pid}/fd/{fd}") == str(lock)):
            assert launcher.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        time.sleep(0.3)
        lock.unlink()  # as the pruner does, while holding it
        holder.close()
        while not ready.exists():
            assert launcher.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        assert lock.exists() and _held(lock), (
            f"the run holds the deleted {which} lock, not the one at its path")
        ready.unlink()
        assert launcher.wait(timeout=60) == 0, log.read_text()
    finally:
        holder.close()
        if launcher is not None and launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)


# ── the default full run's xdist workers ──────────────────────────────────

def _pytest_argv(tmp_path: Path, *args: str, **env: str | None):
    """Run the launcher with pytest's stand-in recording the argv it got."""
    venv_bin = tmp_path / "venv" / "bin"
    stubs = tmp_path / "stubs"
    venv_bin.mkdir(parents=True)
    stubs.mkdir()
    record = tmp_path / "argv"
    (venv_bin / "python").write_text(
        f"#!/bin/bash\nprintf '%s\n' \"$@\" > '{record}'\n", encoding="utf-8")
    # The stand-in for uv leaves a mark: a refusal must come before it.
    (stubs / "uv").write_text(f"#!/bin/sh\ntouch '{tmp_path}/uv-called'\nexit 0\n",
                              encoding="utf-8")
    for stub in (venv_bin / "python", stubs / "uv"):
        stub.chmod(0o755)
    base = {"HOME": str(tmp_path / "home"),
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "PSEUDOLIFE_SUITE_VENV": str(tmp_path / "venv"),
            "PSEUDOLIFE_SUITE_COMMIT": None, "PSEUDOLIFE_SUITE_DISPATCHED": None,
            "PSEUDOLIFE_SUITE_WORKERS": None}
    base.update(env)
    proc = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=120, env=hermetic_env(**base))
    argv = (record.read_text(encoding="utf-8").splitlines()
            if record.exists() else None)
    return proc, argv


_FULL = ["-m", "pytest", "tests/", "-q", "-rs"]


@_LINUX_ONLY
def test_the_default_full_run_takes_the_workers_asked_for(tmp_path):
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == [*_FULL, "-n", "3", "--dist", "loadfile", "--durations=30"]
    assert "full suite, 3 xdist workers" in proc.stdout


@pytest.mark.parametrize("workers", ["0", "1"])
@_LINUX_ONLY
def test_one_worker_or_none_runs_the_suite_in_one_process(tmp_path, workers):
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS=workers)
    assert proc.returncode == 0, proc.stderr
    assert argv == _FULL
    assert "full suite, one process" in proc.stdout


@_LINUX_ONLY
def test_arguments_still_replace_the_default_untouched(tmp_path):
    proc, argv = _pytest_argv(tmp_path, "tests/test_bm25.py", "-x",
                              PSEUDOLIFE_SUITE_WORKERS="4")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", "tests/test_bm25.py", "-x"]


@_LINUX_ONLY
def test_a_malformed_worker_count_is_refused_before_pytest(tmp_path):
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS="four")
    assert proc.returncode == 2
    assert argv is None
    assert "PSEUDOLIFE_SUITE_WORKERS must be a whole number" in proc.stderr


def _expected_workers() -> int:
    """The launcher's rule, derived here from the same sources: at most 4,
    one per two CPUs, one per 6 GB of the smaller of RAM and the cgroup's
    memory.max (a dispatched run's systemd scope caps it at 16 GB)."""
    with open("/proc/meminfo", encoding="ascii") as f:
        mem_kb = next(int(line.split()[1]) for line in f
                      if line.startswith("MemTotal:"))
    with open("/proc/self/cgroup", encoding="ascii") as f:
        lines = [line.rstrip("\n").split(":", 2) for line in f]
    v2 = next((path for _, ctl, path in lines if ctl == ""), "")
    v1 = next((path for _, ctl, path in lines if "memory" in ctl.split(",")), "")
    # cgroup v2's memory.max, else v1's memory controller (Codex review,
    # 2026-10-19: the launcher reads both, so the oracle must too).
    for limit_file in (Path(f"/sys/fs/cgroup{v2}/memory.max"),
                       Path(f"/sys/fs/cgroup/memory{v1}/memory.limit_in_bytes")):
        try:
            limit = limit_file.read_text().strip()
        except OSError:
            continue
        if limit.isdigit():
            mem_kb = min(mem_kb, int(limit) // 1024)
        break
    cpus = len(os.sched_getaffinity(0))
    return min(4, cpus // 2, mem_kb // (6 * 1024 * 1024))


@_LINUX_ONLY
def test_the_default_worker_count_follows_cpus_and_memory(tmp_path):
    """OMP_NUM_THREADS is set on purpose, as CI's Linux lanes set it: GNU
    nproc reports that value instead of the CPUs (Codex review,
    2026-10-19), and the launcher must count the CPUs."""
    proc, argv = _pytest_argv(tmp_path, OMP_NUM_THREADS="1",
                              OMP_THREAD_LIMIT="1")
    assert proc.returncode == 0, proc.stderr
    workers = _expected_workers()
    if workers >= 2:
        assert argv == [*_FULL, "-n", str(workers), "--dist", "loadfile",
                        "--durations=30"]
    else:
        assert argv == _FULL


@_LINUX_ONLY
def test_a_zero_padded_worker_count_is_read_as_decimal(tmp_path):
    """bash arithmetic reads "08" as bad octal and "010" as 8 (Claude
    review, 2026-10-19); the launcher reads the count as decimal."""
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS="08")
    assert proc.returncode == 0, proc.stderr
    assert argv == [*_FULL, "-n", "8", "--dist", "loadfile", "--durations=30"]


_PROFILE = ["-p", "tests.suite_profile_plugin"]


@_LINUX_ONLY
def test_profiling_adds_the_plugin_and_keeps_the_default_workers(tmp_path):
    profile = tmp_path / "profile"
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS="3",
                              PSEUDOLIFE_SUITE_PROFILE="1",
                              PSEUDOLIFE_SUITE_PROFILE_DIR=str(profile))
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", *_PROFILE, "tests/", "-q", "-rs", "-n", "3",
                    "--dist", "loadfile", "--durations=30"]
    assert f"wsl-suite: profiling to {profile}" in proc.stdout
    assert profile.is_dir()


@_LINUX_ONLY
def test_profiling_keeps_chosen_arguments_as_they_are(tmp_path):
    proc, argv = _pytest_argv(tmp_path, "tests/test_bm25.py", "-x",
                              PSEUDOLIFE_SUITE_PROFILE="1",
                              PSEUDOLIFE_SUITE_PROFILE_DIR=str(tmp_path / "p"))
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", *_PROFILE, "tests/test_bm25.py", "-x"]


@pytest.mark.parametrize("value", ["", "0", "off", "false", "no"])
@_LINUX_ONLY
def test_profiling_is_off_unless_asked_for(tmp_path, value):
    proc, argv = _pytest_argv(tmp_path, PSEUDOLIFE_SUITE_WORKERS="1",
                              PSEUDOLIFE_SUITE_PROFILE=value)
    assert proc.returncode == 0, proc.stderr
    assert argv == _FULL
    assert "profiling" not in proc.stdout


@_LINUX_ONLY
def test_a_failing_profiled_run_keeps_its_exit_code(tmp_path):
    """pytest's code survives the summary step, even when the summary
    itself fails; the summary runs only once the plugin wrote a manifest."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls"
    (venv_bin / "python").write_text(
        "#!/bin/bash\n"
        f"echo \"$1\" >> '{calls}'\n"
        'if [[ "$1" == -m ]]; then\n'
        '    echo "{}" > "$PSEUDOLIFE_SUITE_PROFILE_DIR/manifest.json"; exit 5\n'
        "fi\n"
        "exit 1\n", encoding="utf-8")
    (stubs / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for stub in (venv_bin / "python", stubs / "uv"):
        stub.chmod(0o755)
    env = hermetic_env(
        HOME=str(tmp_path / "home"), PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
        PSEUDOLIFE_SUITE_VENV=str(tmp_path / "venv"), PSEUDOLIFE_SUITE_COMMIT=None,
        PSEUDOLIFE_SUITE_DISPATCHED=None, PSEUDOLIFE_SUITE_WORKERS="1",
        PSEUDOLIFE_SUITE_PROFILE="1",
        PSEUDOLIFE_SUITE_PROFILE_DIR=str(tmp_path / "profile"))
    proc = subprocess.run([BASH, str(SCRIPT)], capture_output=True, text=True,
                          timeout=120, env=env)

    assert proc.returncode == 5, proc.stderr
    assert calls.read_text(encoding="utf-8").split() == [
        "-m", "evals/suite_profile_summary.py"]
    assert "the profile summary failed" in proc.stderr


# The launcher's argument contract (plan_run in ops/wsl-suite.sh): display
# options and worker choices keep the full suite; anything else, or a `--`,
# goes to pytest unchanged; a malformed choice is refused before anything is
# fetched or installed.

_AUTO = ["-n", "3", "--dist", "loadfile", "--durations=30"]


@pytest.mark.parametrize("args, expected", [
    (["-q"], ["tests/", "-q", *_AUTO]),
    (["-v", "-rs"], ["tests/", "-v", "-rs", *_AUTO]),
    (["-vv", "-ra", "--tb=short"], ["tests/", "-vv", "-ra", "--tb=short", *_AUTO]),
    (["--tb", "line"], ["tests/", "--tb=line", *_AUTO]),
    (["--quiet"], ["tests/", "--quiet", *_AUTO]),
    (["--verbose", "-qqq"], ["tests/", "--verbose", "-qqq", *_AUTO]),
    (["-vvvv"], ["tests/", "-vvvv", *_AUTO]),
    (["--durations-min=.5"], ["tests/", "--durations-min=.5", *_AUTO]),
    (["--durations-min", "1e-3"], ["tests/", "--durations-min=1e-3", *_AUTO]),
    (["--durations-min=1."], ["tests/", "--durations-min=1.", *_AUTO]),
    # A chosen --durations replaces the default one, never adds a second.
    (["-q", "--durations=10"], ["tests/", "-q", "--durations=10", "-n", "3",
                                "--dist", "loadfile"]),
    (["--durations", "5", "--durations-min=0.5"],
     ["tests/", "--durations=5", "--durations-min=0.5", "-n", "3", "--dist", "loadfile"]),
])
@_LINUX_ONLY
def test_display_options_keep_the_full_suite_and_its_workers(tmp_path, args, expected):
    proc, argv = _pytest_argv(tmp_path, *args, PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", *expected]
    assert "full suite, 3 xdist workers (PSEUDOLIFE_SUITE_WORKERS)" in proc.stdout


@pytest.mark.parametrize("args", [
    ["-n", "2"], ["-n2"], ["--numprocesses=2"], ["--numprocesses", "2"], ["-n", "02"],
])
@_LINUX_ONLY
def test_a_worker_choice_wins_and_is_never_doubled(tmp_path, args):
    proc, argv = _pytest_argv(tmp_path, *args, PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", "tests/", "-q", "-rs", "-n", "2",
                    "--dist", "loadfile", "--durations=30"]
    assert "full suite, 2 xdist workers (-n)" in proc.stdout


@pytest.mark.parametrize("args, expected, note", [
    (["-n", "0"], ["-n", "0"], "full suite, one process (-n)"),
    (["-q", "-n", "1"], ["-n", "1", "--dist", "loadfile", "--durations=30"],
     "full suite, 1 xdist worker (-n)"),
    (["-n", "auto"], ["-n", "auto", "--dist", "loadfile", "--durations=30"],
     "full suite, auto xdist workers (-n)"),
    (["--dist", "worksteal", "-n", "4"], ["-n", "4", "--dist", "worksteal",
                                          "--durations=30"],
     "full suite, 4 xdist workers (-n)"),
])
@_LINUX_ONLY
def test_serial_auto_and_distribution_choices_are_kept(tmp_path, args, expected, note):
    proc, argv = _pytest_argv(tmp_path, *args, PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    display = ["-q"] if "-q" in args else ["-q", "-rs"]
    assert argv == ["-m", "pytest", "tests/", *display, *expected]
    assert note in proc.stdout


@_LINUX_ONLY
def test_a_distribution_with_one_automatic_worker_stays_one_process(tmp_path):
    """xdist ignores --dist without workers (checked 2026-10-20, xdist
    3.8): the choice is passed on, and the run says it is one process."""
    proc, argv = _pytest_argv(tmp_path, "--dist", "loadscope",
                              PSEUDOLIFE_SUITE_WORKERS="1")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", "tests/", "-q", "-rs", "--dist", "loadscope"]
    assert "full suite, one process (PSEUDOLIFE_SUITE_WORKERS)" in proc.stdout


@_LINUX_ONLY
def test_a_chosen_distribution_replaces_loadfile_with_automatic_workers(tmp_path):
    proc, argv = _pytest_argv(tmp_path, "-q", "--dist=loadscope",
                              PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", "tests/", "-q", "-n", "3", "--dist", "loadscope",
                    "--durations=30"]


@pytest.mark.parametrize("args", [
    ["tests/test_bm25.py"],
    ["-q", "tests/test_bm25.py"],
    ["-k", "a and b"],
    ["-m", "not slow", "-n", "2"],
    ["--collect-only", "-q"],
    ["--help"],
    ["-x"],
    ["--lf"],
    ["-rZ"],
    ["-q", "--", "-n", "4"],
    ["--", "tests/"],
    ["-n", "2", "tests/x.py"],
    ["--tb", "short", "-k", "foo"],
    ["-n", "3", "-n", "4", "tests/x.py"],
    ["tests/x.py", "-n", "3", "-n", "4"],
    ["--tb=bogus", "-k", "foo"],
    ["-n", "four", "tests/x.py"],
    ["--durations=", "5"],
    ["tests/test_a.py::test_b[x y]"],
])
@_LINUX_ONLY
def test_selections_and_unknown_options_pass_through_unchanged(tmp_path, args):
    proc, argv = _pytest_argv(tmp_path, *args, PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", *args]
    assert ("chosen arguments, passed to pytest unchanged (no xdist workers added)"
            in proc.stdout)


@pytest.mark.parametrize("args, message", [
    (["-n"], "-n needs a value"),
    (["-q", "--numprocesses"], "--numprocesses needs a value"),
    (["-n", "four"], "-n takes a whole number, auto or logical, not 'four'"),
    (["-n", "-1"], "-n takes a whole number"),
    (["-n", "2", "-n", "3"], "workers chosen twice"),
    (["-n2", "--numprocesses=3"], "workers chosen twice"),
    (["--dist", "bogus"], "--dist takes an xdist mode, not 'bogus'"),
    (["--dist", "load", "--dist=each"], "--dist given twice"),
    (["--dist"], "--dist needs a value"),
    (["--durations"], "--durations needs a value"),
    (["--durations=ten"], "--durations takes a number, not 'ten'"),
    (["--tb", "verbose"], "--tb takes auto, long, short, line, native or no"),
    (["--durations=", "-q"], "--durations needs a value"),
    (["--tb=", "-q"], "--tb needs a value"),
    (["--numprocesses=", "-v"], "--numprocesses needs a value"),
    (["--dist=", "-v"], "--dist needs a value"),
    (["--durations-min=fast"], "--durations-min takes a number"),
])
@_LINUX_ONLY
def test_a_malformed_choice_is_refused_before_anything_is_installed(tmp_path, args, message):
    proc, argv = _pytest_argv(tmp_path, *args)
    assert proc.returncode == 2
    assert argv is None
    assert message in proc.stderr
    assert not (tmp_path / "uv-called").exists()


@_LINUX_ONLY
def test_the_built_command_is_printed_and_chosen_arguments_are_not(tmp_path):
    proc, _ = _pytest_argv(tmp_path, "-v", PSEUDOLIFE_SUITE_WORKERS="2")
    assert ("wsl-suite: pytest tests/ -v -n 2 --dist loadfile --durations=30"
            in proc.stdout)
    # A chosen argument may name a local path; it is passed on, not echoed.
    proc, argv = _pytest_argv(tmp_path / "2", "--junitxml=/srv/private/j.xml",
                              PSEUDOLIFE_SUITE_WORKERS="2")
    assert argv == ["-m", "pytest", "--junitxml=/srv/private/j.xml"]
    assert "/srv/private" not in proc.stdout + proc.stderr
    assert "wsl-suite: pytest" not in proc.stdout


@_LINUX_ONLY
def test_profiling_keeps_display_options_on_the_full_suite(tmp_path):
    proc, argv = _pytest_argv(tmp_path, "-q", PSEUDOLIFE_SUITE_WORKERS="3",
                              PSEUDOLIFE_SUITE_PROFILE="1",
                              PSEUDOLIFE_SUITE_PROFILE_DIR=str(tmp_path / "p"))
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", *_PROFILE, "tests/", "-q", *_AUTO]


@_LINUX_ONLY
def test_dist_no_runs_the_full_suite_in_one_process(tmp_path):
    """xdist turns --dist no into load whenever it has workers (xdist 3.8
    plugin.py), so a chosen --dist no must not get automatic ones."""
    proc, argv = _pytest_argv(tmp_path, "--dist", "no", PSEUDOLIFE_SUITE_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert argv == ["-m", "pytest", "tests/", "-q", "-rs", "--dist", "no"]
    assert "full suite, one process (--dist no)" in proc.stdout



# ── the real native client (PSEUDOLIFE_SUITE_NATIVE_CLIENT, #1634) ─────────
#
# The hosted `test` lane builds the native client and exports
# PSEUDOLIFE_NATIVE_TEST_BINARY with PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED=1
# (tests/test_ci_native_client.py pins that side). A local run that never did
# passed an integration batch that then failed 18 real-client tests hosted.
# Here cargo, df, uv and pytest are stubs: what is checked is the launcher's
# contract, not a real build.

_STUB_CARGO = r'''#!/bin/bash
printf '%s|%s|%s\n' "$PWD" "${CARGO_TARGET_DIR:-}" "$*" >> "$CARGO_CALLS"
# CARGO_FDS: what this cargo (and so its rustc and wrappers) holds open.
if [ -n "${CARGO_FDS:-}" ]; then ls -l "/proc/$$/fd" >> "$CARGO_FDS" 2>/dev/null; fi
if [ "$1" = metadata ]; then
    printf '%s' '{"packages":[],"workspace_members":["path+file:///w/rust/shim#pseudolife-stdio@0.24.0","path+file:///w/rust/daemon#pseudolife-daemon@0.24.0"],"resolve":null}'
    exit 0
fi
if [ "$1" = clean ]; then exit "${STUB_CLEAN_EXIT:-0}"; fi
if [ -n "${STUB_CARGO_FAIL:-}" ]; then
    echo "error[E0425]: cannot find value \`nope\` in this scope" >&2
    exit 101
fi
mkdir -p "$CARGO_TARGET_DIR/debug"
cat > "$CARGO_TARGET_DIR/debug/pseudolife-stdio" <<'EOF'
#!/bin/sh
if [ "$1" = version ]; then echo "pseudolife-mcp 1.2.3"; exit "${STUB_VERSION_EXIT:-0}"; fi
exit 0
EOF
chmod 755 "$CARGO_TARGET_DIR/debug/pseudolife-stdio"
'''


def _native_run(tmp_path: Path, *args: str, df_kb: int | None = None,
                cargo: bool = True, popen: bool = False, cwd: Path | None = None,
                **env: str | None):
    """Run the launcher in worktree mode with stubs for uv, cargo (which
    writes a client that answers `version`) and, when `df_kb` is given, df.
    pytest's stand-in records its argv and the client variables it got."""
    home = tmp_path / "home"
    venv_bin = home / "v" / "bin"
    stubs = tmp_path / "stubs"
    venv_bin.mkdir(parents=True)
    stubs.mkdir()
    (venv_bin / "python").write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" > '{tmp_path}/argv'\n"
        f"printf '%s\\n' \"binary=${{PSEUDOLIFE_NATIVE_TEST_BINARY:-}}\" "
        f"\"required=${{PSEUDOLIFE_NATIVE_TEST_BINARY_REQUIRED:-}}\" "
        f"\"switch=${{PSEUDOLIFE_SUITE_NATIVE_CLIENT:-}}\" > '{tmp_path}/client-env'\n",
        encoding="utf-8")
    (stubs / "uv").write_text(f"#!/bin/sh\ntouch '{tmp_path}/uv-called'\nexit 0\n",
                              encoding="utf-8")
    shims = [venv_bin / "python", stubs / "uv"]
    if cargo:
        (stubs / "cargo").write_text(_STUB_CARGO, encoding="utf-8")
        shims.append(stubs / "cargo")
    if df_kb is not None:
        (stubs / "df").write_text(
            "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
            f"echo '/dev/x 999999999 1 {df_kb} 1% /'\n", encoding="utf-8")
        shims.append(stubs / "df")
    for shim in shims:
        shim.chmod(0o755)
    base = {"HOME": str(home),
            # No ~/.cargo/bin of the real user: only the stub is a cargo.
            "PATH": f"{stubs}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            "PSEUDOLIFE_SUITE_VENV": str(home / "v"),
            "PSEUDOLIFE_SUITE_COMMIT": None, "PSEUDOLIFE_SUITE_DISPATCHED": None,
            "PSEUDOLIFE_SUITE_WORKERS": "0", "PSEUDOLIFE_SUITE_PRUNE": "off",
            "PSEUDOLIFE_SUITE_LOCK": None, "PSEUDOLIFE_SUITE_LOCK_DIR": None,
            "CARGO_CALLS": str(tmp_path / "cargo-calls")}
    base.update(env)
    argv = [BASH, str(SCRIPT), *args]
    if popen:
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd, env=hermetic_env(**base))
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=cwd,
                          env=hermetic_env(**base))


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _cargo(tmp_path: Path, command: str) -> list[str]:
    """The stub cargo's calls of one subcommand (build, clean, metadata)."""
    return [call for call in _read(tmp_path / "cargo-calls")
            if call.split("|")[2].split(" ")[0] == command]


def _head() -> str:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


@_LINUX_ONLY
@pytest.mark.parametrize("value", [None, "", "0", "off", "false", "no"])
def test_without_the_switch_the_run_says_it_has_no_real_client(tmp_path, value):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT=value)
    assert proc.returncode == 0, proc.stderr
    assert "wsl-suite: real client: no (PSEUDOLIFE_SUITE_NATIVE_CLIENT is off" in proc.stdout
    assert _read(tmp_path / "cargo-calls") == []
    assert _read(tmp_path / "client-env") == ["binary=", "required=", "switch="]
    # Said before the environment is installed: among the run's first lines.
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("wsl-suite: real client: no"), lines


@_LINUX_ONLY
@_NEEDS_GIT
def test_the_switch_builds_the_client_as_the_hosted_lane_does_and_exports_it(tmp_path):
    target = tmp_path / "target"
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                       PSEUDOLIFE_SUITE_CARGO_TARGET_DIR=str(target))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # One build, from the tested checkout's rust/ (rust-toolchain.toml
    # applies), as ci.yml's "Build the native client the installer tests run".
    (call,) = _cargo(tmp_path, "build")
    cwd, target_dir, cargo_args = call.split("|")
    assert Path(cwd).parent == ROOT and Path(cwd).name == "rust" and target_dir == str(target)
    assert cargo_args == "build --locked -p pseudolife-stdio -j 4"

    # pytest gets a copy in the run's own environment, never the shared
    # target's file another build may replace, and must run it.
    copy = tmp_path / "home" / "v" / "native-client" / "pseudolife-stdio"
    assert _read(tmp_path / "client-env") == [f"binary={copy}", "required=1", "switch="]
    assert copy.read_bytes() == (target / "debug" / "pseudolife-stdio").read_bytes()
    assert not copy.samefile(target / "debug" / "pseudolife-stdio")
    said = [ln for ln in proc.stdout.splitlines() if ln.startswith("wsl-suite: real client: yes")]
    assert said == [f"wsl-suite: real client: yes, pseudolife-stdio built from {_head()} "
                    "(dev profile, --locked; pseudolife-mcp 1.2.3), "
                    "at ~/v/native-client/pseudolife-stdio"]
    assert _read(tmp_path / "argv")[:2] == ["-m", "pytest"]


@_LINUX_ONLY
def test_the_build_cache_is_one_per_machine_by_default(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (call,) = _cargo(tmp_path, "build")
    assert call.split("|")[1] == str(tmp_path / "home" / ".cache" / "pseudolife-suite" / "cargo-target")


@_LINUX_ONLY
def test_a_general_cargo_target_dir_is_never_the_cache(tmp_path):
    # Other builds write a general one, and cargo can take their units as
    # this checkout's (review of #1634); only these runs write the cache.
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="on",
                       CARGO_TARGET_DIR=str(tmp_path / "shared"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (call,) = _cargo(tmp_path, "build")
    assert call.split("|")[1] == str(tmp_path / "home" / ".cache" / "pseudolife-suite" / "cargo-target")


@_LINUX_ONLY
def test_a_relative_cache_is_resolved_before_cargo_changes_directory(tmp_path):
    # cargo would resolve it from rust/, the copy step from the launcher's
    # directory (Codex review of #1634).
    work = tmp_path / "launched-from"
    work.mkdir()
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", cwd=work,
                       PSEUDOLIFE_SUITE_CARGO_TARGET_DIR="rel-target")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (call,) = _cargo(tmp_path, "build")
    assert call.split("|")[1] == str(work / "rel-target")
    assert (work / "rel-target" / "debug" / "pseudolife-stdio").is_file()


@_LINUX_ONLY
def test_a_build_after_another_checkouts_cleans_the_workspace_packages_first(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / ".suite-build-source").write_text("/elsewhere/other-copy\n", encoding="utf-8")
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                       PSEUDOLIFE_SUITE_CARGO_TARGET_DIR=str(target))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (clean,) = _cargo(tmp_path, "clean")
    assert clean.split("|")[1] == str(target)
    assert clean.split("|")[2] == ("clean --quiet -p path+file:///w/rust/shim#pseudolife-stdio@0.24.0 "
                                   "-p path+file:///w/rust/daemon#pseudolife-daemon@0.24.0")
    assert "last held another checkout's build; cleaning this workspace's 2 packages" in proc.stdout
    # Cleaned before the build, and the cache now names this checkout.
    calls = [c.split("|")[2].split(" ")[0] for c in _read(tmp_path / "cargo-calls")]
    assert calls.index("clean") < calls.index("build")
    assert (target / ".suite-build-source").read_text(encoding="utf-8").strip() == str(ROOT)

    # The same checkout again: its own units are there, so nothing is cleaned.
    (tmp_path / "cargo-calls").unlink()
    proc = _native_run(tmp_path / "again", PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                       PSEUDOLIFE_SUITE_CARGO_TARGET_DIR=str(target),
                       CARGO_CALLS=str(tmp_path / "cargo-calls"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _cargo(tmp_path, "clean") == [] and len(_cargo(tmp_path, "build")) == 1


@_LINUX_ONLY
def test_a_fresh_cache_is_cleaned_too(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_cargo(tmp_path, "clean")) == 1


@_LINUX_ONLY
def test_a_failed_clean_fails_the_run(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", STUB_CLEAN_EXIT="4")
    assert proc.returncode == 4
    assert "real client: build failed: `cargo clean` exited 4" in proc.stdout
    assert _cargo(tmp_path, "build") == [] and not (tmp_path / "argv").exists()


@_LINUX_ONLY
def test_two_builds_into_one_cache_take_turns(tmp_path):
    import fcntl
    target = tmp_path / "target"
    target.mkdir()
    with open(target / ".suite-build.lock", "wb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", popen=True,
                           PSEUDOLIFE_SUITE_CARGO_TARGET_DIR=str(target))
        try:
            _wait_for_line(proc, "another run is building into")
            assert _cargo(tmp_path, "build") == []
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, out + err
    assert len(_cargo(tmp_path, "build")) == 1


@_LINUX_ONLY
def test_a_failed_build_fails_the_run_with_cargos_error_and_no_stand_in(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", STUB_CARGO_FAIL="1")
    assert proc.returncode == 101
    assert "cannot find value `nope`" in proc.stderr
    assert "wsl-suite: real client: build failed (cargo exit 101" in proc.stdout
    assert "not falling back to the stand-in" in proc.stdout
    assert not (tmp_path / "argv").exists()  # pytest never ran


@_LINUX_ONLY
def test_a_client_whose_version_fails_fails_the_run(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", STUB_VERSION_EXIT="3")
    assert proc.returncode == 3
    assert "real client: build failed: the built client's `version` exited 3" in proc.stdout
    assert not (tmp_path / "argv").exists()


@_LINUX_ONLY
def test_the_build_refuses_under_the_8_gb_floor(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", df_kb=8 * 1024 * 1024 - 1)
    assert proc.returncode == 2
    assert "under the 8 GB floor" in proc.stderr and "refusing" in proc.stderr
    assert _read(tmp_path / "cargo-calls") == [] and not (tmp_path / "argv").exists()


@_LINUX_ONLY
def test_the_build_starts_at_the_8_gb_floor(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", df_kb=8 * 1024 * 1024)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_cargo(tmp_path, "build")) == 1


@_LINUX_ONLY
def test_the_switch_needs_cargo(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", cargo=False)
    assert proc.returncode == 2
    assert "needs cargo" in proc.stderr and not (tmp_path / "argv").exists()


@_LINUX_ONLY
@pytest.mark.parametrize("value", ["2", "yes please", "On "])
def test_a_mistyped_switch_is_refused_before_anything_is_installed(tmp_path, value):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT=value)
    assert proc.returncode == 2
    assert "PSEUDOLIFE_SUITE_NATIVE_CLIENT takes 1" in proc.stderr
    assert not (tmp_path / "uv-called").exists()


@_LINUX_ONLY
def test_a_callers_own_client_is_reported_as_given_not_built(tmp_path):
    given = tmp_path / "home" / "my-client"
    proc = _native_run(tmp_path, PSEUDOLIFE_NATIVE_TEST_BINARY=str(given))
    assert proc.returncode == 0, proc.stderr
    assert ("wsl-suite: real client: yes, given by the caller (not built by this run), "
            "at ~/my-client") in proc.stdout
    assert _read(tmp_path / "cargo-calls") == []


def _wait_for_line(proc: subprocess.Popen, text: str, deadline: float = 60) -> None:
    """Read the launcher's stdout until a line contains `text`, failing
    after `deadline` seconds rather than hanging the job (Linux: select)."""
    import selectors
    end = time.monotonic() + deadline
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            left = end - time.monotonic()
            assert left > 0, f"the launcher did not say {text!r} within {deadline}s"
            if not sel.select(timeout=left):
                continue
            line = proc.stdout.readline()
            assert line or proc.poll() is None, f"the launcher ended without saying {text!r}: {proc.stderr.read()}"
            if text in line:
                return


@_LINUX_ONLY
@pytest.mark.parametrize("args", [(), ("tests/test_ci_native_client.py",)])
def test_a_build_waits_for_a_running_full_suite_and_holds_it_off(tmp_path, args):
    """A full or a selected run: a four-job build never overlaps a full suite
    (CLAUDE.md "Sharing the machine")."""
    import fcntl
    locks = tmp_path / "home" / ".pseudolife-mcp" / "locks"
    locks.mkdir(parents=True)
    with open(locks / "full-suite.lock", "wb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        proc = _native_run(tmp_path, *args, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", popen=True)
        try:
            _wait_for_line(proc, "waiting for it before building")
            assert _cargo(tmp_path, "build") == []  # it says so before it blocks
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, out + err
    assert len(_cargo(tmp_path, "build")) == 1


@_LINUX_ONLY
def test_a_run_with_the_suite_lock_off_does_not_wait_for_it(tmp_path):
    import fcntl
    locks = tmp_path / "home" / ".pseudolife-mcp" / "locks"
    locks.mkdir(parents=True)
    with open(locks / "full-suite.lock", "wb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                           PSEUDOLIFE_SUITE_LOCK="off")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "waiting" not in proc.stdout and len(_cargo(tmp_path, "build")) == 1


@_LINUX_ONLY
def test_pytest_never_inherits_the_build_locks(tmp_path):
    # The suite's own conftest takes full-suite.lock; a descriptor left open
    # here would make it wait on itself.
    venv_python = tmp_path / "home" / "v" / "bin" / "python"
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert venv_python.exists()
    record = tmp_path / "fds"
    venv_python.write_text(f"#!/bin/bash\nls -l /proc/$$/fd > '{record}'\n", encoding="utf-8")
    proc = _native_run(tmp_path / "second", PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                       HOME=str(tmp_path / "home"), PSEUDOLIFE_SUITE_VENV=str(tmp_path / "home" / "v"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fds = record.read_text(encoding="utf-8")
    assert "full-suite.lock" not in fds and ".suite-build.lock" not in fds, fds


# ── ops/remote-suite.ps1: the batch default and the result JSON ────────────

PWSH = shutil.which("pwsh")
_STUB_WSL = r'''#!/bin/bash
# wsl.exe as ops/remote-suite.ps1 and ops/wsl-suite.ps1 call it.
case "$*" in
    *wslpath*) printf '%s\n' "${@: -1}" ;;
    *"sh -c"*)  # the suite lock probe: STUB_LOCAL_STATES answers in turn (default free)
        n=$(cat "$STUB_LOCAL_PROBES" 2>/dev/null || echo 0)
        [[ -n "${STUB_LOCAL_PROBES:-}" ]] && echo $((n + 1)) > "$STUB_LOCAL_PROBES"
        IFS=, read -ra states <<< "${STUB_LOCAL_STATES:-0}"
        exit "${states[$(( n < ${#states[@]} ? n : ${#states[@]} - 1 ))]}" ;;
    *wsl-suite.sh*)
        printf 'switch=%s\nwslenv=%s\nunavailable=%s\n' "${PSEUDOLIFE_SUITE_NATIVE_CLIENT:-}" "${WSLENV:-}" \
            "${PSEUDOLIFE_SUITE_PC_UNAVAILABLE:-}" > "$WSL_CALLS"
        if [ "${PSEUDOLIFE_SUITE_NATIVE_CLIENT:-}" = 1 ]; then
            echo "wsl-suite: real client: yes, pseudolife-stdio built from $PSEUDOLIFE_SUITE_COMMIT (dev profile, --locked; pseudolife-mcp 1.2.3), at ~/v/native-client/pseudolife-stdio"
        else
            echo "wsl-suite: real client: no (PSEUDOLIFE_SUITE_NATIVE_CLIENT is off)"
        fi
        echo "3 passed in 0.1s" ;;
esac
'''


def _remote_suite(tmp_path: Path, branch: str, *, origin: bool = False,
                  script: str = "remote-suite.ps1", **env: str | None):
    """ops/remote-suite.ps1 (or `script`), ops/wsl-suite.ps1 and
    ops/suite-availability.ps1, committed in a repository of their own on
    `branch` (pushed to a bare origin when `origin`), with a wsl.exe
    stand-in; returns the run, the result JSON it wrote and the commit."""
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    for name in ("remote-suite.ps1", "wsl-suite.ps1", "suite-availability.ps1", "dev-tmp-space.sh"):
        shutil.copy(ROOT / "ops" / name, repo / "ops" / name)
    for argv in (["init", "-q", "-b", branch], ["add", "-A"],
                 ["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    if origin:
        bare = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        # On origin/master too: a commit the remote clone fetches from origin
        # (one that is not travels as a bundle; the bundle tests below).
        for argv in (["remote", "add", "origin", str(bare)], ["push", "-q", "origin", branch, f"{branch}:master"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "wsl.exe").write_text(_STUB_WSL, encoding="utf-8")
    (stubs / "wsl.exe").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = {"HOME": str(home), "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "WSL_CALLS": str(tmp_path / "wsl-calls"), "PSEUDOLIFE_SUITE_WHERE": "local",
            "PSEUDOLIFE_SUITE_REMOTE": None, "PSEUDOLIFE_WSL_DISTRO": None,
            "PSEUDOLIFE_LEASES_HELD": None, "WSLENV": None,
            "PSEUDOLIFE_TEST_PG_LOGIN_FILE": str(tmp_path / "no-login")}
    base.update(env)
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(repo / "ops" / script)],
                          capture_output=True, text=True, timeout=120, env=hermetic_env(**base))
    results = sorted((home / ".pseudolife-mcp" / "suite-results").glob("*.json"))
    result = json.loads(results[-1].read_text(encoding="utf-8-sig")) if results else None
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    return proc, result, head


_NEEDS_PWSH = pytest.mark.skipif(PWSH is None, reason="needs pwsh")


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_batch_branch_runs_the_real_client_and_records_it(tmp_path):
    proc, result, head = _remote_suite(tmp_path, "integrate/2026-11-16-batch-9")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is an integration batch, so the run builds the native client" in proc.stdout
    switch, wslenv = _read(tmp_path / "wsl-calls")[:2]
    assert switch == "switch=1" and "PSEUDOLIFE_SUITE_NATIVE_CLIENT" in wslenv.split("=", 1)[1].split(":")
    assert result["real_client"] is True and result["real_client_commit"] == head
    assert result["real_client_path"] == "~/v/native-client/pseudolife-stdio"
    assert result["args"] == [] and result["exit_code"] == 0
    assert f"real client: yes, built from {head[:8]}" in proc.stdout.splitlines()[-1]


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_an_ordinary_branch_stays_without_the_real_client_and_says_so(tmp_path):
    proc, result, _ = _remote_suite(tmp_path, "fix/something")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "integration batch" not in proc.stdout
    assert _read(tmp_path / "wsl-calls")[0] == "switch="
    assert result["real_client"] is False and result["real_client_commit"] is None
    assert "real client: no" in proc.stdout.splitlines()[-1]


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
@pytest.mark.parametrize("branch, value, real", [("integrate/2026-11-16-batch-9", "0", False),
                                                 ("fix/something", "1", True)])
def test_the_switch_set_by_hand_wins_over_the_branch(tmp_path, branch, value, real):
    proc, result, _ = _remote_suite(tmp_path, branch, PSEUDOLIFE_SUITE_NATIVE_CLIENT=value)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "integration batch" not in proc.stdout
    assert result["real_client"] is real


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_failed_build_is_recorded_as_no_real_client(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_STUB_WSL", _STUB_WSL.replace(
        'echo "wsl-suite: real client: yes, pseudolife-stdio built from $PSEUDOLIFE_SUITE_COMMIT',
        'echo "wsl-suite: real client: build failed (cargo exit 101; its error is above)"; exit 101; echo "'))
    proc, result, _ = _remote_suite(tmp_path, "integrate/2026-11-16-batch-9")
    assert proc.returncode == 101
    assert result["real_client"] is False and result["exit_code"] == 101


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_callers_own_client_is_not_shown_as_a_gate_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_STUB_WSL", _STUB_WSL.replace(
        "pseudolife-stdio built from $PSEUDOLIFE_SUITE_COMMIT (dev profile, --locked; pseudolife-mcp 1.2.3)",
        "given by the caller (not built by this run)"))
    proc, result, _ = _remote_suite(tmp_path, "integrate/2026-11-16-batch-9")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result["real_client"] is True and result["real_client_commit"] is None
    assert result["real_client_path"] == "~/v/native-client/pseudolife-stdio"
    assert "not built by this run (ops/integrate.py open refuses it)" in proc.stdout.splitlines()[-1]


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_launcher_that_never_says_leaves_real_client_null(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_STUB_WSL",
                        _STUB_WSL.replace("wsl-suite: real client:", "something else:"))
    proc, result, _ = _remote_suite(tmp_path, "integrate/2026-11-16-batch-9")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result["real_client"] is None and result["real_client_commit"] is None
    assert "real client: not reported" in proc.stdout.splitlines()[-1]


# ── which machine runs a full suite: the remote, and this PC only while its
#    owner has said it is available (maintainer ruling, 16 November 2026) ──

def _availability(tmp_path: Path, content: str | None, now: str = "2026-11-16T20:00:00+11:00",
                  unreadable: bool = False) -> dict:
    """ops/suite-availability.ps1's verdict on a pc-available file."""
    home = tmp_path / "home"
    locks = home / ".pseudolife-mcp" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    if content is not None:
        (locks / "pc-available").write_text(content, encoding="utf-8")
        if unreadable:
            (locks / "pc-available").chmod(0)
    script = (f". '{ROOT / 'ops' / 'suite-availability.ps1'}'; "
              f"Get-SuiteAvailability '{home}' ([DateTimeOffset]::Parse('{now}')) | ConvertTo-Json -Compress")
    proc = subprocess.run([PWSH, "-NoProfile", "-Command", script], capture_output=True, text=True,
                          timeout=60, env=hermetic_env(HOME=str(home)))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@_NEEDS_PWSH
@pytest.mark.parametrize("content, available, why", [
    (None, False, "no ~/.pseudolife-mcp/locks/pc-available"),
    ("", True, "until it is removed"),
    ("\n  \n", True, "until it is removed"),
    # Only an empty file opens the PC with no end: a comment, or a
    # commented-out expiry, is not (conductor ruling, 16 November 2026).
    ("# overnight, as the maintainer said\n\n", False, "is not one"),
    ("#until=2026-11-17T07:00+11:00\n", False, "is not one"),
    ("until=2026-11-17T07:00+11:00\n", True, "until 2026-11-17T07:00+11:00"),
    # "now" is 2026-11-16T20:00+11:00, 09:00Z: a second later is still open.
    ("until=2026-11-16T09:00:01Z", True, "until 2026-11-16T09:00:01Z"),
    # Fail closed: expired (the moment itself included), no offset, garbage,
    # two settings, a time that does not exist.
    ("until=2026-11-16T19:59+11:00\n", False, "expired at 2026-11-16T19:59+11:00"),
    ("until=2026-11-16T09:00:00Z\n", False, "expired at 2026-11-16T09:00:00Z"),
    ("until=2026-11-17T07:00\n", False, "is not one"),
    ("yes please\n", False, "is not one"),
    ("until=2026-11-17T07:00+11:00\nuntil=2026-11-18T07:00+11:00\n", False, "is not one"),
    ("until=2026-13-45T07:00+11:00\n", False, "does not exist"),
])
def test_the_availability_file_is_read_fail_closed(tmp_path, content, available, why):
    verdict = _availability(tmp_path, content)
    assert verdict["Available"] is available
    assert why in verdict["Why"]


@_LINUX_ONLY
@_NEEDS_PWSH
def test_an_unreadable_availability_file_reads_as_not_available(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    verdict = _availability(tmp_path, "", unreadable=True)
    assert verdict["Available"] is False and "cannot be read" in verdict["Why"]


_STUB_SSH = r'''#!/bin/bash
# ssh as ops/remote-suite.ps1 calls it: the suite-lock probe, or the run.
if [[ "${@: -1}" == *full-suite.lock* ]]; then
    n=$(cat "$STUB_SSH_PROBES" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_SSH_PROBES"
    # STUB_REMOVE_AT=N: this, the Nth probe, removes STUB_REMOVE_FILE.
    if [[ "${STUB_REMOVE_AT:-}" == "$((n + 1))" ]]; then rm -f -- "$STUB_REMOVE_FILE"; fi
    # STUB_MOVE_AT=N: this probe commits in STUB_MOVE_REPO, as a checkout
    # that moves while the run waits.
    if [[ "${STUB_MOVE_AT:-}" == "$((n + 1))" ]]; then
        git -C "$STUB_MOVE_REPO" -c user.email=t@example.com -c user.name=t commit -q --allow-empty -m moved
    fi
    IFS=, read -ra states <<< "${STUB_REMOTE_STATES:-0}"
    i=$(( n < ${#states[@]} ? n : ${#states[@]} - 1 ))
    exit "${states[$i]}"
fi
cat > /dev/null
echo ran >> "$SSH_CALLS"
echo "wsl-suite: real client: no (PSEUDOLIFE_SUITE_NATIVE_CLIENT is off)"
echo "3 passed in 0.1s"
'''


def _dispatch(tmp_path: Path, *, remote_states: str | None = "0", pc_available: str | None = None,
              pushed: bool = True, **env: str | None):
    """ops/remote-suite.ps1 in a repository of its own, with stand-ins for
    ssh (STUB_REMOTE_STATES: the remote's probe exits in turn, 0 free, 1
    held, else a failed probe; None configures no remote) and wsl.exe."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(parents=True, exist_ok=True)
    (stubs / "ssh").write_text(_STUB_SSH, encoding="utf-8")
    (stubs / "ssh").chmod(0o755)
    home = tmp_path / "home"
    if pc_available is not None:
        locks = home / ".pseudolife-mcp" / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "pc-available").write_text(pc_available, encoding="utf-8")
    extra = {"PSEUDOLIFE_SUITE_REMOTE": "box" if remote_states is not None else None,
             "STUB_REMOTE_STATES": remote_states, "STUB_SSH_PROBES": str(tmp_path / "probes"),
             "SSH_CALLS": str(tmp_path / "ssh-calls"), "PSEUDOLIFE_SUITE_WHERE": None,
             "PSEUDOLIFE_SUITE_POLL_SECONDS": "0"}
    extra.update(env)
    return _remote_suite(tmp_path, "fix/something", origin=pushed, **extra)


def _ran_on(tmp_path: Path) -> list[str]:
    return (["remote"] if _read(tmp_path / "ssh-calls") else []) + \
           (["local"] if (tmp_path / "wsl-calls").exists() else [])


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_with_a_remote_and_no_availability_only_the_remote_runs_it(tmp_path):
    proc, result, _ = _dispatch(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["remote"] and result["machine"] == "remote"
    lines = proc.stdout.splitlines()
    assert any("only the remote machine is a candidate: this PC is not available for full suites "
               "(no ~/.pseudolife-mcp/locks/pc-available" in ln for ln in lines), lines
    assert "remote-suite: running on the remote machine: its suite lock is free" in lines


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_held_remote_is_waited_for_while_this_pc_is_not_available(tmp_path):
    proc, result, _ = _dispatch(tmp_path, remote_states="1,1,0")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["remote"]
    assert ("waiting for the remote machine, which is running a full suite; this PC is not "
            "available for full suites") in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
@pytest.mark.parametrize("pc_available", ["", "until=2999-01-01T00:00+11:00\n"])
def test_with_availability_the_remote_comes_first_and_this_pc_takes_the_overflow(tmp_path, pc_available):
    proc, _, _ = _dispatch(tmp_path / "free", pc_available=pc_available)
    assert proc.returncode == 0 and _ran_on(tmp_path / "free") == ["remote"], proc.stdout

    proc, result, _ = _dispatch(tmp_path / "held", remote_states="1", pc_available=pc_available)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path / "held") == ["local"] and result["machine"] == "local-wsl"
    assert ("running on this PC (local WSL): the remote machine is running a full suite and this PC "
            "is available") in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_availability_withdrawn_while_waiting_takes_this_pc_out(tmp_path):
    # Both held at first; the file goes during the wait, and local frees on
    # the next probe while the remote is still held: without the re-read the
    # run would take this PC (both reviews of #1634).
    pc_file = tmp_path / "home" / ".pseudolife-mcp" / "locks" / "pc-available"
    proc, _, _ = _dispatch(tmp_path, remote_states="1,1,0", pc_available="",
                           STUB_LOCAL_STATES="1,0", STUB_LOCAL_PROBES=str(tmp_path / "local-probes"),
                           STUB_REMOVE_AT="1", STUB_REMOVE_FILE=str(pc_file))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["remote"]
    assert "this PC is no longer available for full suites (no ~/.pseudolife-mcp/locks/pc-available" in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_an_expired_availability_is_no_availability(tmp_path):
    proc, _, _ = _dispatch(tmp_path, remote_states="1,0", pc_available="until=2020-01-01T00:00Z\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["remote"]
    assert "expired at 2020-01-01T00:00Z" in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_remote_that_cannot_be_probed_is_a_refusal_without_availability(tmp_path):
    proc, result, _ = _dispatch(tmp_path, remote_states="255")
    assert proc.returncode == 2
    assert "cannot probe the remote machine's suite lock (error (exit 255))" in proc.stderr
    assert "this PC is not available for full suites" in proc.stderr
    assert _ran_on(tmp_path) == [] and result is None


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_remote_that_cannot_be_probed_falls_to_an_available_pc(tmp_path):
    proc, _, _ = _dispatch(tmp_path, remote_states="255", pc_available="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["local"]
    assert "the remote machine could not be probed (error (exit 255)) and this PC is available" in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_forcing_local_overrides_the_rule_and_says_so(tmp_path):
    proc, result, _ = _dispatch(tmp_path, PSEUDOLIFE_SUITE_WHERE="local")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["local"]
    assert proc.stdout.splitlines()[0].startswith(
        "remote-suite: PSEUDOLIFE_SUITE_WHERE=local overrides the availability rule")
    assert "running on this PC (local WSL): forced by PSEUDOLIFE_SUITE_WHERE=local" in proc.stdout


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_with_no_remote_configured_this_pc_runs_it_as_before(tmp_path):
    proc, result, _ = _dispatch(tmp_path, remote_states=None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _ran_on(tmp_path) == ["local"] and "availability" not in proc.stdout
    assert "running on this PC (local WSL): no remote machine is configured" in proc.stdout


# ops/wsl-suite.ps1 started directly: it sends the reason across, and
# ops/wsl-suite.sh refuses a full run with it (one classifier: plan_run).

def _direct_wsl(tmp_path: Path, **env: str | None) -> tuple[subprocess.CompletedProcess, str]:
    """ops/wsl-suite.ps1 run directly, and the wsl.exe stand-in's record."""
    env.setdefault("PSEUDOLIFE_SUITE_WHERE", None)  # _remote_suite's default forces local
    proc, _, _ = _remote_suite(tmp_path, "fix/something", script="wsl-suite.ps1", **env)
    return proc, "\n".join(_read(tmp_path / "wsl-calls"))


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
@pytest.mark.parametrize("env, sent", [
    ({"PSEUDOLIFE_SUITE_REMOTE": "box"}, True),
    ({"PSEUDOLIFE_SUITE_REMOTE": "box", "PC_AVAILABLE": ""}, False),
    ({"PSEUDOLIFE_SUITE_REMOTE": "box", "PSEUDOLIFE_SUITE_CHOSEN": "local"}, False),
    ({"PSEUDOLIFE_SUITE_REMOTE": "box", "PSEUDOLIFE_SUITE_WHERE": "local"}, False),
    ({"PSEUDOLIFE_SUITE_REMOTE": None}, False),
])
def test_a_direct_run_sends_the_reason_only_when_the_rule_applies(tmp_path, env, sent):
    env = dict(env)
    if "PC_AVAILABLE" in env:
        locks = tmp_path / "home" / ".pseudolife-mcp" / "locks"
        locks.mkdir(parents=True)
        (locks / "pc-available").write_text(env.pop("PC_AVAILABLE"), encoding="utf-8")
    proc, call = _direct_wsl(tmp_path, **env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("unavailable=no ~/.pseudolife-mcp/locks/pc-available" in call) is sent, call
    wslenv = next(line for line in call.splitlines() if line.startswith("wslenv="))
    assert ("PSEUDOLIFE_SUITE_PC_UNAVAILABLE" in wslenv.split("=", 1)[1].split(":")) is sent, call
    assert ("overrides the availability rule" in proc.stdout) is (env.get("PSEUDOLIFE_SUITE_WHERE") == "local")
    # The dispatcher's mark is never silent, since a hand-set one skips the rule.
    assert ("PSEUDOLIFE_SUITE_CHOSEN=local: ops/remote-suite.ps1 chose this PC" in proc.stdout) is \
        (env.get("PSEUDOLIFE_SUITE_CHOSEN") == "local" and env.get("PSEUDOLIFE_SUITE_REMOTE") is not None)


@_LINUX_ONLY
def test_wsl_suite_sh_refuses_a_full_run_while_this_pc_is_not_available(tmp_path):
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_PC_UNAVAILABLE="no ~/.pseudolife-mcp/locks/pc-available")
    assert proc.returncode == 2
    assert "a full suite runs on the remote machine while this PC is not available" in proc.stderr
    assert "$env:PSEUDOLIFE_SUITE_WHERE='remote'; pwsh -NoProfile -File ops/remote-suite.ps1" in proc.stderr
    assert not (tmp_path / "uv-called").exists() and not (tmp_path / "argv").exists()


@_LINUX_ONLY
@pytest.mark.parametrize("args", [("tests/test_ci_native_client.py",), ("tests/",), ("-x",),
                                  ("-k", "not slow")])
def test_a_selection_goes_to_pytest_with_the_marker_for_conftest_to_classify(tmp_path, args):
    # `tests/`, `-x` and an exclude-only `-k` are the whole suite too, which
    # plan_run cannot tell; tests/conftest.py refuses those with the lock's
    # own classifier (test_suite_lock.py), a targeted one goes ahead.
    venv_python = tmp_path / "home" / "v" / "bin" / "python"
    record = tmp_path / "env"
    proc = _native_run(tmp_path, *args,
                       PSEUDOLIFE_SUITE_PC_UNAVAILABLE="no ~/.pseudolife-mcp/locks/pc-available")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _read(tmp_path / "argv")[:2 + len(args)] == ["-m", "pytest", *args]
    venv_python.write_text(f"#!/bin/bash\nprintf '%s' \"${{PSEUDOLIFE_SUITE_PC_UNAVAILABLE-unset}}\" > '{record}'\n",
                           encoding="utf-8")
    proc = _native_run(tmp_path / "again", *args, HOME=str(tmp_path / "home"),
                       PSEUDOLIFE_SUITE_VENV=str(tmp_path / "home" / "v"),
                       PSEUDOLIFE_SUITE_PC_UNAVAILABLE="no ~/.pseudolife-mcp/locks/pc-available")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert record.read_text(encoding="utf-8") == "no ~/.pseudolife-mcp/locks/pc-available"


# The remote machine fetches a pushed commit from origin; one that is not on
# origin travels inside the dispatch as a bundle, and nothing is pushed.
# Here ssh runs the dispatch body for real against a local "remote" clone;
# only its last step (sudo systemd-run ... wsl-suite.sh) is a stand-in.

_STUB_SSH_RUNS_BODY = r'''#!/bin/bash
if [[ "${@: -1}" == *full-suite.lock* ]]; then
    # MOVE_REPO: the first probe moves that checkout to another commit and
    # finds the remote busy, as a checkout switched while the run waits.
    if [[ -n "${MOVE_REPO:-}" && ! -e "$MOVE_REPO.moved" ]]; then
        touch "$MOVE_REPO.moved"
        git -C "$MOVE_REPO" switch -q -c elsewhere master
        echo other > "$MOVE_REPO/other.txt"
        git -C "$MOVE_REPO" add other.txt
        git -C "$MOVE_REPO" -c user.email=t@example.com -c user.name=t commit -q -m other
        exit 1
    fi
    exit 0
fi
HOME="$REMOTE_HOME" PATH="$FAKE_BIN:$PATH" PSEUDOLIFE_TEST_PG_HOST_PORT=127.0.0.1:5999 bash -c "${@: -1}"
'''
_FAKE_SUDO = r'''#!/bin/bash
# The dispatch's last step: report what the remote clone now holds.
repo="${PSEUDOLIFE_SUITE_GIT_COMMON%/.git}"
got="$(git -C "$repo" rev-parse --verify --quiet "refs/heads/suite/$PSEUDOLIFE_SUITE_COMMIT" || echo missing)"
echo "ran $PSEUDOLIFE_SUITE_COMMIT $got" >> "$SSH_CALLS"
# The dispatch's temporary copy of wsl-suite.sh, which the real step deletes.
for arg in "$@"; do [[ "$prev" == _ ]] && rm -f -- "$arg"; prev="$arg"; done
echo "wsl-suite: real client: no (PSEUDOLIFE_SUITE_NATIVE_CLIENT is off)"
echo "3 passed in 0.1s"
'''


def _bundle_world(tmp_path: Path, *, push_head: bool, move_head: bool = False, to_master: bool = False):
    repo, bare = tmp_path / "repo", tmp_path / "origin.git"
    (repo / "ops").mkdir(parents=True)
    for name in ("remote-suite.ps1", "wsl-suite.ps1", "suite-availability.ps1", "wsl-suite.sh", "dev-tmp-space.sh"):
        shutil.copy(ROOT / "ops" / name, repo / "ops" / name)
    def git(cwd, *argv):
        return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.com",
                               "-c", "user.name=t", *argv],
                              check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare)], check=True)
    git(tmp_path, "init", "-q", "-b", "master", str(repo))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "master")
    git(repo, "switch", "-q", "-c", "integrate/2026-11-16-batch-9")
    (repo / "batch.txt").write_text("the batch's own change\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "batch merge")
    head = git(repo, "rev-parse", "HEAD")
    if push_head:
        git(repo, "push", "-q", "origin", "HEAD:master" if to_master else "HEAD")
    remote_home = tmp_path / "remote-home"
    (remote_home / "projects").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(remote_home / "projects" / "Pseudolife-MCP")],
                   check=True, capture_output=True)
    stubs, fake_bin = tmp_path / "stubs", tmp_path / "fake-bin"
    stubs.mkdir()
    fake_bin.mkdir()
    for path, body in ((stubs / "ssh", _STUB_SSH_RUNS_BODY), (stubs / "wsl.exe", _STUB_WSL),
                       (fake_bin / "sudo", _FAKE_SUDO)):
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
           "REMOTE_HOME": str(remote_home), "FAKE_BIN": str(fake_bin),
           "SSH_CALLS": str(tmp_path / "ssh-calls"), "WSL_CALLS": str(tmp_path / "wsl-calls"),
           "PSEUDOLIFE_SUITE_REMOTE": "box", "PSEUDOLIFE_SUITE_WHERE": "remote",
           "PSEUDOLIFE_SUITE_NATIVE_CLIENT": None, "PSEUDOLIFE_MCP_TOKEN": None,
           "PSEUDOLIFE_WSL_DISTRO": None, "WSLENV": None, "PSEUDOLIFE_SUITE_POLL_SECONDS": "0",
           "MOVE_REPO": str(repo) if move_head else None}
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(repo / "ops" / "remote-suite.ps1")],
                          capture_output=True, text=True, timeout=120, env=hermetic_env(**env))
    on_origin = subprocess.run(["git", "-C", str(bare), "cat-file", "-e", f"{head}^{{commit}}"],
                               capture_output=True).returncode == 0
    return proc, head, on_origin


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
@pytest.mark.parametrize("push_head, to_master", [(False, False), (True, False), (True, True)])
def test_the_remote_machine_gets_the_commit_and_an_unpushed_one_is_never_pushed(tmp_path, push_head, to_master):
    # Unpushed and a pushed branch travel as a bundle (nothing rests on
    # remote-tracking refs); a commit on origin/master is fetched from origin.
    proc, head, on_origin = _bundle_world(tmp_path, push_head=push_head, to_master=to_master)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _read(tmp_path / "ssh-calls") == [f"ran {head} {head}"]
    assert on_origin is push_head          # a bundle pushes nothing
    assert ("travels as a bundle" in proc.stdout) is (not to_master)


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_the_bundle_carries_the_commit_chosen_even_if_the_checkout_moved_while_waiting(tmp_path):
    proc, head, on_origin = _bundle_world(tmp_path, push_head=False, move_head=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _read(tmp_path / "ssh-calls") == [f"ran {head} {head}"]
    assert not on_origin
    # The private ref that named it for the bundle is gone again.
    refs = subprocess.run(["git", "-C", str(tmp_path / "repo"), "for-each-ref", "refs/pseudolife-suite-bundle/"],
                          capture_output=True, text=True, check=True).stdout
    assert refs == ""


# ── review of #1652 (fix round) ────────────────────────────────────────────

@_LINUX_ONLY
def test_cargo_and_its_children_hold_none_of_the_runs_locks(tmp_path):
    # A RUSTC_WRAPPER daemon that inherited full-suite.lock would hold off
    # every later suite on the machine; cargo gets no lock descriptor.
    fds = tmp_path / "cargo-fds"
    proc = _native_run(tmp_path, PSEUDOLIFE_SUITE_NATIVE_CLIENT="1", CARGO_FDS=str(fds))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {c.split("|")[2].split(" ")[0] for c in _read(tmp_path / "cargo-calls")} == {"metadata", "clean", "build"}
    seen = fds.read_text(encoding="utf-8")
    assert seen.count("total") == 3, seen   # one listing per cargo call
    assert "full-suite.lock" not in seen and ".suite-build.lock" not in seen, seen


@_LINUX_ONLY
def test_no_build_for_a_selection_conftest_may_still_refuse(tmp_path):
    proc = _native_run(tmp_path, "tests/", PSEUDOLIFE_SUITE_NATIVE_CLIENT="1",
                       PSEUDOLIFE_SUITE_PC_UNAVAILABLE="no ~/.pseudolife-mcp/locks/pc-available")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _read(tmp_path / "cargo-calls") == []
    assert ("wsl-suite: real client: no (not built: this PC is not available for full suites, so "
            "tests/conftest.py first decides") in proc.stdout
    assert _read(tmp_path / "client-env")[:2] == ["binary=", "required="]


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_the_result_takes_the_launchers_own_line_not_one_quoted_later(tmp_path, monkeypatch):
    # A failing test's captured output may quote a "real client: yes" line.
    monkeypatch.setattr(sys.modules[__name__], "_STUB_WSL", _STUB_WSL.replace(
        'echo "3 passed in 0.1s" ;;',
        'echo "wsl-suite: real client: yes, pseudolife-stdio built from $(printf %040d 0) (quoted), at ~/x"; '
        'echo "3 passed in 0.1s" ;;'))
    proc, result, _ = _remote_suite(tmp_path, "fix/something")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result["real_client"] is False and result["real_client_commit"] is None


@_LINUX_ONLY
@_NEEDS_PWSH
@_NEEDS_GIT
def test_a_local_run_refuses_a_checkout_that_moved_while_it_waited(tmp_path):
    # Local WSL tests HEAD while the result records the commit chosen
    # first; the remote run carries that commit itself (the bundle test).
    proc, result, head = _dispatch(tmp_path, remote_states="1", pc_available="",
                                   STUB_MOVE_AT="1", STUB_MOVE_REPO=str(tmp_path / "repo"))
    assert proc.returncode == 2
    # `head` is read after the run: where the checkout moved to.
    assert re.search(rf"the checkout moved from [0-9a-f]{{8}} to {head[:8]} while this run waited", proc.stderr)
    assert _ran_on(tmp_path) == [] and result is None
