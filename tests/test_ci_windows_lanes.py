"""ci.yml's two Windows Python lanes, and what keeps them honest.

``test-lite-windows`` is a merge gate: it runs the product paths that differ
on Windows and nothing that retries. ``test-windows-nightly`` is not: it runs
the process-spawning installer, hook, board and test-tooling plumbing on a
schedule (or by hand) through ``ops/pytest_retry_once.py``, which retries
first-attempt failures once and keeps the first attempt on record. Both
pass ``-ra --durations=30`` and keep a junit artifact, so a Windows-only
failure names its test.

A test runs in at most one of the two. A file is listed whole in one lane,
or split: one lane names some of its tests and the other runs the file with
exactly those tests deselected (a lane may also name a few tests of a file
no Windows lane otherwise runs). ``--deselect`` matches node id prefixes, so a
split must never name a test whose name begins another's.
"""
from __future__ import annotations

import ast
import re
import shlex
import warnings
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
REQUIRED, NIGHTLY = "test-lite-windows", "test-windows-nightly"
WRAPPER = "ops/pytest_retry_once.py"

# The required lane's files on 2026-10-20, before the split: each keeps
# Windows coverage in one lane or the other unless it leaves the tree.
LANE_ON_2026_10_20 = {
    "test_memory_changes_hook", "test_coordination_turn_digest", "test_delegate_watch",
    "test_embedded_pg", "test_backup_cli", "test_shim", "test_schema_version",
    "test_credentials", "test_coordination_credentials", "test_coordination_adapter",
    "test_wait_mail_cli", "test_codex_hooks", "test_subagent_board_guard",
    "test_codex_hook_setup", "test_codex_hook_setup_edges", "test_update_cli",
    "test_ops_update_wrappers", "test_register_claude_desktop", "test_client_install_ux",
    "test_installer_shim_upgrade", "test_installer_existing_upgrade", "test_installer_plugin",
    "test_tool_annotations", "test_doctor_cli", "test_suite_lock", "test_codex_doorbell",
    "test_tunnel_bridge", "test_tunnel_cli", "test_tunnel_profiles", "test_tunnel_runtime",
    "test_tunnel_service",
}
# A test that skips itself everywhere but Windows.
WINDOWS_ONLY = re.compile(
    r"skipif\(\s*(?:os\.name\s*!=\s*[\"']nt[\"']|sys\.platform\s*!=\s*[\"']win32[\"'])")


def jobs():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def lane_argv(job_id):
    """The one step of a lane that runs its tests, as argv."""
    steps = [s for s in jobs()[job_id]["steps"]
             if "pytest" in s.get("run", "") or WRAPPER in s.get("run", "")]
    assert len(steps) == 1, f"{job_id} must run its tests in exactly one step"
    return shlex.split(steps[0]["run"])


def selection(argv):
    """(whole files, {file: named tests}, {file: deselected tests})."""
    files, named, deselected = set(), {}, {}
    for i, token in enumerate(argv):
        if token == "--deselect":
            path, _, test = argv[i + 1].partition("::")
            deselected.setdefault(path, set()).add(test)
        elif token.startswith("tests/") and argv[i - 1] != "--deselect":
            path, _, test = token.partition("::")
            if test:
                named.setdefault(path, set()).add(test)
            else:
                files.add(path)
    return files, named, deselected


def lanes():
    return selection(lane_argv(REQUIRED)), selection(lane_argv(NIGHTLY))


def top_level_tests(path):
    with warnings.catch_warnings():
        # Only names are read; a test file's escape-sequence warning is not ours.
        warnings.simplefilter("ignore")
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name.startswith(("test", "Test"))}


def test_every_listed_file_and_test_exists():
    for files, named, deselected in lanes():
        for path in files | set(named) | set(deselected):
            assert (ROOT / path).is_file(), path
        for path, tests in [*named.items(), *deselected.items()]:
            assert tests <= top_level_tests(path), (path, tests - top_level_tests(path))


def listed_files():
    return {path for files, named, _ in lanes() for path in files | set(named)}


