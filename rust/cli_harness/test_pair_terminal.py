"""Real controlling-terminal interruption; set R1_PAIR_CANDIDATE for native proof."""
import os
from pathlib import Path
import select
import signal
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX controlling-terminal regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
def test_interrupt_restores_terminal_echo(tmp_path, arm):
    import pty
    import termios

    candidate = os.environ.get("R1_PAIR_CANDIDATE")
    if arm == "rust" and not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    command = ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
               else [candidate])
    command += ["pair", "http://localhost:1", "--read-code", "--token-file", str(tmp_path / "token")]
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(Path(__file__).resolve().parents[2])
        env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
        env.update(HOME=str(tmp_path), PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"))
        os.execvpe(command[0], command, env)
    status = None
    try:
        before = termios.tcgetattr(master)
        output = b""
        deadline = time.monotonic() + 15
        while b"pairing code: " not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert b"pairing code: " in output, output
        assert not termios.tcgetattr(master)[3] & termios.ECHO
        os.write(master, b"\x03")
        while time.monotonic() < deadline:
            waited, status_value = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = status_value
                break
            time.sleep(0.01)
        assert status is not None, "interrupted prompt did not exit"
        assert termios.tcgetattr(master) == before, "terminal modes were not restored"
        assert os.waitstatus_to_exitcode(status) in (-signal.SIGINT, 130)
        assert not (tmp_path / "token").exists()
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
