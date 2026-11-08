"""Board base-delta dumps: under a real-bank base the board harness dumps,
normalizes and clock-checks only the rows that differ from the base.

On a copy of a real board (96k board events, forced real-data run, daemon
box, 2026-11-08) dumping every row after each write step took about 10 s, so
60-second listener windows and waiter liveness expired mid-scenario, and the
clock-window check reported about 270k pre-existing rows. The database cases
need the disposable-bank login (``pgdisposable``); without one they skip.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from board import run  # noqa: E402
from board.norm import Normalizer, Windows  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable  # noqa: E402

pg = pgdisposable.Slice("mir")
OLD = 1_000_000.5          # a base row's clock: outside any captured window
CANONICAL = '{"a":2,"b":1}'
UNSORTED = '{"b":1,"a":2}'  # valid JSON, not Python's canonical text


def test_base_missing_passes_through_normalization_into_the_state_diff():
    norm = Normalizer(Windows())
    gone = {"coordination_events": {"count": 1, "digest": "d1"}}
    kept = {"coordination_events": {"count": 0, "digest": "d0"}}
    states = [run.normalize_state({"coordination_events": [], "meta": [], "sequences": [],
                                   "base_missing": missing}, norm) for missing in (gone, kept)]
    assert states[0]["base_missing"] == gone
    assert any(d.startswith("base_missing:") for d in run.diff_state(*states)), \
        "a base row gone on one side only compared equal"
    assert not norm.problems


def _event(seq: int, prev: str, payload: str, created_at: float) -> dict:
    from pseudolife_memory.storage.coordination import audit_hash
    row = {"seq": seq, "event": "note", "actor": "daemon", "principal": "", "agent_id": "",
           "recipient_agent_id": None, "project": "", "task": "", "message_id": None,
           "payload": payload, "created_at": created_at, "hlc": "", "prev_hash": prev}
    row["hash"] = audit_hash(prev, row)
    return row


def _insert(conn, row: dict) -> None:
    cols = list(row)
    conn.execute(f"INSERT INTO coordination_events ({', '.join(cols)}) "
                 f"VALUES ({', '.join(['%s'] * len(cols))})", [row[c] for c in cols])


def _head(conn) -> tuple[int, str]:
    return conn.execute("SELECT seq, hash FROM coordination_events ORDER BY seq DESC LIMIT 1").fetchone()


@pytest.fixture
def base(monkeypatch):
    """A three-event chain, the second payload not canonical, in a base
    every copy made under ``PL_HARNESS_BASE_DATABASE`` starts from."""
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.coordination import GENESIS_HASH
    from pseudolife_memory.storage.schema import SCHEMA_SQL
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    name = pg.name("pl_cf_mir_boardbase")
    dbstate._BASE_HASHES.clear()
    try:
        with psycopg.connect(pg.create(name), autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(SCHEMA_SQL)
            prev = GENESIS_HASH
            for seq, payload in ((1, CANONICAL), (2, UNSORTED), (3, CANONICAL)):
                row = _event(seq, prev, payload, OLD + seq)
                _insert(conn, row)
                prev = row["hash"]
        yield name
    finally:
        dbstate._BASE_HASHES.clear()
        pg.drop(name)


@pytest.fixture
def copy(base, monkeypatch):
    """Copies of the base, dropped after the test whatever it did."""
    made = []

    def make(label: str) -> str:
        monkeypatch.setenv(pgdisposable.BASE_ENV, base)
        name = pg.name(f"pl_cf_mir_{label}")
        made.append(name)
        return pg.create(name)
    try:
        yield make
    finally:
        for name in made:
            pg.drop(name)


def test_an_untouched_copy_dumps_no_board_rows(copy):
    state = run.dump(copy("boardsame"))
    assert state["coordination_events"] == [], "rows equal to the base were dumped"
    assert all(state["base_missing"][t]["count"] == 0 for t in run.BOARD_TABLES)
    assert set(state["base_missing"]) == set(run.BOARD_TABLES)


def test_new_rewritten_and_deleted_base_rows_show(copy):
    import psycopg
    dsn = copy("boardedit")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE coordination_events SET payload = %s WHERE seq = 2", (CANONICAL,))
        conn.execute("DELETE FROM coordination_events WHERE seq = 3")
        _insert(conn, _event(4, "x" * 64, CANONICAL, time.time()))
    state = run.dump(dsn)
    assert sorted(r["seq"] for r in state["coordination_events"]) == [2, 4]
    rewritten = next(r for r in state["coordination_events"] if r["seq"] == 2)
    assert rewritten["payload"] == CANONICAL and rewritten["created_at"] == OLD + 2
    # The base's versions of seq 2 and seq 3 are both gone.
    assert state["base_missing"]["coordination_events"]["count"] == 2


def test_clock_windows_check_new_rows_but_not_unchanged_base_rows(copy):
    import psycopg
    dsn = copy("boardclock")
    now = time.time()
    with psycopg.connect(dsn, autocommit=True) as conn:
        seq, prev = _head(conn)
        inside = _event(seq + 1, prev, CANONICAL, now)
        _insert(conn, inside)
        _insert(conn, _event(seq + 2, inside["hash"], CANONICAL, 2_000_000.25))
    windows = Windows()
    windows.steps.append((now - 1, now + 1))
    norm = Normalizer(windows)
    run.normalize_state(run.dump(dsn), norm)
    unmatched = [p for p in norm.problems if "does not match the captured clock windows" in p]
    assert any("2000000.25" in p for p in unmatched), "a new row's foreign clock went unreported"
    assert not [p for p in unmatched if "1000001" in p or "1000002" in p or "1000003" in p], \
        f"unchanged base rows were clock-checked: {unmatched}"


def test_chain_check_verifies_every_row_but_reports_only_changed_payloads(copy):
    import psycopg
    dsn = copy("boardchain")
    with psycopg.connect(dsn, autocommit=True) as conn:
        seq, prev = _head(conn)
        _insert(conn, _event(seq + 1, prev, UNSORTED, time.time()))
    problems = run.chain_check(dsn)
    assert [p for p in problems if p.startswith("seq 4:")], "a new non-canonical payload went unreported"
    assert not [p for p in problems if p.startswith("seq 2:")], \
        f"the base's own payload was reported: {problems}"
    assert not [p for p in problems if p.startswith("verify_audit_chain")], problems
    # Verification still walks the base's rows: a chain cut at its start fails.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM coordination_events WHERE seq = 1")
    assert [p for p in run.chain_check(dsn) if p.startswith("verify_audit_chain")]


def _reference_dump(dsn: str) -> dict:
    """``run.dump`` as it was before base deltas: every row of every table."""
    import psycopg
    from psycopg.rows import dict_row
    out = {}
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET extra_float_digits = 3")
        for t in run.BOARD_TABLES:
            out[t] = conn.execute(f"SELECT * FROM {t}").fetchall()
        out["meta"] = conn.execute("SELECT key, value FROM meta WHERE key = ANY(%s) ORDER BY key",
                                   (list(run.META_KEYS),)).fetchall()
        out["sequences"] = conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences WHERE sequencename = ANY(%s) "
            "ORDER BY sequencename", (list(run.SEQUENCES),)).fetchall()
    return out


def test_without_a_base_the_dump_and_its_checks_are_unchanged(base, monkeypatch):
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    dsn = pg.dsn(base)
    state = run.dump(dsn)
    assert "base_missing" not in state
    assert json.dumps(state, sort_keys=True, default=str) == \
        json.dumps(_reference_dump(dsn), sort_keys=True, default=str)
    assert len(state["coordination_events"]) == 3
    norm = Normalizer(Windows())
    run.normalize_state(state, norm)
    assert len([p for p in norm.problems if p.startswith("created_at:")]) == 3
    assert [p for p in run.chain_check(dsn) if p.startswith("seq 2:")]
