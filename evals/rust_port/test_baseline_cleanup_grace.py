"""The baseline liveness observation waits briefly and still rejects survivors."""
from contextlib import contextmanager
import subprocess
import sys
import threading
import time

import psutil
import pytest

from evals.rust_baseline import test_baseline as baseline
from evals.rust_port.harness import isolated_env


@contextmanager
def disposable_child(tmp_path):
    pid_file = tmp_path / "child-pid"
    stop_file = tmp_path / "stop"
    command = ("import os,time\nfrom pathlib import Path\n"
               f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
               f"stop=Path({str(stop_file)!r})\n"
               "while not stop.exists(): time.sleep(0.01)\n")
    process = subprocess.Popen([sys.executable, "-c", command], cwd=tmp_path,
                               env=isolated_env(tmp_path / "home"),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists(), "disposable child did not start"
        assert int(pid_file.read_text()) == process.pid
        assert process.poll() is None
        yield process, pid_file, stop_file
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def observed_waits(monkeypatch):
    timeouts = []
    actual_wait = psutil.Process.wait

    def wait(child, timeout=None):
        timeouts.append(timeout)
        return actual_wait(child, timeout=timeout)

    monkeypatch.setattr(psutil.Process, "wait", wait)
    return timeouts


def test_observation_accepts_real_child_exit_during_grace(tmp_path, monkeypatch):
    timeouts = observed_waits(monkeypatch)
    with disposable_child(tmp_path) as (process, pid_file, stop_file):
        release = threading.Timer(0.2, lambda: stop_file.write_text("stop"))
        release.start()
        try:
            baseline.BaselineTests().assert_child_stopped(pid_file)
        finally:
            release.cancel()
            release.join(timeout=2)
        assert timeouts == [2]
        assert process.wait(timeout=0) == 0


def test_observation_rejects_real_survivor_and_cleans_exact_child(tmp_path, monkeypatch):
    timeouts = observed_waits(monkeypatch)
    with disposable_child(tmp_path) as (process, pid_file, _):
        started = time.monotonic()
        with pytest.raises(AssertionError, match="owned descendant survived"):
            baseline.BaselineTests().assert_child_stopped(pid_file)
        assert timeouts == [2, 5]
        assert 1.8 <= time.monotonic() - started < 4
        assert process.wait(timeout=0) is not None
