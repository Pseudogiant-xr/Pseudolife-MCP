"""The daemon's exit save runs when it is stopped with SIGTERM.

``mcp_server`` registers the exit save with ``atexit``, but uvicorn (0.29
and later) re-raises a captured SIGTERM after its graceful shutdown, with
the default handler restored, so a daemon outside a container died without
running atexit handlers: every change since the last autosave (the
``cms_counters`` row, access counts, dirty cortex/world/lesson slots) was
lost on each stop. And a client holding its streamable-HTTP stream open kept
uvicorn waiting for that connection forever, so a ``docker stop`` ended in
SIGKILL before any save. The save now runs from the app's lifespan shutdown,
which uvicorn reaches before the re-raise and, with a bounded graceful
shutdown, even while a stream stays open; atexit stays as the fallback, and
the save runs once.

The subprocess tests start a real ``pseudolife-mcp serve`` on a bank of
their own (so the bank outlives the daemon and its ``cms_counters`` row can
be read after the stop) and are POSIX-only: on Windows a test can only stop
the daemon with TerminateProcess, which gives the process no signal to
handle.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import subprocess

import pytest

from tests.helpers import pg_reachable, private_bank, spawn_serve, stop_daemon

_TOKEN = "exit-save-test-token"
_FLUSH_LINE = "durability: flushed changed CMS on exit"

posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="SIGTERM is POSIX: on Windows a test stops the daemon with "
           "TerminateProcess, which gives it no signal to handle")


# ---------------------------------------------------------------------------
# A real daemon, stopped with SIGTERM
# ---------------------------------------------------------------------------

def _counter_rows(bank_url) -> int:
    import psycopg

    with psycopg.connect(bank_url, connect_timeout=5) as conn:
        return conn.execute("SELECT count(*) FROM cms_counters").fetchone()[0]


async def _store(url: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client, streamable_http_client)

    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool("memory_store", {
                    "text": "the exit save test stored this before SIGTERM",
                    "source": "exit-save-test"})
                assert not result.is_error, result


def _post(port: int, body: dict, session: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {_TOKEN}"}
    if session:
        headers["mcp-session-id"] = session
    try:
        conn.request("POST", "/mcp", json.dumps(body).encode(), headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status, resp.getheader("mcp-session-id")
    finally:
        conn.close()


def _open_event_stream(port: int) -> http.client.HTTPConnection:
    """An MCP session whose server-to-client event stream stays open, the
    way a connected shim holds one. Returns the open connection."""
    import mcp.types as types

    status, session = _post(port, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": types.LATEST_PROTOCOL_VERSION,
                   "capabilities": {},
                   "clientInfo": {"name": "exit-save-test", "version": "0"}}})
    assert status == 200 and session, (status, session)
    status, _ = _post(port, {"jsonrpc": "2.0",
                             "method": "notifications/initialized"}, session)
    assert status in (200, 202), status
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", "/mcp", headers={
        "Accept": "text/event-stream", "mcp-session-id": session,
        "Authorization": f"Bearer {_TOKEN}"})
    resp = conn.getresponse()
    assert resp.status == 200, resp.status
    return conn


def _stderr(data_dir) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(data_dir.glob("daemon-stderr-*.log")))


def _sigterm_after_a_store(tmp_path, label: str, *, hold_stream: bool):
    """Start a daemon, store one memory, SIGTERM it and wait for it to exit.

    Returns ``(rows_before, rows_after, returncode, stderr)``. The autosave
    interval is pushed past the test's length, so only the exit save can
    write the ``cms_counters`` row.
    """
    from tests.pg_fixtures import resolve_test_db_url

    url = resolve_test_db_url()
    if not pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with private_bank(url, label) as bank_url:
        proc, _health, port = spawn_serve(data_dir, bank_url, env_extra={
            "PSEUDOLIFE_MCP_TOKEN": _TOKEN,
            "PSEUDOLIFE_MCP_TOKENS": None,
            "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "100000"})
        stream = None
        try:
            asyncio.run(_store(f"http://127.0.0.1:{port}"))
            rows_before = _counter_rows(bank_url)
            if hold_stream:
                stream = _open_event_stream(port)
            proc.send_signal(signal.SIGTERM)
            try:
                returncode = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                returncode = None
            rows_after = _counter_rows(bank_url)
        finally:
            if stream is not None:
                stream.close()
            if proc.poll() is None:
                stop_daemon(proc)
        return rows_before, rows_after, returncode, _stderr(data_dir)


@posix_only
def test_sigterm_runs_the_exit_save(tmp_path):
    before, after, returncode, stderr = _sigterm_after_a_store(
        tmp_path, "exitsave", hold_stream=False)
    assert before == 0, "an autosave wrote the row first: the test proves nothing"
    assert returncode is not None, "the daemon did not exit within 60 s of SIGTERM"
    assert after == 1, "SIGTERM skipped the exit save:\n" + stderr[-3000:]
    assert stderr.count(_FLUSH_LINE) == 1, stderr[-3000:]


@posix_only
def test_sigterm_saves_while_a_client_holds_its_event_stream_open(tmp_path):
    """A connected shim keeps the session's GET event stream open. Without a
    bounded graceful shutdown uvicorn waited for that connection forever,
    and ``docker stop`` ended in SIGKILL with nothing saved."""
    before, after, returncode, stderr = _sigterm_after_a_store(
        tmp_path, "exitsavestream", hold_stream=True)
    assert before == 0, "an autosave wrote the row first: the test proves nothing"
    assert returncode is not None, (
        "the daemon was still waiting on the open stream 60 s after SIGTERM:\n"
        + stderr[-3000:])
    assert after == 1, "SIGTERM skipped the exit save:\n" + stderr[-3000:]
    assert stderr.count(_FLUSH_LINE) == 1, stderr[-3000:]


# ---------------------------------------------------------------------------
# In process: the lifespan hook and the once-only guard (any OS)
# ---------------------------------------------------------------------------

def test_the_exit_save_runs_once_however_many_paths_reach_it(monkeypatch):
    """The lifespan shutdown and atexit both call the exit save; a daemon
    that survives the re-raised signal (PID 1 in a container) or stops on
    SIGINT reaches both, and must save once."""
    from pseudolife_memory import mcp_server

    calls = []
    monkeypatch.setattr(mcp_server, "_exit_flush_done", False)
    monkeypatch.setattr(mcp_server.service, "autosave_if_changed",
                        lambda: calls.append(1) or {"saved_to": "test"})
    mcp_server.flush_on_exit()
    mcp_server.flush_on_exit()
    assert calls == [1]


def test_a_failed_exit_save_is_not_retried(monkeypatch):
    from pseudolife_memory import mcp_server

    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("database gone")

    monkeypatch.setattr(mcp_server, "_exit_flush_done", False)
    monkeypatch.setattr(mcp_server.service, "autosave_if_changed", broken)
    mcp_server.flush_on_exit()
    mcp_server.flush_on_exit()
    assert calls == [1]


def _drive_lifespan(app):
    """Run ``app`` through a lifespan startup and shutdown the way uvicorn
    does; returns the messages the app sent, in order."""
    incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent = []

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message["type"])

    asyncio.run(app({"type": "lifespan"}, receive, send))
    return sent


def _inner_lifespan_app(events, *, fail_shutdown: bool = False):
    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        events.append("inner shutdown")
        await send({"type": "lifespan.shutdown.failed" if fail_shutdown
                    else "lifespan.shutdown.complete"})
    return app


@pytest.mark.parametrize("fail_shutdown", [False, True])
def test_lifespan_shutdown_runs_the_save_after_the_app_shuts_down(fail_shutdown):
    from pseudolife_memory.daemon import save_on_lifespan_shutdown

    events = []
    app = save_on_lifespan_shutdown(
        _inner_lifespan_app(events, fail_shutdown=fail_shutdown),
        lambda: events.append("save"))
    sent = _drive_lifespan(app)
    assert events == ["inner shutdown", "save"]
    assert sent[-1] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed")


def test_lifespan_startup_and_http_traffic_do_not_save():
    from pseudolife_memory.daemon import save_on_lifespan_shutdown

    saves = []
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    app = save_on_lifespan_shutdown(inner, lambda: saves.append(1))
    asyncio.run(app({"type": "http"}, None, None))
    assert seen == ["http"] and saves == []
