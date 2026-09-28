"""``pseudolife-mcp lease``: a named lease held around a command.

A process-held lease's truth is an OS file lock (pseudolife_memory/os_lock.py),
released by the OS the instant its holder exits or dies; the agent board only
mirrors it, so other agents can see the holder, queue in FIFO order and see
the expected end. Without a daemon the command is ``flock`` with a name.

Every test uses a temporary lock directory (``PSEUDOLIFE_LEASE_LOCK_DIR``) and
talks to the board only through ``httpx.MockTransport``: nothing here touches
the real ``~/.pseudolife-mcp/locks`` or a live daemon.
"""
from __future__ import annotations

import _thread
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from pseudolife_memory import cli as console
from pseudolife_memory import lease_cli, os_lock

ROOT = Path(__file__).resolve().parent.parent

# Bounds a hang on a loaded Windows host; not a performance expectation.
START_TIMEOUT = 60.0


# --- the OS lock -------------------------------------------------------------

@pytest.mark.parametrize("name", ["gpu", "full-suite", "a.b_c-9", "GPU0"])
def test_a_safe_name_maps_to_its_own_file_name(name):
    assert os_lock.lock_file_name(name) == f"lease-{name}.lock"


@pytest.mark.parametrize("name", ["claim:a/b", "a b", "daemon/maintenance", "gpué"])
def test_an_unsafe_name_is_replaced_and_hashed(name):
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    file_name = os_lock.lock_file_name(name)
    assert file_name.endswith(f"-{digest}.lock")
    assert re.fullmatch(r"lease-[A-Za-z0-9._-]+\.lock", file_name)


def test_replaced_names_cannot_collide_with_the_literal_spelling():
    # Without the hash suffix both would be lease-claim_a_b.lock.
    assert os_lock.lock_file_name("claim:a/b") != os_lock.lock_file_name("claim_a_b")
    assert os_lock.lock_file_name("claim:a/b") != os_lock.lock_file_name("claim/a:b")


def test_the_lock_directory_defaults_to_home_and_honours_the_override(tmp_path):
    assert os_lock.lock_dir({}) == Path.home() / ".pseudolife-mcp" / "locks"
    assert os_lock.lock_dir({os_lock.LOCK_DIR_ENV: str(tmp_path)}) == tmp_path


def test_a_second_holder_in_the_same_process_is_excluded(tmp_path):
    path = tmp_path / "lease-gpu.lock"
    first, second = os_lock.OsLock(path), os_lock.OsLock(path)
    assert first.acquire()
    assert first.held
    try:
        assert not second.acquire()
        assert not second.held
    finally:
        first.release()
    assert not first.held
    assert second.acquire()  # freed by the release
    second.release()


def test_acquire_creates_the_directory(tmp_path):
    lock = os_lock.OsLock(tmp_path / "nested" / "locks" / "lease-x.lock")
    assert lock.acquire()
    lock.release()


