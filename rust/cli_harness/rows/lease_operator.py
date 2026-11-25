"""CLI-LEASE remainder: hold and offline operator actions."""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import time
import uuid
from pathlib import Path

from rust.harness_support.audit_oracle import GENESIS_HASH, audit_hash, canonical_payload

from .. import lease_fixture, normalize
from ..core import Case
from . import _bank
from .lease import TOKEN, T0, Board, _Holder, _after, _local, _release_local, coordination_state


# What the frozen CoordinationStore wrote and answered for this row, captured
# once from the store at 8b1be337a (the README's "Seeding" section):
# - "seeds": per operator scenario, the coordination rows its setup wrote and
#   the sequence positions, every timestamp an integer offset from the seed
#   stamp and every event without its two hashes, which the seed recomputes
#   for the stamp it writes;
# - "hold": the board's answers to the hold cases' requests (the store's clock
#   fixed at T0), with the watched pid templated as "pid <PID>".
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lease-operator.json"
_FIXTURE: dict | None = None
# In insertion order: an agent before the leases, waiters and events naming it.
_SEED_TABLES = ("coordination_agents", "coordination_leases", "coordination_lease_waiters",
                "coordination_events")
_PID = "pid <PID>"
# Every operator case's seed scenario, filled as cases() builds them.
CASE_SCENARIOS: dict[str, str] = {}


def _fixture() -> dict:
    global _FIXTURE
    if _FIXTURE is None:
        _FIXTURE = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return _FIXTURE


def _absolute(value, stamp):
    """The recorded offsets (the clock-named fields, as the comparison names
    them) as times after ``stamp``."""
    if isinstance(value, dict):
        return {key: stamp + item if _timestamp(key, item) else _absolute(item, stamp)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_absolute(item, stamp) for item in value]
    return value


def seed_rows(scenario: str, stamp: float) -> tuple[dict, dict, str | None]:
    """The rows, sequence positions and target agent the frozen store's setup
    wrote for ``scenario`` with its clock at ``stamp``."""
    seed = _fixture()["seeds"][scenario]
    rows = {table: _absolute(records, stamp) for table, records in seed["rows"].items()}
    previous = GENESIS_HASH
    for row in sorted(rows.get("coordination_events", []), key=lambda row: row["seq"]):
        row["payload"] = canonical_payload(row["payload"])
        row["prev_hash"] = previous
        row["hash"] = previous = audit_hash(previous, row)
    if set(rows) - set(_SEED_TABLES):
        raise ValueError(f"lease_operator seed {scenario}: unexpected tables {sorted(set(rows) - set(_SEED_TABLES))}")
    return rows, dict(seed["sequences"]), seed["target"]


def seed_bank(conn, scenario: str, stamp: float) -> str | None:
    """Write ``scenario``'s recorded rows in one transaction; returns the
    target agent's id (None when the scenario registers none)."""
    # psycopg only where a bank is written: the row loads without it.
    from rust.harness_support.fixture_rows import checked_fixture_insert  # noqa: PLC0415

    rows, sequences, target = seed_rows(scenario, stamp)
    with conn.transaction():
        inserts = [checked_fixture_insert(conn, table, rows[table]) for table in _SEED_TABLES if table in rows]
        for statement, params in inserts:
            conn.execute(statement, params)
        for sequence, value in sequences.items():
            # A position the store never advanced stays at the fresh bank's.
            if value is not None:
                conn.execute("SELECT setval(%s::regclass, %s, true)", ("public." + sequence, value))
    return target


class OperatorBank(Board):
    """A disposable bank the operator CLI writes directly, seeded with the
    rows the frozen store wrote, behind a loopback board with no recorded
    answers: the operator actions never call it, and a request they sent
    would be answered 500 and fail the case."""

    def __init__(self):
        name = _bank.name(f"pl_cf_lease_{uuid.uuid4().int % 10**18}")
        with _bank._admin() as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                raise RuntimeError("disposable name collision")
        _bank.create(name)
        super().__init__("operator", recorded={"initial": None, "transcripts": []})
        self.name = name
        self.conn = _bank.connect(name, autocommit=True)

    def seed(self, scenario: str, stamp: float) -> str | None:
        return seed_bank(self.conn, scenario, stamp)

    def final_state(self) -> dict:
        return coordination_state(_bank.dump(self.name))

    def close(self):
        super().close()
        self.conn.close()
        _bank.teardown(self.name)


