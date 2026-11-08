"""sweep-analyzer-tick settles on its own rows, not the whole judge state.

On a copy of the real bank the analyzer walks every entity 50 per tick, so
its ``analyzer_duplicate_cursor`` in ``meta`` moved on each sweep and the
settle's quiet phase, a digest over all judge tables and ``meta``, never held:
the scenario errored on every real-bank attempt (forced real-data run, daemon
box, 2026-11-08). Its settle now watches only the proposals, decisions and
dismissals touching its seeded entities, and the reconcile cursor.
"""
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

OWN = ("Billing Gateway", "Billing Gateway Service", "Ledger Export", "Ledger Exporter")


@pytest.fixture
def bank(monkeypatch):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import ensure_schema
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    pg = pgdisposable.Slice("w2e")
    name = pg.name("pl_cf_w2e_sweep_settle")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            ensure_schema(conn)
            for i, display in enumerate(OWN + ("Real Entity A", "Real Entity B")):
                conn.execute("INSERT INTO entities (canonical, display, created_at) "
                             "VALUES (%s, %s, %s)", (display.lower(), display, float(i)))
            conn.execute("INSERT INTO meta (key, value) VALUES "
                         "('analyzer_pair_reconcile_cursor', '{\"at\": 1}')")
            yield dsn, conn
    finally:
        monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
        pg.drop(name)


def _settle_spec():
    import w2e
    spec = w2e.SCENARIOS["sweep-analyzer-tick"]["init_settle"]
    return {**spec, "quiet_s": 0.6, "timeout_s": 5.0}


def _ids(conn, *displays):
    return [conn.execute("SELECT id FROM entities WHERE display = %s", (d,)).fetchone()[0]
            for d in displays]


def _churn(dsn, stop, sql):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        i = 0
        while not stop.is_set():
            i += 1
            conn.execute(sql, (i,))
            stop.wait(0.1)


def _settle_under(dsn, sql, spec):
    import w2e
    stop = threading.Event()
    t = threading.Thread(target=_churn, args=(dsn, stop, sql), daemon=True)
    t.start()
    try:
        w2e.settle_sides(spec, {"python": dsn, "rust": dsn}, None, {})
    finally:
        stop.set()
        t.join()


CURSOR_WALK = ("INSERT INTO meta (key, value) VALUES ('analyzer_duplicate_cursor', "
               "jsonb_build_object('entity_id', %s::bigint)) "
               "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")


def test_the_analyzer_walking_the_real_bank_does_not_keep_it_unsettled(bank):
    dsn, conn = bank
    a, b = _ids(conn, "Real Entity A", "Real Entity B")
    conn.execute("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, "
                 "status, created_at) VALUES ('merge', %s, %s, 0.7, 'analyzer-duplicate', "
                 "'pending', 1)", (a, b))
    _settle_under(dsn, CURSOR_WALK, _settle_spec())
    # Proposals on real entities it never seeded churn too, and do not count.
    _settle_under(dsn, "UPDATE entity_proposals SET score = %s / 1000.0", _settle_spec())


def test_the_whole_judge_digest_still_governs_without_a_scoped_query(bank):
    """The near miss: a settle that declares no ``quiet_sql`` still waits on
    every judge table, so the moving cursor keeps it unsettled."""
    dsn, _ = bank
    spec = {k: v for k, v in _settle_spec().items() if k != "quiet_sql"}
    spec["timeout_s"] = 2.0
    with pytest.raises(RuntimeError, match="did not settle"):
        _settle_under(dsn, CURSOR_WALK, spec)


def test_its_own_rows_moving_keep_it_unsettled(bank):
    """What the scoped query must still see: the scenario's own filings and
    the reconcile cursor."""
    dsn, conn = bank
    own_a, own_b = _ids(conn, "Billing Gateway", "Billing Gateway Service")
    conn.execute("INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, "
                 "status, created_at) VALUES ('merge', %s, %s, 0.7, 'analyzer-duplicate', "
                 "'pending', 1)", (own_a, own_b))
    spec = {**_settle_spec(), "timeout_s": 2.0}
    for churn in ("UPDATE entity_proposals SET score = %s / 1000.0",
                  "UPDATE meta SET value = jsonb_build_object('at', %s::bigint) "
                  "WHERE key = 'analyzer_pair_reconcile_cursor'"):
        with pytest.raises(RuntimeError, match="did not settle"):
            _settle_under(dsn, churn, spec)
