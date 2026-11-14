"""Independent judge failures must survive later successful phases."""
import copy
import json
from pathlib import Path

import pytest
import yaml

import judge_summary as summary

PHASES = ("oracle-setup", "lease-check", "wait-mail-tests", "lease-headers", "cli-argv",
          "phase1-judges", "phase2-dispatch", "cli-process")


def fixture(tmp_path):
    checkout, temporary = tmp_path / "checkout", tmp_path / "temporary"
    checkout.mkdir()
    temporary.mkdir()
    (temporary / "junit").mkdir()
    (temporary / "junit/run-unchanged-candidates-and-differential-judges.xml").write_text(
        '<testsuites><testsuite><testcase classname="wait_mail" name="case-a"/></testsuite></testsuites>')
    documents = {
        "lease-header-policy.json": {"cases": [{"case": {"id": "header-a"}, "passed": True, "controls_rejected": [True]}]},
        "rust-cli-argv.json": {"cases": [{"case": {"id": "argv-a"}, "differences": []}]},
        "rust-parity.json": {"differences": [], "process_tests": {"outcomes": []}},
        "phase2b-cli-process.json": {"records": [{"id": "lease-a", "passed": True}], "candidate_output_controls": []},
    }
    for name, value in documents.items():
        (temporary / name).write_text(json.dumps(value))
    path = checkout / "evals/results/rust-phase2-ci-Windows.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"python_self_replay": {"differences": []}, "candidate_replay": {"differences": []},
                               "judge_sensitivity_controls": [], "process_tests": {}}))
    manifest = {"schema": 1, "phases": [{"name": name, "exit_code": 0, "seconds": 1.0} for name in PHASES]}
    return checkout, temporary, manifest


