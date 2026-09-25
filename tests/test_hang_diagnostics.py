"""A hung test should leave a traceback in a log that survives.

The 2026-09-22 master run of PR #330 (Actions run 35683291422) sat in the
``test`` job until the 50-minute timeout cancelled it, and GitHub stored no
log for the cancelled job: nothing said which test hung or where.
``faulthandler_timeout`` makes pytest dump every thread's stack when one
test item (setup + call + teardown) outlives it. The dump is diagnostic
only: the test keeps running and can still pass. It works from inside
xdist workers too (checked 2026-09-25 with ``-n 2 --dist loadfile``).
``ops/ci_tests.sh`` bounds pytest at 40 minutes inside the step, so a hang
fails the step (keeping its log) instead of being cancelled with the job.
Neither helps if the runner itself stops responding.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _faulthandler_timeout() -> float:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    value = config["tool"]["pytest"]["ini_options"].get("faulthandler_timeout")
    assert value is not None, (
        "pyproject.toml sets no faulthandler_timeout: a hung test leaves no "
        "traceback before the CI job timeout cancels the run")
    return float(value)


def _shortest_ci_job_timeout_seconds() -> int:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    minutes = [int(m) for m in re.findall(r"timeout-minutes:\s*(\d+)", text)]
    assert minutes, "no timeout-minutes in ci.yml"
    return min(minutes) * 60


def test_a_hung_test_dumps_its_stack_before_ci_cancels_the_job():
    assert _faulthandler_timeout() < _shortest_ci_job_timeout_seconds()


def test_ci_ends_a_hung_run_inside_the_step_after_the_dump():
    # The step must end before the job timeout (or the log is lost with the
    # cancelled job) and after the faulthandler dump (or there is nothing
    # to keep).
    script = (REPO / "ops" / "ci_tests.sh").read_text(encoding="utf-8")
    m = re.search(r"timeout --kill-after=\d+s (\d+)m\s*\\\s*\n\s*python -m pytest", script)
    assert m, "ops/ci_tests.sh does not bound the pytest run with timeout"
    bound = int(m.group(1)) * 60
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = [int(t) * 60 for t, _ in re.findall(
        r"timeout-minutes:\s*(\d+)((?:(?!timeout-minutes)[\s\S])*?)run: bash ops/ci_tests\.sh",
        workflow)]
    assert jobs, "no ci.yml job runs ops/ci_tests.sh"
    assert _faulthandler_timeout() < bound < min(jobs)


def test_the_timeout_clears_the_slowest_legitimate_test():
    # 71.6 s is the slowest test item across four green master runs
    # (2026-09-23/24). The 300 s floor keeps a false dump off a slow runner.
    assert _faulthandler_timeout() >= 300
