"""Unattended refusal parity against isolated HTTP and platform-command fixtures.

These proofs never call host Docker, a scheduler or a bank. Set
PSEUDOLIFE_RUST_BINARY to the serialized-build candidate for the native arm.
Python remains the oracle, including for the cross-language update lock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {
    "current": 3,
    "knob-off": 4,
    "active": 4,
    "malformed-board": 4,
    "board-off": 4,
    "no-bearer": 4,
    "notice-refused": 4,
    "lock-held": 4,
    "moved-before-lock": 3,
    "missing-version": 2,
}


def _native():
    from .runner import _default_candidate
    candidate = Path(os.environ["PSEUDOLIFE_RUST_BINARY"]) if os.environ.get("PSEUDOLIFE_RUST_BINARY") else _default_candidate()
    if not candidate.is_file():
        pytest.fail("native unattended proof needs the serialized-build PSEUDOLIFE_RUST_BINARY candidate")
    return str(candidate.resolve())


def _environment(home, url, docker):
    from .core import _INHERIT
    env = {key: os.environ[key] for key in _INHERIT if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "roaming"),
               LOCALAPPDATA=str(home / "local"), XDG_DATA_HOME=str(home / "xdg-data"),
               XDG_CONFIG_HOME=str(home / "config"), CODEX_HOME=str(home / "codex"),
               CLAUDE_CONFIG_DIR=str(home / "claude"), GEMINI_CONFIG_DIR=str(home / "gemini"),
               PSEUDOLIFE_MCP_DATA_DIR=str(home / "data"), PSEUDOLIFE_DOCKER=str(docker),
               PSEUDOLIFE_MCP_DAEMON_URL=url, PSEUDOLIFE_MCP_TOKEN="fixture-bearer",
               PSEUDOLIFE_MCP_NO_SPAWN="1", PSEUDOLIFE_RELEASE_CHECK="0",
               PSEUDOLIFE_MCP_PYTHON="absent-fixture-python", PYTHONPATH=str(ROOT),
               PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="-1")
    return env


def _docker(home):
    path = home / ("fixture-docker.cmd" if os.name == "nt" else "fixture-docker")
    if os.name == "nt":
        path.write_bytes(b'@echo off\r\nif "%~1"=="inspect" (echo fixture-container& exit /b 0)\r\nexit /b 97\r\n')
    else:
        path.write_text('#!/bin/sh\nif [ "$1" = inspect ]; then printf "fixture-container\\n"; exit 0; fi\nexit 97\n', encoding="utf-8")
        path.chmod(0o700)
    return path


@contextmanager
def _endpoint(scenario):
    requests = []
    health_reads = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal health_reads
            requests.append(("GET", self.path))
            if self.path != "/health":
                return self.reply({}, 404)
            health_reads += 1
            version = "0.15.1" if scenario == "current" or scenario == "moved-before-lock" and health_reads > 1 else "0.15.0"
            reply = {"status": "ok", "version": version, "schema": 49,
                     "updates": {"unattended_daemon": scenario != "knob-off"}}
            if scenario == "missing-version":
                del reply["version"]
            self.reply(reply)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            assert length < 16384
            body = json.loads(self.rfile.read(length))
            requests.append(("POST", self.path))
            if self.path == "/api/coordination/register":
                return self.reply({"agent_id": "fixture-address", "credential": "fixture-key"})
            if self.path == "/api/coordination/agents":
                assert self.headers.get("X-PL-Agent-Key") == "fixture-key"
                if scenario == "malformed-board":
                    return self.reply({"agents": None})
                if scenario == "board-off":
                    return self.reply({"enabled": False})
                active = scenario in ("active", "notice-refused")
                return self.reply({"agents": [{"agent_id": "fixture-peer", "label": "fixture", "task": "working"}] if active else [],
                                   "idle_omitted": 3})
            if self.path == "/api/daemon-notice":
                assert isinstance(body.get("text"), str)
                if scenario == "notice-refused":
                    return self.reply({"error": "principal_not_allowed: fixture"}, 400)
                return self.reply({"recipients": 1})
            self.reply({}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _proof(tmp_path, scenario, command):
    home = tmp_path / "isolated home é"
    home.mkdir()
    docker = _docker(home)
    lock = None
    if scenario == "lock-held":
        from pseudolife_memory.os_lock import OsLock
        data = home / "data"
        data.mkdir()
        lock = OsLock(data / "update.lock")
        assert lock.acquire()
        (data / "update.lock.pid").write_text("4242\n", encoding="utf-8")
    try:
        with _endpoint(scenario) as (url, requests):
            env = _environment(home, url, docker)
            if scenario == "no-bearer":
                env.pop("PSEUDOLIFE_MCP_TOKEN")
            result = home / "result"
            output = subprocess.run([*command, "update", "--unattended", "--tag", "0.15.1",
                                     "--daemon-url", url, "--result-file", str(result), "--json"],
                                    env=env, cwd=home, capture_output=True, timeout=30,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert output.returncode == SCENARIOS[scenario], output.stderr.decode("utf-8", errors="replace")
            # The oracle's failure tee prints its stopped line even in JSON
            # mode. Compare the report and exit contract without changing it.
            report = json.loads(output.stdout[output.stdout.index(b"{"):])
            assert report["exit_code"] == SCENARIOS[scenario]
            assert result.read_text(encoding="utf-8").strip() == str(SCENARIOS[scenario])
            assert b"fixture-bearer" not in output.stdout + output.stderr
            assert (home / "data" / "unattended-update.log").is_file()
            # No transaction, backup or runtime artifact may be left by a hold.
            assert not (home / "data" / "compose").exists()
            assert not (home / "xdg-data" / "pseudolife-mcp" / "runtimes").exists()
            if scenario in ("current", "knob-off", "missing-version"):
                assert not any(verb == "POST" for verb, _ in requests)
            if scenario in ("active", "malformed-board", "board-off", "no-bearer", "notice-refused", "lock-held"):
                assert ("POST", "/api/daemon-notice") in requests
            return {key: report.get(key) for key in ("exit_code", "current", "target")}
    finally:
        if lock is not None:
            lock.release()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_oracle_unattended_refusal_contract(tmp_path, scenario):
    _proof(tmp_path, scenario, [sys.executable, "-P", "-m", "pseudolife_memory.cli"])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_native_unattended_refusal_parity(tmp_path, scenario):
    native = _native()
    oracle_home, native_home = tmp_path / "oracle", tmp_path / "native"
    oracle_home.mkdir()
    native_home.mkdir()
    oracle = _proof(oracle_home, scenario, [sys.executable, "-P", "-m", "pseudolife_memory.cli"])
    actual = _proof(native_home, scenario, [native])
    assert actual == oracle
