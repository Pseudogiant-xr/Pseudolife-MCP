"""The precheck retains IDs and continues independent failures."""
import json
import os
import subprocess
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
    value = plan(linux=["doctor", "mail"], windows=["doctor", "mail"], judges=["lease-headers", "cli-argv"])
    assert replay.run(value, Path("candidate"), tmp_path / "private", tmp_path / "public") == 1
    assert calls == ["golden/doctor", "golden/mail", "oracle-setup", "lease-check", "lease-headers", "cli-argv"]
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
    def postgres(private, candidate):
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
    assert phases["golden-postgres"]["diagnostic_errors"] == ["owned PostgreSQL setup failed (RuntimeError)"]
    assert "private connection detail" not in text


@pytest.mark.parametrize("row", ["lease", "lease_operator", "transfer", "maintainer", "invite", "test_login"])
def test_bank_rows_prepare_owned_pg_before_their_existing_cases(tmp_path, monkeypatch, row):
    mock_summary_replace(monkeypatch)
    events = []
    commands = {}
    @contextmanager
    def postgres(private, candidate):
        events.append("pg-open")
        assert candidate == Path("candidate")
        yield {"owned": "fixture"}
        events.append("pg-close")
    def execute(phase, command, *args, env=None, **kwargs):
        events.append(phase)
        commands[phase] = command
        if phase == f"golden/{row}":
            assert env == {"owned": "fixture"}
        return {"phase": phase, "exit_code": 0, "seconds": 0, "failed_cases": [], "diagnostic_errors": []}
    monkeypatch.setattr(replay, "golden_postgres", postgres)
    monkeypatch.setattr(replay, "execute", execute)
    value = plan(linux=[row], windows=[row])
    assert replay.run(value, Path("candidate"), tmp_path / "private", tmp_path / "public") == 0
    assert events == ["golden-lease-check", "pg-open", f"golden/{row}", "pg-close"]
    # The suite-lease check is the candidate's own native command now.
    assert commands["golden-lease-check"] == ["candidate", "lease", "check", "full-suite"]


class _Server:
    host, port, password = "127.0.0.1", 55432, "synthetic-secret"
    data_dir = Path("pl-precheck-postgres-fixture")

    def __init__(self, events, stops=True):
        self.events, self.stops = events, stops

    def stop(self):
        self.events.append("stop")
        if isinstance(self.stops, BaseException):
            raise self.stops
        return self.stops


def test_owned_golden_fixture_does_not_inherit_a_configured_bank(tmp_path, monkeypatch):
    """The cluster comes from disposable_postgres and the login from the
    candidate's native test-login; the daemon's bank never reaches the rows."""
    import disposable_postgres
    configured = "postgresql://fixture.invalid/configured-bank"
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", configured)
    events = []
    def start(parent, prefix):
        events.append(("start", parent, prefix))
        return _Server(events)
    def create_test_login(candidate, server, login):
        events.append(("login", candidate, login))
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(disposable_postgres, "start", start)
    monkeypatch.setattr(disposable_postgres, "create_test_login", create_test_login)
    with replay.golden_postgres(tmp_path, Path("candidate")) as env:
        assert "PSEUDOLIFE_MCP_DATABASE_URL" not in env
        assert env["PSEUDOLIFE_TEST_PG_HOST_PORT"] == "127.0.0.1:55432"
        assert env["PSEUDOLIFE_TEST_PG_LOGIN_FILE"] == str(tmp_path / "test-pg.env")
    assert events == [("start", Path.home(), "pl-precheck-postgres-"),
                      ("login", Path("candidate"), tmp_path / "test-pg.env"), "stop"]


def test_owned_golden_fixture_stops_its_cluster_when_the_login_fails(tmp_path, monkeypatch):
    import disposable_postgres
    events = []
    monkeypatch.setattr(disposable_postgres, "start", lambda parent, prefix: _Server(events))
    monkeypatch.setattr(disposable_postgres, "create_test_login",
                        lambda candidate, server, login: subprocess.CompletedProcess([], 3, "", ""))
    with pytest.raises(RuntimeError, match="test-role provisioning failed"):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    assert events == ["stop"]


