"""Restore the captured continuum seed into a fresh disposable bank.

The frozen writer stored these entries under the ``continuum`` preset.
Serving them with the default ``flat`` preset exercises band hydration.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from w2d_seed_rows import load_capture, replay  # noqa: E402

CAPTURE_SHA256 = "c41b4a93c2ddcbb14c642f872749489be90fec6966a69717633a1aa0f317bade"


def seed(dsn):
    capture = load_capture("continuum", CAPTURE_SHA256)
    replay(dsn, capture["bank"])


if __name__ == "__main__":
    seed(os.environ["PL_HARNESS_SEED_DSN"])
