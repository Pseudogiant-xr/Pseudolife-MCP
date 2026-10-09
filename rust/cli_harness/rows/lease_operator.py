"""CLI-LEASE remainder: hold and offline operator actions."""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import time

from .. import core, normalize
from ..core import Case
from ..mutants import Mutant
from . import _bank
from .lease import Board, TOKEN, T0, _Holder, _after, _local, _release_local, coordination_state


class OperatorBoard(Board):
    def dispatch(self, action, body, headers):
        if action == "agents":
            assert headers.get("Authorization") == f"Bearer {TOKEN}"
            self.calls[action] = self.calls.get(action, 0) + 1
            reply = self.store.list_agents("alice", headers.get("X-PL-Agent"), headers.get("X-PL-Agent-Key"), **body)
            self.states.append({"action": action, "state": _bank.dump(self.name)})
            return 200, reply
        return super().dispatch(action, body, headers)



@normalize.rule("lease-fixture-pid")
def fixture_pid(obs):
    """Associate the watched process PID, after validating raw audit chains."""
    from pseudolife_memory.storage.coordination import audit_hash, verify_audit_chain
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
    from pseudolife_memory import os_lock
    env = {"PSEUDOLIFE_LEASE_LOCK_DIR": "{HOME}/locks", "PSEUDOLIFE_AGENT_PROJECT": "fixture",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if board:
        env["PSEUDOLIFE_MCP_TOKEN"] = TOKEN
    def setup(arm):
        _local()(arm)
        watched = _Holder(arm.home / "watch.lock")
        assert watched.line() == "HELD"
        arm.daemon.locals.append(watched)
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
            if path.exists() and os_lock.probe(path) is True:
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
                           "local_free": os_lock.probe(arm.home / "locks" / "lease-fixture.lock") is not True,
                           "watched_gone": arm.state["watched"].proc.poll() is not None}
        if arm.name == "python" and not gone:
            assert obs["listener"]["local_seen"] and obs["listener"]["stable_hold"]
        if queued:
            assert b"waiting for the local lock" in base64.b64decode(obs["stderr"])
    case = Case(identifier, [], env=env, setup=setup, during=during, after=after,
                before_capture=_release_local if queued else None,
                daemon=lambda: OperatorBoard(bank=board), timeout=35,
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
        # audit path, so a mutant cannot become a matching projection.
        return
    obs.clear()
    obs.update(projected)


def _operator_clock(obs):
    """Validate raw clocks/chains, then associate only their timestamp paths."""
    from pseudolife_memory.storage.coordination import audit_hash, verify_audit_chain
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
            if row.get("name", "").startswith("delegate:"):
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


def operator_case(identifier, argv, scenario="missing", events=(), local=False, stdout_closed=False):
    from pseudolife_memory.lease_cli import parse_duration
    hold = parse_duration(argv[argv.index("--for") + 1]) if "--for" in argv else 86400
    env = {"PSEUDOLIFE_DAEMON_EXEC": "1", "PYTHONUTF8": "1",
           "PYTHONIOENCODING": "utf-8", "PSEUDOLIFE_LEASE_LOCK_DIR": "{HOME}/locks"}

    def setup(arm):
        env["PSEUDOLIFE_MCP_DATABASE_URL"] = _bank.url(arm.daemon.name)
        _local("held" if local else "absent")(arm)
        start = time.time()
        stamp = time.time()
        arm.daemon.store.clock = lambda: stamp
        arm.state["seed_stamp"] = stamp
        target = None
        if scenario != "missing":
            target = arm.daemon.register(label="codex", project="fixture", task="fixture target",
                                         capabilities={"resumable": True, "pull": True},
                                         wake_enabled=scenario in ("listener", "ring"))
        if scenario in ("held", "queue", "free"):
            arm.daemon.store.acquire_lease("alice", target["agent_id"], target["credential"],
                                         name="fixture", ttl=300, expect=60, purpose="fixture work")
        if scenario == "queue":
            other = arm.daemon.register(label="codex", project="fixture", task="first waiter")
            arm.daemon.store.acquire_lease("alice", other["agent_id"], other["credential"],
                                         name="fixture", ttl=120, expect=30, purpose="waiter work")
        if scenario == "free":
            arm.daemon.store.release_lease("alice", target["agent_id"], target["credential"], name="fixture")
        if scenario == "replace":
            other = arm.daemon.register(label="codex", project="fixture", task="old delegate")
            arm.daemon.store.grant_delegate("fixture", other["agent_id"], hold=300)
        if scenario == "coordinator":
            arm.daemon.store.acquire_lease("alice", target["agent_id"], target["credential"],
                                         name="coordinator:fixture", ttl=300)
        if scenario == "ambiguous":
            arm.daemon.register(label="codex", project="fixture", task="another target")
        if scenario in ("listener", "ring"):
            arm.daemon.store.attach("alice", target["agent_id"], target["credential"],
                                    attachment_id="fixture-listener", wake_enabled=True,
                                    ring=scenario == "ring", ring_armed_until=stamp + 600 if scenario == "ring" else None)
        case.argv[:] = ["lease", *(target["agent_id"] if arg == "{AGENT}" else arg for arg in argv)]
        arm.state["before"] = coordination_state(_bank.dump(arm.daemon.name))
        arm.state["seed_window"] = [start, time.time()]

    def after(arm, obs):
        if local:
            from pseudolife_memory.os_lock import probe
            obs["listener"] = {"local_still_held": probe(arm.home / "locks" / "lease-fixture.lock") is True}
            if arm.name == "python":
                assert obs["listener"]["local_still_held"]
        _after(arm, obs)
        obs["db"]["before"] = arm.state["before"]
        obs["seed_stamp"] = arm.state["seed_stamp"]
        obs["seed_window"] = arm.state["seed_window"]
        obs["expected_events"] = list(events)
        obs["hold"] = hold
        before = [json.loads(row) for row in obs["db"]["before"]["tables"]["coordination_events"]]
        final = [json.loads(row) for row in obs["db"]["final"]["tables"]["coordination_events"]]
        new = sorted(final, key=lambda row: row["seq"])[len(before):]
        assert [row["event"] for row in new] == list(events)

    case = Case(identifier, ["lease", *argv], env=env, setup=setup, after=after,
                daemon=OperatorBoard, stdout_closed=stdout_closed,
                rules=("lease-http-json", "lease-operator-clock", *(('lease-native-stdout',) if stdout_closed else ())))
    return case


def cases():
    result = [operator_case("break-missing", ["break", "missing"]),
              operator_case("break-free", ["break", "fixture"], "free"),
              operator_case("break-held", ["break", "fixture"], "held", ("lease_break",)),
              operator_case("break-fifo", ["break", "fixture"], "queue", ("lease_break", "lease_grant"))]
    for action in ("delegate", "designate"):
        result.append(operator_case(action + "-normal", [action, "fixture", "{AGENT}", "--for", "2m"],
                                    "target", ("lease_delegate",)))
    for scenario in ("replace", "coordinator"):
        events = ("lease_delegate",) if scenario == "replace" else ("lease_break", "lease_delegate")
        result.append(operator_case("delegate-" + scenario, ["delegate", "fixture", "{AGENT}", "--for", "2m"],
                                    scenario, events))
    result.extend([
        operator_case("delegate-prefix", ["delegate", "fixture", "00000000", "--for", "2m"],
                      "target", ("lease_delegate",)),
        operator_case("delegate-unknown", ["delegate", "fixture", "f" * 32, "--for", "2m"]),
        operator_case("delegate-ambiguous", ["delegate", "fixture", "00000000", "--for", "2m"], "ambiguous"),
        operator_case("delegate-default", ["delegate", "fixture", "{AGENT}"], "target", ("lease_delegate",)),
        operator_case("delegate-seven-days", ["delegate", "fixture", "{AGENT}", "--for", "7d"], "target", ("lease_delegate",)),
        operator_case("delegate-unicode-project", ["delegate", "资源", "{AGENT}", "--for", "2m"], "target", ("lease_delegate",)),
        operator_case("delegate-live-listener", ["delegate", "fixture", "{AGENT}", "--for", "2m"], "listener", ("lease_delegate",)),
        operator_case("delegate-live-ring", ["delegate", "fixture", "{AGENT}", "--for", "2m"], "ring", ("lease_delegate",)),
        hold_case("hold-no-board"), hold_case("hold-gone-pid", gone=True),
        hold_case("hold-local-wait", queued=True), hold_case("hold-board", board=True),
        hold_case("hold-board-renew", board=True, renew=True),
        operator_case("break-preserves-local-holder", ["break", "fixture"], "held", ("lease_break",), local=True),
        operator_case("break-closed-stdout", ["break", "fixture"], "held", ("lease_break",), stdout_closed=True),
        operator_case("break-refused-credential-name", ["break", "sk-" + "Ab9_" * 10]),
    ])
    for action in ("hold", "break", "delegate", "designate"):
        result.append(Case("help-" + action, ["lease", action, "--help"],
                           env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}))
    from .lease_transport import cases as transport_cases
    result.extend(transport_cases())
    return result


MUTANTS = [
    Mutant("lease-hold-status", "lease_operator", "shim/src/cli/lease/hold.rs",
           "        Ok(0)\n    };", "        Ok(7)\n    };", ("hold-no-board",)),
    Mutant("lease-break-holder", "lease_operator", "shim/src/cli/lease/operator/store.rs",
           "holder_agent_id=NULL,holder_principal=''", "holder_agent_id=holder_agent_id,holder_principal=''",
           ("break-held",)),
    Mutant("lease-delegate-duration", "lease_operator", "shim/src/cli/lease/operator/store/delegate.rs",
           "let expires_at = now + hold as f64;", "let expires_at = now + (hold + 1) as f64;",
           ("delegate-normal",)),
    Mutant("lease-designate-warning", "lease_operator", "shim/src/cli/lease/args.rs",
           "pseudolife-mcp lease designate is deprecated: use pseudolife-mcp lease delegate",
           "pseudolife-mcp lease designate is deprecated: changed alias warning", ("designate-normal",)),
]
