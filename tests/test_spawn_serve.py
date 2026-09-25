"""``tests.helpers.spawn_serve`` ends up talking to the daemon it started.

A port from ``free_port()`` is only free until something else binds it, and
about twenty sessions run tests on the maintainer's host at once. The
daemon then either fails its bind and exits, or, while it is still booting,
another process answers /health on its port. These tests take the port
first and drive the helper with a stand-in daemon, a few lines of uvicorn,
so a test costs about a second rather than a real daemon's cold start.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import pytest

from tests import helpers

# Serves /health the way the daemon does, after an optional delay, so the
# helper's poll can meet a foreign server before this one has bound.
_STAND_IN = """
import json, os, time
import uvicorn

time.sleep(float(os.environ.get("STAND_IN_BIND_DELAY", "0")))


async def app(scope, receive, send):
    body = json.dumps({"status": "ok", "stand_in": os.getpid()}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


uvicorn.run(app, host=os.environ["PSEUDOLIFE_MCP_HOST"],
            port=int(os.environ["PSEUDOLIFE_MCP_PORT"]),
            log_level="warning", lifespan="off")
"""


def _first_port(monkeypatch, taken: int) -> None:
    """Make the helper's first pick the port the test already holds."""
    real = helpers.free_port
    picks = iter([taken])
    monkeypatch.setattr(helpers, "free_port", lambda: next(picks, None) or real())


def _started_by(proc, pid: int) -> bool:
    family = psutil.Process(proc.pid)
    return pid in {proc.pid, *(c.pid for c in family.children(recursive=True))}


def test_a_daemon_that_loses_its_port_is_started_again_on_another(
        tmp_path, monkeypatch):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        taken = holder.getsockname()[1]
        _first_port(monkeypatch, taken)
        proc, health, port = helpers.spawn_serve(
            tmp_path, "unused", argv=("-c", _STAND_IN), wait_s=30)
        try:
            assert port != taken
            assert _started_by(proc, health["stand_in"])
            assert "error while attempting to bind" in (
                tmp_path / "daemon-stderr-1.log").read_text(errors="replace")
        finally:
            helpers.stop_daemon(proc)


def test_a_foreign_server_on_the_port_is_not_taken_for_the_daemon(
        tmp_path, monkeypatch):
    class Foreign(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"status": "ok", "foreign": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    foreign = ThreadingHTTPServer(("127.0.0.1", 0), Foreign)
    worker = threading.Thread(target=foreign.serve_forever, daemon=True)
    worker.start()
    try:
        _first_port(monkeypatch, foreign.server_port)
        proc, health, port = helpers.spawn_serve(
            tmp_path, "unused", argv=("-c", _STAND_IN), wait_s=30,
            env_extra={"STAND_IN_BIND_DELAY": "1"})
        try:
            assert "foreign" not in health, (
                "the helper returned another process's /health as the daemon's")
            assert port != foreign.server_port
            assert _started_by(proc, health["stand_in"])
        finally:
            helpers.stop_daemon(proc)
    finally:
        foreign.shutdown()
        foreign.server_close()
        worker.join(timeout=5)


def test_the_daemon_runs_the_checkout_under_test(tmp_path, monkeypatch):
    """Whatever directory pytest was started from. ``-m`` resolves the
    package from the working directory first; from elsewhere, a machine
    with an editable install of another checkout (the maintainer's global
    Python has one) would run that checkout's daemon instead."""
    from pathlib import Path

    import pseudolife_memory

    reports_package = ("import sys, pseudolife_memory; "
                       "sys.stderr.write(pseudolife_memory.__file__); "
                       "sys.exit(3)")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError) as failed:
        helpers.spawn_serve(tmp_path, "unused", wait_s=30,
                            argv=("-c", reports_package))
    imported = str(failed.value).rsplit("\n", 1)[-1]
    assert Path(imported).resolve() == Path(pseudolife_memory.__file__).resolve()


def test_the_runs_own_database_is_refused_before_any_spawn(
        tmp_path, monkeypatch):
    """Every daemon fixture, present or future: in-process tests hold the
    run database's writer lease, so a daemon there boots degraded."""
    import subprocess

    from tests import pg_fixtures
    from tests.pg_defaults import conninfo_with_dbname, default_admin_url

    run_db = conninfo_with_dbname(default_admin_url(),
                                  pg_fixtures._target_db_name())
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail(
        "spawned a daemon on the run's own database"))
    with pytest.raises(ValueError, match="run's own database"):
        helpers.spawn_serve(tmp_path, run_db)


def test_any_other_early_exit_fails_at_once_with_its_stderr(tmp_path):
    """Only a lost port is worth a second start; any other exit is a real
    daemon failure, reported with what it printed."""
    with pytest.raises(RuntimeError, match=r"exited early \(3\)[\s\S]*boom"):
        helpers.spawn_serve(
            tmp_path, "unused", wait_s=30,
            argv=("-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"))
    assert not (tmp_path / "daemon-stderr-2.log").exists()
