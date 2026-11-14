"""Name each failing test in the log the moment it fails, for CI.

Loaded only by ``ops/ci_tests.sh`` (``-p ops.pytest_failure_lines``). pytest
names failures in its end-of-session summary; when the run is killed before
that (the 35-minute bound in batch run 43525635914 on 2026-11-14, issue
#1408), the log keeps only an ``F`` in the progress line and nothing says
which test failed. This prints ``FAILED <node id> - <reason>`` (``ERROR``
for a setup or teardown failure) as soon as the report reaches the
controller, under xdist too, where the controller receives every worker's
reports, and flushes it so it is in the step log even if the run dies next.

With ``PSEUDOLIFE_CI_TEST_TIMES`` naming a file, it also appends one line per
test phase as it ends (node id, phase, outcome, seconds), so a killed run
still says where its time went; ``--durations`` prints only at the end.
Standard library and pytest only.
"""
from __future__ import annotations

import os

import pytest


def _reason(report: pytest.TestReport) -> str:
    """The one-line cause: the crash message's first line, if any."""
    crash = getattr(report.longrepr, "reprcrash", None)
    message = getattr(crash, "message", None)
    if message is None and report.longrepr is not None:
        lines = str(report.longrepr).strip().splitlines()
        message = lines[-1] if lines else ""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return first[:200]


class _FailureLines:
    def __init__(self, config: pytest.Config):
        self.config = config
        self.times = os.environ.get("PSEUDOLIFE_CI_TEST_TIMES") or None

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.times:
            with open(self.times, "a", encoding="utf-8") as out:
                out.write(f"{report.nodeid}\t{report.when}\t{report.outcome}\t{report.duration:.3f}\n")
        if not report.failed:
            return
        kind = "FAILED" if report.when == "call" else "ERROR"
        line = f"{kind} {report.nodeid}"
        reason = _reason(report)
        if reason:
            line += f" - {reason}"
        reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            # Its own line: under -q the progress characters (`.F`) share
            # one, and write_line would append to it.
            if getattr(reporter._tw, "width_of_current_line", 0):
                reporter._tw.line()
            reporter.write_line(line)
            reporter._tw.flush()
        else:                                   # -p no:terminal
            print(line, flush=True)


def pytest_configure(config: pytest.Config) -> None:
    # Only the controller (or a run without xdist) prints: a worker's output
    # does not reach the step log, and the controller hears every report.
    if not hasattr(config, "workerinput"):
        config.pluginmanager.register(_FailureLines(config), "pseudolife-failure-lines")
