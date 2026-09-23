"""The bank writer lease: one ``PostgresStorage`` writer per database.

The daemon keeps a resident copy of every canonical store and writes each
slot back from that copy (``sync_cortex_slots`` DELETEs the slot's rows and
re-inserts the resident ones), and a flush rewrites whole tables. A second
process writing the same bank therefore does not merely race; it overwrites
the first one's durable history from a stale copy. The single-writer rule
was documented, never enforced (fresh-eyes review 2026-09-23, Tier A item
8): ``ops/dedup_cortex.py --dry-run`` could rewrite ``facts`` under a
running daemon. ``PostgresStorage`` now holds a Postgres session advisory
lock on its write connection for as long as it lives, and a second one on
the same database refuses to start.

Every probe here runs against the per-run test database (``pg_url``).
"""
from __future__ import annotations

import os
import time

import psycopg
import pytest

from pseudolife_memory.storage.postgres import PostgresStorage
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

LEASE_KEY = "pseudolife-bank-writer"


def _lease_holder_pids(conn) -> list[int]:
    """Backends holding the writer lease in this database, read straight
    from ``pg_locks`` (independent of the implementation's own query)."""
    key = conn.execute(
        "SELECT hashtextextended(%s, 0)", (LEASE_KEY,)).fetchone()[0]
    rows = conn.execute(
        "SELECT pid FROM pg_locks "
        "WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
        "  AND database = (SELECT oid FROM pg_database "
        "                  WHERE datname = current_database()) "
        "  AND classid = %s::bigint::oid AND objid = %s::bigint::oid",
        ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
    ).fetchall()
    conn.commit()  # never leave the probe connection idle in a transaction
    return sorted(r[0] for r in rows)


def _backends(conn) -> int:
    count = conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchone()[0]
    conn.commit()
    return count


def _kill(conn, pid: int) -> None:
    conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
    conn.commit()


def test_second_storage_on_same_database_refuses_naming_the_holder(pg_conn, pg_url):
    first = PostgresStorage(pg_url)
    try:
        holder = first.conn.info.backend_pid
        assert _lease_holder_pids(pg_conn) == [holder]
        before = _backends(pg_conn)

        with pytest.raises(RuntimeError, match="writer lease") as refused:
            PostgresStorage(pg_url)

        message = str(refused.value)
        assert f"backend pid {holder}" in message
        # The holder's application_name names this process, so an operator
        # can tell the daemon from a script from a test run.
        assert f"pid={os.getpid()}" in message
        # The refused instance released its connection; nothing leaked
        # that could keep a half-open session around.
        assert _backends(pg_conn) == before
        assert _lease_holder_pids(pg_conn) == [holder]
    finally:
        first.close()


def test_lease_is_released_when_the_holder_closes(pg_conn, pg_url):
    first = PostgresStorage(pg_url)
    first.close()
    second = PostgresStorage(pg_url)
    try:
        assert _lease_holder_pids(pg_conn) == [second.conn.info.backend_pid]
    finally:
        second.close()


def test_failed_open_releases_the_lease_for_its_own_retry(pg_conn, pg_url,
                                                          monkeypatch):
    """A schema refusal (e.g. the v25 dim-mismatch guard) happens AFTER the
    lease is taken. The half-built instance must let go at once, or the
    caller's retry would refuse against its own earlier attempt."""
    from pseudolife_memory.storage import postgres as postgres_module

    def _refuse(conn):
        raise RuntimeError("schema refusal after the lease was taken")

    monkeypatch.setattr(postgres_module, "ensure_schema", _refuse)
    with pytest.raises(RuntimeError, match="schema refusal") as failed:
        PostgresStorage(pg_url)
    monkeypatch.undo()

    # `failed` still holds the traceback, and with it the half-built
    # instance, so only an explicit close can have released the lease.
    assert failed.value is not None
    assert _lease_holder_pids(pg_conn) == []
    retry = PostgresStorage(pg_url)
    retry.close()


def test_explicit_opt_out_coexists_and_never_holds_the_lease(pg_conn, pg_url):
    first = PostgresStorage(pg_url)
    peer = PostgresStorage(pg_url, writer_lease=False)
    try:
        assert _lease_holder_pids(pg_conn) == [first.conn.info.backend_pid]
        first.close()
        # The opted-out peer is still connected, yet a new writer gets in:
        # an opt-out never takes (or blocks) the lease.
        third = PostgresStorage(pg_url)
        third.close()
    finally:
        first.close()
        peer.close()


def test_reconnect_reacquires_the_lease_before_the_next_write(pg_conn, pg_url):
    first = PostgresStorage(pg_url)
    try:
        _kill(pg_conn, first.conn.info.backend_pid)
        # Heal-on-next-use: the call that meets the dead session raises...
        with pytest.raises(psycopg.OperationalError):
            first.conn.execute("SELECT 1")
        # ...and the next one reconnects, holding the lease again.
        healed = first.conn.info.backend_pid
        assert _lease_holder_pids(pg_conn) == [healed]
        with pytest.raises(RuntimeError, match="writer lease"):
            PostgresStorage(pg_url)
    finally:
        first.close()


