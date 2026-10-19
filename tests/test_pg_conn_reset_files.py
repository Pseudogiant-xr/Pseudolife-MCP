"""``pg_conn``'s per-test reset rewrites only the tables that held rows.

``TRUNCATE`` gives every table, index, TOAST table and owned sequence it
touches a new file, and the next checkpoint fsyncs each new file. A full
reset of ``BENCH_RESET_TABLES`` made 242 of them (43 tables, 140 indexes,
41 TOAST tables, 18 sequences; measured 2026-10-19 on the bench Postgres
18.6), and ``pg_conn`` runs it before every test that uses it. A serial
WSL full suite left ~354k files to fsync (2026-10-17, one checkpoint 112 s),
and a 4-worker run's two largest checkpoints fsynced ~357k files each in
150-172 s. Every ``DROP DATABASE`` waits for such a checkpoint, so module
teardowns took 85-209 s and the coordination proof's bounded cleanup gave up
(``tests/test_coordination_proof.py``, xdist only).

The reset now truncates the tables holding rows (always ``meta``, which it
re-seeds) and rewinds the identity sequences of the rest with ``setval``,
which changes a sequence page in place instead of minting a file. These
tests drive real setup passes against the bench database.
"""

from __future__ import annotations

import psycopg

from tests import pg_fixtures
from tests.pg_fixtures import pg_url  # noqa: F401  (fixture)

_TABLE_FILES = (
    "SELECT relname, relfilenode FROM pg_class "
    "WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace")


def _reset(url):
    """One full ``pg_conn`` setup pass; returns nothing, leaves no session."""
    session = pg_fixtures._pg_conn_session(url)
    try:
        next(session)
    finally:
        session.close()


def _table_files(url):
    with psycopg.connect(url) as conn:
        return dict(conn.execute(_TABLE_FILES).fetchall())


def _execute(url, *statements):
    with psycopg.connect(url) as conn:
        rows = [conn.execute(sql).fetchall() for sql in statements]
        conn.commit()
    return rows


def test_a_reset_mints_files_only_for_the_tables_that_held_rows(pg_url):  # noqa: F811
    _reset(pg_url)
    _execute(pg_url,
             "INSERT INTO outcome_signals (created_at, task, outcome) "
             "VALUES (1.0, 'reset-files', 'success') RETURNING id")
    before = _table_files(pg_url)
    _reset(pg_url)
    after = _table_files(pg_url)
    rewritten = sorted(t for t in before if after.get(t) != before[t])
    assert rewritten == ["meta", "outcome_signals"], (
        "the reset gave these tables new files; only the ones holding rows "
        f"(and meta, which it re-seeds) should be truncated: {rewritten}")


def test_an_emptied_table_still_restarts_its_ids(pg_url):  # noqa: F811
    """The near miss of skipping empty tables: a test that inserted and then
    deleted rows leaves the table empty but its sequence advanced. TRUNCATE
    ... RESTART IDENTITY restarted it; the skipped table must restart too."""
    _reset(pg_url)
    _execute(pg_url,
             "INSERT INTO retrieval_events (created_at, query_text) "
             "VALUES (1.0, 'a'), (2.0, 'b') RETURNING id",
             "DELETE FROM retrieval_events RETURNING id")
    _reset(pg_url)
    [[(first,)]] = _execute(
        pg_url,
        "INSERT INTO retrieval_events (created_at, query_text) "
        "VALUES (3.0, 'c') RETURNING id")
    assert first == 1


def test_every_table_that_held_rows_is_empty_after_the_reset(pg_url):  # noqa: F811
    _reset(pg_url)
    _execute(pg_url,
             "INSERT INTO outcome_signals (created_at, task, outcome) "
             "VALUES (1.0, 'reset-files', 'success') RETURNING id",
             "INSERT INTO merge_decisions (decided_at, status) "
             "VALUES (1.0, 'accepted') RETURNING id",
             "INSERT INTO entities (created_at, canonical, display) "
             "VALUES (1.0, 'reset', 'Reset') RETURNING id",
             "INSERT INTO entity_aliases (entity_id, alias) "
             "SELECT id, 'r' FROM entities RETURNING entity_id")
    _reset(pg_url)
    [counts] = _execute(pg_url, " UNION ALL ".join(
        f"SELECT '{t}', count(*) FROM {t}"
        for t in ("outcome_signals", "merge_decisions", "entities",
                  "entity_aliases")))
    assert all(n == 0 for _, n in counts), counts
    [[(version,)]] = _execute(
        pg_url, "SELECT value FROM meta WHERE key = 'schema_version'")
    assert version is not None
