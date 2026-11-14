"""Name each failing test in the log the moment it fails, for CI.

Loaded only by ``ops/ci_tests.sh`` (``-p ops.pytest_failure_lines``). pytest
names failures in its end-of-session summary; when the run is killed before
that (the 35-minute bound in batch run 43525635914 on 2026-11-14, issue
#1408), the log keeps only an ``F`` in the progress line and nothing says
which test failed. This prints ``FAILED <node id> - <reason>`` (``ERROR``
for a setup or teardown failure, a crashed worker, or a file that fails to
collect) as soon as the report reaches the controller, under xdist too,
where the controller receives every worker's reports, and flushes it so it
is in the step log even if the run dies next.

With ``PSEUDOLIFE_CI_TEST_TIMES`` naming a file, it also appends one line per
test phase as it ends (node id, phase, outcome, seconds), so a killed run
still says where its time went; ``--durations`` prints only at the end.

A diagnostic must never fail the run: an unwritable times file is reported
once and then left alone.
Standard library and pytest only.
"""
from __future__ import annotations

import os

import pytest


def _reason(report) -> str:
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

    def _say(self, line: str) -> None:
        reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        writer = getattr(reporter, "_tw", None)
        if reporter is None or writer is None:      # -p no:terminal, or a pytest without _tw
            print(line, flush=True)
            return
        # Its own line: under -q the progress characters (`.F`) share one,
        # and write_line would append to it.
        if getattr(writer, "width_of_current_line", 0):
            writer.line()
        reporter.write_line(line)
        writer.flush()

    def _record(self, report: pytest.TestReport) -> None:
        if not self.times:
            return
        try:
            with open(self.times, "a", encoding="utf-8") as out:
                out.write(f"{report.nodeid}\t{report.when}\t{report.outcome}\t{report.duration:.3f}\n")
        except OSError as error:
            self._say(f"pytest_failure_lines: stopped recording test times in {self.times}: {error}")
            self.times = None

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._record(report)
        if not report.failed:
            return
        kind = "FAILED" if report.when == "call" else "ERROR"
        reason = _reason(report)
        self._say(f"{kind} {report.nodeid}" + (f" - {reason}" if reason else ""))

    @pytest.hookimpl(trylast=True)
    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A file that fails to import: xdist sends it through this hook, and
        # the other files keep running, so it too would wait for the summary.
        if report.failed:
            reason = _reason(report)
            self._say(f"ERROR {report.nodeid or '<collection>'}" + (f" - {reason}" if reason else ""))


def pytest_configure(config: pytest.Config) -> None:
    # Only the controller (or a run without xdist) prints: a worker's output
    # does not reach the step log, and the controller hears every report.
    if not hasattr(config, "workerinput"):
        config.pluginmanager.register(_FailureLines(config), "pseudolife-failure-lines")
