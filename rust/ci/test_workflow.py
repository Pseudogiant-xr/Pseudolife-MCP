"""Keep every Rust gate while changing the workflow's execution graph."""

import copy
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/rust.yml"
SYSTEMS = ["ubuntu-latest", "windows-latest"]
RUST_CHECKS = (
    "Bind the selected Python interpreter for Rust fixtures",
    "Install the Python SDK fixture dependency",
    "Bind the event-selected Python oracle",
    "Prepare selected package metadata for the Rust identity contract",
    "Install pinned nextest in the runner temporary directory",
    "Check formatting", "Check all targets", "Clippy", "Nextest",
    "Check all targets without default features", "Clippy without default features",
    "Nextest without default features",
)
PARITY_CHECKS = {
    "Install checkout and test dependencies": None,
    "Bind the event-selected Python oracle": None,
    "Check CI coverage contract": "eval",
    "Run every eval harness test": "eval",
    "CLI differential harness": "cli",
    "Prepare disposable PostgreSQL for CLI lease row": "cli",
    "CLI lease differential harness": "cli",
    "Run unchanged candidates and differential judges": "judges",
}


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_shards_cover_both_systems_and_build_once():
    jobs = workflow()["jobs"]
    for name in ("rust", "candidate", "parity-checks", "parity"):
        assert jobs[name]["strategy"]["matrix"]["os"] == SYSTEMS
        assert jobs[name]["strategy"]["fail-fast"] is False
        assert not jobs[name].get("continue-on-error", False)
    assert jobs["rust"]["name"] == "Rust / ${{ matrix.os }}"
    assert jobs["parity"]["name"] == "Parity / ${{ matrix.os }}"
    assert jobs["parity-checks"]["needs"] == "candidate"
    assert jobs["parity-checks"]["strategy"]["matrix"]["suite"] == ["eval", "cli", "judges"]
    builds = [step for name in ("rust", "candidate", "parity-checks")
              for step in jobs[name]["steps"]
              if "cargo build --locked --release --bin pseudolife-stdio" in step.get("run", "")]
    assert len(builds) == 1
    assert builds[0]["run"].strip() == "cargo build --locked --release --bin pseudolife-stdio -j 4"


def test_original_checks_remain_gated_on_the_expected_shards():
    jobs = workflow()["jobs"]
    # Keep an editable check inventory, not a frozen historical workflow:
    # future rows and diagnostic flags may extend a check's command.
    for job, inventory in (("rust", dict.fromkeys(RUST_CHECKS)), ("parity-checks", PARITY_CHECKS)):
        for name, suite in inventory.items():
            matches = [step for step in jobs[job]["steps"] if step.get("name") == name]
            assert len(matches) == 1, name
            actual = matches[0]
            assert actual.get("run", "").strip(), name
            assert not actual.get("continue-on-error", False)
            assert actual.get("if") == (f"matrix.suite == '{suite}'" if suite else None)


def test_capture_remains_separate_from_ordinary_ci():
    capture = workflow()["jobs"]["capture-windows"]
    assert capture["name"] == "Capture / Windows / wait-mail"
    assert capture["runs-on"] == "windows-latest"
    assert capture["if"] == "github.event_name == 'workflow_dispatch' && inputs.mode == 'wait-mail'"
    assert "needs" not in capture


def test_artifact_is_from_this_run_and_executable_on_linux():
    job = workflow()["jobs"]["parity-checks"]
    download = next(s for s in job["steps"] if s.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {"name": "rust-shim-${{ runner.os }}", "path": "rust/target/release"}
    permission = next(s for s in job["steps"] if s.get("name") == "Restore executable permission")
    assert permission["if"] == "runner.os == 'Linux'"
    assert permission["run"] == "chmod +x rust/target/release/pseudolife-stdio"


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_required_parity_gate_fails_closed(result):
    gate = workflow()["jobs"]["parity"]
    assert gate["needs"] == ["rust", "candidate", "parity-checks"]
    assert gate["if"].startswith("always() &&")
    step = gate["steps"][0]
    assert step["env"] == {"GATE_RESULTS": "${{ toJSON(needs) }}"}
    import os
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs["parity-checks"]["result"] = result
    executed = subprocess.run(["pwsh", "-NoProfile", "-Command", step["run"]],
                              env=dict(os.environ, GATE_RESULTS=json.dumps(needs)),
                              capture_output=True, text=True)
    assert executed.returncode == (0 if result == "success" else 1), executed.stderr


def test_coverage_guard_rejects_a_removed_check(monkeypatch):
    changed = copy.deepcopy(workflow())
    steps = changed["jobs"]["parity-checks"]["steps"]
    steps[:] = [s for s in steps if s.get("name") != "CLI lease differential harness"]
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    with pytest.raises(AssertionError):
        test_original_checks_remain_gated_on_the_expected_shards()


def test_coverage_contract_allows_added_timing_output(monkeypatch):
    changed = copy.deepcopy(workflow())
    step = next(s for s in changed["jobs"]["parity-checks"]["steps"]
                if s.get("name") == "Run every eval harness test")
    step["run"] = step["run"].replace("--junitxml", "--durations=10 --junitxml")
    monkeypatch.setattr(__import__(__name__, fromlist=["workflow"]), "workflow", lambda: changed)
    test_original_checks_remain_gated_on_the_expected_shards()
