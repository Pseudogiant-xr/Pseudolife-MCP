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

TOKEN = "tok-SECRET-bearer-5b1c"
CREDENTIAL = "cred-SECRET-instance-9f3e"
AGENT = "0123456789abcdef0123456789abcdef"


def _holder(label="other-run", purpose="nightly eval", age=240.0, expected=600.0):
    now = time.time()
    return {"agent_id": "f" * 32, "label": label, "principal": "reviewer",
            "purpose": purpose, "acquired_at": now - age, "expires_at": now + 100,
            "expected_end": None if expected is None else now + expected}


def HELD(name="gpu"):
    now = time.time()
    return 200, {"name": name, "state": "held", "fence": 7, "expires_at": now + 120,
                 "expected_end": None, "position": None, "queued": 0,
                 "holder": {"agent_id": AGENT, "label": "lease-run", "principal": "default",
                            "purpose": "", "acquired_at": now, "expires_at": now + 120,
                            "expected_end": None}}


def QUEUED(position=2, queued=2, holder="default", name="gpu"):
    return 200, {"name": name, "state": "queued", "fence": None, "expires_at": None,
                 "expected_end": None, "position": position, "queued": queued,
                 "holder": _holder() if holder == "default" else holder}


class FakeDaemon:
    """The coordination REST actions the lease CLI calls, each scripted as a
    list of replies: ``(status, json)``, an exception to raise, or a callable
    taking the request. The last reply of a script repeats."""

    def __init__(self, *, register=None, lease=None, release=None, leases=None):
        self.calls: list[tuple[str, httpx.Headers, dict, float]] = []
        self.scripts = {
            "register": list(register or [(200, {"agent_id": AGENT, "credential": CREDENTIAL,
                                                 "label": "lease-run"})]),
            "lease": list(lease or [HELD()]),
            "release": list(release or [(200, {"name": "gpu", "released": True,
                                               "dequeued": False})]),
            "leases": list(leases or [(200, {"leases": [], "truncated": False})]),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.startswith("/api/coordination/")
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((action, request.headers, body, time.time()))
        script = self.scripts[action]
        reply = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(request)
        status, payload = reply
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]

    def bodies(self, action: str) -> list[dict]:
        return [call[2] for call in self.calls if call[0] == action]


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


def _command(tmp_path, *, exit_code=0, sleep=0.0, ready=None):
    """A child that records when it ran and what PSEUDOLIFE_LEASES_HELD it
    saw, then exits with ``exit_code``. With ``ready`` it first creates that
    file, to say it is up."""
    marker = tmp_path / "ran.json"
    code = (
        "import json, os, sys, time\n"
        + (f"open({str(ready)!r}, 'w').close()\n" if ready else "")
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
    assert daemon.actions()[-1] == "release"
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
    assert daemon.actions()[-1] == "release"


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
    assert daemon.actions()[-1] == "release"


def test_the_exit_code_propagates_and_release_follows_a_failure(lease_env, tmp_path):
    daemon = FakeDaemon()
    command, marker = _command(tmp_path, exit_code=7)
    assert _run(["run", "gpu", "--", *command], daemon) == 7
    assert marker.exists()
    assert daemon.actions()[-1] == "release"
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_a_missing_command_exits_127_and_releases(lease_env, tmp_path, capsys):
    daemon = FakeDaemon()
    code = _run(["run", "gpu", "--", str(tmp_path / "no-such-program")], daemon)
    assert code == 127
    assert "not found" in capsys.readouterr().err
    assert daemon.actions()[-1] == "release"
    assert os_lock.probe(lease_env / "lease-gpu.lock") is False


def test_timeout_while_queued_exits_75_without_running(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[QUEUED()])
    command, marker = _command(tmp_path)
    started = time.monotonic()

    assert _run(["run", "gpu", "--timeout", "1s", "--", *command], daemon) == 75

    assert time.monotonic() - started >= 0.9
    assert not marker.exists()
    assert "gave up" in capsys.readouterr().err
    assert daemon.actions()[-1] == "release"  # leaves the queue
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
    assert daemon.actions()[-1] == "release"


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
    assert daemon.actions()[-1] == "release"


def test_a_refused_renewal_warns_once_and_stops_renewing(lease_env, tmp_path, capsys):
    daemon = FakeDaemon(lease=[HELD(), (401, {"error": "unauthorized"})])
    command, marker = _command(tmp_path, sleep=0.5)

    assert _run(["run", "gpu", "--", *command], daemon) == 0

    assert marker.exists()
    assert capsys.readouterr().err.count("no longer shows") == 1
    assert daemon.actions().count("lease") == 2
    assert daemon.actions()[-1] == "release"


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
    command, marker = _command(tmp_path, sleep=0.1)
    seen = []

    def slow_renewal(request):
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
    assert daemon.actions()[-1] == "release"
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
    assert daemon.actions()[-1] == "release"
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
    assert daemon.actions()[-1] == "release"


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
    assert daemon.actions()[-1] == "release"
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
