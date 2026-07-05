"""Connection-loss durability: a mid-store disconnect must not hand out
phantom entry ids, and the dream must recover from a stale in-memory->PG
mapping instead of stalling forever (2026-07-04 bench incident: dream_run
hit memory_traces_entry_id_fkey and retried the same write every sweep
until process restart). PG-gated; skips without a test server.
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (pytest fixtures)

psycopg = pytest.importorskip("psycopg")


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


def test_txn_raises_when_connection_breaks_before_commit(storage, pg_conn):
    """psycopg's Transaction.__exit__ silently SKIPS the COMMIT when the
    connection broke during the block (pgconn.status != OK): the block exits
    cleanly while the server rolls the work back. ``_txn`` must detect the
    missed commit and raise — otherwise ``insert_entry`` returns a RETURNING
    id for a row that never committed, poisoning the in-memory db_id map."""
    pid = storage._conn.info.backend_pid
    with pytest.raises(psycopg.OperationalError):
        with storage._txn():
            storage.conn.execute(
                "INSERT INTO meta (key, value) VALUES ('txn-canary', %s::jsonb)",
                ("1",))
            # Simulate AdminShutdown mid-store: the server kills the backend...
            pg_conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
            pg_conn.commit()
            # ...and the client notices (status -> BAD) before COMMIT is sent.
            # Use the raw _conn: the .conn property would heal-and-hide it.
            try:
                storage._conn.execute("SELECT 1")
            except psycopg.Error:
                pass
    row = pg_conn.execute(
        "SELECT 1 FROM meta WHERE key = 'txn-canary'").fetchone()
    assert row is None, "insert must have rolled back with the lost connection"


def test_dream_recovers_from_stale_entry_ids(pg_conn, pg_url, tmp_path):
    """Regression for the 2026-07-04 dream stall: in-memory entries holding
    db_ids whose rows are gone (rolled-back insert after a connection loss)
    made every trace write hit memory_traces_entry_id_fkey, and the
    claim-write hold retried the SAME write every sweep — a permanent stall.
    dream_run must verify/repair the entry mapping and consolidate on the
    next sweep."""
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.memory.dream import Claim

    class _Stub:
        def extract(self, texts, vocab):
            return [Claim(entity="checkout-service", attribute="default port",
                          value="9090", confidence=0.8, origin="agent")]

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.store("checkout-service default port note", source="t")
    # Reproduce the corrupt end state directly: the row vanishes from PG while
    # the in-memory entry keeps its id (the _txn commit check closes the
    # genesis, but a bank that already holds stale ids must still recover).
    pg_conn.execute("DELETE FROM entries")
    pg_conn.commit()

    svc.dream_run(_Stub(), limit=100)          # hits the FK, must self-repair
    r2 = svc.dream_run(_Stub(), limit=100)     # repaired mapping consolidates
    assert not r2.get("extractor_failed"), "dream stalled on stale entry ids"
    assert r2["claims"] >= 1
    assert r2["traces"] >= 1
    rec = svc.cortex_lookup("checkout-service", "default port")
    assert rec is not None and rec["value"] == "9090"
    # The entry was re-flushed under a fresh id and the trace cites a live row.
    rows = pg_conn.execute(
        "SELECT e.text FROM memory_traces t JOIN entries e ON e.id = t.entry_id"
    ).fetchall()
    assert rows and "checkout-service" in rows[0][0]
