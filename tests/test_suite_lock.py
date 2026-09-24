"""The machine-wide full-suite lock (tests/suite_lock.py) and its conftest wiring.

Every lock these tests take lives under a ``tmp_path`` directory
(``PSEUDOLIFE_SUITE_LOCK_DIR``), never the real
``~/.pseudolife-mcp/locks`` — a full suite running this file holds the real
lock for its whole lifetime, and a child that queued behind its own parent
would wait forever.
"""

from __future__ import annotations

import errno
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import suite_lock

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Child processes can be slow to start on a loaded Windows host; these bound
# a hang, they are not performance expectations.
START_TIMEOUT = 60.0
PYTEST_TIMEOUT = 240.0

# A lock holder/waiter as its own process: acquire, report, then hold until
# a line arrives on stdin (clean release) or the process is killed (crash).
_LOCK_PROCESS = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests import suite_lock
directory = Path(sys.argv[2])
held = suite_lock.acquire(directory, "wait", worktree=sys.argv[3],
                          poll=0.05, notice_every=0.2, out=sys.stdout)
print("ACQUIRED", os.getpid(), flush=True)
sys.stdin.readline()
suite_lock.release(held)
print("RELEASED", flush=True)
"""


class _Proc:
    """A child process whose merged output is read line by line."""

    def __init__(self, args: list[str], env: dict[str, str] | None = None):
        self.proc = subprocess.Popen(
            args, cwd=ROOT, env=env, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.seen: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def expect(self, marker: str, timeout: float = START_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                raise AssertionError(
                    f"exited before {marker!r}; output: {self.seen}")
            self.seen.append(line)
            if marker in line:
                return line
        raise AssertionError(f"no {marker!r} within {timeout}s; output: {self.seen}")

    def drain(self, timeout: float = PYTEST_TIMEOUT) -> int:
        code = self.proc.wait(timeout=timeout)
        while (line := self._lines.get(timeout=START_TIMEOUT)) is not None:
            self.seen.append(line)
        return code

    def send_release(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write("\n")
        self.proc.stdin.flush()

    def stop(self) -> None:
        # EOF on stdin first, so a lock process releases and exits on its
        # own; kill only what does not.
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait(timeout=START_TIMEOUT)


def _kill_pid(pid: int) -> None:
    """Hard-kill the interpreter that holds the lock, by its own pid: under
    a Windows venv, Popen.pid is the launcher in front of it."""
    os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)


@pytest.fixture
def procs():
    started: list[_Proc] = []

    def start(args: list[str], env: dict[str, str] | None = None) -> _Proc:
        proc = _Proc(args, env)
        started.append(proc)
        return proc

    yield start
    for proc in started:
        proc.stop()


def _lock_process(procs, directory: Path, worktree: str) -> _Proc:
    return procs([sys.executable, "-c", _LOCK_PROCESS, str(ROOT),
                  str(directory), worktree])


@pytest.fixture
def held(tmp_path, procs):
    """A lock directory whose lock another process holds."""
    holder = _lock_process(procs, tmp_path, "holder-wt")
    pid = int(holder.expect("ACQUIRED").split()[1])
    return SimpleNamespace(dir=tmp_path, pid=pid, holder=holder)


def _config(args, *, cwd: Path = ROOT, worker: bool = False, **options):
    option = SimpleNamespace(
        keyword="", markexpr="", collectonly=False, markers=False,
        showfixtures=False, show_fixtures_per_test=False)
    for name, value in options.items():
        setattr(option, name, value)
    config = SimpleNamespace(
        args=list(args), option=option,
        invocation_params=SimpleNamespace(dir=cwd))
    if worker:
        config.workerinput = {"workerid": "gw0"}
    return config


# --- which runs are full ----------------------------------------------------

@pytest.mark.parametrize(("args", "cwd"), [
    (["tests"], ROOT),                 # `pytest` from the root (testpaths)
    (["tests/"], ROOT),
    ([str(TESTS)], ROOT),
    (["."], ROOT),                     # an ancestor of tests/ covers it too
    ([str(TESTS)], TESTS),             # `pytest` from inside tests/
    (["tests/test_bm25.py", "tests"], ROOT),
])
def test_a_run_over_the_whole_tests_tree_is_full(args, cwd):
    assert suite_lock.is_full_run(args, cwd, TESTS)


@pytest.mark.parametrize("args", [
    ["tests/test_bm25.py"],
    ["tests/test_bm25.py::test_idf_prefers_rare_terms"],
    ["tests/test_bm25.py", "tests/test_graph.py"],
    [str(ROOT / "evals")],
])
def test_a_run_over_named_files_is_targeted(args):
    assert not suite_lock.is_full_run(args, ROOT, TESTS)


@pytest.mark.parametrize("narrowing", [
    {"keyword": "graph"},
    {"keyword": "graph and not slow"},
    {"keyword": "not slow and graph"},   # starts with "not", still narrows
    {"keyword": "nothing_shadowed"},     # "not" only as a keyword
    {"keyword": "not-slow"},             # one identifier in pytest's grammar
    {"keyword": "NOT slow"},             # pytest rejects it before any test
    {"markexpr": "slow"},
    {"keyword": "not graph", "markexpr": "real_model"},
    {"listing_only": True},
])
def test_selection_and_listing_runs_over_the_tree_are_targeted(narrowing):
    assert not suite_lock.is_full_run(["tests"], ROOT, TESTS, **narrowing)


@pytest.mark.parametrize("exclusion", [
    {"keyword": "not graph"},
    {"keyword": " not (graph or bm25)"},
    {"keyword": "(not graph)"},
    {"keyword": "graph or not bm25"},    # keeps everything that is not bm25
    {"markexpr": "not slow"},
])
def test_an_exclusion_only_selection_is_still_full(exclusion):
    assert suite_lock.is_full_run(["tests"], ROOT, TESTS, **exclusion)


def test_naming_most_test_files_is_full():
    # `pytest tests/test_*.py` reaches pytest as every file, one by one.
    files = sorted(str(path) for path in TESTS.glob("test_*.py"))
    half = files[: (len(files) + 1) // 2]
    assert suite_lock.is_full_run(files, ROOT, TESTS)
    assert suite_lock.is_full_run(half, ROOT, TESTS)
    assert not suite_lock.is_full_run(half[:-1], ROOT, TESTS)
    # Only real test modules count: not conftest.py, a helper, or a typo.
    padded = [*half[:-1], str(TESTS / "conftest.py"), str(TESTS / "suite_lock.py"),
              str(TESTS / "test_no_such_module.py")]
    assert not suite_lock.is_full_run(padded, ROOT, TESTS)


# --- mode -------------------------------------------------------------------

def test_mode_waits_locally_and_is_off_on_github_actions():
    assert suite_lock.lock_mode({}) == "wait"
    assert suite_lock.lock_mode({"GITHUB_ACTIONS": "true"}) == "off"
    # A generic CI flag is not enough: agent harnesses export CI=true too,
    # and those are exactly the sessions the lock exists for.
    assert suite_lock.lock_mode({"CI": "true"}) == "wait"


def test_an_explicit_mode_wins_everywhere_and_a_typo_is_refused():
    gha = {"GITHUB_ACTIONS": "true"}
    assert suite_lock.lock_mode({**gha, "PSEUDOLIFE_SUITE_LOCK": "wait"}) == "wait"
    assert suite_lock.lock_mode({"PSEUDOLIFE_SUITE_LOCK": " FAIL "}) == "fail"
    assert suite_lock.lock_mode({"PSEUDOLIFE_SUITE_LOCK": "off"}) == "off"
    with pytest.raises(ValueError, match="PSEUDOLIFE_SUITE_LOCK"):
        suite_lock.lock_mode({"PSEUDOLIFE_SUITE_LOCK": "sometimes"})


def test_the_lock_lives_in_the_home_directory_unless_redirected(tmp_path):
    assert suite_lock.lock_dir({}) == Path.home() / ".pseudolife-mcp" / "locks"
    assert suite_lock.lock_dir({"PSEUDOLIFE_SUITE_LOCK_DIR": str(tmp_path)}) == tmp_path


# --- CUDA -------------------------------------------------------------------

def test_hide_cuda_sets_minus_one_and_yields_only_to_the_opt_in():
    environ = {"CUDA_VISIBLE_DEVICES": "0"}
    assert suite_lock.hide_cuda(environ) is True
    assert environ["CUDA_VISIBLE_DEVICES"] == "-1"

    opted_in = {"PSEUDOLIFE_TEST_CUDA": "1", "CUDA_VISIBLE_DEVICES": "0"}
    assert suite_lock.hide_cuda(opted_in) is False
    assert opted_in["CUDA_VISIBLE_DEVICES"] == "0"
    assert suite_lock.hide_cuda({"PSEUDOLIFE_TEST_CUDA": "1"}) is False


@pytest.mark.skipif(os.environ.get("PSEUDOLIFE_TEST_CUDA") == "1",
                    reason="CUDA opted in for this run")
def test_this_test_session_sees_no_gpu():
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == "-1"
    torch = pytest.importorskip("torch")
    # is_available(), the call the embedder picks its device with, asks the
    # driver; device_count() only parses the variable, so it reads 0 even
    # for an empty value that leaves the GPU usable.
    assert not torch.cuda.is_available()


# --- the session glue, in process -------------------------------------------

def test_a_full_run_takes_the_lock_and_records_its_holder(tmp_path):
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(tmp_path)}
    held_lock = suite_lock.take_for_session(_config(["tests"]), environ, TESTS)
    try:
        assert held_lock is not None
        record = suite_lock.read_holder(tmp_path)
        assert record["pid"] == os.getpid()
        assert record["worktree"] == str(ROOT)
        assert "started" in record
        with pytest.raises(suite_lock.SuiteLockBusy):
            suite_lock.acquire(tmp_path, "fail", worktree="second")
    finally:
        suite_lock.release(held_lock)
    assert suite_lock.read_holder(tmp_path) is None
    suite_lock.release(suite_lock.acquire(tmp_path, "fail", worktree="after"))


def test_fail_mode_refuses_and_names_the_holder(held):
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(held.dir),
               "PSEUDOLIFE_SUITE_LOCK": "fail"}
    with pytest.raises(pytest.UsageError,
                       match=rf"holder-wt \(pid {held.pid}\) since \d\d:\d\d"):
        suite_lock.take_for_session(_config(["tests"]), environ, TESTS)


def test_off_bypasses_a_held_lock(held):
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(held.dir),
               "PSEUDOLIFE_SUITE_LOCK": "off"}
    assert suite_lock.take_for_session(_config(["tests"]), environ, TESTS) is None


def test_a_targeted_run_ignores_a_held_lock(held):
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(held.dir),
               "PSEUDOLIFE_SUITE_LOCK": "fail"}
    for config in (_config(["tests/test_bm25.py"]),
                   _config(["tests"], keyword="graph"),
                   _config(["tests"], collectonly=True)):
        assert suite_lock.take_for_session(config, environ, TESTS) is None


def test_an_xdist_worker_never_takes_the_lock(held):
    # The controller holds it for the whole run; a worker that queued behind
    # its own controller would deadlock the run.
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(held.dir),
               "PSEUDOLIFE_SUITE_LOCK": "fail"}
    worker = _config(["tests"], worker=True)
    assert suite_lock.take_for_session(worker, environ, TESTS) is None


def _lock_backend_raises(monkeypatch, code: int) -> None:
    def refuse(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    if os.name == "nt":
        monkeypatch.setattr(suite_lock.msvcrt, "locking", refuse)
    else:
        monkeypatch.setattr(suite_lock.fcntl, "flock", refuse)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EAGAIN])
def test_contention_errors_read_as_busy(tmp_path, monkeypatch, code):
    _lock_backend_raises(monkeypatch, code)
    with pytest.raises(suite_lock.SuiteLockBusy):
        suite_lock.acquire(tmp_path, "fail", worktree="w")


def test_any_other_lock_error_is_raised_instead_of_waited_on(tmp_path, monkeypatch):
    # Read as "busy", a filesystem without locks would queue the run forever
    # behind a holder that does not exist. (Fail mode, so a regression fails
    # the test rather than hanging it.)
    _lock_backend_raises(monkeypatch, errno.ENOLCK)
    with pytest.raises(OSError) as raised:
        suite_lock.acquire(tmp_path, "fail", worktree="w")
    assert raised.value.errno == errno.ENOLCK
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(tmp_path),
               "PSEUDOLIFE_SUITE_LOCK": "fail"}
    with pytest.raises(pytest.UsageError, match="PSEUDOLIFE_SUITE_LOCK=off skips it"):
        suite_lock.take_for_session(_config(["tests"]), environ, TESTS)


def test_a_bad_mode_is_a_usage_error_even_for_a_targeted_run(tmp_path):
    environ = {"PSEUDOLIFE_SUITE_LOCK_DIR": str(tmp_path),
               "PSEUDOLIFE_SUITE_LOCK": "sometimes"}
    with pytest.raises(pytest.UsageError, match="PSEUDOLIFE_SUITE_LOCK"):
        suite_lock.take_for_session(_config(["tests/test_bm25.py"]), environ, TESTS)


# --- two processes ----------------------------------------------------------

def test_a_second_process_waits_then_takes_the_lock_on_release(held, procs):
    waiter = _lock_process(procs, held.dir, "waiter-wt")
    notice = waiter.expect("waiting for the full-suite lock")
    assert f"held by holder-wt (pid {held.pid}) since" in notice
    waiter.expect("waiting for the full-suite lock")   # repeats while held

    held.holder.send_release()
    held.holder.expect("RELEASED")
    waiter_pid = int(waiter.expect("ACQUIRED").split()[1])
    assert suite_lock.read_holder(held.dir)["pid"] == waiter_pid


def test_the_lock_is_released_when_its_holder_crashes(held, procs):
    waiter = _lock_process(procs, held.dir, "waiter-wt")
    waiter.expect("waiting for the full-suite lock")

    _kill_pid(held.pid)                         # no release, no cleanup
    waiter_pid = int(waiter.expect("ACQUIRED").split()[1])
    # The crashed holder's record was stale; the new holder replaced it.
    assert suite_lock.read_holder(held.dir)["pid"] == waiter_pid


# --- the conftest wiring, end to end ----------------------------------------

def _pytest_env(directory: Path, mode: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PSEUDOLIFE_SUITE_LOCK_DIR"] = str(directory)
    env["PSEUDOLIFE_SUITE_LOCK"] = mode
    # These runs never reach a PG test; don't provision a database for them.
    env.pop("PSEUDOLIFE_REQUIRE_TEST_POSTGRES", None)
    return env


def _full_run_collecting_nothing(workers: int = 0) -> list[str]:
    # Paths cover tests/, so the run is full, but every entry is ignored:
    # the lock is taken (or refused) and then nothing is collected (exit 5).
    xdist = ["-n", str(workers)] if workers else ["-p", "no:xdist"]
    return [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
            *xdist, f"--ignore-glob={TESTS / '*'}"]


def test_a_full_pytest_run_refuses_to_queue_in_fail_mode(held, procs):
    run = procs(_full_run_collecting_nothing(), _pytest_env(held.dir, "fail"))
    assert run.drain() == pytest.ExitCode.USAGE_ERROR, run.seen
    assert any(f"holder-wt (pid {held.pid})" in line for line in run.seen), run.seen


def test_a_full_pytest_run_waits_for_the_holder_then_runs(held, procs):
    run = procs(_full_run_collecting_nothing(), _pytest_env(held.dir, "wait"))
    run.expect(f"waiting for the full-suite lock held by holder-wt (pid {held.pid})")
    held.holder.send_release()
    assert run.drain() == pytest.ExitCode.NO_TESTS_COLLECTED, run.seen
    assert any("full-suite lock acquired after waiting" in line for line in run.seen)
    # pytest_unconfigure released it explicitly: exit alone would free the
    # lock but leave this run's record behind, describing a dead holder.
    assert suite_lock.read_holder(held.dir) is None


def test_an_xdist_run_takes_the_lock_once_in_its_controller(tmp_path, procs):
    # Fail mode, lock free: the controller takes it, so a worker that tried
    # as well would find it held and error out instead of queueing forever.
    # One worker proves the worker path; each one costs a torch import.
    pytest.importorskip("xdist")
    run = procs(_full_run_collecting_nothing(workers=1), _pytest_env(tmp_path, "fail"))
    assert run.drain() == pytest.ExitCode.NO_TESTS_COLLECTED, run.seen
    # The refusal line is the signal: xdist 3.8 still exits 5 when a worker
    # fails in pytest_configure, even with --max-worker-restart=0 (checked).
    assert not any("full-suite lock held by" in line for line in run.seen), run.seen


@pytest.mark.parametrize("listing", [
    "--collect-only", "--fixtures", "--fixtures-per-test", "--markers", "--help",
])
def test_a_listing_pytest_run_over_the_tree_is_not_locked(held, procs, listing):
    # Pins LISTING_OPTIONS to the option names pytest really uses.
    run = procs([*_full_run_collecting_nothing(), listing],
                _pytest_env(held.dir, "fail"))
    assert run.drain() != pytest.ExitCode.USAGE_ERROR, run.seen
    assert not any("full-suite lock held by" in line for line in run.seen), run.seen


def test_a_targeted_pytest_run_is_not_locked(held, procs):
    target = "tests/test_suite_lock.py::test_mode_waits_locally_and_is_off_on_github_actions"
    run = procs([sys.executable, "-m", "pytest", target, "-q",
                 "-p", "no:cacheprovider", "-p", "no:xdist"],
                _pytest_env(held.dir, "fail"))
    assert run.drain() == pytest.ExitCode.OK, run.seen
