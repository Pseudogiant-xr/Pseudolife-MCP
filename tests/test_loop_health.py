"""Loop-health metrics — is the memory loop actually being exercised?

Storage counts windowed activity (stores, outcome signals, sessions,
lessons); the service wraps it with availability + per-session rates; the
Console tile reads it from /api/overview (``loop``). Instruction-block changes are
supposed to move these numbers — this is the measurement side.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

NOW = 1_784_200_000.0
DAY = 86_400.0
ZERO_VEC = "[" + ",".join(["0"] * 1024) + "]"


def _hook(n: int) -> str:
    """A root key as the SessionStart hook writes it: the client's own
    session id, a dashed UUID."""
    return f"{n:08x}-0000-4000-8000-000000000000"


def _shim(n: int) -> str:
    """A root key as the stdio shim writes it: a fresh 32-hex uuid."""
    return f"{n:032x}"


def _seed(conn):
    """3 stores in-window (one of them source=status), 1 store prior-window,
    2 in-window outcome signals (success+failure), 1 prior-window signal
    (consumed), 2 in-window hook-keyed session roots + 1 sub-episode + 1 old
    session, 1 current lesson."""
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
    for eid, started, parent, key in (("s1", NOW - 1 * DAY, None, _hook(1)),
                                      ("s2", NOW - 2 * DAY, None, _hook(2)),
                                      ("sub", NOW - 1 * DAY, "s1", None),
                                      ("old", NOW - 20 * DAY, None, _hook(3))):
        conn.execute(
            "INSERT INTO episodes (id, title, started_at, parent_id, "
            "session_key) VALUES (%s, 't', %s, %s, %s)",
            (eid, started, parent, key))
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
    assert h["root_episodes"] == 2
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
    assert h["root_episodes"] == 0
    assert h["last_lesson_at"] is None


def _root(conn, eid, key, started, parent=None):
    conn.execute(
        "INSERT INTO episodes (id, title, started_at, parent_id, session_key) "
        "VALUES (%s, 't', %s, %s, %s)", (eid, started, parent, key))


def test_storage_loop_health_counts_client_sessions_not_root_episodes(
        pg_conn, pg_url):
    """2026-09-25: most new root episodes on the live bank were idle
    stdio-shim roots, and the tile divided its per-session rates by every
    root. ``sessions`` now follows evals/capture_metrics.py: hook roots
    always count, other keyed roots only with memory activity in the
    window, and a hook root and a nearby active shim root that are each
    other's only candidate are one session (pairing rules:
    ``test_hook_shim_pairing_rules``). The raw count stays as
    ``root_episodes``."""
    from pseudolife_memory.storage.postgres import PostgresStorage

    t0 = NOW - 1 * DAY
    _root(pg_conn, "hook-idle", _hook(1), t0)             # counts: hook root
    _root(pg_conn, "shim-idle-a", _shim(1), t0 + 600)     # dropped: idle shim
    _root(pg_conn, "shim-idle-b", _shim(2), t0 + 1200)    # dropped: idle shim
    _root(pg_conn, "shim-store", _shim(3), t0 + 1800)     # counts: stored
    _root(pg_conn, "shim-store-sub", None, t0 + 1801, parent="shim-store")
    pg_conn.execute(                                       # in a sub-episode
        "INSERT INTO entries (band, text, embedding, ts, source, episode_id) "
        "VALUES ('working', 'x', %s::vector, %s, 'proj', 'shim-store-sub')",
        (ZERO_VEC, t0 + 1900))
    _root(pg_conn, "shim-outcome", _shim(4), t0 + 2400)   # counts: outcome
    pg_conn.execute(
        "INSERT INTO outcome_signals (task, outcome, created_at, episode_id) "
        "VALUES ('t', 'success', %s, 'shim-outcome')", (t0 + 2500,))
    _root(pg_conn, "shim-search", _shim(5), t0 + 3000)    # counts: searched,
    pg_conn.execute(                                       # by session key only
        "INSERT INTO retrieval_events (query_text, session_id, created_at) "
        "VALUES ('q', %s, %s)", (_shim(5), t0 + 3100))
    # One client session, two roots: the hook's (idle) and the shim's, which
    # carries the activity, opened 2 s apart. Counted once.
    _root(pg_conn, "pair-hook", _hook(2), t0 + 3600)
    _root(pg_conn, "pair-shim", _shim(6), t0 + 3602)
    pg_conn.execute(
        "INSERT INTO retrieval_events (query_text, episode_id, created_at) "
        "VALUES ('q', 'pair-shim', %s)", (t0 + 3700,))
    _root(pg_conn, "keyless", None, t0 + 4200)            # no key: not a session
    # Out-of-window activity does not make an in-window shim root active.
    _root(pg_conn, "shim-stale", _shim(7), t0 + 4800)
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, source, episode_id) "
        "VALUES ('working', 'x', %s::vector, %s, 'proj', 'shim-stale')",
        (ZERO_VEC, NOW - 10 * DAY))
    pg_conn.commit()

    st = PostgresStorage(pg_url)
    try:
        h = st.loop_health(window_s=7 * DAY, now=NOW)
    finally:
        st.close()
    # hook-idle, shim-store, shim-outcome, shim-search, pair-hook+pair-shim
    assert h["sessions"] == 5
    # every root started in the window, idle and keyless ones included
    assert h["root_episodes"] == 10


def test_a_search_counts_for_its_callers_session_not_its_stamped_episode(
        pg_conn, pg_url):
    """A retrieval_events row's episode_id is the daemon's process-wide
    current episode, not the caller's; its session_id is the caller's own
    (evals/capture_metrics.py at a0b1f533). Here the stamped episode is an
    unrelated idle shim root: crediting it would count a session that never
    searched and leave the caller's shim root unpaired with its hook."""
    from pseudolife_memory.storage.postgres import PostgresStorage

    t0 = NOW - 1 * DAY
    _root(pg_conn, "hook", _hook(1), t0)
    _root(pg_conn, "caller", _shim(1), t0 + 2)
    _root(pg_conn, "stamped", _shim(2), t0 + 600)
    pg_conn.execute(
        "INSERT INTO retrieval_events (query_text, session_id, episode_id, "
        "created_at) VALUES ('q', %s, 'stamped', %s)", (_shim(1), t0 + 700))
    # A caller whose key names no root in the window (its root was pruned,
    # or opened before the window) is NOT handed to the stamped episode.
    pg_conn.execute(
        "INSERT INTO retrieval_events (query_text, session_id, episode_id, "
        "created_at) VALUES ('q', %s, 'stamped', %s)", (_shim(99), t0 + 800))
    # A key reused by a later root: the search goes to the later one.
    _root(pg_conn, "reused-old", _shim(3), t0 + 1200)
    _root(pg_conn, "reused-new", _shim(3), t0 + 2400)
    pg_conn.execute(
        "INSERT INTO retrieval_events (query_text, session_id, created_at) "
        "VALUES ('q', %s, %s)", (_shim(3), t0 + 2500))
    # A search with no session id falls back to its episode stamp.
    _root(pg_conn, "anonymous", _shim(4), t0 + 3600)
    pg_conn.execute(
        "INSERT INTO retrieval_events (query_text, episode_id, created_at) "
        "VALUES ('q', 'anonymous', %s)", (t0 + 3700,))
    # An empty-string key is still a key, as in the bench (NOT NULL only).
    _root(pg_conn, "empty-key", "", t0 + 4800)
    pg_conn.execute(
        "INSERT INTO outcome_signals (task, outcome, created_at, episode_id) "
        "VALUES ('t', 'success', %s, 'empty-key')", (t0 + 4900,))
    pg_conn.commit()

    st = PostgresStorage(pg_url)
    try:
        keyed = [(r[0], r[1], r[2]) for r in pg_conn.execute(
            "SELECT id, session_key, started_at FROM episodes "
            "WHERE session_key IS NOT NULL ORDER BY started_at, id")]
        assert st._active_roots(keyed, t0) == {
            "caller", "reused-new", "anonymous", "empty-key"}
        h = st.loop_health(window_s=7 * DAY, now=NOW)
    finally:
        st.close()
    # hook+caller paired, reused-new, anonymous, empty-key
    assert h["sessions"] == 4


