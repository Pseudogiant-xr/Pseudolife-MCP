"""External stdio routing fails closed without changing Python defaults."""
import json
from pathlib import Path
import subprocess
import sys

from evals.rust_port.harness import isolated_env


def test_explicit_stdio_selection_refuses_unmapped_function(tmp_path):
    source = tmp_path / "test_unmapped.py"
    source.write_text("def test_unmapped():\n    raise AssertionError('must not execute')\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "evals.rust_port.pytest_plugin",
        str(source), "--port-stdio-json", json.dumps([sys.executable]), "--confcutdir", str(tmp_path), "-q"],
        cwd=Path(__file__).resolve().parents[2], env=isolated_env(tmp_path / "home"),
        capture_output=True, timeout=15)
    assert result.returncode == 4
    assert b"selected tests have no process adapter" in result.stderr
    assert b"must not execute" not in result.stdout


def test_without_candidate_switch_unmapped_python_test_runs(tmp_path):
    source = tmp_path / "test_default.py"
    source.write_text("def test_default():\n    assert 1 + 1 == 2\n")
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "evals.rust_port.pytest_plugin",
        str(source), "--confcutdir", str(tmp_path), "-q"],
        cwd=Path(__file__).resolve().parents[2], env=isolated_env(tmp_path / "home"),
        capture_output=True, timeout=15)
    assert result.returncode == 0
    assert b"1 passed" in result.stdout
