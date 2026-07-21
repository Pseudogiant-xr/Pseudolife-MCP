"""Published benchmark numbers must be backed by committed evidence.

Two audits (2026-07-17, 2026-07-21) found the same failure twice: a number
reaches the docs while the run that produced it stays in a terminal or an
untracked working-copy file. Nothing contradicts such a claim, so no guard
test and no docs-currency pass ever surfaces it — a reader simply cannot
check it, and neither can we.

This pins the load-bearing published numbers to the artifacts they came
from. Deciding whether a number is *right* needs a GPU and stays a manual
gate; deciding whether it is *backed* is pure parsing, so it runs here.

Adding a benchmark claim to the docs means adding a row below. The
`needle` is verbatim doc text: if a rewrite drops it, the guard fails
rather than quietly stopping guarding.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

REPO = Path(__file__).resolve().parents[1]
RESULTS = "evals/results/"

# Artifact shorthands — every path is repo-relative so it can be checked
# against `git ls-files` directly.
CEILING = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v2.agg.json"
ARM1 = RESULTS + "longmemeval-ku-oracle-e4b-ft-arm1.agg.json"
ARM1_BASE = RESULTS + "longmemeval-ku-oracle-e4b-ft-arm1-baseline.agg.json"
LME_V2 = RESULTS + "lme-v2-smoke-slice1.agg.json"


def _abl(policy: str, mode: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-e4b-ft-arm1-abl-"
            f"{policy}-{mode}.agg.json")


def _abl_cmp(mode: str, arm: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-e4b-ft-arm1-abl-"
            f"{mode}-{arm}.compare.json")


@dataclass(frozen=True)
class Claim:
    """One published number and the artifact(s) that justify it."""

    id: str
    doc: str
    needle: str          # verbatim text in `doc` that states the number
    artifacts: tuple[str, ...]
    value: Callable[..., float]   # receives the loaded artifacts, in order
    stated: float        # the number exactly as published
    places: int          # decimals the doc rounds to

    def actual(self) -> float:
        loaded = [json.loads((REPO / a).read_text(encoding="utf-8"))
                  for a in self.artifacts]
        return self.value(*loaded)


def _mean(arm: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm]["mean"]


def _std(arm: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm]["std"]


def _delta(arm: str) -> Callable[[dict, dict], float]:
    """Continuum minus flat, the direction the ablation table publishes."""
    return lambda c, f: c["arms"][arm]["mean"] - f["arms"][arm]["mean"]


BENCH = "docs/guide/benchmarks.md"
READ_ME = "README.md"

# ── the local-ceiling table (README front door + guide) ───────────────────
_CEILING_ROWS = [
    ("rag", "| naive RAG (top-6 turns) | 0.567 ± 0.017 |", 0.567, 0.017),
    ("cortex", "| cortex facts only | 0.559 ± 0.029 |", 0.559, 0.029),
    ("hybrid",
     "| **hybrid (facts + top-3 turns)** | **0.710 ± 0.019** |", 0.710, 0.019),
]

CLAIMS: list[Claim] = []

for _doc, _slug in ((READ_ME, "readme"), (BENCH, "guide")):
    for _arm, _needle, _mean_v, _std_v in _CEILING_ROWS:
        CLAIMS.append(Claim(
            id=f"ceiling-{_slug}-{_arm}-mean", doc=_doc, needle=_needle,
            artifacts=(CEILING,), value=_mean(_arm), stated=_mean_v, places=3))
        CLAIMS.append(Claim(
            id=f"ceiling-{_slug}-{_arm}-std", doc=_doc, needle=_needle,
            artifacts=(CEILING,), value=_std(_arm), stated=_std_v, places=3))

# ── the replicated Arm-1 vs baseline table ───────────────────────────────
for _arm, _needle, _a_mean, _b_mean in [
    ("rag", "| naive RAG (control) | 0.574 ± 0.006 | 0.585 ± 0.015 |",
     0.574, 0.585),
    ("cortex", "| cortex facts only | 0.682 ± 0.017 | 0.603 ± 0.013 |",
     0.682, 0.603),
    ("hybrid", "| hybrid | 0.762 ± 0.027 | 0.749 ± 0.015 |", 0.762, 0.749),
]:
    CLAIMS.append(Claim(
        id=f"arm1-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(ARM1,), value=_mean(_arm), stated=_a_mean, places=3))
    CLAIMS.append(Claim(
        id=f"arm1-baseline-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(ARM1_BASE,), value=_mean(_arm), stated=_b_mean, places=3))

# ── the LongMemEval-V2 procedure slice ───────────────────────────────────
for _arm, _needle, _ku, _compose in [
    ("rag", "| naive RAG (control) | 0.300 [0.30–0.30] | 0.500 [0.40–0.60] |",
     0.300, 0.500),
    ("cortex", "| cortex facts only | 0.167 [0.00–0.30] | 0.233 [0.10–0.30] |",
     0.167, 0.233),
    ("hybrid",
     "| hybrid | **0.533 [0.50–0.60]** | **0.633 [0.60–0.70]** |",
     0.533, 0.633),
]:
    CLAIMS.append(Claim(
        id=f"lmev2-ku-{_arm}", doc=BENCH, needle=_needle, artifacts=(LME_V2,),
        value=_mean(f"KU.{_arm}"), stated=_ku, places=3))
    CLAIMS.append(Claim(
        id=f"lmev2-compose-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(LME_V2,), value=_mean(f"compose.{_arm}"),
        stated=_compose, places=3))

# ── the band-structure ablation (deltas AND their p-values) ──────────────
# The p-values are the load-bearing part of a *significance* claim, so they
# need an artifact of their own — a mean alone cannot justify "p = 0.015".
# Each cell carries its own decimal count: the table prints most p-values
# to 2 places but the significant one to 3, and a guard that rounded them
# alike would stop distinguishing 0.015 from 0.02.
for _arm, _needle, _wall, _hist in [
    ("rag", "| naive RAG | −0.067 | 0.10 | **−0.090** | **0.015** |",
     (-0.067, 0.10, 2), (-0.090, 0.015, 3)),
    ("cortex", "| cortex facts only | +0.008 | 0.76 | −0.010 | 0.53 |",
     (0.008, 0.76, 2), (-0.010, 0.53, 2)),
    ("hybrid", "| hybrid | −0.023 | 0.24 | +0.018 | 0.47 |",
     (-0.023, 0.24, 2), (0.018, 0.47, 2)),
]:
    for _mode, (_d, _p, _p_places) in (("wall", _wall), ("hist", _hist)):
        CLAIMS.append(Claim(
            id=f"ablation-{_mode}-{_arm}-delta", doc=BENCH, needle=_needle,
            artifacts=(_abl("continuum", _mode), _abl("flat", _mode)),
            value=_delta(_arm), stated=_d, places=3))
        CLAIMS.append(Claim(
            id=f"ablation-{_mode}-{_arm}-p", doc=BENCH, needle=_needle,
            artifacts=(_abl_cmp(_mode, _arm),),
            value=lambda d: d["p_value"], stated=_p, places=_p_places))


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, text=True,
                         capture_output=True)
    if out.returncode != 0:  # pragma: no cover - only without git
        pytest.skip("git unavailable")
    return set(out.stdout.split())


def test_every_published_number_names_a_committed_artifact():
    """A claim whose evidence is untracked cannot be checked by a reader.

    Working-copy-only files count as missing on purpose: `git ls-files`
    ignores them, which is exactly the state a fresh clone sees.
    """
    tracked = _tracked()
    missing = sorted({a for c in CLAIMS for a in c.artifacts
                      if a not in tracked})
    assert not missing, (
        "published benchmark numbers cite evidence that is not committed:\n  "
        + "\n  ".join(missing)
        + "\n\nCommit the artifact in the same change as the claim, or drop "
          "the claim from the docs.")


@pytest.mark.parametrize("claim", CLAIMS, ids=lambda c: c.id)
def test_published_number_matches_its_artifact(claim: Claim):
    for artifact in claim.artifacts:
        if not (REPO / artifact).exists():
            pytest.fail(f"{claim.id}: missing artifact {artifact}")
    actual = claim.actual()
    assert round(actual, claim.places) == round(claim.stated, claim.places), (
        f"{claim.id}: {claim.doc} publishes {claim.stated}, but "
        f"{'+'.join(claim.artifacts)} gives {actual:.5f}")


@pytest.mark.parametrize("claim", CLAIMS, ids=lambda c: c.id)
def test_claim_text_still_appears_in_its_doc(claim: Claim):
    """Keeps the guard load-bearing.

    Without this, rewording a table would leave the row above asserting
    against a number no page still shows — green, and guarding nothing.
    """
    text = (REPO / claim.doc).read_text(encoding="utf-8")
    assert claim.needle in text, (
        f"{claim.id}: {claim.doc} no longer contains the guarded text\n  "
        f"{claim.needle!r}\nIf the number changed, update this table; if the "
        f"claim was dropped, delete its row.")
