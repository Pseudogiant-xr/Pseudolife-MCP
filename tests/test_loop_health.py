"""Loop-health metrics — is the memory loop actually being exercised?

Storage counts windowed activity (stores, outcome signals, sessions,
lessons); the service wraps it with availability + per-session rates; the
Console tile consumes /api/loop-health. Instruction-block changes are
supposed to move these numbers — this is the measurement side.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

NOW = 1_784_200_000.0
DAY = 86_400.0
ZERO_VEC = "[" + ",".join(["0"] * 384) + "]"


def _seed(conn):
    """3 stores in-window (one of them source=status), 1 store prior-window,
    2 in-window outcome signals (success+failure), 1 prior-window signal
    (consumed), 2 in-window session episodes + 1 sub-episode + 1 old session,
    1 current lesson."""
    for ts, source in ((NOW - 1 * DAY, "proj"), (NOW - 2 * DAY, "proj"),
                       (NOW - 3 * DAY, "status"), (NOW - 10 * DAY, "proj")):
        conn.execute(
            "INSERT INTO entries (band, text, embedding, ts, source) "
            "VALUES ('working', 'x', %s::vector, %s, %s)",
            (ZERO_VEC, ts, source))
    for created, outcome, consumed in (
            (NOW - 1 * DAY, "success", None),
            (NOW - 2 * DAY, "failure", None),
            (NOW - 9 * DAY, "success", NOW - 8 * DAY)):
        conn.execute(
            "INSERT INTO outcome_signals (task, outcome, created_at, consumed_at) "
            "VALUES ('t', %s, %s, %s)", (outcome, created, consumed))
    for eid, started, parent in (("s1", NOW - 1 * DAY, None),
                                 ("s2", NOW - 2 * DAY, None),
                                 ("sub", NOW - 1 * DAY, "s1"),
                                 ("old", NOW - 20 * DAY, None)):
        conn.execute(
            "INSERT INTO episodes (id, title, started_at, parent_id) "
            "VALUES (%s, 't', %s, %s)", (eid, started, parent))
    conn.execute(
        "INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, "
        "value, status, confidence, asserted_at, last_confirmed) "
        "VALUES ('t', 'a', 't', 'a', 'v', 'current', 0.9, %s, %s)",
        (NOW - 1 * DAY, NOW - 1 * DAY))
    conn.commit()


def test_storage_loop_health_windowed_counts(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    _seed(pg_conn)
    st = PostgresStorage(pg_url)
    try:
        h = st.loop_health(window_s=7 * DAY, now=NOW)
    finally:
        st.close()

    assert h["stores"] == {"current": 3, "previous": 1}
    assert h["outcomes"]["current"] == 2
    assert h["outcomes"]["previous"] == 1     # consumed signals still count
    assert h["outcomes"]["by_outcome"] == {"success": 1, "failure": 1}
    assert h["sessions"] == 2                  # sub-episodes and old excluded
    assert h["pending_signals"] == 2
    assert h["last_lesson_at"] == pytest.approx(NOW - 1 * DAY)
    assert h["lessons_current"] == 1


def test_storage_loop_health_empty_bank(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    st = PostgresStorage(pg_url)
    try:
        h = st.loop_health(window_s=7 * DAY, now=NOW)
    finally:
        st.close()
    assert h["stores"] == {"current": 0, "previous": 0}
    assert h["sessions"] == 0
    assert h["last_lesson_at"] is None


def test_service_loop_health_without_storage(pristine_service):
    """No Postgres → available: False, never a raise (the Console tile
    renders a 'needs Postgres' state)."""
    result = pristine_service.loop_health()
    assert result == {"available": False}


def test_service_loop_health_rates(pg_url, pg_conn, tmp_path, monkeypatch):
    """Service wraps storage counts with per-session rates."""
    from pseudolife_memory.service import MemoryService

    _seed(pg_conn)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    # no close(): pg_conn's next-test backend reap handles the connection
    h = svc.loop_health(window_days=7, now=NOW)
    assert h["available"] is True
    assert h["window_days"] == 7
    assert h["stores_per_session"] == pytest.approx(1.5)   # 3 / 2
    assert h["outcomes_per_session"] == pytest.approx(1.0)  # 2 / 2
