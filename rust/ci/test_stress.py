"""rust.yml's stress mode: one selection on N fresh hosted runners, one row each."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import stress

ROOT = Path(__file__).resolve().parents[2]
CI_ONLY = "(github.event_name != 'workflow_dispatch' || inputs.mode == 'ci')"
CI_OR_STRESS = "github.event_name != 'workflow_dispatch' || (inputs.mode == 'ci' || inputs.mode == 'stress')"


def raw_workflow():
    return yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("selection, parsed", [
    ("row:hook", ("row", ["hook"])),
    (" row:hook/briefing-https-hook-json ", ("row", ["hook/briefing-https-hook-json"])),
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
    with pytest.raises(ValueError, match=r"cargo:<package>/\[bin/\]<test binary>\[::<test>\]"):
        stress.parse(selection)


DAEMON_BIN = "pseudolife-daemon/bin/pseudolife-daemon"
SHUTDOWN = "background::tests::shutdown_ends_within_its_deadlines_when_work_is_stuck"


def test_a_selection_can_name_a_binary_targets_unit_tests():
    # A binary crate has no library for an integration test to import, so its
    # timing-sensitive tests are unit tests in the bin target's test binary.
    assert stress.parse(f"cargo:{DAEMON_BIN}::{SHUTDOWN}") == ("cargo", [f"{DAEMON_BIN}::{SHUTDOWN}"])
    assert stress.cargo_item(f"{DAEMON_BIN}::{SHUTDOWN}") == (
        "pseudolife-daemon", "bin/pseudolife-daemon", SHUTDOWN)


@pytest.mark.parametrize("target", ["lib", "lib::runtime::tests::launch", "test/lib::integration_case",
                                   "bin/library"])
def test_a_library_selection_and_nearby_target_names_parse(target):
    name = f"pseudolife-stdio/{target}"
    assert stress.parse(f"cargo:{name}") == ("cargo", [name])


@pytest.mark.parametrize("suffix", ["", "::case"])
def test_the_reserved_library_name_cannot_be_combined_with_bin(suffix):
    with pytest.raises(ValueError, match="lib is reserved"):
        stress.parse(f"cargo:pseudolife-stdio/bin/lib{suffix}")


def test_a_library_selection_uses_the_packages_binary_id_and_keeps_exact_tests(tmp_path):
    names = ["pseudolife-stdio/lib::runtime::tests::launch", "pseudolife-stdio/lib",
             "pseudolife-stdio/test/lib::integration_case"]
    assert stress.cargo_item(names[0]) == ("pseudolife-stdio", "lib", "runtime::tests::launch")
    assert stress.filterset(names) == (
        "(binary_id(=pseudolife-stdio) & test(=runtime::tests::launch))"
        " | (binary_id(=pseudolife-stdio))"
        " | (binary_id(=pseudolife-stdio::lib) & test(=integration_case))")
    argv = stress.archive_command(names, tmp_path / "a.tar.zst")
    assert argv.count("--lib") == 1
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--test"] == ["lib"]
    assert "--bin" not in argv
    assert argv[argv.index("-E") + 1] == "binary_id(=pseudolife-stdio) | binary_id(=pseudolife-stdio::lib)"


def test_library_and_bin_targets_can_share_an_archive(tmp_path):
    argv = stress.archive_command(["pseudolife-stdio/lib::case", f"{DAEMON_BIN}::{SHUTDOWN}"],
                                 tmp_path / "a.tar.zst")
    assert argv.count("--lib") == 1
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--bin"] == ["pseudolife-daemon"]
    assert "--test" not in argv


@pytest.mark.parametrize("selection", [
    "cargo:pseudolife-daemon/bin/", "cargo:pseudolife-daemon/bin/../x", "cargo:pseudolife-daemon/bin/bin/x",
    "cargo:pseudolife-daemon/lib/x", "cargo:pseudolife-daemon/bin//x", "cargo:pseudolife-daemon/bin/X",
    "cargo:pseudolife-daemon/lib/bin/x", "cargo:pseudolife-daemon/test/library",
])
def test_a_malformed_bin_selection_is_refused(selection):
    with pytest.raises(ValueError, match=r"cargo:<package>/\[bin/\]<test binary>"):
        stress.parse(selection)


def test_a_bin_item_is_archived_with_bin_and_filtered_by_nextests_binary_id(tmp_path):
    names = [f"{DAEMON_BIN}::{SHUTDOWN}", "pseudolife-daemon/pdfium_provision"]
    # nextest names a bin target's test binary <package>::bin/<name>.
    assert stress.filterset(names) == (
        f"(binary_id(=pseudolife-daemon::bin/pseudolife-daemon) & test(={SHUTDOWN}))"
        " | (binary_id(=pseudolife-daemon::pdfium_provision))")
    argv = stress.archive_command(names, tmp_path / "a.tar.zst")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--bin"] == ["pseudolife-daemon"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--test"] == ["pdfium_provision"]
    assert argv[argv.index("-E") + 1] == (
        "binary_id(=pseudolife-daemon::bin/pseudolife-daemon) | binary_id(=pseudolife-daemon::pdfium_provision)")


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


@pytest.mark.parametrize("target", ["board_retry_contract", "lib::runtime::tests::launch", "test/lib"])
def test_archive_runs_cargo_in_the_rust_workspace(tmp_path, monkeypatch, target):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run",
                        lambda argv, cwd: calls.append((argv, cwd)) or SimpleNamespace(returncode=0))
    archive = tmp_path / "a.tar.zst"
    assert stress.main(["archive", "--selection", f"cargo:pseudolife-stdio/{target}",
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
    # The Rust job's `cargo nextest run -j 4`: nextest reads -j as test threads.
    assert argv[argv.index("--test-threads") + 1] == "4"
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
     {f"pseudolife-stdio::{RETRY}": [("a", ""), ("c", IGNORED)]}, ""),
    (["pseudolife-stdio/board_retry_contract::c"],
     {f"pseudolife-stdio::{RETRY}": [("a", FILTERED), ("c", IGNORED)]}, "no test ran for pseudolife-stdio/board_retry_contract::c"),
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


def test_a_whole_bin_and_one_of_its_tests_archive_the_bin_once(tmp_path):
    names = [DAEMON_BIN, f"{DAEMON_BIN}::{SHUTDOWN}"]
    assert stress.parse(" ".join(f"cargo:{n}" for n in names)) == ("cargo", names)
    assert stress.cargo_item(DAEMON_BIN) == ("pseudolife-daemon", "bin/pseudolife-daemon", None)
    argv = stress.archive_command(names, tmp_path / "a.tar.zst")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--bin"] == ["pseudolife-daemon"]
    assert "--test" not in argv
    assert stress.filterset(names) == ("(binary_id(=pseudolife-daemon::bin/pseudolife-daemon))"
                                       " | (binary_id(=pseudolife-daemon::bin/pseudolife-daemon)"
                                       f" & test(={SHUTDOWN}))")


def test_a_bin_items_evidence_reads_its_binary_id_suite(tmp_path):
    suite = "pseudolife-daemon::bin/pseudolife-daemon"
    (tmp_path / "junit.xml").write_text(nextest_junit({suite: [("other", FILTERED), (SHUTDOWN, "")]}),
                                        encoding="utf-8")
    assert stress.evidence("cargo", [f"{DAEMON_BIN}::{SHUTDOWN}"], tmp_path) == ""
    # The integration-test binary of the same name is not the bin target's.
    (tmp_path / "junit.xml").write_text(nextest_junit({"pseudolife-daemon::pseudolife-daemon": [(SHUTDOWN, "")]}),
                                        encoding="utf-8")
    assert stress.evidence("cargo", [f"{DAEMON_BIN}::{SHUTDOWN}"], tmp_path).startswith("no test ran for")


@pytest.mark.parametrize("target, suite", [("lib", "pseudolife-stdio"),
                                          ("test/lib", "pseudolife-stdio::lib")])
def test_library_and_integration_lib_evidence_use_distinct_suites(tmp_path, target, suite):
    other = "pseudolife-stdio::lib" if target == "lib" else "pseudolife-stdio"
    names = [f"pseudolife-stdio/{target}::runtime::tests::launch"]
    report = tmp_path / "junit.xml"
    report.write_text(nextest_junit({suite: [("unselected", FILTERED), ("runtime::tests::launch", "")],
                                    other: [("runtime::tests::launch", IGNORED)]}).replace(
                                        'name="runtime::tests::launch"',
                                        'name="runtime::tests::launch" time="1.250"'), encoding="utf-8")
    assert stress.evidence("cargo", names, tmp_path) == ""
    assert stress.cargo_times(names, report) == {f"{suite}::runtime::tests::launch": 1.25}
    report.write_text(nextest_junit({other: [("runtime::tests::launch", "")]}), encoding="utf-8")
    assert stress.evidence("cargo", names, tmp_path) == "no test ran for " + names[0]


@pytest.mark.parametrize("cases, note", [
    ([("a", ""), ("b", "")], ""),
    ([("a", ""), ("b", IGNORED)], ""),
    ([("a", IGNORED)], "no test ran for pseudolife-stdio/lib"),
    ([("a", '<failure message="x"/>')], "1 of 1 selected tests did not pass"),
    ([], "no test ran for pseudolife-stdio/lib"),
])
def test_whole_library_evidence_requires_every_selected_test(tmp_path, cases, note):
    (tmp_path / "junit.xml").write_text(nextest_junit({"pseudolife-stdio": cases}), encoding="utf-8")
    assert stress.evidence("cargo", ["pseudolife-stdio/lib"], tmp_path) == note


@pytest.mark.parametrize("child", [
    '<skipped message="runtime dependency unavailable"/>',
    '<skipped message="test does not match the run-ignored option"/>',
    '<skipped/>',
    FILTERED,
    IGNORED + '<failure message="failed before exclusion"/>',
])
def test_runtime_or_unrecognized_skips_still_fail_a_whole_binary(tmp_path, child):
    (tmp_path / "junit.xml").write_text(nextest_junit({
        f"pseudolife-stdio::{RETRY}": [("passed", ""), ("skipped", child)],
    }), encoding="utf-8")
    assert stress.evidence("cargo", [f"pseudolife-stdio/{RETRY}"], tmp_path) == (
        "1 of 2 selected tests skipped; a skip is not evidence")


def test_default_ignored_tests_do_not_appear_as_executed_timings(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(nextest_junit({f"pseudolife-stdio::{RETRY}": [
        ("passed", ""), ("ignored", IGNORED),
    ]}), encoding="utf-8")
    assert stress.cargo_times([f"pseudolife-stdio/{RETRY}"], report) == {
        f"pseudolife-stdio::{RETRY}::passed": 0.0,
    }


def test_a_cargo_iteration_without_a_junit_report_is_no_evidence(tmp_path):
    assert stress.evidence("cargo", ["pseudolife-stdio/board"], tmp_path) == "no JUnit report from nextest"


def built_here(tmp_path, workspace=None):
    """An archive path whose workspace record names this checkout (or another)."""
    archive = tmp_path / "a.tar.zst"
    Path(str(archive) + ".workspace").write_text(workspace or str((tmp_path / "rust").resolve()), encoding="utf-8")
    return archive


def cargo_run(out, archive, target=RETRY):
    return stress.main(["run", "--selection", f"cargo:pseudolife-stdio/{target}", "--iteration", "1",
                        "--candidates", "rust/target/release", "--archive-file", str(archive), "--out", str(out)])


@pytest.mark.parametrize("target, suite", [(RETRY, f"pseudolife-stdio::{RETRY}"),
                                          ("lib::a", "pseudolife-stdio"),
                                          ("test/lib::a", "pseudolife-stdio::lib")])
def test_a_cargo_run_keeps_this_iterations_junit_report(tmp_path, monkeypatch, target, suite):
    # The report lands under the remapped workspace's target dir; a stale one
    # from an earlier run must never count, and the row keeps a copy.
    monkeypatch.chdir(tmp_path)
    junit = stress.cargo_junit(Path("rust"))
    junit.parent.mkdir(parents=True)
    junit.write_text(nextest_junit({suite: [("stale", "")]}), encoding="utf-8")

    def fresh(argv, env):
        assert not junit.exists(), "a stale report survived into the run"
        junit.write_text(nextest_junit({suite: [("a", "")]}).replace(
            'name="a"', 'name="a" time="1.250"'), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(stress.subprocess, "run", fresh)
    out = tmp_path / "stress" / "stress-row-1.json"
    assert cargo_run(out, built_here(tmp_path), target) == 0
    assert 'name="a"' in (out.parent / "junit.xml").read_text(encoding="utf-8")
    row = json.loads(out.read_text(encoding="utf-8"))
    # Each selected test's time, so a test that returned at once (an
    # env-gated child helper) shows as 0 s rather than hiding in "pass".
    assert row["outcome"] == "pass" and row["tests"] == {f"{suite}::a": 1.25}


def test_a_cargo_run_that_left_no_report_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, env: SimpleNamespace(returncode=0))
    out = tmp_path / "stress" / "stress-row-1.json"
    assert cargo_run(out, built_here(tmp_path)) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["note"] == "no JUnit report from nextest"


@pytest.mark.parametrize("workspace", ["D:\\a\\other\\other\\rust", None])
def test_a_cargo_run_refuses_an_archive_built_in_another_checkout_path(tmp_path, monkeypatch, workspace):
    # The tests compiled in env!() paths under the build job's checkout; on a
    # runner whose path differs they would fail to spawn and read as flakes
    # (reviewer, 2026-10-23). A missing record is refused too.
    monkeypatch.chdir(tmp_path)
    archive = built_here(tmp_path, workspace) if workspace else tmp_path / "a.tar.zst"
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, env: calls.append(argv))
    out = tmp_path / "stress" / "stress-row-1.json"
    assert cargo_run(out, archive) == 1
    assert not calls
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["outcome"] == "fail" and "checkout path" in row["note"]


def test_archive_records_the_checkout_path_it_was_built_in(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, cwd: SimpleNamespace(returncode=0))
    archive = tmp_path / "out" / "a.tar.zst"
    archive.parent.mkdir()
    assert stress.main(["archive", "--selection", f"cargo:pseudolife-stdio/{RETRY}",
                        "--archive-file", str(archive)]) == 0
    assert Path(str(archive) + ".workspace").read_text(encoding="utf-8") == str((tmp_path / "rust").resolve())


def test_a_failed_archive_build_records_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, cwd: SimpleNamespace(returncode=101))
    archive = tmp_path / "a.tar.zst"
    assert stress.main(["archive", "--selection", f"cargo:pseudolife-stdio/{RETRY}",
                        "--archive-file", str(archive)]) == 101
    assert not Path(str(archive) + ".workspace").exists()


# nextest 0.9.146's report-skipped = "all" JUnit, verbatim from a toy
# workspace run (2026-10-23): one named test of probe_one, all of probe_two.
NEXTEST_0_9_146 = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="nextest-run" tests="5" skipped="3" failures="0" errors="0" uuid="ed590db6-d378-40b9-bb1e-358db7a7b697" timestamp="2026-10-23T19:46:47.034+11:00" time="0.018">
    <testsuite name="app-x::probe_one" tests="4" skipped="3" errors="0" failures="0">
        <testcase name="ignored" classname="app-x::probe_one" time="0.000">
            <skipped message="Skipped: test does not match the run-ignored option"/>
        </testcase>
        <testcase name="plain" classname="app-x::probe_one" time="0.000">
            <skipped message="Skipped: test does not match the provided expression filters"/>
        </testcase>
        <testcase name="uses_bin" classname="app-x::probe_one" time="0.000">
            <skipped message="Skipped: test does not match the provided expression filters"/>
        </testcase>
        <testcase name="inner::nested" classname="app-x::probe_one" timestamp="2026-10-23T19:46:47.035+11:00" time="0.012"/>
    </testsuite>
    <testsuite name="app-x::probe_two" tests="1" skipped="0" errors="0" failures="0">
        <testcase name="other" classname="app-x::probe_two" timestamp="2026-10-23T19:46:47.035+11:00" time="0.018"/>
    </testsuite>
</testsuites>
"""


