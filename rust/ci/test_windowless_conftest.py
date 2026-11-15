"""rust/conftest.py installs tests/windowless_children.py, so the console
children of a Python test under rust/ open no window on a developer's
desktop when the run has no console (Windows)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_rust_conftest_installs_the_windowless_children_helper():
    source = (ROOT / "rust" / "conftest.py").read_text(encoding="utf-8")
    assert '"tests" / "windowless_children.py"' in source and "module.install()" in source
    assert (ROOT / "tests" / "windowless_children.py").is_file()


def test_the_helper_is_installed_in_this_run():
    # Off Windows install() leaves Popen alone; on Windows it marks it.
    import os
    marked = getattr(subprocess.Popen, "_pseudolife_windowless_children", False)
    assert marked is (os.name == "nt")