def test_owned_golden_fixture_fails_its_cleanup_when_the_stop_is_unconfirmed(tmp_path, monkeypatch, capsys):
    """A cluster that could not be confirmed stopped fails the cleanup phase
    instead of passing it; a stop that fails during a setup error is
    reported beside that error, which stays the one raised."""
    import disposable_postgres
    events = []
    monkeypatch.setattr(disposable_postgres, "start", lambda parent, prefix: _Server(events, stops=False))
    monkeypatch.setattr(disposable_postgres, "create_test_login",
                        lambda candidate, server, login: subprocess.CompletedProcess([], 0, "", ""))
    with pytest.raises(RuntimeError, match="could not be confirmed stopped"):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    # run() publishes the type only, so the directory to stop is in the step log.
    first = capsys.readouterr().err
    assert "left running" in first and "pl-precheck-postgres-fixture" in first
    monkeypatch.setattr(disposable_postgres, "create_test_login",
                        lambda candidate, server, login: subprocess.CompletedProcess([], 3, '{"exit": 3, "error": "refused"}', ""))
    with pytest.raises(RuntimeError, match="test-role provisioning failed"):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    err = capsys.readouterr().err
    assert "test-login create failed (exit 3): refused" in err and "left running" in err


def test_a_stop_that_raises_keeps_the_setup_error(tmp_path, monkeypatch, capsys):
    import disposable_postgres
    events = []
    monkeypatch.setattr(disposable_postgres, "start",
                        lambda parent, prefix: _Server(events, stops=PermissionError("in use")))
    monkeypatch.setattr(disposable_postgres, "create_test_login",
                        lambda candidate, server, login: subprocess.CompletedProcess([], 3, "", ""))
    with pytest.raises(RuntimeError, match="test-role provisioning failed"):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    err = capsys.readouterr().err
    assert events == ["stop"] and "cleanup failed (PermissionError)" in err and "left running" in err


def test_a_refused_start_names_its_reason_in_the_step_log(tmp_path, monkeypatch, capsys):
    import disposable_postgres
    def start(parent, prefix):
        raise disposable_postgres.SetupError("pg0 reported a server on another host; install the pinned pg0")
    monkeypatch.setattr(disposable_postgres, "start", start)
    with pytest.raises(disposable_postgres.SetupError):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    assert "did not start: pg0 reported a server on another host; install the pinned pg0" in capsys.readouterr().err


def test_a_failed_login_without_a_report_never_logs_the_password(tmp_path, monkeypatch, capsys):
    """The child had the password in PGPASSWORD; its raw stderr is redacted."""
    import disposable_postgres
    events = []
    monkeypatch.setattr(disposable_postgres, "start", lambda parent, prefix: _Server(events))
    monkeypatch.setattr(disposable_postgres, "create_test_login", lambda candidate, server, login:
                        subprocess.CompletedProcess([], 101, "", "panicked: PGPASSWORD=synthetic-secret"))
    with pytest.raises(RuntimeError, match="test-role provisioning failed"):
        with replay.golden_postgres(tmp_path, Path("candidate")):
            pass
    err = capsys.readouterr().err
    assert "synthetic-secret" not in err and "test-login create failed (exit 101): panicked: PGPASSWORD=***" in err


