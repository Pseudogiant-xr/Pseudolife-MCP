"""Real Python daemons on disposable banks, one per arm, shared by a row.

Each arm gets its own daemon on its own bank (``pl_cf_w1b_<row>_<arm>``),
started once and reused across the row's cases, so the two banks receive the
same operation sequence and can be compared after every case. The daemon is
the oracle checkout's ``pseudolife-mcp serve``, tokenless on loopback, with
the cached embedding model read offline and the GPU hidden. Banks are created
and dropped through the test login only (``_bank.py``).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from . import _bank
from ..core import PLATFORM

ORACLE: dict = {"python": None, "source": None}
_POOL: dict[str, "RealDaemon"] = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hf_home() -> str:
    return os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")


class RealDaemon:
    def __init__(self, bank: str):
        self.bank = bank
        self.data = Path(tempfile.mkdtemp(prefix="pl-cli-daemon-"))
        _bank.create(bank, schema=False)
        port = _free_port()
        self.url = f"http://127.0.0.1:{port}"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PSEUDOLIFE", "CLAUDE"))}
        env.update({
            "PSEUDOLIFE_MCP_HOST": "127.0.0.1", "PSEUDOLIFE_MCP_PORT": str(port),
            "PSEUDOLIFE_MCP_DATABASE_URL": _bank.url(bank),
            "PSEUDOLIFE_MCP_DATA_DIR": str(self.data), "PSEUDOLIFE_RELEASE_CHECK": "0",
            "HF_HUB_OFFLINE": "1", "HF_HOME": _hf_home(), "CUDA_VISIBLE_DEVICES": "-1",
            "PYTHONPATH": str(ORACLE["source"]), "HOME": str(self.data),
            "USERPROFILE": str(self.data),
        })
        self._log = open(self.data / "daemon.log", "wb")
        self.proc = None
        try:
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(
                [ORACLE["python"], "-P", "-m", "pseudolife_memory.cli", "serve"],
                env=env, cwd=str(ORACLE["source"]), stdout=self._log,
                stderr=subprocess.STDOUT, creationflags=creation)
            self._await_health()
        except BaseException:
            # Not yet in the pool, so nothing else would stop it or drop its bank.
            self.shutdown()
            raise

    def _await_health(self) -> None:
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("daemon exited:\n" + self.tail())
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        return
            except Exception:  # noqa: BLE001 — not up yet
                pass
            time.sleep(0.5)
        raise RuntimeError("daemon never became healthy:\n" + self.tail())

    def settle(self, fast: float = 0.08, streak: int = 5, limit: float = 90.0) -> None:
        """Wait until ``/health`` answers well inside the CLIs' 0.25 s probe
        budget several times running: a daemon still warming its model can
        miss that budget, and both CLIs then stay silent by contract, which
        would make a bank case's outcome depend on load, not on the port."""
        deadline, run = time.time() + limit, 0
        while run < streak:
            if time.time() > deadline:
                raise RuntimeError("daemon never settled under the probe budget:\n"
                                   + self.tail())
            started = time.monotonic()
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=1) as r:
                    r.read()
                run = run + 1 if time.monotonic() - started < fast else 0
            except Exception:  # noqa: BLE001
                run = 0
            time.sleep(0.05)

    def tail(self) -> str:
        return (self.data / "daemon.log").read_text(errors="replace")[-3000:]

    # The fixture-daemon interface core.run_arm uses.
    def requests(self) -> list:
        return []

    def close(self) -> None:
        pass  # shared across the row; shutdown() at exit

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(20)
        self._log.close()
        _bank.drop(self.bank)
        shutil.rmtree(self.data, ignore_errors=True)


def shared(row: str):
    """A per-arm factory: ``core.run_arm`` calls it with the arm's name."""
    def factory(arm: str) -> RealDaemon:
        key = f"{row}_{arm}"
        if key not in _POOL:
            # Per host and process: two harness runs (say Windows and WSL)
            # share the bench server and must never drop each other's banks.
            _POOL[key] = RealDaemon(f"pl_cf_w1b_{key}_{PLATFORM[:3]}{os.getpid()}")
        _POOL[key].settle()
        return _POOL[key]
    factory.per_arm = True
    return factory


@atexit.register
def _shutdown_all() -> None:
    while _POOL:
        _POOL.popitem()[1].shutdown()


def rows(bank: str, query: str, params: tuple) -> list:
    """Rows as JSON text, in a stable order, from one arm's bank."""
    with _bank.connect(bank, autocommit=True) as conn:
        return [r[0] for r in conn.execute(query, params)]
