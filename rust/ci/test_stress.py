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


@pytest.mark.parametrize("selection, parsed", [
    ("cargo:pseudolife-stdio/board_retry_contract", ("cargo", ["pseudolife-stdio/board_retry_contract"])),
    (" cargo:pseudolife-stdio/board_retry_contract::board_identity_check_rides_out_one_failure ",
     ("cargo", ["pseudolife-stdio/board_retry_contract::board_identity_check_rides_out_one_failure"])),
    # A test inside a module keeps its libtest path.
    ("cargo:pseudolife-daemon/http_contract::tests::Inner_case",
     ("cargo", ["pseudolife-daemon/http_contract::tests::Inner_case"])),
    # #844's four binaries in one loop.
    ("cargo:pseudolife-stdio/board_retry_contract cargo:pseudolife-stdio/board_codex_recovery",
     ("cargo", ["pseudolife-stdio/board_retry_contract", "pseudolife-stdio/board_codex_recovery"])),
])
def test_a_selection_can_name_cargo_integration_test_binaries(selection, parsed):
    assert stress.parse(selection) == parsed


@pytest.mark.parametrize("selection", [
    "cargo:", "cargo:pseudolife-stdio", "cargo:pseudolife-stdio/", "cargo:/board_retry_contract",
    "cargo:Pseudolife/board", "cargo:pseudolife-stdio/board::", "cargo:pseudolife-stdio/board::a::",
    "cargo:pseudolife-stdio/board::a-b", "cargo:pseudolife-stdio/board::a;rm",
    "cargo:pseudolife-stdio/board -E all()", "cargo:pseudolife-stdio/../board",
    # One kind per loop: the build and the evidence differ by kind.
    "cargo:pseudolife-stdio/board tests/test_a.py", "tests/test_a.py cargo:pseudolife-stdio/board",
])
def test_a_malformed_or_mixed_cargo_selection_is_refused(selection):
    with pytest.raises(ValueError, match=r"cargo:<package>/<test binary>\[::<test>\]"):
        stress.parse(selection)


def test_cargo_items_become_one_nextest_filterset():
    names = ["pseudolife-stdio/board_retry_contract::a::b", "pseudolife-stdio/board_codex_recovery"]
    assert stress.filterset(names) == ("(binary_id(=pseudolife-stdio::board_retry_contract) & test(=a::b))"
                                       " | (binary_id(=pseudolife-stdio::board_codex_recovery))")
    # The archive holds whole binaries; test() is a run-time choice.
    assert stress.filterset(names, binaries_only=True) == (
        "binary_id(=pseudolife-stdio::board_retry_contract) | binary_id(=pseudolife-stdio::board_codex_recovery)")


def test_the_build_archives_only_the_selected_binaries_from_the_rust_workspace(tmp_path):
    archive = tmp_path / "stress-tests.tar.zst"
    argv = stress.archive_command(["pseudolife-stdio/board_retry_contract::x",
                                   "pseudolife-stdio/board_codex_recovery"], archive)
    # The Rust job's profile and lockfile: `cargo nextest` from rust/, so
    # rust-toolchain.toml pins the compiler, --locked, the test profile.
    assert argv[:4] == ["cargo", "nextest", "archive", "--locked"]
    assert argv[argv.index("--archive-file") + 1] == str(archive)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-p"] == ["pseudolife-stdio"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--test"] == ["board_retry_contract",
                                                                          "board_codex_recovery"]
    assert argv[argv.index("-E") + 1] == stress.filterset(
        ["pseudolife-stdio/board_retry_contract::x", "pseudolife-stdio/board_codex_recovery"], binaries_only=True)
    assert "--release" not in argv and "--no-default-features" not in argv


def test_archive_runs_cargo_in_the_rust_workspace(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run",
                        lambda argv, cwd: calls.append((argv, cwd)) or SimpleNamespace(returncode=0))
    archive = tmp_path / "a.tar.zst"
    assert stress.main(["archive", "--selection", "cargo:pseudolife-stdio/board_retry_contract",
                        "--archive-file", str(archive)]) == 0
    assert calls[0][1] == "rust" and calls[0][0][:3] == ["cargo", "nextest", "archive"]


