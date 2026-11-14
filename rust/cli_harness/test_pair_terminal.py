"""Real controlling-terminal interruption; set R1_PAIR_CANDIDATE for native proof."""
import os
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import threading

import pytest


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe interruption regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
@pytest.mark.parametrize("termination", ["SIGINT", "SIGTERM", "SIGHUP", "SIGKILL"])
def test_interrupt_with_open_partial_stdin(tmp_path, arm, termination):
    import array
    import fcntl
    import termios

    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if arm == "rust" and not candidate:
        pytest.skip("a native candidate is required")
    command = ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
               else [candidate])
    token = tmp_path / "token"
    command += ["pair", "http://localhost:1", "--read-code", "--token-file", str(token)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"))
    reader, writer = os.pipe()
    flags = fcntl.fcntl(reader, fcntl.F_GETFL)
    child = subprocess.Popen(command, stdin=reader, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, start_new_session=True,
                             cwd=Path(__file__).resolve().parents[2])
    try:
        os.write(writer, b"ABCD")  # No newline or EOF: the reader must still be waiting.
        deadline = time.monotonic() + 15
        while True:
            queued = array.array("i", [0])
            fcntl.ioctl(reader, termios.FIONREAD, queued, True)
            if queued[0] == 0:
                break  # The actual CLI consumed the partial line.
            assert child.poll() is None, child.communicate()
            assert time.monotonic() < deadline, "CLI never read its input"
            time.sleep(0.01)
        assert child.poll() is None, child.communicate()
        during = fcntl.fcntl(reader, fcntl.F_GETFL)
        signum = getattr(signal, termination)
        child.send_signal(signum)
        assert child.wait(timeout=3) in (-signum, 128 + signum)
        assert fcntl.fcntl(reader, fcntl.F_GETFL) == flags
        assert during == flags, "reading a partial line changed the shared pipe flags"
        assert not token.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
        os.close(reader)
        os.close(writer)


@pytest.mark.skipif(os.name != "posix", reason="POSIX controlling-terminal regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
@pytest.mark.parametrize("phase", ["prompt", "health", "redemption"])
def test_interrupt_restores_terminal_echo(tmp_path, arm, phase):
    import pty
    import termios

    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if arm == "rust" and not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    command = ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
               else [candidate])
    from .fixture_daemon import FixtureDaemon, json_body
    received, release = threading.Event(), threading.Event()

    def blocked(_request):
        received.set()
        release.wait(20)
        return json_body({})

    daemon = None
    url = "http://localhost:1"
    if phase != "prompt":
        daemon = FixtureDaemon({
            "/health": blocked if phase == "health" else json_body({"status": "ok", "auth": True}),
            "/api/pair": blocked,
        })
        daemon.server.handle_error = lambda *_: None  # The interrupted client closes its socket.
        url = daemon.url
    command += ["pair", url, "--read-code", "--token-file", str(tmp_path / "token")]
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
        if phase != "prompt":
            os.write(master, b"ABCD-EFGH-JKMN\n")
            assert received.wait(5), "pairing never reached the blocked request"
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
        assert (tmp_path / "token").exists() == (phase == "redemption")
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
        release.set()
        if daemon:
            daemon.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows console regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
@pytest.mark.parametrize("action", ["interrupt", "backspace"])
def test_windows_prompt_restores_console_modes(tmp_path, arm, action):
    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if arm == "rust" and not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    command = ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
               else [candidate])
    token = tmp_path / "token"
    command += ["pair", "http://localhost:1", "--read-code", "--token-file", str(token)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"))
    result_path = tmp_path / "console-result.json"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    helper = Path(__file__).with_name("_windows_pair_console.py")
    # Keep the test runner and every existing console out of the signal target.
    result = subprocess.run(
        [sys._base_executable, str(helper), str(result_path), str(token), action, *command],
        env=env, cwd=Path(__file__).resolve().parents[2], startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE, timeout=45,
    )
    assert result.returncode == 0
    observation = json.loads(result_path.read_text(encoding="utf-8"))
    assert "error" not in observation, observation
    assert observation["isolated_console"]
    assert not observation["during"] & 4, observation
    assert observation["after"] == observation["before"], observation
    assert not observation["token_exists"], observation
    if action == "interrupt":
        assert observation["exit_code"] in (130, 0xC000013A), observation
    else:
        assert observation["exit_code"] == 2, observation
        assert observation["empty_code"], observation


def _connect_home(tmp_path: Path, url: str) -> tuple[dict, Path]:
    """A throwaway home holding one Claude Code registration of the shim
    that names ``url``, and the environment that points every lookup at it."""
    registration = tmp_path / ".claude.json"
    registration.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": str(tmp_path / "shim-bin"),
        "env": {"PSEUDOLIFE_MCP_DAEMON_URL": url}}}}, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "cwd").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PSEUDOLIFE_")}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / ".config"),
               XDG_DATA_HOME=str(tmp_path / ".local" / "share"),
               CODEX_HOME=str(tmp_path / ".codex"),
               PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "data"),
               # The run's cwd is inside the home, so no project-scope client
               # file of this checkout is read; the oracle imports from here.
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    return env, registration