# A holder as its own process: lock, report, hold until a line on stdin
# (clean release) or until it is killed (a crash).
_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pseudolife_memory.os_lock import OsLock
lock = OsLock(Path(sys.argv[2]))
print("HELD" if lock.acquire() else "BUSY", flush=True)
sys.stdin.readline()
lock.release()
print("RELEASED", flush=True)
"""


class _Holder:
    """A child process holding a lock, read line by line with a timeout."""

    def __init__(self, path: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(ROOT), str(path)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line.strip())
        self._lines.put(None)

    def line(self) -> str | None:
        return self._lines.get(timeout=START_TIMEOUT)

    def release(self):
        self.proc.stdin.write("\n")
        self.proc.stdin.flush()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=START_TIMEOUT)


@pytest.fixture
def holder(tmp_path):
    started = []

    def start(path):
        started.append(_Holder(path))
        return started[-1]

    yield start
    for proc in started:
        proc.stop()


def _eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_another_process_is_excluded_until_it_releases(tmp_path, holder):
    path = tmp_path / "lease-gpu.lock"
    child = holder(path)
    assert child.line() == "HELD"
    mine = os_lock.OsLock(path)
    assert not mine.acquire()
    child.release()
    assert child.line() == "RELEASED"
    assert _eventually(mine.acquire)
    mine.release()


def test_the_os_releases_the_lock_when_its_holder_dies(tmp_path, holder):
    path = tmp_path / "lease-gpu.lock"
    child = holder(path)
    assert child.line() == "HELD"
    mine = os_lock.OsLock(path)
    assert not mine.acquire()
    child.proc.kill()  # no release: the OS must drop it
    child.proc.wait(timeout=START_TIMEOUT)
    assert _eventually(mine.acquire)
    mine.release()


def test_a_child_process_is_excluded_while_this_process_holds(tmp_path, holder):
    path = tmp_path / "lease-gpu.lock"
    mine = os_lock.OsLock(path)
    assert mine.acquire()
    try:
        child = holder(path)
        assert child.line() == "BUSY"
    finally:
        mine.release()


def test_probe_reports_held_free_and_missing_without_creating(tmp_path):
    path = tmp_path / "lease-gpu.lock"
    assert os_lock.probe(path) is None
    assert not path.exists()  # a probe never creates a lock file
    lock = os_lock.OsLock(path)
    assert lock.acquire()
    try:
        assert os_lock.probe(path) is True
    finally:
        lock.release()
    assert os_lock.probe(path) is False
    assert lock.acquire()  # the probe let go of what it tried
    lock.release()


# --- durations ---------------------------------------------------------------

@pytest.mark.parametrize(("text", "seconds"), [
    ("90", 90), ("90s", 90), ("20m", 1200), ("2h", 7200), (" 5M ", 300), ("0", 0),
])
def test_durations_accept_seconds_minutes_and_hours(text, seconds):
    assert lease_cli.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "m", "1.5h", "-5", "5d", "5 m", "abc", "1h30m"])
def test_malformed_durations_are_refused(text):
    with pytest.raises(ValueError):
        lease_cli.parse_duration(text)


def test_renewal_runs_at_a_third_of_the_ttl():
    assert lease_cli._renew_interval(120) == 40


def test_exit_status_follows_shell_conventions():
    assert lease_cli._exit_status(3, windows=False) == 3
    assert lease_cli._exit_status(-15, windows=False) == 143  # killed by SIGTERM
    # Windows reports NTSTATUS codes unsigned; sys.exit needs them signed to
    # hand the same 32 bits back (0xC000013A is STATUS_CONTROL_C_EXIT).
    assert lease_cli._exit_status(0xC000013A, windows=True) == 0xC000013A - (1 << 32)
    assert lease_cli._exit_status(7, windows=True) == 7


# --- a fake board ------------------------------------------------------------
#
# tests/fake_board.py: the scripted daemon, shared with the suite lock's tests.

from tests.fake_board import (  # noqa: E402
    AGENT, AGENTS, CREDENTIAL, HELD, QUEUED, SENT, TOKEN, FakeDaemon, peer, released_last,
)
from tests.fake_board import holder_record as _holder  # noqa: E402


@pytest.fixture
def lease_env(tmp_path, monkeypatch):
    """A temp lock directory, a bearer token, an unroutable daemon URL (the
    fake board is a MockTransport; nothing may reach a real daemon) and
    fast polling."""
    monkeypatch.setenv(os_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
    for name in ("PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_LEASES_HELD",
                 "PSEUDOLIFE_AGENT_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lease_cli, "BOARD_POLL", 0.01)
    monkeypatch.setattr(lease_cli, "LOCK_POLL", 0.01)
    monkeypatch.setattr(lease_cli, "NOTICE_EVERY", 0.0)
    monkeypatch.setattr(lease_cli, "CHILD_POLL", 0.02)
    monkeypatch.setattr(lease_cli, "TRANSIENT_RETRY", 0.01)
    monkeypatch.setattr(lease_cli, "KILL_GRACE", 5.0)
    monkeypatch.setattr(lease_cli, "_renew_interval", lambda ttl: 0.05)
    return tmp_path / "locks"


def _command(tmp_path, *, exit_code=0, sleep=0.0, ready=None, wait_for=None):
    """A child that records when it ran and what PSEUDOLIFE_LEASES_HELD it
    saw, then exits with ``exit_code``. With ``ready`` it first creates that
    file, to say it is up; with ``wait_for`` it does not exit until that
    file exists (or START_TIMEOUT has passed)."""
    marker = tmp_path / "ran.json"
    code = (
        "import json, os, sys, time\n"
        + (f"open({str(ready)!r}, 'w').close()\n" if ready else "")
        + (f"deadline = time.monotonic() + {START_TIMEOUT!r}\n"
           f"while not os.path.exists({str(wait_for)!r}) and time.monotonic() < deadline:\n"
           "    time.sleep(0.01)\n" if wait_for else "")
        + f"time.sleep({sleep!r})\n"
        f"with open({str(marker)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'held': os.environ.get('PSEUDOLIFE_LEASES_HELD'),"
        " 't': time.time(),"
        " 'credential': any('cred-SECRET' in v for v in os.environ.values())}, f)\n"
        f"sys.exit({exit_code})\n")
    return [sys.executable, "-c", code], marker


def _ran(marker) -> dict:
    return json.loads(marker.read_text(encoding="utf-8"))


def _run(argv, daemon):
    return lease_cli.main(argv, transport=daemon.transport)


def _hold(lease_env, name="gpu"):
    blocker = os_lock.OsLock(lease_env / os_lock.lock_file_name(name))
    assert blocker.acquire()
    return blocker


def _free_after_waiting(monkeypatch, blocker, seconds=0.4) -> dict:
    """Release ``blocker`` once the run has spent ``seconds`` waiting on it.
    The hook is the run's own pause, which it reaches only after a busy
    attempt, so the lock can never be freed before the first try however
    slow the host is. Returns a dict that gets the wall time of the release
    under ``freed``."""
    pause = lease_cli._pause
    state: dict = {}

    def pause_then_maybe_free(poll, deadline):
        state.setdefault("first", time.monotonic())
        if "freed" not in state and time.monotonic() - state["first"] >= seconds:
            state["freed"] = time.time()
            blocker.release()
        pause(poll, deadline)

    monkeypatch.setattr(lease_cli, "_pause", pause_then_maybe_free)
    return state


# --- lease run: the board path -------------------------------------------------

def test_a_held_lease_runs_the_command_then_releases(lease_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_AGENT_PROJECT", "pseudolife")
    monkeypatch.setenv("PSEUDOLIFE_LEASES_HELD", "suite")
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)

    code = _run(["run", "gpu", "--expect", "20m", "--purpose", "nightly eval", "--",
                 *command], daemon)

    assert code == 0
    assert _ran(marker)["held"] == "suite,gpu"  # appended to the inherited value
    assert daemon.actions()[0] == "register"
    assert released_last(daemon)
    assert daemon.bodies("register") == [{
        "label": "lease-run", "project": "pseudolife",
        "task": f"gpu: {os.path.basename(sys.executable)}", "status": "",
        "capabilities": {"resumable": False}, "wake_enabled": False}]
    assert daemon.bodies("lease")[0] == {"name": "gpu", "ttl": 120, "expect": 1200,
                                         "purpose": "nightly eval"}
    assert daemon.bodies("release") == [{"name": "gpu"}]
    for action, headers, _body, _at in daemon.calls:
        assert headers["authorization"] == f"Bearer {TOKEN}"
        if action == "register":
            assert "x-pl-agent" not in headers and "x-pl-agent-key" not in headers
        else:
            assert headers["x-pl-agent"] == AGENT
            assert headers["x-pl-agent-key"] == CREDENTIAL
    assert (lease_env / "lease-gpu.lock").exists()
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False  # released
    assert capsys.readouterr().out == ""


def test_the_task_label_is_truncated_to_the_board_limit(lease_env, tmp_path):
    daemon = FakeDaemon()
    command, _ = _command(tmp_path)
    name = "n" * 120
    assert _run(["run", name, "--", *command], daemon) == 0
    task = daemon.bodies("register")[0]["task"]
    assert len(task) == 120 and task.startswith(name[:100])


def test_a_queued_run_reports_its_place_then_runs_once_held(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[QUEUED(position=2, queued=2), QUEUED(position=1, queued=1),
                               HELD()])
    command, marker = _command(tmp_path)

    assert _run(["run", "gpu", "--", *command], daemon) == 0

    assert marker.exists()
    out, err = capsys.readouterr()
    assert out == ""
    assert "position 2 of 2" in err and "position 1 of 1" in err
    assert "other-run" in err and "reviewer" in err and "nightly eval" in err
    assert daemon.actions().count("lease") >= 3
    assert released_last(daemon)


def test_every_lease_call_repeats_the_same_expect(lease_env, tmp_path):
    # The board keeps each hold's expect: repeating the same value leaves
    # the expected end alone, and a changed one would be logged as a change,
    # so it is never recomputed as the work goes on.
    daemon = FakeDaemon(lease=[QUEUED(), HELD()])
    command, _ = _command(tmp_path, sleep=0.4)

    assert _run(["run", "gpu", "--expect", "90", "--", *command], daemon) == 0

    bodies = daemon.bodies("lease")
    assert len(bodies) >= 4  # queued, held, then renewals while the command ran
    assert all(body.get("expect") == 90 for body in bodies)
    assert all(body["ttl"] == 120 for body in bodies)


def test_without_expect_no_lease_call_carries_one(lease_env, tmp_path):
    daemon = FakeDaemon()
    command, _ = _command(tmp_path, sleep=0.2)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert daemon.bodies("lease") and all("expect" not in body
                                          for body in daemon.bodies("lease"))


def test_a_holder_without_a_board_lease_delays_the_run_while_renewing(
        lease_env, tmp_path, monkeypatch, capsys):
    daemon = FakeDaemon()
    blocker = _hold(lease_env)
    state = _free_after_waiting(monkeypatch, blocker)
    command, marker = _command(tmp_path)
    try:
        code = _run(["run", "gpu", "--expect", "5m", "--", *command], daemon)
    finally:
        blocker.release()

    assert code == 0
    started = _ran(marker)["t"]
    assert "freed" in state and started >= state["freed"]
    err = capsys.readouterr().err
    assert "board does not show" in err
    renewals = [at for action, _h, body, at in daemon.calls
                if action == "lease" and at < started]
    assert len(renewals) >= 3  # the board lease was kept alive during the wait
    assert all(body.get("expect") == 300 for body in daemon.bodies("lease"))
    assert released_last(daemon)


def test_the_exit_code_propagates_and_release_follows_a_failure(lease_env, tmp_path):
    daemon = FakeDaemon()
    command, marker = _command(tmp_path, exit_code=7)
    assert _run(["run", "gpu", "--", *command], daemon) == 7
    assert marker.exists()
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_a_missing_command_exits_127_and_releases(lease_env, tmp_path, capsys):
    daemon = FakeDaemon()
    code = _run(["run", "gpu", "--", str(tmp_path / "no-such-program")], daemon)
    assert code == 127
    assert "not found" in capsys.readouterr().err
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_timeout_while_queued_exits_75_without_running(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[QUEUED()])
    command, marker = _command(tmp_path)
    started = time.monotonic()

    assert _run(["run", "gpu", "--timeout", "1s", "--", *command], daemon) == 75

    assert time.monotonic() - started >= 0.9
    assert not marker.exists()
    assert "gave up" in capsys.readouterr().err
    assert released_last(daemon)  # leaves the queue
    assert os_lock.probe(lease_env / "lease-gpu.lock") in (None, False)


def test_timeout_on_the_local_lock_exits_75_without_running(lease_env, tmp_path):
    daemon = FakeDaemon()
    blocker = _hold(lease_env)
    command, marker = _command(tmp_path)
    try:
        assert _run(["run", "gpu", "--timeout", "0", "--", *command], daemon) == 75
    finally:
        blocker.release()
    assert not marker.exists()
    assert released_last(daemon)


def test_transient_board_errors_while_acquiring_are_retried(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[(429, {"error": "rate_limited"}),
                               (503, {"error": "coordination_unavailable"}),
                               httpx.ReadTimeout("slow"),
                               (400, {"error": "lease_queue_full"}),
                               HELD()])
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    assert "board skipped" not in capsys.readouterr().err
    assert daemon.actions().count("register") == 1


def test_a_board_that_keeps_failing_falls_back_to_the_local_lock(
        lease_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lease_cli, "BOARD_GIVE_UP", 0.1)
    daemon = FakeDaemon(lease=[(503, {"error": "coordination_unavailable"})])
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    err = capsys.readouterr().err
    assert err.count("board skipped") == 1
    assert "release" in daemon.actions()


# --- lease run: the fallback --------------------------------------------------

@pytest.mark.parametrize(("stage", "reply", "why"), [
    ("register", httpx.ConnectError("refused"), "unreachable"),
    ("register", (401, {"error": "unauthorized"}), "bearer"),
    ("register", (403, {"error": "principal_not_allowed"}), "principal"),
    ("register", (200, {"enabled": False}), "disabled"),
    ("register", (400, {"error": "coordination_requires_postgres"}), "postgresql"),
    ("lease", (400, {"error": "unknown_coordination_action"}), "leases"),
    ("lease", (200, {"enabled": False}), "disabled"),
    ("lease", (403, {"error": "invalid_credential"}), "board address"),
    ("lease", (404, {"error": "instance_not_found"}), "board address"),
])
def test_an_unusable_board_falls_back_to_the_local_lock(
        lease_env, tmp_path, monkeypatch, capsys, stage, reply, why):
    monkeypatch.setattr(lease_cli, "BOARD_GIVE_UP", 30.0)
    daemon = FakeDaemon(**{stage: [reply]})
    command, marker = _command(tmp_path, exit_code=3)
    started = time.monotonic()

    assert _run(["run", "gpu", "--", *command], daemon) == 3

    assert time.monotonic() - started < 15  # at once, not after the give-up window
    assert _ran(marker)["held"] == "gpu"
    err = capsys.readouterr().err
    assert err.count("board skipped") == 1
    assert why in err.lower()
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_without_a_token_the_board_is_never_contacted(lease_env, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN")
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    assert daemon.calls == []
    assert "PSEUDOLIFE_MCP_TOKEN" in capsys.readouterr().err


def test_an_unusable_token_file_skips_the_board(lease_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(tmp_path / "missing-token"))
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    assert daemon.calls == []
    assert "credential" in capsys.readouterr().err


def test_an_invalid_daemon_url_skips_the_board(lease_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1/api?x=1")
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    assert daemon.calls == []
    assert "PSEUDOLIFE_MCP_DAEMON_URL" in capsys.readouterr().err


def test_no_board_skips_the_daemon_entirely(lease_env, tmp_path, capsys):
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--no-board", "--", *command], daemon) == 0
    assert marker.exists()
    assert daemon.calls == []
    assert "--no-board" in capsys.readouterr().err


def test_the_fallback_waits_for_the_local_lock(lease_env, tmp_path, monkeypatch, capsys):
    daemon = FakeDaemon()
    blocker = _hold(lease_env)
    state = _free_after_waiting(monkeypatch, blocker)
    command, marker = _command(tmp_path)
    try:
        code = _run(["run", "gpu", "--no-board", "--", *command], daemon)
    finally:
        blocker.release()
    assert code == 0
    assert "freed" in state and _ran(marker)["t"] >= state["freed"]
    assert "waiting for the local lock" in capsys.readouterr().err


# --- lease run: renewal while the command runs --------------------------------

def test_renewal_survives_a_transient_error_and_warns_once_when_lost(
        lease_env, tmp_path, capsys):
    # acquire, a transient failure, a renewal, the loss, then the board
    # grants it again (the last reply repeats).
    daemon = FakeDaemon(lease=[HELD(), (503, {"error": "coordination_unavailable"}),
                               HELD(), QUEUED(), HELD()])
    command, marker = _command(tmp_path, sleep=0.6)

    assert _run(["run", "gpu", "--", *command], daemon) == 0

    assert marker.exists()
    err = capsys.readouterr().err
    assert err.count("no longer shows") == 1
    assert "503" not in err  # transient failures are retried quietly
    # Renewals go on after the loss: the same call is what takes the lease
    # back when the board grants it again.
    assert daemon.actions().count("lease") >= 6
    assert released_last(daemon)


def test_a_refused_renewal_warns_once_and_stops_renewing(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[HELD(), (401, {"error": "unauthorized"})])
    command, marker = _command(tmp_path, sleep=0.5)

    assert _run(["run", "gpu", "--", *command], daemon) == 0

    assert marker.exists()
    assert capsys.readouterr().err.count("no longer shows") == 1
    assert daemon.actions().count("lease") == 2
    assert released_last(daemon)


@pytest.fixture
def signal_later():
    """Deliver a signal to this process from another thread, ``delay``
    seconds after the file ``ready`` appears (the command is up). SIGINT is
    ``interrupt_main``: a Ctrl-C aimed at this process alone, which the
    command does not see. Others go through ``signal.raise_signal``. Python
    runs handlers in the main thread, where the test (and so ``main``) must
    be; xdist >= 3.6 runs tests there. A delivery still pending when the test
    ends is cancelled, so a stray signal never reaches pytest itself."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("needs the main thread to receive a simulated signal")
    cancelled = threading.Event()
    threads = []

    def arm(delay, *, ready=None, signum=signal.SIGINT):
        def deliver():
            deadline = time.monotonic() + START_TIMEOUT
            while ready is not None and not ready.exists():
                if cancelled.wait(0.01) or time.monotonic() > deadline:
                    return
            if cancelled.wait(delay):
                return
            if signum == signal.SIGINT:
                _thread.interrupt_main()
            else:
                signal.raise_signal(signum)

        threads.append(threading.Thread(target=deliver, daemon=True))
        threads[-1].start()

    yield arm
    cancelled.set()
    for thread in threads:
        thread.join(timeout=START_TIMEOUT)