@pytest.mark.parametrize("names, note", [
    (["app-x/probe_one::inner::nested", "app-x/probe_two"], ""),
    # Whole probe_one: the two expression-filtered tests were requested but did not run.
    (["app-x/probe_one"], "2 of 3 selected tests skipped"),
    (["app-x/probe_one::ignored"], "no test ran for app-x/probe_one::ignored"),
])
def test_cargo_evidence_reads_a_real_nextest_report(tmp_path, names, note):
    (tmp_path / "junit.xml").write_text(NEXTEST_0_9_146, encoding="utf-8")
    found = stress.evidence("cargo", names, tmp_path)
    assert found.startswith(note) and bool(found) == bool(note), found


@pytest.mark.parametrize("child, ok", [
    ("<system-out>log</system-out><system-err>log</system-err>", True),
    ('<error message="x"/>', False),
    ('<rerunFailure message="x"/>', False),
])
def test_captured_output_is_not_a_failure_but_an_error_is(tmp_path, child, ok):
    (tmp_path / "junit.xml").write_text(nextest_junit({f"pseudolife-stdio::{RETRY}": [("a", child)]}),
                                        encoding="utf-8")
    assert (stress.evidence("cargo", [f"pseudolife-stdio/{RETRY}"], tmp_path) == "") == ok


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
    # Only a cargo selection with a database profile says postgres=true; the
    # other kinds always get PostgreSQL (the workflow's step condition).
    assert output.read_text(encoding="utf-8") == f"iterations=[1, 2, 3]\nkind={kind}\npostgres=false\n"


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
    # The wait-mail capture and body-evidence modes left with the Python
    # freeze (14 November 2026).
    assert inputs["mode"]["options"] == ["ci", "stress"]
    assert inputs["selection"]["type"] == "string"
    assert inputs["iterations"] == {**inputs["iterations"], "type": "number", "default": 20}
    assert inputs["os"]["options"] == ["windows-latest", "ubuntu-latest"]
    assert inputs["os"]["default"] == "windows-latest"


