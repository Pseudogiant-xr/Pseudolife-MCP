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
import database_gate  # noqa: E402

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
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
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


# ---- the fast delta: only rows written since the copy are hashed ---------------------

@pytest.fixture
def rich_base(monkeypatch):
    """A base whose rows two transactions wrote, with a keyed table of large
    rows, a keyless table holding a duplicate and an empty table."""
    if not pgdisposable.LOGIN_FILE.exists():
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    for env in (pgdisposable.BASE_ENV, dbstate.BASE_CACHE_ENV, dbstate.FAST_ENV, dbstate.VERIFY_ENV):
        monkeypatch.delenv(env, raising=False)
    name = pg.name("pl_cf_mir_fastbase")
    import psycopg
    with psycopg.connect(pg.create(name), autocommit=True) as conn:
        conn.execute("CREATE TABLE docs (id INT PRIMARY KEY, body TEXT NOT NULL, n DOUBLE PRECISION)")
        conn.execute("INSERT INTO docs SELECT g, repeat(md5(g::text), 300), g / 3.0 "
                     "FROM generate_series(1, 40) g")
        conn.execute("UPDATE docs SET n = -1 WHERE id <= 5")  # a second base xmin
        conn.execute("CREATE TABLE tags (tag TEXT)")
        conn.execute("INSERT INTO tags VALUES ('a'), ('a'), ('b')")
        conn.execute("CREATE TABLE spare (id INT PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE plain (id INT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO plain SELECT g, 'v' || g FROM generate_series(1, 10) g")
        conn.execute("CREATE TYPE mood AS ENUM ('calm', 'busy')")
        conn.execute("CREATE TABLE moods (id INT PRIMARY KEY, m mood)")
        conn.execute("INSERT INTO moods VALUES (1, 'calm'), (2, 'calm'), (3, 'busy')")
        conn.execute("CREATE TABLE times (id INT PRIMARY KEY, ts TIMESTAMP)")
        conn.execute("INSERT INTO times VALUES (1, NULL), (2, '2026-01-02 03:04:05')")
        conn.execute("CREATE TABLE moodsets (id INT PRIMARY KEY, ms mood[])")
        conn.execute("INSERT INTO moodsets VALUES (1, '{calm}'), (2, '{busy}')")
        conn.execute("CREATE DOMAIN dmood AS mood")
        conn.execute("CREATE TABLE dmoods (id INT PRIMARY KEY, m dmood)")
        conn.execute("INSERT INTO dmoods VALUES (1, 'calm'), (2, 'busy')")
        conn.execute("CREATE TYPE pair AS (a INT, b TEXT)")
        conn.execute("CREATE TABLE pairs (id INT PRIMARY KEY, p pair)")
        conn.execute("INSERT INTO pairs VALUES (1, NULL), (2, '(2,y)'), (3, '(3,z)')")
    dbstate._BASE_HASHES.clear()
    monkeypatch.setenv(pgdisposable.BASE_ENV, name)
    yield name
    dbstate._BASE_HASHES.clear()
    monkeypatch.delenv(pgdisposable.BASE_ENV)
    pg.drop(name)