def _converse(command: list[str], env: dict, cwd: Path, steps: list[tuple[bytes, bytes]]):
    """Run ``command`` on a pseudo-terminal: wait for each prompt, record
    whether echo was off while it waited, then answer it. The output, the
    echo states and the exit code."""
    import pty
    import termios

    pid, master = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(command[0], command, env)
    output, echoes, status = b"", [], None
    try:
        deadline = time.monotonic() + 30
        for prompt, answer in steps:
            while prompt not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        break
            assert prompt in output, output
            echoes.append(bool(termios.tcgetattr(master)[3] & termios.ECHO))
            os.write(master, answer)
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                output += chunk
            waited, value = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = value
                break
        assert status is not None, output
        return output, echoes, os.waitstatus_to_exitcode(status)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)


def _connect_command(arm: str) -> list[str]:
    candidate = os.environ.get("R1_PAIR_CANDIDATE") or os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if arm == "rust" and not candidate:
        pytest.skip("R1_PAIR_CANDIDATE is required for the native arm")
    return ([sys.executable, "-m", "pseudolife_memory.cli"] if arm == "python"
            else [candidate]) + ["connect"]


CODE = b"ABCD-EFGH-JKMN"


@pytest.mark.skipif(os.name != "posix", reason="POSIX controlling-terminal regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
@pytest.mark.parametrize("answer", [b"n\n", b"\n", b"y\n"])
def test_connect_reads_the_code_without_echo_and_redeems_only_after_yes(tmp_path, arm, answer):
    from .fixture_daemon import FixtureDaemon, json_body
    daemon = FixtureDaemon({
        "/health": json_body({"status": "ok", "auth": True}),
        "/api/pair": json_body({"principal": "laptop", "tier": "writer", "bank": "b1"}),
        "/api/episodes": json_body({"episodes": []}),
    })
    try:
        env, registration = _connect_home(tmp_path, "http://10.0.0.5:8765")
        before = registration.read_bytes()
        command = _connect_command(arm) + [daemon.url, "--client", "claude-code", "--read-code"]
        output, echoes, code = _converse(command, env, tmp_path / "cwd", [
            (b"pairing code: ", CODE + b"\n"),
            (b"Apply these changes? [y/N] ", answer),
        ])
        # The code prompt hides what is typed; the question echoes it.
        assert echoes == [False, True], (echoes, output)
        assert CODE.lower() not in output.lower(), output
        pairs = [r for r in daemon.requests() if r["target"] == "/api/pair"]
        if answer == b"y\n":
            # Redeemed once, after the yes; nothing here answers MCP, so the
            # verification fails and names the paired token file it kept.
            assert len(pairs) == 1, daemon.requests()
            assert code in (4, 5), output
            assert b"is kept" in output, output
        else:
            assert code == 2, output
            assert b"not confirmed; nothing was changed" in output, output
            assert pairs == [], daemon.requests()
            assert registration.read_bytes() == before
            assert not (tmp_path / ".pseudolife-mcp").exists()
    finally:
        daemon.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX controlling-terminal regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
def test_connect_asks_before_it_reads_a_token_without_echo(tmp_path, arm):
    from .fixture_daemon import FixtureDaemon, json_body
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": True})})
    try:
        env, registration = _connect_home(tmp_path, "http://10.0.0.5:8765")
        token = tmp_path / "chosen.token"
        secret = b"fixture-terminal-token-0123456789"
        command = _connect_command(arm) + [daemon.url, "--client", "claude-code",
                                           "--read-token", "--token-file", str(token)]
        output, echoes, code = _converse(command, env, tmp_path / "cwd", [
            (b"Apply these changes? [y/N] ", b"y\n"),
            (b"token: ", secret + b"\n"),
        ])
        assert echoes == [True, False], (echoes, output)
        assert secret not in output, output
        # Nothing answers MCP here, so the run stops at verification.
        assert code != 0, output
    finally:
        daemon.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX controlling-terminal regression")
@pytest.mark.parametrize("arm", ["python", "rust"])
def test_connect_stops_on_ctrl_c_at_the_question_after_a_code_prompt(tmp_path, arm):
    # The code prompt listens for SIGINT; on the native arm that listener
    # leaves Tokio's handler installed, which must not swallow a later Ctrl-C.
    from .fixture_daemon import FixtureDaemon, json_body
    daemon = FixtureDaemon({"/health": json_body({"status": "ok", "auth": True})})
    try:
        env, registration = _connect_home(tmp_path, "http://10.0.0.5:8765")
        before = registration.read_bytes()
        command = _connect_command(arm) + [daemon.url, "--client", "claude-code", "--read-code"]
        output, _, code = _converse(command, env, tmp_path / "cwd", [
            (b"pairing code: ", CODE + b"\n"),
            (b"Apply these changes? [y/N] ", b"\x03"),
        ])
        assert code in (130, -signal.SIGINT), output
        assert not [r for r in daemon.requests() if r["target"] == "/api/pair"]
        assert registration.read_bytes() == before
    finally:
        daemon.close()
