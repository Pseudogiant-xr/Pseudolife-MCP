"""Retain stdio frames without discarding wire order or JSON numeric types."""
from __future__ import annotations

import base64
import hashlib
import queue
import subprocess
import threading
import time
from pathlib import Path

from .harness import AbnormalTermination, isolated_env, strict_json_loads
from .processes import owned_process


class Wire:
    def __init__(self, process):
        self.process = process
        self.incoming = queue.Queue()
        self.frames = []
        self.stderr = bytearray()
        self.inputs = []
        self.readers = [threading.Thread(target=self._stdout),
                        threading.Thread(target=self._stderr)]
        for reader in self.readers:
            reader.start()

    def _stdout(self):
        for line in iter(self.process.stdout.readline, b""):
            self.frames.append(line)
            try:
                self.incoming.put(strict_json_loads(line))
            except Exception as error:
                self.incoming.put(error)
        self.incoming.put(None)

    def _stderr(self):
        self.stderr.extend(self.process.stderr.read())

    def send(self, frame):
        import json
        line = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.inputs.append(line)
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            frame = self.incoming.get(timeout=max(0.001, deadline - time.monotonic()))
            if frame is None:
                raise RuntimeError("EOF before required stdio frame")
            if isinstance(frame, Exception):
                raise RuntimeError("invalid JSON in stdio frame") from frame
            if predicate(frame):
                return frame

    def response(self, identifier, timeout=15):
        return self.until(lambda frame: frame.get("id") == identifier, timeout)

    def finish(self, timeout=15):
        self.process.stdin.close()
        self.process.wait(timeout=timeout)
        for reader in self.readers:
            reader.join(timeout=3)
            if reader.is_alive():
                raise RuntimeError("stdio reader did not stop")
        if type(self.process.returncode) is not int or not 0 <= self.process.returncode <= 255:
            raise AbnormalTermination("stdio shim exited abnormally")

    def transcript(self):
        frames = []
        for line in self.frames:
            try:
                frames.append(strict_json_loads(line))
            except ValueError as error:
                frames.append({"boundary_error": type(error).__name__})
        return {"stdin_frames_b64": [base64.b64encode(line).decode("ascii") for line in self.inputs],
                "stdout_frames_b64": [base64.b64encode(line).decode("ascii") for line in self.frames],
                "stdout": frames,
                "stderr_b64": base64.b64encode(self.stderr).decode("ascii"),
                "exit_code": self.process.returncode}


def shim_environment(home: Path, url: str, token: str | None = None):
    env = isolated_env(home)
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                "PSEUDOLIFE_AGENT_COORDINATION": "0", "PSEUDOLIFE_CODEX_DOORBELL": "0",
                "PSEUDOLIFE_RELEASE_CHECK": "0", "PYTHONDONTWRITEBYTECODE": "1",
                "PSEUDOLIFE_AGENT_STATE_DIR": str(home)})
    if token is not None:
        env["PSEUDOLIFE_MCP_TOKEN"] = token
    return env


def capture(command, *, cwd, home, url, exercise, token=None, timeout=15, env_extra=None, boundary_errors=False):
    env = shim_environment(Path(home), url, token)
    env.update(env_extra or {})
    wire = None
    try:
        with owned_process(command, cwd=cwd, env=env, stdin=subprocess.PIPE) as process:
            wire = Wire(process)
            exercise_error = None
            try:
                exercise(wire)
            except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, queue.Empty) as error:
                if not boundary_errors:
                    raise
                exercise_error = type(error).__name__
            shutdown_started = time.monotonic()
            wire.finish(timeout)
            result = wire.transcript()
            if exercise_error:
                result["boundary_error"] = exercise_error
            result["shutdown_seconds"] = time.monotonic() - shutdown_started
    finally:
        if wire is not None:
            for reader in wire.readers:
                reader.join(timeout=3)
                if reader.is_alive():
                    raise RuntimeError("stdio reader survived owned process cleanup")
    result["cleanup"] = dict(process.owned_cleanup)
    result["command_identity"] = {"executable_basename": Path(command[0]).name,
                                  "executable_sha256": hashlib.sha256(Path(command[0]).read_bytes()).hexdigest(),
                                  "arguments": command[1:] if command[1:] == ["-m", "pseudolife_memory.cli"] else None,
                                  "public_cli_module": command[1:] == ["-m", "pseudolife_memory.cli"]}
    if token and token.encode() in b"".join(wire.frames) + wire.stderr:
        raise RuntimeError("credential echo in stdio capture; transcript publication refused")
    return result
