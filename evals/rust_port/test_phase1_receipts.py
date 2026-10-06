"""A pytest skip and an executable replacement cannot become passing evidence."""
import hashlib
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

import pytest

from evals.rust_port.phase1_receipts import candidate_bindings, candidate_identity, pytest_outcomes
from evals.rust_port.phase1_receipts import reusable_python_process_receipt


def test_actual_pytest_report_records_pass_skip_and_setup_error(tmp_path):
    source = tmp_path / "test_probe.py"
    source.write_text("import pytest\n"
                      "def test_pass():\n    assert True\n"
                      "def test_skip():\n    pytest.skip('synthetic skip')\n"
                      "def test_failure():\n    assert False, 'synthetic failure'\n"
                      "@pytest.fixture\ndef broken():\n    raise RuntimeError('synthetic setup error')\n"
                      "def test_error(broken):\n    pass\n")
    report = tmp_path / "report.xml"
    result = subprocess.run([sys.executable, "-m", "pytest", "--confcutdir", str(tmp_path),
        str(source), "--junitxml", str(report), "-q"], cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}, capture_output=True, timeout=30)
    assert result.returncode == 1
    outcomes = pytest_outcomes(report, ["test_probe.py::test_" + name for name in ("pass", "skip", "failure", "error")])
    assert outcomes["report_complete"]
    assert not outcomes["all_nodes_passed"]
    assert outcomes["outcomes"] == [{"nodeid": "test_probe.py::test_pass", "outcome": "passed"},
                                    {"nodeid": "test_probe.py::test_skip", "outcome": "skipped"},
                                    {"nodeid": "test_probe.py::test_failure", "outcome": "failed"},
                                    {"nodeid": "test_probe.py::test_error", "outcome": "error"}]
    assert outcomes["junit_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert "synthetic setup error" not in str(outcomes)


@pytest.mark.parametrize("xml", [None, "<invalid", "<testsuites/>",
    '<testsuite><testcase classname="tests.test_shim" name="test_a"/></testsuite>',
    '<testsuite><testcase classname="tests.test_shim" name="test_a"/>'
    '<testcase classname="tests.test_shim" name="test_a"/></testsuite>',
    '<testsuite><testcase classname="tests.test_shim" name="test_a"/>'
    '<testcase classname="tests.test_shim" name="test_b"/>'
    '<testcase classname="tests.test_shim" name="test_extra"/></testsuite>'])
def test_incomplete_report_is_not_a_pass(tmp_path, xml):
    report = tmp_path / "report.xml"
    if xml is not None:
        report.write_text(xml)
    outcomes = pytest_outcomes(report, ["tests/test_shim.py::test_a", "tests/test_shim.py::test_b"])
    assert not outcomes["report_complete"]
    assert not outcomes["all_nodes_passed"]


def test_exit_zero_skip_is_not_passing_node_evidence(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text('<testsuite><testcase classname="tests.test_shim" name="test_a">'
                      '<skipped message="private location"/></testcase></testsuite>')
    outcomes = pytest_outcomes(report, ["tests/test_shim.py::test_a"])
    assert outcomes["report_complete"]
    assert not outcomes["all_nodes_passed"]
    assert outcomes["outcomes"] == [{"nodeid": "tests/test_shim.py::test_a", "outcome": "skipped"}]
    assert "private location" not in str(outcomes)


def test_process_tests_binds_the_executable_and_rejects_a_zero_exit_skip(tmp_path, monkeypatch):
    from evals.rust_port import phase1
    nodes = ["tests/test_shim.py::test_a"]
    monkeypatch.setattr(phase1, "selected_nodes", lambda: nodes)
    routed = []
    def module_command(module, root, arguments):
        routed.append((module, root))
        return [sys.executable, "-m", module, *arguments]
    monkeypatch.setattr(phase1, "module_command", module_command)

    @contextmanager
    def process(argv, **kwargs):
        report = Path(argv[argv.index("--junitxml") + 1])
        report.write_text('<testsuite><testcase classname="tests.test_shim" name="test_a">'
                          '<skipped/></testcase></testsuite>')
        class Child:
            returncode = 0
            owned_cleanup = {"process_stopped": True, "subtree_stopped": True}
            def communicate(self, timeout):
                return b"private stdout", b"private stderr"
        yield Child()

    monkeypatch.setattr(phase1, "owned_process", process)
    result = phase1.process_tests([sys.executable], tmp_path, tmp_path / "capture.log")
    assert result["exit_code"] == 0
    assert routed == [("pytest", tmp_path)]
    assert not result["passed"]
    assert result["command_identity"]["executable_sha256"] == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    assert "private stdout" not in str(result)


def test_reuse_requires_current_instrument_and_actual_passed_outcomes():
    nodes = ["tests/test_shim.py::test_a"]
    identity = {"public_cli_module": True, "executable_sha256": "frozen"}
    current = {"schema": 2, "oracle_head": "pin", "capture_platform": {"os": "synthetic"},
               "instrument_sha256": {"phase1.py": "current"}, "baseline_instrument_sha256": {"daemon.py": "current"}}
    old = {**current, "arms": [{"command_identity": identity}], "process_tests": {
        "passed": True, "exit_code": 0, "report_complete": True, "all_nodes_passed": True,
        "nodes": nodes, "outcomes": [{"nodeid": nodes[0], "outcome": "passed"}],
        "command_identity": identity, "junit_sha256": "report"}}
    assert reusable_python_process_receipt(old, current, nodes, identity)
    assert not reusable_python_process_receipt({**old, "schema": 1}, current, nodes, identity)
    assert not reusable_python_process_receipt({**old, "instrument_sha256": {}}, current, nodes, identity)
    assert not reusable_python_process_receipt({**old, "baseline_instrument_sha256": {}}, current, nodes, identity)
    old["process_tests"]["outcomes"][0]["outcome"] = "skipped"
    assert not reusable_python_process_receipt(old, current, nodes, identity)


def test_clean_native_source_identity_changes_with_executable_and_rejects_dirty_source(tmp_path):
    root = tmp_path / "source"
    (root / "rust").mkdir(parents=True)
    source = root / "rust/Cargo.toml"
    source.write_text('[workspace]\nmembers = []\n')
    for args in (["init", "-q"], ["add", "rust"],
                 ["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com", "commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=10)
    executable = tmp_path / "candidate"
    executable.write_bytes(b"frozen executable")
    identity = candidate_identity([str(executable)], root)
    assert identity["source_head"] == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert identity["source_tree"] == subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    assert identity["source_files_sha256"] == {"rust/Cargo.toml": hashlib.sha256(source.read_bytes()).hexdigest()}
    from evals.rust_baseline.shim_measurement import candidate_source_identity
    measurement = candidate_source_identity(root)
    assert measurement["source_tree"] == identity["source_tree"]
    assert measurement["files_sha256"] == identity["source_files_sha256"]
    assert str(tmp_path) not in str(identity)
    executable.write_bytes(b"replacement executable")
    assert candidate_identity([str(executable)], root)["executable_sha256"] != identity["executable_sha256"]
    source.write_text('[workspace]\nmembers = []\n# changed\n')
    with pytest.raises(RuntimeError, match="committed clean Rust source"):
        candidate_identity([str(executable)], root)
    with pytest.raises(RuntimeError, match="committed clean Rust source"):
        candidate_source_identity(root)


def test_candidate_source_without_native_git_is_refused(tmp_path):
    executable = tmp_path / "candidate"
    executable.write_bytes(b"synthetic")
    with pytest.raises(RuntimeError, match="native Git checkout"):
        candidate_identity([str(executable)], tmp_path)


def test_bindings_cover_every_candidate_cell_and_process_tests():
    identity = {"executable_basename": "candidate", "executable_sha256": "frozen"}
    cell = {"era": "synthetic", "arm": "candidate", "command_identity": identity}
    receipt = {"arms": [cell, {**cell, "arm": "oracle"}], "eof": {"cells": [cell]},
               "faults": {"cells": [cell]}, "process_tests": {"command_identity": identity}}
    bindings = candidate_bindings(receipt, identity)
    assert all(len(bindings[key]) == 1 for key in ("real_bank_arms", "eof_cells", "fault_cells"))
    assert bindings["process_tests"] == identity
    receipt["eof"]["cells"] = [{**cell, "command_identity": {**identity, "executable_sha256": "replacement"}}]
    with pytest.raises(RuntimeError, match="identity changed in eof_cells"):
        candidate_bindings(receipt, identity)


def test_complete_judge_invokes_scenarios_binds_all_cells_and_keeps_raw_cells_private(tmp_path, monkeypatch):
    from evals.rust_port import phase1, full_bank, stdio_faults, stdio_process_controls, stdio_scenarios
    identity = {"executable_basename": "python", "executable_sha256": "frozen", "executable_bytes": 1,
                "public_cli_module": True, "arguments": ["-m", "pseudolife_memory.cli"]}

    def cells(count, *, startup=False, concurrent=False):
        result = []
        for index in range(count):
            cell = {"arm": "candidate", "command_identity": identity, "stdout_frames_b64": ["private raw cell"]}
            if startup:
                cell["case"] = "startup-" + str(index)
            else:
                cell["era"] = "era-" + str(index)
            if concurrent:
                cell["release_order"] = "AB"
            result.append(cell)
        return result

    monkeypatch.setattr(phase1, "require_phase1_source", lambda root: {"oracle_head": "pin"})
    monkeypatch.setattr(phase1, "runtime_metadata", lambda root: {})
    monkeypatch.setattr(phase1, "candidate_identity", lambda *args: identity)
    monkeypatch.setattr(phase1, "process_tests", lambda *args: {"passed": True, "command_identity": identity})
    monkeypatch.setattr(phase1, "corpus", lambda *args: {"arms": cells(2), "differences": []})
    monkeypatch.setattr(phase1, "eof", lambda *args: {"cells": cells(7), "differences": []})
    monkeypatch.setattr(stdio_faults, "run", lambda *args: {"cells": cells(10), "differences": []})
    scenarios = {"startup": {"cells": cells(7, startup=True), "differences": [], "policy": "strict"},
                 "concurrent": {"cells": cells(6, concurrent=True)}, "differences": [],
                 "contract_sha256": {"stdio_startup_contract.json": "startup", "stdio_concurrent_orders.json": "orders"}}
    calls = []
    def scenario_run(*args):
        calls.append(args)
        return scenarios
    monkeypatch.setattr(stdio_scenarios, "run", scenario_run)
    monkeypatch.setattr(stdio_process_controls, "run", lambda *args: {"controls": {}})
    monkeypatch.setattr(full_bank, "run", lambda *args, **kwargs: (None, {
        "status": "passed", "cases": [], "differences": [], "identity_proxy_validation": {},
        "oracle_source_check": {}, "graded_controls": {}}))
    private = tmp_path / "private.json"
    public = tmp_path / "public.json"
    monkeypatch.setattr(sys, "argv", ["phase1", "--out", str(private), "--public-out", str(public)])
    assert phase1.main() == 0
    receipt = json.loads(public.read_text())
    assert len(calls) == 1
    assert receipt["candidate_bindings"]["checked_cell_count"] == 32
    assert len(receipt["candidate_bindings"]["startup_cells"]) == 7
    assert len(receipt["candidate_bindings"]["concurrent_cells"]) == 6
    assert "private raw cell" not in public.read_text()
    assert "cells" not in receipt["scenarios"]["startup"]
    assert "concurrent" not in receipt["scenarios"]
    for name in ("stdio_startup_contract.json", "stdio_concurrent_orders.json"):
        assert receipt["instrument_sha256"][name] == hashlib.sha256(Path(phase1.__file__).with_name(name).read_bytes()).hexdigest()

    monkeypatch.setattr(sys, "argv", ["phase1", "--skip-faults", "--out", str(tmp_path / "incomplete.json")])
    assert phase1.main() == 1
    assert len(calls) == 2
    assert json.loads((tmp_path / "incomplete.json").read_text())["status"] == "incomplete"


@pytest.mark.parametrize("section", ["startup", "concurrent"])
def test_scenario_executable_replacement_is_refused(section):
    identity = {"executable_basename": "candidate", "executable_sha256": "frozen"}
    cell = {"arm": "candidate", "case": "scenario", "command_identity": {**identity, "executable_sha256": "changed"}}
    receipt = {"arms": [], "eof": {"cells": []}, "scenarios": {section: {"cells": [cell]}}}
    with pytest.raises(RuntimeError, match="identity changed in " + section):
        candidate_bindings(receipt, identity)
