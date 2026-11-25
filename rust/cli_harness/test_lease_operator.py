"""Pure CPU controls using captured operator and hold observations."""

import ast
import base64
import copy
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from rust.harness_support.audit_oracle import audit_hash, verify_audit_chain
from rust.harness_support.import_blocker import cold_harness_imports

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
    identifier = "break-fifo" if change in ("expected-end", "new-event-clock") else "conductor-normal"
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
    good = captured(platform, "conductor-normal")
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


@pytest.mark.parametrize("identifier,project,rewrite,rules", [
    pytest.param("break-fifo", lease_operator._operator_clock, shift_clocks, CLOCK_RULES, id="clock"),
    pytest.param("hold-board", lease_operator.fixture_pid, rebind_pid, PID_RULES, id="pid"),
])
def test_banked_lease_comparison_blocks_frozen_imports(identifier, project, rewrite, rules):
    original = captured("linux", identifier)
    assert audit(original["db"]["final"])
    with cold_harness_imports():
        project(copy.deepcopy(original))
        assert not compare.diff(original, rewrite(original), rules)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_pid_association_checks_raw_audit_before_rehashing(platform):
    bad = captured(platform, "hold-board")
    final = bad["db"]["final"]
    rows = audit(final)
    rows[-1]["hash"] = "f" * 64
    store(final, "coordination_events", rows)
    with pytest.raises(AssertionError):
        normalize.apply(bad, PID_RULES, None)


def _failure(arm, stderr, exit_code=1, failure_class="RaiseException", commit_unknown=False):
    return {"arm": arm, "exit": exit_code, "failure_class": failure_class,
            "commit_unknown": commit_unknown, "stderr": base64.b64encode(stderr).decode()}


NATIVE_FAILURE = b"break failed (PostgreSQL transaction failed); nothing was changed"
LOST_TAIL = b"; it may have been committed: run `pseudolife-mcp lease list` before retrying"


def _lost(arm, stderr, **kwargs):
    kwargs.setdefault("failure_class", "AdminShutdown")
    return _failure(arm, stderr, commit_unknown=True, **kwargs)


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
@pytest.mark.parametrize("verb", [b"break", b"conductor"])
def test_an_expected_lost_commit_line_maps_to_the_native_diagnostic(verb, eol):
    obs = _lost("python", verb + b" lost the answer to its COMMIT (AdminShutdown)" + LOST_TAIL + eol)
    normalize.RULES["lease-native-pg-diagnostics"](obs)
    expected = verb + b" lost the answer to its COMMIT (PostgreSQL session ended)" + LOST_TAIL + eol
    assert base64.b64decode(obs["stderr"]) == expected


@pytest.mark.parametrize("obs", [
    _lost("rust", b"break lost the answer to its COMMIT (AdminShutdown)" + LOST_TAIL + b"\n"),
    # The near miss in each direction: a case expecting the uncertain line
    # never maps the certain one, and the reverse.
    _lost("python", b"break failed (AdminShutdown); nothing was changed\n"),
    _failure("python", b"break lost the answer to its COMMIT (RaiseException)" + LOST_TAIL + b"\n"),
    _lost("python", b"break lost the answer to its COMMIT (OperationalError)" + LOST_TAIL + b"\n"),
    _lost("python", b"break lost the answer to its COMMIT (AdminShutdown)" + LOST_TAIL + b"\n", exit_code=0),
    _lost("python", b"break lost the answer to its COMMIT (AdminShutdown)" + LOST_TAIL + b"\nmore\n"),
    _lost("python", b"break lost the answer to its COMMIT (AdminShutdown); it may have been committed\n"),
], ids=["candidate", "certain-when-unknown", "unknown-when-certain", "other-class", "exit-0",
        "suffix", "wording"])
def test_near_miss_lost_commit_lines_keep_the_raw_stream(obs):
    before = copy.deepcopy(obs)
    normalize.RULES["lease-native-pg-diagnostics"](obs)
    assert obs == before


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
@pytest.mark.parametrize("verb", [b"break", b"conductor"])
def test_only_the_expected_oracle_failure_class_maps_to_the_native_diagnostic(verb, eol):
    rule = normalize.RULES["lease-native-pg-diagnostics"]
    obs = _failure("python", verb + b" failed (RaiseException); nothing was changed" + eol)
    rule(obs)
    expected = verb + b" failed (PostgreSQL transaction failed); nothing was changed" + eol
    assert base64.b64decode(obs["stderr"]) == expected