def _recording_spawn(monkeypatch) -> list:
    """Record every child ``main`` starts; returns the (live) list."""
    children = []
    spawn = lease_cli._spawn

    def spawn_and_record(command, env):
        children.append(spawn(command, env))
        return children[-1]

    monkeypatch.setattr(lease_cli, "_spawn", spawn_and_record)
    return children


def _sleeper(ready, *, ignore=()) -> list[str]:
    """A command that says it is up, then sleeps a minute, ignoring the
    named signals."""
    lines = ["import pathlib, signal, time"]
    lines += [f"signal.signal(signal.{name}, signal.SIG_IGN)" for name in ignore]
    lines += [f"pathlib.Path({str(ready)!r}).touch()", "time.sleep(60)"]
    return [sys.executable, "-c", "\n".join(lines)]


def _reap(children) -> list:
    """The children still running (to assert on), then kill them all so a
    failing test never leaves a sleeper behind."""
    orphaned = [child for child in children if child.poll() is None]
    for child in orphaned:
        child.kill()
        child.wait(timeout=START_TIMEOUT)
    return orphaned


def test_the_local_lock_is_freed_as_soon_as_the_command_ends(lease_env, tmp_path):
    """A renewal stuck on a slow daemon must not keep the resource locked
    after the work is done: the OS lock goes first, the board after."""
    path = lease_env / "lease-gpu.lock"
    # The command exits only once the renewal below is on the wire, so it is
    # in flight, not merely due, when the cleanup starts. Left to a timer, the
    # first renewal (0.05 s after the lease is held) raced the command's
    # lifetime, and a slow runner (macOS CI, 2026-09-28) ended the command
    # first: no renewal ever ran and ``seen`` stayed empty.
    renewing = tmp_path / "renewing"
    command, marker = _command(tmp_path, wait_for=renewing)
    seen = []

    def slow_renewal(request):
        renewing.touch()
        deadline = time.monotonic() + START_TIMEOUT
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        # The command has exited and main is in its cleanup. Released first,
        # the lock frees within moments; released after the renewal thread is
        # joined (up to REQUEST_TIMEOUT + 1 s, and this renewal is what it
        # would be joining), it is still held when this 5 s window closes.
        deadline = time.monotonic() + 5.0
        while (state := os_lock.probe(path)) and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.append(state)
        return HELD()

    daemon = FakeDaemon(lease=[HELD(), slow_renewal])
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert seen and seen[0] is False


