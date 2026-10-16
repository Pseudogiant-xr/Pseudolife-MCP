"""Publication uses an allowlist; raw comparison bytes remain private."""
import base64
import copy
import json
from pathlib import Path

import pytest

from evals.rust_port import cli_process


def receipt():
    secret_path = "/home/private-fixture/runtime"
    encoded = base64.b64encode(secret_path.encode()).decode()
    command = {"executable_sha256": "a" * 64, "executable_bytes": 123,
               "source_head": "b" * 40, "source_tree": "c" * 40, "public_cli_module": False}
    from evals.rust_port.cli_public import CLI_HELPERS
    files = {name: "d" * 64 for name in CLI_HELPERS}
    result = {"schema": 1, "passed": True, "oracle_head": "e" * 40, "source_head": "e" * 40, "oracle_schema": 54,
              "instrument_head": "b" * 40, "instrument_dirty": False,
              "production_source_matches_pin": True, "behavior_test_sources_match_pin": True,
              "capture_platform": {"os": "Linux", "architecture": "x86_64"},
              "capture_runtime": {"python": "3.11.15", "package_runtime_version": "0.16.1",
                                  "environment_kind": "virtualenv", "executable_sha256": "a" * 64,
                                  "source_origin_matches_selected_root": True,
                                  "distribution_versions": {"pseudolife-mcp": "0.16.1"}},
              "captured_at_utc": "2026-10-05T00:00:00Z", "policy": "cli-state-compared", "normalizations": [],
              "command_identities": {"candidate": command, "oracle": {**command, "public_cli_module": True}},
              "cli_instrument_binding": {"instrument": {"source_head": "b" * 40, "source_tree": "c" * 40,
                   "source_dirty": False, "source_root": secret_path, "source_files_sha256": files,
                   "source_files_git_blob": {key: "f" * 40 for key in files}}, "production_ownership": {
                       "source_head": "e" * 40, "source_tree": "c" * 40, "source_dirty": False,
                       "source_files_sha256": {key: "d" * 64 for key in (
                           "pseudolife_memory/credentials.py", "pseudolife_memory/storage/schema.py",
                           "tests/pg_defaults.py", "tests/fake_embedder.py")},
                       "source_files_git_blob": {key: "f" * 40 for key in (
                           "pseudolife_memory/credentials.py", "pseudolife_memory/storage/schema.py",
                           "tests/pg_defaults.py", "tests/fake_embedder.py")}}},
              "daemon_cleanup": {key: True for key in ("readiness_identity_verified", "daemon_stopped", "children_stopped", "database_dropped")},
              "records": [], "candidate_output_controls": []}
    result["daemon_cleanup"]["actual_child_runtime"] = copy.deepcopy(result["capture_runtime"])
    for case in cli_process.cases(("help", "version")):
        observation = {"request": case, "environment": {"HOME": secret_path}, "pre_files_b64": {"pyvenv.cfg": encoded},
                       "response": {"exit_code": 0, "stdout_b64": encoded, "stderr_b64": encoded,
                                    "post_files_b64": {"runtime.json": encoded}},
                       "execution": {"cwd": secret_path, "effective_argv": [secret_path], "command_identity": command}}
        result["records"].append({"id": case["id"], "mode": case["mode"], "passed": True, "differences": [],
                                  "oracle": {**observation, "execution": {**observation["execution"],
                                      "command_identity": {**command, "public_cli_module": True}}}, "candidate": observation})
        for field in ("exit_code", "stdout_b64", "stderr_b64", "post_files_b64"):
            result["candidate_output_controls"].append({"case": case["id"], "mode": case["mode"], "field": field,
                "mutation_arm": "candidate", "rejected": True, "differences": [{"path": "/" + field, "reason": "value"}]})
    result["private_path"] = secret_path
    result["private_bytes"] = encoded
    return result, secret_path, encoded


def test_projection_keeps_complete_installed_inventory_and_no_plain_or_encoded_paths():
    raw, path, encoded = receipt()
    before = copy.deepcopy(raw)
    summary = cli_process.public_summary(raw)
    serialized = json.dumps(summary)
    assert path not in serialized and encoded not in serialized
    assert raw == before
    assert len(summary["cases"]) == 28
    assert sum(row["mode"] == "version" for row in summary["cases"]) == 13
    assert len(summary["candidate_controls"]) == 112
    assert summary["status"] == "passed"


@pytest.mark.parametrize("missing", ["installed-case", "controls", "binding", "helper", "cleanup", "candidate"])
def test_incomplete_public_evidence_is_refused(missing):
    raw, _, _ = receipt()
    if missing == "installed-case":
        raw["records"].pop()
    elif missing == "controls":
        raw["candidate_output_controls"].pop()
    elif missing == "binding":
        raw.pop("cli_instrument_binding")
    elif missing == "helper":
        raw["cli_instrument_binding"]["instrument"]["source_files_sha256"].pop("evals/rust_port/cli_version.py")
    elif missing == "cleanup":
        raw["daemon_cleanup"].pop("database_dropped")
    else:
        raw["command_identities"]["candidate"].pop("executable_sha256")
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_workflow_uploads_only_allowlisted_cli_summary():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/rust.yml").read_text()
    upload = workflow.split("- name: Retain comparison summary", 1)[1]
    assert "phase2b-cli-process-public.json" in upload
    assert "phase2b-cli-process.json" not in upload
    invocation = next(line for line in workflow.splitlines() if "& $oraclePython -c $processBootstrap" in line)
    assert "--public-out $processPublicReceipt" in invocation


@pytest.mark.parametrize("invalid", ["private-version", "source-binding", "windows-ownership"])
def test_invalid_public_provenance_cannot_project_unchecked_values(invalid):
    raw, path, _ = receipt()
    if invalid == "private-version":
        raw["capture_runtime"]["package_runtime_version"] = path
        raw["capture_runtime"]["distribution_versions"]["pseudolife-mcp"] = path
    elif invalid == "source-binding":
        raw["cli_instrument_binding"]["instrument"]["source_head"] = "0" * 40
    else:
        raw["capture_platform"]["os"] = "Windows"
    with pytest.raises(ValueError):
        cli_process.public_summary(raw)


def test_failed_case_reports_only_safe_outcome_without_comparison_details():
    raw, path, encoded = receipt()
    raw["passed"] = False
    raw["records"][0].update(passed=False, differences=[{"path": path, "oracle": encoded, "candidate": path}])
    summary = cli_process.public_summary(raw)
    assert summary["status"] == "failed"
    assert summary["cases"][0]["difference_count"] == 1
    assert path not in json.dumps(summary) and encoded not in json.dumps(summary)