def test_stress_jobs_run_only_in_stress_mode_and_reuse_the_candidate():
    jobs = raw_workflow()["jobs"]
    plan, loop, report = jobs["stress-plan"], jobs["stress"], jobs["stress-report"]
    assert plan["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'stress'"
    assert plan["outputs"] == {"iterations": "${{ steps.plan.outputs.iterations }}",
                               "kind": "${{ steps.plan.outputs.kind }}",
                               "postgres": "${{ steps.plan.outputs.postgres }}",
                               "oracle_base": "${{ steps.oracle.outputs.oracle_base }}"}
    assert loop["needs"] == ["stress-plan", "candidate", "candidate-harness", "stress-build"]
    assert loop["if"] == ("${{ !cancelled() && needs.stress-plan.result == 'success' "
                          "&& needs.candidate.result == 'success' "
                          "&& needs.candidate-harness.result == 'success' "
                          "&& (needs.stress-plan.outputs.kind != 'cargo' || needs.stress-build.result == 'success') }}")
    assert loop["strategy"] == {"fail-fast": False, "max-parallel": 10,
                                "matrix": {"iteration": "${{ fromJSON(needs.stress-plan.outputs.iterations) }}"}}
    assert loop["runs-on"] == "${{ inputs.os }}"
    assert loop["name"] == "Stress / ${{ inputs.os }} / ${{ matrix.iteration }}"
    download = next(step for step in loop["steps"] if step.get("uses") == "actions/download-artifact@v4"
                    and step["with"]["name"].startswith("rust-shim-"))
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    harness = next(step for step in loop["steps"] if step.get("name") == "Download harness candidate")
    assert harness["with"] == {"name": "rust-shim-harness-${{ runner.os }}", "path": "rust/target/release"}
    # An iteration never compiles: a cargo selection runs stress-build's archive.
    for step in loop["steps"]:
        assert not re.search(r"\bcargo (build|test|nextest (archive|run))\b|\bcargo \+", step.get("run", "")), \
            step.get("name")
    # Binding remains best-effort for selections that do not consume an
    # event oracle. Branch stress can bind its merge-base oracle, and
    # oracle-dependent selections still require a valid receipt.
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
    assert binds[0]["env"] == {"GITHUB_REF": "${{ github.ref }}",
                                "PSEUDOLIFE_PORT_DISPATCH_BASE": "${{ needs.stress-plan.outputs.oracle_base }}"}
    names = [step.get("name") for step in loop["steps"]]
    assert names.index(binds[0]["name"]) < names.index("Run the selection once")
    assert report["needs"] == ["stress-plan", "stress"]
    assert report["if"] == "${{ always() && needs.stress-plan.result == 'success' }}"
    upload = next(step for step in report["steps"] if step.get("name") == "Retain the stress report")
    assert upload["if"] == "always()" and upload["with"]["path"] == "stress-report.json"
    # The candidate build (and scope, which it needs) runs for a stress loop.
    for name in ("scope", "candidate"):
        assert CI_OR_STRESS in jobs[name]["if"], name


def step_named(job, name):
    return next(step for step in job["steps"] if step.get("name") == name)


def test_stress_plan_freezes_the_remote_master_once_for_all_iterations():
    plan = raw_workflow()["jobs"]["stress-plan"]
    checkout = next(step for step in plan["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-depth"] == 0
    freeze = next(step for step in plan["steps"] if step.get("id") == "oracle")
    assert "if" not in freeze  # Actions string comparisons ignore case.
    assert freeze["shell"] == "python"


@pytest.mark.parametrize("ref", ["refs/heads/master", "refs/heads/Master",
                                 "refs/heads/MASTER", "refs/heads/topic"])
def test_stress_plan_master_check_is_case_sensitive(tmp_path, ref):
    plan = raw_workflow()["jobs"]["stress-plan"]
    freeze = next(step for step in plan["steps"] if step.get("id") == "oracle")
    assert "if" not in freeze, "Actions must not skip case-distinct branch refs"
    assert freeze["shell"] == "python"
    if ref != "refs/heads/master":
        subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                        "commit", "--quiet", "--allow-empty", "-m", "fixture"],
                       cwd=tmp_path, check=True, capture_output=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
        subprocess.run(["git", "update-ref", "refs/remotes/origin/master", base],
                       cwd=tmp_path, check=True, capture_output=True)
    output = tmp_path / "github-output"
    output.touch()
    result = subprocess.run([sys.executable, "-c", freeze["run"]], cwd=tmp_path,
                            env={**os.environ, "GITHUB_REF": ref, "GITHUB_OUTPUT": str(output)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if ref == "refs/heads/master":
        assert output.read_text(encoding="utf-8") == ""
    else:
        assert output.read_text(encoding="utf-8") == f"oracle_base={base}\n"


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
    assert cache["with"] == {"prefix-key": "v0-rust-${{ steps.cache_week.outputs.week }}", "workspaces": "rust",
                             "shared-key": "stress-tests", "cache-on-failure": True}
    archive = step_named(build, "Build the selected test binaries into a nextest archive")
    assert "rust/ci/stress.py archive --selection $env:STRESS_SELECTION" in archive["run"]
    assert "stress-tests.tar.zst" in archive["run"]
    upload = next(step for step in build["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    # The archive and the checkout path it was built in, which every
    # iteration checks before running it.
    assert upload["with"] == {"name": "stress-tests-${{ runner.os }}",
                              "path": "${{ runner.temp }}/stress-tests.tar.zst\n"
                                      "${{ runner.temp }}/stress-tests.tar.zst.workspace\n",
                              "if-no-files-found": "error"}


def test_a_cargo_iteration_gets_the_rust_jobs_setup_and_not_the_parity_lanes():
    loop = raw_workflow()["jobs"]["stress"]
    assert loop["env"] == {"STRESS_SELECTION": "${{ inputs.selection }}",
                           "STRESS_KIND": "${{ needs.stress-plan.outputs.kind }}"}
    # The Rust job runs nextest with no PostgreSQL and only the Python SDK
    # fixture dependency; a cargo iteration matches it, except a selection
    # with a database profile (stress.CARGO_DATABASE_PROFILES).
    for name in ("Install checkout and test dependencies", "Prepare disposable PostgreSQL"):
        assert step_named(loop, name)["if"] == CARGO_POSTGRES, name
    rust = raw_workflow()["jobs"]["rust"]
    # cli_version_contract reads PSEUDOLIFE_PORT_ORACLE_ROOT (Codex review of
    # 50ca4be5). Since the Python freeze the Rust job points it at the
    # checkout's own pyproject.toml, and a cargo iteration does the same,
    # without continue-on-error: no oracle export can fail any more.
    bind = step_named(loop, "Bind the event-selected Python oracle when the dispatch has one")
    assert "if" not in bind and bind["continue-on-error"] is True
    metadata = step_named(loop, "Point the Rust identity contract at the checkout's package metadata")
    assert metadata["if"] == "env.STRESS_KIND == 'cargo'" and not metadata.get("continue-on-error")
    assert metadata["run"] == step_named(rust, "Point the Rust identity contract at the checkout's package metadata")["run"]
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
    for kind in ("row:<row>[/<case>]", "scenario:<run.py scenario>", stress.CARGO_FORM,
                 "pytest node ids"):
        assert kind in description, kind


def test_a_stress_loop_runs_no_ordinary_ci_job():
    jobs = raw_workflow()["jobs"]
    for name in ("rust", "daemon-image", "parity-checks", "parity"):
        assert CI_ONLY in jobs[name]["if"] and "wait-mail" not in jobs[name]["if"], name
    for name in ("parity-selfhosted", "source-mutants"):
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


# Several rows or several scenarios in one dispatch: each runs as its own
# harness process, as a one-item selection would, in every iteration.

@pytest.mark.parametrize("selection, parsed", [
    ("row:mail row:hook/briefing-https-hook-json row:lease",
     ("row", ["mail", "hook/briefing-https-hook-json", "lease"])),
    ("  scenario:tokenless   scenario:security-open ", ("scenario", ["tokenless", "security-open"])),
])
def test_a_selection_can_name_several_rows_or_several_scenarios(selection, parsed):
    assert stress.parse(selection) == parsed


@pytest.mark.parametrize("selection, item", [
    # One kind per selection, as with cargo items.
    ("row:hook scenario:tokenless", "scenario:tokenless"),
    ("scenario:tokenless row:hook", "row:hook"),
    ("row:hook tests/test_a.py", "tests/test_a.py"),
    ("row:hook cargo:pseudolife-stdio/board", "cargo:pseudolife-stdio/board"),
    ("tests/test_a.py row:hook", "row:hook"),
])
def test_a_selection_mixing_row_or_scenario_with_another_kind_names_the_stray_item(selection, item):
    with pytest.raises(ValueError, match=re.escape(f"item {item!r}") + ".*one kind"):
        stress.parse(selection)


@pytest.mark.parametrize("selection, item", [
    ("row:hook row:Bad", "row:Bad"),
    ("row:hook row:mail/", "row:mail/"),
    ("scenario:tokenless scenario:a/b", "scenario:a/b"),
    ("scenario:tokenless scenario:", "scenario:"),
])
def test_a_malformed_item_in_a_multi_item_selection_is_named(selection, item):
    with pytest.raises(ValueError, match=re.escape(f"item {item!r}")):
        stress.parse(selection)


@pytest.mark.parametrize("selection, item", [
    ("row:hook row:hook", "row:hook"),
    ("scenario:tokenless scenario:security-open scenario:tokenless", "scenario:tokenless"),
])
def test_an_item_named_twice_is_refused(selection, item):
    with pytest.raises(ValueError, match=re.escape(f"{item!r} is named twice")):
        stress.parse(selection)


@pytest.mark.parametrize("scenario", sorted(stress.REMOTE_BIND))
def test_plan_refuses_a_remote_bind_scenario_beside_others_naming_it(tmp_path, capsys, scenario):
    # One iteration cannot provide the debug loopback-bind build, so the whole
    # dispatch is refused rather than run with that item on the wrong binary.
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", f"scenario:tokenless scenario:{scenario} scenario:security-open",
                        "--iterations", "20", "--github-output", str(output)]) == 2
    assert not output.exists()
    err = capsys.readouterr().err
    assert f"scenario:{scenario} needs the debug loopback-bind build" in err and "nothing was started" in err


def test_plan_refuses_rows_mixed_with_scenarios(tmp_path, capsys):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", "row:hook scenario:tokenless", "--iterations", "20",
                        "--github-output", str(output)]) == 2
    assert not output.exists()
    err = capsys.readouterr().err
    assert "item 'scenario:tokenless'" in err and "dispatch" in err and "nothing was started" in err


@pytest.mark.parametrize("selection, kind", [("row:mail row:lease", "row"),
                                             ("scenario:tokenless scenario:security-open", "scenario")])
def test_plan_accepts_several_rows_or_scenarios_as_one_loop(tmp_path, capsys, selection, kind):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", selection, "--iterations", "20", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == (
        f"iterations={json.dumps(list(range(1, 21)))}\nkind={kind}\npostgres=false\n")
    assert "2 items" in capsys.readouterr().out