def test_archive_refuses_a_selection_that_is_not_cargo(tmp_path, capsys):
    assert stress.main(["archive", "--selection", "row:hook", "--archive-file", str(tmp_path / "a")]) == 2
    assert "only cargo: selections" in capsys.readouterr().err


def test_a_cargo_iteration_runs_the_archive_as_the_rust_job_runs_nextest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "stress-tests.tar.zst"
    names = ["pseudolife-stdio/board_retry_contract"]
    argv = stress.command("cargo", names, tmp_path / "bin", tmp_path / "scratch", archive)
    assert argv[:3] == ["cargo-nextest", "nextest", "run"]
    assert argv[argv.index("--archive-file") + 1] == str(archive)
    # Extracted where the build put its target dir, so the env!() paths the
    # tests compiled in (CARGO_BIN_EXE_pseudolife-stdio) still resolve.
    workspace = str((tmp_path / "rust").resolve())
    assert argv[argv.index("--workspace-remap") + 1] == workspace
    assert argv[argv.index("--extract-to") + 1] == workspace and "--extract-overwrite" in argv
    assert argv[argv.index("--profile") + 1] == "stress"
    # A retry would turn a flake into a pass.
    assert argv[argv.index("--retries") + 1] == "0" and "--no-fail-fast" in argv
    assert argv[argv.index("-E") + 1] == stress.filterset(names)


def test_a_cargo_iteration_needs_its_archive(tmp_path):
    with pytest.raises(ValueError, match="--archive-file"):
        stress.command("cargo", ["pseudolife-stdio/board"], tmp_path, tmp_path, None)


def test_a_cargo_iteration_gets_the_rust_jobs_environment_not_the_parity_candidates(tmp_path):
    env = stress.environment("cargo", ["pseudolife-stdio/board"], tmp_path / "bin", tmp_path)
    # The Rust job binds the selected interpreter for the Python fixtures.
    assert env == {"PSEUDOLIFE_MCP_PYTHON": sys.executable}


def test_the_stress_nextest_profile_reports_every_skip_and_never_retries():
    import tomllib
    config = tomllib.loads((ROOT / "rust/.config/nextest.toml").read_text(encoding="utf-8"))
    profile = config["profile"]["stress"]
    assert profile["retries"] == 0 and profile["fail-fast"] is False
    assert profile["junit"] == {"path": "junit.xml", "report-skipped": "all"}
    # The stress profile inherits the default's slow-timeout, as it must.
    assert "slow-timeout" not in profile and "slow-timeout" in config["profile"]["default"]
    assert stress.cargo_junit(Path("rust")) == Path("rust/target/nextest/stress/junit.xml")


def nextest_junit(suites):
    """A nextest JUnit report: {binary id: [(test, child element or "")]}."""
    body = "".join(
        f'<testsuite name="{suite}" tests="{len(cases)}">'
        + "".join(f'<testcase name="{name}" classname="{suite}">{child}</testcase>' for name, child in cases)
        + "</testsuite>" for suite, cases in suites.items())
    return f'<?xml version="1.0"?><testsuites name="nextest-run">{body}</testsuites>'


FILTERED = '<skipped message="Skipped: test does not match the provided expression filters"/>'
IGNORED = '<skipped message="Skipped: test does not match the run-ignored option"/>'
RETRY = "board_retry_contract"


