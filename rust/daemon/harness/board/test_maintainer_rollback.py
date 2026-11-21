"""A refused maintainer action stays cheap to verify on a large board.

On a copy of a real board (96k board events), each refused step's state dumps
and full-row rollback snapshots outlasted the 120 s maintainer challenge, so
the refusal scenarios answered 410 challenge_expired instead of the refusal
they test (forced real-data run, daemon box, 2026-11-07).
"""
import sys
from pathlib import Path

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
def board_db(monkeypatch):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        import database_gate
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    import psycopg
    from rust.harness_support.schema import ensure_schema
    # The assertions in this module account for the whole unit-test bank.
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    pg = pgdisposable.Slice("w2f")
    name = pg.name("pl_cf_w2f_snapshot")
    try:
        dsn = pg.create(name)
        with psycopg.connect(dsn, autocommit=True) as conn:
            ensure_schema(conn)
        yield dsn
    finally:
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
        # A refusal that spends a lease fence number is seen too: the skipped
        # cross-side dump held the sequences and meta keys as well.
        conn.execute("SELECT nextval('coordination_lease_fence')")
        fenced = maintainer.snapshot(board_db, None)
    assert [t for t in changed if changed[t] != fenced[t]] == ["sequences"]
    # A digest per table, computed by the server: no row crosses the wire.
    assert all(isinstance(first[t], tuple) and len(first[t]) == 2
               for t in first if t not in ("meta", "sequences")), first


def test_host_code_reissue_keeps_history_and_replaces_only_the_pending_code(board_db):
    import hashlib
    import time

    import psycopg
    from rust.harness_support.import_blocker import cold_harness_imports

    with psycopg.connect(board_db, autocommit=True) as conn, cold_harness_imports():
        from board.maintainer_refusals import BootstrapRefusals
        from board.run import Arm

        arm = Arm("rust", None, board_db)
        conn.execute("INSERT INTO maintainer_bootstrap (code_hash,expires_at,used_at) VALUES ('redeemed', 2, 1)")
        steps = {step.name: step for step in BootstrapRefusals().steps()}
        for title, code in (("host issues bootstrap code", "BOARDTESTA"),
                            ("issue replacement code", "BOARDTESTB"),
                            ("replace expired host code", "BOARDTESTC")):
            before = time.time()
            arm.run(steps[title])
            assert arm.ctx.vals["code"] == code
            used, active = conn.execute(
                "SELECT code_hash,expires_at,used_at,credential_id,failed_attempts "
                "FROM maintainer_bootstrap ORDER BY used_at NULLS LAST").fetchall()
            assert used == ("redeemed", 2, 1, None, 0)
            assert active[0] == hashlib.sha256(code.encode("ascii")).hexdigest()
            assert active[2:] == (None, None, 0)
            assert before + 600 <= active[1] <= time.time() + 600
            conn.execute("UPDATE maintainer_bootstrap SET failed_attempts=5 WHERE used_at IS NULL")


def test_host_bootstrap_seed_rolls_back_if_a_key_is_already_active(board_db):
    import psycopg
    from board.maintainer_refusals import BootstrapRefusals
    from board.run import Arm

    with psycopg.connect(board_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO maintainer_passkeys (credential_id,public_key,alg,label,enrolled_by,state,created_at) "
            "VALUES ('fixture-key',%s,-8,'Existing fixture','bootstrap','active',1)",
            (b"fixture",))
        before = maintainer.snapshot(board_db, None)
        step = BootstrapRefusals().steps()[0]
        arm = Arm("rust", None, board_db)
        with pytest.raises(AssertionError, match="without a live passkey"):
            arm.run(step)
        assert maintainer.snapshot(board_db, None) == before
        assert conn.execute("SELECT value FROM meta WHERE key='maintainer_secret_v1'").fetchone() is None


