"""``evals/capture_metrics.py`` counts client sessions, not root episodes.

A client session can leave two root episodes: the SessionStart hook opens one
keyed by the client's own session id (a dashed UUID), and the stdio shim opens
another keyed by a fresh 32-hex uuid and titled after its working directory.
Idle shim roots (on 2026-09-25, 111 of 193 roots in 48 h, titled after the
shared shim runtime directory) inflated every per-session denominator.
"""
from __future__ import annotations

import json

import pytest

from evals import capture_metrics as cm
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

NOW = 1_784_200_000.0
DAY = 86_400.0
SINCE = NOW - 2 * DAY
ZERO_VEC = "[" + ",".join(["0"] * 1024) + "]"


def hook_key(n: int) -> str:
    return f"{n:08x}-1111-4222-8333-444455556666"


def shim_key(n: int) -> str:
    return f"{n:032x}"


def _root(conn, rid, key, started, title="session - 2026-07-16 10:00", parent=None):
    conn.execute("INSERT INTO episodes (id, title, started_at, session_key, parent_id) "
                 "VALUES (%s, %s, %s, %s, %s)", (rid, title, started, key, parent))


def _entry(conn, episode, ts, source="proj"):
    return conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, source, episode_id) "
        "VALUES ('working', 'x', %s::vector, %s, %s, %s) RETURNING id",
        (ZERO_VEC, ts, source, episode)).fetchone()[0]


def _outcome(conn, episode, ts, origin=None):
    conn.execute("INSERT INTO outcome_signals (task, outcome, origin, episode_id, created_at) "
                 "VALUES ('t', 'success', %s, %s, %s)", (origin, episode, ts))


def _search(conn, ts, session=None, episode=None, served=()):
    return conn.execute(
        "INSERT INTO retrieval_events (query_text, session_id, episode_id, served, created_at) "
        "VALUES ('q', %s, %s, %s::jsonb, %s) RETURNING id",
        (session, episode, json.dumps([{"entry_id": i, "rank": r} for r, i in enumerate(served)]),
         ts)).fetchone()[0]


def _credit(conn, event_id, entry_id, ts):
    conn.execute("INSERT INTO retrieval_uses (event_id, entry_id, used_via, created_at) "
                 "VALUES (%s, %s, 'outcome', %s)", (event_id, entry_id, ts))


def _seed_sessions(conn):
    t = SINCE + 3600
    # 1. A hook root that did everything itself, in a sub-episode; its shim
    #    root stayed idle.
    _root(conn, "H1", hook_key(1), t)
    _root(conn, "H1-sub", None, t + 5, title="sub", parent="H1")
    _root(conn, "S1", shim_key(1), t + 1, title="PseudoLife-MCP - 2026-07-16 10:00")
    e1 = _entry(conn, "H1-sub", t + 120)
    ev1 = _search(conn, t + 60, session=hook_key(1), episode="H1", served=[e1])
    _outcome(conn, "H1-sub", t + 600)
    _credit(conn, ev1, e1, t + 600)
    # 2. A hook root left idle while its shim root, started two seconds
    #    later, took the work: one client session.
    t2 = t + 4 * 3600
    _root(conn, "H2", hook_key(2), t2)
    _root(conn, "S2", shim_key(2), t2 + 2, title="PseudoLife-MCP - 2026-07-16 14:00")
    _search(conn, t2 + 30, session=shim_key(2), episode="S2")
    _entry(conn, "S2", t2 + 90)
    # 3. Idle shim roots named after the shared runtime directory: dropped.
    _root(conn, "S3", shim_key(3), t2 + 600, title="coordination-e41a575a - 2026-07-16 14:10")
    # 4. A shim-only client (no hook) that logged an outcome and searched
    #    late. Its search row names H1 as the episode: a search's episode_id
    #    is the daemon's current episode, not the caller's.
    t4 = t + 8 * 3600
    _root(conn, "S4", shim_key(4), t4, title="Desktop - 2026-07-16 18:00")
    _outcome(conn, "S4", t4 + 100)
    _search(conn, t4 + 2000, session=shim_key(4), episode="H1")
    # 5. A hook-registered session that never touched memory: it counts.
    _root(conn, "H3", hook_key(3), t + 10 * 3600)
    # 6. An idle hook root with TWO active shim roots within five seconds:
    #    not attributable, so nothing merges and the pair is reported.
    t6 = t + 12 * 3600
    _root(conn, "H4", hook_key(4), t6)
    _root(conn, "S5", shim_key(5), t6 + 1, title="a - 2026-07-16 22:00")
    _root(conn, "S6", shim_key(6), t6 + 3, title="b - 2026-07-16 22:00")
    _search(conn, t6 + 10, session=shim_key(5))
    _entry(conn, "S6", t6 + 50)
    # 7. A compliant session: its writes pass episode= and land on the hook
    #    root, its searches carry the shim's key. Both roots are active;
    #    they are one session.
    t7 = t + 14 * 3600
    _root(conn, "H5", hook_key(5), t7)
    _root(conn, "S7", shim_key(7), t7 + 2, title="PseudoLife-MCP - 2026-07-17 00:00")
    _entry(conn, "H5", t7 + 60)
    _search(conn, t7 + 20, session=shim_key(7), episode="H4")
    # 8. Activity whose session root the daemon pruned at SessionEnd.
    _search(conn, t7 + 100, session=shim_key(99))
    _outcome(conn, "gone-episode", t7 + 200)
    # Outside the window, and a legacy root without a session key.
    _root(conn, "OLD", hook_key(9), SINCE - DAY)
    _root(conn, "NOKEY", None, t)
    conn.commit()