def test_no_test_runs_in_both_windows_lanes():
    (r_files, r_named, r_desel), (n_files, n_named, n_desel) = lanes()
    assert not r_files & n_files, "a whole file is listed in both lanes"
    for (files, named, _), (other_files, other_named, other_desel) in (
            ((r_files, r_named, r_desel), (n_files, n_named, n_desel)),
            ((n_files, n_named, n_desel), (r_files, r_named, r_desel))):
        assert not set(named) & set(other_named), "a split names tests in both lanes"
        for path, tests in named.items():
            assert path not in files, f"{path} is listed whole and by test in one lane"
            # A file the other lane does not run is a pick, not a split.
            if path in other_files:
                assert other_desel.get(path) == tests, (
                    f"{path} is split: the other lane must run it with exactly these "
                    f"tests deselected: {sorted(tests)}")
    for path in set(r_desel) | set(n_desel):
        assert path in r_named or path in n_named, (
            f"{path}: tests deselected in one lane that the other lane does not run")


def test_a_split_names_no_test_that_prefixes_another():
    for _, named, _ in lanes():
        for path, tests in named.items():
            everything = top_level_tests(path)
            for test in tests:
                others = {t for t in everything if t != test and t.startswith(test)}
                assert not others, (
                    f"--deselect {path}::{test} would also drop {sorted(others)}; "
                    "name the test so no other test's name begins with it")


def test_no_file_lost_its_windows_coverage_in_the_split():
    listed = {Path(p).stem for p in listed_files()}
    present = {stem for stem in LANE_ON_2026_10_20 if (ROOT / "tests" / f"{stem}.py").exists()}
    assert present <= listed, sorted(present - listed)


def test_every_file_with_a_windows_only_test_runs_in_a_windows_lane():
    listed = listed_files()
    missing = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")
        if WINDOWS_ONLY.search(path.read_text(encoding="utf-8"))
        and path.relative_to(ROOT).as_posix() not in listed)
    assert not missing, (
        f"{missing} hold Windows-only tests that no Windows lane runs; add each to "
        f"{NIGHTLY} (or to {REQUIRED} when it is a product path that differs on Windows)")


def test_the_merge_gate_never_retries_and_the_nightly_lane_retries_once():
    required, nightly = lane_argv(REQUIRED), lane_argv(NIGHTLY)
    retrying = ("--reruns", "--lf", "--last-failed", "rerunfailures", WRAPPER)
    assert not any(flag in token for token in required for flag in retrying)
    assert required[:3] == ["python", "-m", "pytest"]
    assert nightly[:2] == ["python", WRAPPER]


def test_both_lanes_name_their_failures_and_keep_their_results():
    for job_id in (REQUIRED, NIGHTLY):
        argv = lane_argv(job_id)
        assert "-ra" in argv and "--durations=30" in argv, job_id
        upload = [s for s in jobs()[job_id]["steps"]
                  if s.get("uses", "").startswith("actions/upload-artifact@")]
        assert len(upload) == 1 and upload[0]["if"] == "always()", job_id
        results = upload[0]["with"]["path"]
        if job_id == REQUIRED:
            assert any(t.startswith(f"--junitxml={results}") for t in argv), job_id
        else:
            assert argv[2].startswith(results), "the wrapper writes its junit under the upload"


def test_the_nightly_lane_never_runs_for_a_pull_request_or_merge_group():
    job = jobs()[NIGHTLY]
    assert job["if"] == "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
    assert "needs" not in job


def test_both_lanes_run_their_postgres_tests_instead_of_skipping_them():
    # Hosted Windows has no service containers: without an embedded server
    # the shim-to-daemon and PG-backed coordination tests skip, and did,
    # unnoticed, before 2026-10-20. PSEUDOLIFE_REQUIRE_TEST_POSTGRES turns a
    # missing server into a failure.
    for job_id in (REQUIRED, NIGHTLY):
        steps = jobs()[job_id]["steps"]
        start = next(i for i, s in enumerate(steps) if "attach_or_start" in s.get("run", ""))
        tests = next(i for i, s in enumerate(steps)
                     if "pytest" in s.get("run", "") or WRAPPER in s.get("run", ""))
        assert start < tests, job_id
        assert "PSEUDOLIFE_TEST_DATABASE_URL=" in steps[start]["run"], job_id
        assert steps[tests]["env"]["PSEUDOLIFE_REQUIRE_TEST_POSTGRES"] == "1", job_id
