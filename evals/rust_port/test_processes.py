"""Actual child processes must not survive an owned invocation."""
import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from evals.rust_port.harness import isolated_env, run_cli
from evals.rust_port import processes


@pytest.mark.parametrize("mode", ["timeout", "parent_exit", "pipe_holder"])
def test_cli_reclaims_descendant_even_after_parent_exit(tmp_path, mode):
    marker = "rust-port-owned-child-" + tmp_path.name
    pid_file = tmp_path / "child.json"
    child = "import time; time.sleep(4)"
    redirect = "" if mode == "pipe_holder" else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    parent = ("import json,subprocess,sys,time; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r},{marker!r}],stdin=subprocess.DEVNULL{redirect}); "
              f"open({str(pid_file)!r},'w').write(json.dumps(p.pid)); "
              + ("time.sleep(4)" if mode == "timeout" else ""))
    started = time.monotonic()
    try:
        if mode == "parent_exit":
            result = run_cli([sys.executable], ["-c", parent], cwd=tmp_path,
                             env=isolated_env(tmp_path / "home"), timeout=0.75)
            assert result["exit_code"] == 0
        else:
            with pytest.raises(subprocess.TimeoutExpired):
                run_cli([sys.executable], ["-c", parent], cwd=tmp_path,
                        env=isolated_env(tmp_path / "home"), timeout=0.75)
        elapsed = time.monotonic() - started
        assert pid_file.exists(), "spawning parent must execute before timeout"
        pid = json.loads(pid_file.read_text())
        try:
            process = psutil.Process(pid)
            process.wait(timeout=1)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            # An orphan's POSIX exit can precede init reaping its zombie.
            if process.status() != psutil.STATUS_ZOMBIE:
                pytest.fail("owned descendant survived invocation")
        assert elapsed < 2.5, "held output pipes must not extend the timeout to child lifetime"
        assert run_cli([sys.executable], ["-c", "print('next')"], cwd=tmp_path,
                       env=isolated_env(tmp_path / "next-home"), timeout=5)["exit_code"] == 0
    finally:
        # A RED regression owns its leftovers too; never discover unrelated PIDs.
        if pid_file.exists():
            try:
                process = psutil.Process(json.loads(pid_file.read_text()))
                if process.status() != psutil.STATUS_ZOMBIE and marker in process.cmdline():
                    process.kill()
                    process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows job ownership failure")
def test_unavailable_windows_job_refuses_before_launch(tmp_path, monkeypatch):
    from pseudolife_memory import codex_doorbell
    monkeypatch.setattr(codex_doorbell._KillJob, "create", classmethod(lambda cls: None))
    launched = []
    monkeypatch.setattr(processes.subprocess, "Popen", lambda *a, **k: launched.append(True))
    with pytest.raises(OSError, match="ownership unavailable"):
        run_cli([sys.executable], ["-c", "print('never')"], cwd=tmp_path,
                env=isolated_env(tmp_path / "home"), timeout=5)
    assert launched == []


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended assignment failure")
@pytest.mark.parametrize("failure", ["assignment", "resume"])
def test_windows_assignment_or_resume_failure_never_runs_unowned_code(tmp_path, monkeypatch, failure):
    from pseudolife_memory import codex_doorbell
    from types import SimpleNamespace
    actual = codex_doorbell._kernel32

    class FailingAssignment:
        def __getattr__(self, name):
            return (lambda *a: 0) if name == "AssignProcessToJobObject" else getattr(actual, name)

    if failure == "assignment":
        monkeypatch.setattr(codex_doorbell, "_kernel32", FailingAssignment())
        monkeypatch.setattr(codex_doorbell, "_ntdll", SimpleNamespace(
            NtResumeProcess=lambda *a: pytest.fail("unassigned process must remain suspended")))
    else:
        monkeypatch.setattr(codex_doorbell, "_ntdll", SimpleNamespace(NtResumeProcess=lambda *a: -1))
    launched = []
    actual_popen = subprocess.Popen

    def capture(*a, **k):
        process = actual_popen(*a, **k)
        launched.append(process)
        return process

    monkeypatch.setattr(processes.subprocess, "Popen", capture)
    sentinel = tmp_path / "must-not-execute"
    with pytest.raises(OSError, match="assignment unavailable|resume failed"):
        run_cli([sys.executable], ["-c", f"open({str(sentinel)!r},'w').write('executed')"],
                cwd=tmp_path, env=isolated_env(tmp_path / "home"), timeout=5)
    assert len(launched) == 1
    assert launched[0].wait(timeout=0) is not None
    assert launched[0].owned_cleanup == {"process_stopped": True, "subtree_stopped": True}
    assert not sentinel.exists()