def fake_harness(cases, calls, codes=None):
    """A subprocess.run stand-in: each harness run writes a summary comparing
    cases[<row or scenario>] cases and exits codes.get(<name>, 0)."""
    def run(argv, env):
        calls.append((argv, env))
        name = argv[argv.index("--row") + 1] if "--row" in argv else argv[argv.index("--only") + 1]
        out = Path(argv[argv.index("--out") + 1])
        if "--row" in argv:
            summary = {"complete": True, "rows": {name: {"cases": cases[name]}}}
        else:
            summary = {"scenarios": [{"cases": [{}] * cases[name]}], "refusals": []}
        out.write_text(json.dumps(summary), encoding="utf-8")
        return SimpleNamespace(returncode=(codes or {}).get(name, 0))
    return run


def run_row(tmp_path, selection):
    out = tmp_path / "stress" / "stress-row-1.json"
    code = stress.main(["run", "--selection", selection, "--iteration", "1",
                        "--candidates", str(tmp_path / "bin"), "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_each_row_runs_in_its_own_harness_process_with_its_own_setup(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", fake_harness({"mail": 2, "lease": 3}, calls))
    code, row = run_row(tmp_path, "row:mail/some-case row:lease")
    assert code == 0 and row["outcome"] == "pass" and row["exit_code"] == 0
    (mail, _), (lease, _) = calls
    assert mail[mail.index("--row") + 1] == "mail" and mail[mail.index("--case") + 1] == "some-case"
    assert "--skip-bank" in mail and "--skip-bank" not in lease and "--case" not in lease
    outs = [Path(argv[argv.index("--out") + 1]) for argv, _ in calls]
    assert len(set(outs)) == 2 and all(out.parent.parent == tmp_path / "stress" for out in outs)
    assert [(item["item"], item["outcome"], item["compared"]) for item in row["items"]] == [
        ("row:mail/some-case", "pass", 2), ("row:lease", "pass", 3)]


def test_each_scenario_runs_on_its_own_lanes_binary_and_env(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", fake_harness({"security-open": 1, "tokenless": 4}, calls))
    code, row = run_row(tmp_path, "scenario:security-open scenario:tokenless")
    assert code == 0 and [item["outcome"] for item in row["items"]] == ["pass", "pass"]
    (static, static_env), (h5, h5_env) = calls
    assert Path(static[static.index("--rust-bin") + 1]).stem == "pseudolife-daemon-static"
    assert "--fixture" not in static and static_env["PL_HARNESS_SLICE"] == "http"
    assert Path(h5[h5.index("--rust-bin") + 1]).stem == "pseudolife-daemon"
    assert "--fixture" in h5 and "PL_HARNESS_SLICE" not in h5_env
    assert static_env["PL_HARNESS_SCRATCH"] != h5_env["PL_HARNESS_SCRATCH"]


@pytest.mark.parametrize("cases, codes, failed, note", [
    # The evidence rule holds per item: a row that compared nothing fails the
    # iteration even though the other rows compared cases and all exited 0.
    ({"mail": 2, "hook": 0, "lease": 5}, {}, ["row:hook"], "no case was compared"),
    ({"mail": 2, "hook": 3, "lease": 5}, {"lease": 1}, ["row:lease"], "exit code 1"),
    ({"mail": 0, "hook": 3, "lease": 0}, {}, ["row:mail", "row:lease"], "no case was compared"),
])
def test_an_iteration_passes_only_if_every_item_compared_a_case(tmp_path, monkeypatch, cases, codes,
                                                                failed, note):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", fake_harness(cases, calls, codes))
    code, row = run_row(tmp_path, "row:mail row:hook row:lease")
    # Every item runs even after one fails, so each has its own result.
    assert len(calls) == 3
    assert code != 0 and row["outcome"] == "fail" and row["exit_code"] == code
    assert [item["item"] for item in row["items"] if item["outcome"] == "fail"] == failed
    assert all(note in item["note"] for item in row["items"] if item["outcome"] == "fail")
    assert all("note" not in item for item in row["items"] if item["outcome"] == "pass")
    assert all(name in row["note"] for name in failed)


def test_a_single_row_iteration_records_exactly_what_it_did_before(tmp_path, monkeypatch):
    # Backward compatibility: one item keeps the summary at scratch/harness.json
    # and the row's keys, with no per-item list.
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", fake_harness({"hook": 2}, calls))
    code, row = run_row(tmp_path, "row:hook/some-case")
    assert code == 0 and len(calls) == 1
    argv, env = calls[0]
    assert argv == stress.command("row", ["hook", "some-case"], tmp_path / "bin", tmp_path / "stress")
    assert argv[argv.index("--out") + 1] == str(tmp_path / "stress" / "harness.json")
    assert set(row) == {"iteration", "attempt", "selection", "runner", "exit_code", "outcome", "seconds"}
    assert row["selection"] == "row:hook/some-case"


def test_a_single_scenario_keeps_its_scratch_paths(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stress.subprocess, "run", fake_harness({"tokenless": 0}, calls))
    code, row = run_row(tmp_path, "scenario:tokenless")
    argv, env = calls[0]
    assert argv[argv.index("--out") + 1] == str(tmp_path / "stress" / "harness.json")
    assert env["PL_HARNESS_SCRATCH"] == str(tmp_path / "stress" / "harness-scratch")
    assert code == 1 and "items" not in row
    assert row["note"] == "no case was compared (all filtered out on this platform, or no summary)"


def write_items_row(directory, iteration, outcomes):
    path = directory / f"stress-row-{iteration}" / f"stress-row-{iteration}.json"
    path.parent.mkdir(parents=True)
    items = [{"item": item, "exit_code": 0, "outcome": outcome, "compared": 3 if outcome == "pass" else 0,
              "seconds": 4.0, **({} if outcome == "pass" else {"note": "no case was compared"})}
             for item, outcome in outcomes.items()]
    failed = [item["item"] for item in items if item["outcome"] == "fail"]
    path.write_text(json.dumps({"iteration": iteration, "attempt": 1,
                                "outcome": "fail" if failed else "pass", "exit_code": 1 if failed else 0,
                                "seconds": 12.5, "runner": {"image": "win25 1", "cpus": 4}, "items": items,
                                **({"note": "failed: " + ", ".join(failed)} if failed else {})}),
                    encoding="utf-8")


def test_the_report_names_each_items_result_per_iteration_and_in_total(tmp_path):
    rows = tmp_path / "rows"
    rows.mkdir()
    write_items_row(rows, 1, {"row:mail": "pass", "row:hook": "pass"})
    write_items_row(rows, 2, {"row:mail": "pass", "row:hook": "fail"})
    out, summary = tmp_path / "stress-report.json", tmp_path / "summary.md"
    code = stress.main(["report", "--selection", "row:mail row:hook", "--iterations", "3", "--os", "windows-latest",
                        "--rows", str(rows), "--out", str(out), "--summary", str(summary)])
    assert code == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["items"] == {"row:mail": {"passed": 2, "failed": 0, "no_result": 1},
                               "row:hook": {"passed": 1, "failed": 1, "no_result": 1}}
    text = summary.read_text(encoding="utf-8")
    assert "row:mail: 2 of 3 iterations passed" in text and "row:hook: 1 of 3 iterations passed" in text
    second = next(line for line in text.splitlines() if line.startswith("| 2 |"))
    assert "row:mail pass" in second and "row:hook fail: no case was compared" in second


@pytest.mark.parametrize("kind, names", [
    ("row", ["mail", "hook", "lease", "lease_operator", "doctor", "transfer", "audit"]),
    ("scenario", ["tokenless", "session-reap", "security-open", "security-closed", "body-limits",
                  "static-build", "static-paths"]),
])
def test_plan_refuses_more_items_than_one_job_can_run(tmp_path, capsys, kind, names):
    # Items run one after another in one 45-minute job: about 4-5 minutes of
    # setup plus 2-3 minutes per row (review of 6ffc6a9f).
    output = tmp_path / "out"
    selection = " ".join(f"{kind}:{name}" for name in names)
    assert stress.main(["plan", "--selection", selection, "--iterations", "20",
                        "--github-output", str(output)]) == 2
    assert not output.exists()
    err = capsys.readouterr().err
    assert f"{len(names)} items" in err and f"at most {stress.MAX_ITEMS}" in err and "split" in err
    assert stress.MAX_ITEMS == 6
    assert stress.parse(" ".join(f"{kind}:{name}" for name in names[:6]))[1] == names[:6]
    # The budget the cap was sized for is the stress job's own.
    assert raw_workflow()["jobs"]["stress"]["timeout-minutes"] == stress.ITERATION_MINUTES


@pytest.mark.parametrize("selection, message", [
    # A whole row already runs its cases; the same case twice is one item.
    ("row:hook row:hook/x", "item 'row:hook/x' overlaps 'row:hook'"),
    ("row:hook/x row:mail row:hook", "item 'row:hook' overlaps 'row:hook/x'"),
    ("row:hook/x row:mail row:hook/x", "'row:hook/x' is named twice"),
])
def test_overlapping_row_items_are_refused(selection, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        stress.parse(selection)


def test_distinct_cases_of_one_row_are_separate_items():
    assert stress.parse("row:hook/x row:hook/y") == ("row", ["hook/x", "hook/y"])


def test_the_row_file_is_rewritten_after_each_item(tmp_path, monkeypatch):
    # A job that times out keeps the finished items' results and names the
    # item that was running (review of 6ffc6a9f).
    out = tmp_path / "stress" / "stress-row-1.json"
    seen = []
    inner = fake_harness({"mail": 2, "hook": 3, "lease": 1}, [])

    def run(argv, env):
        seen.append(json.loads(out.read_text(encoding="utf-8")))
        return inner(argv, env)
    monkeypatch.setattr(stress.subprocess, "run", run)
    code, row = run_row(tmp_path, "row:mail row:hook row:lease")
    assert code == 0 and row["outcome"] == "pass" and "note" not in row
    during_hook = seen[1]
    assert during_hook["outcome"] == "fail"
    assert [(item["item"], item["outcome"]) for item in during_hook["items"]] == [
        ("row:mail", "pass"), ("row:hook", "fail")]
    assert during_hook["items"][1]["note"] == "not finished"
    assert "not finished: row:hook" in during_hook["note"] and "not run: row:lease" in during_hook["note"]


@pytest.mark.parametrize("broken", ["command", "environment", "subprocess"])
def test_an_item_that_raises_is_a_failure_and_the_rest_still_run(tmp_path, monkeypatch, broken):
    calls = []
    harness = fake_harness({"mail": 2, "hook": 3, "lease": 1}, calls)

    def boom(*args, **kwargs):
        raise FileNotFoundError("no such candidate: pseudolife-stdio")
    if broken == "subprocess":
        def run(argv, env):
            return boom() if argv[argv.index("--row") + 1] == "hook" else harness(argv, env)
        monkeypatch.setattr(stress.subprocess, "run", run)
    else:
        monkeypatch.setattr(stress.subprocess, "run", harness)
        real = getattr(stress, broken)
        monkeypatch.setattr(stress, broken, lambda kind, names, *rest: boom() if names[0] == "hook"
                            else real(kind, names, *rest))
    code, row = run_row(tmp_path, "row:mail row:hook row:lease")
    assert code == 1 and row["outcome"] == "fail" and row["exit_code"] == 1
    assert [item["outcome"] for item in row["items"]] == ["pass", "fail", "pass"]
    hook = row["items"][1]
    assert hook["exit_code"] is None
    assert hook["note"] == "FileNotFoundError: no such candidate: pseudolife-stdio"
    assert [argv[argv.index("--row") + 1] for argv, _ in calls] == ["mail", "lease"]
    assert "row:hook" in row["note"] and "not run" not in row["note"]


@pytest.mark.parametrize("summary", ["[]", "3", "\"text\""])
def test_an_item_whose_summary_is_not_an_object_fails_and_the_rest_still_run(tmp_path, monkeypatch, summary):
    # Valid JSON that is not an object makes compared() raise AttributeError
    # or TypeError, after the item's exit code is known (review of 68e97311).
    calls = []
    harness = fake_harness({"hook": 3}, calls)

    def run(argv, env):
        if argv[argv.index("--row") + 1] == "mail":
            calls.append((argv, env))
            Path(argv[argv.index("--out") + 1]).write_text(summary, encoding="utf-8")
            return SimpleNamespace(returncode=1)
        return harness(argv, env)
    monkeypatch.setattr(stress.subprocess, "run", run)
    code, row = run_row(tmp_path, "row:mail row:hook")
    assert [argv[argv.index("--row") + 1] for argv, _ in calls] == ["mail", "hook"]
    assert code == 1 and row["outcome"] == "fail" and row["exit_code"] == 1
    mail, hook = row["items"]
    assert mail["item"] == "row:mail" and mail["outcome"] == "fail" and mail["exit_code"] == 1
    assert mail["note"].split(":")[0] in ("AttributeError", "TypeError")
    assert hook["outcome"] == "pass" and "row:mail" in row["note"]


# Ids without spaces: a stress dispatch names pytest nodes space-separated.
@pytest.mark.parametrize("selection", ["row:hook", "row:mail row:hook"], ids=["single", "multi"])
def test_the_row_is_replaced_atomically(tmp_path, monkeypatch, selection):
    # A kill mid-write must not leave a torn row: the row is written to a
    # temporary file beside it, then moved over it (review of 68e97311).
    replaced = []
    real = stress.os.replace

    def replace(source, target):
        replaced.append((Path(source), Path(target)))
        return real(source, target)
    monkeypatch.setattr(stress.os, "replace", replace)
    monkeypatch.setattr(stress.subprocess, "run", fake_harness({"mail": 2, "hook": 3}, []))
    code, row = run_row(tmp_path, selection)
    out = tmp_path / "stress" / "stress-row-1.json"
    assert code == 0 and replaced and all(target == out and source.parent == out.parent and source != out
                                          for source, target in replaced)
    assert out.is_file() and not list(out.parent.rglob("*.tmp"))


@pytest.mark.parametrize("content, reason", [
    ('{"iteration": 2, "attempt": 1, "outc', "unreadable"),  # torn mid-write
    ('{"iteration": 2, "attempt": 1}', "incomplete"),  # no outcome, runner or seconds
    ('[2]', "incomplete"),
    ('{"iteration": 2, "attempt": 1, "outcome": "pass", "exit_code": 0, "seconds": 1, "runner": {},'
     ' "items": [{"outcome": "pass"}]}', "incomplete"),  # an item without its name
])
def test_the_report_counts_an_unreadable_row_as_that_iterations_failure(tmp_path, content, reason):
    rows = tmp_path / "rows"
    rows.mkdir()
    write_row(rows, 1, "pass")
    write_row(rows, 3, "pass")
    broken = rows / "stress-row-2" / "stress-row-2.json"
    broken.parent.mkdir()
    broken.write_text(content, encoding="utf-8")
    out, summary = tmp_path / "stress-report.json", tmp_path / "summary.md"
    code = stress.main(["report", "--selection", "row:mail row:hook", "--iterations", "3", "--os", "windows-latest",
                        "--rows", str(rows), "--out", str(out), "--summary", str(summary)])
    assert code == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert [row["outcome"] for row in report["rows"]] == ["pass", "fail", "pass"]
    assert report["passed"] == 2 and report["failed"] == 1
    second = report["rows"][1]
    assert second["iteration"] == 2 and reason in second["note"] and "stress-row-2.json" in second["note"]
    assert "stress-row-2.json" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("selection, summary, error", [
    # compared() raising on a summary that is valid JSON but not an object.
    ("row:hook", "[]", ("AttributeError", "TypeError")),
    ("scenario:tokenless", "3", ("AttributeError", "TypeError")),
    ("row:hook/some-case", '"text"', ("AttributeError", "TypeError")),
])
def test_a_single_item_whose_summary_cannot_be_judged_fails(tmp_path, monkeypatch, selection, summary, error):
    # The harness exited 0, but a run whose evidence cannot be read is no
    # evidence: the row must say fail, never pass (review of 3715a45a).
    def harness(argv, env):
        Path(argv[argv.index("--out") + 1]).write_text(summary, encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(stress.subprocess, "run", harness)
    code, row = run_row(tmp_path, selection)
    assert code == 1 and row["outcome"] == "fail" and row["exit_code"] == 1
    assert row["note"].split(":")[0] in error and len(row["note"].split(": ", 1)[1]) > 0
    assert "items" not in row


@pytest.mark.parametrize("selection", ["row:hook", "scenario:tokenless", "tests/test_a.py"])
def test_a_single_item_whose_evidence_check_raises_fails(tmp_path, monkeypatch, selection):
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, env: SimpleNamespace(returncode=0))

    def broken(kind, names, scratch):
        raise RuntimeError("summary unreadable")
    monkeypatch.setattr(stress, "evidence", broken)
    code, row = run_row(tmp_path, selection)
    assert code == 1 and row["outcome"] == "fail" and row["exit_code"] == 1
    assert row["note"] == "RuntimeError: summary unreadable"


@pytest.mark.parametrize("selection", ["row:hook", "scenario:tokenless", "tests/test_a.py"])
def test_an_interrupt_before_evidence_is_judged_leaves_a_failed_row(tmp_path, monkeypatch, selection):
    # A job timeout or cancel (SIGINT) while evidence() runs: the harness
    # exited 0, but nothing was judged, so the row the always() upload
    # delivers must say fail, and the interrupt still ends the step
    # (review of 3280332b).
    monkeypatch.setattr(stress.subprocess, "run", lambda argv, env: SimpleNamespace(returncode=0))

    def interrupted(kind, names, scratch):
        raise KeyboardInterrupt
    monkeypatch.setattr(stress, "evidence", interrupted)
    out = tmp_path / "stress" / "stress-row-1.json"
    with pytest.raises(KeyboardInterrupt):
        stress.main(["run", "--selection", selection, "--iteration", "1",
                     "--candidates", str(tmp_path / "bin"), "--out", str(out)])
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["outcome"] == "fail" and row["note"] == "interrupted before evidence was judged"


@pytest.mark.parametrize("failing", ["write", "replace"])
def test_a_failed_row_write_leaves_no_temporary_file_and_keeps_the_previous_row(tmp_path, monkeypatch, failing):
    out = tmp_path / "stress-row-1.json"
    stress.write_row(out, {"iteration": 1, "outcome": "fail"})
    previous = out.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise PermissionError("denied")

    def torn(self, text, *args, **kwargs):
        # A disk-full write: part of the temporary file lands, then it fails.
        with open(self, "w", encoding="utf-8") as handle:
            handle.write(text[:5])
        raise PermissionError("no space left")
    if failing == "replace":
        monkeypatch.setattr(stress.os, "replace", boom)
    else:
        monkeypatch.setattr(stress.Path, "write_text", torn)
    with pytest.raises(PermissionError):
        stress.write_row(out, {"iteration": 1, "outcome": "pass"})
    assert out.read_text(encoding="utf-8") == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ["stress-row-1.json"]


def test_a_single_item_report_is_unchanged(tmp_path):
    rows = tmp_path / "rows"
    rows.mkdir()
    write_row(rows, 1, "pass")
    out, summary = tmp_path / "stress-report.json", tmp_path / "summary.md"
    assert stress.main(["report", "--selection", "row:hook", "--iterations", "1", "--os", "windows-latest",
                        "--rows", str(rows), "--out", str(out), "--summary", str(summary)]) == 0
    assert "items" not in json.loads(out.read_text(encoding="utf-8"))
    assert summary.read_text(encoding="utf-8") == (
        "### Stress: row:hook on windows-latest\n1 of 1 iterations passed.\n\n"
        "| Iteration | Attempt | Outcome | Seconds | Image | CPUs | Note |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 1 | pass | 12.5 | win25 1 | 4 |  |\n")


# ---- cargo selections whose tests need a disposable PostgreSQL ------------

SHADOW_SERVICE = [f"{DAEMON_BIN}::service::shadow_refresh_tests::db_{name}" for name in (
    "dropped_request_cannot_let_two_refreshes_overlap", "failed_preread_records_not_ready_and_backs_off",
    "meta_commit_during_service_init_is_refreshed")]
SHADOW_STORE = f"{DAEMON_BIN}::shadow::tests::db_added_entry_is_appended_and_searchable"
DAEMON_SUITE = "pseudolife-daemon::bin/pseudolife-daemon"
CARGO_POSTGRES = "env.STRESS_KIND != 'cargo' || needs.stress-plan.outputs.postgres == 'true'"


def cargo_selection(names):
    return " ".join(f"cargo:{name}" for name in names)


@pytest.mark.parametrize("names, profile", [
    (SHADOW_SERVICE, "shadow-refresh"),
    ([SHADOW_STORE, *SHADOW_SERVICE], "shadow-refresh"),
    # Near misses: not a db_ test, or no exact test at all, need no database.
    ([f"{DAEMON_BIN}::shadow::tests::planner_patches_only_what_changed"], None),
    ([f"{DAEMON_BIN}::{SHUTDOWN}"], None),
    ([DAEMON_BIN], None),
    (["pseudolife-stdio/board_retry_contract"], None),
])
def test_a_cargo_selection_names_the_database_profile_its_tests_need(names, profile):
    assert stress.parse(cargo_selection(names)) == ("cargo", names)
    assert stress.database_profile(names) == profile


@pytest.mark.parametrize("names, profile", [(SHADOW_SERVICE, "true"), ([f"{DAEMON_BIN}::{SHUTDOWN}"], "false")])
def test_plan_says_whether_a_cargo_selection_needs_postgres(tmp_path, capsys, names, profile):
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", cargo_selection(names), "--iterations", "20",
                        "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == (
        f"iterations={json.dumps(list(range(1, 21)))}\nkind=cargo\npostgres={profile}\n")
    if profile == "true":
        assert "shadow-refresh" in capsys.readouterr().out


@pytest.mark.parametrize("item", [
    # A db_ test no profile covers would run without its database: it
    # skips (returns early) and would count as run.
    f"{DAEMON_BIN}::bank::startup_tests::db_seating_recovers_an_abandoned_writer_transaction",
    # A profile's prefix in another target is not that profile's test.
    "pseudolife-stdio/bin/pseudolife-stdio::shadow::tests::db_added_entry_is_appended_and_searchable",
    "pseudolife-daemon/lib::service::shadow_refresh_tests::db_meta_commit_during_service_init_is_refreshed",
])
def test_plan_refuses_a_database_test_no_profile_covers(tmp_path, capsys, item):
    with pytest.raises(ValueError, match="CARGO_DATABASE_PROFILES"):
        stress.parse(f"cargo:{item}")
    output = tmp_path / "out"
    assert stress.main(["plan", "--selection", f"cargo:{item}", "--iterations", "20",
                        "--github-output", str(output)]) == 2
    assert not output.exists()
    error = capsys.readouterr().err
    assert "nothing was started" in error and "rust/ci/stress.py" in error and item in error


@pytest.mark.parametrize("other", [f"{DAEMON_BIN}::{SHUTDOWN}", DAEMON_BIN, "pseudolife-stdio/board_retry_contract"])
def test_plan_refuses_database_tests_mixed_with_tests_of_another_profile(tmp_path, capsys, other):
    # One loop prepares one profile's database and runs its tests serially:
    # anything else would run under that setup, not its own lane's.
    selection = cargo_selection([SHADOW_SERVICE[0], other])
    with pytest.raises(ValueError, match="dispatch .* separately"):
        stress.parse(selection)
    assert stress.main(["plan", "--selection", selection, "--iterations", "20",
                        "--github-output", str(tmp_path / "out")]) == 2
    assert other in capsys.readouterr().err


def test_every_database_profile_names_its_setup():
    assert stress.CARGO_DATABASE_PROFILES["shadow-refresh"] == {
        "target": DAEMON_BIN,
        "prefixes": ("shadow::tests::db_", "service::shadow_refresh_tests::db_"),
        "slice": "pgs",
        "dsn_env": "PL_SHADOW_REFRESH_DSN",
        "env": {"PL_SHADOW_REFRESH_REQUIRED": "1", "CUDA_VISIBLE_DEVICES": "-1",
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        "fixture": {"env": "PL_SHADOW_EMBED_FIXTURE", "dimension": 1024},
        "ort_env": "ORT_DYLIB_PATH",
    }
    for name, profile in stress.CARGO_DATABASE_PROFILES.items():
        assert stress.NAME.fullmatch(name) and stress.CARGO.fullmatch(f"cargo:{profile['target']}"), name
        assert all(prefix.rpartition("::")[2].startswith("db_") for prefix in profile["prefixes"]), name


def test_a_database_selection_runs_serially_with_its_required_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv = stress.command("cargo", SHADOW_SERVICE, tmp_path / "bin", tmp_path / "scratch", tmp_path / "a.tar.zst")
    # nextest runs each test in its own process, so the tests' in-process
    # database mutex cannot keep them off one another's shared bank.
    assert argv[argv.index("--test-threads") + 1] == "1"
    assert argv[argv.index("-E") + 1] == stress.filterset(SHADOW_SERVICE)
    env = stress.environment("cargo", SHADOW_SERVICE, tmp_path / "bin", tmp_path)
    # Required: a missing setup panics, so it fails the iteration instead of skipping.
    assert env == {"PSEUDOLIFE_MCP_PYTHON": sys.executable, "PL_SHADOW_REFRESH_REQUIRED": "1",
                   "CUDA_VISIBLE_DEVICES": "-1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


@pytest.mark.parametrize("names, warned", [
    ([DAEMON_BIN], True),
    ([f"{DAEMON_BIN}::{SHUTDOWN}"], False),
    (["pseudolife-stdio/board_retry_contract"], False),
])
def test_plan_warns_that_a_whole_binary_skips_its_database_tests(tmp_path, capsys, names, warned):
    # A whole-binary item gets no database: its db_ tests return early and
    # count as run, so the dispatcher is told to name them.
    assert stress.main(["plan", "--selection", cargo_selection(names), "--iterations", "20",
                        "--github-output", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert ("warning:" in out and "name them as exact tests" in out) is warned


@pytest.mark.parametrize("text", [
    "connection to postgresql://login:secret@127.0.0.1:5433/pl_cf_x failed",
    "connection failed: host=127.0.0.1 user=login password=secret dbname=pl_cf_x",
    "bad conninfo password = 'secret' port=5433",
])
def test_a_setup_error_never_carries_the_password(text):
    assert "secret" not in stress.redacted(RuntimeError(text))


class FakeSlice:
    """pgdisposable.Slice's create/drop, recorded."""

    def __init__(self, slice_id, events, fail=None):
        self.PREFIX, self.events, self.fail = f"pl_cf_{slice_id}_", events, fail or set()

    def name(self, label):
        return label + "_run"

    def create(self, name):
        self.events.append(("create", name))
        if "create" in self.fail:
            raise RuntimeError("could not connect")
        if "login" in self.fail:
            # pgdisposable's login check exits rather than raising.
            raise SystemExit("test login file is missing")
        return f"postgresql://login:secret@127.0.0.1:5433/{name}"

    def drop(self, name):
        self.events.append(("drop", name))
        if "drop" in self.fail:
            raise RuntimeError("drop refused")


def database_run(tmp_path, monkeypatch, events, nextest, fail=None, fixture=None):
    """Run the shadow Service selection once through stress.main with a fake
    slice, fixture and nextest; returns (exit code, row, nextest calls)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stress, "database_slice", lambda profile: FakeSlice(profile["slice"], events, fail))

    def fake_fixture(profile, root):
        events.append(("fixture", root))
        if fixture:
            raise fixture
        return {"PL_SHADOW_EMBED_FIXTURE": str(root / "model"), "ORT_DYLIB_PATH": str(root / "onnxruntime.dll")}
    monkeypatch.setattr(stress, "fixture_environment", fake_fixture)
    calls = []

    def run(argv, env):
        calls.append((argv, env))
        events.append(("nextest", None))
        return nextest(argv, env)
    monkeypatch.setattr(stress.subprocess, "run", run)
    out = tmp_path / "stress" / "stress-row-1.json"
    code = stress.main(["run", "--selection", cargo_selection(SHADOW_SERVICE), "--iteration", "1",
                        "--candidates", "rust/target/release", "--archive-file", str(built_here(tmp_path)),
                        "--out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8")), calls


def nextest_passes(argv, env):
    junit = stress.cargo_junit(Path("rust"))
    junit.parent.mkdir(parents=True, exist_ok=True)
    junit.write_text(nextest_junit({DAEMON_SUITE: [(name.partition("::")[2], "") for name in SHADOW_SERVICE]}),
                     encoding="utf-8")
    return SimpleNamespace(returncode=0)


def test_a_database_iteration_gets_a_fresh_bank_fixture_and_runtime(tmp_path, monkeypatch):
    events = []
    code, row, calls = database_run(tmp_path, monkeypatch, events, nextest_passes)
    assert code == 0 and row["outcome"] == "pass"
    assert set(row["tests"]) == {f"{DAEMON_SUITE}::{name.partition('::')[2]}" for name in SHADOW_SERVICE}
    (argv, env), = calls
    name = events[0][1]
    # The tests assert the bank is a disposable pl_cf_ database.
    assert name.startswith("pl_cf_pgs_stress_")
    assert env["PL_SHADOW_REFRESH_DSN"] == f"postgresql://login:secret@127.0.0.1:5433/{name}"
    assert env["PL_SHADOW_REFRESH_REQUIRED"] == "1"
    fixture_root = events[1][1]
    assert env["PL_SHADOW_EMBED_FIXTURE"] == str(fixture_root / "model")
    assert env["ORT_DYLIB_PATH"] == str(fixture_root / "onnxruntime.dll")
    # The fixture is not written into the scratch directory the job uploads.
    assert (tmp_path / "stress").resolve() not in Path(fixture_root).resolve().parents
    assert argv[argv.index("--test-threads") + 1] == "1"
    assert [event for event, _ in events] == ["create", "fixture", "nextest", "drop"]
    assert events[-1] == ("drop", name)
    # The DSN carries the test login's password: it never reaches the row.
    assert "secret" not in json.dumps(row)


def test_a_failed_database_iteration_still_drops_its_bank(tmp_path, monkeypatch):
    events = []
    code, row, _ = database_run(tmp_path, monkeypatch, events, lambda argv, env: SimpleNamespace(returncode=100))
    assert code == 100 and row["outcome"] == "fail"
    assert [event for event, _ in events] == ["create", "fixture", "nextest", "drop"]


def test_a_database_iteration_that_cannot_start_nextest_still_drops_its_bank(tmp_path, monkeypatch):
    events = []

    def missing(argv, env):
        raise FileNotFoundError(argv[0])
    with pytest.raises(FileNotFoundError):
        database_run(tmp_path, monkeypatch, events, missing)
    assert [event for event, _ in events] == ["create", "fixture", "nextest", "drop"]
    row = json.loads((tmp_path / "stress" / "stress-row-1.json").read_text(encoding="utf-8"))
    assert row["outcome"] == "fail"


@pytest.mark.parametrize("fail, fixture, expected", [
    ({"create"}, None, ["create", "drop"]),
    ({"login"}, None, ["create", "drop"]),
    (None, ModuleNotFoundError("No module named 'onnx'"), ["create", "fixture", "drop"]),
])
def test_a_database_setup_failure_is_a_failed_row_and_drops_the_bank(tmp_path, monkeypatch, fail, fixture,
                                                                      expected):
    events = []
    code, row, calls = database_run(tmp_path, monkeypatch, events, nextest_passes, fail, fixture)
    assert code == 1 and row["outcome"] == "fail" and not calls
    assert [event for event, _ in events] == expected
    assert row["note"].startswith("database profile shadow-refresh setup failed: ")
    assert "secret" not in json.dumps(row)


def test_a_drop_that_fails_is_reported_but_leaves_the_evidence_alone(tmp_path, monkeypatch, capsys):
    # The runner's embedded cluster ends with the job; the tests' verdict stands.
    events = []
    code, row, _ = database_run(tmp_path, monkeypatch, events, nextest_passes, {"drop"})
    assert code == 0 and row["outcome"] == "pass"
    assert "could not drop" in capsys.readouterr().err


@pytest.mark.parametrize("windows, library", [(True, "onnxruntime.dll"), (False, "libonnxruntime.so.1.27.0")])
def test_the_onnx_runtime_library_is_the_wheels_own(tmp_path, windows, library):
    # The wheel ships its shared library in onnxruntime/capi, as the
    # startup lane's shadow fixture finds it.
    (tmp_path / library).write_bytes(b"")
    (tmp_path / "onnxruntime_pybind11_state.pyd").write_bytes(b"")
    assert stress.onnx_runtime_library(tmp_path, windows) == tmp_path / library


def test_the_fixture_is_the_embedding_fixture_and_the_wheels_onnx_runtime(tmp_path, monkeypatch):
    capi = tmp_path / "site" / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    library = capi / ("onnxruntime.dll" if os.name == "nt" else "libonnxruntime.so.1.27.0")
    library.write_bytes(b"")
    created = []
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(__file__=str(capi.parent / "__init__.py")))
    monkeypatch.setitem(sys.modules, "embedding_fixture",
                        SimpleNamespace(create=lambda path, dimension: created.append((path, dimension))))
    env = stress.fixture_environment(stress.CARGO_DATABASE_PROFILES["shadow-refresh"], tmp_path / "fixture")
    assert created == [(tmp_path / "fixture" / "model", 1024)]
    assert env == {"PL_SHADOW_EMBED_FIXTURE": str(tmp_path / "fixture" / "model"), "ORT_DYLIB_PATH": str(library)}


def test_a_missing_onnx_runtime_library_is_named(tmp_path, monkeypatch):
    capi = tmp_path / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(__file__=str(capi.parent / "__init__.py")))
    monkeypatch.setitem(sys.modules, "embedding_fixture", SimpleNamespace(create=lambda path, dimension: None))
    with pytest.raises(FileNotFoundError, match="onnxruntime==1.27.0"):
        stress.fixture_environment(stress.CARGO_DATABASE_PROFILES["shadow-refresh"], tmp_path)


def test_a_database_selection_gets_postgres_and_the_fixture_dependencies():
    jobs = raw_workflow()["jobs"]
    assert jobs["stress-plan"]["outputs"]["postgres"] == "${{ steps.plan.outputs.postgres }}"
    loop = jobs["stress"]
    install = step_named(loop, "Install checkout and test dependencies")
    prepare = step_named(loop, "Prepare disposable PostgreSQL")
    # lease_ci.py and pgdisposable import the checkout (pg0, psycopg); the
    # checkout install also brings the fixture's numpy, onnx and tokenizers
    # and the wheel whose library ORT_DYLIB_PATH names.
    assert install["if"] == prepare["if"] == CARGO_POSTGRES
    assert '-e ".[dev,lite]"' in install["run"]
    assert '"onnx==1.22.0"' in install["run"] and '"onnxruntime==1.27.0"' in install["run"]
    assert "python rust/cli_harness/lease_ci.py" in prepare["run"]
    names = [step.get("name") for step in loop["steps"]]
    assert names.index(install["name"]) < names.index(prepare["name"]) < names.index("Run the selection once")
