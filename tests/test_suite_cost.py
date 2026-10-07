"""evals/suite_cost.py samples the process the tests actually run in."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import suite_cost  # noqa: E402

# The base interpreter, never a Windows venv launcher: the child IS the
# process to sample, as when pytest runs directly (Linux, CI, no venv).
PYTHON = getattr(sys, "_base_executable", sys.executable)
HOLD = "import time; block = bytearray(64 * 2**20); time.sleep(1.0)"


def test_a_short_direct_run_is_sampled_from_the_start():
    child = subprocess.Popen([PYTHON, "-c", HOLD])
    sampler = suite_cost.Sampler(psutil.Process(child.pid), 0.05,
                                 expect_launcher=False)
    sampler.start()
    child.wait()
    sampler.stop.set()
    sampler.join()

    assert sampler.samples >= 3
    assert sampler.peak_root_private >= 64 * 2**20


def test_a_run_with_no_samples_reports_unknown_peaks_not_zero():
    sampler = suite_cost.Sampler(psutil.Process(), 0.05,
                                 expect_launcher=False)

    assert suite_cost.peaks(sampler) == {
        "peak_pytest_private_gb": None, "peak_children_private_gb": None,
        "peak_tree_private_gb": None, "peak_pytest_threads": None,
    }