def test_ctrl_c_lets_the_command_finish_its_own_cleanup(lease_env, tmp_path, signal_later):
    # A console Ctrl-C reaches the command as well (one console on Windows,
    # one foreground process group on POSIX). Terminating it at once would
    # cut exactly the cleanup that Ctrl-C started: a test run's teardown, a
    # nested lease run's release.
    daemon = FakeDaemon()
    ready = tmp_path / "ready"
    command, marker = _command(tmp_path, sleep=1.0, ready=ready)
    signal_later(0.1, ready=ready)

    code = _run(["run", "gpu", "--", *command], daemon)

    assert code == 130
    assert marker.exists()  # it finished on its own; nothing terminated it
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_ctrl_c_stops_a_command_that_keeps_running_then_exits_130(
        lease_env, tmp_path, monkeypatch, signal_later):
    monkeypatch.setattr(lease_cli, "KILL_GRACE", 0.3)
    daemon = FakeDaemon()
    children = _recording_spawn(monkeypatch)
    ready = tmp_path / "ready"
    signal_later(0.0, ready=ready)
    try:
        code = _run(["run", "gpu", "--", *_sleeper(ready)], daemon)
    finally:
        orphaned = _reap(children)

    assert code == 130
    assert children and not orphaned  # stopped by main, not left running
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_a_command_that_ignores_signals_is_killed(lease_env, tmp_path, monkeypatch,
                                                   signal_later):
    monkeypatch.setattr(lease_cli, "KILL_GRACE", 0.3)
    daemon = FakeDaemon()
    children = _recording_spawn(monkeypatch)
    ready = tmp_path / "ready"
    signal_later(0.0, ready=ready)
    try:
        code = _run(["run", "gpu", "--", *_sleeper(ready, ignore=("SIGINT", "SIGTERM"))],
                    daemon)
    finally:
        orphaned = _reap(children)

    assert code == 130
    assert children and not orphaned
    if os.name != "nt":  # Windows' terminate is already TerminateProcess
        assert children[0].returncode == -signal.SIGKILL


@pytest.mark.skipif(os.name == "nt", reason="POSIX forwards SIGINT; Windows has no such signal to send")
def test_an_interrupt_aimed_at_the_wrapper_alone_is_forwarded(
        lease_env, tmp_path, monkeypatch, signal_later):
    # `kill -INT <lease pid>` reaches only this process: after the grace the
    # command is interrupted too, so it can still clean up.
    monkeypatch.setattr(lease_cli, "KILL_GRACE", 0.3)
    daemon = FakeDaemon()
    ready, got = tmp_path / "ready", tmp_path / "got-sigint"
    handler = ("import pathlib, sys, time\n"
               "try:\n"
               f"    pathlib.Path({str(ready)!r}).touch()\n"
               "    time.sleep(60)\n"
               "except KeyboardInterrupt:\n"
               f"    pathlib.Path({str(got)!r}).touch()\n"
               "    sys.exit(0)\n")
    signal_later(0.0, ready=ready)
    assert _run(["run", "gpu", "--", sys.executable, "-c", handler], daemon) == 130
    assert got.exists()


def test_ctrl_c_while_waiting_releases_and_exits_130(lease_env, tmp_path, signal_later):
    daemon = FakeDaemon(lease=[QUEUED()])
    command, marker = _command(tmp_path)
    signal_later(0.3)
    code = _run(["run", "gpu", "--", *command], daemon)
    assert code == 130
    assert not marker.exists()
    assert released_last(daemon)


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_stop_signal_stops_the_command_releases_and_exits_128_plus_n(
        lease_env, tmp_path, monkeypatch, signal_later, name):
    # `kill <lease pid>`, `timeout`, a service or CI stop: without a handler
    # the default action ends this process without its cleanup, the OS drops
    # the lock, and a second run of the lease starts beside the command.
    if not hasattr(signal, name):
        pytest.skip(f"no {name} on this platform")
    signum = getattr(signal, name)
    monkeypatch.setattr(lease_cli, "KILL_GRACE", 0.3)
    daemon = FakeDaemon()
    children = _recording_spawn(monkeypatch)
    ready = tmp_path / "ready"

    class Unhandled(Exception):
        """What a signal main left to the previous handler raises here."""

    def unhandled(signo, frame):
        raise Unhandled(signo)

    previous = signal.signal(signum, unhandled)
    signal_later(0.1, ready=ready, signum=signum)
    try:
        code = _run(["run", "gpu", "--", *_sleeper(ready)], daemon)
        restored = signal.getsignal(signum)
    finally:
        signal.signal(signum, previous)
        orphaned = _reap(children)

    assert code == 128 + signum
    assert children and not orphaned
    assert restored is unhandled  # main put the previous handler back
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


# --- lease run: nesting, and board failures nobody planned for ------------------

def test_a_nested_run_of_the_same_lease_fails_fast(lease_env, tmp_path, monkeypatch, capsys):
    # A run inside a run of the same lease would wait for its own parent
    # forever (on the board it queues behind it; locally it waits on its lock).
    monkeypatch.setenv("PSEUDOLIFE_LEASES_HELD", "suite,gpu")
    parent = _hold(lease_env)  # what the enclosing run holds
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    started = time.monotonic()
    try:
        # --timeout only bounds the wait this test exists to rule out.
        code = _run(["run", "gpu", "--timeout", "3", "--", *command], daemon)
    finally:
        parent.release()
    assert code == lease_cli.EXIT_NESTED
    assert code not in (0, 2, 71, 75, 126, 127, 130)
    assert time.monotonic() - started < 5
    assert not marker.exists()
    assert daemon.calls == []
    assert "enclosing" in capsys.readouterr().err


def test_an_inherited_lease_whose_lock_is_free_is_not_nesting(lease_env, tmp_path, monkeypatch):
    # A process started by an old run keeps its environment after that run
    # ends; only a lock actually held makes it a nested run.
    monkeypatch.setenv("PSEUDOLIFE_LEASES_HELD", "gpu")
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert _ran(marker)["held"] == "gpu,gpu"


def test_nesting_is_judged_on_whole_names(lease_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_LEASES_HELD", "gpu2,xgpu")
    other = _hold(lease_env)  # a different process holds gpu: wait, not nesting
    daemon = FakeDaemon()
    command, _ = _command(tmp_path)
    try:
        assert _run(["run", "gpu", "--no-board", "--timeout", "0", "--", *command],
                    daemon) == 75
    finally:
        other.release()


def test_a_bom_in_the_token_file_skips_the_board(lease_env, tmp_path, monkeypatch, capsys):
    # credentials accepts U+FEFF (not whitespace); httpx cannot put it in a
    # header, and raised UnicodeEncodeError out of main before the fix.
    from pseudolife_memory.credentials import _write_token_file

    token_file = tmp_path / "token"
    _write_token_file(token_file, "﻿tok-with-bom")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(token_file))
    daemon = FakeDaemon()
    command, marker = _command(tmp_path)

    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    err = capsys.readouterr().err
    assert "board skipped" in err and "UnicodeEncodeError" in err
    assert _run(["list"], daemon) == 0
    assert "UnicodeEncodeError" in capsys.readouterr().out
    assert daemon.calls == []


