"""The Parity guard rejects ignored, empty and deselected eval test files."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from .collection_guard import ROOT, uncollected_test_files


def test_every_eval_test_file_is_collected(request):
    if not request.config.getoption("--eval-collection-guard", default=False):
        pytest.skip("the whole-directory guard is enabled in Parity")
    assert uncollected_test_files(ROOT, request.session.items) == []


@pytest.mark.parametrize("omission", ["none", "ignore", "empty", "deselect", "new-nested"])
def test_actual_pytest_collection_rejects_omitted_eval_file(tmp_path, omission):
    port = tmp_path / "evals/rust_port"
    baseline = tmp_path / "evals/rust_baseline"
    port.mkdir(parents=True)
    baseline.mkdir(parents=True)
    (port / "test_port.py").write_text("def test_port(): pass\n")
    (baseline / "test_baseline.py").write_text("def test_baseline(): pass\n")
    arguments = ["evals/rust_port", "evals/rust_baseline", "--collect-only", "-q",
                 "--eval-collection-guard", "--confcutdir", str(tmp_path)]
    if omission == "ignore":
        arguments += ["--ignore", "evals/rust_baseline/test_baseline.py"]
    elif omission == "empty":
        (baseline / "test_baseline.py").write_text("# No test items.\n")
    elif omission == "deselect":
        arguments += ["--deselect", "evals/rust_baseline/test_baseline.py::test_baseline"]
    elif omission == "new-nested":
        nested = baseline / "new"
        nested.mkdir()
        (nested / "test_new.py").write_text("def test_new(): pass\n")
        arguments += ["--ignore", "evals/rust_baseline/new"]
    bootstrap = """
import pathlib, sys, pytest
from evals.rust_port import collection_guard
collection_guard.ROOT = pathlib.Path.cwd()
raise SystemExit(pytest.main(sys.argv[1:], plugins=[collection_guard]))
"""
    environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run([sys.executable, "-c", bootstrap, *arguments], cwd=tmp_path,
                            env=environment, capture_output=True, timeout=30)
    output = result.stdout + result.stderr
    assert result.returncode == (0 if omission == "none" else 4), output
    if omission != "none":
        assert b"uncollected eval test files:" in output
        assert (b"evals/rust_baseline/new/test_new.py" if omission == "new-nested" else
                b"evals/rust_baseline/test_baseline.py") in output
