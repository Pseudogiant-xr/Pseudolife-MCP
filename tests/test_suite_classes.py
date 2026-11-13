"""Every test file is classified frozen or live, and only live ones collect by default."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import suite_classes, suite_lock

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def test_every_test_file_is_classified_exactly_once_and_every_listed_one_exists():
    classes = suite_classes.load()
    on_disk = {path.name for path in TESTS.glob("test_*.py")}
    assert sorted(on_disk - set(classes)) == [], "classify each new test file in tests/suite_classes.txt"
    assert sorted(set(classes) - on_disk) == [], "a listed test file no longer exists; remove its line"
    names = [line.split()[1] for line in suite_classes.CLASSES_FILE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert names == sorted(names), "keep tests/suite_classes.txt in name order"


def test_this_guard_and_the_classifier_are_live():
    classes = suite_classes.load()
    assert classes["test_suite_classes.py"] == "live"
    assert classes["test_suite_lock.py"] == "live"


@pytest.mark.parametrize("line, message", [
    ("live tests/test_x.py", "want"), ("frozen test_x.txt", "want"), ("kept test_x.py", "want"),
    ("live test_x.py extra", "want"), ("live x.py", "want"),
])
def test_a_malformed_line_is_refused_with_its_number(tmp_path, line, message):
    path = tmp_path / "suite_classes.txt"
    path.write_text(f"# header\nlive test_a.py\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=rf"suite_classes.txt:3: {message}"):
        suite_classes.load(path)


def test_a_repeated_file_is_refused(tmp_path):
    path = tmp_path / "suite_classes.txt"
    path.write_text("live test_a.py\nfrozen test_a.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="classified twice"):
        suite_classes.load(path)


def _collect(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                           *args], cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env={**__import__("os").environ, "PSEUDOLIFE_SUITE_LOCK": "off"})


def test_naming_a_frozen_file_without_the_switch_is_refused_with_its_name():
    frozen = sorted(suite_classes.frozen())[0]
    result = _collect(f"tests/{frozen}")
    assert result.returncode != 0
    assert f"tests/{frozen}" in result.stderr + result.stdout
    assert suite_classes.SWITCH in result.stderr + result.stdout


def test_the_switch_collects_a_frozen_file():
    frozen = sorted(suite_classes.frozen())[0]
    result = _collect(suite_classes.SWITCH, f"tests/{frozen}")
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert f"tests/{frozen}::" in result.stdout


def test_a_directory_run_collects_live_files_only():
    # A light directory: two live files and one frozen, copied in place.
    live = sorted(name for name, kind in suite_classes.load().items() if kind == "live")
    result = _collect("tests", "-k", "test_every_test_file_is_classified or test_label_pair")
    collected = {line.split("::", 1)[0].removeprefix("tests/") for line in result.stdout.splitlines()
                 if line.startswith("tests/test_")}
    assert collected and collected <= set(live), sorted(collected - set(live))


def test_ci_and_the_launchers_name_only_live_files():
    classes = suite_classes.load()
    named = set()
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        named |= set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", workflow.read_text(encoding="utf-8")))
    frozen = sorted(name for name in named if classes.get(name) == "frozen")
    assert frozen == [], f"a workflow names frozen test files (refused at collection): {frozen}"


def test_the_suite_lock_counts_a_full_run_against_the_live_files():
    live = sorted(TESTS / name for name, kind in suite_classes.load().items() if kind == "live")
    excluded = suite_classes.frozen()
    half = live[: (len(live) + 1) // 2]
    assert suite_lock.is_full_run([str(path) for path in half], ROOT, TESTS, excluded=excluded)
    assert not suite_lock.is_full_run([str(path) for path in half[:-1]], ROOT, TESTS, excluded=excluded)
    # Frozen files named beside them count for nothing.
    padded = [str(path) for path in half[:-1]] + [str(TESTS / name) for name in sorted(excluded)]
    assert not suite_lock.is_full_run(padded, ROOT, TESTS, excluded=excluded)
    assert suite_lock.is_full_run(["tests"], ROOT, TESTS, excluded=excluded)
