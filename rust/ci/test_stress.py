"""rust.yml's stress mode: one selection on N fresh hosted runners, one row each."""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import stress

ROOT = Path(__file__).resolve().parents[2]
CI_ONLY = "(github.event_name != 'workflow_dispatch' || inputs.mode == 'ci')"
NOT_CAPTURE = "(github.event_name != 'workflow_dispatch' || inputs.mode != 'wait-mail')"


def raw_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("selection, parsed", [
    ("row:hook", ("row", ["hook"])),
    (" row:hook/briefing-https-hook-json ", ("row", ["hook", "briefing-https-hook-json"])),
    ("scenario:security-open", ("scenario", ["security-open"])),
    ("tests/test_wait_mail_cli.py::test_ring_wakes", ("pytest", ["tests/test_wait_mail_cli.py::test_ring_wakes"])),
    ("tests/test_a.py::test_x[1-2] rust/daemon/harness/test_mcp_wire.py",
     ("pytest", ["tests/test_a.py::test_x[1-2]", "rust/daemon/harness/test_mcp_wire.py"])),
])
def test_a_selection_names_a_row_a_scenario_or_pytest_nodes(selection, parsed):
    assert stress.parse(selection) == parsed


@pytest.mark.parametrize("selection", [
    "", "row:", "row:Hook", "row:hook/", "scenario:", "scenario:a b",
    # Options and paths outside the tree are not a named selection.
    "-k wait", "tests/test_a.py -x", "../outside.py", "tests/test_a.py;rm", "tests/",
])
def test_anything_else_is_refused_with_the_accepted_forms(selection):
    with pytest.raises(ValueError, match="row:<row>|scenario:"):
        stress.parse(selection)


@pytest.mark.parametrize("value, ok", [("1", True), ("20", True), ("50", True), ("0", False), ("51", False)])
def test_iterations_are_bounded(value, ok):
    if ok:
        assert stress.iterations(value) == int(value)
    else:
        with pytest.raises(ValueError, match="choose 1 to 50"):
            stress.iterations(value)


def test_each_kind_runs_the_existing_harness(tmp_path):
    candidates = tmp_path / "bin"
    row = stress.command("row", ["episode", "bank-start-git"], candidates, tmp_path)
    assert row[1:4] == ["rust/cli_harness", "--row", "episode"] and row[4:6] == ["--case", "bank-start-git"]
    assert "--golden" not in row and "--skip-bank" not in row and "--out" in row
    # A fresh runner has no model cache: bank-backed cases need the offline
    # embedder fixture (Codex review of e56fe0f6).
    assert "--fixture" in row
    tests = stress.command("pytest", ["tests/test_a.py"], candidates, tmp_path)
    assert tests[1:4] == ["-m", "pytest", "tests/test_a.py"] and "-ra" in tests and "--junitxml" in tests


@pytest.mark.parametrize("scenario, binary, fixture", [
    # cli-live's steps: the default static build, no embedder fixture.
    ("security-open", "pseudolife-daemon-static", False),
    ("body-limits", "pseudolife-daemon-static", False),
    ("static-build", "pseudolife-daemon-static", False),
    # The H5 shards (rust/ci/run_coverage.py): the mutants build with --fixture.
    ("tokenless", "pseudolife-daemon", True),
    ("session-reap", "pseudolife-daemon", True),
])
def test_a_scenario_runs_on_its_required_lanes_binary(tmp_path, scenario, binary, fixture):
    argv = stress.command("scenario", [scenario], tmp_path / "bin", tmp_path)
    assert argv[1:3] == ["rust/daemon/harness/run.py", "live"]
    assert argv[argv.index("--only") + 1] == scenario
    assert Path(argv[argv.index("--rust-bin") + 1]).stem == binary
    assert ("--fixture" in argv) == fixture


@pytest.mark.parametrize("scenario", sorted(stress.REMOTE_BIND))
def test_remote_bind_scenarios_are_refused_before_anything_starts(scenario):
    # They need the debug loopback-bind build, which only the rust job makes.
    with pytest.raises(ValueError, match="debug loopback-bind build"):
        stress.parse(f"scenario:{scenario}")


