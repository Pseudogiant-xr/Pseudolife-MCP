"""CLI-COORDINATION-RECOVERY: offline gates plus disposable restored histories.

Non-bank cases record the real Python CLI. Live bank cases seed via the
oracle's own CoordinationStore, run both executables, validate each chain
with the oracle, then compare all affected rows. No daemon is started.
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .. import core, normalize
from ..run_names import RunNames
from ..mutants import Mutant

MUTANTS = [
    Mutant("recovery-stop-gate-dropped", "coordination_recovery",
           "shim/src/cli/coordination_recovery/mod.rs",
           "if !args.stopped {", "if false {", ("recovery-stop-required",)),
]

DISABLED = "coordination:\n  enabled: false\n  allowed_principals: [alice, daemon, maintainer]\n"
ID_A = "aaaaaaaa000000000000000000000001"
ID_B = "aaaaaaaa000000000000000000000002"
NAMES = RunNames()
_CREATED = set()


def _setup(text=DISABLED, existing=False, repository=False):
    def setup(arm):
        (arm.home / "config.yaml").write_text(text, encoding="utf-8")
        if existing:
            (arm.home / "agent.json").write_text("unchanged", encoding="utf-8")
        if repository:
            (arm.home / "checkout").mkdir()
            (arm.home / "checkout" / ".git").write_text("gitdir: fixture", encoding="utf-8")
    return setup


def _url(name):
    from tests.pg_defaults import default_login
    NAMES.require_owned(name)
    if not re.fullmatch(r"r1_[a-z0-9_]+", name):
        raise ValueError("recovery fixtures require an owned r1_ database")
    user, password = default_login()[:2]
    if user != "pseudolife_test":
        raise RuntimeError("recovery fixtures require the disposable test login")
    host = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{host}/{name}"


def _admin():
    from . import _bank
    return _bank._admin()


def _drop(name):
    from pseudolife_memory.storage.schema import refuse_production_database
    NAMES.require_owned(name)
    refuse_production_database(name)
    with _admin() as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname=%s AND usename=current_user AND pid<>pg_backend_pid()", (name,))
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    _CREATED.discard(name)


def _cleanup():
    for name in sorted(_CREATED):
        with contextlib.suppress(Exception):
            _drop(name)


atexit.register(_cleanup)


class Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


def _bank_setup(kind):
    def setup(arm):
        import psycopg
        from unittest import mock
        from pseudolife_memory.storage.coordination import CoordinationStore, COORDINATION_SCHEMA_SQL
        from pseudolife_memory.storage.schema import assert_disposable_database
        from ..pg_create import create_from_default_template

        name = NAMES.name("r1_coordination_recovery_" + kind.replace("-", "_"), max_length=63)
        _drop(name)
        with _admin() as conn:
            create_from_default_template(conn, f'CREATE DATABASE "{name}"')
        _CREATED.add(name)
        arm.state["bank"] = name
        _setup()(arm)
        with psycopg.connect(_url(name), autocommit=True) as conn:
            assert_disposable_database(conn)
            conn.execute(COORDINATION_SCHEMA_SQL)
            store = CoordinationStore(Storage(conn), clock=lambda: 1000.0)
            owner = "paired" if kind in ("invited", "invited-denied") else "alice"
            # Fixed ids and credentials come through the oracle's own writer.
            import uuid
            ids = iter([uuid.UUID(ID_A), uuid.UUID(ID_B)] + [uuid.UUID(int=n) for n in range(3, 100)])
            with mock.patch("pseudolife_memory.storage.coordination.uuid.uuid4",
                            side_effect=lambda: next(ids)), \
                 mock.patch("pseudolife_memory.storage.coordination.secrets.token_urlsafe",
                            return_value="fixture-mailbox-credential"), \
                 mock.patch("pseudolife_memory.storage.coordination.secrets.token_hex",
                            side_effect=lambda n: "ab" * n):
                a = store.register(owner, project="recovery", task="fixture")
                b = store.register(owner, project="recovery", task="fixture")
                store.attach(owner, b["agent_id"], b["credential"], attachment_id="old", wake_enabled=True)
                store.send(owner, a["agent_id"], a["credential"], to=b["agent_id"],
                           text="pending mail survives restore", request_id="r1-pending")
                store.acquire_lease(owner, a["agent_id"], a["credential"], name="r1-resource", ttl=120)
                store.acquire_lease(owner, b["agent_id"], b["credential"], name="r1-resource", ttl=120)
                if kind not in ("recover", "unrevoked"):
                    store.recover()
                if kind == "repeat-recover":
                    store.recover()
            if kind in ("invited", "invited-denied"):
                from pseudolife_memory import principal_store
                from pseudolife_memory.storage.schema import PRINCIPALS_SCHEMA_SQL
                conn.execute(PRINCIPALS_SCHEMA_SQL)
                with mock.patch.object(principal_store, "_NOW", "1000.0"), \
                     mock.patch.object(principal_store, "new_pairing_code", return_value="0123456789AB"):
                    principal_store.create_invite(conn, "paired", tier=None, board=kind=="invited", ttl_seconds=60, replace=False)
            if kind == "owner-mismatch":
                (arm.home / "config.yaml").write_text(DISABLED.replace("alice, daemon", "alice, bob, daemon"), encoding="utf-8")
            if kind == "old":
                conn.execute("DROP TABLE coordination_events")
            if kind == "reservation-race":
                # The CLI's UPDATE waits after reserving the file, allowing
                # the during hook to replace only that disposable reservation.
                conn.execute("CREATE FUNCTION r1_pause_rebind() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(78421013); RETURN NEW; END $$")
                conn.execute("CREATE TRIGGER r1_pause_rebind BEFORE UPDATE ON coordination_agents FOR EACH ROW EXECUTE FUNCTION r1_pause_rebind()")
            arm.state["before"] = _dump(conn)
        if kind == "reservation-race":
            blocker = psycopg.connect(_url(name), autocommit=True)
            blocker.execute("SELECT pg_advisory_lock(78421013)")
            arm.state["blocker"] = blocker
    return setup


def _dump(conn):
    names = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    return {name: [json.loads(r[0]) for r in conn.execute(f'SELECT to_jsonb(t)::text FROM "{name}" t ORDER BY to_jsonb(t)::text')]
            for name in names}


def _bank_after(kind):
    def after(arm, obs):
        import psycopg
        from pseudolife_memory.storage.coordination import audit_hash, verify_audit_chain
        name = arm.state["bank"]
        if kind == "reservation-race":
            assert arm.state.get("race_applied"), "reservation-race barrier was not exercised"
        with psycopg.connect(_url(name), autocommit=True) as conn:
            dump = _dump(conn)
        events = dump.get("coordination_events", [])
        events.sort(key=lambda v: v["seq"])
        if events:
            assert verify_audit_chain(events)["ok"]
            for event in events:
                assert event["hash"] == audit_hash(event["prev_hash"], event)
        before = arm.state["before"]
        assert dump["coordination_messages"] == before["coordination_messages"]
        if obs["exit"] == 0 and kind in ("recover", "old", "repeat-recover"):
            for row in dump["coordination_agents"]:
                assert row["credential_hash"] is None and row["wake_enabled"] is False
                assert row["attachment_id"] is None and row["lease_until"] is None
                prior = next(v for v in before["coordination_agents"] if v["agent_id"] == row["agent_id"])
                assert row["generation"] == prior["generation"] + 1
            assert not dump["coordination_lease_waiters"]
            assert all(v["holder_agent_id"] is None for v in dump["coordination_leases"])
        if obs["exit"] == 0 and kind in ("rebind", "invited"):
            private = json.loads((arm.home / "agent.json").read_text(encoding="utf-8"))
            assert set(private) == {"bank_url", "agent_id", "credential"}
            row = next(v for v in dump["coordination_agents"] if v["agent_id"] == private["agent_id"])
            assert row["credential_hash"] == hashlib.sha256(private["credential"].encode()).hexdigest()
            prior = next(v for v in before["coordination_agents"] if v["agent_id"] == row["agent_id"])
            assert not row["wake_enabled"] and row["generation"] == prior["generation"]
            assert private["credential"].encode() not in core.decode(obs, "stdout") + core.decode(obs, "stderr")
            # Normalize only after checking the real private credential against
            # the actual durable hash. The file is still inspected for privacy.
            arm.state["credential"] = private["credential"]
        elif obs["exit"] != 0:
            assert dump == before, "refused rebind changed durable state"
        obs["db"] = dump
        obs["recovery_before"] = before
        obs["recovery_credential"] = arm.state.get("credential")
        _drop(name)
    return after


def _reservation_race(arm, proc):
    import time
    blocker = arm.state["blocker"]
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            waiting = blocker.execute("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event='advisory')").fetchone()[0]
            if waiting and (arm.home / "agent.json").exists():
                reserved = arm.home / "agent.json"
                reserved.rename(arm.home / "original-reservation.json")
                # The inode changes while the original empty reservation
                # remains live, so filesystem reuse cannot hide the race.
                from pseudolife_memory.private_state import open_private
                fd = open_private(reserved, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                arm.state["race_applied"] = True
                break
            if proc.poll() is not None:
                raise RuntimeError("rebind exited before the reservation-race barrier")
            time.sleep(0.02)
        else:
            raise RuntimeError("rebind did not reach the reservation-race barrier")
    finally:
        blocker.execute("SELECT pg_advisory_unlock(78421013)")
        blocker.close()


@normalize.rule("coordination-recovery-dynamic")
def _dynamic(obs):
    """Validate clocks/hash chain in after; replace only new stamps/hashes and the verified credential."""
    credential = obs.get("recovery_credential")
    for row in obs.get("db", {}).get("coordination_agents", []):
        if credential and row["credential_hash"] == hashlib.sha256(credential.encode()).hexdigest():
            row["credential_hash"] = "<validated-issued-credential-hash>"
    original_events = {v["seq"]: v for v in obs.get("recovery_before", {}).get("coordination_events", [])}
    for row in obs.get("db", {}).get("coordination_events", []):
        if row["seq"] not in original_events:
            assert obs["window"][0] <= row["created_at"] <= obs["window"][1]
            row["created_at"] = "<command-clock>"
            row["hash"] = "<validated-chain-hash>"
    for row in obs.get("db", {}).get("coordination_leases", []):
        if row.get("freed_at") and row["freed_at"] > 1000:
            assert obs["window"][0] <= row["freed_at"] <= obs["window"][1]
            row["freed_at"] = "<command-clock>"
    if credential:
        value = obs["files"].get("agent.json", "")
        if value.startswith("file:"):
            private = json.loads(base64.b64decode(value[5:]))
            assert private["credential"] == credential
            private["credential"] = "<validated-issued-credential>"
            obs["files"]["agent.json"] = "file:" + base64.b64encode(json.dumps(private, sort_keys=True).encode()).decode()


def cases():
    config = ["--config", "{HOME}/config.yaml"]
    recover = ["coordination-recovery", "recover", *config]
    rebind = ["coordination-recovery", "rebind", *config, "--confirm-daemon-stopped",
              "--agent", ID_B, "--principal", "alice", "--bank-url", "http://localhost:8765",
              "--state", "{HOME}/agent.json"]
    result = [
        core.Case("recovery-help", ["coordination-recovery", "--help"]),
        core.Case("recovery-default-on", recover + ["--confirm-daemon-stopped", "--confirm-restore"], setup=_setup("memory: {preset: flat}\n")),
        core.Case("recovery-stop-required", recover + ["--confirm-restore"], setup=_setup()),
        core.Case("recovery-restore-required", recover + ["--confirm-daemon-stopped"], setup=_setup()),
        core.Case("recovery-no-automatic-database", recover + ["--confirm-daemon-stopped", "--confirm-restore"], setup=_setup()),
        core.Case("recovery-rebind-flags-refused", recover + ["--confirm-daemon-stopped", "--confirm-restore", "--agent", ID_A], setup=_setup()),
        core.Case("rebind-existing-file", rebind, setup=_setup(existing=True)),
        core.Case("rebind-git-worktree-refused", [v.replace("{HOME}/agent.json", "{HOME}/checkout/agent.json") for v in rebind], setup=_setup(repository=True)),
        core.Case("rebind-no-database-keeps-reservation", rebind, setup=_setup()),
    ]
    # Allocate one owned database per case; setup recreates it for each arm.
    for kind, argv in [("recover", recover + ["--confirm-daemon-stopped", "--confirm-restore"]),
                       ("rebind", rebind),
                       ("ambiguous", [v.replace(ID_B, "aaaaaaaa") for v in rebind]),
                       ("wrong-owner", [v.replace("alice", "maintainer") for v in rebind]),
                       ("owner-mismatch", [v.replace("alice", "bob") for v in rebind]),
                       ("unrevoked", rebind),
                       ("invited", [v.replace("alice", "paired") for v in rebind]),
                       ("invited-denied", [v.replace("alice", "paired") for v in rebind]),
                       ("reservation-race", rebind),
                       ("repeat-recover", recover + ["--confirm-daemon-stopped", "--confirm-restore"]),
                       ("old", recover + ["--confirm-daemon-stopped", "--confirm-restore"])]:
        name = NAMES.name("r1_coordination_recovery_" + kind.replace("-", "_"), max_length=63)
        # URL resolution is lazy: constructing --skip-bank rows must not read
        # a machine's database login. The bank case expands it in setup via a
        # disposable launch configuration below.
        case = core.Case("restored-" + kind, argv, setup=_bank_setup(kind), after=_bank_after(kind),
                         env={"PSEUDOLIFE_MCP_DATABASE_URL": "{R1_FIXTURE_DSN}"},
                         bank=True, golden=False, rules=("coordination-recovery-dynamic",))
        def setup(arm, original=case.setup, case=case, name=name):
            original(arm)
            case.env["PSEUDOLIFE_MCP_DATABASE_URL"] = _url(name)
        case.setup = setup
        if kind == "reservation-race":
            case.during = _reservation_race
        result.append(case)
    return result