def test_reconnect_fails_closed_when_another_writer_took_the_lease(pg_conn, pg_url):
    first = PostgresStorage(pg_url)
    second = None
    try:
        _kill(pg_conn, first.conn.info.backend_pid)
        with pytest.raises(psycopg.OperationalError):
            first.conn.execute("SELECT 1")
        # The session died, so its lease went with it; another writer
        # takes the bank before the first one heals.
        second = PostgresStorage(pg_url)

        with pytest.raises(RuntimeError, match="writer lease"):
            first.meta_set("lease-probe", "stale writer")
        assert pg_conn.execute(
            "SELECT count(*) FROM meta WHERE key = 'lease-probe'"
        ).fetchone()[0] == 0
        # Still refusing on every later attempt while the other holds it,
        # and the failed reconnects leaked no sessions.
        with pytest.raises(RuntimeError, match="writer lease"):
            first.meta_set("lease-probe", "stale writer")
        assert _lease_holder_pids(pg_conn) == [second.conn.info.backend_pid]
        # /health's probe must see it: ping() is on its own connection, so
        # without this a lost lease would still read as a healthy daemon.
        with pytest.raises(RuntimeError, match="writer lease"):
            first.ping()
        # A burst of calls pays the lease wait once, not once per call (each
        # would otherwise hold the service lock for the whole wait).
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="writer lease"):
            first.meta_set("lease-probe", "stale writer")
        assert time.monotonic() - started < 1.0
        # Once the other writer has gone, the probe stops reporting it
        # even before anything reconnects. (A closed client's server
        # process releases the lock a moment later; wait for that first.)
        second.close()
        deadline = time.monotonic() + 5
        while _lease_holder_pids(pg_conn) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert first.ping() is True
    finally:
        first.close()
        if second is not None:
            second.close()


def test_memory_service_inherits_the_lease_before_any_model_load(
        pg_conn, pg_url, tmp_path):
    from pseudolife_memory.daemon import _build_health_payload
    from pseudolife_memory.service import MemoryService

    holder = PostgresStorage(pg_url)
    try:
        svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
        with pytest.raises(RuntimeError, match="writer lease"):
            svc._ensure_init()  # noqa: SLF001 — the lazy init every tool runs
        # Refused at the storage connect, which precedes the model load
        # (2026-08-04 boot balloon): a refused writer costs a connect.
        assert svc._embedder is None  # noqa: SLF001
        assert svc._cms is None  # noqa: SLF001
        payload = _build_health_payload(svc, token_present=False)
        assert payload["status"] == "degraded"
        # Retryable (the holder may be a script, or a session still
        # exiting), so not the permanent refusal the shim exits on.
        assert "writer lease" in payload["not_ready"]
        assert "init_refusal" not in payload
        from pseudolife_memory import shim
        shim._accept_health("http://127.0.0.1:1", payload)  # attaches

        # A burst of calls pays the lease wait once: later calls inside the
        # retry window are refused at once, without a connect.
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="not ready.*writer lease"):
            svc._ensure_init()  # noqa: SLF001
        assert time.monotonic() - started < 1.0

        holder.close()
        svc._init_retry_at = 0.0  # noqa: SLF001 — let the window expire
        svc._ensure_init()  # noqa: SLF001 — the bank is free again
        payload = _build_health_payload(svc, token_present=False)
        assert payload["status"] == "ok" and "not_ready" not in payload
    finally:
        holder.close()


def test_lease_retries_collect_no_garbage(pg_conn, pg_url, tmp_path,
                                          monkeypatch):
    """A lease refusal builds no stores, so its retries have nothing to
    collect; a full collection under the service lock on every retry is
    pure cost (the 2026-08-04 boot-burst shape)."""
    import pseudolife_memory.service as service_module
    from pseudolife_memory.service import MemoryService

    collections: list[int] = []
    real_gc = service_module.gc

    class _CountingGc:
        def collect(self, *args):
            collections.append(1)
            return real_gc.collect(*args)

    monkeypatch.setattr(service_module, "gc", _CountingGc())
    holder = PostgresStorage(pg_url)
    try:
        svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="writer lease"):
                svc._ensure_init()  # noqa: SLF001
            svc._init_retry_at = 0.0  # noqa: SLF001
        assert collections == []
    finally:
        holder.close()


def test_a_holder_that_is_exiting_is_waited_out_not_refused(pg_conn, pg_url):
    """A session that is closing still holds the lease until its server
    process exits (a daemon restart, or the reconnect after a closed
    connection). A new writer must wait that moment out, not refuse."""
    import threading

    first = PostgresStorage(pg_url)
    closer = threading.Timer(0.5, first.close)
    closer.start()
    try:
        second = PostgresStorage(pg_url)  # arrives while `first` still holds
        second.close()
    finally:
        closer.join()
        first.close()


def test_application_name_names_the_command_but_never_its_arguments(
        monkeypatch):
    from pseudolife_memory.storage.postgres import _application_name

    monkeypatch.setattr("sys.argv", ["/usr/local/bin/pseudolife-mcp", "serve"])
    assert _application_name().endswith(" pseudolife-mcp serve")
    monkeypatch.setattr("sys.argv", [
        "ops/restore_from_pt.py", "--dsn", "postgresql://u:hunter2@h/db"])
    name = _application_name()
    assert name.endswith(" restore_from_pt.py")
    assert "hunter2" not in name and "--dsn" not in name
