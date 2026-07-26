"""Guards on the regression gate's replicate count and committed baseline.

The gate compares a replicated run against `regression_gate.baseline.json`.
Both sides of that comparison are noisy — the judge is an LLM — so the
replicate count is not a cosmetic default, it is what decides whether the
gate is informative or just expensive noise.

Two failure modes, both observed on 2026-07-26:

* **Too few replicates.** At n=3 the margin was ~1.2 standard errors of
  the difference, roughly a 1-in-5 false-fail rate. The gate failed twice
  in a row on changes that provably could not have caused it, and the
  second failure was on *clean master*.
* **A zero-variance baseline.** `make_baseline` derives
  ``margin = max(0.03, 2 * std)``, so a baseline recording ``std == 0``
  silently pins the margin to the floor AND — because zero variance means
  every replicate returned the same number — tends to freeze whichever end
  of the range that run happened to hit. The retired baseline's cortex
  0.7051 turned out to be the *maximum* of the true distribution, hit 2
  times in 8, so every honest run afterwards looked like a regression.

These are cheap text/JSON assertions rather than a live run: the gate
itself costs ~3 minutes of GPU per replicate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "evals" / "regression_gate.ps1"
BASELINE = REPO / "evals" / "results" / "regression_gate.baseline.json"

DEFAULT_REPLICATES = 10


def _declared_default() -> int:
    m = re.search(r"param\(\[int\]\$Replicates\s*=\s*(\d+)", GATE.read_text(encoding="utf-8"))
    assert m, "could not find the -Replicates parameter declaration"
    return int(m.group(1))


def test_gate_defaults_to_enough_replicates():
    """A 3-replicate default is what made the gate cry wolf. Anyone who
    forgets the flag should still get a usable run."""
    assert _declared_default() == DEFAULT_REPLICATES


def test_committed_baseline_was_established_at_the_default_or_more():
    """A baseline measured at fewer replicates than the gate runs is not a
    like-for-like comparator — its mean is the noisier of the two."""
    n = json.loads(BASELINE.read_text(encoding="utf-8"))["n_replicates"]
    assert n >= DEFAULT_REPLICATES, (
        f"baseline established at {n} replicates, below the gate default "
        f"of {DEFAULT_REPLICATES} — re-establish it")


def test_committed_baseline_records_real_variance():
    """The load-bearing one. ``margin = max(0.03, 2 * std)``, so a
    zero-std baseline disables the gate's own calibration and pins the
    margin to the floor. It also means the run had no spread to average
    over, which is how the retired baseline ended up frozen at the maximum
    of the distribution rather than its centre."""
    arms = json.loads(BASELINE.read_text(encoding="utf-8"))["arms"]
    zero = [a for a, v in arms.items() if not v.get("std")]
    assert not zero, (
        f"arms with zero recorded variance: {zero}. A baseline whose "
        f"replicates all return the same number is not a measurement of "
        f"the mean — re-establish it with more replicates.")


def test_margin_follows_the_measured_spread():
    """Pins the relationship the gate relies on, so a hand-edited margin
    that no longer tracks the spread is caught."""
    arms = json.loads(BASELINE.read_text(encoding="utf-8"))["arms"]
    for arm, v in arms.items():
        expected = max(0.03, 2 * v["std"])
        assert abs(v["margin"] - round(expected, 4)) < 1e-6, (
            f"{arm}: margin {v['margin']} does not equal "
            f"max(0.03, 2*std={v['std']:.4f}) = {expected:.4f}")