def test_oversized_timestamps_from_the_board_are_survived(lease_env, tmp_path, capsys):
    huge = 10 ** 400  # math.isfinite raises OverflowError on it
    holder = {**_holder(), "acquired_at": huge, "expires_at": huge, "expected_end": huge}
    daemon = FakeDaemon(
        lease=[QUEUED(holder=holder), HELD()],
        leases=[(200, {"leases": [{"name": "gpu", "holder": holder, "fence": 1,
                                   "expires_at": huge, "expected_end": huge,
                                   "stale": False, "queued": 1,
                                   "queue": [{"agent_id": "e" * 32, "label": "w",
                                              "enqueued_at": huge, "purpose": ""}]}],
                        "truncated": False})])
    command, marker = _command(tmp_path)

    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    err = capsys.readouterr().err
    assert "position 2 of 2" in err and "board skipped" not in err
    assert _run(["list"], daemon) == 0
    assert "lease gpu: held by other-run" in capsys.readouterr().out


def test_an_unexpected_board_error_never_stops_the_command(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[RuntimeError("boom")])
    command, marker = _command(tmp_path)
    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert marker.exists()
    err = capsys.readouterr().err
    assert "board skipped" in err and "RuntimeError" in err
    assert "release" in daemon.actions()  # left any queue place it had


def test_an_unexpected_error_while_releasing_keeps_the_exit_code(lease_env, tmp_path):
    daemon = FakeDaemon(release=[RuntimeError("boom")])
    command, marker = _command(tmp_path, exit_code=4)
    assert _run(["run", "gpu", "--", *command], daemon) == 4
    assert marker.exists()
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_the_instance_credential_is_never_printed(lease_env, tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(lease_cli, "BOARD_GIVE_UP", 0.05)
    daemon = FakeDaemon(lease=[QUEUED(), HELD(), (503, {"error": "coordination_unavailable"}),
                               QUEUED(), (400, {"error": "lease_not_held"})],
                        release=[(400, {"error": "lease_not_held"})])
    command, marker = _command(tmp_path, sleep=0.3)

    assert _run(["run", "gpu", "--", *command], daemon) == 0
    assert _run(["list"], daemon) == 0

    assert _ran(marker)["credential"] is False  # never exported to the command
    out, err = capfd.readouterr()
    assert CREDENTIAL not in out + err
    assert TOKEN not in out + err


# --- usage errors ---------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    [],
    ["run", "gpu"],
    ["run", "gpu", "--"],
    ["run", "", "--", "x"],
    ["run", "   ", "--", "x"],
    ["run", "a\tb", "--", "x"],
    ["run", "x" * 121, "--", "x"],
    ["run", "gpu", "--ttl", "29", "--", "x"],
    ["run", "gpu", "--ttl", "86401", "--", "x"],
    ["run", "gpu", "--expect", "0", "--", "x"],
    ["run", "gpu", "--expect", "604801", "--", "x"],
    ["run", "gpu", "--purpose", "p" * 241, "--", "x"],
    ["run", "gpu", "--purpose", "a\nb", "--", "x"],
    ["run", "gpu", "--timeout", "soon", "--", "x"],
    ["list", "--", "x"],
    ["bogus"],
])
def test_usage_errors_exit_2_before_touching_anything(lease_env, argv):
    daemon = FakeDaemon()
    assert _run(argv, daemon) == 2
    assert daemon.calls == []
    assert not lease_env.exists() or not any(lease_env.iterdir())


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["list", "--help"]])
def test_help_exits_0(lease_env, argv, capsys):
    assert _run(argv, FakeDaemon()) == 0
    assert "pseudolife-mcp lease" in capsys.readouterr().out


# --- lease list -----------------------------------------------------------------

def test_list_without_a_board_reports_local_locks(lease_env, monkeypatch, capsys):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN")
    gpu = _hold(lease_env, "gpu")
    free = os_lock.OsLock(lease_env / os_lock.lock_file_name("free"))
    assert free.acquire()
    free.release()
    suite = os_lock.OsLock(lease_env / "full-suite.lock")
    assert suite.acquire()
    daemon = FakeDaemon()
    try:
        assert _run(["list"], daemon) == 0
    finally:
        gpu.release()
        suite.release()
    out = capsys.readouterr().out
    assert daemon.calls == []
    assert "board unavailable" in out
    assert "lease-gpu.lock: held" in out
    assert "lease-free.lock: free" in out
    assert "test-suite lock: held" in out


def test_list_merges_the_board_with_local_locks(lease_env, capsys):
    now = time.time()
    board_leases = {"leases": [
        {"name": "gpu", "holder": _holder(purpose="nightly eval", age=600, expected=-60),
         "fence": 3, "expires_at": now + 90, "expected_end": now - 60, "stale": True,
         "queued": 1, "queue": [{"agent_id": "e" * 32, "label": "waiting-run",
                                 "enqueued_at": now - 30, "purpose": "retrain"}]},
        {"name": "suite", "holder": None, "fence": 2, "expires_at": None,
         "expected_end": None, "stale": False, "queued": 0, "queue": []},
    ], "truncated": False}
    daemon = FakeDaemon(leases=[(200, board_leases)])
    gpu = _hold(lease_env, "gpu")
    bare = _hold(lease_env, "bare")
    try:
        assert _run(["list"], daemon) == 0
        text = capsys.readouterr().out
        assert _run(["list", "--json"], daemon) == 0
        report = json.loads(capsys.readouterr().out)
    finally:
        gpu.release()
        bare.release()

    assert daemon.bodies("leases") == [{"limit": 50}, {"limit": 50}]
    for _action, headers, _body, _at in daemon.calls:
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert "x-pl-agent" not in headers  # bearer only
    gpu_block = text.split("lease gpu", 1)[1].split("lease suite", 1)[0]
    assert "other-run" in gpu_block and "nightly eval" in gpu_block
    assert "10m" in gpu_block  # held for ten minutes
    assert "stale" in gpu_block
    assert "waiting-run" in gpu_block and "retrain" in gpu_block
    assert "local lock: held" in gpu_block
    suite_block = text.split("lease suite", 1)[1]
    assert "free" in suite_block
    assert "lease-bare.lock: held" in text and "not on the board" in text

    assert report["board"]["available"] is True
    by_name = {lease["name"]: lease for lease in report["board"]["leases"]}
    assert by_name["gpu"]["local_lock"] == "held"
    assert by_name["suite"]["local_lock"] is None  # no local file for it
    assert {"file": "lease-bare.lock", "state": "held"} in report["local"]
    assert report["test_suite_lock"] is None


def test_list_with_a_name_asks_the_board_for_that_lease(lease_env, capsys):
    daemon = FakeDaemon()
    assert _run(["list", "gpu"], daemon) == 0
    assert daemon.bodies("leases") == [{"name": "gpu"}]


def test_list_falls_back_to_local_state_when_the_board_refuses(lease_env, capsys):
    daemon = FakeDaemon(leases=[(403, {"error": "principal_not_allowed"})])
    gpu = _hold(lease_env, "gpu")
    try:
        assert _run(["list", "--json"], daemon) == 0
    finally:
        gpu.release()
    report = json.loads(capsys.readouterr().out)
    assert report["board"]["available"] is False
    assert "principal" in report["board"]["reason"]
    assert {"file": "lease-gpu.lock", "state": "held"} in report["local"]


# --- the console entry point -----------------------------------------------------

def test_the_console_usage_lists_lease():
    assert re.search(r"^  lease\s", console._USAGE, re.M)


def test_the_console_dispatches_lease(lease_env, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "pseudolife-mcp", "lease", "run", "gpu", "--no-board", "--",
        sys.executable, "-c", "import sys; sys.exit(5)"])
    with pytest.raises(SystemExit) as stop:
        console.main()
    assert stop.value.code == 5


# --- lease check: the launch gate ------------------------------------------------
#
# An orchestrator (an overnight GPU brief, a stress repro) gates on the lease
# instead of sampling process CPU: exit 0 when free, 1 when the OS lock or the
# board says held, with the holder and its expected end on stdout.

