"""Two arms on a copy of a real bank, compared as the w2e and route harnesses
compare them.

A step may legitimately rewrite a base row with a fresh clock (a reaper's
close, a slot read's ``last_read_at``), and each daemon writes its own now():
that clock, inside the arm's captured clock window, compares as a write
during the run, while a base row's clock outside every window is still
compared exactly. A retrieval event's served scores rest on float32
arithmetic over the bank's embeddings, so they compare within the search
harnesses' score tolerance, and nothing else does.

The clock cases need the disposable-bank login (``pgdisposable``); without
one they skip. The score cases need no database.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

import dbstate  # noqa: E402
import pgdisposable  # noqa: E402
import database_gate  # noqa: E402

pg = pgdisposable.Slice("mir")
RULES = {("stamps", "seen"): "clock"}
# The arm's captured clock window, in wall-clock seconds: synthetic, so the
# cases do not depend on the host clock.
WINDOW = (100.0, 200.0)


@pytest.fixture
def base(monkeypatch):
    """A base holding one stamped row, its clock far before the window."""
    if not pgdisposable.LOGIN_FILE.exists():
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    for env in (pgdisposable.BASE_ENV, dbstate.BASE_CACHE_ENV, dbstate.FAST_ENV, dbstate.VERIFY_ENV):
        monkeypatch.delenv(env, raising=False)
    name = pg.name("pl_cf_mir_clockbase")
    import psycopg
    with psycopg.connect(pg.create(name), autocommit=True) as conn:
        conn.execute("CREATE TABLE stamps (id INT PRIMARY KEY, body TEXT NOT NULL, "
                     "seen DOUBLE PRECISION)")
        conn.execute("INSERT INTO stamps VALUES (1, 'base', 0.5), (2, 'other', 0.5)")
    dbstate._BASE_HASHES.clear()
    yield name
    dbstate._BASE_HASHES.clear()
    pg.drop(name)


@pytest.fixture
def arms(base, monkeypatch):
    """Two copies of the base, with their dumps before either arm wrote."""
    monkeypatch.setenv(pgdisposable.BASE_ENV, base)
    names = {s: pg.name(f"pl_cf_mir_clock{s}") for s in ("py", "rs")}
    try:
        dsns = {s: pg.create(n) for s, n in names.items()}
        yield dsns, {s: dbstate.dump(d) for s, d in dsns.items()}
    finally:
        for n in names.values():
            pg.drop(n)


def _write(dsn: str, statement: str) -> None:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(statement)


def _after(dsns: dict, before: dict, windows=(WINDOW,)) -> dict:
    return {s: dbstate.normalize(dbstate.dump(dsns[s]), RULES, before[s], windows=windows)
            for s in dsns}


def test_a_base_row_both_arms_restamp_inside_the_window_compares_equal(arms):
    dsns, before = arms
    for side, stamp in (("py", 150.25), ("rs", 160.5)):
        _write(dsns[side], f"UPDATE stamps SET seen = {stamp} WHERE id = 1")
    after = _after(dsns, before)
    assert dbstate.diff(after["py"], after["rs"]) == []
    # A fresh stamp normalizes as a new row's clock does.
    assert after["py"]["rows"]["public.stamps"]["rows"] == [[1, "base", "<seen>"]]


def test_a_base_row_restamped_outside_the_window_still_differs(arms):
    """The near miss: a rewritten clock the run's window does not explain
    is compared exactly, as before."""
    dsns, before = arms
    for side, stamp in (("py", 250.25), ("rs", 260.5)):
        _write(dsns[side], f"UPDATE stamps SET seen = {stamp} WHERE id = 1")
    after = _after(dsns, before)
    assert dbstate.diff(after["py"], after["rs"]), "an out-of-window clock rewrite was hidden"
    assert after["py"]["rows"]["public.stamps"]["rows"] == [[1, "base", 250.25]]
    # Without a window the rule is today's: every base row's clock is exact.
    plain = _after(dsns, before, windows=None)
    assert plain["py"]["rows"]["public.stamps"]["rows"] == [[1, "base", 250.25]]


def test_a_clock_one_arm_keeps_and_the_other_restamps_still_differs(arms):
    """The near miss: both arms rewrite the row, only one restamps its clock;
    the kept base clock stays raw, so the arms differ."""
    dsns, before = arms
    _write(dsns["py"], "UPDATE stamps SET body = 'edited' WHERE id = 1")
    _write(dsns["rs"], "UPDATE stamps SET body = 'edited', seen = 150.25 WHERE id = 1")
    after = _after(dsns, before)
    assert after["py"]["rows"]["public.stamps"]["rows"] == [[1, "edited", 0.5]]
    assert after["rs"]["rows"]["public.stamps"]["rows"] == [[1, "edited", "<seen>"]]
    assert dbstate.diff(after["py"], after["rs"])


def test_a_base_row_only_one_arm_restamps_still_differs(arms):
    """The near miss: the untouched arm does not dump the row at all."""
    dsns, before = arms
    _write(dsns["rs"], "UPDATE stamps SET seen = 150.25 WHERE id = 1")
    after = _after(dsns, before)
    py, rs = (after[s]["rows"]["public.stamps"] for s in ("py", "rs"))
    assert (py["rows"], py["base_missing"]["count"]) == ([], 0)
    assert (rs["rows"], rs["base_missing"]["count"]) == ([[1, "base", "<seen>"]], 1)
    assert dbstate.diff(after["py"], after["rs"])


# ---- retrieval-event scores: within the search harnesses' tolerance ----------------------

def _events(dense: float, score: float = 0.5, rank: int = 0, query: str = "probe") -> dict:
    served = [{"entry_id": 7, "score": score, "rank": rank, "via": "dense", "bank": "flat",
               "components": {"dense": dense, "bm25": 1.25, "recency": 0.0}}]
    return {"catalog": {}, "rows": {
        "public.retrieval_events": {"columns": ["id", "query_text", "served", "created_at"],
                                    "rows": [[1, query, served, "<created_at>"]]},
        "public.notes": {"columns": ["id", "weight"], "rows": [[1, 0.25]]}}}


def _diff(a: dict, b: dict) -> list[str]:
    return dbstate.diff(*dbstate.event_scores_free(a, b))


def test_a_dense_score_within_float32_noise_compares_equal():
    assert _diff(_events(0.3123456), _events(0.3123456 + 1e-6)) == []
    assert _diff(_events(0.3, score=0.5), _events(0.3, score=0.5 + 1.4e-6)) == []


def test_a_score_apart_by_more_than_the_tolerance_still_differs():
    tol = dbstate.EVENT_SCORE_TOL
    assert _diff(_events(0.3), _events(0.3 + 1e-3))
    assert _diff(_events(0.3), _events(0.3 + 10 * tol))
    assert _diff(_events(0.3, score=0.5), _events(0.3, score=0.5 + 10 * tol))


def test_no_other_column_or_field_is_loosened():
    # An event's other served fields and columns are compared exactly.
    assert _diff(_events(0.3), _events(0.3, rank=1))
    assert _diff(_events(0.3), _events(0.3, query="probe two"))
    # So is a float outside the retrieval events.
    a, b = _events(0.3), _events(0.3)
    b["rows"]["public.notes"]["rows"] = [[1, 0.25 + 1e-9]]
    assert _diff(a, b)
    # The inputs are left as they are.
    a, b = _events(0.3), _events(0.3 + 1e-6)
    _diff(a, b)
    assert a["rows"]["public.retrieval_events"]["rows"][0][2][0]["components"]["dense"] == 0.3


def test_the_route_and_w2e_comparisons_share_the_tolerance():
    import w2e
    assert w2e.EVENT_SCORE_TOL == dbstate.EVENT_SCORE_TOL
    assert w2e.EVENT_FLOAT_KEYS == dbstate.EVENT_FLOAT_KEYS
