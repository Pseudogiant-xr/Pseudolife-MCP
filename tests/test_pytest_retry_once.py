"""ops/pytest_retry_once.py: one retry of first-attempt failures, both on record.

ci.yml's non-required ``test-windows-nightly`` lane runs through it, so the
lane can pass on a retry while the first attempt still shows how often each
test flakes. Each case runs the wrapper over a throwaway test file in its own
directory, outside this repository's pytest configuration.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "ops" / "pytest_retry_once.py"

TOY = '''
import os, pathlib
MARK = pathlib.Path(os.environ["TOY_MARK"])

def test_passes():
    pass

def test_flaky_once():
    if os.environ.get("TOY_FLAKY") and not MARK.exists():
        MARK.write_text("seen")
        assert False, "first attempt fails"

def test_always_fails():
    assert not os.environ.get("TOY_BROKEN"), "broken"
'''


def run(tmp_path, *, flaky=False, broken=False, source=TOY):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_toy.py").write_text(source, encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PYTEST_", "TOY_"))}
    env.update(TOY_MARK=str(tmp_path / "mark"), GITHUB_STEP_SUMMARY=str(summary))
    if flaky:
        env["TOY_FLAKY"] = "1"
    if broken:
        env["TOY_BROKEN"] = "1"
    result = subprocess.run([sys.executable, str(WRAPPER), "res", "-q", "test_toy.py"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    res = tmp_path / "res"
    record = json.loads((res / "attempts.json").read_text(encoding="utf-8"))
    return result, record, res, summary.read_text(encoding="utf-8")


def test_a_clean_first_attempt_is_not_retried(tmp_path):
    result, record, res, summary = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["first_attempt"] == {"exit": 0, "tests": 3, "failed": []}
    assert record["retry"] is None and not (res / "retry.xml").exists()
    assert (res / "first-attempt.xml").exists()
    assert "0 failed or errored" in summary


def test_a_test_that_passes_on_retry_passes_the_job_and_stays_on_record(tmp_path):
    result, record, res, summary = run(tmp_path, flaky=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert record["first_attempt"]["failed"] == ["test_toy::test_flaky_once"]
    assert record["flaky"] == ["test_toy::test_flaky_once"]
    assert record["still_failing"] == []
    # The retry runs only the first attempt's failures.
    assert record["retry"]["passed"] == ["test_toy::test_flaky_once"]
    assert "::warning title=Flaky on its first attempt::test_toy::test_flaky_once" in result.stdout
    # The first attempt's junit keeps the failure the final result hides.
    assert "first attempt fails" in (res / "first-attempt.xml").read_text(encoding="utf-8")
    assert "`test_toy::test_flaky_once`" in summary


def test_a_test_that_fails_twice_fails_the_job(tmp_path):
    result, record, _, summary = run(tmp_path, flaky=True, broken=True)
    assert result.returncode == 1
    assert record["flaky"] == ["test_toy::test_flaky_once"]
    assert record["still_failing"] == ["test_toy::test_always_fails"]
    assert "Failed on both attempts" in summary


def test_a_run_that_is_not_a_test_failure_is_returned_without_a_retry(tmp_path):
    # A module that cannot be imported is a collection error (exit 2), not a
    # test result: retrying it would only hide a broken lane.
    result, record, res, _ = run(tmp_path, source="import not_a_module_anywhere\n")
    assert result.returncode == 2
    assert record["first_attempt"]["exit"] == 2
    assert record["retry"] is None and not (res / "retry.xml").exists()