def _deltas(dsn: str) -> dict:
    """Each base table's (fast, full) delta, read in one snapshot as the dump
    reads it."""
    import psycopg
    base = dbstate._base_hashes(dsn)
    out = {}
    with psycopg.connect(dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        for schema, table in dbstate._tables(conn):
            q = f"{schema}.{table}"
            select = dbstate._select_list(conn, schema, table)[0]
            ordering = ", ".join(f'"{c}"::text' for c in dbstate._order_columns(conn, schema, table)) or "1"
            args = (conn, schema, table, base[q], select, ordering)
            out[q] = (dbstate._fast_delta(*args), dbstate._full_delta(*args))
    return out


def _agree(dsn: str) -> dict:
    """Every table's fast delta, after checking it ran and matches the full
    hash."""
    out = {}
    for q, (fast, full) in _deltas(dsn).items():
        assert fast is not None, f"{q}: the fast delta declined an ordinary table"
        assert fast == full, f"{q}: fast {fast[1:]} vs full {full[1:]}"
        out[q] = {"rows": [list(r) for r in fast[1]], "missing": fast[2]["count"]}
    return out


def test_the_fast_delta_reports_exactly_what_the_full_hash_reports(rich_base):
    """Inserts, updates, deletes, rewrites to identical bytes, a delete and
    identical reinsert, duplicates in a keyless table, vacuum and freeze:
    after each, the fast delta equals the full hash's answer."""
    import psycopg
    copy = pg.name("pl_cf_mir_fastcopy")
    try:
        dsn = pg.create(copy)
        state = _agree(dsn)
        assert state["public.docs"] == {"rows": [], "missing": 0}

        def write(*statements):
            with psycopg.connect(dsn, autocommit=True) as conn:
                for s in statements:
                    conn.execute(s)
            return _agree(dsn)

        state = write("INSERT INTO docs VALUES (100, 'new', 1.5)")
        assert state["public.docs"] == {"rows": [[100, "new", 1.5]], "missing": 0}
        state = write("UPDATE docs SET body = 'changed' WHERE id = 7")
        # (ordered as the dump orders: by the key's text)
        assert state["public.docs"] == {"rows": [[100, "new", 1.5], [7, "changed", 7 / 3.0]],
                                        "missing": 1}
        # A rewrite to the same bytes, of a row of either base transaction:
        # a new tuple, but no difference.
        state = write("UPDATE docs SET n = n WHERE id IN (3, 8)")
        assert state["public.docs"]["missing"] == 1 and len(state["public.docs"]["rows"]) == 2
        state = write("DELETE FROM docs WHERE id = 9")
        assert state["public.docs"]["missing"] == 2
        # Deleted and put back byte for byte: no difference either.
        state = write("DELETE FROM docs WHERE id = 10",
                      "INSERT INTO docs SELECT 10, repeat(md5('10'), 300), 10 / 3.0")
        assert state["public.docs"]["missing"] == 2 and len(state["public.docs"]["rows"]) == 2
        # A keyless table: one more copy of a held row, one copy of another gone.
        state = write("INSERT INTO tags VALUES ('a')",
                      "DELETE FROM tags WHERE ctid = (SELECT max(ctid) FROM tags WHERE tag = 'b')")
        assert state["public.tags"] == {"rows": [["a"]], "missing": 1}
        state = write("INSERT INTO spare VALUES (1, 'x')")
        assert state["public.spare"] == {"rows": [[1, "x"]], "missing": 0}
        # Vacuum reclaims the dead tuples and freeze marks the live ones;
        # neither moves a live tuple or changes its raw xmin.
        after = write("VACUUM docs", "VACUUM (FREEZE) docs", "VACUUM (FREEZE) tags")
        assert after == state
    finally:
        pg.drop(copy)


def test_the_fast_delta_declines_a_rewritten_or_altered_table(rich_base):
    """VACUUM FULL moves every row (ctids no longer locate base rows), an
    added column changes every row's text, and so does a renamed enum label
    with neither a rewrite nor a column change: the fast delta declines and
    the full hash answers."""
    import psycopg
    copy = pg.name("pl_cf_mir_fastalter")
    try:
        dsn = pg.create(copy)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM docs WHERE id = 2")
            conn.execute("VACUUM FULL docs")
            # The rows after a deleted middle row slide down onto base ctids
            # written by the same transaction: only the file shows the move.
            conn.execute("DELETE FROM plain WHERE id = 5")
            conn.execute("VACUUM FULL plain")
            conn.execute("ALTER TABLE tags ADD COLUMN extra INT DEFAULT 0")
            conn.execute("ALTER TYPE mood RENAME VALUE 'calm' TO 'still'")
            # The table's own catalog rows stay; only the type's change, and
            # the first row's NULL prints the same.
            conn.execute("ALTER TYPE pair ADD ATTRIBUTE c INT")
        deltas = _deltas(dsn)
        assert deltas["public.moods"][1][2]["count"] == 2  # no row hashes as the base's now
        assert deltas["public.pairs"][1][2]["count"] == 2
        for q in ("public.docs", "public.plain", "public.tags", "public.moods", "public.pairs"):
            assert deltas[q][0] is None, f"{q}: the fast delta vouched for a rewritten or altered table"
        assert deltas["public.docs"][1][2]["count"] == 1
        assert deltas["public.tags"][1][2]["count"] == 3  # every row's text changed
        assert deltas["public.spare"][0] == deltas["public.spare"][1]
        assert dbstate.dump(dsn)["rows"]["public.docs"]["base_missing"]["count"] == 1
    finally:
        pg.drop(copy)


def test_the_fast_delta_declines_a_change_its_canary_row_does_not_show(rich_base):
    """Renaming an enum label only a later row uses (in an enum column, an
    enum array, a domain over the enum), and retyping a column without a
    rewrite where the first row holds NULL, leave the canary row's text as
    the base's and the file as it was; only the catalog shows the change,
    and the fast delta declines on it."""
    import psycopg
    copy = pg.name("pl_cf_mir_fastcanary")
    try:
        dsn = pg.create(copy)
        with psycopg.connect(dsn, autocommit=True) as conn:
            files = {t: conn.execute("SELECT pg_relation_filenode(%s::regclass)", (t,)).fetchone()[0]
                     for t in ("moods", "times")}
            conn.execute("ALTER TYPE mood RENAME VALUE 'busy' TO 'hurried'")
            conn.execute("SET timezone = 'UTC'")  # no rewrite for timestamp -> timestamptz
            conn.execute("ALTER TABLE times ALTER COLUMN ts TYPE TIMESTAMPTZ")
            assert files == {t: conn.execute("SELECT pg_relation_filenode(%s::regclass)",
                                             (t,)).fetchone()[0] for t in files}, "a table was rewritten"
        deltas = _deltas(dsn)
        for q in ("public.moods", "public.times", "public.moodsets", "public.dmoods"):
            fast, full = deltas[q]
            assert full[2]["count"] == 1, f"{q}: the change should reach one base row"
            assert fast is None, f"{q}: the fast delta vouched for a table whose text changed"
    finally:
        pg.drop(copy)


def test_a_session_rendering_values_differently_is_declined(rich_base, monkeypatch):
    """A session setting that renders rows differently from the base read
    (here ``extra_float_digits``) changes no catalog row, and the canary row
    may print the same (a whole number); the settings show it, and the fast
    delta declines."""
    copy = pg.name("pl_cf_mir_fastrender")
    try:
        dsn = pg.create(copy)
        assert _agree(dsn)["public.docs"] == {"rows": [], "missing": 0}  # the base read
        monkeypatch.setenv("PGOPTIONS", "-c extra_float_digits=-3")
        fast, full = _deltas(dsn)["public.docs"]
        assert 0 < full[2]["count"] < 40, "some docs rows, not all, should render differently"
        assert fast is None, "the fast delta vouched for rows that no longer hash as the base's"
    finally:
        pg.drop(copy)


def test_a_frozen_base_still_tells_its_rows_from_new_ones(rich_base):
    """Freezing keeps a row's raw xmin, in the base before it is read and in
    the copy after a write: a new frozen row is still a candidate."""
    import psycopg
    copy = pg.name("pl_cf_mir_fastfrozen")
    try:
        with psycopg.connect(pg.dsn(rich_base), autocommit=True) as conn:
            conn.execute("VACUUM (FREEZE) docs")
        dsn = pg.create(copy)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO docs VALUES (101, 'frozen', 2.5)")
            conn.execute("VACUUM (FREEZE) docs")
            # Frozen only if no open transaction on the shared bench held
            # the horizon back: relfrozenxid passed the new row's xmin.
            frozen = conn.execute(
                "SELECT (SELECT relfrozenxid FROM pg_class WHERE oid = 'docs'::regclass)::text::bigint "
                "> xmin::text::bigint FROM docs WHERE id = 101").fetchone()[0]
        if not frozen:
            pytest.skip("an open transaction on the bench kept VACUUM FREEZE from freezing the row")
        assert _agree(dsn)["public.docs"] == {"rows": [[101, "frozen", 2.5]], "missing": 0}
    finally:
        pg.drop(copy)


def test_an_unwritable_cache_only_costs_the_cache(rich_base, monkeypatch, tmp_path):
    """The cache saves time; a directory that cannot be written leaves this
    process with its own read, and the dump answers."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    monkeypatch.setenv(dbstate.BASE_CACHE_ENV, str(blocker / "cache"))
    copy = pg.name("pl_cf_mir_fastnocache")
    try:
        dsn = pg.create(copy)
        assert dbstate.dump(dsn)["rows"]["public.docs"]["base_missing"]["count"] == 0
        assert list(tmp_path.iterdir()) == [blocker]
    finally:
        pg.drop(copy)


def test_verify_mode_refuses_a_fast_delta_that_disagrees(rich_base, monkeypatch):
    """``PL_HARNESS_DUMP_VERIFY=1`` (the A/B runs) checks every fast answer
    against the full hash; a wrong one stops the dump."""
    import psycopg
    copy = pg.name("pl_cf_mir_fastverify")
    try:
        dsn = pg.create(copy)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM docs WHERE id = 4")
        monkeypatch.setenv(dbstate.VERIFY_ENV, "1")
        assert dbstate.dump(dsn)["rows"]["public.docs"]["base_missing"]["count"] == 1
        real = dbstate._fast_delta

        def wrong(*a):
            got = real(*a)
            return got and (got[0], got[1], {**got[2], "count": got[2]["count"] + 1})
        monkeypatch.setattr(dbstate, "_fast_delta", wrong)
        with pytest.raises(dbstate.DeltaMismatch):
            dbstate.dump(dsn)
        monkeypatch.delenv(dbstate.VERIFY_ENV)
        assert dbstate.dump(dsn)["rows"]["public.docs"]["base_missing"]["count"] == 2  # unchecked
    finally:
        pg.drop(copy)


def test_the_kill_switch_hashes_every_row(rich_base, monkeypatch):
    """``PL_HARNESS_DUMP_FAST=0`` takes the full hash, whatever the fast
    delta would answer."""
    copy = pg.name("pl_cf_mir_fastoff")
    try:
        dsn = pg.create(copy)
        monkeypatch.setattr(dbstate, "_fast_delta", lambda *a: pytest.fail("the fast delta ran"))
        monkeypatch.setenv(dbstate.FAST_ENV, "0")
        assert dbstate.dump(dsn)["rows"]["public.docs"]["base_missing"]["count"] == 0
    finally:
        pg.drop(copy)


def test_a_run_reads_each_base_once_and_again_after_it_changes(rich_base, monkeypatch, tmp_path):
    """With ``PL_HARNESS_BASE_CACHE`` a second harness process loads the
    first one's read; a write to the base invalidates it."""
    import psycopg
    monkeypatch.setenv(dbstate.BASE_CACHE_ENV, str(tmp_path))
    reads = []
    real = dbstate._read_base_table
    monkeypatch.setattr(dbstate, "_read_base_table", lambda *a: reads.append(a[1:]) or real(*a))
    copy = pg.name("pl_cf_mir_fastcache")
    try:
        dsn = pg.create(copy)
        first = {q: dict(c) for q, c in dbstate._base_hashes(dsn).items()}
        assert reads and (tmp_path / f"{rich_base}.json").exists()
        read_once = len(reads)
        dbstate._BASE_HASHES.clear()  # a new harness process
        assert {q: dict(c) for q, c in dbstate._base_hashes(dsn).items()} == first
        assert len(reads) == read_once, "the second process read the base again"
        assert _agree(dsn)["public.docs"] == {"rows": [], "missing": 0}
        with psycopg.connect(pg.dsn(rich_base), autocommit=True) as conn:
            conn.execute("UPDATE docs SET body = 'base moved' WHERE id = 1")
        dbstate._BASE_HASHES.clear()
        moved = dbstate._base_hashes(dsn)
        assert len(reads) == 2 * read_once, "a changed base was not read again"
        assert moved["public.docs"] != first["public.docs"]
        # An added column changes every row's text, but no row's xmin, the
        # rows' count or the file (a row added and deleted leaves the census).
        with psycopg.connect(pg.dsn(rich_base), autocommit=True) as conn:
            conn.execute("INSERT INTO plain VALUES (11, 'v11')")
            conn.execute("DELETE FROM plain WHERE id = 11")
            conn.execute("ALTER TABLE plain ADD COLUMN w INT DEFAULT 7")
        dbstate._BASE_HASHES.clear()
        altered = dbstate._base_hashes(dsn)
        assert len(reads) == 3 * read_once, "an altered base was not read again"
        assert altered["public.plain"] != moved["public.plain"]
        # A process whose session renders values differently hashes them
        # differently: it reads the base itself.
        monkeypatch.setenv("PGOPTIONS", "-c extra_float_digits=-3")
        dbstate._BASE_HASHES.clear()
        rendered = dbstate._base_hashes(dsn)
        assert len(reads) == 4 * read_once, "a cache hashed under other settings was reused"
        assert rendered["public.docs"] != altered["public.docs"]
    finally:
        pg.drop(copy)