def test_hook_shim_pairing_rules():
    """Pure pairing rule (evals/capture_metrics.py at a0b1f533)."""
    from pseudolife_memory.storage.postgres import count_client_sessions

    h1, h2, s1 = ("h1", _hook(1), 100.0), ("h2", _hook(2), 101.0), \
        ("s1", _shim(1), 102.0)
    # The unique case merges: one hook, one active shim, 2 s apart.
    assert count_client_sessions([h1, s1], active_roots={"s1"}) == 1
    # So does an ACTIVE hook root: writes passing ``episode=`` land on the
    # hook root while the searches carry the shim's key.
    assert count_client_sessions([h1, s1], active_roots={"h1", "s1"}) == 1
    # Two hook roots within the window of ONE active shim root: which
    # session the shim belongs to is unknowable, so nothing merges. The
    # count is an upper bound, never a guess.
    assert count_client_sessions([h1, h2, s1], active_roots={"s1"}) == 3
    assert count_client_sessions([h1, h2, s1],
                                 active_roots={"h2", "s1"}) == 3
    # One hook root with two candidate shim roots: no merge either.
    s2 = ("s2", _shim(2), 99.0)
    assert count_client_sessions([h1, s1, s2],
                                 active_roots={"s1", "s2"}) == 3
    # The window is inclusive at exactly SESSION_PAIR_WINDOW_S.
    edge = ("s1", _shim(1), 105.0)
    assert count_client_sessions([h1, edge], active_roots={"s1"}) == 1
    far = ("s1", _shim(1), 105.001)
    assert count_client_sessions([h1, far], active_roots={"s1"}) == 2
    # An idle non-hook root is not a session at all.
    assert count_client_sessions([h1, s1], active_roots=set()) == 1


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
    # The Console's demo fixture serves the same keys the real service does,
    # so the tile is exercised on the shape it will actually read.
    from pseudolife_memory.web.fixtures import FixtureService
    assert set(FixtureService().loop_health()) == set(h)


def test_service_pending_signals_split_at_the_retry_window(
        pg_url, pg_conn, tmp_path, monkeypatch):
    """The tile says the next dream distils the pending signals, so it counts
    only those inside signal_retry_days. Older pending rows are kept as
    evidence but never offered again; they are reported apart, not hidden."""
    from pseudolife_memory.service import MemoryService

    _seed(pg_conn)                           # 2 pending, both in the window
    pg_conn.execute(
        "INSERT INTO outcome_signals (task, outcome, created_at) "
        "VALUES ('t', 'failure', %s)", (NOW - 40 * DAY,))
    pg_conn.commit()
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    h = svc.loop_health(window_days=7, now=NOW)
    assert h["pending_signals"] == 2
    assert h["pending_signals_expired"] == 1
