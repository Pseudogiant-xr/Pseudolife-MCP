"""The lease-recovery checks read the scenario's own lease, not the only one."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from board import lease_recovery  # noqa: E402


def _ctx():
    return SimpleNamespace(id=lambda who: f"id-{who}")


def _lease(name, holder, queue=()):
    return {"name": name, "holder": {"agent_id": holder}, "queue": [{"agent_id": a} for a in queue]}


def test_the_holder_check_finds_the_scenario_lease_among_a_real_boards_own():
    """On a copy of a real board the listing also holds its own leases: the
    single-row unpack raised (lease-* scenarios, forced run, 2026-11-07)."""
    step = lease_recovery.holder(SimpleNamespace(), "A", "BC")
    response = {"status": 200, "json": {"leases": [
        _lease("full-suite", "real-agent"), _lease("resource", "id-A", ("id-B", "id-C")),
        _lease("conductor:other", "real-agent-2")]}}
    step.check(None, _ctx(), response, None)


def test_the_holder_check_still_refuses_a_wrong_holder_or_a_missing_lease():
    step = lease_recovery.holder(SimpleNamespace(), "A", "")
    with pytest.raises(AssertionError):
        step.check(None, _ctx(), {"status": 200, "json": {"leases": [_lease("resource", "id-B")]}}, None)
    with pytest.raises(AssertionError):
        step.check(None, _ctx(), {"status": 200, "json": {"leases": [_lease("full-suite", "id-A")]}}, None)


@pytest.fixture
def seed_bank_dsn(monkeypatch):
    import database_gate
    import pgdisposable
    import psycopg
    from rust.harness_support.schema import ensure_schema

    if not pgdisposable.LOGIN_FILE.exists():
        database_gate.unavailable("no disposable-bank login here",
                                  "point PSEUDOLIFE_TEST_PG_LOGIN_FILE at a test login")
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    pg = pgdisposable.Slice("w2f")
    name = pg.name("pl_cf_w2f_seed_rows")
    try:
        dsn = pg.create(name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            ensure_schema(conn)
        yield dsn
    finally:
        pg.drop(name)


def test_paging_seed_preserves_a_full_mailbox_and_the_later_grant(seed_bank_dsn):
    import psycopg
    from psycopg.rows import dict_row
    from rust.harness_support.audit_oracle import verify_audit_chain
    from rust.harness_support.import_blocker import cold_harness_imports

    with psycopg.connect(seed_bank_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO coordination_agents (agent_id, principal, created_at, last_activity) "
            "SELECT agent, 'default', 1, 1 FROM unnest(%s::text[]) AS agent",
            (["id-A", "id-B", "id-C"],))
        with cold_harness_imports():
            from board import run
            from board import lease_recovery as cold
            step = next(s for s in cold.LeaseNoticePages().steps()
                        if s.name == "full first page of grant recipients")
            arm = run.Arm("rust", None, seed_bank_dsn)
            arm.ctx = _ctx()
            arm.run(step)

        assert conn.execute(
            "SELECT recipient_sequence, request_id, text FROM coordination_messages "
            "WHERE recipient_agent_id='id-B' AND acknowledged_at IS NULL ORDER BY recipient_sequence"
        ).fetchall() == [(i + 1, f"pending-{i}", "pending") for i in range(256)]
        assert [r[0] for r in conn.execute(
            "SELECT count(*) FROM coordination_messages GROUP BY sender_agent_id ORDER BY count(*) DESC"
        )] == [60, 60, 60, 60, 16]
        assert conn.execute(
            "SELECT name, holder_agent_id, fence, expires_at-acquired_at FROM coordination_leases ORDER BY name"
        ).fetchall() == [(f"resource-{i:02}", "id-B" if i < 50 else "id-C", 2 * i + 2, 300)
                         for i in range(51)]
        assert conn.execute("SELECT last_value, is_called FROM coordination_lease_fence").fetchone() == (102, True)
        assert conn.execute(
            "SELECT last_value, is_called FROM coordination_lease_waiters_ticket_seq"
        ).fetchone() == (51, True)
        assert not conn.execute("SELECT 1 FROM coordination_lease_waiters").fetchone()
        assert not conn.execute("SELECT 1 FROM coordination_wakes").fetchone()
        with conn.cursor(row_factory=dict_row) as cursor:
            events = cursor.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall()
        assert verify_audit_chain(events)["ok"]
        assert [row["event"] for row in events] == (
            [kind for i in range(256) for kind in (["register", "send"] if i % 60 == 0 else ["send"])]
            + ["lease_acquire", "lease_queue", "lease_release", "lease_grant"] * 51)


def test_board_seed_refuses_a_bank_outside_its_disposable_slice(monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock

    import psycopg
    from board.scenarios import seed_bank

    conn = Mock()
    conn.execute.return_value.fetchone.return_value = ("pl_cf_w2e_other_bank",)
    conn.transaction.return_value = nullcontext()
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: nullcontext(conn))
    seed = Mock()
    with pytest.raises(ValueError, match="outside a w2f disposable bank"):
        seed_bank("unused", seed, _ctx())
    conn.execute.assert_called_once_with("SELECT current_database()")
    seed.assert_not_called()


def test_startup_seed_keeps_first_acquisition_before_second_registration(seed_bank_dsn):
    import psycopg
    from board.scenarios import Ctx, seed_bank
    from board.seed_rows import startup_grant

    ctx = Ctx()
    seed_bank(seed_bank_dsn, startup_grant, ctx)
    a, b = ctx.id("A"), ctx.id("B")
    with psycopg.connect(seed_bank_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT event, agent_id FROM coordination_events ORDER BY seq").fetchall() == [
            ("register", a), ("lease_acquire", a), ("register", b), ("lease_queue", b),
            ("lease_release", a), ("lease_grant", b)]
