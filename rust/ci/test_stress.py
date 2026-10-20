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
    tests = stress.command("pytest", ["tests/test_a.py"], candidates, tmp_path)
    assert tests[1:4] == ["-m", "pytest", "tests/test_a.py"] and "-ra" in tests and "--junitxml" in tests


@pytest.mark.parametrize("row, skip_bank", [
    # cli-live's "CLI differential harness live arms" step: bank cases skipped.
    ("mail", True), ("hook", True), ("episode", True),
    # The cli lease steps, W1-C and H5 live arms run the bank cases.
    ("lease", False), ("lease_operator", False), ("doctor", False), ("transfer", False),
    ("test_login", False), ("audit", False), ("backup", False),
])
def test_a_row_runs_as_its_required_live_arm_does(tmp_path, row, skip_bank):
    # No required live arm passes --fixture; a stress loop that did would
    # measure another configuration (reviewer, 2026-10-20).
    argv = stress.command("row", [row, "some-case"], tmp_path / "bin", tmp_path)
    assert argv[1:4] == ["rust/cli_harness", "--row", row] and argv[4:6] == ["--case", "some-case"]
    assert ("--skip-bank" in argv) == skip_bank
    assert "--fixture" not in argv and "--golden" not in argv and "--out" in argv


@pytest.mark.parametrize("scenario, binary, fixture, http", [
    # cli-live's steps: the default static build, http slice, no fixture.
    ("security-open", "pseudolife-daemon-static", False, True),
    ("body-limits", "pseudolife-daemon-static", False, True),
    ("static-build", "pseudolife-daemon-static", False, True),
    # The H5 shards (rust/ci/run_coverage.py): the mutants build with --fixture.
    ("tokenless", "pseudolife-daemon", True, False),
    ("session-reap", "pseudolife-daemon", True, False),
])
def test_a_scenario_runs_on_its_required_lanes_binary(tmp_path, scenario, binary, fixture, http):
    argv = stress.command("scenario", [scenario], tmp_path / "bin", tmp_path)
    assert argv[1:3] == ["rust/daemon/harness/run.py", "live"]
    assert argv[argv.index("--only") + 1] == scenario
    assert Path(argv[argv.index("--rust-bin") + 1]).stem == binary
    assert ("--fixture" in argv) == fixture
    env = stress.environment("scenario", [scenario], tmp_path / "bin", tmp_path)
    assert (env.get("PL_HARNESS_SLICE") == "http") == http
    assert env["PL_HARNESS_SCRATCH"].startswith(str(tmp_path))


def test_pytest_selections_get_the_parity_steps_candidates(tmp_path):
    env = stress.environment("pytest", ["rust/daemon/harness/test_static.py"], tmp_path / "bin", tmp_path)
    for name, binary in (("PL_STATIC_DEFAULT_BIN", "pseudolife-daemon-static"),
                         ("PL_SECURITY_DEFAULT_BIN", "pseudolife-daemon-static"),
                         ("PL_SECURITY_RELEASE_MUTANTS_BIN", "pseudolife-daemon"),
                         ("PL_DREAM_CONTRACT_BINARY", "dream-contract"),
                         ("PSEUDOLIFE_SENT_YAML_BIN", "pseudolife-stdio"),
                         ("PSEUDOLIFE_RUST_BINARY", "pseudolife-stdio")):
        assert Path(env[name]).stem == binary and Path(env[name]).parent == tmp_path / "bin", name
    assert env["PSEUDOLIFE_BACKUP_NATIVE_REQUIRED"] == "1"


@pytest.mark.parametrize("scenario", sorted(stress.REMOTE_BIND))
def test_remote_bind_scenarios_are_refused_before_anything_starts(scenario):
    # They need the debug loopback-bind build, which only the rust job makes.
    with pytest.raises(ValueError, match="debug loopback-bind build"):
        stress.parse(f"scenario:{scenario}")


JUNIT = '<?xml version="1.0"?><testsuites><testsuite name="pytest" errors="0" failures="0" skipped="{skipped}" tests="{tests}"></testsuite></testsuites>'


@pytest.mark.parametrize("tests, skipped, note", [
    (2, 0, ""),
    # Any skip, not only all of them: a skipped live test beside passing unit
    # tests would pass 20 of 20 without its timing-sensitive part (reviewer).
    (2, 1, "1 of 2 selected tests skipped"),
    (2, 2, "2 of 2 selected tests skipped"),
    (0, 0, "no selected test was collected"),
])
def test_a_pytest_iteration_needs_every_selected_test_to_run(tmp_path, tests, skipped, note):
    (tmp_path / "junit.xml").write_text(JUNIT.format(tests=tests, skipped=skipped), encoding="utf-8")
    assert stress.evidence("pytest", ["tests/test_a.py"], tmp_path).startswith(note)
    assert bool(stress.evidence("pytest", ["tests/test_a.py"], tmp_path)) == bool(note)


