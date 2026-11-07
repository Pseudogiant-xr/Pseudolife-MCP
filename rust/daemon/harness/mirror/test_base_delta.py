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
        conn.execute("CREATE TABLE stamps (id INT PRIMARY KEY, seen DOUBLE PRECISION)")
        conn.execute("INSERT INTO stamps VALUES (1, 0.5)")
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


def test_a_rewritten_base_row_keeps_its_clock_compared_exactly(base, monkeypatch):
    """A row equal to the base is not dumped, but it existed: a clock one side
    rewrites on it must still differ (normalize's existing-row rule), and a
    consumer comparing it with its earlier self gets the base's version."""
    import psycopg
    monkeypatch.setenv(pgdisposable.BASE_ENV, base)
    sides = {s: pg.name(f"pl_cf_mir_clock{s}") for s in ("py", "rs")}
    try:
        dsns = {s: pg.create(n) for s, n in sides.items()}
        before = {s: dbstate.dump(d) for s, d in dsns.items()}
        assert before["py"]["rows"]["public.stamps"]["rows"] == []  # the base row is not dumped
        for s, stamp in (("py", "1.0"), ("rs", "2.0")):
            with psycopg.connect(dsns[s], autocommit=True) as conn:
                conn.execute(f"UPDATE stamps SET seen = {stamp} WHERE id = 1")
        rules = {("stamps", "seen"): "clock"}
        after = {s: dbstate.normalize(dbstate.dump(dsns[s]), rules, before[s]) for s in dsns}
        assert dbstate.diff(after["py"], after["rs"]), "a one-sided clock rewrite was hidden"
        assert dbstate.prior_rows(before["py"], "public.stamps", [[1, 1.0]]) == [[1, 0.5]]
    finally:
        for n in sides.values():
            pg.drop(n)
