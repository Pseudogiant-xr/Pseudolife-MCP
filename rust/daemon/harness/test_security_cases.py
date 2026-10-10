"""Security fixture and remote-bind admission safeguards."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

import run
import security_cases


ORDINARY = ("security-open", "security-closed", "security-encodings",
            "security-encoding-priority", "security-terminal-byte")
REMOTE = ("security-remote-open", "security-remote-auth")


def test_admission_cases_cannot_declare_a_security_refusal():
    ordinary = [c for name in ORDINARY for c in run.SCENARIOS[name]().cases()]
    assert len(ordinary) == 71
    for name in ORDINARY + REMOTE:
        scenario = run.SCENARIOS[name]()
        cases = scenario.cases()
        assert cases
        assert all(c["declared"] is None and "expected_status" in c for c in cases)
        assert scenario.unreachable_database and not scenario.settle


def test_source_control_witnesses_are_real_admission_cases():
    assert len(security_cases.CONTROLS) == 6
    for _, (scenario, witness, oracle_status, mutant_status) in security_cases.CONTROLS.items():
        c = next(c for c in run.SCENARIOS[scenario]().cases() if c["name"] == witness)
        assert c["expected_status"] == oracle_status
        assert oracle_status != mutant_status
        assert c["declared"] is None


def test_public_error_is_checked_even_when_both_arms_agree():
    c = run.SCENARIOS["security-open"]().cases()[0]
    response = {"status": c["expected_status"], "headers": {},
                "json": {"error": "wrong_refusal"}}
    assert run.compare_case(c, response, response)["diffs"]


@pytest.mark.parametrize("witness_caught", [False, True])
def test_security_mutant_requires_its_named_http_witness(tmp_path, monkeypatch, witness_caught):
    monkeypatch.setattr("sys.argv", ["run.py", "mutants", "--rust-bin", "not-executed",
                                    "--only", "security-open", "--mutants", "security-origin-open"])
    clean = {"scenario": "security-open", "cases": [{"case": "control", "diffs": []}],
             "db_diffs": []}
    changed = {"scenario": "security-open", "cases": [{
        "case": "foreign origin" if witness_caught else "unrelated",
        "python_status": 403, "rust_status": 404, "diffs": ["status differs"]}], "db_diffs": []}
    with patch.object(run.daemons, "scratch_root", return_value=tmp_path), \
            patch.object(run, "run_scenario", side_effect=[clean, changed]):
        assert run.main() == (0 if witness_caught else 1)


@pytest.mark.parametrize("name", REMOTE)
@pytest.mark.parametrize("reply", [subprocess.CompletedProcess([], 0, b"false\n", b""),
                                  subprocess.CompletedProcess([], 2, b"true\n", b""),
                                  subprocess.CompletedProcess([], 0, b"", b"")])
def test_remote_refuses_unsupported_artifact_before_bank_or_listener(tmp_path, name, reply):
    with patch("subprocess.run", return_value=reply), \
            patch.object(run.pg, "create") as create, \
            patch.object(run.daemons, "python_daemon") as python_launch, \
            patch.object(run.daemons, "rust_daemon") as rust_launch:
        with pytest.raises(RuntimeError, match="refuse before any wildcard listener"):
            run.run_scenario(run.SCENARIOS[name](), Path("not-executed"), tmp_path, "live", False)
    create.assert_not_called()
    python_launch.assert_not_called()
    rust_launch.assert_not_called()


@pytest.mark.parametrize("name,expected_exit,stdout", [
    ("PL_SECURITY_DEFAULT_BIN", 2, b""),
    ("PL_SECURITY_RELEASE_MUTANTS_BIN", 0, b"false\n"),
])
def test_release_artifacts_have_no_loopback_bind_override(tmp_path, name, expected_exit, stdout):
    candidate = os.environ.get(name)
    if not candidate:
        pytest.skip("requires an explicitly selected release artifact")
    env = run.daemons.base_env(tmp_path, {"PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES": "1",
                                        "PSEUDOLIFE_DAEMON_TEST_LOOPBACK_BIND": "1"})
    result = subprocess.run([candidate], env=env, capture_output=True, timeout=10)
    assert result.returncode == expected_exit
    assert result.stdout == stdout
