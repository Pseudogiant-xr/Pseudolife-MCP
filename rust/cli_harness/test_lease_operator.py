"""Pure CPU controls using captured operator and hold observations."""

import base64
import copy
import json
import re
from pathlib import Path

import pytest

from pseudolife_memory.storage.coordination import audit_hash, verify_audit_chain

from . import compare, normalize
from .rows import lease_operator
from .rows.lease import T0


PLATFORMS = ("windows", "linux")
CLOCK_RULES = ("lease-operator-clock",)
PID_RULES = ("lease-http-json", "lease-fixture-pid")


def captured(platform, identifier):
    path = Path(__file__).parent / "goldens" / f"lease_operator.{platform}.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"][identifier]


def records(state, table):
    return [json.loads(raw) for raw in state["tables"][table]]


def store(state, table, rows):
    state["tables"][table] = [json.dumps(row, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")) for row in rows]


def audit(state):
    return sorted(records(state, "coordination_events"), key=lambda row: row["seq"])


def rehash(state):
    rows = audit(state)
    previous = rows[0]["prev_hash"] if rows else None
    for row in rows:
        row["prev_hash"] = previous
        row["hash"] = audit_hash(previous, row)
        previous = row["hash"]
    store(state, "coordination_events", rows)
    assert verify_audit_chain(rows)["ok"]


def clock_fields(value, change):
    if isinstance(value, dict):
        return {key: change(item) if isinstance(item, (int, float))
                and not isinstance(item, bool)
                and (key.endswith(("_at", "_until"))
                     or key in ("expected_end", "last_activity", "last_seen"))
                else clock_fields(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [clock_fields(item, change) for item in value]
    return value


def shift_clocks(obs, seed_delta=4096, operation_delta=8192):
    """Move the captured seed and operation independently, preserving durations."""
    out = copy.deepcopy(obs)
    seeded = set()
    for rows in obs["db"]["before"]["tables"].values():
        for raw in rows:
            row = json.loads(raw)
            clock_fields(row, lambda value: seeded.add(value) or value)
            if "payload" in row:
                clock_fields(json.loads(row["payload"]), lambda value: seeded.add(value) or value)
    for which in ("before", "final"):
        state = out["db"][which]
        for table in state["tables"]:
            rows = records(state, table)
            def shift(value):
                return value + (seed_delta if value in seeded else operation_delta)
            for row in rows:
                updated = clock_fields(row, shift)
                if "payload" in row:
                    updated["payload"] = json.dumps(clock_fields(json.loads(row["payload"]), shift),
                                                     ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":"))
                row.clear()
                row.update(updated)
            store(state, table, rows)
        rehash(state)
    out["seed_stamp"] += seed_delta
    out["seed_window"] = [value + seed_delta for value in obs["seed_window"]]
    out["window"] = [value + operation_delta for value in obs["window"]]
    raw = base64.b64decode(obs["stdout"])
    raw = re.sub(rb'("expires_at": )([0-9.eE+-]+)',
                 lambda match: match[1] + str(float(match[2]) + operation_delta).encode(), raw)
    out["stdout"] = base64.b64encode(raw).decode()
    return out


CLOCK_CASES = tuple(identifier for identifier, obs in json.loads(
    (Path(__file__).parent / "goldens" / "lease_operator.windows.json").read_text(
        encoding="utf-8"))["cases"].items() if "seed_stamp" in obs)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("identifier", CLOCK_CASES)
def test_captured_clocks_compare_in_their_own_seed_and_operation_windows(platform, identifier):
    original = captured(platform, identifier)
    raw_copy = copy.deepcopy(original)
    later = shift_clocks(original)
    assert not compare.diff(original, later, CLOCK_RULES)
    projected = normalize.apply(original, CLOCK_RULES, None)
    old = audit(projected["db"]["before"])
    final = audit(projected["db"]["final"])
    assert verify_audit_chain(old)["ok"] and verify_audit_chain(final)["ok"]
    assert final[:len(old)] == old
    assert [row["created_at"] for row in final[len(old):]] == [T0 + 100] * len(original["expected_events"])
    assert original == raw_copy
    assert verify_audit_chain(audit(original["db"]["before"]))["ok"]
    assert verify_audit_chain(audit(original["db"]["final"]))["ok"]
    if old:
        assert projected["db"]["before"] != original["db"]["before"]


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("change", ["expiry", "stdout-expiry", "duration", "expected-end",
                                   "new-event-clock", "operation-window", "seed-window",
                                   "raw-hash", "old-prefix", "seed-stamp", "before-clock"])
def test_invalid_clocks_and_audit_paths_keep_the_entire_raw_observation(platform, change):
    identifier = "break-fifo" if change in ("expected-end", "new-event-clock") else "delegate-normal"
    good = captured(platform, identifier)
    bad = copy.deepcopy(good)
    final = bad["db"]["final"]
    leases = records(final, "coordination_leases")
    held = next(row for row in leases if row["holder_agent_id"])
    if change == "expiry":
        held["expires_at"] += 1
        store(final, "coordination_leases", leases)
    elif change == "stdout-expiry":
        reply = json.loads(base64.b64decode(bad["stdout"]))
        reply["expires_at"] += 1
        bad["stdout"] = base64.b64encode(json.dumps(reply).encode()).decode()
    elif change == "duration":
        bad["hold"] += 1
    elif change == "expected-end":
        held["expected_end"] += 1
        store(final, "coordination_leases", leases)
    elif change in ("new-event-clock", "raw-hash", "old-prefix"):
        rows = audit(final)
        if change == "new-event-clock":
            rows[-1]["created_at"] += 0.01
        elif change == "old-prefix":
            rows[0]["created_at"] += 0.01
        else:
            rows[-1]["hash"] = "f" * 64
        store(final, "coordination_events", rows)
        if change != "raw-hash":
            rehash(final)
    elif change == "operation-window":
        bad["window"] = [value + 86400 for value in bad["window"]]
    elif change == "seed-window":
        bad["seed_window"] = [value + 86400 for value in bad["seed_window"]]
    elif change == "seed-stamp":
        bad["seed_stamp"] = bad["seed_window"][1] + 1
    else:
        rows = records(bad["db"]["before"], "coordination_agents")
        rows[0]["created_at"] += 1
        store(bad["db"]["before"], "coordination_agents", rows)
    differences = compare.diff(good, bad, CLOCK_RULES)
    assert differences, change
    if change == "before-clock":
        # An unrelated seed-table edit may remain projectable, but must survive comparison.
        assert any(line.startswith("db differ:") for line in differences)
    else:
        assert normalize.apply(bad, CLOCK_RULES, None) == bad


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("change", ["in-window-seed", "operation-as-seed"])
def test_adversarial_seed_metadata_and_before_tables_cannot_create_a_match(platform, change):
    good = captured(platform, "delegate-normal")
    bad = copy.deepcopy(good)
    if change == "in-window-seed":
        bad["seed_stamp"] = sum(bad["seed_window"]) / 2
        assert bad["seed_stamp"] != good["seed_stamp"]
    else:
        # A fabricated before-table timestamp must not authorize a new event's
        # clock to disappear as seed data; both raw audit chains still verify.
        operation = audit(bad["db"]["final"])[-1]["created_at"]
        for which in ("before", "final"):
            state = bad["db"][which]
            agents = records(state, "coordination_agents")
            agents[0]["created_at"] = operation
            store(state, "coordination_agents", agents)
            assert verify_audit_chain(audit(state))["ok"]
    assert compare.diff(good, bad, CLOCK_RULES)


def rebind_pid(obs):
    out = copy.deepcopy(obs)
    old, new = obs["fixture_pid"], obs["fixture_pid"] + 1
    pattern = re.compile(rf"\bpid {old}\b")
    for request in out["requests"]:
        request["body"] = pattern.sub(f"pid {new}", request["body"])
        request["headers"]["content-length"] = str(len(request["body"].encode()))
    for stream in ("stdout", "stderr"):
        raw = base64.b64decode(out[stream]).decode()
        out[stream] = base64.b64encode(pattern.sub(f"pid {new}", raw).encode()).decode()
    states = [item["state"] for item in out.get("db", {}).get("writes", [])]
    if "final" in out.get("db", {}):
        states.append(out["db"]["final"])
    for state in states:
        for table, rows in state["tables"].items():
            state["tables"][table] = [pattern.sub(f"pid {new}", raw) for raw in rows]
        rehash(state)
    out["fixture_pid"] = new
    return out


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("identifier", ["hold-gone-pid", "hold-board", "hold-board-renew"])
def test_only_the_captured_watched_pid_is_associated(platform, identifier):
    original = captured(platform, identifier)
    rebound = rebind_pid(original)
    assert not compare.diff(original, rebound, PID_RULES)
    rebound["fixture_pid"] = original["fixture_pid"]
    assert compare.diff(original, rebound, PID_RULES)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_pid_association_checks_raw_audit_before_rehashing(platform):
    bad = captured(platform, "hold-board")
    final = bad["db"]["final"]
    rows = audit(final)
    rows[-1]["hash"] = "f" * 64
    store(final, "coordination_events", rows)
    with pytest.raises(AssertionError):
        normalize.apply(bad, PID_RULES, None)
