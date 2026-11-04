"""The precheck retains IDs and continues independent failures."""
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

import pr_precheck_run as replay


def plan(**updates):
    return {"schema": 1, "linux": ["doctor"], "windows": ["doctor"], "judges": [], **updates}


def mock_summary_replace(monkeypatch):
    # These tests check summary data, not native filesystem replacement.
    monkeypatch.setattr(Path, "replace", lambda source, target: target.write_bytes(source.read_bytes()))


@pytest.mark.parametrize("updates", [{"schema": True}, {"linux": ["unknown"]}, {"judges": ["unknown"]},
                                     {"windows": ["lease"]}, {"linux": ["doctor", "doctor"]}])
def test_invalid_plans_are_rejected(updates):
    with pytest.raises(ValueError):
        replay.plan_value(json.dumps(plan(**updates)))


def test_golden_replay_uses_the_existing_case_selector():
    command = replay.golden_command("doctor", Path("candidate"), Path("receipt"))
    assert command[1:] == ["rust/cli_harness", "--row", "doctor", "--golden", "--candidate", "candidate", "--out", "receipt"]
    assert not {"--skip-bank", "--case", "--record", "--mutants"} & set(command)


def test_h5_rows_reuse_the_existing_record_then_golden_protocol(tmp_path):
    from coverage_plan import AUDIT_GOLDEN_CASES
    for row in ("audit", "backup", "migrate_flat"):
        commands = replay.row_commands(row, tmp_path / "pseudolife-stdio.exe", tmp_path)
        assert [phase for phase, _, _ in commands] == [f"record/{row}", f"golden/{row}"]
        for _, command, _ in commands:
            assert "--golden-dir" in command
            assert [command[index + 1] for index, value in enumerate(command) if value == "--case"] == (AUDIT_GOLDEN_CASES if row == "audit" else [])


@pytest.mark.parametrize("phase", replay.JUDGES)
def test_frozen_judge_commands_preserve_existing_modes_and_full_selection(phase, tmp_path):
    command, receipt = replay.judge_command(phase, Path("python"), Path("source"), Path("candidate"), tmp_path, "checked")
    assert receipt is not None
    assert not {"--case", "--skip-process-tests", "--skip-faults", "--skip-generic-controls", "-k"} & set(command)
    if phase == "cli-process":
        assert command[command.index("--modes") + 1:command.index("--modes") + 4] == ["help", "version", "lease"]
    if phase == "wait-mail-tests":
        assert all(path in command for path in replay.WAIT_MAIL_TESTS)


def test_golden_failures_expose_case_ids_without_raw_differences(tmp_path):
    receipt = tmp_path / "golden.json"
    receipt.write_text(json.dumps({"complete": True, "rows": {"doctor": {
        "diffs": ["stale-output"], "results": {"stale-output": {"detail": ["private raw stream"]}}}}}))
    assert replay.case_ids("golden/doctor", receipt) == (["doctor/stale-output"], [])


def test_pytest_failures_retain_their_node_identifiers(tmp_path):
    receipt = tmp_path / "wait.xml"
    receipt.write_text('<testsuites><testsuite><testcase classname="evals.rust_port.test_wait" name="test_case"><failure>raw</failure></testcase></testsuite></testsuites>')
    ids, errors = replay.case_ids("wait-mail-tests", receipt)
    assert ids == ["evals.rust_port.test_wait::test_case"] and errors == []