def test_check_says_free_and_exits_0_without_a_board(lease_env, monkeypatch, capsys):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN")
    daemon = FakeDaemon()
    assert _run(["check", "gpu"], daemon) == 0
    assert "lease gpu: free" in capsys.readouterr().out
    assert daemon.calls == []


def test_check_exits_1_while_the_local_lock_is_held(lease_env, capsys):
    daemon = FakeDaemon()
    gpu = _hold(lease_env, "gpu")
    try:
        assert _run(["check", "gpu"], daemon) == 1
    finally:
        gpu.release()
    out = capsys.readouterr().out
    assert "lease gpu: held" in out and "local lock" in out
    assert _run(["check", "gpu"], daemon) == 0


def test_check_of_the_full_suite_probes_the_suite_lock_and_names_its_holder(
        lease_env, monkeypatch, capsys):
    # The suite's own lock (tests/suite_lock.py) is the truth for full-suite:
    # its file is full-suite.lock, not lease-full-suite.lock, and its holder
    # record names the pid and worktree.
    monkeypatch.setenv("PSEUDOLIFE_SUITE_LOCK_DIR", str(lease_env))
    lease_env.mkdir(parents=True, exist_ok=True)
    suite = os_lock.OsLock(lease_env / "full-suite.lock")
    assert suite.acquire()
    (lease_env / "full-suite.holder.json").write_text(json.dumps(
        {"pid": 4242, "worktree": "wt-alpha", "started": "2026-09-28T02:30:00+10:00"}),
        encoding="utf-8")
    daemon = FakeDaemon()
    try:
        assert _run(["check", "full-suite"], daemon) == 1
        text = capsys.readouterr().out
        assert _run(["check", "full-suite", "--json"], daemon) == 1
        report = json.loads(capsys.readouterr().out)
    finally:
        suite.release()
    assert "pid 4242" in text and "wt-alpha" in text
    assert report["held"] is True
    assert report["local"]["state"] == "held" and report["local"]["pid"] == 4242
    assert _run(["check", "full-suite"], daemon) == 0


def test_check_exits_1_when_only_the_board_shows_a_holder(lease_env, capsys):
    now = time.time()
    daemon = FakeDaemon(leases=[(200, {"leases": [
        {"name": "gpu", "holder": _holder(purpose="overnight brief", age=120, expected=1800),
         "fence": 3, "expires_at": now + 90, "expected_end": now + 1800, "stale": False,
         "queued": 0, "queue": []}], "truncated": False})])
    assert _run(["check", "gpu"], daemon) == 1
    out = capsys.readouterr().out
    assert "other-run" in out and "overnight brief" in out and "expected end" in out
    assert daemon.bodies("leases") == [{"name": "gpu"}]
    for _action, headers, _body, _at in daemon.calls:
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert "x-pl-agent" not in headers  # bearer only, like list
    assert _run(["check", "gpu", "--json"], daemon) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["held"] is True
    assert report["board"]["holder"]["label"] == "other-run"
    assert report["board"]["expected_end"] == pytest.approx(now + 1800, abs=5)
    assert report["local"]["state"] is None  # no lock file yet


def test_check_falls_back_to_the_local_lock_when_the_board_refuses(lease_env, capsys):
    daemon = FakeDaemon(leases=[(403, {"error": "principal_not_allowed"})])
    assert _run(["check", "gpu", "--json"], daemon) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["held"] is False and report["board"]["available"] is False
    assert "principal" in report["board"]["reason"]


# --- lease hold: a lease around a process this run did not start --------------
#
# Start-Qwen launches a detached server and returns; the lease must outlive
# the launcher and end with the server. ``hold NAME --while-pid PID`` takes the
# OS lock, mirrors it on the board, and releases both when PID exits.

def _live_process(seconds=1.0):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


@pytest.fixture
def sleeper():
    started = []

    def start(seconds=1.0):
        started.append(_live_process(seconds))
        # Reaped as it exits: on POSIX an unreaped child is a zombie, which
        # still answers signal 0 where there is no procfs to read its state.
        threading.Thread(target=started[-1].wait, daemon=True).start()
        return started[-1]

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=START_TIMEOUT)


