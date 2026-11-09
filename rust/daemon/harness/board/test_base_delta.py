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


def test_new_rewritten_and_deleted_base_rows_show(copy, monkeypatch):
    """The board's dump, with its own session settings, takes the fast
    delta for every table, and it shows new, rewritten and deleted base
    rows."""
    import psycopg
    dsn = copy("boardedit")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE coordination_events SET payload = %s WHERE seq = 2", (CANONICAL,))
        conn.execute("DELETE FROM coordination_events WHERE seq = 3")
        _insert(conn, _event(4, "x" * 64, CANONICAL, time.time()))
    answered, real = [], dbstate._fast_delta

    def fast(*args):
        got = real(*args)
        if got is not None:
            answered.append(args[2])
        return got
    monkeypatch.setattr(dbstate, "_fast_delta", fast)
    state = run.dump(dsn)
    assert sorted(answered) == sorted(run.BOARD_TABLES), "the board's dump declined the fast delta"
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


def test_rewritten_base_rows_keep_their_old_clocks_without_a_clock_problem(copy):
    """A step that rewrites base rows (the scenarios' ``age_bank`` shifts
    every row's clocks) dumps them with clocks no captured window holds:
    like ``dbstate.normalize``, a row whose key the base holds is an
    existing row, compared exactly rather than clock-checked."""
    import psycopg
    dsn = copy("boardaged")
    now = time.time()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE coordination_events SET created_at = created_at - 50")
        seq, prev = _head(conn)
        _insert(conn, _event(seq + 1, prev, CANONICAL, 2_000_000.25))
    windows = Windows()
    windows.steps.append((now - 1, now + 1))
    norm = Normalizer(windows)
    state = run.normalize_state(run.dump(dsn), norm)
    unmatched = [p for p in norm.problems if "does not match the captured clock windows" in p]
    assert not [p for p in unmatched if "999951" in p or "999952" in p or "999953" in p], \
        f"rewritten base rows were clock-checked: {unmatched}"
    # The near miss: a new row's foreign clock is still reported.
    assert any("2000000.25" in p for p in unmatched), "a new row's foreign clock went unreported"
    # The rewritten rows' clocks stay raw, so a difference between arms shows.
    assert any('"created_at": 999951' in r for r in state["coordination_events"])


def test_a_new_waiter_on_a_lease_the_base_queues_on_is_still_clock_checked(base, copy):
    """Review (Codex): a waiter's key is (name, agent_id), not its first
    column, so a new waiter on a lease the base already queues on is a new
    row, checked against the captured windows; the base's own waiter,
    rewritten, is compared exactly."""
    import psycopg
    with psycopg.connect(pg.dsn(base), autocommit=True) as conn:
        conn.execute("INSERT INTO coordination_lease_waiters (name, agent_id, principal, ttl, "
                     "enqueued_at) VALUES ('gpu', 'agent-base', 'p', 60, %s)", (OLD,))
    dbstate._BASE_HASHES.clear()
    dsn = copy("boardwaiter")
    now = time.time()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("UPDATE coordination_lease_waiters SET enqueued_at = enqueued_at - 50")
        conn.execute("INSERT INTO coordination_lease_waiters (name, agent_id, principal, ttl, "
                     "enqueued_at) VALUES ('gpu', 'agent-new', 'p', 60, 2000000.25)")
    windows = Windows()
    windows.steps.append((now - 1, now + 1))
    norm = Normalizer(windows)
    run.normalize_state(run.dump(dsn), norm)
    unmatched = [p for p in norm.problems if "does not match the captured clock windows" in p]
    assert any("2000000.25" in p for p in unmatched), f"a new waiter went unchecked: {unmatched}"
    assert not [p for p in unmatched if "999950.5" in p], \
        f"the base's rewritten waiter was clock-checked: {unmatched}"


def test_a_rewritten_base_row_keeps_its_non_clock_problems(monkeypatch):
    """Review (Codex): only the clock-window problems of an existing row are
    dropped; any other check a normalizer makes on it (the maintainer's proof
    verification) still reports."""
    class Checks(Normalizer):
        def value(self, v, key=None):
            if key is None and isinstance(v, dict) and "seq" in v:  # the row, not its payload
                self.problems.append("maintainer response MAC does not authenticate its payload")
                self.unmatched("created_at", 999951.5)
            return super().value(v, key)

    monkeypatch.setattr(run, "_base_row_keys",
                        lambda table: (["seq"], {json.dumps([2])}) if table == "coordination_events"
                        else ([], set()))
    norm = Checks(Windows())
    state = {"coordination_events": [{"seq": 2, "created_at": 999951.5, "payload": CANONICAL}],
             "meta": [],
             "sequences": [], "base_missing": {"coordination_events": {"count": 1, "digest": "d"}}}
    run.normalize_state(state, norm)
    assert "maintainer response MAC does not authenticate its payload" in norm.problems
    assert not [p for p in norm.problems if "captured clock windows" in p]


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


# ---- responses that list base rows ------------------------------------------------
# The Console's board view lists the copied board's newest events (owner
# scope), its held leases and their holders; the maintainer status lists the
# base's key changes as ``at``. Those clocks predate every captured window.

LEASE_AT = 1_500_000.75    # a base lease's acquired_at


def _clock_problems(norm) -> list[str]:
    return [p for p in norm.problems if "does not match the captured clock windows" in p]


@pytest.fixture
def board_base(base):
    """The base with one held lease beside its three events."""
    import psycopg
    with psycopg.connect(pg.dsn(base), autocommit=True) as conn:
        conn.execute("INSERT INTO coordination_leases (name, holder_agent_id, holder_principal, "
                     "fence, acquired_at) VALUES ('suite', 'agent-base', 'p', 1, %s)", (LEASE_AT,))
    dbstate._BASE_HASHES.clear()
    return base