def test_failed_lease_and_later_argv_are_both_reported(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    manifest["phases"][3]["exit_code"] = 3
    manifest["phases"][4]["exit_code"] = 5
    (temporary / "lease-header-policy.json").write_text(json.dumps({"cases": [
        {"case": {"id": "header-a"}, "passed": False, "controls_rejected": []}]}))
    (temporary / "rust-cli-argv.json").write_text(json.dumps({"cases": [
        {"case": {"id": "argv-a"}, "differences": [{"path": "/stdout"}]}]}))
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["failed_phases"] == ["lease-headers", "cli-argv"]
    assert result["exit_code"] == 3
    assert [p["phase"] for p in result["phases"]] == list(PHASES)
    reports = {p["phase"]: p for p in result["phases"]}
    assert reports["lease-headers"]["failed_cases"] == ["header-a"]
    assert reports["cli-argv"]["failed_cases"] == ["argv-a"]
    assert reports["cli-process"]["outcome"] == "passed"


def test_missing_or_torn_receipts_do_not_erase_other_phases(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    manifest["phases"][3]["exit_code"] = 1
    (temporary / "lease-header-policy.json").write_text('{"cases":')
    (temporary / "rust-cli-argv.json").unlink()
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert len(result["phases"]) == 8 and result["exit_code"] == 1
    assert result["phases"][3]["diagnostic_errors"]
    assert result["phases"][4]["diagnostic_errors"]
    assert result["phases"][-1]["outcome"] == "passed"


def test_observer_does_not_replace_a_successful_command_verdict(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    (temporary / "rust-cli-argv.json").unlink()
    assert summary.summarize(manifest, checkout, temporary, "Windows")["exit_code"] == 0


@pytest.mark.parametrize("phase", PHASES)
def test_an_incomplete_phase_never_reports_success(tmp_path, phase):
    checkout, temporary, manifest = fixture(tmp_path)
    next(row for row in manifest["phases"] if row["name"] == phase)["exit_code"] = None
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["exit_code"] != 0 and phase in result["failed_phases"]


def test_failed_resource_gate_retains_its_exit_and_marks_later_work_unrun(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    manifest["phases"] = manifest["phases"][:2]
    manifest["phases"][1]["exit_code"] = 75
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["exit_code"] == 75
    assert all(row["outcome"] == "not_run" for row in result["phases"][2:])


def test_negative_controls_and_raw_streams_are_not_reported_as_failures(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    path = checkout / "evals/results/rust-phase2-ci-Windows.json"
    payload = json.loads(path.read_text())
    payload["judge_sensitivity_controls"] = [{"case": "expected-mutant", "rejected": True,
                                              "differences": [{"path": "/stdout"}]}]
    payload["private_stream"] = "synthetic-private-value"
    path.write_text(json.dumps(payload))
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["failed_phases"] == []
    assert result["phases"][6]["failed_cases"] == []
    assert "synthetic-private-value" not in json.dumps(result)


def test_every_receipt_shape_names_its_actual_failed_case_ids(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    (temporary / "junit/run-unchanged-candidates-and-differential-judges.xml").write_text(
        '<testsuites><testsuite><testcase classname="wait_mail" name="case-a"><failure/></testcase></testsuite></testsuites>')
    (temporary / "rust-parity.json").write_text(json.dumps({"differences": [{"case": "stdio-a"}],
        "process_tests": {"outcomes": [{"nodeid": "tests/test_a.py::test_one", "outcome": "failed"}]}}))
    path = checkout / "evals/results/rust-phase2-ci-Windows.json"
    payload = json.loads(path.read_text())
    payload["candidate_replay"]["differences"] = [{"case": "dispatch-a"}]
    payload["judge_sensitivity_controls"] = [{"case": "control-a", "rejected": False}]
    path.write_text(json.dumps(payload))
    (temporary / "phase2b-cli-process.json").write_text(json.dumps({"records": [{"id": "lease-a", "passed": False}]}))
    reports = {row["phase"]: row for row in summary.summarize(manifest, checkout, temporary, "Windows")["phases"]}
    assert reports["wait-mail-tests"]["failed_cases"] == ["wait_mail::case-a"]
    assert reports["phase1-judges"]["failed_cases"] == ["stdio-a", "tests/test_a.py::test_one"]
    assert reports["phase2-dispatch"]["failed_cases"] == ["control-a", "dispatch-a"]
    assert reports["cli-process"]["failed_cases"] == ["lease-a"]


@pytest.mark.parametrize("change", [
    lambda manifest: manifest.update(schema=True),
    lambda manifest: manifest["phases"].append(copy.deepcopy(manifest["phases"][0])),
    lambda manifest: manifest["phases"][0].update(exit_code="synthetic-private-value"),
    lambda manifest: manifest["phases"][0].update(seconds=float("nan")),
    lambda manifest: manifest["phases"][0].update(seconds=10 ** 1000),
    lambda manifest: manifest["phases"][0].pop("seconds"),
    lambda manifest: manifest["phases"][0].update(seconds=None),
])
def test_malformed_completion_records_fail_without_copying_private_values(tmp_path, change):
    checkout, temporary, manifest = fixture(tmp_path)
    change(manifest)
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["exit_code"] != 0 and len(result["phases"]) == 8
    assert "synthetic-private-value" not in json.dumps(result)


def test_cli_keeps_a_record_for_every_phase_when_the_exporter_crashes(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    manifest["phases"][3]["exit_code"] = 1
    (temporary / "lease-header-policy.json").unlink()
    path = temporary / "manifest.json"
    path.write_text(json.dumps(manifest))
    output, markdown = temporary / "summary", temporary / "summary.md"
    assert summary.main(["--manifest", str(path), "--checkout", str(checkout), "--temporary", str(temporary),
                         "--platform", "Windows", "--out-dir", str(output), "--summary", str(markdown)]) == 1
    assert {p.stem for p in output.glob("*.json")} == {"summary", *PHASES}
    assert "lease-headers" in markdown.read_text()


def test_missing_manifest_does_not_claim_the_phases_never_ran(tmp_path):
    checkout, temporary, _ = fixture(tmp_path)
    result = summary.summarize(None, checkout, temporary, "Windows")
    assert result["exit_code"] != 0
    assert all(row["outcome"] == "unverified" and row["failed_cases"] == [] for row in result["phases"])


def test_intentional_platform_skips_keep_the_existing_pytest_exit_semantics(tmp_path):
    checkout, temporary, manifest = fixture(tmp_path)
    (temporary / "junit/run-unchanged-candidates-and-differential-judges.xml").write_text(
        '<testsuites><testsuite><testcase name="other-platform"><skipped/></testcase></testsuite></testsuites>')
    result = summary.summarize(manifest, checkout, temporary, "Windows")
    assert result["exit_code"] == 0 and result["phases"][2]["failed_cases"] == []


def test_no_workflow_runs_the_judges_since_the_python_freeze():
    # The differential judges compared the candidate with a live Python
    # oracle and left CI on 14 November 2026; this summariser stays as
    # source until the deletion stream removes the harness.
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/rust.yml").read_text())
    text = json.dumps(workflow)
    assert "judge_summary.py" not in text
    assert "Run unchanged candidates and differential judges" not in text
