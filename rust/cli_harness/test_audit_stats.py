"""The stats comparison must retain every computed field and reject bad clocks."""
import base64
import copy
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rust.harness_support.database_guard import ProductionDatabaseError
from rust.harness_support.import_blocker import cold_harness_imports

from . import core
from .rows import audit


STATS_FIXTURE = Path(__file__).resolve().parents[1] / "shim/src/cli/board_audit/testdata/stats.json"


def stats_corpus():
    return json.loads(STATS_FIXTURE.read_text(encoding="utf-8"))


def test_live_reference_runs_only_the_candidate(monkeypatch, tmp_path):
    calls = []

    def reference(home):
        calls.append(("reference", home))
        return {"exit": 0, "stdout": "", "stderr": ""}

    def run_arm(case, target, home):
        assert target.name == "rust", "the live reference launched the Python arm"
        calls.append(("rust", home))
        return {"exit": 1, "stdout": "", "stderr": ""}

    monkeypatch.setattr(core, "_home_root", lambda: tmp_path / "case")
    monkeypatch.setattr(core, "run_arm", run_arm)
    case = core.Case("reference", [])
    case.reference = reference
    expected, actual = core.run_case(case, core.Target("python", []), core.Target("rust", []))

    assert calls == [("reference", tmp_path / "case/h"), ("rust", tmp_path / "case/h")]
    assert expected["exit"] == 0 and actual["exit"] == 1


@pytest.mark.skipif(core.WINDOWS, reason="POSIX permission bits")
@pytest.mark.parametrize(("mask", "directory", "file_mode"), [
    (0o002, "0o775", "0o664"),
    (0o077, "0o700", "0o600"),
])
def test_live_recorded_reference_retains_permission_checks(tmp_path, mask, directory, file_mode):
    previous = os.umask(mask)
    try:
        want = audit._recorded_reference("transport-rust-export-0", tmp_path / "h")
    finally:
        os.umask(previous)
    assert want["modes"] == {
        "cwd": directory, "docker.jsonl": file_mode, "docker.stdin": file_mode, "out.jsonl": "0o600",
    }


def test_committed_stats_fixture_matches_its_synthetic_inputs_and_report_bytes():
    with cold_harness_imports():
        empty, telemetry, integer = stats_corpus()
        events = audit.stats_events()
        durations = [json.loads(line) for line in audit.stats_durations().splitlines()[1:]]
    assert [case["name"] for case in (empty, telemetry, integer)] == [
        "empty", "telemetry", "integer-times",
    ]
    assert empty["events"] == empty["wakes"] == empty["durations"] == []
    assert telemetry["events"] == events
    assert integer["events"] == [dict(row, created_at=int(row["created_at"])) for row in events]
    assert all(type(row["created_at"]) is float for row in telemetry["events"])
    assert all(type(row["created_at"]) is int for row in integer["events"])
    assert telemetry["wakes"] == integer["wakes"] == [
        {"recipient_agent_id": "b" * 32, "decision": decision, "created_at": created, "served_at": served}
        for decision, created, served in [
            ("rung", 1400.0, 1401.0), ("nudged", 1410.0, None), ("rung", 1450.0, 5000.0),
        ]
    ]
    assert telemetry["durations"] == integer["durations"] == durations
    for case in (empty, telemetry, integer):
        assert json.dumps(case["report"], ensure_ascii=True, separators=(",", ":")) == case["line"]


@pytest.fixture
def stats_connection(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    return MagicMock()


@pytest.mark.parametrize("database,error,message", [
    ("pseudolife_memory", ProductionDatabaseError, "production bank"),
    ("pseudolife_bench", ValueError, "disposable w1c database"),
])
def test_stats_seed_refuses_server_database_before_writes(stats_connection, database, error, message):
    stats_connection.execute.return_value.fetchone.return_value = (database,)
    with pytest.raises(error, match=message):
        audit.seed_stats(stats_connection)
    stats_connection.execute.assert_called_once_with("SELECT current_database()")
    stats_connection.transaction.assert_not_called()
    stats_connection.cursor.assert_not_called()


def test_stats_seed_builds_the_chain_without_the_frozen_package(stats_connection):
    stats_connection.execute.return_value.fetchone.return_value = ("pl_cf_w1c_audit_stats_fixture",)
    with cold_harness_imports():
        assert audit.seed_stats(stats_connection) == {}
    stats_connection.execute.assert_called_once_with("SELECT current_database()")
    stats_connection.transaction.assert_called_once_with()
    cursor = stats_connection.cursor.return_value.__enter__.return_value
    (events_call, wakes_call) = cursor.executemany.call_args_list
    events_sql, events = events_call.args
    wakes_sql, wakes = wakes_call.args
    assert "INSERT INTO coordination_events" in events_sql
    assert "INSERT INTO coordination_wakes" in wakes_sql
    assert [row["seq"] for row in events] == list(range(1, len(audit.stats_events()) + 4))
    assert [row["created_at"] for row in events] == sorted(row["created_at"] for row in events)
    assert audit.verify_audit_chain(events)["ok"]
    assert [wake[3] for wake in wakes] == ["rung", "nudged", "rung", "rung", "hinted"]


def observation():
    report = dict(stats_corpus()[0]["report"], generated_at=6000.123, version="0.24.0", sources={})
    return {"arm": "python", "window": [6000.0, 6001.0], "stats_defaults": False,
            "stdout": base64.b64encode((json.dumps(report, separators=(",", ":")) + "\n").encode()).decode(),
            "stderr": "", "exit": 0, "files": {}}


def test_stats_observation_loads_without_the_frozen_package():
    with cold_harness_imports():
        obs = observation()
        audit.stats_clock(obs)
    assert json.loads(base64.b64decode(obs["stdout"]))["generated_at"] == "<generated_at>"


def test_only_validated_clock_and_version_are_replaced():
    obs = observation()
    before = json.loads(base64.b64decode(obs["stdout"]))
    audit.stats_clock(obs)
    after = json.loads(base64.b64decode(obs["stdout"]))
    assert after.pop("generated_at") == "<generated_at>"
    assert after.pop("version") == "<version>"
    before.pop("generated_at")
    before.pop("version")
    assert after == before


@pytest.mark.parametrize("field,value", [("generated_at", 50), ("version", "invented")])
def test_invalid_runtime_fields_fail_the_comparison(field, value):
    obs = observation()
    report = json.loads(base64.b64decode(obs["stdout"]))
    report[field] = value
    obs["stdout"] = base64.b64encode(json.dumps(report).encode()).decode()
    with pytest.raises(AssertionError):
        audit.stats_clock(obs)


def test_computed_counts_and_output_file_payload_are_not_hidden():
    obs = observation()
    bad = copy.deepcopy(obs)
    report = json.loads(base64.b64decode(bad["stdout"]))
    report["mail_latency"]["sends"] = 3
    bad["stdout"] = base64.b64encode(json.dumps(report).encode()).decode()
    obs["files"]["out.jsonl"] = "file:" + obs["stdout"]
    bad["files"]["out.jsonl"] = "file:" + bad["stdout"]
    audit.stats_clock(obs)
    audit.stats_clock(bad)
    assert obs["stdout"] != bad["stdout"]
    assert obs["files"] != bad["files"]
