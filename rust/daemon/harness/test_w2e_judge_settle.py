"""A judge scenario's settle waits for the proposals it inserted, not every
pending proposal in the bank.

On a copy of a real bank the merge judge also receives proposals the
scenario never filed. It judges 8 per sweep tick while more arrive, so a
settle on "no pending proposal lacks a first verdict" never held, and every
judge-merge scenario errored instead of comparing (forced real-data run,
daemon box, 2026-11-08: ``pending_unjudged`` 24, 32, 44, 52 across ticks).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import w2e  # noqa: E402


def test_the_insert_settle_names_only_its_own_pairs():
    until = w2e.judge_insert("A", "B")["settle"]["until"]
    assert "'write-dedup: a'" in until and "'write-dedup: b'" in until
    assert "'write-dedup: g'" not in until
    # An explicit condition still wins.
    assert w2e.judge_insert("L", until="SELECT true")["settle"]["until"] == "SELECT true"


@pytest.fixture
def bank():
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import ensure_schema
    pg = pgdisposable.Slice("w2e")
    name = pg.name("pl_cf_w2e_judge_settle")
    dsn = pg.create(name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        ensure_schema(conn)  # every migration: the judge columns are among them
        conn.execute("INSERT INTO entities (canonical, display, created_at) VALUES "
                     "('alpha-node', 'Alpha Node', 1.0), ('alpha-service', 'Alpha Service', 1.0), "
                     "('beta-node', 'Beta Node', 1.0), ('beta-store', 'Beta Store', 1.0), "
                     "('real-one', 'Real One', 1.0), ('real-two', 'Real Two', 1.0)")
        yield conn
    pg.drop(name)


def holds(conn, sql: str) -> bool:
    return bool(conn.execute(sql).fetchone()[0])


def test_a_foreign_unjudged_proposal_does_not_hold_the_settle(bank):
    op = w2e.judge_insert("A", "B")
    bank.execute(op["sql"])
    until = op["settle"]["until"]
    assert not holds(bank, until), "own proposals are not judged yet"
    bank.execute("UPDATE entity_proposals SET judge_verdict = 'accept' "
                 "WHERE reason = 'write-dedup: a'")
    assert not holds(bank, until), "B still lacks its first verdict"
    # A proposal the scenario never filed, as the real bank's judge receives.
    bank.execute(f"INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at) "
                 f"VALUES ('merge', {w2e.eid('real-one')}, {w2e.eid('real-two')}, 0.99, "
                 "'write-dedup: real one', 1.0)")
    bank.execute("UPDATE entity_proposals SET judge_verdict = 'reject' "
                 "WHERE reason = 'write-dedup: b'")
    assert holds(bank, until)