def test_operator_role_replaces_the_expired_fence_and_preserves_an_unrelated_coordinator(board_db):
    import json

    import psycopg
    from psycopg.rows import dict_row
    from rust.harness_support.audit_oracle import verify_audit_chain
    from rust.harness_support.import_blocker import cold_harness_imports

    with psycopg.connect(board_db, autocommit=True) as conn, cold_harness_imports():
        from board.host_fixtures import operator_role
        from board.scenarios import Ctx, seed_bank

        conn.execute(
            "INSERT INTO coordination_agents (agent_id,principal,project,task,created_at,last_activity) "
            "VALUES ('a','default','p','first task',1,1),('c','alice','p','next task',1,1)")
        conn.execute(
            "INSERT INTO coordination_leases (name,holder_agent_id,holder_principal,fence,acquired_at,expires_at) "
            "VALUES ('coordinator:p','other','default',nextval('coordination_lease_fence'),1,2)")
        seed_bank(board_db, lambda rows, _: operator_role(rows, "p", "a"), Ctx())
        conn.execute("UPDATE coordination_leases SET expires_at=1 WHERE name='conductor:p'")
        seed_bank(board_db, lambda rows, _: operator_role(rows, "p", "c"), Ctx())
        with conn.cursor(row_factory=dict_row) as cursor:
            events = cursor.execute("SELECT * FROM coordination_events ORDER BY seq").fetchall()
        assert verify_audit_chain(events)["ok"]
        assert [(r["event"], r["actor"], r["agent_id"]) for r in events] == [
            ("lease_conductor", "operator", "a"), ("lease_expire", "daemon", "a"),
            ("lease_conductor", "operator", "c")]
        assert [json.loads(r["payload"]) for r in events] == [
            {"fence": 2, "hold": 3600, "name": "conductor:p", "replaced": None},
            {"fence": 2, "name": "conductor:p"},
            {"fence": 3, "hold": 3600, "name": "conductor:p", "replaced": None}]
        assert conn.execute(
            "SELECT name,holder_agent_id,fence FROM coordination_leases ORDER BY name"
        ).fetchall() == [("conductor:p", "c", 3), ("coordinator:p", "other", 1), ("delegate:p", None, 0)]


def test_role_scenarios_leave_copied_legacy_holds_and_coordinator_queues_alone(board_db):
    import psycopg
    from board import conductor_wake
    from board.host_fixtures import operator_role
    from board.run import Arm
    from board.scenarios import seed_bank

    with psycopg.connect(board_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO coordination_agents (agent_id,principal,project,created_at,last_activity) "
            "VALUES ('wake','default',%s,1,1),('role','default',%s,1,1)",
            (conductor_wake.PROJECT, maintainer.ROLE_PROJECT))
        conn.execute(
            "INSERT INTO coordination_leases (name,holder_agent_id,holder_principal,fence,acquired_at,expires_at) "
            "VALUES ('delegate:p1','other','default',nextval('coordination_lease_fence'),1,2),"
            "('coordinator:p','other','default',nextval('coordination_lease_fence'),1,2)")
        conn.execute(
            "INSERT INTO coordination_lease_waiters (name,agent_id,principal,ttl,enqueued_at) "
            "VALUES ('coordinator:p','waiting','default',120,1)")
        arm = Arm("rust", None, board_db)
        arm.ctx.agents["A"] = {"agent_id": "wake"}
        grant = next(s for s in conductor_wake.ConductorWake().steps() if s.name == "operator grant")
        arm.run(grant)
        arm.ctx.agents["A"] = {"agent_id": "role"}
        seed_bank(board_db, lambda rows, ctx: operator_role(
            rows, maintainer.ROLE_PROJECT, ctx.id("A"), coordinator=True), arm.ctx)
        withdraw = next(s for s in maintainer.Roles().steps()
                        if s.name == "operator replaces coordinator after preview")
        arm.run(withdraw)
        assert conn.execute(
            "SELECT holder_agent_id FROM coordination_leases WHERE name IN ('coordinator:p','delegate:p1') "
            "ORDER BY name").fetchall() == [("other",), ("other",)]
        assert conn.execute("SELECT name,agent_id FROM coordination_lease_waiters").fetchall() == [
            ("coordinator:p", "waiting")]
        assert conn.execute("SELECT holder_agent_id FROM coordination_leases WHERE name=%s",
                            (f"coordinator:{maintainer.ROLE_PROJECT}",)).fetchone() == (None,)


def test_host_revoke_ends_only_live_passkey_sessions_issued_by_that_key(board_db):
    import psycopg
    from board.host_fixtures import revoke_key
    from board.scenarios import Ctx, seed_bank

    with psycopg.connect(board_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO maintainer_passkeys (credential_id,public_key,alg,label,enrolled_by,state,created_at) "
            "VALUES ('fixture-key',%s,-8,'Fixture','bootstrap','active',1)",
            (b"fixture",))
        conn.execute(
            "INSERT INTO console_sessions "
            "(session_id,token_hash,principal,source,issuer,method,created_at,expires_at,revoked_at) "
            "VALUES ('issued','one','default','fixture','fixture-key','passkey',1,4102444800,NULL),"
            "('other','two','default','fixture','other-key','passkey',1,4102444800,NULL),"
            "('link','three','default','fixture','fixture-key','link',1,4102444800,NULL),"
            "('prior','four','default','fixture','fixture-key','passkey',1,2,1)")
        seed_bank(board_db, lambda rows, _: revoke_key(rows, "fixture-key"), Ctx())
        sessions = dict(conn.execute("SELECT session_id,revoked_at FROM console_sessions").fetchall())
        assert sessions.pop("issued") > 1
        assert sessions == {"other": None, "link": None, "prior": 1}
        assert conn.execute("SELECT state FROM maintainer_passkeys").fetchone() == ("revoked",)