@pytest.mark.parametrize("names, suites, note", [
    (["pseudolife-stdio/board_retry_contract"],
     {f"pseudolife-stdio::{RETRY}": [("a", ""), ("m::b", "")]}, ""),
    # A named test: the rest of its binary is filtered out, which is not a skip of the selection.
    (["pseudolife-stdio/board_retry_contract::m::b"],
     {f"pseudolife-stdio::{RETRY}": [("a", FILTERED), ("c", IGNORED), ("m::b", "")]}, ""),
    (["pseudolife-stdio/board_retry_contract"],
     {f"pseudolife-stdio::{RETRY}": [("a", ""), ("c", IGNORED)]}, "1 of 2 selected tests skipped"),
    (["pseudolife-stdio/board_retry_contract::c"],
     {f"pseudolife-stdio::{RETRY}": [("a", FILTERED), ("c", IGNORED)]}, "1 of 1 selected tests skipped"),
    (["pseudolife-stdio/board_retry_contract"],
     {f"pseudolife-stdio::{RETRY}": [("a", '<failure message="x"/>')]}, "1 of 1 selected tests did not pass"),
    (["pseudolife-stdio/board_retry_contract"],
     {f"pseudolife-stdio::{RETRY}": [("a", '<flakyFailure message="x"/>')]}, "1 of 1 selected tests did not pass"),
    # A wrong binary or test name must not pass as "nothing failed".
    (["pseudolife-stdio/board_retry_contract", "pseudolife-stdio/board_codex_recovery"],
     {f"pseudolife-stdio::{RETRY}": [("a", "")]}, "no test ran for pseudolife-stdio/board_codex_recovery"),
    (["pseudolife-stdio/board_retry_contract::missing"],
     {f"pseudolife-stdio::{RETRY}": [("a", FILTERED)]}, "no test ran for pseudolife-stdio/board_retry_contract::missing"),
    (["pseudolife-stdio/board_retry_contract"], {}, "no test ran for pseudolife-stdio/board_retry_contract"),
])
def test_a_cargo_iteration_needs_every_selected_test_to_run_and_pass(tmp_path, names, suites, note):
    (tmp_path / "junit.xml").write_text(nextest_junit(suites), encoding="utf-8")
    found = stress.evidence("cargo", names, tmp_path)
    assert found.startswith(note) and bool(found) == bool(note), found


def test_a_cargo_iteration_without_a_junit_report_is_no_evidence(tmp_path):
    assert stress.evidence("cargo", ["pseudolife-stdio/board"], tmp_path) == "no JUnit report from nextest"


