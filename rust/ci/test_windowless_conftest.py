"""Every Python entry point under rust/ installs tests/windowless_children.py,
so on Windows the console children of a run that has no console open no
window on a developer's desktop: the conftest for pytest runs, and the three
harnesses that also run as scripts."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINTS = ("rust/conftest.py", "rust/cli_harness/__main__.py", "rust/daemon/harness/run.py",
                "rust/extractor_harness/run.py")


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_the_entry_point_installs_the_windowless_children_helper(entry):
    source = (ROOT / entry).read_text(encoding="utf-8")
    assert '"tests" / "windowless_children.py"))["install"]()' in source, entry
    assert (ROOT / "tests" / "windowless_children.py").is_file()


def test_the_helper_is_installed_in_this_run():
    # Off Windows install() leaves Popen alone; on Windows it marks it.
    marked = getattr(subprocess.Popen, "_pseudolife_windowless_children", False)
    assert marked is (os.name == "nt")