JUNIT = '<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="0" skipped="{skipped}" tests="{tests}"></testsuite></testsuites>'


@pytest.mark.parametrize("tests, skipped, ran", [(2, 0, True), (2, 1, True), (2, 2, False), (0, 0, False)])
def test_a_report_counts_only_tests_that_ran(tmp_path, tests, skipped, ran):
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT.format(tests=tests, skipped=skipped), encoding="utf-8")
    assert stress.tests_ran(report) == ran
    assert not stress.tests_ran(tmp_path / "missing.xml")


def test_an_all_skipped_pytest_iteration_fails(tmp_path, monkeypatch):
    # pytest exits 0 when every selected test skips; that is no evidence
    # (Codex review of e56fe0f6: test_fork.py on Windows, 2 skipped, "pass").
    out = tmp_path / "stress" / "stress-row-1.json"

    def skipped(argv, env):
        Path(argv[argv.index("--junitxml") + 1]).write_text(JUNIT.format(tests=2, skipped=2), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(stress.subprocess, "run", skipped)
    assert stress.main(["run", "--selection", "rust/cli_harness/test_fork.py", "--iteration", "1",
                        "--candidates", str(tmp_path), "--out", str(out)]) == 1
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["outcome"] == "fail" and "no selected test ran" in row["note"]


def test_plan_lists_the_iterations(tmp_path):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", "row:hook", "--iterations", "3", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "iterations=[1, 2, 3]\n"


@pytest.mark.parametrize("selection, count", [("-k x", "20"), ("row:hook", "0")])
def test_plan_refuses_before_starting_anything(tmp_path, capsys, selection, count):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", selection, "--iterations", count,
                        "--github-output", str(output)]) == 2
    assert not output.exists()
    assert "nothing was started" in capsys.readouterr().err


@pytest.mark.parametrize("code, outcome", [(0, "pass"), (1, "fail"), (5, "fail")])
def test_run_writes_its_row_whatever_the_outcome(tmp_path, monkeypatch, code, outcome):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run",
                        lambda argv, env: calls.append((argv, env)) or SimpleNamespace(returncode=code))
    monkeypatch.setenv("ImageOS", "win25")
    monkeypatch.setenv("ImageVersion", "20261012.1.0")
    out = tmp_path / "stress" / "stress-row-4.json"
    assert stress.main(["run", "--selection", "row:hook", "--iteration", "4",
                        "--candidates", str(tmp_path / "bin"), "--out", str(out)]) == code
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["iteration"] == 4 and row["outcome"] == outcome and row["exit_code"] == code
    assert row["runner"]["image"] == "win25 20261012.1.0" and row["seconds"] >= 0
    assert calls[0][1]["PSEUDOLIFE_RUST_BINARY"].startswith(str(tmp_path / "bin"))


def test_run_writes_a_failed_row_when_the_command_cannot_start(tmp_path, monkeypatch):
    def missing(argv, env):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(stress.subprocess, "run", missing)
    out = tmp_path / "stress-row-1.json"
    with pytest.raises(FileNotFoundError):
        stress.main(["run", "--selection", "scenario:security-open", "--iteration", "1",
                     "--candidates", str(tmp_path), "--out", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["outcome"] == "fail"


def write_row(directory, iteration, outcome):
    path = directory / f"stress-row-{iteration}" / f"stress-row-{iteration}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"iteration": iteration, "outcome": outcome, "exit_code": 0 if outcome == "pass" else 1,
                                "seconds": 12.5, "runner": {"image": "win25 1", "cpus": 4}}), encoding="utf-8")


