"""Start and stop the two daemons under test on disposable homes.

The Python oracle runs from the checkout (``python -m pseudolife_memory.cli
serve``); the Rust daemon runs the built binary. Both get the same
environment: a disposable data directory (``PSEUDOLIFE_MCP_DATA_DIR``), a
disposable home, no GPU, offline model hub, and the scenario's own env vars.
Processes are stopped by PID only.
"""
from __future__ import annotations

import http.client
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

# Environment variables the daemons read that must never leak in from the
# caller's shell (a real token, a real bank, a real config).
_SCRUB_PREFIXES = ("PSEUDOLIFE_",)
_KEEP = {"PSEUDOLIFE_TEST_PG_HOST_PORT", "PSEUDOLIFE_TEST_PG_LOGIN_FILE"}


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Daemon:
    def __init__(self, kind: str, argv: list[str], env: dict[str, str], cwd: Path,
                 port: int, log: Path):
        self.kind, self.argv, self.env, self.cwd, self.port, self.log = kind, argv, env, cwd, port, log
        self.proc: subprocess.Popen | None = None

    def start(self, wait_s: float = 120.0, expect_exit: bool = False) -> "Daemon":
        self._out = open(self.log, "wb")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd, stdout=self._out,
                                     stderr=subprocess.STDOUT, creationflags=flags)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                if expect_exit:
                    return self
                raise RuntimeError(f"{self.kind} daemon exited {self.proc.returncode}; "
                                   f"log {self.log}:\n{self.log.read_text(errors='replace')[-3000:]}")
            if expect_exit:
                time.sleep(0.2)
                continue
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                c.request("GET", "/health")
                c.getresponse().read()
                return self
            except OSError:
                time.sleep(0.2)
        if expect_exit:
            raise RuntimeError(f"{self.kind} daemon did not exit within {wait_s}s")
        self.stop()
        raise RuntimeError(f"{self.kind} daemon did not answer /health within {wait_s}s; log {self.log}")

    def wait_exit(self, timeout: float = 60.0) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout=timeout)

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            if os.name == "nt":
                # Whole tree of this PID only (uvicorn has no children, but be exact).
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True)
            else:
                self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._out.close()
        self.proc = None


def base_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_SCRUB_PREFIXES) or k in _KEEP}
    # The model cache stays the caller's (read-only use); home, config, data
    # and state are disposable.
    hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HF_HOME": hf_home,
        "CUDA_VISIBLE_DEVICES": "-1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PYTHONUTF8": "1",
        "PSEUDOLIFE_MCP_DATA_DIR": str(home / "data"),
    })
    env.update(extra)
    return env


def make_home(root: Path, name: str, config_yaml: str | None) -> Path:
    home = root / name
    if home.exists():
        shutil.rmtree(home)
    (home / "data").mkdir(parents=True)
    if config_yaml is not None:
        (home / "data" / "config.yaml").write_text(config_yaml, encoding="utf-8")
    return home


def python_daemon(home: Path, port: int, env: dict[str, str]) -> Daemon:
    env = dict(env)
    env.setdefault("PSEUDOLIFE_MCP_PORT", str(port))  # a refusal case may set its own
    return Daemon("python", [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
                  dict(env, PYTHONPATH=str(REPO)), home, port, home / "daemon.log")


def rust_daemon(binary: Path, home: Path, port: int, env: dict[str, str]) -> Daemon:
    env = dict(env)
    env.setdefault("PSEUDOLIFE_MCP_PORT", str(port))  # a refusal case may set its own
    return Daemon("rust", [str(binary)], env, home, port, home / "daemon.log")


def scratch_root() -> Path:
    root = Path(os.environ.get("PL_HARNESS_SCRATCH") or Path(tempfile.gettempdir()) / "pl-w1a-harness")
    root.mkdir(parents=True, exist_ok=True)
    return root
