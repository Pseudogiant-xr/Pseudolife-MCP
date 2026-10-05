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
import os
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