def test_a_cargo_run_keeps_this_iterations_junit_report(tmp_path, monkeypatch):
    # The report lands under the remapped workspace's target dir; a stale one
    # from an earlier run must never count, and the row keeps a copy.
    monkeypatch.chdir(tmp_path)
    junit = stress.cargo_junit(Path("rust"))
    junit.parent.mkdir(parents=True)
    junit.write_text(nextest_junit({f"pseudolife-stdio::{RETRY}": [("stale", "")]}), encoding="utf-8")

    def fresh(argv, env):
        assert not junit.exists(), "a stale report survived into the run"
        junit.write_text(nextest_junit({f"pseudolife-stdio::{RETRY}": [("a", "")]}), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(stress.subprocess, "run", fresh)
    out = tmp_path / "stress" / "stress-row-1.json"
    assert stress.main(["run", "--selection", f"cargo:pseudolife-stdio/{RETRY}", "--iteration", "1",
                        "--candidates", "rust/target/release", "--archive-file", str(tmp_path / "a.tar.zst"),
                        "--out", str(out)]) == 0
    assert 'name="a"' in (out.parent / "junit.xml").read_text(encoding="utf-8")
    assert json.loads(out.read_text(encoding="utf-8"))["outcome"] == "pass"


def test_a_cargo_run_that_left_no_report_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, env: SimpleNamespace(returncode=0))
    out = tmp_path / "stress" / "stress-row-1.json"
    assert stress.main(["run", "--selection", f"cargo:pseudolife-stdio/{RETRY}", "--iteration", "1",
                        "--candidates", "rust/target/release", "--archive-file", str(tmp_path / "a.tar.zst"),
                        "--out", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["note"] == "no JUnit report from nextest"


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
    # The eval shard's episode executable nodes skip without it (batch #868
    # review): a JSON list holding the absolute stdio candidate path.
    episode = json.loads(env["PSEUDOLIFE_PORT_EPISODE_JSON"])
    assert len(episode) == 1 and Path(episode[0]).is_absolute()
    assert Path(episode[0]).stem == "pseudolife-stdio"


def test_the_episode_candidate_is_absolute_from_a_relative_candidates_dir(tmp_path, monkeypatch):
    # The workflow passes rust/target/release; the tests resolve it from
    # their own working directory, so the path must not stay relative.
    monkeypatch.chdir(tmp_path)
    env = stress.environment("pytest", ["evals/rust_port/test_episode_executable.py"],
                             Path("rust/target/release"), tmp_path)
    assert json.loads(env["PSEUDOLIFE_PORT_EPISODE_JSON"]) == [
        str((tmp_path / "rust/target/release" / Path(env["PSEUDOLIFE_RUST_BINARY"]).name).resolve())]


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


@pytest.mark.parametrize("selection, kind", [("row:hook", "row"), ("cargo:pseudolife-stdio/board", "cargo")])
def test_plan_lists_the_iterations_and_the_kind(tmp_path, selection, kind):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", selection, "--iterations", "3", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == f"iterations=[1, 2, 3]\nkind={kind}\n"


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
    assert plan["outputs"] == {"iterations": "${{ steps.plan.outputs.iterations }}",
                               "kind": "${{ steps.plan.outputs.kind }}"}
    assert loop["needs"] == ["stress-plan", "candidate", "stress-build"]
    assert loop["if"] == ("${{ !cancelled() && needs.stress-plan.result == 'success' "
                          "&& needs.candidate.result == 'success' "
                          "&& (needs.stress-plan.outputs.kind != 'cargo' || needs.stress-build.result == 'success') }}")
    assert loop["strategy"] == {"fail-fast": False, "max-parallel": 10,
                                "matrix": {"iteration": "${{ fromJSON(needs.stress-plan.outputs.iterations) }}"}}
    assert loop["runs-on"] == "${{ inputs.os }}"
    assert loop["name"] == "Stress / ${{ inputs.os }} / ${{ matrix.iteration }}"
    download = next(step for step in loop["steps"] if step.get("uses") == "actions/download-artifact@v4"
                    and step["with"]["name"].startswith("rust-shim-"))
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    # An iteration never compiles: a cargo selection runs stress-build's archive.
    for step in loop["steps"]:
        assert not re.search(r"\bcargo (build|test|nextest (archive|run))\b|\bcargo \+", step.get("run", "")), \
            step.get("name")
    # The event oracle binding accepts only master dispatches with frozen_head,
    # so a loop on a branch must not stop there (Codex review of e56fe0f6);
    # but oracle-dependent pytest selections need it when it can bind (Codex
    # review of 028c3ebb), so it runs best-effort before the selection.
    # test_login's container cases need the image the W1-C connection shard
    # builds on Linux (batch #868 review); hosted Windows runs Windows
    # containers only, so the row skips them there.
    image = next(step for step in loop["steps"] if step.get("name") == "Build the test_login container image")
    w1c = next(step for step in raw_workflow()["jobs"]["parity-checks"]["steps"]
               if step.get("name") == "Build the test_login container image")
    assert image["run"] == w1c["run"] == "docker build -t pseudolife-pg:18 -f ops/Dockerfile.pg ops"
    assert image["if"] == "runner.os == 'Linux' && contains(env.STRESS_SELECTION, 'test_login')"
    assert image["timeout-minutes"] == w1c["timeout-minutes"]
    names = [step.get("name") for step in loop["steps"]]
    assert names.index(image["name"]) < names.index("Run the selection once")
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


def step_named(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_a_cargo_loop_builds_its_test_binaries_once_as_the_rust_job_does():
    jobs = raw_workflow()["jobs"]
    build, rust = jobs["stress-build"], jobs["rust"]
    assert build["needs"] == "stress-plan"
    assert build["if"] == ("${{ !cancelled() && needs.stress-plan.result == 'success' "
                           "&& needs.stress-plan.outputs.kind == 'cargo' }}")
    # Same OS as the iterations: the archive's env!() paths are this job's.
    assert build["runs-on"] == "${{ inputs.os }}" and build["env"] == {"STRESS_SELECTION": "${{ inputs.selection }}"}
    toolchain = next(step for step in build["steps"] if step.get("uses", "").startswith("dtolnay/rust-toolchain"))
    rust_toolchain = next(step for step in rust["steps"] if step.get("uses", "").startswith("dtolnay/rust-toolchain"))
    assert toolchain["with"]["toolchain"] == rust_toolchain["with"]["toolchain"]
    for name in ("Keep only the pinned Rust toolchain", "Install pinned nextest in the runner temporary directory"):
        # Nextest must be the same version where the archive is built and run.
        assert step_named(build, name)["run"] == step_named(rust, name)["run"], name
    cache = next(step for step in build["steps"] if step.get("uses") == "Swatinem/rust-cache@v2")
    assert cache["with"] == {"workspaces": "rust", "shared-key": "stress-tests", "cache-on-failure": True}
    archive = step_named(build, "Build the selected test binaries into a nextest archive")
    assert "rust/ci/stress.py archive --selection $env:STRESS_SELECTION" in archive["run"]
    assert "stress-tests.tar.zst" in archive["run"]
    upload = next(step for step in build["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"] == {"name": "stress-tests-${{ runner.os }}",
                              "path": "${{ runner.temp }}/stress-tests.tar.zst", "if-no-files-found": "error"}


def test_a_cargo_iteration_gets_the_rust_jobs_setup_and_not_the_parity_lanes():
    loop = raw_workflow()["jobs"]["stress"]
    assert loop["env"] == {"STRESS_SELECTION": "${{ inputs.selection }}",
                           "STRESS_KIND": "${{ needs.stress-plan.outputs.kind }}"}
    # The Rust job runs nextest with no PostgreSQL and only the Python SDK
    # fixture dependency; a cargo iteration matches it.
    for name in ("Install checkout and test dependencies", "Prepare disposable PostgreSQL"):
        assert step_named(loop, name)["if"] == "env.STRESS_KIND != 'cargo'", name
    rust = raw_workflow()["jobs"]["rust"]
    # The Rust job also exports the selected oracle's package metadata, which
    # cli_version_contract reads (Codex review of 50ca4be5). It binds only
    # where the event oracle does, so both steps stay best-effort here: on a
    # branch the test that needs it fails rather than passing.
    bind = step_named(loop, "Bind the event-selected Python oracle when the dispatch has one")
    assert "if" not in bind and bind["continue-on-error"] is True
    metadata = step_named(loop, "Prepare selected package metadata for the Rust identity contract")
    assert metadata["if"] == "env.STRESS_KIND == 'cargo'" and metadata["continue-on-error"] is True
    assert metadata["run"] == step_named(rust, "Prepare selected package metadata for the Rust identity contract")["run"]
    names = [step.get("name") for step in loop["steps"]]
    assert names.index(bind["name"]) < names.index(metadata["name"]) < names.index("Run the selection once")
    fixture = step_named(loop, "Install the Python SDK fixture dependency")
    assert fixture["if"] == "env.STRESS_KIND == 'cargo'"
    assert fixture["run"] == step_named(rust, "Install the Python SDK fixture dependency")["run"]
    nextest = step_named(loop, "Install pinned nextest in the runner temporary directory")
    assert nextest["if"] == "env.STRESS_KIND == 'cargo'"
    assert nextest["run"] == step_named(rust, "Install pinned nextest in the runner temporary directory")["run"]
    archive = next(step for step in loop["steps"] if step.get("uses") == "actions/download-artifact@v4"
                   and step["with"]["name"].startswith("stress-tests-"))
    assert archive["if"] == "env.STRESS_KIND == 'cargo'"
    assert archive["with"] == {"name": "stress-tests-${{ runner.os }}", "path": "${{ runner.temp }}/stress-tests"}
    names = [step.get("name") for step in loop["steps"]]
    run = step_named(loop, "Run the selection once")
    assert "--archive-file (Join-Path $env:RUNNER_TEMP 'stress-tests/stress-tests.tar.zst')" in run["run"]
    for step in (fixture, nextest, archive):
        assert names.index(step.get("name")) < names.index("Run the selection once")


def test_the_selection_input_lists_every_kind():
    description = raw_workflow().get("on", raw_workflow().get(True))["workflow_dispatch"]["inputs"]["selection"][
        "description"]
    for kind in ("row:<row>[/<case>]", "scenario:<run.py scenario>", "cargo:<package>/<test binary>[::<test>]",
                 "pytest node ids"):
        assert kind in description, kind


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
