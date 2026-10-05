"""``pg_conn`` waits for this process's end-of-session dream threads BEFORE
it reaps backends (PG-backed wiring check).

``episode_end_session`` returns as soon as it has started the dream thread,
so the thread routinely outlives its test. Without the wait, the next test's
``pg_conn`` terminates that thread's connection mid-dream, the storage layer
reconnects it on the next query, and the fresh transaction can deadlock the
fixture's TRUNCATE — CI run 35556355319 (2026-09-21) lost
``test_reaped_handle_resume_disabled_warns`` to exactly that on one xdist
worker's private database. The unit tests for the wait helper live in
tests/test_pg_run_isolation.py; this one drives a real setup pass and pins
that the fixture calls it.
"""

from __future__ import annotations

import threading
import time

from tests import pg_fixtures
from tests.pg_fixtures import pg_url  # noqa: F401  (fixture)


def test_pg_conn_setup_waits_for_a_live_session_end_dream(
        pg_url, tmp_path, monkeypatch):  # noqa: F811
    """A dream fired by the previous test is still running when the next
    ``pg_conn`` setup begins; the setup must not open its connection (and so
    reap) until the dream has finished. Asserted on ORDER — was the dream
    done when the setup first called ``psycopg.connect``? — not on elapsed
    time, so a slow machine cannot turn it green without the wait. The dream
    stub holds no connection, so lock timing never enters into it."""
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.service_dream import SESSION_END_DREAM_THREAD_NAME

    # A reap first, as pg_conn would give any other test: the service below
    # takes the bank's writer lease, and a PG-backed service an earlier test
    # dropped keeps that lease until Python's GC frees it (CI job
    # 107963358803, 2026-09-25: WriterLeaseHeld here on an xdist worker).
    before = pg_fixtures._pg_conn_session(pg_url)
    next(before)
    before.close()

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()
    dream_done = threading.Event()

    def _slow_dream():
        time.sleep(1.0)
        dream_done.set()
        return {}

    monkeypatch.setattr(svc, "dream_run_auto", _slow_dream)
    ep = svc.episode_start_session("keyW", "session W")
    svc.store("seed keyW", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyW")            # fires the daemon dream thread
    assert any(t.name == SESSION_END_DREAM_THREAD_NAME and t.is_alive()
               for t in threading.enumerate()), "precondition: dream is live"

    # Stamp whether the dream had finished at the moment the setup pass
    # opened its connection — the reap is that connection's first statement.
    dream_done_at_connect: list[bool] = []
    real_connect = pg_fixtures.psycopg.connect

    def _stamping_connect(*args, **kwargs):
        dream_done_at_connect.append(dream_done.is_set())
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(pg_fixtures.psycopg, "connect", _stamping_connect)

    # The "next test's" pg_conn setup: one pass of the fixture body.
    setup = pg_fixtures._pg_conn_session(pg_url)
    try:
        next(setup)
        assert dream_done_at_connect == [True], (
            "pg_conn connected (and so reaped) while the previous test's "
            "dream thread was still running")
        assert not any(t.name == SESSION_END_DREAM_THREAD_NAME and t.is_alive()
                       for t in threading.enumerate())
    finally:
        setup.close()