def test_failures_do_not_skip_later_rows_or_judges(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    calls = []
    def execute(phase, command, private, receipt, **kwargs):
        calls.append(phase)
        return {"phase": phase, "exit_code": int(phase in ("golden/doctor", "lease-headers")),
                "failed_cases": [phase + "/case"] if phase in ("golden/doctor", "lease-headers") else [],
                "seconds": 0, "diagnostic_errors": []}
    monkeypatch.setattr(replay, "execute", execute)
    value = plan(linux=["doctor", "lease"], windows=["doctor", "lease"], judges=["lease-headers", "cli-argv"])
    assert replay.run(value, Path("candidate"), tmp_path / "private", tmp_path / "public") == 1
    assert calls == ["golden/doctor", "golden/lease", "oracle-setup", "lease-check", "lease-headers", "cli-argv"]
    summary = json.loads((tmp_path / "public/summary.json").read_text())
    assert summary["failed"] == ["golden/doctor", "lease-headers"] and summary["complete"] is True
    assert summary["phases"][0]["failed_cases"] == ["golden/doctor/case"]


def test_incomplete_records_remain_visible(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    replay.write_summary(tmp_path, [{"phase": "golden/doctor", "exit_code": None}])
    assert json.loads((tmp_path / "summary.json").read_text())["complete"] is False


def test_setup_failure_keeps_unrun_rows_visible_and_continues_independent_judges(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    @contextmanager
    def postgres(private):
        raise RuntimeError("private connection detail")
        yield
    monkeypatch.setattr(replay, "golden_postgres", postgres)
    monkeypatch.setattr(replay, "execute", lambda phase, *args, **kwargs: {
        "phase": phase, "exit_code": 0, "seconds": 0, "failed_cases": [], "diagnostic_errors": []})
    value = plan(linux=["audit"], windows=["audit"], judges=["cli-argv"])
    assert replay.run(value, Path("candidate"), tmp_path / "private", tmp_path / "public") == 1
    text = (tmp_path / "public/summary.json").read_text()
    summary = json.loads(text)
    assert summary["complete"] is False
    phases = {row["phase"]: row for row in summary["phases"]}
    assert phases["record/audit"]["exit_code"] is None
    assert phases["cli-argv"]["exit_code"] == 0
    assert "private connection detail" not in text


def test_test_login_prepares_the_pg_bundle_before_its_cluster_cases(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    events = []
    @contextmanager
    def postgres(private):
        events.append("pg-open")
        yield {"owned": "fixture"}
        events.append("pg-close")
    def execute(phase, *args, env=None, **kwargs):
        events.append(phase)
        if phase == "golden/test_login":
            assert env == {"owned": "fixture"}
        return {"phase": phase, "exit_code": 0, "seconds": 0, "failed_cases": [], "diagnostic_errors": []}
    monkeypatch.setattr(replay, "golden_postgres", postgres)
    monkeypatch.setattr(replay, "execute", execute)
    value = plan(linux=["test_login"], windows=["test_login"])
    assert replay.run(value, Path("candidate"), tmp_path / "private", tmp_path / "public") == 0
    assert events == ["golden-lease-check", "pg-open", "golden/test_login", "pg-close"]


def test_owned_golden_fixture_does_not_inherit_a_configured_bank(tmp_path, monkeypatch):
    from evals.rust_port import phase1_ci
    from pseudolife_memory import test_login_cli
    configured = "postgresql://fixture.invalid/configured-bank"
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", configured)
    @contextmanager
    def postgres():
        monkeypatch.setenv("PSEUDOLIFE_BENCH_ADMIN_URL", "host=127.0.0.1 port=1 user=fixture dbname=postgres")
        yield
    def provision(args, **kwargs):
        assert "PSEUDOLIFE_MCP_DATABASE_URL" not in os.environ
        assert args[args.index("--admin-url") + 1] == os.environ["PSEUDOLIFE_BENCH_ADMIN_URL"]
        return 0
    monkeypatch.setattr(phase1_ci, "postgres", postgres)
    monkeypatch.setattr(test_login_cli, "main", provision)
    with replay.golden_postgres(tmp_path) as env:
        assert "PSEUDOLIFE_MCP_DATABASE_URL" not in env
        assert env["PSEUDOLIFE_TEST_PG_HOST_PORT"] == "127.0.0.1:1"
    assert os.environ["PSEUDOLIFE_MCP_DATABASE_URL"] == configured


def test_real_bank_exit_differences_retain_the_actual_protocol_era(tmp_path):
    from evals.rust_port.stdio_judge import StdioPolicy, judge_with_evidence
    from evals.rust_port.stdio_corpus import ERAS
    expected = {"case": "real-bank", "era": ERAS[0], "exit_code": 0,
                "stdout_frames_b64": [], "stderr_b64": ""}
    actual = {**expected, "exit_code": 1}
    # Match Phase 1 corpus()'s emitted shape: it retains era, not case.
    differences = [{"era": expected["era"], **row} for row in judge_with_evidence(expected, actual, StdioPolicy())]
    assert differences and all("case" not in row for row in differences)
    receipt = tmp_path / "phase1.json"
    receipt.write_text(json.dumps({"differences": differences}))
    assert replay.case_ids("phase1-judges", receipt) == ([f"era:{ERAS[0]}"], [])


def test_goldens_and_diagnostic_probes_run_as_separate_steps(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    calls = []
    def execute(phase, *args, **kwargs):
        calls.append(phase)
        return {"phase": phase, "exit_code": int(phase == "cli-argv"), "seconds": 0,
                "failed_cases": ["argv-case"] if phase == "cli-argv" else [], "diagnostic_errors": []}
    monkeypatch.setattr(replay, "execute", execute)
    value = plan(judges=["cli-argv"])
    public = tmp_path / "public"
    assert replay.run(value, Path("candidate"), tmp_path / "golden-private", public / "goldens", only="goldens") == 0
    assert calls == ["golden/doctor"]
    assert replay.run(value, Path("candidate"), tmp_path / "judge-private", public / "judges", only="judges") == 1
    assert calls == ["golden/doctor", "oracle-setup", "lease-check", "cli-argv"]
    target = tmp_path / "job-summary"
    replay.publish(value, public, target)
    report = json.loads((public / "report.json").read_text())
    assert [group["gating"] for group in report["groups"]] == [True, False]
    markdown = target.read_text()
    assert "report only" in markdown and "cli-argv" in markdown and "argv-case" in markdown


def test_golden_failure_remains_fatal_in_the_required_group(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    monkeypatch.setattr(replay, "execute", lambda phase, *args, **kwargs: {
        "phase": phase, "exit_code": 3, "seconds": 0, "failed_cases": ["doctor/case"], "diagnostic_errors": []})
    assert replay.run(plan(judges=["cli-argv"]), Path("candidate"), tmp_path / "private",
                      tmp_path / "public", only="goldens") == 1
    summary = json.loads((tmp_path / "public/summary.json").read_text())
    assert [row["phase"] for row in summary["phases"]] == ["golden/doctor"]


def test_unavailable_probe_is_reported_without_private_details(tmp_path):
    value = plan(linux=[], windows=[], judges=["cli-argv"])
    target = tmp_path / "job-summary"
    replay.publish(value, tmp_path / "public", target)
    report = json.loads((tmp_path / "public/report.json").read_text())
    assert report["groups"][0]["gating"] is False
    assert report["groups"][0]["phases"][0]["exit_code"] is None
    assert "not run" in target.read_text()


def test_invalid_probe_records_are_not_presented_as_case_identifiers(tmp_path):
    public = tmp_path / "public"
    (public / "judges").mkdir(parents=True)
    (public / "judges/summary.json").write_text(json.dumps({"schema": 1, "phases": [
        {"phase": "cli-argv", "exit_code": "private-invalid-value", "seconds": 0,
         "failed_cases": "not-a-list", "diagnostic_errors": []}]}))
    target = tmp_path / "job-summary"
    replay.publish(plan(linux=[], windows=[], judges=["cli-argv"]), public, target)
    assert "private-invalid-value" not in target.read_text()
    assert "not run" in target.read_text()


def test_published_ids_are_exact_in_json_and_safe_in_markdown(tmp_path):
    public = tmp_path / "public"
    (public / "judges").mkdir(parents=True)
    case = "case|<id>`"
    (public / "judges/summary.json").write_text(json.dumps({"schema": 1, "phases": [
        {"phase": "cli-argv", "exit_code": 1, "seconds": 0, "failed_cases": [case],
         "diagnostic_errors": [], "raw": "private stream omitted"}]}))
    target = tmp_path / "job-summary"
    replay.publish(plan(linux=[], windows=[], judges=["cli-argv"]), public, target)
    report = (public / "report.json").read_text()
    assert json.loads(report)["groups"][0]["phases"][0]["failed_cases"] == [case]
    assert "private stream omitted" not in report
    assert "case\\|&lt;id&gt;&#96;" in target.read_text()
