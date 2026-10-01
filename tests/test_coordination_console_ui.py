"""Execute the Console view's DOM behavior with Node's built-in assertions."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_coordination_console_dom_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the Console DOM behavior harness")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "tests/js/coordination_console.mjs"], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