def _lock_seen_held(path, timeout=10.0):
    """Wall time at which ``path`` was first probed held, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os_lock.probe(path) is True:
            return time.time()
        time.sleep(0.02)
    return None


def test_hold_keeps_the_lease_while_the_pid_lives_then_releases(lease_env, sleeper, capsys):
    daemon = FakeDaemon()
    child = sleeper(1.5)
    seen = {}
    watcher = threading.Thread(
        target=lambda: seen.update(held_at=_lock_seen_held(lease_env / "lease-gpu.lock")))
    watcher.start()

    code = _run(["hold", "gpu", "--while-pid", str(child.pid), "--expect", "30m",
                 "--purpose", "bench server", "--worktree", "wt-bench"], daemon)

    watcher.join(10)
    assert code == 0
    assert child.poll() is not None  # it waited for the process to end
    assert seen.get("held_at") is not None
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False
    assert daemon.actions()[0] == "register"
    assert released_last(daemon)
    assert daemon.bodies("register")[0]["label"] == (
        f"lease-hold@{lease_cli.instance_id(lease_env)}")
    assert daemon.bodies("lease")[0] == {"name": "gpu", "ttl": 120, "expect": 1800,
                                         "purpose": "bench server"}
    assert len(daemon.bodies("lease")) >= 3  # renewed while it held
    assert daemon.bodies("release") == [{"name": "gpu"}]
    assert capsys.readouterr().out == ""


def test_hold_announces_to_the_peers_the_lease_concerns(lease_env, sleeper, monkeypatch,
                                                       tmp_path):
    monkeypatch.setenv("PSEUDOLIFE_AGENT_PROJECT", "Pseudolife-MCP")
    peers = [
        peer("a" * 32, "suite-runner", "SUITE-START 8f3d; suite=running",
             project="pseudolife-mcp"),  # the project matches whatever its case
        peer("b" * 32, "gpu-brief", "gpu=waiting for the bench server"),
        peer("c" * 32, "parked", "parked until the GPU frees", park_reason="needs_resource",
             park_clear_by="gpu"),
        peer("d" * 32, "parked-list", "parked", park_reason="needs_resource",
             park_clear_by=["maintainer", "gpu"]),
        peer("e" * 32, "unrelated", "reviewing PR #431"),
        peer("f" * 32, "gone", "suite=queued", lifecycle="detached"),
        peer("g" * 32, "elsewhere", "suite=running", project="another-repo"),
        peer("h" * 32, "other-lease", "parked", park_reason="needs_resource",
             park_clear_by="full-suite"),
        peer(AGENT, "lease-hold", "lease:gpu held"),  # this run's own address
    ]
    daemon = FakeDaemon(agents=[AGENTS(*peers)])
    child = sleeper(0.5)

    worktree = str(tmp_path / "checkouts" / "wt-bench")
    assert _run(["hold", "gpu", "--while-pid", str(child.pid), "--expect", "20m",
                 "--worktree", worktree], daemon) == 0

    sends = daemon.bodies("send")
    acquired = [body for body in sends if "acquired" in body["text"]]
    released = [body for body in sends if "released" in body["text"]]
    wanted = sorted(prefix * 32 for prefix in "abcd")
    assert sorted(body["to"] for body in acquired) == wanted
    assert sorted(body["to"] for body in released) == wanted
    assert len({body["request_id"] for body in sends}) == len(sends)
    text = acquired[0]["text"]
    assert text.startswith("LEASE gpu acquired")
    assert f"pid {child.pid}" in text and "wt-bench" in text and "expected end" in text
    assert "SUITE-START" not in text
    assert "released" in released[0]["text"] and "wt-bench" in released[0]["text"]
    # The checkout's name, never its path (which names the OS user).
    for body in sends:
        assert worktree not in body["text"] and str(tmp_path) not in body["text"]
    assert str(tmp_path) not in daemon.bodies("register")[0]["status"]
    # The peer list was asked with this run's address; the acquired notices
    # followed the lease call, and the released ones follow the board
    # release, so a peer told "released" finds the lease free.
    actions = daemon.actions()
    assert actions.index("agents") > actions.index("lease")
    release_at = len(actions) - 1 - actions[::-1].index("release")
    texts = [call[2].get("text", "") for call in daemon.calls]
    assert all(i > release_at for i, t in enumerate(texts) if "released" in t)
    assert all(i < release_at for i, t in enumerate(texts) if "acquired" in t)
    assert released_last(daemon)
    for action, headers, _body, _at in daemon.calls:
        if action in ("agents", "send"):
            assert headers["x-pl-agent"] == AGENT


def test_hold_without_a_board_holds_the_lock_alone(lease_env, sleeper, monkeypatch, capsys):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN")
    daemon = FakeDaemon()
    child = sleeper(0.5)
    assert _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon) == 0
    assert daemon.calls == []
    err = capsys.readouterr().err
    assert err.count("board skipped") == 1 and "PSEUDOLIFE_MCP_TOKEN" in err


def test_hold_exits_75_at_once_when_the_lock_is_held_and_the_timeout_is_zero(
        lease_env, sleeper, capsys):
    daemon = FakeDaemon()
    blocker = _hold(lease_env)
    child = sleeper(5)
    try:
        code = _run(["hold", "gpu", "--while-pid", str(child.pid), "--timeout", "0"], daemon)
    finally:
        blocker.release()
    assert code == 75
    assert daemon.calls == []  # the OS lock comes first; nothing reached the board
    assert "gave up" in capsys.readouterr().err


def test_hold_waits_for_a_held_lock_then_takes_it(lease_env, sleeper, monkeypatch):
    daemon = FakeDaemon()
    blocker = _hold(lease_env)
    state = _free_after_waiting(monkeypatch, blocker)
    child = sleeper(2)
    try:
        code = _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon)
    finally:
        blocker.release()
    assert code == 0 and "freed" in state
    held_from = [at for action, _h, _b, at in daemon.calls if action == "lease"][0]
    assert held_from >= state["freed"]


def test_hold_of_a_pid_that_is_gone_releases_at_once(lease_env, capsys):
    # 2**22 - 1: odd, so never a Windows pid; above any live pid elsewhere.
    daemon = FakeDaemon()
    started = time.monotonic()
    assert _run(["hold", "gpu", "--while-pid", str(2**22 - 1)], daemon) == 0
    assert time.monotonic() - started < 10
    assert "already gone" in capsys.readouterr().err
    assert daemon.calls == []  # nothing was taken, so nothing is announced
    assert os_lock.probe(lease_env / "lease-gpu.lock") in (None, False)


def test_hold_is_stopped_by_a_signal_and_releases(lease_env, sleeper, capsys):
    daemon = FakeDaemon()
    child = sleeper(20)
    timer = threading.Timer(0.5, _thread.interrupt_main)
    timer.start()
    try:
        code = _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon)
    finally:
        timer.cancel()
    assert code == 130
    assert child.poll() is None  # the process it followed is not its to stop
    assert released_last(daemon)
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["hold", "gpu"],
    ["hold", "gpu", "--while-pid", "abc"],
    ["hold", "gpu", "--while-pid", "0"],
    ["hold", "gpu", "--while-pid", "-4"],
    ["hold", "gpu", "--while-pid", "12", "--", "x"],
    ["check"],
    ["check", "gpu", "--", "x"],
    ["check", "a\tb"],
])
def test_hold_and_check_usage_errors_exit_2(lease_env, argv):
    daemon = FakeDaemon()
    assert _run(argv, daemon) == 2
    assert daemon.calls == []


def test_help_covers_hold_and_check(lease_env, capsys):
    for argv in (["hold", "--help"], ["check", "--help"]):
        assert _run(argv, FakeDaemon()) == 0
        assert "pseudolife-mcp lease" in capsys.readouterr().out


# --- Start-Qwen takes the gpu lease ------------------------------------------------

def test_start_qwen_gates_on_and_holds_the_gpu_lease():
    """evals/qwen_server.ps1 refuses to launch onto a held gpu lease and holds
    one around the server it starts, through this CLI. Pinned as text: the
    launcher itself needs a GPU and a 27B model."""
    script = (ROOT / "evals" / "qwen_server.ps1").read_text(encoding="utf-8")
    assert "lease check gpu" in script
    assert "lease hold gpu" in script and "--while-pid" in script
    assert "Stop-GpuLease" in script and "Start-GpuLease" in script


def test_qwen_server_script_still_parses():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    script = ROOT / "evals" / "qwen_server.ps1"
    probe = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             f"'{script}', [ref]$null, [ref]$errors); $errors | ForEach-Object "
             "{ $_.Message }; exit $errors.Count")
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", probe],
                            capture_output=True, text=True, timeout=START_TIMEOUT)
    assert result.returncode == 0, result.stdout + result.stderr


# --- review findings (2026-09-28) ------------------------------------------------

def _board_holding(label, name="gpu"):
    now = time.time()
    return FakeDaemon(leases=[(200, {"leases": [
        {"name": name, "holder": _holder(label=label), "fence": 3,
         "expires_at": now + 90, "expected_end": None, "stale": False,
         "queued": 0, "queue": []}], "truncated": False})])


def _free_lock_file(lease_env, name="gpu"):
    lease_env.mkdir(parents=True, exist_ok=True)
    free = os_lock.OsLock(lease_env / os_lock.lock_file_name(name))
    assert free.acquire()
    free.release()  # the file stays: a lock file is never deleted


def test_check_trusts_a_free_local_lock_over_this_machines_leftover_hold(lease_env, capsys):
    # A `lease hold` killed outright drops its OS lock at once while its board
    # record lapses at the ttl. Its label carries this lock directory's
    # instance id, so beside the free lock here the record is stale.
    _free_lock_file(lease_env)
    label = f"{lease_cli.HOLD_LABEL}@{lease_cli.instance_id(lease_env)}"
    daemon = _board_holding(label)
    assert _run(["check", "gpu"], daemon) == 0
    out = capsys.readouterr().out
    assert "lease gpu: free" in out and label in out and "stale" in out
    assert _run(["check", "gpu", "--json"], daemon) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["held"] is False and report["board"]["holder"]["label"] == label


@pytest.mark.parametrize("label", [
    f"{lease_cli.HOLD_LABEL}@0123456789ab",  # a hold on another machine, WSL or account
    lease_cli.HOLD_LABEL,                    # no instance id to vouch for it
    lease_cli.LABEL,                         # `lease run`: its lock may be anywhere
])
def test_check_counts_a_hold_it_cannot_vouch_for_as_held(lease_env, capsys, label):
    # The local lock file here says nothing about a lock in another home
    # directory: only a hold stamped with this directory's id yields to it
    # (orchestrator re-check of be50adf5, 2026-09-28).
    _free_lock_file(lease_env)
    lease_cli.instance_id(lease_env)  # this machine's id exists, and differs
    daemon = _board_holding(label)
    assert _run(["check", "gpu"], daemon) == 1
    out = capsys.readouterr().out
    assert "lease gpu: held" in out and "stale" not in out


def test_the_instance_id_is_minted_once_per_lock_directory(tmp_path):
    first = lease_cli.instance_id(tmp_path)
    assert first and len(first) == 12 and int(first, 16) >= 0
    assert lease_cli.instance_id(tmp_path) == first
    assert lease_cli.instance_id(tmp_path / "elsewhere") != first
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / lease_cli.INSTANCE_FILE).write_text("not an id", encoding="ascii")
    assert lease_cli.instance_id(tmp_path / "bad") is None  # never overwritten
    from pseudolife_memory.storage.coordination import looks_like_secret
    assert not looks_like_secret(f"{lease_cli.HOLD_LABEL}@{first}")


def test_check_counts_a_session_claim_as_held_beside_a_free_lock(lease_env, capsys):
    # A session that claimed gpu (memory_agents claim) before loading a model
    # holds it on the board only; the lock file exists from an earlier run
    # and is free. No local process can speak for that claim, so it counts
    # until it is released or lapses (orchestrator re-review, 2026-09-28).
    _free_lock_file(lease_env)
    daemon = _board_holding("claude-code")
    assert _run(["check", "gpu"], daemon) == 1
    out = capsys.readouterr().out
    assert "lease gpu: held" in out and "claude-code" in out and "stale" not in out


def test_a_check_that_fails_is_never_mistaken_for_held(lease_env, monkeypatch, capsys):
    def broken(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(lease_cli, "_local_state", broken)
    code = _run(["check", "gpu"], FakeDaemon())
    assert code not in (0, 1) and code == lease_cli.EXIT_SOFTWARE
    assert "RuntimeError" in capsys.readouterr().err


def test_one_refused_notice_does_not_stop_the_others(lease_env, sleeper):
    peers = [peer(p * 32, f"runner-{p}", "suite=running") for p in "abc"]

    def send(request):
        body = json.loads(request.content)
        if body["to"] == "a" * 32:
            return 400, {"error": "recipient_not_found"}
        return SENT()

    daemon = FakeDaemon(agents=[AGENTS(*peers)], send=[send])
    child = sleeper(0.5)
    assert _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon) == 0
    told = {body["to"] for body in daemon.bodies("send") if "acquired" in body["text"]}
    assert told == {"a" * 32, "b" * 32, "c" * 32}


def test_a_parked_peer_is_told_even_while_detached(lease_env, sleeper):
    peers = [peer("a" * 32, "parked", "parked", lifecycle="detached",
                  park_reason="needs_resource", park_clear_by="gpu"),
             peer("b" * 32, "gone", "suite=running", lifecycle="detached"),
             peer("c" * 32, "revoked", "parked", lifecycle="revoked",
                  park_reason="needs_resource", park_clear_by="gpu")]
    daemon = FakeDaemon(agents=[AGENTS(*peers)])
    child = sleeper(0.5)
    assert _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon) == 0
    assert {body["to"] for body in daemon.bodies("send")} == {"a" * 32}


def test_only_a_standing_park_is_told(lease_env, sleeper):
    # The daemon's rule (CoordinationStore._live_park): a park stands while
    # its reason is set and its park_expires is unset or still ahead. The
    # peer list returns a lapsed park's fields as they were stored.
    now = time.time()
    parked = {"park_reason": "needs_resource", "park_clear_by": "gpu"}
    peers = [peer("a" * 32, "standing", "parked", park_expires=now + 3600, **parked),
             peer("b" * 32, "no-expiry", "parked", park_expires=None, **parked),
             peer("c" * 32, "lapsed", "parked", park_expires=now - 1, **parked),
             peer("d" * 32, "no-reason", "parked", park_reason=None, park_clear_by="gpu",
                  park_expires=now + 3600),
             # A lapsed park still counts its status like any live peer.
             peer("e" * 32, "lapsed-running", "suite=running", park_expires=now - 1,
                  **parked)]
    daemon = FakeDaemon(agents=[AGENTS(*peers)])
    child = sleeper(0.5)
    assert _run(["hold", "gpu", "--while-pid", str(child.pid)], daemon) == 0
    told = {body["to"] for body in daemon.bodies("send") if "acquired" in body["text"]}
    assert told == {"a" * 32, "b" * 32, "e" * 32}


@pytest.mark.parametrize("reason, expires, stands", [
    (None, None, False),               # no park
    ("needs_resource", 1060.0, True),  # live
    ("needs_resource", None, True),    # no expiry
    ("needs_resource", 940.0, False),  # lapsed
    ("needs_resource", 1000.0, False),  # expires == now has lapsed
    (None, 1060.0, False),             # an expiry without a reason
])
def test_a_standing_park_is_the_daemons_live_park(reason, expires, stands):
    # _clear_by copies CoordinationStore._live_park's rule rather than
    # importing it (the lease CLI stays stdlib-only; the store imports
    # psycopg), so both run over the same table and must agree.
    from pseudolife_memory.storage.coordination import CoordinationStore
    now = 1000.0
    row = {"park_reason": reason, "park_clear_by": "gpu", "park_expires": expires}
    assert (CoordinationStore._live_park(None, row, now) is not None) is stands
    assert lease_cli._clear_by(row, now) == (["gpu"] if stands else [])


def test_a_park_with_an_unreadable_expiry_is_told():
    # The daemon stores park_expires only as a positive finite number, so
    # the list never carries anything else; should one arrive, the notice
    # goes out (one extra message) rather than raising in the mirror.
    row = {"park_reason": "needs_resource", "park_clear_by": "gpu", "park_expires": "soon"}
    assert lease_cli._clear_by(row, 1000.0) == ["gpu"]


def test_hold_frees_the_lock_before_the_board_hears(lease_env, sleeper, monkeypatch):
    seen = {}
    real = lease_cli.BoardMirror.release

    def release(self, *args, **kwargs):
        seen["lock"] = os_lock.probe(lease_env / "lease-gpu.lock")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(lease_cli.BoardMirror, "release", release)
    child = sleeper(0.3)
    assert _run(["hold", "gpu", "--while-pid", str(child.pid)], FakeDaemon()) == 0
    assert seen["lock"] is False


# --- the PowerShell launcher against the real CLI ----------------------------------
#
# The mocked harness in test_regression_gate_inputs.py cannot see how
# qwen_server.ps1 finds and calls the CLI; this runs the real helpers against a
# temp lock directory (no board: no token, unroutable daemon URL).

def _pwsh_or_skip():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    return pwsh


def _pwsh(script, env):
    return subprocess.run([_pwsh_or_skip(), "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=START_TIMEOUT * 2, env=env)


def test_the_launcher_gate_sees_a_held_gpu_lease_and_holds_one(lease_env, tmp_path):
    _pwsh_or_skip()
    env = dict(os.environ)
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    env.pop("PSEUDOLIFE_MCP_TOKEN_FILE", None)
    env[os_lock.LOCK_DIR_ENV] = str(lease_env)
    env["PSEUDOLIFE_LEASE_PYTHON"] = sys.executable
    helper = (ROOT / "evals" / "qwen_server.ps1").as_posix()
    script = f"""