@pytest.mark.parametrize("outcomes, accepted", [
    (["pass", "pass", "pass"], True),
    (["pass", "fail", "pass"], False),
    (["pass", None, "pass"], False),  # an iteration that never wrote its row
])
def test_the_report_has_one_row_per_iteration_and_passes_only_when_all_pass(tmp_path, outcomes, accepted):
    rows = tmp_path / "rows"
    rows.mkdir()
    for index, outcome in enumerate(outcomes, 1):
        if outcome:
            write_row(rows, index, outcome)
    out, summary = tmp_path / "stress-report.json", tmp_path / "summary.md"
    code = stress.main(["report", "--selection", "row:hook", "--iterations", "3", "--os", "windows-latest",
                        "--rows", str(rows), "--out", str(out), "--summary", str(summary)])
    assert (code == 0) == accepted
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [row["iteration"] for row in report["rows"]] == [1, 2, 3]
    assert [row["outcome"] for row in report["rows"]] == [o or "no result" for o in outcomes]
    assert report["passed"] == outcomes.count("pass")
    assert f"{outcomes.count('pass')} of 3 iterations passed." in summary.read_text(encoding="utf-8")


def test_dispatch_inputs():
    inputs = raw_workflow().get("on", raw_workflow().get(True))["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["ci", "wait-mail", "stress"]
    assert inputs["selection"]["type"] == "string"
    assert inputs["iterations"] == {**inputs["iterations"], "type": "number", "default": 20}
    assert inputs["os"]["options"] == ["windows-latest", "ubuntu-latest"]
    assert inputs["os"]["default"] == "windows-latest"


def test_stress_jobs_run_only_in_stress_mode_and_reuse_the_candidate():
    jobs = raw_workflow()["jobs"]
    plan, loop, report = jobs["stress-plan"], jobs["stress"], jobs["stress-report"]
    assert plan["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'stress'"
    assert loop["needs"] == ["stress-plan", "candidate"]
    assert loop["if"] == ("${{ !cancelled() && needs.stress-plan.result == 'success' "
                          "&& needs.candidate.result == 'success' }}")
    assert loop["strategy"] == {"fail-fast": False, "max-parallel": 10,
                                "matrix": {"iteration": "${{ fromJSON(needs.stress-plan.outputs.iterations) }}"}}
    assert loop["runs-on"] == "${{ inputs.os }}"
    assert loop["name"] == "Stress / ${{ inputs.os }} / ${{ matrix.iteration }}"
    download = next(step for step in loop["steps"] if step.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    assert not any("cargo" in step.get("run", "") for step in loop["steps"])
    # The event oracle binding accepts only master dispatches; a stress loop
    # runs on any branch against its own checkout (Codex review of e56fe0f6).
    assert not any("oracle_selection" in step.get("run", "") for step in loop["steps"])
    assert report["needs"] == ["stress-plan", "stress"]
    assert report["if"] == "${{ always() && needs.stress-plan.result == 'success' }}"
    upload = next(step for step in report["steps"] if step.get("name") == "Retain the stress report")
    assert upload["if"] == "always()" and upload["with"]["path"] == "stress-report.json"
    # The candidate build (and scope, which it needs) runs for a stress loop.
    for name in ("scope", "candidate"):
        assert NOT_CAPTURE.strip("()") in jobs[name]["if"], name


def test_a_stress_loop_runs_no_ordinary_ci_job():
    jobs = raw_workflow()["jobs"]
    for name in ("rust", "daemon-image", "parity-checks", "parity"):
        assert CI_ONLY in jobs[name]["if"] and "wait-mail" not in jobs[name]["if"], name
    for name in ("parity-live", "parity-selfhosted", "source-mutants"):
        assert "inputs.mode == 'ci'" in jobs[name]["if"] and "pull_request" not in jobs[name]["if"], name


def test_inputs_reach_scripts_only_through_env():
    # A dispatcher-supplied string inlined into a script would run as code.
    jobs = raw_workflow()["jobs"]
    for name, job in jobs.items():
        for step in job.get("steps", []):
            assert not re.search(r"\$\{\{\s*inputs\.", step.get("run", "")), (name, step.get("name"))


def test_stress_loops_get_a_concurrency_group_per_run():
    from test_workflow import concurrency_group
    first = concurrency_group("workflow_dispatch", mode="stress", run_id="7")
    assert first != concurrency_group("workflow_dispatch", mode="stress", run_id="8")
    assert first not in (concurrency_group("workflow_dispatch", mode="ci"), concurrency_group("push"),
                         concurrency_group("schedule"))