def test_a_missing_junit_report_is_no_evidence(tmp_path):
    assert stress.evidence("pytest", ["tests/test_a.py"], tmp_path) == "no selected test was collected"


@pytest.mark.parametrize("summary, ok", [
    ({"complete": True, "rows": {"hook": {"cases": 3}}}, True),
    # Every case filtered out on this platform: the runner prints 0/0 and exits 0.
    ({"complete": True, "rows": {"hook": {"cases": 0}}}, False),
    ({"complete": False, "rows": {"hook": {"cases": 3}}}, False),
    (None, False),
])
def test_a_row_iteration_needs_a_compared_case(tmp_path, summary, ok):
    if summary is not None:
        (tmp_path / "harness.json").write_text(json.dumps(summary), encoding="utf-8")
    assert (stress.evidence("row", ["hook"], tmp_path) == "") == ok


@pytest.mark.parametrize("summary, ok", [
    ({"scenarios": [{"cases": [{}, {}]}], "refusals": []}, True),
    ({"scenarios": [{"cases": []}], "refusals": [{}]}, True),
    ({"scenarios": [{"cases": []}], "refusals": []}, False),
])
def test_a_scenario_iteration_needs_a_compared_case(tmp_path, summary, ok):
    (tmp_path / "harness.json").write_text(json.dumps(summary), encoding="utf-8")
    assert (stress.evidence("scenario", ["tokenless"], tmp_path) == "") == ok


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
    assert row["outcome"] == "fail" and "skipped" in row["note"]


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

    def fake(argv, env):
        calls.append((argv, env))
        Path(argv[argv.index("--out") + 1]).write_text(
            json.dumps({"complete": True, "rows": {"hook": {"cases": 2}}}), encoding="utf-8")
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(stress.subprocess, "run", fake)
    monkeypatch.setenv("ImageOS", "win25")
    monkeypatch.setenv("ImageVersion", "20261012.1.0")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    out = tmp_path / "stress" / "stress-row-4.json"
    assert stress.main(["run", "--selection", "row:hook", "--iteration", "4",
                        "--candidates", str(tmp_path / "bin"), "--out", str(out)]) == code
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["iteration"] == 4 and row["outcome"] == outcome and row["exit_code"] == code
    assert row["attempt"] == 2
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


def write_row(directory, iteration, outcome, attempt=1):
    path = directory / f"stress-row-{iteration}" / f"stress-row-{iteration}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"iteration": iteration, "attempt": attempt, "outcome": outcome,
                                "exit_code": 0 if outcome == "pass" else 1,
                                "seconds": 12.5, "runner": {"image": "win25 1", "cpus": 4}}), encoding="utf-8")


@pytest.mark.parametrize("outcomes, attempts, accepted", [
    (["pass", "pass", "pass"], [1, 1, 1], True),
    (["pass", "fail", "pass"], [1, 1, 1], False),
    (["pass", None, "pass"], [1, 1, 1], False),  # an iteration that never wrote its row
    # "Re-run all jobs": one fresh attempt throughout, still evidence.
    (["pass", "pass", "pass"], [2, 2, 2], True),
    # "Re-run failed jobs": a retry replaced iteration 2's failure (reviewer).
    (["pass", "pass", "pass"], [1, 2, 1], False),
])
def test_the_report_has_one_row_per_iteration_and_passes_only_when_all_pass_at_once(
        tmp_path, outcomes, attempts, accepted):
    rows = tmp_path / "rows"
    rows.mkdir()
    for index, (outcome, attempt) in enumerate(zip(outcomes, attempts), 1):
        if outcome:
            write_row(rows, index, outcome, attempt)
    out, summary = tmp_path / "stress-report.json", tmp_path / "summary.md"
    code = stress.main(["report", "--selection", "row:hook", "--iterations", "3", "--os", "windows-latest",
                        "--rows", str(rows), "--out", str(out), "--summary", str(summary)])
    assert (code == 0) == accepted
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["clean"] == accepted
    assert [row["iteration"] for row in report["rows"]] == [1, 2, 3]
    assert [row["outcome"] for row in report["rows"]] == [o or "no result" for o in outcomes]
    assert report["passed"] == outcomes.count("pass")
    text = summary.read_text(encoding="utf-8")
    assert f"{outcomes.count('pass')} of 3 iterations passed." in text
    assert ("Not clean" in text) == (len(set(attempts)) > 1)


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
    # The event oracle binding accepts only master dispatches with frozen_head,
    # so a loop on a branch must not stop there (Codex review of e56fe0f6);
    # but oracle-dependent pytest selections need it when it can bind (Codex
    # review of 028c3ebb), so it runs best-effort before the selection.
    binds = [step for step in loop["steps"] if "evals.rust_port.oracle_selection" in step.get("run", "")]
    assert len(binds) == 1 and binds[0]["continue-on-error"] is True
    assert binds[0]["env"] == {"GITHUB_REF": "${{ github.ref }}"}
    names = [step.get("name") for step in loop["steps"]]
    assert names.index(binds[0]["name"]) < names.index("Run the selection once")
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