def _bind_text(text: str, pid: int) -> str:
    return text.replace(_PID, f"pid {pid}")


def _bind_state(state: dict, pid: int) -> dict:
    """A recorded state with the watched pid in its rows, its audit chain
    recomputed over them; only the pid and the two hash fields' text change."""
    out = {"tables": {table: [_bind_text(raw, pid) for raw in raws]
                      for table, raws in state["tables"].items()},
           "sequences": dict(state["sequences"])}
    raws = out["tables"].get("coordination_events", [])
    order = sorted(range(len(raws)), key=lambda index: json.loads(raws[index])["seq"])
    previous = json.loads(raws[order[0]])["prev_hash"] if order else None
    for index in order:
        row = json.loads(raws[index])
        digest = audit_hash(previous, row)
        raw = raws[index]
        for field, old, new in (("prev_hash", row["prev_hash"], previous), ("hash", row["hash"], digest)):
            needle = f'"{field}": "{old}"'
            if raw.count(needle) != 1:
                raise ValueError(f"lease_operator hold recording: no single {field} in {raw!r}")
            raw = raw.replace(needle, f'"{field}": "{new}"')
        raws[index] = raw
        previous = digest
    return out


class HoldBoard(Board):
    """The hold cases' loopback board, answering as the recorded frozen store
    did. The watched process's pid is in the CLI's requests and in the store's
    answers and rows, so a request is matched with it templated and answered
    with it bound to ``pid``, which the case's setup sets."""

    def __init__(self, bank=True):
        super().__init__("hold", bank=bank, recorded=_fixture()["hold"] if bank else None)
        self.pid: int | None = None

    def dispatch(self, action, body, headers):
        if self.pid is None:
            raise RuntimeError("lease_operator hold board: the watched pid was never set")
        pattern = re.compile(rf"\bpid {self.pid}\b")
        body = json.loads(pattern.sub(_PID, json.dumps(body, ensure_ascii=False)))
        before = len(self.states)
        status, reply = super().dispatch(action, body, headers)
        self.states[before:] = [{"action": item["action"], "state": _bind_state(item["state"], self.pid)}
                                for item in self.states[before:]]
        return status, json.loads(_bind_text(json.dumps(reply, ensure_ascii=False), self.pid))


