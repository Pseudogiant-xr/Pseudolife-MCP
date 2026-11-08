"""A refused maintainer action stays cheap to verify on a large board.

On a copy of a real board (96k board events), each refused step's state dumps
and full-row rollback snapshots outlasted the 120 s maintainer challenge, so
the refusal scenarios answered 410 challenge_expired instead of the refusal
they test (forced real-data run, daemon box, 2026-11-07).
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from board import maintainer  # noqa: E402


def test_a_rollback_step_skips_the_cross_side_state_dump():
    """Its own snapshot proves its side unchanged; both sides start equal,
    so the per-step dump and diff add nothing but time."""
    assert maintainer.post("refused", "role", rollback=True).write is False
    assert maintainer.post("accepted", "role").write is True


@pytest.fixture
def board_db():
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import SCHEMA_SQL
    pg = pgdisposable.Slice("w2f")
    name = pg.name("pl_cf_w2f_snapshot")
    dsn = pg.create(name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(SCHEMA_SQL)
    yield dsn
    pg.drop(name)


def test_the_rollback_snapshot_sees_any_change_and_ignores_row_order(board_db):
    import psycopg
    with psycopg.connect(board_db, autocommit=True) as conn:
        conn.execute("INSERT INTO maintainer_nonces (nonce, expires_at) VALUES ('n1', 1), ('n2', 2)")
        first = maintainer.snapshot(board_db, None)
        conn.execute("DELETE FROM maintainer_nonces")
        conn.execute("INSERT INTO maintainer_nonces (nonce, expires_at) VALUES ('n2', 2), ('n1', 1)")
        assert maintainer.snapshot(board_db, None) == first  # same rows, other order
        conn.execute("UPDATE maintainer_nonces SET expires_at = 3 WHERE nonce = 'n2'")
        changed = maintainer.snapshot(board_db, None)
    assert changed != first
    assert [t for t in first if first[t] != changed[t]] == ["maintainer_nonces"]
    # A digest per table, computed by the server: no row crosses the wire.
    assert all(isinstance(v, tuple) and len(v) == 2 for v in first.values()), first
