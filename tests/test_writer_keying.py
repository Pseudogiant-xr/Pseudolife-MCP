"""Writer/session keying (v0.4 T4) — per-connection attribution.

Two layers:

* **Unit:** a superseding write stamps ``writer_id``/``session_id`` onto the
  cortex supersession-log entry (instrumentation carries provenance too).
* **Integration:** a fact written over the live daemon with an
  ``X-PL-Writer`` header persists that ``writer_id`` and a non-null
  ``session_id`` — proving the header survives the streamable-HTTP
  session-task boundary into the single-writer service.

The integration test mirrors ``test_daemon_http.py`` (spawns the real
``serve`` process against the test DB) and skips cleanly without Postgres.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tests.pg_fixtures import resolve_test_db_url

psycopg = pytest.importorskip("psycopg")

_TOKEN = "test-secret-token"


# ── unit: supersession log carries the writer/session ────────────────────

def test_supersession_log_records_writer():
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot
    import torch

    s = CortexStore()
    e = torch.ones(384)
    s.write_fact(Slot("server", "port", "8080"), e, support="user",
                 now=1.0, hlc=(1000, 0), writer_id="alice", session_id="sess-A")
    s.write_fact(Slot("server", "port", "9090"), e, support="user",
                 now=2.0, hlc=(2000, 0), writer_id="bob", session_id="sess-B")
    entry = s.supersession_log[-1]
    assert entry["decision"] == "supersede"
    assert entry["writer_id"] == "bob"
    assert entry["session_id"] == "sess-B"


# ── integration: header → persisted writer_id + session_id ───────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _health(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("keying_data")
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATABASE_URL": url,
        "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
        "PSEUDOLIFE_MCP_TOKEN": _TOKEN,
        # The daemon's own default — the per-request header must override this.
        "PSEUDOLIFE_WRITER_ID": "daemon-default",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 60
    health = None
    while time.time() < deadline:
        health = _health(port)
        if health is not None:
            break
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early ({proc.returncode})")
        time.sleep(0.5)
    if health is None:
        proc.terminate()
        pytest.fail("daemon never became healthy")
    yield {"port": port, "url": f"http://127.0.0.1:{port}", "db": url}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _call_with_writer(url: str, writer: str, tool: str, args: dict):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {_TOKEN}", "X-PL-Writer": writer}
    async with streamablehttp_client(url + "/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(tool, args)


def _fact_row(db_url: str, entity: str) -> dict | None:
    with psycopg.connect(db_url) as conn:
        conn.execute("SET search_path TO public, ag_catalog")
        row = conn.execute(
            "SELECT writer_id, session_id FROM public.facts "
            "WHERE entity = %s AND status = 'current'",
            (entity,),
        ).fetchone()
    if row is None:
        return None
    return {"writer_id": row[0], "session_id": row[1]}


def test_fact_write_attributes_writer_from_header(daemon):
    asyncio.run(_call_with_writer(
        daemon["url"], "codex-test", "memory_fact_set",
        {"entity": "keying-probe", "attribute": "owner",
         "value": "codex", "support": "user"},
    ))
    row = _fact_row(daemon["db"], "keying-probe")
    assert row is not None, "fact row not persisted"
    assert row["writer_id"] == "codex-test", row
    assert row["session_id"], "session_id should be non-null (per-connection)"
