"""A real-bank base: every new disposable database copies it, and a dump
holds only what differs from it.

Needs the disposable-bank login (``pgdisposable``): the bench PostgreSQL, or
the mirror's own inside the forced runner. Without one it skips.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[3]))

import dbstate  # noqa: E402
import pgdisposable  # noqa: E402

pg = pgdisposable.Slice("mir")


def test_a_base_outside_the_mirror_names_is_refused(monkeypatch):
    for name in ("pseudolife_memory", "pl_cf_w2e_bank", "postgres"):
        monkeypatch.setenv(pgdisposable.BASE_ENV, name)
        with pytest.raises(Exception, match="refusing|production"):
            pgdisposable.base_database()
    monkeypatch.setenv(pgdisposable.BASE_ENV, "pl_cf_mir_seed")
    assert pgdisposable.base_database() == "pl_cf_mir_seed"
    monkeypatch.delenv(pgdisposable.BASE_ENV)
    assert pgdisposable.base_database() is None


@pytest.fixture
def base(monkeypatch):
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    name = pg.name("pl_cf_mir_basetest")
    import psycopg
    with psycopg.connect(pg.create(name), autocommit=True) as conn:
        conn.execute("CREATE TABLE notes (id INT PRIMARY KEY, body TEXT NOT NULL)")
        conn.execute("CREATE TABLE marks (tag TEXT)")
        conn.execute("INSERT INTO notes VALUES (1, 'one'), (2, 'two'), (3, 'three')")
        conn.execute("INSERT INTO marks VALUES ('a'), ('a')")
    dbstate._BASE_HASHES.clear()
    yield name
    dbstate._BASE_HASHES.clear()
    pg.drop(name)


def test_new_databases_copy_the_base_and_dumps_hold_only_the_difference(base, monkeypatch):
    import psycopg
    monkeypatch.setenv(pgdisposable.BASE_ENV, base)
    copy = pg.name("pl_cf_mir_copytest")
    try:
        dsn = pg.create(copy)
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
        untouched = dbstate.dump(dsn)["rows"]
        assert untouched["public.notes"]["rows"] == []
        assert untouched["public.notes"]["base_missing"]["count"] == 0
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("UPDATE notes SET body = 'TWO' WHERE id = 2")
            conn.execute("DELETE FROM notes WHERE id = 3")
            conn.execute("INSERT INTO notes VALUES (4, 'four')")
            conn.execute("DELETE FROM marks WHERE ctid = (SELECT min(ctid) FROM marks)")
        changed = dbstate.dump(dsn)["rows"]
        assert changed["public.notes"]["rows"] == [[2, "TWO"], [4, "four"]]
        assert changed["public.notes"]["base_missing"]["count"] == 2
        # A table without a key may hold one row twice: losing one copy counts.
        assert changed["public.marks"]["rows"] == []
        assert changed["public.marks"]["base_missing"]["count"] == 1
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO marks VALUES ('a'), ('a'), ('a')")
        # One copy left plus three is four: two more than the base's two.
        assert dbstate.dump(dsn)["rows"]["public.marks"]["rows"] == [["a"], ["a"]]
        assert dbstate.diff(untouched, changed)
    finally:
        pg.drop(copy)
