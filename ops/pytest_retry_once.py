"""Run pytest, retry only its failures once, and record both attempts.

For CI lanes that are not merge gates (ci.yml's ``test-windows-nightly``):
the job passes when every first-attempt failure passes on its one retry,
and the first attempt stays on record so a flake rate can be read off the
artifacts. pytest-rerunfailures was not used because its junit report keeps
only the final result (checked 2026-10-20 with 15.1 and 16.7 on pytest 8.4
and 9.1): a test that failed and then passed reads as a plain pass.

Usage::

    python ops/pytest_retry_once.py <results dir> <pytest args...>

Writes, under the results directory:

* ``first-attempt.xml``: junit of the whole run;
* ``retry.xml``: junit of the retry, only when the first attempt failed;
* ``attempts.json``: exit codes, the first attempt's failed and errored
  tests, which of them passed on retry (``flaky``) and which did not.

A markdown summary goes to ``$GITHUB_STEP_SUMMARY`` when it is set, and
each flaky test gets a warning annotation. Exit status: 0 when the first
attempt passed; otherwise the retry's, and 1 whenever a first-attempt failure
did not pass on the retry (a skip, or not running at all, is not a pass). A
first attempt that ended any other
way than "some tests failed" (exit 1), such as a collection error, an
interrupt or no tests collected, is returned unchanged with no retry: only a
test result is retried.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TESTS_FAILED = 1  # pytest.ExitCode.TESTS_FAILED


def _pytest(args: list[str]) -> int:
    print("+ python -m pytest " + " ".join(args), flush=True)
    return subprocess.run([sys.executable, "-m", "pytest", *args]).returncode


def _results(junit: Path) -> dict[str, list[str]]:
    """Test ids (junit ``classname::name``) by outcome, each id once. A test
    that fails in its call and errors in teardown has two records; any
    failing record makes it failed."""
    outcome: dict[str, str] = {}
    if junit.exists():
        for case in ET.parse(junit).getroot().iter("testcase"):
            test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
            if case.find("failure") is not None or case.find("error") is not None:
                kind = "failed"
            elif case.find("skipped") is not None:
                kind = "skipped"
            else:
                kind = "passed"
            if outcome.get(test_id) != "failed":
                outcome[test_id] = kind
    out: dict[str, list[str]] = {"passed": [], "failed": [], "skipped": []}
    for test_id, kind in outcome.items():
        out[kind].append(test_id)
    return out


def _summary(record: dict) -> str:
    first, retry = record["first_attempt"], record["retry"]
    lines = ["### First attempt and one retry", "",
             f"First attempt: exit {first['exit']}, {first['tests']} tests, "
             f"{len(first['failed'])} failed or errored."]
    if retry is not None:
        lines.append(f"Retry of those: exit {retry['exit']}; "
                     f"{len(record['flaky'])} passed (flaky), "
                     f"{len(record['still_failing'])} failed again.")
    for title, ids in (("Flaky (failed, then passed)", record["flaky"]),
                       ("Failed on both attempts", record["still_failing"])):
        if ids:
            lines += ["", f"**{title}:**", *(f"- `{test_id}`" for test_id in ids)]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    results, args = Path(argv[0]), argv[1:]
    results.mkdir(parents=True, exist_ok=True)
    # A cache of its own, so --lf sees this run's failures and nothing older.
    cache = ["-o", f"cache_dir={(results / '.pytest-cache').as_posix()}"]
    first_xml, retry_xml = results / "first-attempt.xml", results / "retry.xml"
    first_exit = _pytest([*args, *cache, f"--junitxml={first_xml}"])
    first = _results(first_xml)
    record: dict = {
        "first_attempt": {"exit": first_exit, "tests": sum(map(len, first.values())),
                          "failed": first["failed"]},
        "retry": None, "flaky": [], "still_failing": [],
    }
    status = first_exit
    if first_exit == TESTS_FAILED:
        status = _pytest([*args, *cache, "--last-failed", "--last-failed-no-failures", "none",
                          f"--junitxml={retry_xml}"])
        retried = _results(retry_xml)
        record["retry"] = {"exit": status, "passed": retried["passed"],
                           "failed": retried["failed"]}
        passed = set(retried["passed"])
        record["flaky"] = [t for t in first["failed"] if t in passed]
        record["still_failing"] = [t for t in first["failed"] if t not in passed]
        # Only a pass on the retry clears a failure. A retry that skipped
        # it, or that ran a different id (a parametrize id that changed
        # between runs makes --last-failed run the whole file), can exit 0.
        if record["still_failing"] and status == 0:
            status = TESTS_FAILED
        for test_id in record["flaky"]:
            print(f"::warning title=Flaky on its first attempt::{test_id}", flush=True)
    (results / "attempts.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(_summary(record))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