@pytest.fixture
def stats(pg_conn, pg_url, monkeypatch):
    _seed_sessions(pg_conn)
    monkeypatch.setattr(cm, "DSN", pg_url)
    return cm.collect(SINCE)


def test_sessions_are_client_sessions_not_root_episodes(stats):
    # Twelve keyed roots in the window; eight client sessions.
    assert stats["sessions"] == 8
    roots = stats["roots"]
    assert roots["keyed_in_window"] == 12
    assert roots["hook"] == 5 and roots["shim"] == 7
    assert roots["idle_shim_dropped"] == 2
    assert roots["merged_pairs"] == 2          # H2+S2 (idle hook), H5+S7 (both active)
    assert roots["ambiguous_pairs"] == 1


def test_activity_of_pruned_sessions_is_reported_not_attributed(stats):
    assert stats["roots"]["pruned_with_searches"] == 1
    assert stats["roots"]["pruned_with_outcomes"] == 1


def test_working_sessions_used_memory_at_least_once(stats):
    # H1, H2+S2, S4, S5, S6, H5+S7 — H3 and H4 never called a memory tool.
    assert stats["working_sessions"] == 6


def test_online_loop_metrics(stats):
    loop = stats["loop"]
    # Searched within 15 minutes of the session's start: H1, H2+S2, S5, H5+S7.
    # S4's late search names H1's episode but is S4's (by its session key).
    assert loop["searched_early"] == {"n": 4, "of_sessions": 4 / 8, "of_working": 4 / 6}
    # Outcome coverage: H1 and S4 logged one.
    assert loop["outcome_coverage"] == {"n": 2, "of_sessions": 2 / 8, "of_working": 2 / 6}
    # Of the two sessions with an outcome, one credited used_ids.
    assert loop["used_ids"]["sessions_with_credited_ids"] == 1
    assert loop["used_ids"]["of_sessions_with_outcome"] == 1 / 2
    assert loop["used_ids"]["credited_ids"] == 1
    # The bank records neither of these; say so instead of inventing a number.
    assert loop["used_ids"]["unmatched_ids"] is None
    assert loop["lesson_search_used"] is None
    assert "not recorded" in stats["not_recorded"]["lesson_search_used"]


def test_capture_coverage_uses_the_same_denominator(stats):
    # Substantive stores: H1 (sub-episode), H2+S2, S6, H5+S7.
    assert stats["substantive_sessions"] == 4
    assert stats["capture_coverage"] == pytest.approx(4 / 8, abs=1e-3)


def test_stored_entries_retrieved_again_by_another_session(pg_conn, pg_url, monkeypatch):
    """Of the substantive entries whose 14-day window has closed, the share
    another session's search served again within 14 days."""
    t = NOW - 30 * DAY
    _root(pg_conn, "A", hook_key(10), t)
    _root(pg_conn, "B", hook_key(11), t + 5 * DAY)
    reused = _entry(pg_conn, "A", t + 60)
    only_own = _entry(pg_conn, "A", t + 120)
    too_late = _entry(pg_conn, "A", t + 180)
    anonymous = _entry(pg_conn, "A", t + 200)
    _entry(pg_conn, "A", t + 240, source="status")          # not substantive
    _entry(pg_conn, "B", NOW - 2 * DAY)                      # window still open
    # Own session's search, even though the row names B as the episode.
    _search(pg_conn, t + 600, session=hook_key(10), episode="B", served=[only_own])
    _search(pg_conn, t + 5 * DAY + 60, session=hook_key(11), episode="B", served=[reused])
    _search(pg_conn, t + 20 * DAY, session=hook_key(11), episode="B", served=[too_late])
    # No caller identity: cannot say whose search it was.
    _search(pg_conn, t + 3 * DAY, session=None, episode="B", served=[anonymous])
    pg_conn.commit()
    monkeypatch.setattr(cm, "DSN", pg_url)
    reuse = cm.collect(NOW - 40 * DAY, now=NOW)["reuse_14d"]
    assert reuse == {"entries": 4, "retrieved_again": 1,
                     "rate": pytest.approx(1 / 4, abs=1e-3), "unattributable": 1}