def test_retained_popen_does_not_hold_child_output_file(tmp_path):
    log = tmp_path / "child-output"
    with log.open("wb") as output:
        with processes.owned_process([sys.executable, "-c", "print('complete')"],
                                     cwd=tmp_path, env=isolated_env(tmp_path / "home"),
                                     stdout=output, stderr=output) as process:
            assert process.wait(timeout=5) == 0
    assert process.owned_cleanup == {"process_stopped": True, "subtree_stopped": True}
    log.unlink()
    assert not log.exists()


def test_owned_runtime_pid_membership_accepts_real_runtime_and_rejects_other_pids(tmp_path):
    code = "import os,sys;print(os.getpid(),flush=True);sys.stdin.buffer.read()"
    with processes.owned_process([sys.executable, "-c", code], cwd=tmp_path,
                                 env=isolated_env(tmp_path / "home"), stdin=subprocess.PIPE) as owner:
        runtime_pid = int(owner.stdout.readline())
        assert owner.owns_runtime_pid(runtime_pid) is True
        # On Windows a venv redirector owns the actual CPython runtime.
        if os.name == "nt" and sys.prefix != sys.base_prefix:
            assert runtime_pid != owner.pid
        for other in (os.getpid(), runtime_pid + 1, runtime_pid + 2, runtime_pid + 3,
                      2 ** 32 - 1, True, False, 0, -1, 1.0, "1", None,
                      2 ** 32, runtime_pid + 2 ** 32):
            assert owner.owns_runtime_pid(other) is False
        owner.stdin.close()
        assert owner.wait(timeout=5) == 0
        assert owner.owns_runtime_pid(runtime_pid) is False
    assert owner.owns_runtime_pid(runtime_pid) is False
    assert owner.owned_cleanup == {"process_stopped": True, "subtree_stopped": True}


@pytest.mark.skipif(os.name != "nt", reason="Windows native PID membership")
def test_windows_membership_query_failure_refuses_runtime(tmp_path, monkeypatch):
    code = "import os,sys;print(os.getpid(),flush=True);sys.stdin.buffer.read()"
    with processes.owned_process([sys.executable, "-c", code], cwd=tmp_path,
                                 env=isolated_env(tmp_path / "home"), stdin=subprocess.PIPE) as owner:
        runtime_pid = int(owner.stdout.readline())
        def unavailable():
            raise OSError("synthetic native query unavailable")
        monkeypatch.setattr(processes, "_windows_membership_api", unavailable)
        assert owner.owns_runtime_pid(runtime_pid) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows exit code 259 ambiguity")
def test_terminated_runtime_exit_code_259_is_not_alive(tmp_path):
    code = "import os,sys;print(os.getpid(),flush=True);sys.stdin.buffer.read();sys.exit(259)"
    with processes.owned_process([sys.executable, "-c", code], cwd=tmp_path,
                                 env=isolated_env(tmp_path / "home"), stdin=subprocess.PIPE) as owner:
        runtime_pid = int(owner.stdout.readline())
        owner.stdin.close()
        assert owner.wait(timeout=5) == 259
        assert owner.owns_runtime_pid(runtime_pid) is False