@pytest.mark.parametrize("obs", [
    # The candidate's own line is never rewritten, whatever it says.
    _failure("rust", b"break failed (RaiseException); nothing was changed\n"),
    # A class the case did not expect stays raw.
    _failure("python", b"break failed (AdminShutdown); nothing was changed\n"),
    # Any other exit, extra line, refusal or changed wording stays raw.
    _failure("python", b"break failed (RaiseException); nothing was changed\n", exit_code=0),
    _failure("python", b"warning\nbreak failed (RaiseException); nothing was changed\n"),
    _failure("python", b"break failed (RaiseException); nothing was changed\nmore\n"),
    _failure("python", b"break refused: invalid_lease\n"),
    _failure("python", b"break failed (RaiseException); it may have changed\n"),
    _failure("python", b"hold failed (RaiseException); nothing was changed\n"),
], ids=["candidate", "other-class", "exit-0", "prefix", "suffix", "refusal", "wording", "verb"])
def test_near_miss_failure_lines_keep_the_raw_stream(obs):
    before = copy.deepcopy(obs)
    normalize.RULES["lease-native-pg-diagnostics"](obs)
    assert obs == before


def test_a_mapped_oracle_failure_still_differs_from_a_candidate_that_succeeded():
    python = {"exit": 1, "stdout": "", "stderr": base64.b64encode(
        b"break failed (RaiseException); nothing was changed\n").decode(),
        "arm": "python", "failure_class": "RaiseException", "db": {"final": 1}}
    rust = {"exit": 0, "stdout": base64.b64encode(b'{"name": "fixture"}\n').decode(),
            "stderr": "", "arm": "rust", "db": {"final": 2}}
    lines = compare.diff(python, rust, ("lease-native-pg-diagnostics",))
    assert any(line.startswith("exit:") for line in lines)
    assert any(line.startswith("db differ") for line in lines)
    same = dict(rust, exit=1, stdout="", db={"final": 1},
                stderr=base64.b64encode(NATIVE_FAILURE + b"\n").decode())
    assert compare.diff(python, same, ("lease-native-pg-diagnostics",)) == []


# The row's own state, built without the frozen package: the recorded seeds
# and the recorded hold board (fixtures/lease-operator.json).

ROW = Path(__file__).parent / "rows" / "lease_operator.py"


def _names_the_package(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "pseudolife_memory"
                                                for alias in node.names):
            yield node.lineno
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pseudolife_memory":
            yield node.lineno
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "pseudolife_memory" in node.value:
            # An importlib or patch target by name.
            yield node.lineno


def test_lease_operator_row_never_reaches_the_frozen_package():
    # The base row imported the package lazily (inside the board and its
    # dispatch, and as patch targets), which an import-time check cannot see.
    assert list(_names_the_package(ast.parse(ROW.read_text(encoding="utf-8")))) == []