. '{helper}'
$py = '{sys.executable}'
$hidden = @{{}}
if ($IsWindows) {{ $hidden.WindowStyle = 'Hidden' }}
$server = Start-Process -FilePath $py -ArgumentList '-c "import time; time.sleep(30)"' -PassThru @hidden
$before = Test-GpuLeaseHeld
Start-GpuLease -ServerPid $server.Id -Purpose 'probe'
$during = Test-GpuLeaseHeld
$second = Start-Process -FilePath $py -ArgumentList '-c "import time; time.sleep(30)"' -PassThru @hidden
$keep = $script:GpuLeaseProcess
Start-GpuLease -ServerPid $second.Id -Purpose 'probe-2'
$secondHold = $script:GpuLeaseProcess
$script:GpuLeaseProcess = $keep
$server.Kill(); $server.WaitForExit()
Stop-GpuLease -WaitSeconds 20
$after = Test-GpuLeaseHeld
$second.Kill()
Write-Output ("RESULT=" + ($null -eq $before) + "," + ($null -ne $during) + "," + ($null -eq $secondHold) + "," + ($null -eq $after))
"""
    result = _pwsh(script, env)
    line = next((l for l in result.stdout.splitlines() if l.startswith("RESULT=")), None)
    assert line == "RESULT=True,True,True,True", result.stdout + result.stderr
    # The second hold found the lock taken and said so, instead of "started".
    assert "could not take the gpu lease" in result.stdout + result.stderr
