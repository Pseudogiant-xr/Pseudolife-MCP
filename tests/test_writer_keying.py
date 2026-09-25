"""Writer/session keying (v0.4 T4) — per-connection attribution.

Two layers:

* **Unit:** a superseding write stamps ``writer_id``/``session_id`` onto the
  cortex supersession-log entry (instrumentation carries provenance too).
* **Integration:** a fact written over the live daemon with an
  ``X-PL-Writer`` header persists that ``writer_id`` and a non-null
  ``session_id`` — proving the header survives the streamable-HTTP
  session-task boundary into the single-writer service.

The integration test mirrors ``test_daemon_http.py`` (spawns the real
``serve`` process on a private bank on the test server) and skips cleanly
without Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.helpers import serve_on_private_bank as _serve_on_private_bank
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

psycopg = pytest.importorskip("psycopg")

_TOKEN = "test-secret-token"


# ── unit: supersession log carries the writer/session ────────────────────

def test_supersession_log_records_writer():
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot
    import torch

    s = CortexStore()
    e = torch.ones(1024)
    s.write_fact(Slot("server", "port", "8080"), e, support="user",
                 now=1.0, hlc=(1000, 0), writer_id="alice", session_id="sess-A")
    s.write_fact(Slot("server", "port", "9090"), e, support="user",
                 now=2.0, hlc=(2000, 0), writer_id="bob", session_id="sess-B")
    entry = s.supersession_log[-1]
    assert entry["decision"] == "supersede"
    assert entry["writer_id"] == "bob"
    assert entry["session_id"] == "sess-B"


# ── integration: header → persisted writer_id + session_id ───────────────

@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """Its OWN daemon, not a shared one: the assertion under test is that a
    per-request ``X-PL-Writer`` header beats the daemon's configured
    ``PSEUDOLIFE_WRITER_ID`` default, and it authenticates with a token — a
    daemon booted without both would make the test vacuous. It does share
    the spawn/teardown helper (``tests.helpers.serve_on_private_bank``)
    that tests/test_shim.py's module daemon uses, and so a bank of its own.
    """
    with _serve_on_private_bank(
            "keying", tmp_path_factory.mktemp("keying_data"),
            env_extra={
                "PSEUDOLIFE_MCP_TOKEN": _TOKEN,
                # The daemon's own default — the per-request header must
                # override this.
                "PSEUDOLIFE_WRITER_ID": "daemon-default",
            }) as d:
        yield d


async def _call_with_writer(url: str, writer: str, tool: str, args: dict):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client, streamable_http_client)

    headers = {"Authorization": f"Bearer {_TOKEN}", "X-PL-Writer": writer}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool, args)


def _fact_row(db_url: str, entity: str) -> dict | None:
    with psycopg.connect(db_url) as conn:
        conn.execute("SET search_path TO public")
        row = conn.execute(
            "SELECT writer_id, session_id FROM public.facts "
            "WHERE entity = %s AND status = 'current'",
            (entity,),
        ).fetchone()
    if row is None:
        return None
    return {"writer_id": row[0], "session_id": row[1]}


# ── ops: retire by writer ────────────────────────────────────────────────

def test_retire_by_writer_supersedes_only_that_writer(pg_conn, pg_url):
    """ops/retire_by_writer supersedes a rogue writer's current facts and leaves
    everyone else's intact.

    ``pg_conn`` is load-bearing, not just a wipe: its reaper runs before the
    service below takes the bank's writer lease. Without it, a PG-backed
    service an earlier test dropped but Python's GC had not yet freed still
    held that lease, and this test was refused (seen with GC disabled,
    2026-09-25)."""
    import tempfile
    import uuid

    from ops import retire_by_writer
    from pseudolife_memory import writer_context
    from pseudolife_memory.service import MemoryService

    # Unique writer + entity names keep the counts exact.
    tag = uuid.uuid4().hex[:8]
    rogue, ea, eb = f"rogue-{tag}", f"alpha-{tag}", f"beta-{tag}"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d, database_url=pg_url)
        try:
            tok = writer_context.set_writer_context(rogue, "sess-x")
            svc.cortex_write(ea, "k", "1", support="user")
            writer_context.reset_writer_context(tok)
            svc.cortex_write(eb, "k", "2", support="user")  # writer "unknown"

            with psycopg.connect(pg_url) as conn:
                plan = retire_by_writer.run(conn, rogue, apply=False)
                assert plan["counts"]["facts"] == 1          # only the rogue fact
                assert retire_by_writer.run(
                    conn, rogue, apply=True)["retired"] == 1
                rogue_status = conn.execute(
                    "SELECT status FROM public.facts WHERE entity=%s", (ea,)
                ).fetchone()[0]
                other_status = conn.execute(
                    "SELECT status FROM public.facts WHERE entity=%s", (eb,)
                ).fetchone()[0]
            assert rogue_status == "superseded" and other_status == "current"
        finally:
            if svc._storage is not None:
                svc._storage.close()


def test_fact_write_attributes_writer_from_header(daemon):
    asyncio.run(_call_with_writer(
        daemon["url"], "codex-test", "memory_fact_set",
        {"entity": "keying-probe", "attribute": "owner",
         "value": "codex", "support": "user"},
    ))
    row = _fact_row(daemon["db"], "keying-probe")
    assert row is not None, "fact row not persisted"
    assert row["writer_id"] == "codex-test", row
    # Spec 2026-08-25: no X-PL-Session and no episode handle means no session
    # identity — the retired mcp-session-id fallback must NOT stamp the
    # connection id here.
    assert not row["session_id"], row
