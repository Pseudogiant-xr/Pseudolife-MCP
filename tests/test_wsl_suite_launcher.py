"""ops/wsl-suite.sh: its guard for runs dispatched to a second machine, and
its sweep of the processes a run leaves behind.

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

import os
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