def test_an_unconfirmed_stop_after_the_rows_fails_the_cleanup_phase(tmp_path, monkeypatch):
    mock_summary_replace(monkeypatch)
    @contextmanager
    def postgres(private, candidate):
        yield {"owned": "fixture"}
        raise RuntimeError("owned golden PostgreSQL in somewhere could not be confirmed stopped")
    monkeypatch.setattr(replay, "golden_postgres", postgres)
    monkeypatch.setattr(replay, "execute", lambda phase, *args, **kwargs: {
        "phase": phase, "exit_code": 0, "seconds": 0, "failed_cases": [], "diagnostic_errors": []})
    assert replay.run(plan(linux=["lease"], windows=["lease"]), Path("candidate"),
                      tmp_path / "private", tmp_path / "public") == 1
    text = (tmp_path / "public/summary.json").read_text()
    phases = {row["phase"]: row for row in json.loads(text)["phases"]}
    assert phases["golden-postgres"]["exit_code"] == 0 and phases["golden/lease"]["exit_code"] == 0
    assert phases["golden-postgres-cleanup"]["exit_code"] == 1
    assert phases["golden-postgres-cleanup"]["diagnostic_errors"] == ["owned PostgreSQL scope failed (RuntimeError)"]
    assert "somewhere" not in text


def test_precheck_runner_imports_nothing_from_the_package_outside_the_frozen_judges():
    """The golden PostgreSQL and lease check are package-free (a recorded
    row's record/ phase still runs the frozen package through
    rust/cli_harness); PROCESS_BOOTSTRAP's import is a string the frozen
    merge-base oracle's own runtime runs (--only judges)."""
    import ast
    source = Path(replay.__file__).read_text(encoding="utf-8")
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = [alias.name for node in imports if isinstance(node, ast.Import) for alias in node.names]
    names += [node.module or "" for node in imports if isinstance(node, ast.ImportFrom)]
    assert not [name for name in names if name.split(".")[0] == "pseudolife_memory"]
    assert "pseudolife_memory" not in source.replace(replay.PROCESS_BOOTSTRAP, "")

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


def test_each_job_publishes_only_the_group_it_ran(tmp_path, monkeypatch):
    # The Rust job replays goldens and a job of its own runs the frozen
    # probes: neither may report the other's group as "not run".
    mock_summary_replace(monkeypatch)
    monkeypatch.setattr(replay, "execute", lambda phase, *args, **kwargs: {
        "phase": phase, "exit_code": 0, "seconds": 0, "failed_cases": [], "diagnostic_errors": []})
    value = plan(judges=["cli-argv"])
    for only in ("goldens", "judges"):
        public = tmp_path / only / "public"
        assert replay.run(value, Path("candidate"), tmp_path / only / "private", public / only, only=only) == 0
        target = tmp_path / only / "job-summary"
        assert replay.main(["--plan-json", json.dumps(value), "--publish", "--only", only,
                            "--public", str(public), "--job-summary", str(target)]) == 0
        report = json.loads((public / "report.json").read_text())
        assert [group["name"] for group in report["groups"]] == [only]
        markdown = target.read_text()
        assert "not run" not in markdown and "did not produce" not in json.dumps(report)
        assert ("report only" in markdown) == (only == "judges")
        assert ("Golden replay" in markdown) == (only == "goldens")


def test_a_job_with_nothing_selected_writes_no_summary(tmp_path):
    target = tmp_path / "job-summary"
    replay.publish(plan(linux=[], windows=[], judges=["cli-argv"]), tmp_path / "public", target, only="goldens")
    assert json.loads((tmp_path / "public/report.json").read_text())["groups"] == []
    assert not target.exists()


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


def test_the_frozen_judges_are_recorded_as_retiring_with_the_package():
    """package_consumers.toml classes the runner's one remaining package
    import (the judges' PROCESS_BOOTSTRAP) as tooling that retires with it."""
    import fnmatch
    import tomllib
    rules = tomllib.loads((replay.ROOT / "rust/ci/package_consumers.toml").read_text(encoding="utf-8"))["rules"]
    rule = next(rule for rule in rules if "python-import" in rule["kinds"]
                and any(fnmatch.fnmatchcase("rust/ci/pr_precheck_run.py", glob) for glob in rule["globs"]))
    assert rule["class"] == "retained test tooling" and rule.get("retire_with_package") is True