def test_lease_operator_row_loads_with_the_package_blocked():
    # A fresh interpreter with the package import refused. Only the module is
    # imported: building cases is left out, as in the other rows' cold-load
    # tests, because a row's cases may read the test login, which the parity
    # cli lane's pytest step does not have.
    script = "\n".join([
        "import sys",
        "sys.modules['pseudolife_memory'] = None",
        "sys.path.insert(0, sys.argv[1])",
        "from rust.harness_support.import_blocker import cold_harness_imports",
        "with cold_harness_imports():",
        "    from rust.cli_harness.rows import lease_operator",
        "    assert callable(lease_operator.cases)",
        "assert not any(name.startswith('pseudolife_memory.') for name in sys.modules)",
    ])
    repo = Path(__file__).resolve().parents[2]
    child = subprocess.run([sys.executable, "-I", "-c", script, str(repo)],
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr


def _scenarios():
    lease_operator.cases()
    return lease_operator.CASE_SCENARIOS


def _canonical(rows):
    return sorted(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("identifier", CLOCK_CASES)
def test_recorded_seeds_rebuild_every_golden_before_state(platform, identifier):
    obs = captured(platform, identifier)
    scenario = _scenarios()[identifier]
    rows, sequences, target = lease_operator.seed_rows(scenario, obs["seed_stamp"])
    before = obs["db"]["before"]
    for table, raws in before["tables"].items():
        assert _canonical(rows.get(table, [])) == _canonical(json.loads(raw) for raw in raws), table
    assert set(rows) <= set(before["tables"])
    assert sequences == before["sequences"]
    # The case's {AGENT} is the agent the operator's grant went to.
    seeded = {json.dumps(json.loads(raw), sort_keys=True) for raw in before["tables"]["coordination_leases"]}
    granted = [row for row in records(obs["db"]["final"], "coordination_leases")
               if row["name"].startswith("conductor:") and row["holder_agent_id"]
               and json.dumps(row, sort_keys=True) not in seeded]
    for row in granted:
        assert row["holder_agent_id"] == target


def test_every_seed_scenario_is_recorded_and_used():
    assert set(_scenarios().values()) == set(lease_operator._fixture()["seeds"])


class _Connection:
    def __init__(self):
        self.statements = []
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        self.statements.append("BEGIN")
        yield
        self.statements.append("COMMIT")

    def execute(self, statement, params=()):
        self.statements.append((statement, params))


@pytest.mark.parametrize("scenario", ["queue", "missing", "legacy"])
def test_seed_writes_the_recorded_rows_in_one_transaction_with_the_package_blocked(monkeypatch, scenario):
    from rust.harness_support.import_blocker import cold_harness_imports

    inserted = []

    def insert(conn, table, rows):
        # The real helper locks the table and reads its catalog on conn,
        # which needs the seed's transaction.
        conn.statements.append(f"PREPARE {table}")
        inserted.append((table, rows))
        return f"INSERT {table}", (len(rows),)

    conn = _Connection()
    with cold_harness_imports():
        from rust.cli_harness.rows import lease_operator as row
        from rust.harness_support import fixture_rows
        monkeypatch.setattr(fixture_rows, "checked_fixture_insert", insert)
        target = row.seed_bank(conn, scenario, 1_800_000_000.5)
        rows, sequences, expected_target = row.seed_rows(scenario, 1_800_000_000.5)
        tables = row._SEED_TABLES
    assert target == expected_target
    assert inserted == [(table, rows[table]) for table in tables if table in rows]
    setvals = [("SELECT setval(%s::regclass, %s, true)", ("public." + name, value))
               for name, value in sequences.items() if value is not None]
    assert conn.statements == ["BEGIN", *[f"PREPARE {table}" for table, _ in inserted],
                               *[(f"INSERT {table}", (len(written),)) for table, written in inserted],
                               *setvals, "COMMIT"]
    assert conn.transactions == 1
    if scenario != "missing":
        assert verify_audit_chain(sorted(rows["coordination_events"], key=lambda row: row["seq"]))["ok"]
        assert setvals


def test_a_seed_naming_a_table_the_seed_does_not_write_is_refused(monkeypatch):
    fixture = copy.deepcopy(lease_operator._fixture())
    fixture["seeds"]["held"]["rows"]["coordination_messages"] = [{"id": 1}]
    monkeypatch.setattr(lease_operator, "_FIXTURE", fixture)
    with pytest.raises(ValueError, match="unexpected tables"):
        lease_operator.seed_rows("held", 1_800_000_000.0)


def _headers(request):
    headers = HTTPMessage()
    for name, value in request["headers"].items():
        headers[name] = value
    return headers


def _replay(board, obs, pid):
    pattern = re.compile(rf"\bpid {obs['fixture_pid']}\b")
    requests = []
    for request in obs["requests"]:
        body = pattern.sub(f"pid {pid}", request["body"])
        status, reply = board.dispatch(request["target"].rsplit("/", 1)[-1], json.loads(body), _headers(request))
        assert status == 200, reply
        assert "<PID>" not in json.dumps(reply)
        requests.append({**request, "body": body,
                         "headers": {**request["headers"], "content-length": str(len(body.encode()))}})
    return requests


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("identifier", ["hold-board", "hold-board-renew"])
def test_recorded_hold_board_answers_each_golden_for_its_watched_pid(platform, identifier):
    from rust.harness_support.import_blocker import cold_harness_imports

    obs = captured(platform, identifier)
    with cold_harness_imports():
        from rust.cli_harness.rows import lease_operator as row
        same = row.HoldBoard(bank=True)
        other = row.HoldBoard(bank=True)
    try:
        # The golden's own pid: every state it recorded, byte for byte.
        same.pid = obs["fixture_pid"]
        _replay(same, obs, same.pid)
        assert same.states == obs["db"]["writes"]
        assert same.final_state() == obs["db"]["final"]
        # Another pid: the rows carry it, the chains verify, and the
        # observation compares equal to the golden as the replay compares.
        other.pid = obs["fixture_pid"] + 7919
        requests = _replay(other, obs, other.pid)
        for item in other.states:
            text = json.dumps(item["state"])
            assert f"pid {other.pid}" in text and "<PID>" not in text
            assert verify_audit_chain(audit(item["state"]))["ok"]
        candidate = {**copy.deepcopy(obs), "requests": requests, "fixture_pid": other.pid,
                     "db": {"writes": other.states, "final": other.final_state()}}
        assert not compare.diff(obs, candidate, PID_RULES)
    finally:
        same.close()
        other.close()


def test_hold_board_refuses_an_unrecorded_request_and_an_unset_pid():
    from .rows import lease

    obs = captured("linux", "hold-board")
    board = lease_operator.HoldBoard(bank=True)
    try:
        request = obs["requests"][0]
        with pytest.raises(RuntimeError, match="pid was never set"):
            board.dispatch("register", json.loads(request["body"]), _headers(request))
        board.pid = obs["fixture_pid"]
        status, reply = board.dispatch("register", {**json.loads(request["body"]), "task": "other"},
                                       _headers(request))
        assert status == 500 and "not in the recording" in reply["error"]
        with pytest.raises(AssertionError, match="never answered"):
            lease._after(SimpleNamespace(daemon=board, name="rust", state={}), {})
    finally:
        board.close()
