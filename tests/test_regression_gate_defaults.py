"""Guards on the regression gate's replicate count and committed baseline.

The gate compares a replicated run against `regression_gate.baseline.json`.
What the replicate count is *for* changed on 2026-07-27, so these guards
changed with it — the history is kept because both failure modes are real
and the second one nearly repeated.

**Before 2026-07-27 — replicates as an estimator.** The judge appeared
irreducibly noisy (cortex std ~0.033 on this slice), so the count decided
whether the gate was informative or expensive noise. Two failure modes,
both observed 2026-07-26: too few replicates (at n=3 the margin was ~1.2
standard errors, roughly a 1-in-5 false-fail rate — it failed twice running,
once on clean master), and a zero-variance baseline (``margin =
max(0.03, 2*std)``, so ``std == 0`` pinned the margin to the floor *and*
tended to freeze whichever end of the range that run hit; the retired
baseline's cortex 0.7051 was the maximum of the true distribution, hit 2
times in 8, so every honest run afterwards looked like a regression).

**After 2026-07-27 — replicates as a canary.** That spread was not the
judge. It was the TurboQuant fork's fused TBQ4_0 flash-attention KV cache,
which is not bit-reproducible (~7% of verdicts flip on identical input; MTP
and the prompt cache were both ruled out). `evals/qwen_server.ps1` now
serves the stock q8_0 config by default, and replicates of byte-identical
contexts return byte-identical scores.

So ``std == 0`` inverted meaning: it is now the REQUIRED state, and a
non-zero std is the alarm — it means the run drifted onto the fast fork,
which silently inflates every margin and stops the gate detecting what it
exists to detect.

Guarding against the obvious objection, because it is the same shape as the
2026-07-18 mistake: today's baseline records cortex 0.7051 / std 0, which is
numerically identical to the retired one. The difference is evidence.
The retired value came from replicates inside a single server process on a
nondeterministic build. The current one was measured across **7 replicates
spanning 4 separate server processes, with a full teardown between each**
(`regression_gate-2026-07-27-establish-q8-n7-crossrestart.agg.json`): all 7
identical on all 3 arms. A frozen extreme requires a distribution to be
extreme within; cold restarts would have exposed one.

These are cheap text/JSON assertions rather than a live run: the gate itself
costs ~3 minutes of GPU per replicate.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FROZEN_GATE = REPO / "evals" / "_python_regression_gate.ps1"
RUST_GATE = REPO / "evals" / "regression_gate.ps1"
BASELINE = REPO / "evals" / "results" / "regression_gate.baseline.json"
RUST_BASELINE = REPO / "evals" / "results" / "rust-quality" / "regression_gate.baseline.json"
# The reviewed -Establish candidate the maintainer accepted on 25 November 2026
# (run 3, 13:18 to 13:37 AEDT), hashed with LF line endings. Promoting a new
# baseline changes this in the same commit; any other edit to the file fails.
RUST_BASELINE_SHA256 = "164df6866acff69696a472ddfdf637e90ce2ea4e439559ad0ef4abc0cea551b4"

# 2: one run plus one canary. Not an estimator — see the module docstring.
DEFAULT_REPLICATES = 2


def _declared_default(gate: Path) -> int:
    m = re.search(r"param\(\s*\[int\]\$Replicates\s*=\s*(\d+)", gate.read_text(encoding="utf-8"))
    assert m, "could not find the -Replicates parameter declaration"
    return int(m.group(1))


def test_gate_default_keeps_a_determinism_canary():
    """The default must run at least two replicates. One replicate cannot
    disagree with anything, so it cannot detect a drift back onto the
    nondeterministic server — which is the only thing replicates still buy."""
    assert _declared_default(FROZEN_GATE) >= DEFAULT_REPLICATES


def test_rust_gate_default_keeps_a_determinism_canary():
    assert _declared_default(RUST_GATE) >= DEFAULT_REPLICATES


def test_committed_baseline_was_established_at_the_default_or_more():
    """A baseline measured at fewer replicates than the gate runs is not a
    like-for-like comparator."""
    n = json.loads(BASELINE.read_text(encoding="utf-8"))["n_replicates"]
    assert n >= DEFAULT_REPLICATES, (
        f"baseline established at {n} replicates, below the gate default "
        f"of {DEFAULT_REPLICATES} — re-establish it")


def test_committed_rust_baseline_is_the_reviewed_candidate():
    """The gate's own check accepts any in-range score, so only the reviewed
    file's digest catches a hand edit that would turn a regression into a pass."""
    data = RUST_BASELINE.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == RUST_BASELINE_SHA256, (
        "the committed Rust baseline is not the reviewed -Establish candidate; promote "
        "a new candidate by committing it unchanged and updating RUST_BASELINE_SHA256")


def test_committed_rust_baseline_passes_the_gates_own_check():
    """The committed Rust baseline is the file `regression_gate.py` loads when it
    is not establishing. The gate's own check accepts it against the manifest it
    records, so a promoted candidate with a missing arm, an off-floor margin or a
    malformed pin fails here rather than at the start of a GPU run. Whether the
    pins still match the live instruments is the gate's question, asked at run
    time."""
    from evals import regression_gate as gate

    baseline = json.loads(RUST_BASELINE.read_text(encoding="utf-8"))
    assert gate.BASELINE == RUST_BASELINE
    assert gate.baseline_for(RUST_BASELINE, baseline["manifest"]) == baseline
    assert baseline["replicates"] >= DEFAULT_REPLICATES
    assert baseline["manifest"]["policy"] == gate.POLICY
    assert set(baseline["manifest"]["instrument_sha256"]) == set(gate.instrument_digests())


def test_committed_baseline_is_free_of_judge_noise():
    """The load-bearing one, and the inverse of what it asserted before
    2026-07-27. Replicates re-judge byte-identical persisted contexts, so on
    the reproducible server every replicate scores the same. A baseline that
    records spread was measured on the turboq fork: its mean is then a draw
    from a distribution the gate cannot reproduce, and its inflated margin
    (2*std) quietly raises the bar for what counts as a regression."""
    arms = json.loads(BASELINE.read_text(encoding="utf-8"))["arms"]
    noisy = {a: v["std"] for a, v in arms.items() if v.get("std")}
    assert not noisy, (
        f"baseline arms record judge noise: {noisy}. Replicates of identical "
        f"contexts must agree exactly — re-establish with the reproducible "
        f"server (evals/qwen_server.ps1 default, NOT -Fast).")


def test_margin_follows_the_measured_spread():
    """Pins the relationship the gate relies on, so a hand-edited margin
    that no longer tracks the spread is caught. With a reproducible judge
    this resolves to the 0.03 floor on every arm."""
    arms = json.loads(BASELINE.read_text(encoding="utf-8"))["arms"]
    for arm, v in arms.items():
        expected = max(0.03, 2 * v["std"])
        assert abs(v["margin"] - round(expected, 4)) < 1e-6, (
            f"{arm}: margin {v['margin']} does not equal "
            f"max(0.03, 2*std={v['std']:.4f}) = {expected:.4f}")