def _windowed_norm(cls=Normalizer):
    windows = Windows()
    now = time.time()
    windows.steps.append((now - 1, now + 1))
    return cls(windows)


def test_a_response_listing_base_rows_reports_no_clock_problems(board_base, copy):
    """The Console view of a copied board: base events' created_at, a base
    lease holder's acquired_at and a status age read from a base event."""
    run.dump(copy("boardview"))
    norm = _windowed_norm()
    body = {"events": [{"seq": 1, "created_at": OLD + 1}, {"seq": 3, "created_at": OLD + 3}],
            "leases": [{"name": "suite", "holder": {"acquired_at": LEASE_AT, "expires_at": None}}],
            "agents": [{"agent_id": "x", "status_set_at": OLD + 2}]}
    out = run.normalize_response({"status": 200, "json": body}, norm)
    assert not _clock_problems(norm), f"base rows' clocks were window-checked: {norm.problems}"
    # Compared raw, so an arm serving another value still differs.
    assert out["json"]["events"][0]["created_at"] == OLD + 1


def test_a_new_rows_foreign_clock_in_a_response_is_still_reported(board_base, copy):
    """The near miss: beside base rows, a clock the base does not hold (a new
    event, or the base lease re-acquired with a wrong clock) is reported."""
    run.dump(copy("boardnear"))
    norm = _windowed_norm()
    body = {"events": [{"seq": 9, "created_at": 2_000_000.25}, {"seq": 1, "created_at": OLD + 1}],
            "leases": [{"name": "suite", "holder": {"acquired_at": 2_000_001.5}}]}
    run.normalize_response({"status": 200, "json": body}, norm)
    unmatched = _clock_problems(norm)
    assert any("2000000.25" in p for p in unmatched), f"a new event's clock went unreported: {unmatched}"
    assert any("2000001.5" in p for p in unmatched), f"a rewritten lease's clock went unreported: {unmatched}"
    assert not [p for p in unmatched if "1000001.5" in p], unmatched


def test_maintainer_status_key_changes_from_the_base_report_no_clock_problems(board_base, copy):
    """``key_changes`` reads base ``maintainer_key`` events as ``at``; a new
    key change's foreign ``at`` is still reported."""
    from board.maintainer import SignedNormalizer
    run.dump(copy("boardkeys"))
    norm = _windowed_norm(SignedNormalizer)
    body = {"key_changes": [{"at": 2_000_000.25, "change": "enrol"}, {"at": OLD + 2, "change": "enrol"}]}
    run.normalize_response({"status": 200, "json": body}, norm)
    unmatched = _clock_problems(norm)
    assert not [p for p in unmatched if "1000002.5" in p], f"a base key change was window-checked: {unmatched}"
    assert any("2000000.25" in p for p in unmatched), f"a new key change went unreported: {unmatched}"


def test_a_base_passkey_listed_by_status_reports_no_clock_problems(board_base, copy):
    """A scenario's extra tables (the maintainer's) count as base too."""
    import psycopg
    from board.maintainer import TABLES, SignedNormalizer
    with psycopg.connect(pg.dsn(board_base), autocommit=True) as conn:
        conn.execute("INSERT INTO maintainer_passkeys (credential_id, public_key, alg, label, "
                     "enrolled_by, state, created_at, revoked_at) VALUES "
                     "('base-key', %s, -8, 'old', 'bootstrap', 'revoked', %s, %s)", (b"k", OLD, OLD + 7))
    dbstate._BASE_HASHES.clear()
    run.dump(copy("boardpasskey"), TABLES)
    norm = _windowed_norm(SignedNormalizer)
    body = {"passkeys": [{"credential_id": "base-key", "created_at": OLD, "revoked_at": OLD + 7}]}
    run.normalize_response({"status": 200, "json": body}, norm, TABLES)
    assert not _clock_problems(norm), f"a base passkey was window-checked: {norm.problems}"


def test_a_new_row_copying_a_base_clock_is_still_reported_by_the_state_dump(board_base, copy):
    """The backstop: a response leaves a base-held clock unchecked, but a new
    row stored with that clock (a daemon copying an old clock) has a key the
    base lacks, so the state dump still reports it."""
    import psycopg
    dsn = copy("boardcopyclock")
    with psycopg.connect(dsn, autocommit=True) as conn:
        seq, prev = _head(conn)
        _insert(conn, _event(seq + 1, prev, CANONICAL, OLD + 1))
    norm = _windowed_norm()
    run.normalize_response({"status": 200, "json": {"events": [{"seq": seq + 1, "created_at": OLD + 1}]}},
                           norm)
    assert not _clock_problems(norm)
    run.normalize_state(run.dump(dsn), norm)
    assert any("1000001.5" in p for p in _clock_problems(norm)), \
        "a new row stored with a base row's clock went unreported"


def test_without_a_base_a_response_reports_every_foreign_clock(monkeypatch):
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    norm = _windowed_norm()
    run.normalize_response({"status": 200, "json": {"events": [{"seq": 1, "created_at": OLD + 1}],
                                                    "key_changes": [{"at": OLD + 2}]}}, norm)
    assert len(_clock_problems(norm)) == 1  # ``at`` is a maintainer key only
    from board.maintainer import SignedNormalizer
    norm = _windowed_norm(SignedNormalizer)
    run.normalize_response({"status": 200, "json": {"key_changes": [{"at": OLD + 2}]}}, norm)
    assert len(_clock_problems(norm)) == 1