@normalize.rule("lease-fixture-pid")
def fixture_pid(obs):
    """Associate the watched process PID, after validating raw audit chains."""
    pid = obs["fixture_pid"]
    assert isinstance(pid, int) and pid > 0
    pattern = re.compile(rf"\bpid {pid}\b")
    def fields(value):
        if isinstance(value, dict):
            return {key: fields(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fields(item) for item in value]
        if isinstance(value, str):
            return pattern.sub("pid <PID>", value)
        return value
    for request in obs.get("requests", []):
        request["body"] = fields(request["body"])
    for stream in ("stdout", "stderr"):
        raw = base64.b64decode(obs[stream])
        raw = re.sub(rb"\bpid " + str(pid).encode() + rb"\b", b"pid <PID>", raw)
        obs[stream] = base64.b64encode(raw).decode()
    states = [item["state"] for item in obs.get("db", {}).get("writes", [])]
    if "final" in obs.get("db", {}):
        states.append(obs["db"]["final"])
    if not states:
        return
    from rust.harness_support.audit_oracle import audit_hash, verify_audit_chain

    for state in states:
        assert verify_audit_chain(_audit(state))["ok"]
        for table, records in state["tables"].items():
            parsed = [fields(json.loads(raw)) for raw in records]
            if table == "coordination_events" and parsed:
                parsed.sort(key=lambda row: row["seq"])
                previous = parsed[0]["prev_hash"]
                for row in parsed:
                    row["prev_hash"] = previous
                    row["hash"] = audit_hash(previous, row)
                    previous = row["hash"]
            state["tables"][table] = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")) for row in parsed)


def hold_case(identifier, *, board=False, queued=False, gone=False, renew=False):
    env = {"PSEUDOLIFE_LEASE_LOCK_DIR": "{HOME}/locks", "PSEUDOLIFE_AGENT_PROJECT": "fixture",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if board:
        env["PSEUDOLIFE_MCP_TOKEN"] = TOKEN
    def setup(arm):
        _local()(arm)
        watched = _Holder(arm.home / "watch.lock")
        assert watched.line() == "HELD"
        arm.daemon.locals.append(watched)
        arm.daemon.pid = watched.pid
        arm.state["watched"] = watched
        arm.state["local_seen"] = False
        arm.state["stable_hold"] = False
        if queued:
            blocker = _Holder(arm.home / "locks" / "lease-fixture.lock")
            assert blocker.line() == "HELD"
            arm.daemon.locals.append(blocker)
            arm.state["blocker"] = blocker
            arm.state["holder"] = blocker
        if gone:
            watched.stop()
        case.argv[:] = ["lease", "hold", "fixture", "--while-pid", str(watched.pid),
                        "--ttl", "30", "--expect", "2m", "--purpose", "hold proof",
                        "--worktree", "{HOME}/cwd", *([] if board else ["--no-board"])]
    def during(arm, proc):
        if gone:
            return
        deadline = time.monotonic() + 20
        path = arm.home / "locks" / "lease-fixture.lock"
        while proc.poll() is None and time.monotonic() < deadline:
            if path.exists() and lease_fixture.probe(path) is True:
                arm.state["local_seen"] = True
                break
            time.sleep(0.02)
        if arm.state["local_seen"]:
            if board:
                minimum = 2 if renew else 1
                while proc.poll() is None and time.monotonic() < deadline:
                    with arm.daemon.lock:
                        ready = arm.daemon.calls.get("lease", 0) >= minimum and arm.daemon.calls.get("agents", 0) >= 1
                    if ready:
                        break
                    time.sleep(0.02)
            else:
                time.sleep(0.5)
            arm.state["stable_hold"] = proc.poll() is None and arm.state["watched"].proc.poll() is None
        arm.state["watched"].release()
    def after(arm, obs):
        _after(arm, obs)
        obs["fixture_pid"] = arm.state["watched"].pid
        obs["listener"] = {"local_seen": arm.state["local_seen"], "stable_hold": arm.state["stable_hold"],
                           "wait_observed": arm.state.get("waited", False),
                           "local_free": lease_fixture.probe(arm.home / "locks" / "lease-fixture.lock") is not True,
                           "watched_gone": arm.state["watched"].proc.poll() is not None}
        if arm.name == "python" and not gone:
            assert obs["listener"]["local_seen"] and obs["listener"]["stable_hold"]
        if queued:
            assert b"waiting for the local lock" in base64.b64decode(obs["stderr"])
    case = Case(identifier, [], env=env, setup=setup, during=during, after=after,
                before_capture=_release_local if queued else None,
                daemon=lambda: HoldBoard(bank=board), timeout=35,
                rules=("lease-http-json", "lease-fixture-pid", "lease-seeded-clock"))
    return case


def _audit(state):
    return sorted((json.loads(row) for row in state["tables"]["coordination_events"]),
                  key=lambda row: row["seq"])


def _timestamp(key, value):
    return (key.endswith(("_at", "_until")) or key in ("expected_end", "last_activity", "last_seen")) \
        and isinstance(value, (int, float)) and not isinstance(value, bool)


@normalize.rule("lease-operator-clock")
def operator_clock(obs):
    projected = copy.deepcopy(obs)
    try:
        _operator_clock(projected)
    except (AssertionError, KeyError, StopIteration, ValueError):
        # Preserve the entire raw observation on an unvalidated clock or
        # audit path, so a changed result cannot become a matching projection.
        return
    obs.clear()
    obs.update(projected)


def _operator_clock(obs):
    """Validate raw clocks/chains, then associate only their timestamp paths."""
    from rust.harness_support.audit_oracle import audit_hash, verify_audit_chain
    seed = obs["seed_stamp"]
    assert obs["seed_window"][0] <= seed <= obs["seed_window"][1]
    before, final = obs["db"]["before"], obs["db"]["final"]
    old, rows = _audit(before), _audit(final)
    assert verify_audit_chain(old)["ok"]
    assert verify_audit_chain(rows)["ok"]
    assert rows[:len(old)] == old
    new = rows[len(old):]
    assert [row["event"] for row in new] == obs["expected_events"]
    operation = new[0]["created_at"] if new else None
    if operation is not None:
        assert math.isfinite(operation) and obs["window"][0] <= operation <= obs["window"][1]
        assert all(row["created_at"] == operation for row in new)
    seed_values = set()
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if _timestamp(key, item):
                    assert math.isfinite(item)
                    seed_values.add(item)
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for records in before["tables"].values():
        for raw in records:
            row = json.loads(raw)
            collect(row)
            if "payload" in row and isinstance(row["payload"], str):
                collect(json.loads(row["payload"]))

    def stamp(key, value, row):
        if value in seed_values:
            return T0 + (value - seed)
        assert operation is not None and math.isfinite(value)
        if key == "expires_at":
            # The operator's grant: conductor:<project>, delegate:<project> in
            # observations recorded before the role's rename (2026-10-31).
            if row.get("name", "").startswith(("conductor:", "delegate:")):
                hold = obs["hold"]
            else:
                waiter = next(json.loads(raw) for raw in before["tables"]["coordination_lease_waiters"]
                              if json.loads(raw)["name"] == row["name"]
                              and json.loads(raw)["agent_id"] == row["holder_agent_id"])
                hold = min(waiter["ttl"], 300)
            assert value == operation + hold
            return T0 + 100 + hold
        if key == "expected_end":
            assert value == operation + row["expect"]
            return T0 + 100 + row["expect"]
        assert value == operation
        return T0 + 100

    def project(state):
        out = copy.deepcopy(state)
        for table, records in out["tables"].items():
            parsed = [json.loads(raw) for raw in records]
            def fields(value):
                if isinstance(value, dict):
                    return {key: stamp(key, item, value) if _timestamp(key, item) else fields(item)
                            for key, item in value.items()}
                if isinstance(value, list):
                    return [fields(item) for item in value]
                return value
            for row in parsed:
                updated = fields(row)
                row.clear()
                row.update(updated)
                if table == "coordination_events":
                    payload = json.loads(row["payload"])
                    assert row["payload"] == json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":"))
                    row["payload"] = json.dumps(fields(payload), ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"))
            if table == "coordination_events" and parsed:
                parsed.sort(key=lambda row: row["seq"])
                previous = parsed[0]["prev_hash"]
                for row in parsed:
                    row["prev_hash"] = previous
                    row["hash"] = audit_hash(previous, row)
                    previous = row["hash"]
            out["tables"][table] = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")) for row in parsed)
        return out

    obs["db"]["before"] = project(before)
    obs["db"]["final"] = project(final)
    raw = base64.b64decode(obs["stdout"])
    if operation is not None:
        def expiry(match):
            value = float(match.group(2))
            assert value == operation + obs["hold"]
            return match.group(1) + str(float(T0 + 100 + obs["hold"])).encode()
        raw = re.sub(rb'("expires_at": )([0-9.eE+-]+)', expiry, raw)
    obs["stdout"] = base64.b64encode(raw).decode()


_FAULTS = {
    # An audit INSERT the server refuses inside the block: the lease vacate
    # that ran before it must roll back with it.
    "audit-insert": ("BEFORE INSERT ON coordination_events FOR EACH ROW",
                     "RAISE EXCEPTION 'harness: audit insert refused';"),
    # The server refuses the COMMIT itself (an ERROR at COMMIT: rolled back).
    "commit-refused": ("AFTER INSERT ON coordination_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW",
                       "RAISE EXCEPTION 'harness: refused at commit';"),
    # The CLI's own backend ends itself at COMMIT, before the commit record:
    # the client's answer is FATAL 57P01 and the server rolls back, but the
    # client cannot know that, so both arms report the outcome as unknown.
    # Named after transfer's identical fixture; it is not a transport cut.
    "commit-lost": ("AFTER INSERT ON coordination_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW",
                    "PERFORM pg_terminate_backend(pg_backend_pid()); PERFORM pg_sleep(5);"),
}
# Faults whose COMMIT answer is FATAL or lost: the uncertain line, not the
# certain one.
_UNKNOWN_FAULTS = frozenset({"commit-lost"})
_FAILED = re.compile(rb"^(break|conductor) failed \(([A-Za-z]+)\); nothing was changed(\r?\n)\Z")
_LOST = re.compile(rb"^(break|conductor) lost the answer to its COMMIT \(([A-Za-z]+)\); it may have been "
                   rb"committed: run `pseudolife-mcp lease list` before retrying(\r?\n)\Z")
_NATIVE_FAILED = b" failed (PostgreSQL transaction failed); nothing was changed"
_NATIVE_LOST = (b" lost the answer to its COMMIT (PostgreSQL session ended); it may have been "
                b"committed: run `pseudolife-mcp lease list` before retrying")


def _failure_line(obs):
    """The oracle's whole failure line, matched as the kind the case expects."""
    pattern = _LOST if obs.get("commit_unknown") else _FAILED
    return pattern.match(base64.b64decode(obs["stderr"]))


def _install_fault(conn, fault):
    timing, body = _FAULTS[fault]
    kind = "CONSTRAINT TRIGGER" if "DEFERRED" in timing else "TRIGGER"
    conn.execute("CREATE FUNCTION pl_cf_lease_fault() RETURNS trigger LANGUAGE plpgsql "
                 f"AS $$ BEGIN {body} RETURN NULL; END $$")
    conn.execute(f"CREATE {kind} pl_cf_lease_fault {timing} EXECUTE FUNCTION pl_cf_lease_fault()")


@normalize.rule("lease-native-pg-diagnostics")
def native_pg_diagnostics(obs):
    """Declared native-pg-diagnostics: the oracle names the psycopg class in
    its failure line; the candidate names the safe native class. Only the
    oracle arm's single whole failure line maps, only as the line kind the
    case expects (certain, or COMMIT outcome unknown), and only for the class
    the case expects; exit, stdout and every coordination row stay compared."""
    if obs.get("arm") != "python":
        return
    match = _failure_line(obs)
    if obs["exit"] != 1 or not match or match.group(2).decode() != obs.get("failure_class"):
        return
    native = _NATIVE_LOST if obs.get("commit_unknown") else _NATIVE_FAILED
    obs["stderr"] = base64.b64encode(match.group(1) + native + match.group(3)).decode()


def operator_case(identifier, argv, scenario="missing", events=(), local=False, stdout_closed=False,
                  fault=None, failure_class=None):
    # The two explicit fixture inputs and the default hold window, in seconds.
    hold = {"2m": 120, "7d": 604800}[argv[argv.index("--for") + 1]] if "--for" in argv else 86400
    env = {"PSEUDOLIFE_DAEMON_EXEC": "1", "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8", "PSEUDOLIFE_LEASE_LOCK_DIR": "{HOME}/locks"}

    def setup(arm):
        env["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(arm.daemon.name)
        _local("held" if local else "absent")(arm)
        start = time.time()
        stamp = time.time()
        arm.state["seed_stamp"] = stamp
        # The rows the frozen store wrote with its clock at ``stamp``. Every
        # scenario but "missing" first registers the target (label codex,
        # project fixture, capabilities resumable and pull, wake enabled for
        # listener and ring). Then:
        # - held, queue, free: the target takes "fixture" (ttl 300, expect
        #   60); queue adds a waiter (ttl 120, expect 30); free releases it;
        # - replace: another agent holds conductor:fixture (hold 300);
        # - legacy, legacy-expired: another agent holds delegate:fixture, the
        #   role's name before its rename, from stamp - 60 until stamp + 300
        #   (or stamp - 10, lapsed), with its lease_delegate record;
        # - coordinator: the target holds coordinator:fixture (ttl 300);
        # - ambiguous: a second agent shares the target's id prefix;
        # - listener, ring: the target attached as fixture-listener (ring
        #   armed until stamp + 600, which the store caps at its attach lease).
        target = arm.daemon.seed(scenario, stamp)
        case.argv[:] = ["lease", *(target if arg == "{AGENT}" else arg for arg in argv)]
        if fault:
            # After seeding, so only the CLI's own transaction meets it.
            _install_fault(arm.daemon.conn, fault)
        arm.state["before"] = coordination_state(_bank.dump(arm.daemon.name))
        arm.state["seed_window"] = [start, time.time()]

    def after(arm, obs):
        if local:
            obs["listener"] = {"local_still_held": lease_fixture.probe(arm.home / "locks" / "lease-fixture.lock") is True}
            if arm.name == "python":
                assert obs["listener"]["local_still_held"]
        _after(arm, obs)
        if fault:
            obs["arm"] = arm.name
            obs["failure_class"] = failure_class
            obs["commit_unknown"] = fault in _UNKNOWN_FAULTS
            if arm.name == "python":
                # The fixture must do what it claims on the oracle: exit 1
                # and every coordination row exactly the seeded state. A
                # sequence may only advance: nextval (a FIFO grant's fence)
                # is not rolled back. The candidate's bank, and the exact
                # sequence positions, are left to the comparison.
                final, seeded = obs["db"]["final"], arm.state["before"]
                assert obs["exit"] == 1, obs["exit"]
                # Not vacuous: the oracle reached the fault (its own class in
                # the failure line), and a FIFO block got past the grant's
                # nextval before failing.
                failed = _failure_line(obs)
                assert failed and failed.group(2).decode() == failure_class, obs["stderr"]
                if scenario == "queue":
                    fence = "coordination_lease_fence"
                    assert (final["sequences"][fence] or 0) > (seeded["sequences"][fence] or 0), fence
                assert final["tables"] == seeded["tables"], f"oracle: {fault} changed coordination rows"
                assert final["sequences"].keys() == seeded["sequences"].keys()
                assert all(seeded["sequences"][key] is None
                           or (final["sequences"][key] is not None
                               and seeded["sequences"][key] <= final["sequences"][key])
                           for key in seeded["sequences"]), f"oracle: {fault} moved a sequence back"
        obs["db"]["before"] = arm.state["before"]
        obs["seed_stamp"] = arm.state["seed_stamp"]
        obs["seed_window"] = arm.state["seed_window"]
        obs["expected_events"] = list(events)
        obs["hold"] = hold
        before = [json.loads(row) for row in obs["db"]["before"]["tables"]["coordination_events"]]
        final = [json.loads(row) for row in obs["db"]["final"]["tables"]["coordination_events"]]
        new = sorted(final, key=lambda row: row["seq"])[len(before):]
        assert [row["event"] for row in new] == list(events)

    CASE_SCENARIOS[identifier] = scenario
    case = Case(identifier, ["lease", *argv], env=env, setup=setup, after=after,
                daemon=OperatorBank, stdout_closed=stdout_closed,
                rules=("lease-http-json", "lease-operator-clock", *(('lease-native-stdout',) if stdout_closed else ()),
                       *(("lease-native-pg-diagnostics",) if fault else ())))
    return case


def cases():
    result = [operator_case("break-missing", ["break", "missing"]),
              operator_case("break-free", ["break", "fixture"], "free"),
              operator_case("break-held", ["break", "fixture"], "held", ("lease_break",)),
              operator_case("break-fifo", ["break", "fixture"], "queue", ("lease_break", "lease_grant"))]
    # conductor is the action; delegate (until 2026-10-31) and designate
    # (0.16.0) still run it after one deprecation line on stderr.
    for action in ("conductor", "delegate", "designate"):
        result.append(operator_case(action + "-normal", [action, "fixture", "{AGENT}", "--for", "2m"],
                                    "target", ("lease_conductor",)))
    for scenario, events in (("replace", ("lease_conductor",)),
                             ("coordinator", ("lease_break", "lease_conductor")),
                             ("legacy", ("lease_break", "lease_conductor")),
                             ("legacy-expired", ("lease_expire", "lease_conductor"))):
        result.append(operator_case("conductor-" + scenario, ["conductor", "fixture", "{AGENT}", "--for", "2m"],
                                    scenario, events))
    result.extend([
        operator_case("conductor-prefix", ["conductor", "fixture", "00000000", "--for", "2m"],
                      "target", ("lease_conductor",)),
        operator_case("conductor-unknown", ["conductor", "fixture", "f" * 32, "--for", "2m"]),
        operator_case("conductor-ambiguous", ["conductor", "fixture", "00000000", "--for", "2m"], "ambiguous"),
        operator_case("conductor-default", ["conductor", "fixture", "{AGENT}"], "target", ("lease_conductor",)),
        operator_case("conductor-seven-days", ["conductor", "fixture", "{AGENT}", "--for", "7d"], "target", ("lease_conductor",)),
        operator_case("conductor-unicode-project", ["conductor", "资源", "{AGENT}", "--for", "2m"], "target", ("lease_conductor",)),
        operator_case("conductor-live-listener", ["conductor", "fixture", "{AGENT}", "--for", "2m"], "listener", ("lease_conductor",)),
        operator_case("conductor-live-ring", ["conductor", "fixture", "{AGENT}", "--for", "2m"], "ring", ("lease_conductor",)),
        hold_case("hold-no-board"), hold_case("hold-gone-pid", gone=True),
        hold_case("hold-local-wait", queued=True), hold_case("hold-board", board=True),
        hold_case("hold-board-renew", board=True, renew=True),
        operator_case("break-preserves-local-holder", ["break", "fixture"], "held", ("lease_break",), local=True),
        operator_case("break-closed-stdout", ["break", "fixture"], "held", ("lease_break",), stdout_closed=True),
        operator_case("break-refused-credential-name", ["break", "sk-" + "Ab9_" * 10]),
        # Failure outcomes: each must leave every coordination row as seeded.
        operator_case("break-audit-insert-fails", ["break", "fixture"], "queue",
                      fault="audit-insert", failure_class="RaiseException"),
        operator_case("break-commit-refused", ["break", "fixture"], "queue",
                      fault="commit-refused", failure_class="RaiseException"),
        # The near miss of conductor-commit-lost: an ERROR at COMMIT keeps
        # the certain line.
        operator_case("conductor-commit-refused", ["conductor", "fixture", "{AGENT}", "--for", "2m"],
                      "coordinator", fault="commit-refused", failure_class="RaiseException"),
        operator_case("break-commit-lost", ["break", "fixture"], "held",
                      fault="commit-lost", failure_class="AdminShutdown"),
        operator_case("conductor-commit-lost", ["conductor", "fixture", "{AGENT}", "--for", "2m"], "coordinator",
                      fault="commit-lost", failure_class="AdminShutdown"),
    ])
    for action in ("hold", "break", "conductor", "delegate", "designate"):
        result.append(Case("help-" + action, ["lease", action, "--help"],
                           env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}))
    from .lease_transport import cases as transport_cases
    result.extend(transport_cases())
    return result
