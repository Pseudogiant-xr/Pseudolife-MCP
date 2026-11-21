"""The w2d MCP stats seed works on top of a bank that already has a graph.

On a copy of the real bank, `w2d_seed.py --stats-extra` inserted community id
1 and the `graph_digest` meta key, both of which the bank already held:
UniqueViolation, and `mcp-stats` errored on every real-bank attempt (forced
real-data run, daemon box, 2026-11-07). The seed now takes the next free
community id and replaces the digest, which on an empty bank is the same as
before.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))


@pytest.fixture
def bank(monkeypatch):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        import database_gate
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    import psycopg
    from rust.harness_support.import_blocker import cold_harness_imports
    # An empty bank of its own, never a copy of a mirror's base.
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    monkeypatch.delenv("PL_HARNESS_SEED_DSN", raising=False)
    monkeypatch.delenv("PL_HARNESS_SEED_RUST", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    pg = pgdisposable.Slice("w2d")
    name = pg.name("pl_cf_w2d_seed_base")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with cold_harness_imports():
                from rust.harness_support.schema import ensure_schema

                ensure_schema(conn)
            yield conn
    finally:
        pg.drop(name)


def test_the_stats_seed_lands_on_a_bank_that_has_a_graph(bank):
    import w2d_seed
    # What a real bank already holds: its own communities and digest.
    bank.execute("INSERT INTO communities (id, label, size, cohesion, computed_at) VALUES "
                 "(1, 'existing', 4, 0.4, 1.0), (2, 'existing two', 2, 0.6, 1.0)")
    bank.execute("INSERT INTO meta (key, value) VALUES ('graph_digest', '{\"communities\": 2}')")
    w2d_seed.stats_extra(bank)
    rows = bank.execute("SELECT id, label FROM communities ORDER BY id").fetchall()
    assert rows[-1] == (3, "deploy tooling") and len(rows) == 3
    digest = bank.execute("SELECT value FROM meta WHERE key = 'graph_digest'").fetchone()[0]
    assert digest["communities"] == 1


def test_on_an_empty_bank_the_seed_is_unchanged(bank):
    """The near miss: the synthetic bank still gets community 1."""
    import w2d_seed
    w2d_seed.stats_extra(bank)
    assert bank.execute("SELECT id, label FROM communities").fetchall() == [(1, "deploy tooling")]


@pytest.mark.parametrize("name", ["pseudolife_memory", "outside_fixture_namespace"])
def test_seed_connection_refuses_foreign_names_before_connecting(monkeypatch, name):
    import w2d_seed

    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    connect = MagicMock()
    monkeypatch.setattr(w2d_seed.psycopg, "connect", connect)
    with pytest.raises((RuntimeError, ValueError)):
        w2d_seed.open_bank(f"dbname={name}")
    connect.assert_not_called()


def test_move_stats_fixture_name_passes_the_seed_guard(tmp_path):
    import test_move_stats
    import w2d_search
    from principal_fixtures import check_target

    login = tmp_path / "synthetic-login.env"
    login.write_text("PSEUDOLIFE_TEST_PG_USER=fixture\nPSEUDOLIFE_TEST_PG_PASSWORD=synthetic\n",
                     encoding="utf-8")
    space = test_move_stats.bank_space(w2d_search, "127.0.0.1:1", login)
    name = space.name("pl_cf_w2d_template")
    check_target(space.dsn(name))
    assert name.startswith("pl_cf_w2d_r2_")


@pytest.mark.parametrize("missing", [None, "docs", "crafted", "root", "parentless", "steps", "new_steps",
                                    "supersede", "retirement"])
def test_seed_bank_witness_rejects_missing_inputs(missing):
    import w2d_seed as seed

    conn = MagicMock()
    docs = ["fixture document"]
    rows = {
        "docs": [(docs[0],)],
        "crafted": [(text, source, tags or []) for text, source, tags in seed.CRAFTED],
        "root": [("fixture-episode",)],
        "steps": [(text,) for text in seed.EPISODE_TEXTS],
        "supersede": [(seed.SUPERSEDED[0], seed.SUPERSEDED[1], True), (seed.SUPERSEDED[1], None, False)],
    }
    if missing in rows:
        rows[missing].pop()
    if missing == "retirement":
        rows["supersede"][0] = (seed.SUPERSEDED[0], seed.SUPERSEDED[1], False)
    slots = iter(rows)

    def result(query, params):
        slot = next(slots)
        entries = rows[slot]
        cursor = MagicMock()
        if slot == "root":
            if missing == "parentless" and "parent_id IS NULL" in query:
                entries = []
            cursor.fetchone.return_value = entries[0] if entries else None
            cursor.fetchall.return_value = entries
        elif slot in {"docs", "steps"}:
            if missing == "new_steps" and slot == "steps" and "id >" in query:
                entries = []
            cursor.__iter__.return_value = iter(entries)
        else:
            if slot == "supersede" and "superseded_at" not in query:
                entries = [entry[:2] for entry in entries]
            cursor.fetchall.return_value = entries
        return cursor

    conn.execute.side_effect = result
    if missing:
        with pytest.raises(RuntimeError, match="seed precondition not met"):
            seed.check_base(conn, docs, 42)
    else:
        seed.check_base(conn, docs, 42)
        assert conn.execute.call_args_list[0].args[1] == (42,)


@pytest.mark.parametrize("missing", [None, "contender", "restatement", "volatile"])
def test_extras_require_the_persisted_inputs(missing):
    import w2d_seed as seed

    def query(sql, params=()):
        result = MagicMock()
        if sql.startswith("SELECT id FROM episodes"):
            result.fetchall.return_value = [("root",)]
        elif sql.startswith("SELECT e.parent_id"):
            result.fetchall.return_value = [("root",)]
        elif sql.startswith("SELECT status,value"):
            values = [("contested", "Ubuntu 24.04"), ("current", "Debian 13")]
            result.fetchall.return_value = values[1:] if missing == "contender" else values
        elif sql.startswith("SELECT count(*) FROM entries"):
            result.fetchone.return_value = (0 if missing == "restatement" else 1,)
        else:
            assert sql.startswith("UPDATE facts")
            # The copied bank always has Friday, and only the ordinary cell has Thursday.
            result.fetchall.return_value = [] if missing == "volatile" and "thursday afternoon" in params else [(1,)]
        return result

    conn = MagicMock()
    conn.execute.side_effect = query
    if missing:
        with pytest.raises(RuntimeError, match="seed precondition not met"):
            seed.finish_extras(conn, fact=False, stats=False, cutoff=42)
    else:
        seed.finish_extras(conn, fact=False, stats=False, cutoff=42)


def test_chronicle_seed_deduplicates_dated_and_undated_events(bank, monkeypatch):
    import w2d_seed as seed

    monkeypatch.setattr(seed, "check_base", lambda *args: None)
    seed.finish_base(bank, [], 0)
    seed.finish_base(bank, [], 0)
    got = bank.execute("SELECT occurred_phrase,actor,description FROM chronicle_events").fetchall()
    assert sorted(got, key=lambda row: row[2]) == sorted(
        [(phrase, actor, desc) for _, phrase, actor, desc in seed.EVENTS], key=lambda row: row[2])


@pytest.mark.parametrize("missing", [None, "event_window", "community", "digest"])
def test_stats_witness_requires_the_seeded_window_and_graph(monkeypatch, missing):
    import w2d_seed as seed

    now = 1795000000.25
    monkeypatch.setattr(seed, "assert_fixture_database", lambda conn: None)
    monkeypatch.setattr(seed.time, "time", lambda: now)
    events = []

    def query(sql, params=()):
        result = MagicMock()
        if sql.startswith("SELECT id FROM entries"):
            result.__iter__.return_value = iter([(21,), (22,), (23,)])
        elif sql.startswith("SELECT coalesce(max(id),0)"):
            result.fetchone.return_value = (9,)
        elif sql.startswith("INSERT INTO retrieval_events"):
            _, session, payload, created_at = params
            if missing == "event_window":
                created_at -= 31 * 86400
            events.append((session, payload.obj, created_at))
        elif sql.startswith("SELECT session_id,served"):
            result.fetchall.return_value = events if "created_at" in sql else [row[:2] for row in events]
        elif sql.startswith("SELECT id,label,size"):
            result.fetchall.return_value = [] if missing == "community" else [(10, "deploy tooling", 3, 0.5, now - 7200)]
        elif sql.startswith("SELECT value FROM meta"):
            result.fetchone.return_value = None if missing == "digest" else ({"computed_at": 1791000000.25,
                                                                            "communities": 1},)
        return result

    conn = MagicMock()
    conn.execute.side_effect = query
    if missing:
        with pytest.raises(RuntimeError, match="seed precondition not met"):
            seed.stats_extra(conn)
    else:
        seed.stats_extra(conn)
