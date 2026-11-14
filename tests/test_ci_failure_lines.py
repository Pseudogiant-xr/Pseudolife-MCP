"""ops/pytest_failure_lines.py names a failing test before the session ends.

Batch run 43525635914 (2026-11-14, issue #1408) was killed at its pytest
bound with one ``F`` in the progress line and no summary, so nothing named
the failing test. The plugin prints ``FAILED <node id> - <reason>`` as soon
as the controller hears the report. These run a child pytest under xdist
whose second test blocks until this test has seen that line, so the line
provably arrives while the session is still running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

CHILD = '''
import os, time
from pathlib import Path

def test_a_fails():
    assert 1 + 1 == 3, "arithmetic is broken"

def test_b_waits_for_the_parent():
    marker = Path(os.environ["RELEASE_MARKER"])
    deadline = time.monotonic() + 120
    while not marker.exists():
        assert time.monotonic() < deadline, "the parent never saw the FAILED line"
        time.sleep(0.1)
'''


def _run(tmp_path: Path, *plugin: str, wait: float = 60):
    (tmp_path / "test_child.py").write_text(CHILD, encoding="utf-8")
    # Its own empty ini: the repository's pytest settings and conftest stay out.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    marker = tmp_path / "release"
    env = {**os.environ, "RELEASE_MARKER": str(marker), "PYTHONPATH": str(REPO),
           "PSEUDOLIFE_CI_TEST_TIMES": str(tmp_path / "times.tsv"), "PSEUDOLIFE_SUITE_LOCK": "off"}
    # -n 1 --dist loadfile: both tests on one worker, in order, as the CI
    # lane runs whole files; the controller prints the line.
    argv = [sys.executable, "-m", "pytest", *plugin, "-q", "-n", "1", "--dist", "loadfile",
            "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "-c", str(tmp_path / "pytest.ini"), str(tmp_path / "test_child.py")]
    proc = subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    lines: list[str] = []
    seen = threading.Event()

    def read():
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if line.startswith("FAILED test_child.py::test_a_fails"):
                seen.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    saw_it_early = seen.wait(wait)
    before_release = list(lines)
    marker.write_text("go", encoding="utf-8")
    proc.wait(120)
    reader.join(10)
    return saw_it_early, before_release, lines, proc.returncode, tmp_path / "times.tsv"


def test_a_failing_test_is_named_while_the_session_still_runs(tmp_path):
    saw_it_early, before_release, lines, code, times = _run(tmp_path, "-p", "ops.pytest_failure_lines")
    assert saw_it_early, lines
    line = next(l for l in before_release if l.startswith("FAILED test_child.py::test_a_fails"))
    assert "arithmetic is broken" in line
    assert not any("short test summary" in l or " failed" in l for l in before_release), before_release
    assert code == 1
    rows = [row.split("\t") for row in times.read_text(encoding="utf-8").splitlines()]
    assert ["test_child.py::test_a_fails", "call", "failed"] == rows[[r[:2] for r in rows].index(
        ["test_child.py::test_a_fails", "call"])][:3]
    assert any(r[0] == "test_child.py::test_b_waits_for_the_parent" and r[1] == "call" for r in rows)


def test_without_the_plugin_nothing_names_it_before_the_summary(tmp_path):
    # The near miss the plugin exists for: a plain -q xdist run.
    # 15 s: with the plugin the line arrives in under a second.
    saw_it_early, before_release, lines, code, _ = _run(tmp_path, wait=15)
    assert not saw_it_early
    assert not any("test_a_fails" in l for l in before_release), before_release
    assert code == 1 and any("FAILED test_child.py::test_a_fails" in l for l in lines)


def test_ci_tests_sh_loads_the_plugin_and_records_the_times():
    script = (REPO / "ops" / "ci_tests.sh").read_text(encoding="utf-8")
    assert "-p ops.pytest_failure_lines" in script
    assert 'PSEUDOLIFE_CI_TEST_TIMES="$PWD/ci-results/test-times.tsv"' in script
