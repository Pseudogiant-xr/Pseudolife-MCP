"""A hung test should leave a traceback in a log that survives.

The 2026-09-22 master run of PR #330 (Actions run 35683291422) sat in the
``test`` job until the 50-minute timeout cancelled it, and GitHub stored no
log for the cancelled job: nothing said which test hung or where.
``faulthandler_timeout`` makes pytest dump every thread's stack when one
test item (setup + call + teardown) outlives it. The dump is diagnostic
only: the test keeps running and can still pass. It works from inside
xdist workers too (checked 2026-09-25 with ``-n 2 --dist loadfile``).
``ops/ci_tests.sh`` ends pytest at 50 minutes (72 with the frozen test files)
with SIGABRT inside the step,
so a hang fails the step (keeping its log) instead of being cancelled with
the job, and every pytest process dumps its stacks as it dies (checked
2026-09-25 on Linux: ``timeout`` exits 124 after the dump). Neither helps
if the runner itself stops responding.
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


# What runs before pytest in a CI job: install, plus a cold embedder
# download with the warm-up step's retries (~2 min warm, up to ~10 cold).
_CI_SETUP_ALLOWANCE_S = 600


def _ci_bounds() -> tuple[int, int, int]:
    """(kill grace, live-files bound, frozen-inclusive bound) in seconds, from
    ops/ci_tests.sh: the bound is chosen by the frozen switch, then passed to
    the timeout that runs the suite's pytest."""
    script = (REPO / "ops" / "ci_tests.sh").read_text(encoding="utf-8")
    m = re.search(r"timeout --signal=ABRT --kill-after=(\d+)s \"\$bound\"\s*\\\s*\n\s*"
                  r"python -m pytest", script)
    assert m, "ops/ci_tests.sh does not bound the pytest run with timeout --signal=ABRT"
    live = re.search(r"^bound=(\d+)m$", script, re.MULTILINE)
    frozen = re.search(r'^if \[\[ "\$\{PSEUDOLIFE_CI_INCLUDE_FROZEN:-\}" == true \]\]; then\n'
                       r"    frozen=\(--include-frozen\)\n    bound=(\d+)m\nfi$", script, re.MULTILINE)
    assert live and frozen, "ops/ci_tests.sh does not set the bound for both selections"
    return int(m.group(1)), int(live.group(1)) * 60, int(frozen.group(1)) * 60


def test_ci_ends_a_hung_run_inside_the_step_after_the_dump():
    # The step must end before the job timeout (or the log is lost with the
    # cancelled job), counting setup and the kill grace, and after the
    # faulthandler dump. SIGABRT makes every process dump its stacks as it
    # dies, which TERM would not.
    grace, live, frozen = _ci_bounds()
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs = [int(t) * 60 for t, _ in re.findall(
        r"timeout-minutes:\s*(\d+)((?:(?!timeout-minutes)[\s\S])*?)run: bash ops/ci_tests\.sh",
        workflow)]
    assert jobs, "no ci.yml job runs ops/ci_tests.sh"
    for bound in (live, frozen):
        assert _faulthandler_timeout() < bound
        assert _CI_SETUP_ALLOWANCE_S + bound + grace <= min(jobs)


# The slowest legitimate pytest run of each selection in the `test` job, 2
# xdist workers on a 4-CPU ubuntu-latest runner. Live files: 30:35, the
# slowest of three batch runs on 2026-11-24/25 (jobs 138165166960,
# 139459508063, 139591914723). Frozen-inclusive: on 2026-11-26 the old
# 50-minute bound killed both AMD EPYC 7763 attempts of the 0.26.0 cut batch
# (run 45747873168) at 99% after 50:28 and 50:12 with only seconds of tests
# left, so a whole run on that CPU takes at least about 51 minutes.
_SLOWEST_LIVE_RUN_S = 30 * 60 + 35
_SLOWEST_FROZEN_INCLUSIVE_RUN_S = 51 * 60


def test_each_bound_clears_the_slowest_run_of_its_selection_with_headroom():
    # About 40% headroom, the rule ops/ci_tests.sh's comment states, so a
    # runner CPU somewhat slower than the slowest measured still finishes.
    _, live, frozen = _ci_bounds()
    assert live >= 1.4 * _SLOWEST_LIVE_RUN_S
    assert frozen >= 1.4 * _SLOWEST_FROZEN_INCLUSIVE_RUN_S


def test_the_timeout_clears_the_slowest_legitimate_test():
    # 71.6 s is the slowest test item across four green master runs
    # (2026-09-23/24). The 300 s floor keeps a false dump off a slow runner.
    assert _faulthandler_timeout() >= 300
