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
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import pytest

REPO = Path(__file__).resolve().parents[1]

# The commit-gated cascade's routing gate, imported from the harness rather
# than re-implemented: the #188 abstention counts below describe the policy
# the bench actually runs, and a local copy would drift away from it
# silently. `replicate` is import-light by design (no bench, no torch).
sys.path.insert(0, str(REPO / "evals"))
from replicate import cortex_commits as _commits  # noqa: E402
RESULTS = "evals/results/"

# Artifact shorthands — every path is repo-relative so it can be checked
# against `git ls-files` directly.
CEILING = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v2.agg.json"
ARM1 = RESULTS + "longmemeval-ku-oracle-e4b-ft-arm1.agg.json"
ARM1_BASE = RESULTS + "longmemeval-ku-oracle-e4b-ft-arm1-baseline.agg.json"
LME_V2 = RESULTS + "lme-v2-smoke-slice1.agg.json"
LME_V2_FULL = RESULTS + "lme-v2-smoke-slice2.summary.json"
LME_V2_FULL_COMPOSE = RESULTS + "lme-v2-smoke-slice2-compose.summary.json"
WABL_SURVIVAL = RESULTS + "longmemeval-ku-s-qwen-27b-wabl-survival.json"
NEEDLE_SURVIVAL = RESULTS + "longmemeval-ku-s-qwen-27b-needle-survival.json"
# The ceiling run's per-arm CONTEXT TOKENS live in the summary, not the agg.
CEILING_SUMMARY = (RESULTS +
                   "longmemeval-ku-oracle-qwen-27b-ceiling-v2.summary.json")
CEILING_V25 = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v25.agg.json"
CEILING_V25_SUMMARY = (
    RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v25.summary.json")
SHOOTOUT = RESULTS + "embedder-recall-shootout-20260727.json"
E2E = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e.agg.json"
E2E_SUMMARY = (
    RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e.summary.json")
CASC_CONF = RESULTS + "casc-q8-confirmation.json"
# The full six-type sweep (2026-08-03) — the front-door table since
# 2026-08-25 (#188). Single pass, so no .agg.json exists: the summary IS
# the artifact, and the docs say "single pass" beside it.
ALLTYPES = (RESULTS +
            "longmemeval-all-oracle-qwen-27b-alltypes-0803.summary.json")
# The same 78 questions after the 2026-08-17 bench migration to Qwen3.8.
CEILING_V38 = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v38.agg.json"
# Per-question rows, for the abstention / commit-precision counts that
# explain WHY the cascade moved. Recomputed with the harness's own gate.
# (The e2e side reuses the existing `E2E_ROWS` constant defined further
# down, where the channel-union claim first needed it.)
V38_ROWS_JSONL = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl"
# BEAM, documented in evals/README.md from 2026-08-25 (#188).
_BEAM = RESULTS + "beam-100K-qwen-27b-"
BEAM_Q38 = _BEAM + "beam100k-qwen38.summary.json"
BEAM_OPUS = _BEAM + "beam100k-qwen38.rejudge-opus5.summary.json"
BEAM_P1B16 = _BEAM + "p1-b16.summary.json"
BEAM_GRID = RESULTS + "beam-reader-volume-grid-verdict.json"
BEAM_SWEEP = RESULTS + "beam-readersweep-verdict.json"
BM25_AB = RESULTS + "bm25-ab-confirmation.json"
BM25_GATE = (RESULTS +
             "regression_gate-2026-07-30-cortex-bm25-enabled.agg.json")
V25_VERIFY = (RESULTS +
              "regression_gate-2026-07-29-v25-backbone-verify.agg.json")


def _arm1_cmp(arm: str) -> str:
    """Arm-1 vs its pre-fine-tune baseline, per arm (2026-07-29)."""
    return (f"{RESULTS}longmemeval-ku-oracle-e4b-ft-arm1-vs-baseline-"
            f"{arm}.compare.json")


def _wabl(tag: str) -> str:
    return f"{RESULTS}longmemeval-ku-s-qwen-27b-{tag}.agg.json"


def _wabl_cmp(kind: str, mode: str, arm: str) -> str:
    """kind: 'iso' (write-side isolation) | 'sys' (whole system)."""
    return (f"{RESULTS}longmemeval-ku-s-qwen-27b-wabl-"
            f"{kind}-{mode}-{arm}.compare.json")


def _abl(policy: str, mode: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-e4b-ft-arm1-abl-"
            f"{policy}-{mode}.agg.json")


def _abl_cmp(mode: str, arm: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-e4b-ft-arm1-abl-"
            f"{mode}-{arm}.compare.json")


@lru_cache(maxsize=None)
def _load_artifact(rel: str):
    """Parse one committed artifact — once per session, not once per claim.

    The claim table cites the same files over and over: 415 artifact loads
    resolve to a far smaller set of distinct files, and one 835KB rows JSONL
    was re-parsed 8 times. Measured 2026-08-28: 0.73s of loading collapses
    to 0.02s.

    Sharing one parsed object across claims is safe because every
    ``Claim.value`` accessor is a pure read — dict/list indexing, ``next``,
    ``sum``, arithmetic. None of them mutates what it is handed. Keep it
    that way when adding a claim, or this cache leaks state between them.
    """
    text = (REPO / rel).read_text(encoding="utf-8")
    if rel.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


@lru_cache(maxsize=None)
def _read_doc(rel: str) -> str:
    """Read one doc once. CHANGELOG.md was being re-read 160 times."""
    return (REPO / rel).read_text(encoding="utf-8")


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
        return self.value(*(_load_artifact(a) for a in self.artifacts))


def _mean(arm: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm]["mean"]


def _std(arm: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm]["std"]


def _delta(arm: str) -> Callable[[dict, dict], float]:
    """Continuum minus flat, the direction the ablation table publishes."""
    return lambda c, f: c["arms"][arm]["mean"] - f["arms"][arm]["mean"]


BENCH = "docs/guide/benchmarks.md"
READ_ME = "README.md"
CHANGELOG = "CHANGELOG.md"
EVALS = "evals/README.md"
CONFIG_GUIDE = "docs/guide/configuration.md"

# ── the local-ceiling table (README front door + guide) ───────────────────
# Re-based 2026-07-30 onto ceiling-v25 (reproducible q8_0 server). Its std
# is 0.0000 by construction — byte-identical replicates — so the docs
# publish plain accuracies plus a "std 0.0000" determinism sentence, which
# is pinned per-arm below in place of a ± column.
_CEILING_ROWS = [
    ("rag", "| naive RAG (top-6 turns) | 0.628 | 1638 |", 0.628),
    ("cortex", "| cortex facts only | 0.590 | **~182** |", 0.590),
    ("hybrid",
     "| **hybrid (facts + top-3 turns)** | **0.731** | ~1102 |", 0.731),
]

CLAIMS: list[Claim] = []

# 2026-07-30: the README's copy of this table was replaced by the
# end-to-end table (rows below) — the guide keeps the held-fixed rebuild,
# so these rows are guide-only now.
for _doc, _slug in ((BENCH, "guide"),):
    for _arm, _needle, _mean_v in _CEILING_ROWS:
        CLAIMS.append(Claim(
            id=f"ceiling-{_slug}-{_arm}-mean", doc=_doc, needle=_needle,
            artifacts=(CEILING_V25,), value=_mean(_arm), stated=_mean_v,
            places=3))
        CLAIMS.append(Claim(
            id=f"ceiling-{_slug}-{_arm}-std", doc=_doc, needle="std 0.0000",
            artifacts=(CEILING_V25,), value=_std(_arm), stated=0.0,
            places=4))

# ── the end-to-end run on the current stack + the commit-gated cascade ───
# Added 2026-07-30. Fresh qwen-27b extraction under the v25 backbone with
# BM25-on turn retrieval, reproducible q8_0 serving (3 byte-identical
# replicates). Not comparable per-arm to ceiling-v25 above, which holds
# extraction and turn selection at the 2026-07-19 configuration. The
# cascade arm is DERIVED (replicate.cascade_correct) from the judged
# rag/cortex arms — same artifacts, no fourth answered arm.
# 2026-08-25 (#188): the README's copy moved to the 500-question table and
# this one became the guide's "knowledge-update slice" section. The cascade
# cell is RETIRED — the judge migration scores it 0.846 — and per the
# retire-at-the-old-site rule it stays visible with strikethrough, so its
# row stays here, pinned to the artifact that produced it.
_E2E_ROWS = [
    ("rag", "| naive RAG (top-6 turns) | 0.859 | ~1237 |", 0.859, 1237),
    ("cortex", "| cortex facts only | 0.667 | **~259** |", 0.667, 259),
    ("hybrid", "| hybrid (facts + top-3 turns) | 0.833 | ~920 |",
     0.833, 920),
    ("cascade",
     "| **commit-gated cascade** | ~~**0.936**~~ (retired — see below) "
     "| ~702 |",
     0.936, 702),
]
for _doc, _slug in ((BENCH, "guide"),):
    for _arm, _needle, _mean_v, _tokens in _E2E_ROWS:
        CLAIMS.append(Claim(
            id=f"e2e-{_slug}-{_arm}-mean", doc=_doc, needle=_needle,
            artifacts=(E2E,), value=_mean(_arm), stated=_mean_v, places=3))
        CLAIMS.append(Claim(
            id=f"e2e-tokens-{_slug}-{_arm}", doc=_doc, needle=_needle,
            artifacts=(E2E_SUMMARY,),
            value=(lambda a: lambda d: d["arms"][a]["context_tokens"])(_arm),
            stated=_tokens, places=0))

# ── the cascade's full-haystack confirmation (pre-registered) ────────────
# The oracle table above is the friendly slice; this pins the _s-haystack
# run that makes the cascade-beats-RAG claim decision-grade. The p-value
# has its own artifact per the house rule; commit precision is pinned
# because the cascade's mechanism claim rests on it.
for _cid, _needle, _val, _stated in [
    ("casc-s-cascade-mean", "0.462 vs 0.346",
     lambda d: d["arm_means"]["cascade"], 0.462),
    ("casc-s-rag-mean", "0.462 vs 0.346",
     lambda d: d["arm_means"]["rag"], 0.346),
    ("casc-s-delta", "+0.115",
     lambda d: d["paired_permutation"]["cascade_vs_rag"]["delta"], 0.115),
    ("casc-s-p", "p = 0.011",
     lambda d: d["paired_permutation"]["cascade_vs_rag"]["p_value"], 0.011),
    ("casc-s-precision", "commit precision 0.714",
     lambda d: d["commit_precision"], 0.714),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=BENCH, needle=_needle, artifacts=(CASC_CONF,),
        value=_val, stated=_stated, places=3))

# ── the README scan-layer teaser (2026-08-14; re-based 2026-08-25) ───────
# A summary near the top of the README restates the headline numbers from
# the tables pinned elsewhere; a restatement is a claim like any other, so
# each cell is pinned here too. Re-based on 2026-08-25 (#188) from the
# 78-question knowledge-update slice to the full 500-question sweep; the
# full-haystack row left the front door entirely (it is a 2026-07-30
# measurement on the retired judge and has never been re-judged), and now
# lives under a dated currency note in the guide — pinned below.
_TEASER_ALL = "| accuracy, all six question types | 0.688 | 0.690 |"
_TEASER_TOKENS = "| context tokens per question | ~1210 | **~883** |"
_TEASER_KU = ("| knowledge-update slice (78 of the 500) "
              "| 0.859 | ~~0.936~~ (retired — see below) |")
for _cid, _needle, _val, _stated, _places in [
    ("teaser-500-rag", _TEASER_ALL,
     lambda d: d["arms"]["rag"]["accuracy"], 0.688, 3),
    ("teaser-500-cascade", _TEASER_ALL,
     lambda d: d["arms"]["cascade"]["accuracy"], 0.690, 3),
    ("teaser-500-tokens-rag", _TEASER_TOKENS,
     lambda d: d["arms"]["rag"]["context_tokens"], 1210, 0),
    ("teaser-500-tokens-cascade", _TEASER_TOKENS,
     lambda d: d["arms"]["cascade"]["context_tokens"], 883, 0),
    ("teaser-500-ku-rag", _TEASER_KU,
     lambda d: d["types"]["knowledge-update"]["arms"]["rag"], 0.859, 3),
    ("teaser-500-ku-cascade-retired", _TEASER_KU,
     lambda d: d["types"]["knowledge-update"]["cascade"], 0.936, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=READ_ME, needle=_needle, artifacts=(ALLTYPES,),
        value=_val, stated=_stated, places=_places))

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

# ── the ceiling table's TOKEN column ─────────────────────────────────────
# Added 2026-07-29. The accuracy column was pinned from the start; the token
# column never was, and it silently kept the numbers from a superseded run
# when the accuracies were re-pointed at ceiling-v2 on 2026-07-19. The cortex
# cell read "~60" against an artifact saying 124.1 for ten days, and the two
# percentages derived from it ("under 4%", "~60% of the context") were wrong
# in the README and on this page. A column nobody pins is a column that drifts.
# 2026-07-30: repointed at ceiling-v25 with the promotion, and pinned in
# the README too — its hybrid cell had drifted to "~1000" against an artifact
# saying 1043.3, because only the guide copy was guarded. Later that day the
# README's copy of the table was replaced by the end-to-end table (its own
# rows above), so these are guide-only again.
for _doc, _slug in ((BENCH, "guide"),):
    for _arm, _needle, _tokens in [
        ("rag", "| naive RAG (top-6 turns) | 0.628 | 1638 |", 1638),
        ("cortex", "| cortex facts only | 0.590 | **~182** |", 182),
        ("hybrid",
         "| **hybrid (facts + top-3 turns)** | **0.731** | ~1102 |", 1102),
    ]:
        CLAIMS.append(Claim(
            id=f"ceiling-tokens-{_slug}-{_arm}", doc=_doc, needle=_needle,
            artifacts=(CEILING_V25_SUMMARY,),
            value=(lambda a: lambda d: d["arms"][a]["context_tokens"])(_arm),
            stated=_tokens, places=0))

# ── the superseded v2 / TurboQuant table, retained in the guide ──────────
# House rule: retire numbers at the old site, don't delete them. The
# historical block under the promoted table keeps the v2 figures a reader
# will still meet, pinned to the same artifacts as when they were current.
for _arm, _needle, _mean_v, _std_v, _tokens in [
    ("rag", "| naive RAG (top-6 turns) | 0.567 ± 0.017 | 1638 |",
     0.567, 0.017, 1638),
    ("cortex", "| cortex facts only | 0.559 ± 0.029 | **~124** |",
     0.559, 0.029, 124),
    ("hybrid",
     "| **hybrid (facts + top-3 turns)** | **0.710 ± 0.019** | ~1043 |",
     0.710, 0.019, 1043),
]:
    CLAIMS.append(Claim(
        id=f"ceiling-hist-{_arm}-mean", doc=BENCH, needle=_needle,
        artifacts=(CEILING,), value=_mean(_arm), stated=_mean_v, places=3))
    CLAIMS.append(Claim(
        id=f"ceiling-hist-{_arm}-std", doc=BENCH, needle=_needle,
        artifacts=(CEILING,), value=_std(_arm), stated=_std_v, places=3))
    CLAIMS.append(Claim(
        id=f"ceiling-hist-tokens-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(CEILING_SUMMARY,),
        value=(lambda a: lambda d: d["arms"][a]["context_tokens"])(_arm),
        stated=_tokens, places=0))

# ── the Arm-1 table's p-values ───────────────────────────────────────────
# Added 2026-07-29, with the artifacts they had always lacked. The means were
# pinned; the p-values were not, and no comparison file existed at all — a
# significance claim resting on an aggregate of means, which is precisely what
# the "a p-value needs its own artifact" rule forbids. Generating them showed
# the published values were correct to the decimal; the defect was evidentiary,
# not numerical. The `rag` row is the control arm and bounds the other two.
for _arm, _needle, _p in [
    ("rag", "| naive RAG (control) | 0.574 ± 0.006 | 0.585 ± 0.015 | 0.41 |",
     0.41),
    ("cortex",
     "| cortex facts only | 0.682 ± 0.017 | 0.603 ± 0.013 | **0.17** |", 0.17),
    ("hybrid", "| hybrid | 0.762 ± 0.027 | 0.749 ± 0.015 | 0.83 |", 0.83),
]:
    CLAIMS.append(Claim(
        id=f"arm1-pvalue-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(_arm1_cmp(_arm),),
        value=lambda d: d["p_value"], stated=_p, places=2))

# ── the embedding-backbone shootout (schema v25's justification) ─────────
# Added 2026-07-29 with the section itself. This is the largest measured
# retrieval win in the 0.11.0 release and it reached the docs unpinned.
for _arm_key, _needle, _r10 in [
    ("all-MiniLM-L6-v2 (shipped)",
     "| all-MiniLM-L6-v2 (previous default) | 384 | 0.572 |", 0.572),
    ("bge-base-en-v1.5", "| bge-base-en-v1.5 | 768 | 0.742 |", 0.742),
    ("Qwen3-Embedding-0.6B (instructed)",
     "| **Qwen3-Embedding-0.6B (instructed)** | **1024** | **0.809** |",
     0.809),
]:
    CLAIMS.append(Claim(
        id=f"embed-r10-{_arm_key.split()[0]}", doc=BENCH, needle=_needle,
        artifacts=(SHOOTOUT,),
        value=(lambda k: lambda d: next(
            a["recall"]["10"] for a in d["arms"] if a["arm"] == k))(_arm_key),
        stated=_r10, places=3))

# ── the cross-stack offset measured by ceiling-v25 (2026-07-29) ──────────
# The load-bearing number here is the CONTROL arm's, because rebuild_contexts
# copies the rag context verbatim: identical input, so its movement is the
# serving stack and nothing else. Pinned because it is the number that says
# every other number on the page is stack-relative.
for _arm, _needle, _v25 in [
    ("rag", "| naive RAG (**control**) | 0.6282 | 0.5667 ± 0.0167 | **+0.0615** |",
     0.6282),
    ("cortex", "| cortex facts only | 0.5897 | 0.5590 ± 0.0295 | +0.0307 |",
     0.5897),
    ("hybrid", "| hybrid | 0.7308 | 0.7102 ± 0.0194 | +0.0206 |", 0.7308),
]:
    CLAIMS.append(Claim(
        id=f"ceiling-v25-{_arm}", doc=CHANGELOG, needle=_needle,
        artifacts=(CEILING_V25,), value=_mean(_arm), stated=_v25, places=4))
    # The published side of each row, from the run it supersedes.
    CLAIMS.append(Claim(
        id=f"ceiling-v25-{_arm}-published", doc=CHANGELOG, needle=_needle,
        artifacts=(CEILING,), value=_mean(_arm),
        stated={"rag": 0.5667, "cortex": 0.5590, "hybrid": 0.7102}[_arm],
        places=4))

# ── the v25 backbone's regression-gate verification (2026-07-29) ─────────
# The gate's own `*-gate.agg.json` namespace is gitignored and cleared at the
# start of every run, so a PASS there proves nothing to a later reader. This
# pins the promoted copy instead — the "tag the run and promote deliberately"
# half of the same rule that keeps canonical results from being overwritten.
GATE_V25 = RESULTS + "regression_gate-2026-07-29-v25-backbone-verify.agg.json"
for _arm, _needle, _gate_mean in [
    ("rag", "| naive RAG (control) | 0.6282 | 0.6282 | 0.0000 |", 0.6282),
    ("cortex", "| cortex facts only | 0.6923 | 0.7051 | −0.0128 |", 0.6923),
    ("hybrid", "| hybrid | 0.7821 | 0.7692 | +0.0129 |", 0.7821),
]:
    CLAIMS.append(Claim(
        id=f"gate-v25-{_arm}", doc=CHANGELOG, needle=_needle,
        artifacts=(GATE_V25,), value=_mean(_arm),
        stated=_gate_mean, places=4))
    # std 0.0000 is the load-bearing half of "served by the reproducible
    # config": a non-zero std here means the run drifted onto the fast build
    # and the deltas above cannot be read as real.
    CLAIMS.append(Claim(
        id=f"gate-v25-{_arm}-std", doc=CHANGELOG,
        needle="`std` is 0.0000 on all\n  three across both replicates",
        artifacts=(GATE_V25,),
        value=(lambda a: lambda d: d["arms"][a]["std"])(_arm),
        stated=0.0, places=4))

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


# ── the WRITE-side band ablation (flat INGEST, not just flat ranking) ────
# Two comparisons per cell: 'iso' holds the ranking flat on both arms so
# only the surviving entry sets differ; 'sys' is the continuum as designed
# vs flat everything. The cortex arm is definitionally null here (both
# arms build the same fact block) and so is neither published nor pinned.
for _kind, _rows in (
    ("iso", [
        ("rag", "| naive RAG | −0.090 | 0.17 | −0.097 | 0.15 |",
         (-0.090, 0.17, 2), (-0.097, 0.15, 2)),
        ("hybrid",
         "| hybrid | **−0.110** | **0.018** | **−0.108** | **0.027** |",
         (-0.110, 0.018, 3), (-0.108, 0.027, 3)),
    ]),
    ("sys", [
        ("rag",
         "| naive RAG | **−0.274** | **0.0001** | **−0.251** | **0.0001** |",
         (-0.274, 0.0001, 4), (-0.251, 0.0001, 4)),
        ("hybrid",
         "| hybrid | **−0.141** | **0.0038** | **−0.123** | **0.0153** |",
         (-0.141, 0.0038, 4), (-0.123, 0.0153, 4)),
    ]),
):
    for _arm, _needle, _wall, _hist in _rows:
        for _mode, (_d, _p, _p_places) in (("wall", _wall), ("hist", _hist)):
            # Delta from the two aggregates it is a difference of; p from
            # the comparison artifact, which is the only thing that can
            # justify a significance claim.
            _a_tag = (f"abl-flat-{_mode}" if _kind == "iso"
                      else f"abl-continuum-{_mode}")
            CLAIMS.append(Claim(
                id=f"wabl-{_kind}-{_mode}-{_arm}-delta", doc=BENCH,
                needle=_needle,
                artifacts=(_wabl(_a_tag), _wabl(f"wabl-flat-{_mode}")),
                value=_delta(_arm), stated=_d, places=3))
            CLAIMS.append(Claim(
                id=f"wabl-{_kind}-{_mode}-{_arm}-p", doc=BENCH,
                needle=_needle, artifacts=(_wabl_cmp(_kind, _mode, _arm),),
                value=lambda d: d["p_value"], stated=_p, places=_p_places))

# The eviction rate is the mechanism sentence's load-bearing number.
CLAIMS.append(Claim(
    id="wabl-continuum-eviction-rate", doc=BENCH,
    needle="**evicts 31.1%\nof everything stored**",
    artifacts=(WABL_SURVIVAL,),
    value=lambda d: d["continuum_loss_rate"] * 100.0, stated=31.1, places=1))
CLAIMS.append(Claim(
    id="wabl-flat-eviction-rate", doc=BENCH,
    needle="capacity* evicts nothing",
    artifacts=(WABL_SURVIVAL,),
    value=lambda d: d["flat_loss_rate"], stated=0.0, places=3))

# ── needle survival: does the 31.1% eviction discard the EVIDENCE? ────────
# Justifies the overflow fix in the CHANGELOG. Survival rate alone can't
# say whether eviction costs anything; the needle rate can.
for _id, _needle, _get, _stated, _places in [
    ("needle-eviction-rate", "(**37.5%",
     lambda d: d["needle_eviction_rate"] * 100.0, 37.5, 1),
    ("needle-base-rate", "evicted vs a 31.1% base rate**",
     lambda d: d["base_eviction_rate"] * 100.0, 31.1, 1),
    ("needle-questions-affected", "with 58% of questions losing at least",
     lambda d: d["questions_losing_a_needle_frac"] * 100.0, 58, 0),
]:
    CLAIMS.append(Claim(
        id=_id, doc=CHANGELOG, needle=_needle,
        artifacts=(NEEDLE_SURVIVAL,), value=_get,
        stated=_stated, places=_places))

# ── the full 74-question LongMemEval-V2 procedure category ───────────────
for _arm, _needle, _ku, _compose in [
    ("rag", "| naive RAG (control) | 0.162 | 0.284 |", 0.162, 0.284),
    ("cortex", "| cortex facts only | 0.068 | 0.216 |", 0.068, 0.216),
    ("hybrid", "| hybrid | **0.243** | 0.284 |", 0.243, 0.284),
]:
    CLAIMS.append(Claim(
        id=f"lmev2-full-ku-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_FULL,),
        value=lambda d, a=_arm: d["arms"][a]["eval_accuracy"],
        stated=_ku, places=3))
    CLAIMS.append(Claim(
        id=f"lmev2-full-compose-{_arm}", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_FULL_COMPOSE,),
        value=lambda d, a=_arm: d["arms"][a]["eval_accuracy"],
        stated=_compose, places=3))


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, text=True,
                         capture_output=True)
    if out.returncode != 0:  # pragma: no cover - only without git
        pytest.skip("git unavailable")
    return set(out.stdout.split())


# ── the embedding-backbone shootout (CHANGELOG, 2026-07-28) ───────────────
SHOOTOUT = RESULTS + "embedder-recall-shootout-20260727.json"
QWEN_VS_BGE = RESULTS + "embedder-recall-qwen-vs-bge-20260728.json"
QUANT = RESULTS + "embedder-recall-quant-shootout-20260728.json"


def _arm_recall(label: str, k: int) -> Callable[[dict], float]:
    """Exact-label match: several arms share prefixes (bge-base vs its
    query-prefix variant), so prefix matching would silently pin the wrong
    arm's number."""
    return lambda d: next(a for a in d["arms"]
                          if a["arm"] == label)["recall"][str(k)]


def _mcnemar_p(label: str, k: int) -> Callable[[dict], float]:
    return lambda d: next(t for t in d["mcnemar_vs_shipped"]
                          if t["arm"] == label and t["k"] == k)["p_value"]


for _id, _art, _label, _needle, _stated in [
    ("embed-qwen3-r10", SHOOTOUT, "Qwen3-Embedding-0.6B (instructed)",
     "Qwen3-Embedding-0.6B reaches R@10 **0.809** vs bge-base-en-v1.5 0.742",
     0.809),
    ("embed-bge-base-r10", SHOOTOUT, "bge-base-en-v1.5",
     "Qwen3-Embedding-0.6B reaches R@10 **0.809** vs bge-base-en-v1.5 0.742",
     0.742),
    ("embed-q8-r10", QUANT, "Qwen3-Embedding-0.6B Q8_0 (gguf)",
     "Q8_0 GGUF matches fp32 (R@10 0.806 vs 0.809", 0.806),
    ("embed-fp32-anchor-r10", QUANT, "Qwen3-Embedding-0.6B (instructed)",
     "Q8_0 GGUF matches fp32 (R@10 0.806 vs 0.809", 0.809),
    ("embed-4b-q4-r10", QUANT,
     "Qwen3-Embedding-4B Q4_K_M (gguf, native 2560d)",
     "lands BELOW the fp32 0.6B (R@10 0.753", 0.753),
]:
    CLAIMS.append(Claim(
        id=_id, doc=CHANGELOG, needle=_needle, artifacts=(_art,),
        value=_arm_recall(_label, 10), stated=_stated, places=3))

CLAIMS.append(Claim(
    id="embed-qwen-vs-bge-p10", doc=CHANGELOG,
    needle="+32/−12 at k=10, p=0.004",
    artifacts=(QWEN_VS_BGE,),
    value=_mcnemar_p("Qwen3-Embedding-0.6B (instructed)", 10),
    stated=0.004, places=3))


# ── the cortex-BM25 opt-in decision (2026-07-30) ─────────────────────────
# The channel ships OFF because a pre-registered A/B measured no benefit;
# the CHANGELOG states the flat numbers and the gate cost, so both sides
# are pinned. The "before" gate cortex value comes from the committed
# 2026-07-29 v25-verify promotion (same slice, channel absent); the
# "after" from a promoted copy of the gate run with the channel enabled
# (the live gate namespace is gitignored and cleared per run).
for _cid, _artifact, _needle, _val, _stated in [
    ("bm25-ab-cortex-off", BM25_AB, "0.1795 both",
     lambda d: d["off"]["cortex"], 0.1795),
    ("bm25-ab-cortex-on", BM25_AB, "0.1795 both",
     lambda d: d["on"]["cortex"], 0.1795),
    ("bm25-ab-cascade-off", BM25_AB, "0.4231 both",
     lambda d: d["off"]["cascade"], 0.4231),
    ("bm25-ab-cascade-on", BM25_AB, "0.4231 both",
     lambda d: d["on"]["cascade"], 0.4231),
    ("bm25-gate-cortex-before", V25_VERIFY, "0.6923 → 0.6795",
     _mean("cortex"), 0.6923),
    ("bm25-gate-cortex-after", BM25_GATE, "0.6923 → 0.6795",
     _mean("cortex"), 0.6795),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(_artifact,),
        value=_val, stated=_stated, places=4))


C2OP = RESULTS + "c2op-gate-verdict.json"
# ── the C2-op definitive gate (2026-07-31) ───────────────────────────────
# The CHANGELOG states the cascade regression that justified holding the
# op prompt block; both the delta and its p-value are pinned to the
# committed verdict artifact (a p-value needs its own artifact).
CLAIMS.append(Claim(
    id="c2op-cascade-delta", doc=CHANGELOG, needle="cascade −0.141 at p = 0.006",
    artifacts=(C2OP,),
    value=lambda d: d["gates"]["e2e"]["paired_vs_control"]["cascade"]["delta"],
    stated=-0.141, places=3))
CLAIMS.append(Claim(
    id="c2op-cascade-p", doc=CHANGELOG, needle="cascade −0.141 at p = 0.006",
    artifacts=(C2OP,),
    value=lambda d: d["gates"]["e2e"]["paired_vs_control"]["cascade"]["p"],
    stated=0.006, places=3))


C2OP_GUARD = RESULTS + "c2op-guard-verdict.json"
# ── the aggregate-conversion-guard gate (2026-08-01) ─────────────────────
# The CHANGELOG states that the guarded re-run flipped no verdicts and left
# the cascade regression vs control unchanged; both numbers pin to the
# guard verdict artifact.
CLAIMS.append(Claim(
    id="c2op-guard-flips", doc=CHANGELOG, needle="0/78 flips",
    artifacts=(C2OP_GUARD,),
    value=lambda d: d["paired"]["vs_c2op_e2e"]["all_arms"]["flips"],
    stated=0, places=0))
CLAIMS.append(Claim(
    id="c2op-guard-cascade-p", doc=CHANGELOG,
    needle="still −0.141 at p = 0.006 vs the op-less control",
    artifacts=(C2OP_GUARD,),
    value=lambda d: d["paired"]["vs_ceiling_control"]["cascade"]["p"],
    stated=0.006, places=3))


C2OP_COUNT = RESULTS + "c2op-count-verdict.json"
# ── the count-exclusion op-prompt gate (2026-08-01) ──────────────────────
# The CHANGELOG states that under the count-exclusion rule the cascade lands
# exactly at the op-less control and the rule repairs the op block's damage;
# both pin to the count-arm verdict artifact.
CLAIMS.append(Claim(
    id="c2op-count-cascade-vs-control", doc=CHANGELOG,
    needle="cascade lands exactly at the op-less control (delta 0.0, p = 1.0)",
    artifacts=(C2OP_COUNT,),
    value=lambda d: d["paired"]["vs_opless_control"]["cascade"]["delta"],
    stated=0.0, places=3))
CLAIMS.append(Claim(
    id="c2op-count-cascade-vs-c2op", doc=CHANGELOG,
    needle="+0.141 at p = 0.004 over the un-ruled op prompt",
    artifacts=(C2OP_COUNT,),
    value=lambda d: d["paired"]["vs_c2op_e2e"]["cascade"]["p"],
    stated=0.004, places=3))


LIT_CMP = RESULTS + "compare-c2v6-literal-pairs.json"
# ── the literal-fidelity negative result (2026-08-01) ────────────────────
# The CHANGELOG states the v6 prompt's pre-registered KU gate failed:
# cascade delta and p pin to the committed pairs artifact; the rag control
# at exactly zero is what makes the delta a finding rather than noise.
CLAIMS.append(Claim(
    id="lit-v6-cascade-delta", doc=CHANGELOG,
    needle="cascade -0.090 (p = 0.037)",
    artifacts=(LIT_CMP,),
    value=lambda d: d["paired"]["a_vs_b"]["cascade"]["delta"],
    stated=-0.090, places=3))
CLAIMS.append(Claim(
    id="lit-v6-cascade-p", doc=CHANGELOG,
    needle="cascade -0.090 (p = 0.037)",
    artifacts=(LIT_CMP,),
    value=lambda d: d["paired"]["a_vs_b"]["cascade"]["p"],
    stated=0.037, places=3))
CLAIMS.append(Claim(
    id="lit-v6-rag-control", doc=CHANGELOG,
    needle="the rag control at delta 0.000",
    artifacts=(LIT_CMP,),
    value=lambda d: d["paired"]["a_vs_b"]["rag"]["delta"],
    stated=0.0, places=3))


AGGP1 = RESULTS + "compare-aggp1-{}-pairs.json"
# ── the aggregation-aware-recall Phase 1 negative result (2026-08-04) ────
# The CHANGELOG states all four retrieval knobs failed their preregistered
# gates; each per-knob delta and p pins to its committed within-run pairs
# artifact, and the cross-run rag control at exactly zero is what licenses
# reading the deltas as knob effects rather than noise.
def _aggp1(pair_key: str) -> Callable[[dict], dict]:
    return lambda d: d["paired"]["a_vs_b"][pair_key]


for _knob, _set, _needle, _delta_v, _p_v in [
    ("ctg", "weak", "contiguity delta -0.147", -0.147, 0.00000),
    ("tl", "weak",
     "(p 0.00000), timeline -0.011 (p 0.70120), enum rendering -0.071",
     -0.011, 0.70120),
    ("enum", "weak",
     "(p 0.00000), timeline -0.011 (p 0.70120), enum rendering -0.071",
     -0.071, 0.00030),
    ("all", "weak",
     "(p 0.00030), all-three-combined -0.177 (p 0.00000). Timeline also",
     -0.177, 0.00000),
    ("tl", "strong", "(-0.038, p 0.00340, 0 wins /", -0.038, 0.00340),
]:
    _pair = _aggp1(f"hybrid_{_knob}_vs_hybrid")
    CLAIMS.append(Claim(
        id=f"aggp1-{_knob}-{_set}-delta", doc=CHANGELOG, needle=_needle,
        artifacts=(AGGP1.format(f"{_knob}-{_set}"),),
        value=lambda d, g=_pair: g(d)["delta"], stated=_delta_v, places=3))
    CLAIMS.append(Claim(
        id=f"aggp1-{_knob}-{_set}-p", doc=CHANGELOG, needle=_needle,
        artifacts=(AGGP1.format(f"{_knob}-{_set}"),),
        value=lambda d, g=_pair: g(d)["p"], stated=_p_v, places=5))

CLAIMS.append(Claim(
    id="aggp1-rag-control", doc=CHANGELOG,
    needle="is exactly zero (0 flips over 500 questions)",
    artifacts=(AGGP1.format("rag-control"),),
    value=lambda d: d["paired"]["a_vs_b"]["rag_vs_rag"]["delta"],
    stated=0.0, places=4))


CCS = RESULTS + "contiguity-cue-split-20260904.json"
# ── cue-gated contiguity: the Phase-1 follow-up (2026-09-04) ─────────────
# An OFFLINE composite over the same aggp1-variants rows, so every number
# below is a re-read of already-judged verdicts rather than a fresh run.
# That is exactly why it needs pinning: nothing else would contradict it.
# The CHANGELOG carries the argument, the evals README the tables; both
# are guarded, because a reader meets the number in whichever they open.


def _cue(*path) -> Callable[[dict], float]:
    def get(d: dict) -> float:
        node = d["cues"]
        for k in path:
            node = node[k]
        return node
    return get


def _ccs(arm: str, *path) -> Callable[[dict], float]:
    def get(d: dict) -> float:
        node = d["variants"][arm]
        for k in path:
            node = node[k]
        return node
    return get


_CCS_CHANGELOG = [
    # (id, needle, value, stated, places)
    ("ccs-cue-any-rate",
     "engine's own `any` gate fires on 0.702 of the 500 questions (recall",
     _cue("fire_rate", "any"), 0.702, 3),
    ("ccs-cue-weak-recall",
     "0.947 on the weak types, precision 0.718, and 0.692 on",
     _cue("confusion_vs_weak_types", "recall_on_weak"), 0.947, 3),
    ("ccs-cue-weak-precision",
     "0.947 on the weak types, precision 0.718, and 0.692 on",
     _cue("confusion_vs_weak_types", "precision_for_weak"), 0.718, 3),
    ("ccs-cue-ku-rate",
     "0.947 on the weak types, precision 0.718, and 0.692 on",
     _cue("confusion_vs_weak_types", "knowledge_update_fire_rate"),
     0.692, 3),
    ("ccs-cue-date-rate",
     "predicate fires 0.000 times, LongMemEval keeps the date out of the",
     _cue("fire_rate", "date"), 0.0, 3),
    ("ccs-ctg-fired-delta",
     "hybrid is -0.114 on cue-fired rows (n=351, p 0.00000) against -0.047",
     _ccs("hybrid_ctg", "split", "cue_fired", "delta"), -0.114, 3),
    ("ccs-ctg-fired-p",
     "hybrid is -0.114 on cue-fired rows (n=351, p 0.00000) against -0.047",
     _ccs("hybrid_ctg", "split", "cue_fired", "p"), 0.0, 5),
    ("ccs-ctg-fired-n",
     "hybrid is -0.114 on cue-fired rows (n=351, p 0.00000) against -0.047",
     _ccs("hybrid_ctg", "split", "cue_fired", "n"), 351, 0),
    ("ccs-ctg-quiet-delta",
     "hybrid is -0.114 on cue-fired rows (n=351, p 0.00000) against -0.047",
     _ccs("hybrid_ctg", "split", "cue_quiet", "delta"), -0.047, 3),
    ("ccs-ctg-quiet-p",
     "where the cue is quiet (n=149, p 0.18170), and on the weak types the",
     _ccs("hybrid_ctg", "split", "cue_quiet", "p"), 0.18170, 5),
    ("ccs-ctg-quiet-n",
     "where the cue is quiet (n=149, p 0.18170), and on the weak types the",
     _ccs("hybrid_ctg", "split", "cue_quiet", "n"), 149, 0),
    ("ccs-ctg-weak-fired-delta",
     "two splits are indistinguishable (-0.147 fired vs -0.143 quiet). The",
     _ccs("hybrid_ctg", "split_weak_types", "cue_fired", "delta"),
     -0.147, 3),
    ("ccs-ctg-weak-quiet-delta",
     "two splits are indistinguishable (-0.147 fired vs -0.143 quiet). The",
     _ccs("hybrid_ctg", "split_weak_types", "cue_quiet", "delta"),
     -0.143, 3),
    ("ccs-gated-overall-acc",
     "gated composite therefore scores 0.584 overall and 0.320 on the weak",
     _ccs("hybrid_ctg", "gated", "overall", "gated_acc"), 0.584, 3),
    ("ccs-gated-weak-acc",
     "gated composite therefore scores 0.584 overall and 0.320 on the weak",
     _ccs("hybrid_ctg", "gated", "weak_types", "gated_acc"), 0.320, 3),
    ("ccs-hybrid-overall-acc",
     "types against vanilla hybrid's 0.664 / 0.459 (-0.080 and -0.139, both",
     _ccs("hybrid_ctg", "gated", "overall", "hybrid_acc"), 0.664, 3),
    ("ccs-hybrid-weak-acc",
     "types against vanilla hybrid's 0.664 / 0.459 (-0.080 and -0.139, both",
     _ccs("hybrid_ctg", "gated", "weak_types", "hybrid_acc"), 0.459, 3),
    ("ccs-gated-overall-delta",
     "types against vanilla hybrid's 0.664 / 0.459 (-0.080 and -0.139, both",
     _ccs("hybrid_ctg", "gated", "overall", "vs_hybrid", "delta"),
     -0.080, 3),
    ("ccs-gated-weak-delta",
     "types against vanilla hybrid's 0.664 / 0.459 (-0.080 and -0.139, both",
     _ccs("hybrid_ctg", "gated", "weak_types", "vs_hybrid", "delta"),
     -0.139, 3),
    ("ccs-gated-weak-p",
     "  p 0.00000), buying +0.008 of the 0.147 weak-type hole while adding 254",
     _ccs("hybrid_ctg", "gated", "weak_types", "vs_hybrid", "p"), 0.0, 5),
    ("ccs-ctg-turns-added",
     "so on cue-fired rows contiguity adds a mean 1.46 turns and *displaces*",
     _ccs("hybrid_ctg", "context_effect", "cue_fired", "mean_turns_added"),
     1.46, 2),
    ("ccs-ctg-turns-displaced",
     "the same 1.46 ranked hits. The Phase-1 verdict that contiguity stays off",
     _ccs("hybrid_ctg", "context_effect", "cue_fired",
          "mean_turns_displaced"), 1.46, 2),
    ("ccs-tl-gated-overall",
     "(0.640 / 0.447) because the timeline channel is already cue-gated in",
     _ccs("hybrid_tl", "gated", "overall", "gated_acc"), 0.640, 3),
    ("ccs-tl-gated-weak",
     "(0.640 / 0.447) because the timeline channel is already cue-gated in",
     _ccs("hybrid_tl", "gated", "weak_types", "gated_acc"), 0.447, 3),
    ("ccs-tl-gated-equals-ungated",
     "(0.640 / 0.447) because the timeline channel is already cue-gated in",
     _ccs("hybrid_tl", "gated", "weak_types", "ungated_acc"), 0.447, 3),
]
for _cid, _needle, _val, _stated, _places in _CCS_CHANGELOG:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(CCS,),
        value=_val, stated=_stated, places=_places))

# The noise floor is a SUM across the four variant arms, so it gets its
# own accessor: 522 identical-context arm-rows, zero verdict flips.
CLAIMS.append(Claim(
    id="ccs-noise-identical-rows", doc=CHANGELOG,
    needle="the engine, and 522 arm-rows whose served context was byte-identical",
    artifacts=(CCS,),
    value=lambda d: sum(v["noise_control"]["identical_context_rows"]
                        for v in d["variants"].values()),
    stated=522, places=0))
CLAIMS.append(Claim(
    id="ccs-noise-disagreements", doc=CHANGELOG,
    needle="arm — produced zero verdict disagreements, so the splits carry no",
    artifacts=(CCS,),
    value=lambda d: sum(v["noise_control"]["verdict_disagreements"]
                        for v in d["variants"].values()),
    stated=0, places=0))

# The evals README's two tables. The gated-arm table publishes accuracy,
# weak-type accuracy, the UNGATED weak comparison (what gating bought)
# and the token cost, so all four cells of each row pin.
_CCS_GATED_ROWS = [
    ("hybrid_ctg", "| `hybrid_ctg` gated | 0.584 | 0.320 | 0.312 | 1096.4 |",
     0.584, 0.320, 0.312, 1096.4),
    ("hybrid_tl", "| `hybrid_tl` gated | 0.640 | 0.447 | 0.447 | 803.4 |",
     0.640, 0.447, 0.447, 803.4),
    ("hybrid_enum", "| `hybrid_enum` gated | 0.626 | 0.387 | 0.387 | 857.5 |",
     0.626, 0.387, 0.387, 857.5),
    ("hybrid_all", "| `hybrid_all` gated | 0.546 | 0.293 | 0.282 | 1089.0 |",
     0.546, 0.293, 0.282, 1089.0),
]
for _arm, _needle, _ov, _wk, _uw, _tok in _CCS_GATED_ROWS:
    for _suffix, _val, _stated, _places in [
        ("overall", _ccs(_arm, "gated", "overall", "gated_acc"), _ov, 3),
        ("weak", _ccs(_arm, "gated", "weak_types", "gated_acc"), _wk, 3),
        ("ungated-weak",
         _ccs(_arm, "gated", "weak_types", "ungated_acc"), _uw, 3),
        ("tokens",
         _ccs(_arm, "gated", "overall", "gated_context_tokens"), _tok, 1),
    ]:
        CLAIMS.append(Claim(
            id=f"ccs-readme-{_arm}-{_suffix}", doc=EVALS, needle=_needle,
            artifacts=(CCS,), value=_val, stated=_stated, places=_places))

CLAIMS.append(Claim(
    id="ccs-readme-vanilla-tokens", doc=EVALS,
    needle="| vanilla `hybrid` | 0.664 | 0.459 | \u2014 | 842.1 |",
    artifacts=(CCS,),
    value=_ccs("hybrid_ctg", "gated", "overall", "hybrid_context_tokens"),
    stated=842.1, places=1))

# The per-type cue fire-rate table: the two weak types the knobs target
# and the knowledge-update row the cue must NOT fire on.
for _type, _needle, _temporal, _agg, _any in [
    ("multi-session", "| multi-session | 133 | 0.256 | 0.887 | 0.940 |",
     0.256, 0.887, 0.940),
    ("temporal-reasoning",
     "| temporal-reasoning | 133 | 0.820 | 0.421 | 0.955 |",
     0.820, 0.421, 0.955),
    ("knowledge-update",
     "| knowledge-update | 78 | 0.321 | 0.538 | 0.692 |",
     0.321, 0.538, 0.692),
]:
    for _cue_name, _stated in [("temporal", _temporal),
                               ("aggregation", _agg), ("any", _any)]:
        CLAIMS.append(Claim(
            id=f"ccs-readme-fire-{_type}-{_cue_name}", doc=EVALS,
            needle=_needle, artifacts=(CCS,),
            value=_cue("by_type", _type, _cue_name),
            stated=_stated, places=3))


# A narrower gate does not save contiguity either: the README states the
# temporal-only and aggregation-only gates land at the same place, above
# the `any` gate and below vanilla hybrid. Both cue keys pin, because the
# sentence claims they AGREE and one drifting would make it false.
_CCS_NARROW_NEEDLE = (
    "lands at **0.616** overall and **0.376** on the weak types \u2014 better")
for _cue_key in ("temporal", "aggregation"):
    for _slice, _stated, _places in [("overall_acc", 0.616, 3),
                                     ("weak_acc", 0.376, 3)]:
        CLAIMS.append(Claim(
            id=f"ccs-readme-narrow-{_cue_key}-{_slice}", doc=EVALS,
            needle=_CCS_NARROW_NEEDLE, artifacts=(CCS,),
            value=(lambda c, s: lambda d: d["variants"]["hybrid_ctg"]
                   ["gated_by_cue"][c][s])(_cue_key, _slice),
            stated=_stated, places=_places))


EV2 = RESULTS + "compare-ev2-{}-pairs.json"
EV2_SUMMARY = RESULTS + "longmemeval-all-oracle-qwen-27b-ev2-sep-0804.summary.json"
# ── the separate-pass events gate result (2026-08-05) ────────────────────
# The CHANGELOG states all four preregistered gates pass; the two controls
# (rag, claims-inertness) at exactly zero are what license reading the
# hybrid_ev deltas as event effects, so they pin at 4 places.
CLAIMS.append(Claim(
    id="ev2-rag-control", doc=CHANGELOG,
    needle="delta 0.000, 0 flips over 500 questions vs the independent",
    artifacts=(EV2.format("rag-control"),),
    value=lambda d: d["paired"]["a_vs_b"]["rag_vs_rag"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="ev2-claims-inertness", doc=CHANGELOG,
    needle="hybrid at delta 0.000 with 0 flips over all 500",
    artifacts=(EV2.format("claims-inertness"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="ev2-weak-delta", doc=CHANGELOG,
    needle="hybrid by +0.056 (p 0.00450,",
    artifacts=(EV2.format("weak-primary"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["delta"],
    stated=0.056, places=3))
CLAIMS.append(Claim(
    id="ev2-weak-p", doc=CHANGELOG,
    needle="20 wins / 5 losses), concentrated",
    artifacts=(EV2.format("weak-primary"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["p"],
    stated=0.00450, places=5))
CLAIMS.append(Claim(
    id="ev2-strong-delta", doc=CHANGELOG,
    needle="non-inferiority (n=234): delta 0.000 with 0 flips",
    artifacts=(EV2.format("strong-noninferiority"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="ev2-tr-hybrid-ev", doc=CHANGELOG,
    needle="temporal-reasoning 0.534 to 0.624",
    artifacts=(EV2_SUMMARY,),
    value=lambda d: d["types"]["temporal-reasoning"]["arms"]["hybrid_ev"],
    stated=0.624, places=3))
CLAIMS.append(Claim(
    id="ev2-tr-hybrid", doc=CHANGELOG,
    needle="temporal-reasoning 0.534 to 0.624",
    artifacts=(EV2_SUMMARY,),
    value=lambda d: d["types"]["temporal-reasoning"]["arms"]["hybrid"],
    stated=0.534, places=3))

# ── BEAM chronicle re-run (beam100k-ev-0806): honest negative, recorded ──
BEAMEV = RESULTS + "compare-beam-ev-{}-pairs.json"
BEAMEV_SUMMARY = RESULTS + "beam-100K-qwen-27b-beam100k-ev-0806.summary.json"

CLAIMS.append(Claim(
    id="beamev-rag-control", doc=CHANGELOG,
    needle="delta exactly 0 over 400 questions, 0/0 flips",
    artifacts=(BEAMEV.format("rag-control"),),
    value=lambda d: d["paired"]["a_vs_b"]["rag_vs_rag"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="beamev-claims-inertness-delta", doc=CHANGELOG,
    needle="missed its exact-zero bar at −0.002 (p 0.83,",
    artifacts=(BEAMEV.format("claims-inertness"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["delta"],
    stated=-0.002, places=3))
CLAIMS.append(Claim(
    id="beamev-claims-inertness-p", doc=CHANGELOG,
    needle="19W/18L)",
    artifacts=(BEAMEV.format("claims-inertness"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["p"],
    stated=0.8305, places=4))
CLAIMS.append(Claim(
    id="beamev-primary-delta", doc=CHANGELOG,
    needle="event_ordering gate FAILED (−0.016, p 0.68)",
    artifacts=(BEAMEV.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["delta"],
    stated=-0.016, places=3))
CLAIMS.append(Claim(
    id="beamev-noninf-delta", doc=CHANGELOG,
    needle="+0.020 pooled over the 9",
    artifacts=(BEAMEV.format("strong-noninferiority"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["delta"],
    stated=0.020, places=3))
CLAIMS.append(Claim(
    id="beamev-noninf-p", doc=CHANGELOG,
    needle="remaining abilities (p 0.023), driven by temporal_reasoning",
    artifacts=(BEAMEV.format("strong-noninferiority"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["p"],
    stated=0.0233, places=4))
CLAIMS.append(Claim(
    id="beamev-tr-hybrid", doc=CHANGELOG,
    needle="0.4625 → 0.6188 (+0.156, served on 32/40 rows)",
    artifacts=(BEAMEV_SUMMARY,),
    value=lambda d: d["types"]["temporal_reasoning"]["hybrid"],
    stated=0.4625, places=4))
CLAIMS.append(Claim(
    id="beamev-tr-hybrid-ev", doc=CHANGELOG,
    needle="0.4625 → 0.6188 (+0.156, served on 32/40 rows)",
    artifacts=(BEAMEV_SUMMARY,),
    value=lambda d: d["types"]["temporal_reasoning"]["hybrid_ev"],
    stated=0.6188, places=4))

# ── aggregation-cued serving gate run (aggserve-0806) ────────────────────
AGGS = RESULTS + "compare-aggserve-{}-pairs.json"
AGGS_SUMMARY = RESULTS + "longmemeval-all-oracle-qwen-27b-aggserve-0806.summary.json"

CLAIMS.append(Claim(
    id="aggserve-rag-control", doc=CHANGELOG,
    needle="exactly (delta 0.000, 0/0 flips, contexts and",
    artifacts=(AGGS.format("rag-control"),),
    value=lambda d: d["paired"]["a_vs_b"]["rag_vs_rag"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="aggserve-claims-inertness", doc=CHANGELOG,
    needle="missed their exact-zero bars at −0.006",
    artifacts=(AGGS.format("claims-inertness"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["delta"],
    stated=-0.006, places=3))
CLAIMS.append(Claim(
    id="aggserve-reconstruction", doc=CHANGELOG,
    needle="and −0.004 via the same",
    artifacts=(AGGS.format("reconstruction"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid_ev"]["delta"],
    stated=-0.004, places=3))
CLAIMS.append(Claim(
    id="aggserve-primary-delta", doc=CHANGELOG,
    needle="underpowered: +0.038 (p 0.123, 6W/1L)",
    artifacts=(AGGS.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_syn_vs_hybrid_ev"]["delta"],
    stated=0.038, places=3))
CLAIMS.append(Claim(
    id="aggserve-primary-p", doc=CHANGELOG,
    needle="underpowered: +0.038 (p 0.123, 6W/1L)",
    artifacts=(AGGS.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_syn_vs_hybrid_ev"]["p"],
    stated=0.1226, places=4))
CLAIMS.append(Claim(
    id="aggserve-decomp-serving", doc=CHANGELOG,
    needle="serving (+0.030, 4W/0L)",
    artifacts=(AGGS.format("decomp-serving"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_agg_vs_hybrid_ev"]["delta"],
    stated=0.030, places=3))
CLAIMS.append(Claim(
    id="aggserve-decomp-tally", doc=CHANGELOG,
    needle="line (+0.007). The multi-session ladder",
    artifacts=(AGGS.format("decomp-tally"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_syn_vs_hybrid_ev_agg"]["delta"],
    stated=0.007, places=3))
CLAIMS.append(Claim(
    id="aggserve-noninf-strong", doc=CHANGELOG,
    needle="exactly zero flips (n=234)",
    artifacts=(AGGS.format("noninf-strong"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_syn_vs_hybrid_ev"]["delta"],
    stated=0.0, places=4))
for _arm, _stated in (("hybrid", 0.376), ("hybrid_ev", 0.398),
                      ("hybrid_ev_agg", 0.429), ("hybrid_ev_syn", 0.436)):
    CLAIMS.append(Claim(
        id=f"aggserve-ms-{_arm}", doc=CHANGELOG,
        needle=("monotone — hybrid 0.376," if _arm == "hybrid" else
                "+events 0.398, +widened serving 0.429, +tally 0.436,"),
        artifacts=(AGGS_SUMMARY,),
        value=lambda d, a=_arm: d["types"]["multi-session"]["arms"][a],
        stated=_stated, places=3))
CLAIMS.append(Claim(
    id="aggserve-ms-rag", doc=CHANGELOG,
    needle="vs rag 0.504 —",
    artifacts=(AGGS_SUMMARY,),
    value=lambda d: d["types"]["multi-session"]["arms"]["rag"],
    stated=0.504, places=3))

# ── events coverage audit (task #40, no GPU) ─────────────────────────────
AUDIT = RESULTS + "events-coverage-audit-0806.json"

CLAIMS.append(Claim(
    id="audit-amount-arithmetic", doc=CHANGELOG,
    needle="syn-wrong rows, 19 are amount-arithmetic",
    artifacts=(AUDIT,),
    value=lambda d: d["residual_classes"]["amount-arithmetic"],
    stated=19, places=0))
CLAIMS.append(Claim(
    id="audit-cue-miss", doc=CHANGELOG,
    needle="not-event-shaped (static facts, out of events' reach), 5 cue-miss,",
    artifacts=(AUDIT,),
    value=lambda d: d["residual_mechanisms"]["cue-miss"],
    stated=5, places=0))
CLAIMS.append(Claim(
    id="audit-quantity-stripped", doc=CHANGELOG,
    needle="4 extraction-or-retrieval gaps, 4 quantity-stripped, 2 partial",
    artifacts=(AUDIT,),
    value=lambda d: d["residual_mechanisms"]["quantity-not-representable"],
    stated=4, places=0))
CLAIMS.append(Claim(
    id="audit-beam-no-misorder", doc=CHANGELOG,
    needle="0 of 23 served event_ordering",
    artifacts=(AUDIT,),
    value=lambda d: d["beam_event_ordering_autopsy"]["failure_modes"][
        "wrong-order-despite-events"],
    stated=0, places=0))
CLAIMS.append(Claim(
    id="ev2-ms-hybrid-ev", doc=CHANGELOG,
    needle="multi-session 0.383 to 0.406",
    artifacts=(EV2_SUMMARY,),
    value=lambda d: d["types"]["multi-session"]["arms"]["hybrid_ev"],
    stated=0.406, places=3))

# ── events quantity + coverage gate run (evq-0806) ───────────────────────
EVQ = RESULTS + "compare-evq-{}-pairs.json"
EVQ_SUMMARY = RESULTS + "longmemeval-all-oracle-qwen-27b-evq-0806.summary.json"

CLAIMS.append(Claim(
    id="evq-rag-control", doc=CHANGELOG,
    needle="reproduced `aggserve-0806` at delta 0.000",
    artifacts=(EVQ.format("rag-control"),),
    value=lambda d: d["paired"]["a_vs_b"]["rag_vs_rag"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="evq-claims-inertness", doc=CHANGELOG,
    needle="also measured exactly\n  0.000 (0/0 flips)",
    artifacts=(EVQ.format("claims-inertness"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["delta"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="evq-primary-delta", doc=CHANGELOG,
    needle="missed: +0.038 (p 0.226, 8W/3L)",
    artifacts=(EVQ.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"][
        "hybrid_ev_syn_vs_hybrid_ev_syn"]["delta"],
    stated=0.0376, places=4))
CLAIMS.append(Claim(
    id="evq-primary-p", doc=CHANGELOG,
    needle="missed: +0.038 (p 0.226, 8W/3L)",
    artifacts=(EVQ.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"][
        "hybrid_ev_syn_vs_hybrid_ev_syn"]["p"],
    stated=0.2261, places=4))
CLAIMS.append(Claim(
    id="evq-hdr-no-harm", doc=CHANGELOG,
    needle="header arm is free (+0.002 pooled",
    artifacts=(EVQ.format("hdr-harm"),),
    value=lambda d: d["paired"]["a_vs_b"][
        "hybrid_ev_hdr_vs_hybrid_ev_syn"]["delta"],
    stated=0.002, places=3))
CLAIMS.append(Claim(
    id="evq-hdr-overall", doc=CHANGELOG,
    needle="0.720 overall, 0.662\n  temporal-reasoning",
    artifacts=(EVQ_SUMMARY,),
    value=lambda d: d["arms"]["hybrid_ev_hdr"]["accuracy"],
    stated=0.720, places=3))
CLAIMS.append(Claim(
    id="evq-hdr-tr", doc=CHANGELOG,
    needle="0.720 overall, 0.662\n  temporal-reasoning",
    artifacts=(EVQ_SUMMARY,),
    value=lambda d: d["types"]["temporal-reasoning"]["arms"]["hybrid_ev_hdr"],
    stated=0.662, places=3))
CLAIMS.append(Claim(
    id="evq-noninf-strong", doc=CHANGELOG,
    needle="strong four at\n  exactly zero flips (n=234)",
    artifacts=(EVQ.format("noninf-strong"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_ev_syn_vs_hybrid_ev"][
        "delta"],
    stated=0.0, places=4))
for _arm, _stated in (("hybrid", 0.376), ("hybrid_ev", 0.391),
                      ("hybrid_ev_agg", 0.459), ("hybrid_ev_syn", 0.474)):
    CLAIMS.append(Claim(
        id=f"evq-ms-{_arm}", doc=CHANGELOG,
        needle=("v2 bank — hybrid 0.376," if _arm == "hybrid" else
                "+events 0.391, +widened serving 0.459,\n  +tally 0.474,"),
        artifacts=(EVQ_SUMMARY,),
        value=lambda d, a=_arm: d["types"]["multi-session"]["arms"][a],
        stated=_stated, places=3))
CLAIMS.append(Claim(
    id="evq-ms-rag", doc=CHANGELOG,
    needle="+tally 0.474, vs rag 0.504",
    artifacts=(EVQ_SUMMARY,),
    value=lambda d: d["types"]["multi-session"]["arms"]["rag"],
    stated=0.504, places=3))

# ── evq residual decomposition (offline matcher replay + Opus probe) ─────
DECOMP = RESULTS + "evq-residual-decomposition-0807.json"

CLAIMS.append(Claim(
    id="decomp-n-residual", doc=CHANGELOG,
    needle="Of the 18 rows\n  where rag is right",
    artifacts=(DECOMP,),
    value=lambda d: d["n_residual"],
    stated=18, places=0))
CLAIMS.append(Claim(
    id="decomp-at-cap", doc=CHANGELOG,
    needle="0 hit the\n  30-event serving cap",
    artifacts=(DECOMP,),
    value=lambda d: d["tally"]["at_cap_retrieval_side"],
    stated=0, places=0))
CLAIMS.append(Claim(
    id="decomp-extraction-side", doc=CHANGELOG,
    needle="14 rows are\n  sub-cap with matchable instances absent",
    artifacts=(DECOMP,),
    value=lambda d: d["tally"]["subcap_matchable_extraction_side"],
    stated=14, places=0))
CLAIMS.append(Claim(
    id="decomp-losses-block-authority", doc=CHANGELOG,
    needle="The 3 primary-gate losses share one mechanism",
    artifacts=(DECOMP,),
    value=lambda d: sum(1 for l in d["loss_autopsy"]
                        if l["v1_correct"] and not l["v2_correct"]),
    stated=3, places=0))

# ── evlora campaign (e4b-v3 multi-task sidecar, tag evlora-0807) ─────────
EVL = RESULTS + "compare-evlora-{}-pairs.json"
EVL_SUMMARY = RESULTS + "longmemeval-all-oracle-e4b-v3-evlora-0807.summary.json"
EVL_AGG_V2 = RESULTS + "longmemeval-ku-oracle-e4b-v2-evlora-0807.agg.json"
EVL_AGG_V3 = RESULTS + "longmemeval-ku-oracle-e4b-v3-evlora-0807.agg.json"

CLAIMS.append(Claim(
    id="evlora-t1b-cortex-v3", doc=CHANGELOG,
    needle="cortex 0.679 vs the deployed v2's 0.654 over",
    artifacts=(EVL_AGG_V3,),
    value=lambda d: d["arms"]["cortex"]["mean"],
    stated=0.6795, places=4))
CLAIMS.append(Claim(
    id="evlora-t1b-cortex-v2", doc=CHANGELOG,
    needle="cortex 0.679 vs the deployed v2's 0.654 over",
    artifacts=(EVL_AGG_V2,),
    value=lambda d: d["arms"]["cortex"]["mean"],
    stated=0.6538, places=4))
CLAIMS.append(Claim(
    id="evlora-t3-student", doc=CHANGELOG,
    needle="captures 14 of\n  the 18 Opus-covered instances",
    artifacts=(RESULTS + "evlora-capacity-spot-e4b-v3.json",),
    value=lambda d: d["student_covered_total"],
    stated=14, places=0))
CLAIMS.append(Claim(
    id="evlora-primary-delta", doc=CHANGELOG,
    needle="lands +0.128 (p 0.0068, 27W/10L)",
    artifacts=(EVL.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"][
        "hybrid_ev_syn_vs_hybrid_ev_syn"]["delta"],
    stated=0.1278, places=4))
CLAIMS.append(Claim(
    id="evlora-primary-p", doc=CHANGELOG,
    needle="lands +0.128 (p 0.0068, 27W/10L)",
    artifacts=(EVL.format("primary"),),
    value=lambda d: d["paired"]["a_vs_b"][
        "hybrid_ev_syn_vs_hybrid_ev_syn"]["p"],
    stated=0.0068, places=4))
CLAIMS.append(Claim(
    id="evlora-covariate-delta", doc=CHANGELOG,
    needle="(+0.056, p 0.0039) exceeds its attribution bound",
    artifacts=(EVL.format("claims-covariate"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["delta"],
    stated=0.056, places=3))
CLAIMS.append(Claim(
    id="evlora-hybrid-pooled", doc=CHANGELOG,
    needle="hybrid\n  0.714 vs 0.658",
    artifacts=(EVL_SUMMARY,),
    value=lambda d: d["arms"]["hybrid"]["accuracy"],
    stated=0.714, places=3))
CLAIMS.append(Claim(
    id="evlora-ins-pooled", doc=CHANGELOG,
    needle="(0.764 pooled, 0.714",
    artifacts=(EVL_SUMMARY,),
    value=lambda d: d["arms"]["hybrid_ev_ins"]["accuracy"],
    stated=0.764, places=3))
CLAIMS.append(Claim(
    id="evlora-ins-tr", doc=CHANGELOG,
    needle="(0.764 pooled, 0.714",
    artifacts=(EVL_SUMMARY,),
    value=lambda d: d["types"]["temporal-reasoning"]["arms"]["hybrid_ev_ins"],
    stated=0.714, places=3))
CLAIMS.append(Claim(
    id="evlora-stale-v3", doc=CHANGELOG,
    needle="ladder stale_leak 0.1 vs v2's\n  0.0",
    artifacts=(RESULTS + "e4b-v3.json",),
    value=lambda d: d["stale_leak"],
    stated=0.1, places=3))
CLAIMS.append(Claim(
    id="evlora-ku-guard-p", doc=CHANGELOG,
    needle="by 0.006 (2 of 78 questions,\n  p 0.74)",
    artifacts=(EVL.format("ku-guard"),),
    value=lambda d: d["paired"]["a_vs_b"]["hybrid_vs_hybrid"]["p"],
    stated=0.7436, places=4))

# ── retention-interval (ret-0809) + staleness-policy H3 (stalepol-0809) ──
# The ret-0809 rows are retroactive: PR #120 published the numbers without
# evidence rows (caught in the 2026-08-09 H3 change; the same-change rule
# exists precisely because this slip is invisible once merged).
RET_VERDICT = RESULTS + "retention-interval-verdict.json"
STALEPOL_VERDICT = RESULTS + "stale-policy-verdict.json"

CLAIMS.append(Claim(
    id="ret0809-h2-p", doc=CHANGELOG,
    needle="paired sign-flip p 0.0002, 30 pairs",
    artifacts=(RET_VERDICT,),
    value=lambda d: d["h2_flag_efficacy"]["permutation_p_two_sided"],
    stated=0.0002, places=4))
CLAIMS.append(Claim(
    id="ret0809-h2-pairs", doc=CHANGELOG,
    needle="paired sign-flip p 0.0002, 30 pairs",
    artifacts=(RET_VERDICT,),
    value=lambda d: float(d["h2_flag_efficacy"]["paired_units"]),
    stated=30.0, places=0))
CLAIMS.append(Claim(
    id="stalepol-q-p", doc=CHANGELOG,
    needle="quarantine paired sign-flip p 0.0005, 13/30 discordant",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: d["arms"]["policy_quarantine"]["gate1_efficacy"][
        "permutation_p_two_sided"],
    stated=0.0005, places=4))
CLAIMS.append(Claim(
    id="stalepol-q-discordant", doc=CHANGELOG,
    needle="quarantine paired sign-flip p 0.0005, 13/30 discordant",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: float(
        d["arms"]["policy_quarantine"]["gate1_efficacy"]["discordant"]),
    stated=13.0, places=0))
CLAIMS.append(Claim(
    id="stalepol-q-rate", doc=CHANGELOG,
    needle="unqualified-stale-answer rate to 0.0 in every\n  replicate",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: d["arms"]["policy_quarantine"]["stale_answer_rate_mean"],
    stated=0.0, places=3))
CLAIMS.append(Claim(
    id="stalepol-d-p", doc=CHANGELOG,
    needle="demote identical at p 0.0005",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: d["arms"]["policy_demote"]["gate1_efficacy"][
        "permutation_p_two_sided"],
    stated=0.0005, places=4))
CLAIMS.append(Claim(
    id="stalepol-recovery", doc=CHANGELOG,
    needle="recovered on explicit ask at rate 1.0 in every\n  replicate",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: d["gate3_recovery"]["recovery_rate_mean"],
    stated=1.0, places=3))
CLAIMS.append(Claim(
    id="stalepol-fresh-gap", doc=CHANGELOG,
    needle="fresh payloads byte-identical, gap 0.0",
    artifacts=(STALEPOL_VERDICT,),
    value=lambda d: d["arms"]["policy_quarantine"]["gate2_no_harm"][
        "fresh_gap"],
    stated=0.0, places=4))

# ── consolidation quarantine gates (qgate/qreplay 0809) ──────────────────
QGATE = RESULTS + "quarantine-gate-qgate-0809.json"
QREPLAY = RESULTS + "quarantine-replay-qreplay-0809.json"

for _arm in ("quarantine_off", "quarantine_on_paranoid"):
    CLAIMS.append(Claim(
        id=f"qgate-{_arm}-gold", doc=CHANGELOG,
        needle="gold 1.0 / stale_leak 0.1 /\n  19 claims both arms, parked 0",
        artifacts=(QGATE,),
        value=(lambda a: lambda d: d["arms"][a]["gold_recoverable"])(_arm),
        stated=1.0, places=3))
    CLAIMS.append(Claim(
        id=f"qgate-{_arm}-stale", doc=CHANGELOG,
        needle="gold 1.0 / stale_leak 0.1 /\n  19 claims both arms, parked 0",
        artifacts=(QGATE,),
        value=(lambda a: lambda d: d["arms"][a]["stale_leak"])(_arm),
        stated=0.1, places=3))
CLAIMS.append(Claim(
    id="qgate-paranoid-parked", doc=CHANGELOG,
    needle="gold 1.0 / stale_leak 0.1 /\n  19 claims both arms, parked 0",
    artifacts=(QGATE,),
    value=lambda d: float(
        d["arms"]["quarantine_on_paranoid"]["quarantine_parked"]),
    stated=0.0, places=0))
CLAIMS.append(Claim(
    id="qreplay-would-park", doc=CHANGELOG,
    needle="0 of 629 scalar claims would have parked",
    artifacts=(QREPLAY,),
    value=lambda d: float(d["would_park"]),
    stated=0.0, places=0))
CLAIMS.append(Claim(
    id="qreplay-scalar-rows", doc=CHANGELOG,
    needle="0 of 629 scalar claims would have parked",
    artifacts=(QREPLAY,),
    value=lambda d: float(d["scalar_rows"]),
    stated=629.0, places=0))


# ── sidecar cache_prompt pin (measured decision, 0809) ──────────────────
SIDECAR_CACHE = RESULTS + "sidecar-cache-latency-sidecar-cache-0809.json"

CLAIMS.append(Claim(
    id="sidecar-cache-penalty", doc=CHANGELOG,
    needle="the pin costs +7.25s per\n  extraction call",
    artifacts=(SIDECAR_CACHE,),
    value=lambda d: d["nocache_penalty_seconds"],
    stated=7.25, places=2))
CLAIMS.append(Claim(
    id="sidecar-cache-default-mean", doc=CHANGELOG,
    needle="(3.41s → 10.65s over 4",
    artifacts=(SIDECAR_CACHE,),
    value=lambda d: d["default_mean"],
    stated=3.41, places=2))
CLAIMS.append(Claim(
    id="sidecar-cache-nocache-mean", doc=CHANGELOG,
    needle="(3.41s → 10.65s over 4",
    artifacts=(SIDECAR_CACHE,),
    value=lambda d: d["nocache_mean"],
    stated=10.65, places=2))


# ── gaps found by the 2026-08-10 alignment audit ─────────────────────────
# Three published numbers had no evidence row while their table siblings
# did, and the floor-vs-ceiling extractor section had none at all — the
# exact failure class this file's docstring records from the 2026-07-17
# and 2026-07-21 audits.

# The two shootout table rows that were never pinned (siblings were).
for _arm_key, _needle, _r10 in [
    ("granite-embedding-english-r2",
     "| granite-embedding-english-r2 | 768 | 0.662 |", 0.662),
    ("snowflake-arctic-embed-l-v2.0 (query prefix)",
     "| snowflake-arctic-embed-l-v2.0 | 1024 | 0.732 |", 0.732),
]:
    CLAIMS.append(Claim(
        id=f"embed-r10-{_arm_key.split()[0].split('-')[0]}", doc=BENCH,
        needle=_needle, artifacts=(SHOOTOUT,),
        value=(lambda k: lambda d: next(
            a["recall"]["10"] for a in d["arms"] if a["arm"] == k))(_arm_key),
        stated=_r10, places=3))

# The per-question channel union behind the cascade argument. Derived from
# the e2e run's per-question rows (rag_correct OR cortex_correct); the
# three replicates are byte-identical, so the first jsonl suffices.
E2E_ROWS = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e.jsonl"
CLAIMS.append(Claim(
    id="e2e-channel-union", doc=BENCH,
    needle="per-question union is 0.949",
    artifacts=(E2E_ROWS,),
    value=lambda rows: (sum(1 for r in rows
                            if r["rag_correct"] or r["cortex_correct"])
                        / len(rows)),
    stated=0.949, places=3))

# The floor-vs-ceiling extractor comparison (single-run point estimates,
# stated as such in the doc — pinned all the same).
FLOOR_SUMMARY = RESULTS + "longmemeval-ku-oracle-gemma-e2b.summary.json"
CEILING_SINGLE = RESULTS + "longmemeval-ku-oracle-qwen-27b.summary.json"
for _cid, _artifact, _needle, _arm, _stated in [
    ("floorceil-cortex-ceiling", CEILING_SINGLE,
     "0.564 → 0.192 when the extractor shrinks", "cortex", 0.564),
    ("floorceil-cortex-floor", FLOOR_SUMMARY,
     "0.564 → 0.192 when the extractor shrinks", "cortex", 0.192),
    ("floorceil-rag-ceiling", CEILING_SINGLE,
     "0.615 → 0.564 — a shift inside the run-to-run band", "rag", 0.615),
    ("floorceil-rag-floor", FLOOR_SUMMARY,
     "0.615 → 0.564 — a shift inside the run-to-run band", "rag", 0.564),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=BENCH, needle=_needle, artifacts=(_artifact,),
        value=(lambda a: lambda d: d["arms"][a]["accuracy"])(_arm),
        stated=_stated, places=3))

# ── chronicle production soak review (default-on decision, 2026-08-12) ───
SOAK = RESULTS + "chronicle-soak-review-20260812.json"

CLAIMS.append(Claim(
    id="chronicle-soak-events", doc=CHANGELOG,
    needle="188 events\n  written",
    artifacts=(SOAK,),
    value=lambda d: float(d["events"]["total"]),
    stated=188.0, places=0))
CLAIMS.append(Claim(
    id="chronicle-soak-bad-dates", doc=CHANGELOG,
    needle="(0 incorrect dates, including historical",
    artifacts=(SOAK,),
    value=lambda d: float(d["correctness_judgment"]["incorrect_dates"]),
    stated=0.0, places=0))
CLAIMS.append(Claim(
    id="chronicle-soak-dups", doc=CHANGELOG,
    needle="2 duplicate events both caught by the",
    artifacts=(SOAK,),
    value=lambda d: float(d["dream_runs"]["events_duplicate"]),
    stated=2.0, places=0))
CLAIMS.append(Claim(
    id="chronicle-soak-volume", doc=CHANGELOG,
    needle="a negligible 160 kB for the week",
    artifacts=(SOAK,),
    value=lambda d: float(d["events"]["table_total_kb"]),
    stated=160.0, places=0))
CLAIMS.append(Claim(
    id="chronicle-soak-sidecar-events", doc=CHANGELOG,
    needle="the soak's 20 sidecar-extracted events were judged",
    artifacts=(SOAK,),
    value=lambda d: float(
        d["dream_runs"]["by_extractor"]["sidecar-e4b-v3"]["events"]),
    stated=20.0, places=0))

# ── stance+span-gate prereg outcomes (no-ship decision, 2026-08-13) ──────
STANCE_PROBE = RESULTS + "stance-probe-20260813-gate1.json"
SGKU_PAIRED = RESULTS + "stance-ku-paired-verdict.json"


def _sgku(arm: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-qwen-27b-sgku-{arm}"
            ".summary.json")


CLAIMS.append(Claim(
    id="stance-probe-capture", doc=CHANGELOG,
    needle="v8 stance capture 0.92, false-stance 0.00",
    artifacts=(STANCE_PROBE,),
    value=lambda d: d["arms"]["v8"]["metrics"]["stance_capture"],
    stated=0.92, places=2))
CLAIMS.append(Claim(
    id="stance-probe-hedged-drop", doc=CHANGELOG,
    needle="hedged-note recovery 0.30 vs 0.925 plain",
    artifacts=(STANCE_PROBE,),
    value=lambda d: d["arms"]["v5"]["metrics"]["hedged_recovered"],
    stated=0.30, places=2))
CLAIMS.append(Claim(
    id="sgku-v8-cortex", doc=CHANGELOG,
    needle="v8 cortex 0.615 vs v5 control 0.731",
    artifacts=(_sgku("v8"),),
    value=lambda d: d["arms"]["cortex"]["accuracy"],
    stated=0.615, places=3))
CLAIMS.append(Claim(
    id="sgku-v5-cortex-control", doc=CHANGELOG,
    needle="v8 cortex 0.615 vs v5 control 0.731",
    artifacts=(_sgku("v5"),),
    value=lambda d: d["arms"]["cortex"]["accuracy"],
    stated=0.731, places=3))
CLAIMS.append(Claim(
    id="sgku-v8-mcnemar", doc=CHANGELOG,
    needle="paired McNemar\n  p=0.0117, net −9/78",
    artifacts=(SGKU_PAIRED,),
    value=lambda d: d["comparisons"]["v8"]["cortex"]["p_mcnemar_exact"],
    stated=0.0117, places=4))
CLAIMS.append(Claim(
    id="sgku-v9-hybrid", doc=CHANGELOG,
    needle="v9 hybrid\n  0.833 vs 0.897 (0 wins / 5 losses)",
    artifacts=(_sgku("v9"),),
    value=lambda d: d["arms"]["hybrid"]["accuracy"],
    stated=0.833, places=3))

# ── misleading-recall probe baseline (2026-08-13) ────────────────────────
MRP = RESULTS + "misleading-recall-20260813-baseline.json"

CLAIMS.append(Claim(
    id="mrp-unchecked-follow", doc=CHANGELOG,
    needle="the unchecked-follow rate is 0.67",
    artifacts=(MRP,),
    value=lambda d: d["metrics"]["unchecked_follow_rate"],
    stated=0.67, places=2))
CLAIMS.append(Claim(
    id="mrp-harm-with-evidence", doc=CHANGELOG,
    needle="never follows the wrong memory (harm rate 0.00,",
    artifacts=(MRP,),
    value=lambda d: d["metrics"]["harm_rate"],
    stated=0.0, places=2))
CLAIMS.append(Claim(
    id="mrp-evidence-ceiling", doc=CHANGELOG,
    needle="12\n  scenarios, evidence ceiling 1.00",
    artifacts=(MRP,),
    value=lambda d: d["metrics"]["evidence_ceiling"],
    stated=1.0, places=2))

# ── v10 stance prompt ship (2026-08-14) ──────────────────────────────────
V10_PAIRED = RESULTS + "stance-v10-ku-paired-verdict.json"
V10_PROBE = RESULTS + "stance-probe-20260813-v10.json"
V10_DRIFT = RESULTS + "bank-drift-sg2-v5-vs-v10.json"
V10_FLOOR = RESULTS + "bank-drift-crosswindow-v5-floor.json"


def _sg2(arm: str) -> str:
    return (f"{RESULTS}longmemeval-ku-oracle-qwen-27b-sg2-{arm}"
            ".summary.json")


CLAIMS.append(Claim(
    id="v10-probe-capture", doc=CHANGELOG,
    needle="stance capture 0.919 with false-stance 0.00",
    artifacts=(V10_PROBE,),
    value=lambda d: d["arms"]["v10"]["metrics"]["stance_capture"],
    stated=0.919, places=3))
CLAIMS.append(Claim(
    id="v10-probe-false-stance", doc=CHANGELOG,
    needle="stance capture 0.919 with false-stance 0.00",
    artifacts=(V10_PROBE,),
    value=lambda d: d["arms"]["v10"]["metrics"]["false_stance"],
    stated=0.0, places=2))
CLAIMS.append(Claim(
    id="v10-probe-hedged-recovery", doc=CHANGELOG,
    needle="hedged-fact recovery\n  0.30→0.925",
    artifacts=(V10_PROBE,),
    value=lambda d: d["arms"]["v10"]["metrics"]["hedged_recovered"],
    stated=0.925, places=3))
CLAIMS.append(Claim(
    id="v10-drift-slot-ratio", doc=CHANGELOG,
    needle="slot ratio 1.20 / key jaccard 0.49",
    artifacts=(V10_DRIFT,),
    value=lambda d: d["aggregates"]["mean_slot_ratio"],
    stated=1.20, places=2))
CLAIMS.append(Claim(
    id="v10-ku-cortex-unchanged", doc=CHANGELOG,
    needle="cortex exactly\n  unchanged (0.731 vs 0.731, net 0, p=1.0",
    artifacts=(V10_PAIRED,),
    value=lambda d: d["comparisons"]["v10"]["cortex"]["p_mcnemar_exact"],
    stated=1.0, places=2))
CLAIMS.append(Claim(
    id="v10-ku-hybrid-watch", doc=CHANGELOG,
    needle="hybrid 0.859 vs 0.897 (2W/5L, p=0.45",
    artifacts=(_sg2("v10"),),
    value=lambda d: d["arms"]["hybrid"]["accuracy"],
    stated=0.859, places=3))
CLAIMS.append(Claim(
    id="v10-drift-jaccard", doc=CHANGELOG,
    needle="slot ratio 1.20 / key jaccard 0.49",
    artifacts=(V10_DRIFT,),
    value=lambda d: d["aggregates"]["mean_key_jaccard"],
    stated=0.49, places=2))
CLAIMS.append(Claim(
    id="v10-drift-floor", doc=CHANGELOG,
    needle="clean 1.00/1.00 cross-window v5 floor",
    artifacts=(V10_FLOOR,),
    value=lambda d: d["aggregates"]["mean_key_jaccard"],
    stated=1.0, places=2))


# ── the abl25 flat-band verdict (2026-08-15, preregistered) ──────────────
# Every gate number in the verdict doc pins to its committed artifact.
# The verdict is a tie-sweep, so the load-bearing numbers are the deltas'
# p-values (nothing significant) plus the two mechanism receipts: zero
# eviction under the cascade, and the judged-preference null on real
# queries.
ABL25_DOC = "docs/superpowers/specs/2026-08-14-flat-band-verdict-preregistration.md"
_ABL25 = RESULTS + "longmemeval-ku-oracle-e4b-ft-arm1-abl25-{}.compare.json"
_ABL25_S = RESULTS + "longmemeval-ku-s-qwen-27b-{}.compare.json"
for _cid, _needle, _art, _key, _stated, _places in [
    ("abl25-oracle-rag-a", "rag 0.859 vs 0.885", _ABL25.format("off-rag"),
     "a_mean", 0.859, 3),
    ("abl25-oracle-rag-b", "rag 0.859 vs 0.885", _ABL25.format("off-rag"),
     "b_mean", 0.885, 3),
    ("abl25-oracle-rag-p", "(Δ −2.6 pts, p = 0.619)",
     _ABL25.format("off-rag"), "p_value", 0.619, 3),
    ("abl25-oracle-hybrid-a", "hybrid 0.846 vs 0.833 (Δ +1.3, p = 1.0)",
     _ABL25.format("off-hybrid"), "a_mean", 0.846, 3),
    ("abl25-oracle-hybrid-p", "hybrid 0.846 vs 0.833 (Δ +1.3, p = 1.0)",
     _ABL25.format("off-hybrid"), "p_value", 1.0, 3),
    ("abl25-s-hybrid-p", "hybrid 0.744 vs 0.795 (Δ −5.1,\n  p = 0.348)",
     _ABL25_S.format("abl25-continuum-off-vs-abl25-flat-off-hybrid"),
     "p_value", 0.348, 3),
    ("abl25-s-rag-p", "rag 0.859\n  vs 0.833 (Δ +2.6, p = 0.684)",
     _ABL25_S.format("abl25-continuum-off-vs-abl25-flat-off-rag"),
     "p_value", 0.684, 3),
    ("abl25-hist24-rag-delta", "hist24 (86400 s) rag Δ 0.0",
     _ABL25.format("hist24-rag"), "delta", 0.0, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=ABL25_DOC, needle=_needle, artifacts=(_art,),
        value=(lambda k: lambda d: d[k])(_key), stated=_stated,
        places=_places))

ABL25_SURV = RESULTS + "longmemeval-ku-s-qwen-27b-wabl25-survival.json"
for _cid, _key in [("abl25-survival-continuum", "continuum_loss_rate"),
                   ("abl25-survival-flat", "flat_loss_rate")]:
    CLAIMS.append(Claim(
        id=_cid, doc=ABL25_DOC, needle="loss 0.0 for BOTH ingest arms",
        artifacts=(ABL25_SURV,),
        value=(lambda k: lambda d: d[k])(_key), stated=0.0, places=3))

ABL25_EVICT = (RESULTS +
               "longmemeval-ku-s-qwen-27b-evict-policy-scaled257-vs-flat257.json")
for _cid, _needle, _key, _stated in [
    ("abl25-evict-a", "0.459 (scaled 8-band) vs 0.465",
     "a_mean_evidence_survival", 0.459),
    ("abl25-evict-b", "0.459 (scaled 8-band) vs 0.465",
     "b_mean_evidence_survival", 0.465),
    ("abl25-evict-p", "Δ −0.006, p = 1.0",
     "delta_p_paired_perm_10k_seed0", 1.0),
    ("abl25-drop-p", "fraction 0.009 vs 0.009 (p = 1.0)",
     "drop_delta_p_paired_perm_10k_seed0", 1.0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=ABL25_DOC, needle=_needle, artifacts=(ABL25_EVICT,),
        value=(lambda k: lambda d: d[k])(_key), stated=_stated, places=3))

ABL25_E5 = RESULTS + "abl25-e5-live-replay.json"
ABL25_E5J = RESULTS + "abl25-e5-judged-preference.json"
for _cid, _needle, _art, _key, _stated, _places in [
    ("abl25-e5-div6", "top-6 divergence 0.876", ABL25_E5,
     "divergence_rate_topk", 0.876, 3),
    ("abl25-e5-div3", "top-3 0.411", ABL25_E5,
     "divergence_rate_top3", 0.411, 3),
    ("abl25-e5-pref", "banded 0.5508, p = 0.130", ABL25_E5J,
     "mean_banded_preference", 0.5508, 4),
    ("abl25-e5-pref-p", "banded 0.5508, p = 0.130", ABL25_E5J,
     "p_vs_null_paired_perm_10k_seed0", 0.130, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=ABL25_DOC, needle=_needle, artifacts=(_art,),
        value=(lambda k: lambda d: d[k])(_key), stated=_stated,
        places=_places))

for _cid, _art_name, _stated in [
    ("abl25-e6-store-flat", "abl25-e6-latency-flat.json", 17.5),
    ("abl25-e6-store-cont", "abl25-e6-latency-continuum.json", 11.0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=ABL25_DOC,
        needle="flat store\n  median 17.5 ms vs 11.0 ms (1.59x, bar was 1.5x)",
        artifacts=(RESULTS + _art_name,),
        value=lambda d: d["rows"][0]["store_median_ms"], stated=_stated,
        places=1))

# The benchmarks page's 2026-08-15 closing block repeats two rerun
# numbers where readers meet the July tables — pin them there too.
for _cid, _needle, _key, _stated in [
    ("abl25-bench-evict-a", "(0.459 vs 0.465, p = 1.0)",
     "a_mean_evidence_survival", 0.459),
    ("abl25-bench-evict-b", "(0.459 vs 0.465, p = 1.0)",
     "b_mean_evidence_survival", 0.465),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=BENCH, needle=_needle, artifacts=(ABL25_EVICT,),
        value=(lambda k: lambda d: d[k])(_key), stated=_stated, places=3))
CLAIMS.append(Claim(
    id="abl25-bench-survival-zero", doc=BENCH,
    needle="(loss 0.0 both ingest arms",
    artifacts=(ABL25_SURV,),
    value=lambda d: d["continuum_loss_rate"], stated=0.0, places=3))

# The distractor-scale probe's published gates (spec doc, 2026-08-15).
PROBE_DOC = ("docs/superpowers/specs/"
             "2026-08-15-distractor-scale-probe-preregistration.md")
PROBE = RESULTS + "distractor-scale-probe-2026-08-15.json"
for _cid, _needle, _val, _stated, _places in [
    ("probe-1x", "1x 0.830", lambda d:
        d["scales"]["1x"]["evidence_in_top6_mean"], 0.830, 3),
    ("probe-15x", "15x 0.597", lambda d:
        d["scales"]["15x"]["evidence_in_top6_mean"], 0.597, 3),
    ("probe-delta", "delta **+0.233, p < 0.0001**", lambda d:
        d["gates"]["G-D1"]["delta_mean_1x_minus_15x"], 0.233, 3),
    ("probe-bm25-15x", "620 ms (15x)", lambda d:
        d["bm25_latency_ms"]["15x"], 620.0, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=PROBE_DOC, needle=_needle, artifacts=(PROBE,),
        value=_val, stated=_stated, places=_places))


# The judge-model ladder's published auto-reject precisions (CHANGELOG,
# 2026-08-16): the measured floor for the autonomous Step-C judge.
JUDGE_LADDER = RESULTS + "judge-ladder-20260816.json"
for _arm, _needle, _stated in [
    ("fable-5", "fable-5 1.0 (0 false in 73)", 1.0),
    ("opus-5", "opus-5 0.9867", 0.9867),
    ("sonnet-5", "sonnet-5 0.9589", 0.9589),
    ("qwen-27b", "qwen-27b 0.9175", 0.9175),
    ("sidecar-e4b", "sidecar-e4b 0.9583", 0.9583),
]:
    CLAIMS.append(Claim(
        id=f"judge-ladder-{_arm}-auto-prec", doc=CHANGELOG, needle=_needle,
        artifacts=(JUDGE_LADDER,),
        value=(lambda a: lambda d: d["arms"][a]["auto_reject_precision"])(_arm),
        stated=_stated, places=4))


# evals/README's -Fast description (rewritten at the 2026-08-20 docs pass)
# publishes the mainline-MTP migration numbers: byte-determinism and
# verdict-losslessness from the paired determinism check, and the 2.3x
# extraction-shaped decode speedup from the engine A/B probe.
EVALS_README = "evals/README.md"
MTP_DETERMINISM = RESULTS + "judge-determinism-check-qwen38-mtp.json"
B10488_PROBE = RESULTS + "engine-b10488-probe-20260819.json"
CLAIMS.append(Claim(
    id="mtp-byte-deterministic", doc=EVALS_README,
    needle="byte-deterministic",
    artifacts=(MTP_DETERMINISM,),
    value=lambda d: d["configurations"]["qwen38-mtp-repeat"]
                     ["response_diff_rate"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="mtp-verdict-lossless", doc=EVALS_README,
    needle="verdict-lossless",
    artifacts=(MTP_DETERMINISM,),
    value=lambda d: d["configurations"]["mtp-vs-stock"]["verdict_flip_rate"],
    stated=0.0, places=4))
CLAIMS.append(Claim(
    id="mtp-decode-speedup", doc=EVALS_README,
    needle="a 2.3× extraction-decode speedup",
    artifacts=(B10488_PROBE,),
    value=lambda d: (d["configs"]["b10488-ub256-mtp-n2"]["gen_per_second"]
                     / d["configs"]["b10488-ub256-stock"]["gen_per_second"]),
    stated=2.3, places=1))

# ── memory_recall output-cap size reduction (issue #186, 2026-08-25) ─────
# The live 93.7 KB / 53-entity / 75-edge / 45-text audit number is NOT
# pinned here: it's a one-off live-daemon measurement (2026-08-21) with no
# artifact and cannot be regenerated in-tree, so both docs attribute it to
# the audit in prose rather than publishing it as a checked claim. What IS
# checked is the reproducible in-tree probe's own before/after numbers,
# which appear verbatim in both docs.
RETRIEVAL_GUIDE = "docs/guide/retrieval.md"
RECALL_PROBE = RESULTS + "recall-cap-186-payload-probe.json"
_RECALL_CAP_NEEDLE = "24.5 KB → 3.8 KB (84.4%)"
for _doc in (RETRIEVAL_GUIDE, CHANGELOG):
    _slug = "guide" if _doc == RETRIEVAL_GUIDE else "changelog"
    CLAIMS.append(Claim(
        id=f"recall-cap-186-uncapped-{_slug}", doc=_doc,
        needle=_RECALL_CAP_NEEDLE, artifacts=(RECALL_PROBE,),
        value=lambda d: d["uncapped_bytes"] / 1000, stated=24.5, places=1))
    CLAIMS.append(Claim(
        id=f"recall-cap-186-capped-{_slug}", doc=_doc,
        needle=_RECALL_CAP_NEEDLE, artifacts=(RECALL_PROBE,),
        value=lambda d: d["capped_bytes_compact"] / 1000, stated=3.8,
        places=1))
    CLAIMS.append(Claim(
        id=f"recall-cap-186-reduction-pct-{_slug}", doc=_doc,
        needle=_RECALL_CAP_NEEDLE, artifacts=(RECALL_PROBE,),
        value=lambda d: d["reduction_pct_compact"], stated=84.4, places=1))


# ── the #173 multiple-choice re-score corrections (2026-08-25) ───────────
# The MC scorer's no-box fallback read the article "a" as answer A, so
# every lme-v2 number above is superseded. House rule "retire numbers at
# the old site": the original artifacts and their rows stay exactly as
# they were, and the correction is published beside them — which means the
# corrected numbers are claims in their own right and pin here too.
RESCORE = "-rescored-strictmc"
LME_V2_RS = RESULTS + "lme-v2-smoke-slice1" + RESCORE + ".agg.json"
LME_V2_FULL_RS = RESULTS + "lme-v2-smoke-slice2" + RESCORE + ".summary.json"
LME_V2_FULL_COMPOSE_RS = (RESULTS + "lme-v2-smoke-slice2-compose" + RESCORE
                          + ".summary.json")
PAIRED56_RS = (RESULTS + "lme-v2-qwen38-vs-slice2-paired56" + RESCORE
               + ".json")

for _arm, _needle, _ku, _compose in [
    ("rag", "| naive RAG (control) | 0.162 → **0.149** | 0.284 → **0.257** |",
     0.149, 0.257),
    ("cortex", "| cortex facts only | 0.068 → **0.068** | 0.216 → **0.176** |",
     0.068, 0.176),
    ("hybrid", "| hybrid | **0.243** → **0.203** | 0.284 → **0.270** |",
     0.203, 0.270),
]:
    CLAIMS.append(Claim(
        id=f"lmev2-full-ku-{_arm}-corrected", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_FULL_RS,),
        value=lambda d, a=_arm: d["arms"][a]["eval_accuracy"],
        stated=_ku, places=3))
    CLAIMS.append(Claim(
        id=f"lmev2-full-compose-{_arm}-corrected", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_FULL_COMPOSE_RS,),
        value=lambda d, a=_arm: d["arms"][a]["eval_accuracy"],
        stated=_compose, places=3))

# The corrected pilot rows are quoted as inline code in the superseding
# note, so each arm's needle is its own quoted fragment.
for _arm, _needle, _ku, _compose in [
    ("rag", "`0.300 [0.30–0.30] | 0.433 [0.40–0.50]`", 0.300, 0.433),
    ("cortex", "`0.167 [0.00–0.30] | 0.200 [0.10–0.30]`", 0.167, 0.200),
    ("hybrid", "`0.500 [0.40–0.60] | 0.533 [0.50–0.60]`", 0.500, 0.533),
]:
    CLAIMS.append(Claim(
        id=f"lmev2-ku-{_arm}-corrected", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_RS,), value=_mean(f"KU.{_arm}"),
        stated=_ku, places=3))
    CLAIMS.append(Claim(
        id=f"lmev2-compose-{_arm}-corrected", doc=BENCH, needle=_needle,
        artifacts=(LME_V2_RS,), value=_mean(f"compose.{_arm}"),
        stated=_compose, places=3))

# The corrected paired verdict. The p-value has its own artifact per the
# house rule, and the judge arm is pinned because "the judge arms
# reproduce the superseded artifact exactly" is the sentence that licenses
# reading the eval-arm movement as the scorer fix alone.
for _cid, _needle, _key, _field, _stated, _places in [
    ("paired56-corrected-cortex-delta",
     "the cortex delta +0.089 → **+0.036**", "cortex_correct", "delta",
     0.0357, 3),
    ("paired56-corrected-cortex-p",
     "sign-test p 0.125 → 0.625)", "cortex_correct", "sign_test_p", 0.625, 3),
    ("paired56-corrected-hybrid-delta",
     "hybrid −0.018 → **−0.036** (8W/9L →", "hybrid_correct", "delta",
     -0.0357, 3),
    ("paired56-judge-unchanged",
     "The judge arms are unaffected and", "rag_judge", "delta", -0.1071, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(PAIRED56_RS,),
        value=lambda d, k=_key, f=_field: d["arms"][k][f],
        stated=_stated, places=_places))


# ── #188: the full 500-question sweep replaces the KU slice up front ─────
# The README led with cascade 0.936 on 78 of 500 questions — the slice the
# supersession spine is built to win — while the committed six-type
# superset said "wash". Both the superset and the retirement of 0.936 are
# published claims now, so both pin here.
_ALL500_ROWS = [
    ("rag", "| naive RAG (top-6 turns) | 0.688 | ~1210 |", 0.688, 1210),
    ("cortex", "| cortex facts only | 0.416 | **~158** |", 0.416, 158),
    ("hybrid", "| hybrid (facts + top-3 turns) | 0.664 | ~842 |", 0.664, 842),
    ("cascade", "| **commit-gated cascade** | **0.690** | ~883 |", 0.690, 883),
]
for _doc, _slug in ((READ_ME, "readme"), (BENCH, "guide")):
    for _arm, _needle, _acc, _tokens in _ALL500_ROWS:
        CLAIMS.append(Claim(
            id=f"all500-{_slug}-{_arm}", doc=_doc, needle=_needle,
            artifacts=(ALLTYPES,),
            value=(lambda a: lambda d: d["arms"][a]["accuracy"])(_arm),
            stated=_acc, places=3))
        CLAIMS.append(Claim(
            id=f"all500-{_slug}-tokens-{_arm}", doc=_doc, needle=_needle,
            artifacts=(ALLTYPES,),
            value=(lambda a: lambda d: d["arms"][a]["context_tokens"])(_arm),
            stated=_tokens, places=0))

# The per-type breakdown — the part that says where the memory loses. The
# `cascade` column is a sibling key of `arms` in the summary, not a member
# of it (it is derived), hence the two accessors.
_PER_TYPE = [
    ("knowledge-update",
     "| knowledge-update | 78 | 0.859 | 0.756 | 0.910 | ~~0.936~~ "
     "(retired — [below](#the-knowledge-update-slice-78-of-the-500)) |",
     0.859, 0.756, 0.910, 0.936),
    ("single-session-user",
     "| single-session-user | 70 | 0.929 | 0.671 | 0.957 | 0.943 |",
     0.929, 0.671, 0.957, 0.943),
    ("single-session-assistant",
     "| single-session-assistant | 56 | 0.911 | 0.571 | 0.964 | 0.929 |",
     0.911, 0.571, 0.964, 0.929),
    ("single-session-preference",
     "| single-session-preference | 30 | 0.800 | 0.733 | 0.600 | 0.700 |",
     0.800, 0.733, 0.600, 0.700),
    ("temporal-reasoning",
     "| temporal-reasoning | 133 | 0.526 | 0.150 | 0.534 | 0.526 |",
     0.526, 0.150, 0.534, 0.526),
    ("multi-session",
     "| multi-session | 133 | 0.504 | 0.211 | 0.383 | 0.474 |",
     0.504, 0.211, 0.383, 0.474),
]
for _type, _needle, _r, _c, _h, _casc in _PER_TYPE:
    for _arm, _stated in (("rag", _r), ("cortex", _c), ("hybrid", _h)):
        CLAIMS.append(Claim(
            id=f"all500-type-{_type}-{_arm}", doc=BENCH, needle=_needle,
            artifacts=(ALLTYPES,),
            value=(lambda t, a: lambda d: d["types"][t]["arms"][a])(
                _type, _arm),
            stated=_stated, places=3))
    CLAIMS.append(Claim(
        id=f"all500-type-{_type}-cascade", doc=BENCH, needle=_needle,
        artifacts=(ALLTYPES,),
        value=(lambda t: lambda d: d["types"][t]["cascade"])(_type),
        stated=_casc, places=3))

# The README carries a narrower copy of the same breakdown (rag vs cascade
# only). Its knowledge-update cascade cell is the retired 0.936, struck and
# cross-referenced — pinned to the same artifact per the retire-at-the-old-
# site rule.
_PER_TYPE_README = [
    ("knowledge-update",
     "| knowledge-update (facts change) | 78 | 0.859 | ~~0.936~~ "
     "(retired — [why](docs/guide/benchmarks.md"
     "#the-knowledge-update-slice-78-of-the-500)) |", 0.859, 0.936),
    ("single-session-user",
     "| single-session-user | 70 | 0.929 | 0.943 |", 0.929, 0.943),
    ("single-session-assistant",
     "| single-session-assistant | 56 | 0.911 | 0.929 |", 0.911, 0.929),
    ("single-session-preference",
     "| single-session-preference | 30 | 0.800 | 0.700 |", 0.800, 0.700),
    ("temporal-reasoning",
     "| temporal-reasoning | 133 | 0.526 | 0.526 |", 0.526, 0.526),
    ("multi-session",
     "| multi-session | 133 | 0.504 | 0.474 |", 0.504, 0.474),
]
for _type, _needle, _r, _casc in _PER_TYPE_README:
    CLAIMS.append(Claim(
        id=f"all500-readme-type-{_type}-rag", doc=READ_ME, needle=_needle,
        artifacts=(ALLTYPES,),
        value=(lambda t: lambda d: d["types"][t]["arms"]["rag"])(_type),
        stated=_r, places=3))
    CLAIMS.append(Claim(
        id=f"all500-readme-type-{_type}-cascade", doc=READ_ME, needle=_needle,
        artifacts=(ALLTYPES,),
        value=(lambda t: lambda d: d["types"][t]["cascade"])(_type),
        stated=_casc, places=3))

# The two-stack table that retires 0.936: same 78 questions, Qwen3.6 stack
# vs the Qwen3.8 stack the bench migrated to on 2026-08-17. Both sides of
# every row are pinned, because the claim IS the pair.
for _arm, _needle, _old, _new in [
    ("rag", "| naive RAG (control) | 0.859 | 0.859 |", 0.859, 0.859),
    ("cortex", "| cortex facts only | 0.667 | 0.667 |", 0.667, 0.667),
    ("hybrid", "| hybrid (facts + top-3 turns) | 0.833 | 0.846 |",
     0.833, 0.846),
    ("cascade", "| **commit-gated cascade** | **0.936** | **0.846** |",
     0.936, 0.846),
]:
    CLAIMS.append(Claim(
        id=f"v38-transfer-{_arm}-old", doc=BENCH, needle=_needle,
        artifacts=(E2E,), value=_mean(_arm), stated=_old, places=3))
    CLAIMS.append(Claim(
        id=f"v38-transfer-{_arm}-new", doc=BENCH, needle=_needle,
        artifacts=(CEILING_V38,), value=_mean(_arm), stated=_new, places=3))
    CLAIMS.append(Claim(
        id=f"v38-transfer-{_arm}-new-std", doc=BENCH,
        needle="std 0.0000). The naive-RAG control lands on 0.859",
        artifacts=(CEILING_V38,), value=_std(_arm), stated=0.0, places=4))

# The abstention mechanism, recomputed from the committed per-question rows
# with the harness's OWN commit gate — a local re-implementation would let
# the pin drift away from the policy it claims to describe.
def _abstains(rows: list[dict]) -> float:
    return float(sum(1 for r in rows if not _commits(r)))


def _commits_n(rows: list[dict]) -> float:
    return float(sum(1 for r in rows if _commits(r)))


def _commit_precision(rows: list[dict]) -> float:
    committed = [r for r in rows if _commits(r)]
    return sum(1 for r in committed if r["cortex_correct"]) / len(committed)


for _cid, _doc, _needle, _art, _val, _stated, _places in [
    ("abstain-old-readme", READ_ME,
     "abstention behaviour as much as the memory: 32/78 abstentions",
     E2E_ROWS, _abstains, 32, 0),
    ("abstain-old-precision-readme", READ_ME,
     "at 46/46 commit precision on the old stack, 22/78 at 0.839 on the "
     "new one.", E2E_ROWS, _commit_precision, 1.0, 3),
    ("abstain-old-commits-readme", READ_ME,
     "at 46/46 commit precision on the old stack, 22/78 at 0.839 on the "
     "new one.", E2E_ROWS, _commits_n, 46, 0),
    ("abstain-new-readme", READ_ME,
     "at 46/46 commit precision on the old stack, 22/78 at 0.839 on the "
     "new one.", V38_ROWS_JSONL, _abstains, 22, 0),
    ("abstain-new-precision-readme", READ_ME,
     "at 46/46 commit precision on the old stack, 22/78 at 0.839 on the "
     "new one.", V38_ROWS_JSONL, _commit_precision, 0.839, 3),
    ("abstain-old-guide", BENCH,
     "cortex arm abstained on **32 of 78** questions and its 46 commits were",
     E2E_ROWS, _abstains, 32, 0),
    ("abstain-old-commits-guide", BENCH,
     "cortex arm abstained on **32 of 78** questions and its 46 commits were",
     E2E_ROWS, _commits_n, 46, 0),
    ("abstain-old-precision-guide", BENCH,
     "**46/46** correct; on the new stack it abstains **22 of 78** and its 56",
     E2E_ROWS, _commit_precision, 1.0, 3),
    ("abstain-new-guide", BENCH,
     "**46/46** correct; on the new stack it abstains **22 of 78** and its 56",
     V38_ROWS_JSONL, _abstains, 22, 0),
    ("abstain-new-commits-guide", BENCH,
     "**46/46** correct; on the new stack it abstains **22 of 78** and its 56",
     V38_ROWS_JSONL, _commits_n, 56, 0),
    ("abstain-new-precision-guide", BENCH,
     "commits are **0.839** precise", V38_ROWS_JSONL, _commit_precision,
     0.839, 3),
    ("abstain-old-evals", EVALS,
     "22/78 instead of 32/78 and its commit precision drops from 46/46 to "
     "0.839,", E2E_ROWS, _abstains, 32, 0),
    ("abstain-new-evals", EVALS,
     "22/78 instead of 32/78 and its commit precision drops from 46/46 to "
     "0.839,", V38_ROWS_JSONL, _abstains, 22, 0),
    ("abstain-new-precision-evals", EVALS,
     "22/78 instead of 32/78 and its commit precision drops from 46/46 to "
     "0.839,", V38_ROWS_JSONL, _commit_precision, 0.839, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))

# The retirement restated at the README front door and in evals/README.
for _cid, _doc, _needle, _art, _val, _stated in [
    ("retire-readme-cascade-846", READ_ME,
     "Qwen3.8-27B puts the cascade at **0.846**, below the naive-RAG control",
     CEILING_V38, _mean("cascade"), 0.846),
    ("retire-readme-control", READ_ME,
     "which lands on 0.859 on both stacks", CEILING_V38, _mean("rag"), 0.859),
    ("retire-evals-cascade-846", EVALS, "gives cascade **0.846**",
     CEILING_V38, _mean("cascade"), 0.846),
    ("retire-evals-control", EVALS,
     "against an unchanged naive-RAG control of 0.859", CEILING_V38,
     _mean("rag"), 0.859),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=3))

# ── BEAM, documented for the first time (2026-08-25, #188) ───────────────
# The abstention row is the load-bearing one: it is the single published
# claim that reproduces unchanged across two judge families, which is
# exactly the property the retired 0.936 lacked.
def _beam_abstention(arm: str) -> Callable[[dict], float]:
    return lambda d: d["types"]["abstention"][arm]



for _cid, _doc, _needle, _art, _arm, _stated in [
    ("beam-abstain-cortex-readme-teaser", READ_ME,
     "abstention questions the fact spine scores **0.950**",
     BEAM_Q38, "cortex", 0.950),
    ("beam-abstain-cortex-readme-teaser-opus", READ_ME,
     "abstention questions the fact spine scores **0.950**",
     BEAM_OPUS, "cortex", 0.950),
    ("beam-abstain-rag-readme-teaser", READ_ME,
     "0.775, unchanged under two independent judges", BEAM_Q38, "rag", 0.775),
    ("beam-abstain-rag-readme-teaser-opus", READ_ME,
     "0.775, unchanged under two independent judges", BEAM_OPUS, "rag", 0.775),
    ("beam-abstain-cortex-readme-body", READ_ME,
     "the fact-spine arm scores 0.950 against naive RAG's 0.775",
     BEAM_Q38, "cortex", 0.950),
    ("beam-abstain-rag-readme-body", READ_ME,
     "the fact-spine arm scores 0.950 against naive RAG's 0.775",
     BEAM_OPUS, "rag", 0.775),
    ("beam-abstain-cortex-evals", EVALS,
     "the cortex arm scores **0.950** against naive RAG's 0.775",
     BEAM_Q38, "cortex", 0.950),
    ("beam-abstain-cortex-evals-opus", EVALS,
     "the cortex arm scores **0.950** against naive RAG's 0.775",
     BEAM_OPUS, "cortex", 0.950),
    ("beam-abstain-rag-evals-opus", EVALS,
     "the cortex arm scores **0.950** against naive RAG's 0.775",
     BEAM_OPUS, "rag", 0.775),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(_art,),
        value=_beam_abstention(_arm), stated=_stated, places=3))

_BEAM_BUDGET = "rag 0.6425 vs hybrid 0.6226 (−0.020 ± 0.029, a wash)"
_BEAM_TRANSFER = ("moved rag −0.002, cortex +0.007, hybrid −0.016, against a "
                  "same-judge stability floor of mean \\|item delta\\| 0.073")
for _cid, _needle, _art, _val, _stated, _places in [
    ("beam-p1b16-rag", _BEAM_BUDGET, BEAM_P1B16,
     lambda d: d["arms"]["rag"]["score"], 0.6425, 4),
    ("beam-p1b16-hybrid", _BEAM_BUDGET, BEAM_P1B16,
     lambda d: d["arms"]["hybrid"]["score"], 0.6226, 4),
    ("beam-transfer-rag", _BEAM_TRANSFER, BEAM_OPUS,
     lambda d: d["arms"]["rag"]["delta"], -0.002, 3),
    ("beam-transfer-cortex", _BEAM_TRANSFER, BEAM_OPUS,
     lambda d: d["arms"]["cortex"]["delta"], 0.007, 3),
    ("beam-transfer-hybrid", _BEAM_TRANSFER, BEAM_OPUS,
     lambda d: d["arms"]["hybrid"]["delta"], -0.016, 3),
    ("beam-transfer-floor", _BEAM_TRANSFER, BEAM_OPUS,
     lambda d: d["stability_sample"]["mean_abs_delta"], 0.073, 3),
    ("beam-volume-rag48", "takes a local 27B reader to 0.665 full-tier",
     BEAM_GRID, lambda d: d["qwen_full_n400"]["rag48"], 0.665, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))

# ── merge-proposal snippet differential (2026-08-30 live replay) ──────────
# The CHANGELOG's before/after low-differential shares for the snippet-
# attachment fix, pinned to the committed live-queue replay
# (evals/snippet_differential_replay.py), plus the 2026-08-21 shadow
# comparison's 37% defect share that motivated it.
SNIPPET_DIFF = RESULTS + "snippet-differential-live-20260830.json"
JUDGE_SHADOW = RESULTS + "judge-shadow-live-20260821.json"
CLAIMS.append(Claim(
    id="snippet-shadow-share", doc=CHANGELOG,
    needle="merge proposals (37%) carried low-differential evidence",
    artifacts=(JUDGE_SHADOW,),
    value=lambda d: d["evidence_quality"]["share"], stated=0.37, places=2))
for _cid, _half, _needle, _stated in [
    ("snippet-diff-before", "before",
     "evidence share on the live queue from 36% (55 of 152)", 0.36),
    ("snippet-diff-after", "after",
     "to 12% (18 of 152), with zero empty sides remaining", 0.12)]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle,
        artifacts=(SNIPPET_DIFF,),
        value=(lambda h: lambda d: d[h]["low_differential_share"])(_half),
        stated=_stated, places=2))

# ── 2026-08-30: the live shadow-judge record + the promoted 74-row slice ─
# The shadow-vs-triage comparison that justified flipping the deployed
# judge_mode, and the paired74 working copies promoted with the #173
# strict-MC re-score (the raw artifact's retired numbers pin too — the
# CHANGELOG states them as the thing being retired).
SHADOW_LIVE = RESULTS + "judge-shadow-live-20260821.json"
PAIRED74 = RESULTS + "lme-v2-qwen38-vs-slice2-paired74.json"
PAIRED74_RS = (RESULTS + "lme-v2-qwen38-vs-slice2-paired74" + RESCORE
               + ".json")

for _cid, _needle, _art, _val, _stated, _places in [
    ("shadow-live-auto-reject-precision",
     "auto-reject precision is **1.000**", SHADOW_LIVE,
     lambda d: d["auto_reject_simulation"]["live_auto_reject_precision"],
     1.000, 3),
    ("shadow-live-auto-rejected",
     "76/109 proposals cleared automatically", SHADOW_LIVE,
     lambda d: d["auto_reject_simulation"]["would_have_applied"], 76, 0),
    ("shadow-live-agreement",
     "overall agreement 0.927", SHADOW_LIVE,
     lambda d: d["metrics_overall"]["agreement_on_decided"], 0.927, 3),
    ("shadow-live-accept-precision",
     "accept precision is only 0.611", SHADOW_LIVE,
     lambda d: d["metrics_overall"]["accept_precision"], 0.611, 3),
    ("paired74-raw-cortex-delta",
     "+0.0946 (8W/1L, sign-test p 0.0391)", PAIRED74,
     lambda d: d["arms"]["cortex_correct"]["delta"], 0.0946, 4),
    ("paired74-raw-cortex-p",
     "+0.0946 (8W/1L, sign-test p 0.0391)", PAIRED74,
     lambda d: d["arms"]["cortex_correct"]["sign_test_p"], 0.0391, 4),
    ("paired74-corrected-cortex-delta",
     "re-scored it is **+0.0405** (5W/2L, p 0.453)", PAIRED74_RS,
     lambda d: d["arms"]["cortex_correct"]["delta"], 0.0405, 4),
    ("paired74-corrected-cortex-p",
     "re-scored it is **+0.0405** (5W/2L, p 0.453)", PAIRED74_RS,
     lambda d: d["arms"]["cortex_correct"]["sign_test_p"], 0.453, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))

# The 2026-08-31 judge-ladder night run: the CHANGELOG states how many
# fixture rows the caution flag marks, and how many true-accept rows the
# budget-truncated xhigh arm lost (the void that justified --max-tokens).
JUDGE_CAUTION = RESULTS + "judge-ladder-caution-20260831.json"
JUDGE_EFFORT = RESULTS + "judge-ladder-effort-20260831.json"

for _cid, _needle, _art, _val, _stated in [
    ("judge-caution-flagged-rows",
     "40 of its 129 rows flag", JUDGE_CAUTION,
     lambda d: d["arms"]["qwen-27b-thinklow"]["caution_rows"], 40),
    ("judge-xhigh-truncated-accepts",
     "truncated away 30 of the 30 true-accept rows (batches 0-3)",
     JUDGE_EFFORT,
     lambda d: sum(1 for r in d["arms"]["qwen-27b-xhigh"]["per_row"]
                   if r["label"] == "accept"
                   and all(v is None for v in r["votes"])), 30),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=0))

# The GPT-5.6 Terra and Luna ceiling probes' first measurements
# (2026-09-01, single runs on the ChatGPT-plan Codex shim): the evals
# README publishes their gold/stale parity with the Claude ceiling rungs
# and their wordier slot values (tokens/query well above the Claude
# rungs, still inside the gate).
for _rung, _tok_needle, _tok in [
    ("terra", "13.1 tokens/query", 13.1),
    ("luna", "14.6 tokens/query", 14.6),
]:
    _art = RESULTS + f"{_rung}.json"
    for _cid, _needle, _val, _stated, _places in [
        (f"{_rung}-ladder-gold", "gold_recoverable 1.0 / stale_leak 0.0",
         lambda d: d["gold_recoverable"], 1.0, 3),
        (f"{_rung}-ladder-stale", "gold_recoverable 1.0 / stale_leak 0.0",
         lambda d: d["stale_leak"], 0.0, 3),
        (f"{_rung}-ladder-tokens", _tok_needle,
         lambda d: d["tokens_per_query"], _tok, 1),
    ]:
        CLAIMS.append(Claim(
            id=_cid, doc=EVALS_README, needle=_needle, artifacts=(_art,),
            value=_val, stated=_stated, places=_places))

# The gold-answer leak check's first run (2026-09-01, evals/leak_check.py
# over the committed 2026-08-21 BEAM artifact). It is CPU-only re-parsing
# — no model calls — so the numbers regenerate exactly. The recomputed arm
# means are pinned too: they are the reason the leak-free comparator can
# be trusted, and they must keep reproducing the run's own summary.
BEAM38_LEAKCHECK = (RESULTS
                    + "beam-100K-qwen-27b-beam100k-qwen38.leakcheck.json")
_SPLIT_NEEDLE = "**200 `no_gold`** and **10 `trivial_gold`**"
for _cid, _needle, _val, _stated in [
    ("beam38-leakcheck-leaked", "**0 leaked rows**",
     lambda d: d["n_leaked"], 0),
    ("beam38-leakcheck-rows", "committed 2026-08-21 BEAM run (400 rows)",
     lambda d: d["n_rows"], 400),
    ("beam38-leakcheck-no-gold", _SPLIT_NEEDLE,
     lambda d: d["untestable_reasons"]["no_gold"], 200),
    ("beam38-leakcheck-trivial-gold", _SPLIT_NEEDLE,
     lambda d: d["untestable_reasons"]["trivial_gold"], 10),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS_README, needle=_needle,
        artifacts=(BEAM38_LEAKCHECK,), value=_val, stated=_stated, places=0))
for _arm, _stated in (("rag", 0.5005), ("cortex", 0.2918),
                      ("hybrid", 0.4682)):
    CLAIMS.append(Claim(
        id=f"beam38-leakcheck-{_arm}-leak-free", doc=EVALS_README,
        needle="(rag 0.5005, cortex 0.2918, hybrid 0.4682)",
        artifacts=(BEAM38_LEAKCHECK,),
        value=(lambda a: lambda d: d["arms"][a]["leak_free"])(_arm),
        stated=_stated, places=4))
# The testable-only slice published beside them (190 of the 400 rows).
_TESTABLE_NEEDLE = "**rag 0.4789, cortex 0.1759, hybrid 0.4229**"
for _arm, _stated in (("rag", 0.4789), ("cortex", 0.1759),
                      ("hybrid", 0.4229)):
    CLAIMS.append(Claim(
        id=f"beam38-leakcheck-{_arm}-testable", doc=EVALS_README,
        needle=_TESTABLE_NEEDLE, artifacts=(BEAM38_LEAKCHECK,),
        value=(lambda a: lambda d: d["arms"][a]["leak_free_testable"])(_arm),
        stated=_stated, places=4))
CLAIMS.append(Claim(
    id="beam38-leakcheck-testable-n", doc=EVALS_README,
    needle="over only the 190 rows", artifacts=(BEAM38_LEAKCHECK,),
    value=lambda d: d["arms"]["rag"]["n_testable"], stated=190, places=0))

# The same check over the committed LongMemEval ceiling-e2e run
# (2026-09-01). Its recomputed rag mean is pinned against the e2e table's
# own 0.859 above: two independent readings of one artifact, so a drift in
# either goes red.
LME_E2E_LEAKCHECK = (RESULTS
                     + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e"
                     + ".leakcheck.json")
for _cid, _needle, _val, _stated, _places in [
    ("lme-leakcheck-leaked", "the leak check finds **0 leaked rows**",
     lambda d: d["n_leaked"], 0, 0),
    ("lme-leakcheck-rows", "(78 knowledge-update\nquestions)",
     lambda d: d["n_rows"], 78, 0),
    ("lme-leakcheck-trivial-gold", "its 27 untestable\nrows are **all `trivial_gold`**",
     lambda d: d["untestable_reasons"]["trivial_gold"], 27, 0),
    ("lme-leakcheck-no-gold-class-absent",
     "there is no `no_gold` class here",
     lambda d: d["untestable_reasons"].get("no_gold", 0), 0, 0),
    ("lme-leakcheck-rag", "(rag 0.859,\nhybrid 0.8333, cortex 0.6667)",
     lambda d: d["arms"]["rag"]["all"], 0.859, 3),
    ("lme-leakcheck-hybrid", "(rag 0.859,\nhybrid 0.8333, cortex 0.6667)",
     lambda d: d["arms"]["hybrid"]["all"], 0.8333, 4),
    ("lme-leakcheck-cortex", "(rag 0.859,\nhybrid 0.8333, cortex 0.6667)",
     lambda d: d["arms"]["cortex"]["all"], 0.6667, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS_README, needle=_needle,
        artifacts=(LME_E2E_LEAKCHECK,), value=_val, stated=_stated,
        places=_places))

# The answerability + pathway probe's first run (2026-09-01,
# evals/answerability_probe.py — CPU-only re-parsing, regenerates
# exactly). The ceiling-e2e cross-tab and pathway shares are pinned, and
# so is the fact the 2026-08-21 BEAM artifact is entirely untestable
# (it predates context persistence) — that coverage gap is itself the
# published claim.
LME_E2E_ANSWERABILITY = (RESULTS
                         + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e"
                         + ".answerability.json")
_ANS_SHARE_NEEDLE = "**rag 0.9556, hybrid 0.9111, cortex 0.6222**"
_RED_FLAG_NEEDLE = "**rag 2, hybrid 1, cortex 3**"
_PATHWAY_NEEDLE = "**rag 0.9189, hybrid 0.9429, cortex 0.8889**"
for _cid, _needle, _val, _stated, _places in [
    ("lme-answerability-rows", "(**78 rows**, 45 testable per arm",
     lambda d: d["n_rows"], 78, 0),
    ("lme-answerability-testable", "(**78 rows**, 45 testable per arm",
     lambda d: d["arms"]["rag"]["n_testable"], 45, 0),
    ("lme-answerability-trivial", "**27 `trivial_gold`, 6 `abstention`**",
     lambda d: d["arms"]["rag"]["untestable_reasons"]["trivial_gold"],
     27, 0),
    ("lme-answerability-abstention",
     "**27 `trivial_gold`, 6 `abstention`**",
     lambda d: d["arms"]["rag"]["untestable_reasons"]["abstention"], 6, 0),
    ("lme-answerability-cortex-unans-wrong",
     "(**14** `unanswerable_wrong` against 4 `answerable_wrong`)",
     lambda d: d["arms"]["cortex"]["cells"]["unanswerable_wrong"], 14, 0),
    ("lme-answerability-cortex-ans-wrong",
     "(**14** `unanswerable_wrong` against 4 `answerable_wrong`)",
     lambda d: d["arms"]["cortex"]["cells"]["answerable_wrong"], 4, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS_README, needle=_needle,
        artifacts=(LME_E2E_ANSWERABILITY,), value=_val, stated=_stated,
        places=_places))
for _arm, _stated in (("rag", 0.9556), ("hybrid", 0.9111),
                      ("cortex", 0.6222)):
    CLAIMS.append(Claim(
        id=f"lme-answerability-{_arm}-share", doc=EVALS_README,
        needle=_ANS_SHARE_NEEDLE, artifacts=(LME_E2E_ANSWERABILITY,),
        value=(lambda a: lambda d: d["arms"][a]["answerable_share"])(_arm),
        stated=_stated, places=4))
for _arm, _stated in (("rag", 2), ("hybrid", 1), ("cortex", 3)):
    CLAIMS.append(Claim(
        id=f"lme-answerability-{_arm}-red-flag", doc=EVALS_README,
        needle=_RED_FLAG_NEEDLE, artifacts=(LME_E2E_ANSWERABILITY,),
        value=(lambda a: lambda d:
               d["arms"][a]["cells"]["unanswerable_correct"])(_arm),
        stated=_stated, places=0))
for _arm, _stated in (("rag", 0.9189), ("hybrid", 0.9429),
                      ("cortex", 0.8889)):
    CLAIMS.append(Claim(
        id=f"lme-answerability-{_arm}-pathway", doc=EVALS_README,
        needle=_PATHWAY_NEEDLE, artifacts=(LME_E2E_ANSWERABILITY,),
        value=(lambda a: lambda d:
               d["arms"][a]["pathway"]["supported_share"])(_arm),
        stated=_stated, places=4))

# The manual red-flag audit is a published conclusion ("no confirmed
# memory-support failure"), so its evidence is a committed artifact like
# any other — one served-evidence snippet per audited arm-row
# (tests/test_answerability_probe.py keeps it in sync with the probe's
# red-flag ids).
LME_E2E_REDFLAG_AUDIT = (RESULTS
                         + "longmemeval-ku-oracle-qwen-27b-ceiling-e2e"
                         + ".redflag-audit.json")
for _cid, _val, _stated in [
    ("lme-redflag-audit-arm-rows",
     lambda d: d["n_arm_rows"], 6),
    ("lme-redflag-audit-questions",
     lambda d: d["n_questions"], 3),
    ("lme-redflag-audit-all-inference-gap",
     lambda d: sum(1 for e in d["entries"]
                   if e["verdict"] == "inference_gap"), 6),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS_README,
        needle="**six red-flag arm-rows (three distinct questions)**",
        artifacts=(LME_E2E_REDFLAG_AUDIT,), value=_val, stated=_stated,
        places=0))

BEAM38_ANSWERABILITY = (RESULTS
                        + "beam-100K-qwen-27b-beam100k-qwen38"
                        + ".answerability.json")
_BEAM38_ANS_NEEDLE = ("**200 `no_gold`,\n10 `trivial_gold`, "
                      "190 `no_context`**")
for _cid, _needle, _val, _stated in [
    ("beam38-answerability-rows",
     "probe classifies all **400 rows** untestable",
     lambda d: d["n_rows"], 400),
    ("beam38-answerability-testable", "`n_testable`\n**0** on every arm",
     lambda d: max(a["n_testable"] for a in d["arms"].values()), 0),
    ("beam38-answerability-no-gold", _BEAM38_ANS_NEEDLE,
     lambda d: d["arms"]["rag"]["untestable_reasons"]["no_gold"], 200),
    ("beam38-answerability-trivial", _BEAM38_ANS_NEEDLE,
     lambda d: d["arms"]["rag"]["untestable_reasons"]["trivial_gold"], 10),
    ("beam38-answerability-no-context", _BEAM38_ANS_NEEDLE,
     lambda d: d["arms"]["rag"]["untestable_reasons"]["no_context"], 190),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS_README, needle=_needle,
        artifacts=(BEAM38_ANSWERABILITY,), value=_val, stated=_stated,
        places=0))

# The BEAM findings table also quotes three RANGES that live in a verdict
# file as strings, not floats — the Claim machinery only compares numbers,
# so they get their own check rather than going unguarded.
_BEAM_VERDICT_QUOTES = [
    (BEAM_SWEEP, "summarization", "0.38 -> 0.47"),
    (BEAM_SWEEP, "event_ordering", "0.21 -> 0.52"),
    (BEAM_SWEEP, "abstention", "0.62 -> 0.50"),
]


# The v35 write-time label heuristic audit (2026-09-02,
# evals/label_heuristic_audit.py — regenerates from a bank dump plus the
# committed verdict-hash file; no bank text is committed). The CHANGELOG
# quotes the shipped rule's fact hit rate, precision and entry count and
# the three rejected variants; labels.py's module docstring quotes the
# decomposition. Both are pinned to the artifact.
LABEL_AUDIT = RESULTS + "label-heuristic-audit-20260902.json"
LABELS_PY = "pseudolife_memory/memory/labels.py"
# 2026-09-03: the rule stopped reading "a must-read" / "materials are a
# must" as deontics. The CHANGELOG's 2026-09-02 entry keeps quoting the
# pre-fix artifact (retired at its site); the module docstring and the
# 2026-09-03 Fixed entry quote the re-measurement — live bank, the same
# dump under the pre-fix rule, and the chip-5 BEAM chat-text replay.
LABEL_AUDIT_0903 = RESULTS + "label-heuristic-audit-20260903.json"
LABEL_AUDIT_0903_PREFIX = RESULTS + "label-heuristic-audit-20260903-prefix-rule.json"
LABEL_AUDIT_0903_BEAM = RESULTS + "label-heuristic-audit-20260903-beam-chip5.json"
_LA_SHIPPED = lambda d: d["distortion_tolerance_variants"][  # noqa: E731
    "shipped_strong_or_framing_or_opener_cap400"]
for _cid, _doc, _needle, _val, _stated, _places in [
    ("label-audit-fact-hit-rate", "CHANGELOG.md",
     "fires on ~1.6% of facts at ~0.86",
     lambda d: _LA_SHIPPED(d)["fact_hit_rate"] * 100, 1.6, 1),
    ("label-audit-precision", "CHANGELOG.md",
     "fires on ~1.6% of facts at ~0.86",
     lambda d: _LA_SHIPPED(d)["precision"], 0.86, 2),
    ("label-audit-entry-hits", "CHANGELOG.md", "on 1 of 836 entries",
     lambda d: _LA_SHIPPED(d)["entry_hits"], 1, 0),
    ("label-audit-entries", "CHANGELOG.md", "on 1 of 836 entries",
     lambda d: d["sample"]["current_entries"], 836, 0),
    ("label-audit-loose-entry-rate", "CHANGELOG.md",
     "any deontic\n    word anywhere: 36% of entries",
     lambda d: d["distortion_tolerance_variants"][
         "loose_any_deontic_word_anywhere_no_cap"]["entry_hit_rate"] * 100,
     36, 0),
    ("label-audit-imperative-anywhere", "CHANGELOG.md",
     "mid-sentence never/always: 0.53",
     lambda d: d["distortion_tolerance_variants"][
         "imperative_never_always_do_not_anywhere"]["precision"], 0.53, 2),
    ("label-audit-attribute-rule", "CHANGELOG.md",
     "attribute-name rule: 0.52",
     lambda d: d["distortion_tolerance_variants"][
         "attribute_name_rule_increment"]["precision"], 0.52, 2),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(LABEL_AUDIT,),
        value=_val, stated=_stated, places=_places))

_LA_STRONG = lambda d: _LA_SHIPPED(d)["decomposition"][  # noqa: E731
    "strong_deontic_or_framing"]
_LA_OPENER = lambda d: _LA_SHIPPED(d)["decomposition"][  # noqa: E731
    "imperative_opener_increment"]
_LA_DOC_SHIPPED = ("fires on 86 facts (1.6%) of which 73 read as a genuine "
                   "rule (0.85)")
_LA_FIX_LIVE = "86 fact hits, 73 genuine, 0.85;"
_LA_FIX_PREFIX = "the pre-fix rule on the same dump: 88 hits, 73 genuine, 0.83"
_LA_FIX_PARTS = "Strong-deontic part 62 of 75, opener increment"
_LA_FIX_BEAM = "8 hits, 8 genuine"
for _cid, _doc, _needle, _art, _val, _stated, _places in [
    # the module docstring (re-measured)
    ("label-fix-doc-shipped-hits", LABELS_PY, _LA_DOC_SHIPPED,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["fact_hits"], 86, 0),
    ("label-fix-doc-shipped-genuine", LABELS_PY, _LA_DOC_SHIPPED,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["judged_genuine"], 73, 0),
    ("label-fix-doc-shipped-precision", LABELS_PY, _LA_DOC_SHIPPED,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["precision"], 0.85, 2),
    ("label-fix-doc-strong", LABELS_PY, "62 of 75",
     LABEL_AUDIT_0903, lambda d: _LA_STRONG(d)["judged_genuine"], 62, 0),
    ("label-fix-doc-strong-hits", LABELS_PY, "62 of 75",
     LABEL_AUDIT_0903, lambda d: _LA_STRONG(d)["fact_hits"], 75, 0),
    ("label-fix-doc-opener", LABELS_PY, "11 of 11",
     LABEL_AUDIT_0903, lambda d: _LA_OPENER(d)["judged_genuine"], 11, 0),
    ("label-fix-doc-opener-hits", LABELS_PY, "11 of 11",
     LABEL_AUDIT_0903, lambda d: _LA_OPENER(d)["fact_hits"], 11, 0),
    ("label-fix-doc-entries", LABELS_PY, "and on 1 of 869 entries",
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["entry_hits"], 1, 0),
    ("label-fix-doc-entries-total", LABELS_PY, "and on 1 of 869 entries",
     LABEL_AUDIT_0903, lambda d: d["sample"]["current_entries"], 869, 0),
    ("label-fix-doc-beam-hits", LABELS_PY, "fires\non 8 values, all 8",
     LABEL_AUDIT_0903_BEAM, lambda d: _LA_SHIPPED(d)["fact_hits"], 8, 0),
    ("label-fix-doc-beam-genuine", LABELS_PY, "fires\non 8 values, all 8",
     LABEL_AUDIT_0903_BEAM, lambda d: _LA_SHIPPED(d)["judged_genuine"], 8, 0),
    # the CHANGELOG Fixed entry
    ("label-fix-live-hits", CHANGELOG, _LA_FIX_LIVE,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["fact_hits"], 86, 0),
    ("label-fix-live-genuine", CHANGELOG, _LA_FIX_LIVE,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["judged_genuine"], 73, 0),
    ("label-fix-live-precision", CHANGELOG, _LA_FIX_LIVE,
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["precision"], 0.85, 2),
    ("label-fix-prefix-hits", CHANGELOG, _LA_FIX_PREFIX,
     LABEL_AUDIT_0903_PREFIX, lambda d: _LA_SHIPPED(d)["fact_hits"], 88, 0),
    ("label-fix-prefix-genuine", CHANGELOG, _LA_FIX_PREFIX,
     LABEL_AUDIT_0903_PREFIX, lambda d: _LA_SHIPPED(d)["judged_genuine"],
     73, 0),
    ("label-fix-prefix-precision", CHANGELOG, _LA_FIX_PREFIX,
     LABEL_AUDIT_0903_PREFIX, lambda d: _LA_SHIPPED(d)["precision"], 0.83, 2),
    ("label-fix-strong", CHANGELOG, _LA_FIX_PARTS,
     LABEL_AUDIT_0903, lambda d: _LA_STRONG(d)["judged_genuine"], 62, 0),
    ("label-fix-strong-hits", CHANGELOG, _LA_FIX_PARTS,
     LABEL_AUDIT_0903, lambda d: _LA_STRONG(d)["fact_hits"], 75, 0),
    ("label-fix-opener", CHANGELOG, "11 of 11, still 1 of 869 entries",
     LABEL_AUDIT_0903, lambda d: _LA_OPENER(d)["judged_genuine"], 11, 0),
    ("label-fix-opener-hits", CHANGELOG, "11 of 11, still 1 of 869 entries",
     LABEL_AUDIT_0903, lambda d: _LA_OPENER(d)["fact_hits"], 11, 0),
    ("label-fix-entries", CHANGELOG, "11 of 11, still 1 of 869 entries",
     LABEL_AUDIT_0903, lambda d: _LA_SHIPPED(d)["entry_hits"], 1, 0),
    ("label-fix-sample-entries", CHANGELOG, "(869 entries / 5,435 facts,",
     LABEL_AUDIT_0903, lambda d: d["sample"]["current_entries"], 869, 0),
    ("label-fix-sample-facts", CHANGELOG, "(869 entries / 5,435 facts,",
     LABEL_AUDIT_0903, lambda d: d["sample"]["current_facts"], 5435, 0),
    ("label-fix-beam-hits", CHANGELOG, _LA_FIX_BEAM,
     LABEL_AUDIT_0903_BEAM, lambda d: _LA_SHIPPED(d)["fact_hits"], 8, 0),
    ("label-fix-beam-genuine", CHANGELOG, _LA_FIX_BEAM,
     LABEL_AUDIT_0903_BEAM, lambda d: _LA_SHIPPED(d)["judged_genuine"], 8, 0),
    ("label-fix-beam-strong-zero", CHANGELOG, _LA_FIX_BEAM,
     LABEL_AUDIT_0903_BEAM, lambda d: _LA_STRONG(d)["fact_hits"], 0, 0),
    ("label-fix-beam-facts", CHANGELOG, "(1,099 facts of chat text,",
     LABEL_AUDIT_0903_BEAM, lambda d: d["sample"]["current_facts"], 1099, 0),
    ("label-fix-beam-superset", CHANGELOG, "Of the 16 superset hits the 8 non-genuine",
     LABEL_AUDIT_0903_BEAM,
     lambda d: d["distortion_tolerance_variants"]["audited_superset_cap400"][
         "fact_hits"], 16, 0),
    ("label-fix-beam-superset-nongenuine", CHANGELOG,
     "Of the 16 superset hits the 8 non-genuine", LABEL_AUDIT_0903_BEAM,
     lambda d: (d["distortion_tolerance_variants"]["audited_superset_cap400"][
         "fact_hits"] - d["distortion_tolerance_variants"][
         "audited_superset_cap400"]["judged_genuine"]), 8, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))


# ── the chip-5 paired gates (CHANGELOG, 2026-09-03) ────────────────────────
# PR #245 shipped with its extraction-ladder arms GATE-PENDING; the gates
# ran 2026-09-03 and the CHANGELOG entry now quotes them. The ladder
# verdict and the BEAM paired comparison are committed beside the runs
# they were computed from, and the per-run summaries are pinned too, so
# the paired file, the rows and the summaries cannot drift apart.
LADDER_CHIP5 = RESULTS + "ladder-chip5-paired-verdict.json"
BEAM_CHIP5_PAIRED = _BEAM + "chip5-b16.vs-chip12-b16.paired.json"
BEAM_CHIP5 = _BEAM + "chip5-b16.summary.json"
BEAM_CHIP12 = _BEAM + "chip12-b16.summary.json"
BEAM_CHIP5_LABELS = _BEAM + "chip5-b16.labels.json"
_LADDER_IDENT = "verdict-identical on both rungs"
_LADDER_FLOOR = "(floor gold 0.1 / stale 0.1 / 3.4 tok per query;"
_LADDER_QWEN = "qwen-27b gold 1.0 / stale 0.0 / 13.4 tok per query"
_CHIP5_RAG = "the identical-input `rag` control moved 0.0000 (0 rows);"
_CHIP5_HYBRID = "hybrid +0.0004 ± 0.0014 (0.6226 → 0.6230);"
_CHIP5_CORTEX = "cortex +0.0036 ± 0.0029 (0.2829 → 0.2866)"
_CHIP5_CTX = "The 30 rows whose served context differs"
_CHIP5_LABELS = "(3 of 1099 facts; `quoted` 11 of 1099)"


def _rung_metric(rung: str, metric: str) -> Callable[[dict], float]:
    return lambda d: d["rungs"][rung]["metrics"][metric]["post"]


def _rung_identical(rung: str) -> Callable[[dict], float]:
    return lambda d: float(d["rungs"][rung]["identical"])


def _paired(arm: str, key: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm][key]


def _beam_score(arm: str) -> Callable[[dict], float]:
    return lambda d: d["arms"][arm]["score"]


for _cid, _needle, _art, _val, _stated, _places in [
    ("chip5-ladder-floor-identical", _LADDER_IDENT, LADDER_CHIP5,
     _rung_identical("floor"), 1, 0),
    ("chip5-ladder-qwen-identical", _LADDER_IDENT, LADDER_CHIP5,
     _rung_identical("qwen-27b"), 1, 0),
    ("chip5-ladder-floor-gold", _LADDER_FLOOR, LADDER_CHIP5,
     _rung_metric("floor", "gold_recoverable"), 0.1, 1),
    ("chip5-ladder-floor-stale", _LADDER_FLOOR, LADDER_CHIP5,
     _rung_metric("floor", "stale_leak"), 0.1, 1),
    ("chip5-ladder-floor-tokens", _LADDER_FLOOR, LADDER_CHIP5,
     _rung_metric("floor", "tokens_per_query"), 3.4, 1),
    ("chip5-ladder-qwen-gold", _LADDER_QWEN, LADDER_CHIP5,
     _rung_metric("qwen-27b", "gold_recoverable"), 1.0, 1),
    ("chip5-ladder-qwen-stale", _LADDER_QWEN, LADDER_CHIP5,
     _rung_metric("qwen-27b", "stale_leak"), 0.0, 1),
    ("chip5-ladder-qwen-tokens", _LADDER_QWEN, LADDER_CHIP5,
     _rung_metric("qwen-27b", "tokens_per_query"), 13.4, 1),
    ("chip5-beam-paired-rows", "paired on all 400 questions",
     BEAM_CHIP5_PAIRED, lambda d: d["paired_rows"], 400, 0),
    ("chip5-beam-rag-delta", _CHIP5_RAG, BEAM_CHIP5_PAIRED,
     _paired("rag", "delta_mean"), 0.0, 4),
    ("chip5-beam-rag-moved", _CHIP5_RAG, BEAM_CHIP5_PAIRED,
     _paired("rag", "rows_moved"), 0, 0),
    ("chip5-beam-hybrid-delta", _CHIP5_HYBRID, BEAM_CHIP5_PAIRED,
     _paired("hybrid", "delta_mean"), 0.0004, 4),
    ("chip5-beam-hybrid-se", _CHIP5_HYBRID, BEAM_CHIP5_PAIRED,
     _paired("hybrid", "delta_se"), 0.0014, 4),
    ("chip5-beam-hybrid-a", _CHIP5_HYBRID, BEAM_CHIP12,
     _beam_score("hybrid"), 0.6226, 4),
    ("chip5-beam-hybrid-b", _CHIP5_HYBRID, BEAM_CHIP5,
     _beam_score("hybrid"), 0.6230, 4),
    ("chip5-beam-cortex-delta", _CHIP5_CORTEX, BEAM_CHIP5_PAIRED,
     _paired("cortex", "delta_mean"), 0.0036, 4),
    ("chip5-beam-cortex-se", _CHIP5_CORTEX, BEAM_CHIP5_PAIRED,
     _paired("cortex", "delta_se"), 0.0029, 4),
    ("chip5-beam-cortex-a", _CHIP5_CORTEX, BEAM_CHIP12,
     _beam_score("cortex"), 0.2829, 4),
    ("chip5-beam-cortex-b", _CHIP5_CORTEX, BEAM_CHIP5,
     _beam_score("cortex"), 0.2866, 4),
    ("chip5-beam-context-rows-hybrid", _CHIP5_CTX, BEAM_CHIP5_PAIRED,
     _paired("hybrid", "rows_context_differs"), 30, 0),
    ("chip5-beam-context-rows-cortex", _CHIP5_CTX, BEAM_CHIP5_PAIRED,
     _paired("cortex", "rows_context_differs"), 30, 0),
    # The two chats are the whole mechanism story (constraint label ->
    # recall pin -> different served context), so the sentence naming
    # them is pinned, not only the row count.
    ("chip5-beam-context-chats", "all sit in chats 13 and 15",
     BEAM_CHIP5_PAIRED,
     lambda d: float(sorted(d["chats_with_context_diff"]) == ["13", "15"]),
     1, 0),
    ("chip5-labels-constraint", _CHIP5_LABELS, BEAM_CHIP5_LABELS,
     lambda d: d["distortion_tolerance"]["constraint"], 3, 0),
    ("chip5-labels-quoted", _CHIP5_LABELS, BEAM_CHIP5_LABELS,
     lambda d: d["authority"]["quoted"], 11, 0),
    ("chip5-labels-facts", _CHIP5_LABELS, BEAM_CHIP5_LABELS,
     lambda d: d["facts_current_dumped"], 1099, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))


# The evals README's judge-ladder section (v0.15.0 docs pass) restates the
# effort ladder's truncation finding; a restatement is a claim like any
# other, so it is pinned to the same artifact as the CHANGELOG row above.
CLAIMS.append(Claim(
    id="judge-xhigh-truncated-accepts-evals", doc=EVALS,
    needle="run truncated all 30 true-accept rows at the default budget",
    artifacts=(JUDGE_EFFORT,),
    value=lambda d: sum(1 for r in d["arms"]["qwen-27b-xhigh"]["per_row"]
                        if r["label"] == "accept"
                        and all(v is None for v in r["votes"])),
    stated=30, places=0))



# ── the 2026-09-02 five-arm BEAM run bounds the abstention headline ──────
# README + evals/README stated the cortex arm's 0.950 on BEAM abstention as
# the fact spine's one decisive win; the budget-matched five-arm run scores
# the no-memory arm at 1.000 there. Retired as a headline 2026-09-04; the
# numbers stay, with the floor pinned beside them. The paired column comes
# from the within-run pairing artifact (evals/beam_within_run_pairs.py).
BEAM_CHIP12_PAIRS = _BEAM + "chip12-b16.arms-vs-rag.json"
BEAM_P1B16_VS_CHIP12 = _BEAM + "p1-b16.vs-chip12-b16.paired.json"


def _beam_type(arm: str, qtype: str) -> Callable[[dict], float]:
    return lambda d: d["types"][qtype][arm]


def _pairs(arm: str, key: str) -> Callable[[dict], float]:
    return lambda d: float(d["arms"][arm][key])


_NOMEM_ABSTAIN = _beam_type("nomem", "abstention")
for _cid, _doc, _needle, _art, _val, _stated, _places in [
    ("beam-abstain-nomem-readme-teaser", READ_ME,
     "an arm served no memory at all scores 1.000",
     BEAM_CHIP12, _NOMEM_ABSTAIN, 1.000, 3),
    ("beam-abstain-nomem-readme-body", READ_ME,
     "a no-memory arm scores 1.000", BEAM_CHIP12, _NOMEM_ABSTAIN, 1.000, 3),
    ("beam-abstain-nomem-evals-finding", EVALS,
     "an arm served no memory scores 1.000",
     BEAM_CHIP12, _NOMEM_ABSTAIN, 1.000, 3),
    ("beam-abstain-nomem-changelog", CHANGELOG,
     "scores the no-memory arm at 1.000",
     BEAM_CHIP12, _NOMEM_ABSTAIN, 1.000, 3),
    ("beam-abstain-nomem-evals-table", EVALS,
     "scores 1.000 on\n  abstention", BEAM_CHIP12, _NOMEM_ABSTAIN, 1.000, 3),
    ("beam-nomem-pref-evals", EVALS, "0.469 on preference_following",
     BEAM_CHIP12, _beam_type("nomem", "preference_following"), 0.469, 3),
    ("beam-nomem-instr-evals", EVALS, "0.344 on\n  instruction_following",
     BEAM_CHIP12, _beam_type("nomem", "instruction_following"), 0.344, 3),
    ("beam-abstain-cortex-evals-table", EVALS,
     "(cortex 0.950, rag 0.725, hybrid 0.650, refind 0.575)",
     BEAM_CHIP12, _beam_type("cortex", "abstention"), 0.950, 3),
    ("beam-abstain-rag-evals-table", EVALS,
     "(cortex 0.950, rag 0.725, hybrid 0.650, refind 0.575)",
     BEAM_CHIP12, _beam_type("rag", "abstention"), 0.725, 3),
    ("beam-abstain-hybrid-evals-table", EVALS,
     "(cortex 0.950, rag 0.725, hybrid 0.650, refind 0.575)",
     BEAM_CHIP12, _beam_type("hybrid", "abstention"), 0.650, 3),
    ("beam-abstain-refind-evals-table", EVALS,
     "(cortex 0.950, rag 0.725, hybrid 0.650, refind 0.575)",
     BEAM_CHIP12, _beam_type("refind", "abstention"), 0.575, 3),
    ("beam-refind-contradiction-evals", EVALS,
     "(0.616 vs 0.500) clears the judge-transfer floor",
     BEAM_CHIP12, _beam_type("refind", "contradiction_resolution"), 0.616, 3),
    ("beam-rag-contradiction-evals", EVALS,
     "(0.616 vs 0.500) clears the judge-transfer floor",
     BEAM_CHIP12, _beam_type("rag", "contradiction_resolution"), 0.500, 3),
    ("beam-chip12-rag-score", EVALS, "| rag | 0.6425 |",
     BEAM_CHIP12, _beam_score("rag"), 0.6425, 4),
    ("beam-chip12-refind-score", EVALS, "| refind | 0.6272 |",
     BEAM_CHIP12, _beam_score("refind"), 0.6272, 4),
    ("beam-chip12-hybrid-score", EVALS, "| hybrid | 0.6226 |",
     BEAM_CHIP12, _beam_score("hybrid"), 0.6226, 4),
    ("beam-chip12-cortex-score", EVALS, "| cortex | 0.2829 |",
     BEAM_CHIP12, _beam_score("cortex"), 0.2829, 4),
    ("beam-chip12-nomem-score", EVALS, "| nomem | 0.1812 |",
     BEAM_CHIP12, _beam_score("nomem"), 0.1812, 4),
    ("beam-chip12-refind-delta", EVALS, "−0.0152 ± 0.0362 (p 0.41)",
     BEAM_CHIP12_PAIRS, _pairs("refind", "delta_vs_control"), -0.0152, 4),
    ("beam-chip12-refind-ci", EVALS, "−0.0152 ± 0.0362 (p 0.41)",
     BEAM_CHIP12_PAIRS, _pairs("refind", "ci95_halfwidth"), 0.0362, 4),
    ("beam-chip12-refind-p", EVALS, "−0.0152 ± 0.0362 (p 0.41)",
     BEAM_CHIP12_PAIRS, _pairs("refind", "perm_p"), 0.41, 2),
    ("beam-chip12-hybrid-delta", EVALS, "−0.0199 ± 0.0285 (p 0.18)",
     BEAM_CHIP12_PAIRS, _pairs("hybrid", "delta_vs_control"), -0.0199, 4),
    ("beam-chip12-hybrid-ci", EVALS, "−0.0199 ± 0.0285 (p 0.18)",
     BEAM_CHIP12_PAIRS, _pairs("hybrid", "ci95_halfwidth"), 0.0285, 4),
    ("beam-chip12-hybrid-p", EVALS, "−0.0199 ± 0.0285 (p 0.18)",
     BEAM_CHIP12_PAIRS, _pairs("hybrid", "perm_p"), 0.18, 2),
    ("beam-chip12-cortex-delta", EVALS, "−0.3595 ± 0.0485 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("cortex", "delta_vs_control"), -0.3595, 4),
    ("beam-chip12-cortex-ci", EVALS, "−0.3595 ± 0.0485 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("cortex", "ci95_halfwidth"), 0.0485, 4),
    ("beam-chip12-cortex-p", EVALS, "−0.3595 ± 0.0485 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("cortex", "perm_p"), 0.0001, 4),
    ("beam-chip12-nomem-delta", EVALS, "−0.4612 ± 0.0479 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("nomem", "delta_vs_control"), -0.4612, 4),
    ("beam-chip12-nomem-ci", EVALS, "−0.4612 ± 0.0479 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("nomem", "ci95_halfwidth"), 0.0479, 4),
    ("beam-chip12-nomem-p", EVALS, "−0.4612 ± 0.0479 (p < 0.0001)",
     BEAM_CHIP12_PAIRS, _pairs("nomem", "perm_p"), 0.0001, 4),
    ("beam-chip12-nomem-full-marks", EVALS, "62 of 400 rows score full marks",
     BEAM_CHIP12_PAIRS, _pairs("nomem", "full_marks_rows"), 62, 0),
    ("beam-chip12-refind-chars", EVALS,
     "(41,757 vs 22,158 mean characters",
     BEAM_CHIP12_PAIRS, _pairs("refind", "context_chars_mean"), 41757, 0),
    ("beam-chip12-rag-chars", EVALS,
     "(41,757 vs 22,158 mean characters",
     BEAM_CHIP12_PAIRS, lambda d: float(d["control_context_chars_mean"]),
     22158, 0),
    ("beam-chip12-refind-temporal", EVALS,
     "temporal_reasoning (0.669 vs 0.644)",
     BEAM_CHIP12, _beam_type("refind", "temporal_reasoning"), 0.669, 3),
    ("beam-chip12-rag-temporal", EVALS,
     "temporal_reasoning (0.669 vs 0.644)",
     BEAM_CHIP12, _beam_type("rag", "temporal_reasoning"), 0.644, 3),
    ("beam-chip12-refind-ordering", EVALS,
     "event_ordering (0.496 vs 0.472)",
     BEAM_CHIP12, _beam_type("refind", "event_ordering"), 0.496, 3),
    ("beam-chip12-rag-ordering", EVALS,
     "event_ordering (0.496 vs 0.472)",
     BEAM_CHIP12, _beam_type("rag", "event_ordering"), 0.472, 3),
    ("beam-abstain-rag-readme-fivearm", READ_ME, "(rag 0.725 there;",
     BEAM_CHIP12, _beam_type("rag", "abstention"), 0.725, 3),
    ("beam-chip12-p1b16-rag-control", EVALS,
     "exactly 0.0000 over all 400 rows",
     BEAM_P1B16_VS_CHIP12, _paired("rag", "delta_mean"), 0.0, 4),
    ("beam-chip12-p1b16-hybrid-control", EVALS,
     "exactly 0.0000 over all 400 rows",
     BEAM_P1B16_VS_CHIP12, _paired("hybrid", "delta_mean"), 0.0, 4),
    ("beam-chip12-hybrid-chars", EVALS, "| hybrid | 0.6226 | −0.0199 ± 0.0285 (p 0.18) | 24,398 |",
     BEAM_CHIP12_PAIRS, _pairs("hybrid", "context_chars_mean"), 24398, 0),
    ("beam-chip12-cortex-chars", EVALS, "| cortex | 0.2829 | −0.3595 ± 0.0485 (p < 0.0001) | 2,207 |",
     BEAM_CHIP12_PAIRS, _pairs("cortex", "context_chars_mean"), 2207, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))


def test_beam_range_quotes_match_the_committed_verdict():
    """The evals README quotes three sweep ranges; they must be the
    verdict file's, not a recollection of it."""
    doc = _read_doc(EVALS)
    for artifact, key, quoted in _BEAM_VERDICT_QUOTES:
        verdict = _load_artifact(artifact)
        assert quoted in verdict["structural_findings"][key], (
            f"{artifact}:{key} no longer says {quoted!r}")
        assert quoted.replace("->", "→") in doc, (
            f"{EVALS} no longer quotes the {key} range {quoted!r}")


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
    text = _read_doc(claim.doc)
    assert claim.needle in text, (
        f"{claim.id}: {claim.doc} no longer contains the guarded text\n  "
        f"{claim.needle!r}\nIf the number changed, update this table; if the "
        f"claim was dropped, delete its row.")


# ── the review-queue judge gates (2026-09-02) ─────────────────────────────
# The CHANGELOG publishes the two-vote merge gates and the single-vote
# accept precision they replace; all three come from the scrubbed panel
# artifact (labels + votes), never from the private evidence pack.
PANEL_0902 = "evals/results/queue-judge-panel-20260902.json"
for _cid, _needle, _val, _stated, _places in [
    ("queue-judge-two-vote-reject-n", "two-vote rejects 8/8",
     lambda d: d["merge_gate_table"]["R2_two_vote_reject_mean_ge0.7"]["n"], 8, 0),
    ("queue-judge-two-vote-reject-bad", "two-vote rejects 8/8",
     lambda d: len(d["merge_gate_table"]["R2_two_vote_reject_mean_ge0.7"]["bad"]), 0, 0),
    ("queue-judge-two-vote-accept-n", "non-low-differential accepts 6/6",
     lambda d: d["merge_gate_table"]["A4_two_vote_accept_mean_ge0.6_not_lowdiff"]["n"], 6, 0),
    ("queue-judge-two-vote-accept-bad", "non-low-differential accepts 6/6",
     lambda d: len(d["merge_gate_table"]["A4_two_vote_accept_mean_ge0.6_not_lowdiff"]["bad"]), 0, 0),
    ("queue-judge-single-accept-precision", "same rows 0.74",
     lambda d: d["single_vote_accept_precision"]["shadow_opus"]["precision"], 0.74, 2),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(PANEL_0902,),
        value=_val, stated=_stated, places=_places))

LADDER_0902 = "evals/results/queue-judge-ladder-20260902.json"


def _ladder(queue, metric, field):
    return lambda d: d["arms"]["opus-r2"]["queues"][queue][metric][field]


for _cid, _needle, _val, _stated in [
    ("queue-ladder-link-accept-n", "link auto-accept 4/4", _ladder("links", "auto_accept", "n"), 4),
    ("queue-ladder-link-accept-bad", "link auto-accept 4/4", _ladder("links", "auto_accept", "bad"), 0),
    ("queue-ladder-link-reject-n", "auto-reject 5/5", _ladder("links", "auto_reject", "n"), 5),
    ("queue-ladder-link-reject-bad", "auto-reject 5/5", _ladder("links", "auto_reject", "bad"), 0),
    ("queue-ladder-junk-delete-n", "evidence bar 6/6", _ladder("junk", "auto_delete_under_bar", "n"), 6),
    ("queue-ladder-junk-delete-bad", "evidence bar 6/6", _ladder("junk", "auto_delete_under_bar", "bad"), 0),
    ("queue-ladder-junk-keep-n", "auto-keep 7/7", _ladder("junk", "auto_keep", "n"), 7),
    ("queue-ladder-curation-distinct-n", "auto-distinct 21/21", _ladder("curation", "auto_distinct", "n"), 21),
    ("queue-ladder-curation-distinct-bad", "auto-distinct 21/21", _ladder("curation", "auto_distinct", "bad"), 0),
    ("queue-ladder-candidate-propose-n", "auto-propose 7/8", _ladder("candidates", "auto_propose", "n"), 8),
    ("queue-ladder-candidate-propose-bad", "auto-propose 7/8", _ladder("candidates", "auto_propose", "bad"), 1),
    ("queue-ladder-candidate-dismiss-n", "auto-dismiss 15/16", _ladder("candidates", "auto_dismiss", "n"), 16),
    ("queue-ladder-candidate-dismiss-bad", "auto-dismiss 15/16", _ladder("candidates", "auto_dismiss", "bad"), 1),
    ("queue-ladder-merge-two-vote-accept-n", "accept 4/4", _ladder("merges", "two_vote_accept_not_lowdiff", "n"), 4),
    ("queue-ladder-merge-two-vote-accept-bad", "accept 4/4", _ladder("merges", "two_vote_accept_not_lowdiff", "bad"), 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(LADDER_0902,),
        value=_val, stated=_stated, places=0))
# The 2026-09-03 full-length-evidence rerun (same 63 rows, snippets
# recovered at full length, judge cap 3000) — a NEGATIVE result that keeps
# judge_snippet_max_chars at 240; every number the CHANGELOG cites for it,
# plus the 2026-09-02 comparators, is read from the artifacts.
LADDER_0903 = "evals/results/queue-judge-ladder-20260903-fulllen.json"


def _ladder_0903(metric, field):
    return lambda d: d["arms"]["opus-r2-fulllen"]["queues"]["merges"][metric][field]


def _disagreeing_rows(arm):
    return lambda d: sum(
        1 for r in d["arms"][arm]["queues"]["merges"]["per_row"]
        if len({v["verdict"] for v in r["votes"] if v}) > 1)


for _cid, _needle, _art, _val, _stated, _places in [
    ("fulllen-accept-precision", "fell to 0.70", LADDER_0903,
     _ladder_0903("accept_precision", "precision"), 0.70, 2),
    ("fulllen-accept-precision-clipped", "from 0.85 on clipped", LADDER_0902,
     _ladder("merges", "accept_precision", "precision"), 0.85, 2),
    ("fulllen-two-vote-accept-n", "passed 6/7", LADDER_0903,
     _ladder_0903("two_vote_accept_not_lowdiff", "n"), 7, 0),
    ("fulllen-two-vote-accept-bad", "passed 6/7", LADDER_0903,
     _ladder_0903("two_vote_accept_not_lowdiff", "bad"), 1, 0),
    ("fulllen-two-vote-reject-n", "7/7 two-vote", LADDER_0903,
     _ladder_0903("two_vote_reject", "n"), 7, 0),
    ("fulllen-two-vote-reject-bad", "7/7 two-vote", LADDER_0903,
     _ladder_0903("two_vote_reject", "bad"), 0, 0),
    ("fulllen-disagreement", "6/63 rows", LADDER_0903,
     _disagreeing_rows("opus-r2-fulllen"), 6, 0),
    ("fulllen-disagreement-clipped", "2/63) — the delta", LADDER_0902,
     _disagreeing_rows("opus-r2"), 2, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(_art,),
        value=_val, stated=_stated, places=_places))

CLAIMS.append(Claim(
    id="queue-ladder-curation-keep-precision", doc=CHANGELOG,
    needle="precision was 0.5625", artifacts=(LADDER_0902,),
    value=_ladder("curation", "duplicate_keep_precision", "precision"),
    stated=0.5625, places=4))


# ── docs-currency pass (2026-09-04, v0.15.0): the same review-queue judge
# and v35-label numbers, re-stated in evals/README.md's own prose and in
# docs/guide/security-posture.md's mechanism table. Same artifacts, new
# needles — the CHANGELOG rows above guard the historical entry, these
# guard the pages a reader actually lands on.
SECURITY = "docs/guide/security-posture.md"

for _cid, _needle, _val, _stated, _places in [
    ("evals-queue-ladder-merge-two-vote-reject-n",
     "merge two-vote reject\n8/8, two-vote non-low-differential accept 4/4;",
     _ladder("merges", "two_vote_reject", "n"), 8, 0),
    ("evals-queue-ladder-merge-two-vote-reject-bad",
     "merge two-vote reject\n8/8, two-vote non-low-differential accept 4/4;",
     _ladder("merges", "two_vote_reject", "bad"), 0, 0),
    ("evals-queue-ladder-merge-two-vote-accept-n",
     "merge two-vote reject\n8/8, two-vote non-low-differential accept 4/4;",
     _ladder("merges", "two_vote_accept_not_lowdiff", "n"), 4, 0),
    ("evals-queue-ladder-merge-two-vote-accept-bad",
     "merge two-vote reject\n8/8, two-vote non-low-differential accept 4/4;",
     _ladder("merges", "two_vote_accept_not_lowdiff", "bad"), 0, 0),
    ("evals-queue-ladder-link-accept-n", "link auto-accept 4/4,\nauto-reject 5/5;",
     _ladder("links", "auto_accept", "n"), 4, 0),
    ("evals-queue-ladder-link-accept-bad", "link auto-accept 4/4,\nauto-reject 5/5;",
     _ladder("links", "auto_accept", "bad"), 0, 0),
    ("evals-queue-ladder-link-reject-n", "link auto-accept 4/4,\nauto-reject 5/5;",
     _ladder("links", "auto_reject", "n"), 5, 0),
    ("evals-queue-ladder-link-reject-bad", "link auto-accept 4/4,\nauto-reject 5/5;",
     _ladder("links", "auto_reject", "bad"), 0, 0),
    ("evals-queue-ladder-junk-delete-n",
     "auto-delete-under-the-evidence-bar 6/6, auto-keep\n7/7;",
     _ladder("junk", "auto_delete_under_bar", "n"), 6, 0),
    ("evals-queue-ladder-junk-delete-bad",
     "auto-delete-under-the-evidence-bar 6/6, auto-keep\n7/7;",
     _ladder("junk", "auto_delete_under_bar", "bad"), 0, 0),
    ("evals-queue-ladder-junk-keep-n",
     "auto-delete-under-the-evidence-bar 6/6, auto-keep\n7/7;",
     _ladder("junk", "auto_keep", "n"), 7, 0),
    ("evals-queue-ladder-candidate-propose-n",
     "candidate auto-propose 7/8, auto-dismiss 15/16;",
     _ladder("candidates", "auto_propose", "n"), 8, 0),
    ("evals-queue-ladder-candidate-propose-bad",
     "candidate auto-propose 7/8, auto-dismiss 15/16;",
     _ladder("candidates", "auto_propose", "bad"), 1, 0),
    ("evals-queue-ladder-candidate-dismiss-n",
     "candidate auto-propose 7/8, auto-dismiss 15/16;",
     _ladder("candidates", "auto_dismiss", "n"), 16, 0),
    ("evals-queue-ladder-candidate-dismiss-bad",
     "candidate auto-propose 7/8, auto-dismiss 15/16;",
     _ladder("candidates", "auto_dismiss", "bad"), 1, 0),
    ("evals-queue-ladder-curation-distinct-n", "curation\nauto-distinct 21/21",
     _ladder("curation", "auto_distinct", "n"), 21, 0),
    ("evals-queue-ladder-curation-distinct-bad", "curation\nauto-distinct 21/21",
     _ladder("curation", "auto_distinct", "bad"), 0, 0),
    ("evals-queue-ladder-curation-keep-precision",
     "keep-side precision is only 0.5625,",
     _ladder("curation", "duplicate_keep_precision", "precision"), 0.5625, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(LADDER_0902,),
        value=_val, stated=_stated, places=_places))

CLAIMS.append(Claim(
    id="evals-fulllen-accept-precision", doc=EVALS,
    needle="accept precision\nfell to 0.70 (from 0.85 clipped)",
    artifacts=(LADDER_0903,),
    value=_ladder_0903("accept_precision", "precision"),
    stated=0.70, places=2))
CLAIMS.append(Claim(
    id="evals-fulllen-accept-precision-clipped", doc=EVALS,
    needle="accept precision\nfell to 0.70 (from 0.85 clipped)",
    artifacts=(LADDER_0902,),
    value=_ladder("merges", "accept_precision", "precision"),
    stated=0.85, places=2))

# The v35 label-heuristic companion paragraph on the same page (86/73/0.85
# on the live bank, 8/8 on the chip-5 BEAM chat-text bank) — LABEL_AUDIT_0903
# and LABEL_AUDIT_0903_BEAM, _LA_SHIPPED already defined above.
_EVALS_LA_LIVE = "shipped rule fires on 86 facts, of which 73 read as a genuine rule (0.85\nprecision), on 1 of 869 entries;"
for _cid, _val, _stated, _places in [
    ("evals-label-fix-live-hits", lambda d: _LA_SHIPPED(d)["fact_hits"], 86, 0),
    ("evals-label-fix-live-genuine", lambda d: _LA_SHIPPED(d)["judged_genuine"], 73, 0),
    ("evals-label-fix-live-precision", lambda d: _LA_SHIPPED(d)["precision"], 0.85, 2),
    ("evals-label-fix-live-entries", lambda d: _LA_SHIPPED(d)["entry_hits"], 1, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_EVALS_LA_LIVE, artifacts=(LABEL_AUDIT_0903,),
        value=_val, stated=_stated, places=_places))

CLAIMS.append(Claim(
    id="evals-label-fix-live-sample-entries", doc=EVALS,
    needle="On the live bank (2026-09-03, 869 entries / 5,435 current facts)",
    artifacts=(LABEL_AUDIT_0903,),
    value=lambda d: d["sample"]["current_entries"], stated=869, places=0))
CLAIMS.append(Claim(
    id="evals-label-fix-live-sample-facts", doc=EVALS,
    needle="On the live bank (2026-09-03, 869 entries / 5,435 current facts)",
    artifacts=(LABEL_AUDIT_0903,),
    value=lambda d: d["sample"]["current_facts"], stated=5435, places=0))

_EVALS_LA_BEAM = "1,099\ncurrent facts) it fires on 8 values, all 8 genuine."
CLAIMS.append(Claim(
    id="evals-label-fix-beam-hits", doc=EVALS, needle=_EVALS_LA_BEAM,
    artifacts=(LABEL_AUDIT_0903_BEAM,),
    value=lambda d: _LA_SHIPPED(d)["fact_hits"], stated=8, places=0))
CLAIMS.append(Claim(
    id="evals-label-fix-beam-genuine", doc=EVALS, needle=_EVALS_LA_BEAM,
    artifacts=(LABEL_AUDIT_0903_BEAM,),
    value=lambda d: _LA_SHIPPED(d)["judged_genuine"], stated=8, places=0))
CLAIMS.append(Claim(
    id="evals-label-fix-beam-facts", doc=EVALS, needle=_EVALS_LA_BEAM,
    artifacts=(LABEL_AUDIT_0903_BEAM,),
    value=lambda d: d["sample"]["current_facts"], stated=1099, places=0))

# The chip-5 paired regression-gate paragraph (evals/README.md, mirrors the
# CHANGELOG entry) — LADDER_CHIP5 / BEAM_CHIP5_PAIRED, _rung_identical /
# _paired already defined above.
CLAIMS.append(Claim(
    id="evals-chip5-ladder-floor-identical", doc=EVALS,
    needle="verdict-identical on both rungs, as predicted",
    artifacts=(LADDER_CHIP5,), value=_rung_identical("floor"),
    stated=1, places=0))
CLAIMS.append(Claim(
    id="evals-chip5-ladder-qwen-identical", doc=EVALS,
    needle="verdict-identical on both rungs, as predicted",
    artifacts=(LADDER_CHIP5,), value=_rung_identical("qwen-27b"),
    stated=1, places=0))

_EVALS_CHIP5_QUESTIONS = ("run at the matched\n16/16 budget against the "
                          "2026-09-02 pre-#245 baseline on all 400\n"
                          "questions:")
CLAIMS.append(Claim(
    id="evals-chip5-beam-paired-rows", doc=EVALS,
    needle=_EVALS_CHIP5_QUESTIONS, artifacts=(BEAM_CHIP5_PAIRED,),
    value=lambda d: d["paired_rows"], stated=400, places=0))

_EVALS_CHIP5_RAG = "the identical-input `rag` control moved 0.0000, hybrid"
CLAIMS.append(Claim(
    id="evals-chip5-beam-rag-delta", doc=EVALS, needle=_EVALS_CHIP5_RAG,
    artifacts=(BEAM_CHIP5_PAIRED,), value=_paired("rag", "delta_mean"),
    stated=0.0, places=4))

_EVALS_CHIP5_DELTAS = "\n+0.0004±0.0014, cortex +0.0036±0.0029 — every delta"
CLAIMS.append(Claim(
    id="evals-chip5-beam-hybrid-delta", doc=EVALS, needle=_EVALS_CHIP5_DELTAS,
    artifacts=(BEAM_CHIP5_PAIRED,), value=_paired("hybrid", "delta_mean"),
    stated=0.0004, places=4))
CLAIMS.append(Claim(
    id="evals-chip5-beam-hybrid-se", doc=EVALS, needle=_EVALS_CHIP5_DELTAS,
    artifacts=(BEAM_CHIP5_PAIRED,), value=_paired("hybrid", "delta_se"),
    stated=0.0014, places=4))
CLAIMS.append(Claim(
    id="evals-chip5-beam-cortex-delta", doc=EVALS, needle=_EVALS_CHIP5_DELTAS,
    artifacts=(BEAM_CHIP5_PAIRED,), value=_paired("cortex", "delta_mean"),
    stated=0.0036, places=4))
CLAIMS.append(Claim(
    id="evals-chip5-beam-cortex-se", doc=EVALS, needle=_EVALS_CHIP5_DELTAS,
    artifacts=(BEAM_CHIP5_PAIRED,), value=_paired("cortex", "delta_se"),
    stated=0.0029, places=4))

_EVALS_CHIP5_CTX = "The 30 rows whose served context differed all sit"
CLAIMS.append(Claim(
    id="evals-chip5-beam-context-rows-hybrid", doc=EVALS,
    needle=_EVALS_CHIP5_CTX, artifacts=(BEAM_CHIP5_PAIRED,),
    value=_paired("hybrid", "rows_context_differs"), stated=30, places=0))
CLAIMS.append(Claim(
    id="evals-chip5-beam-context-rows-cortex", doc=EVALS,
    needle=_EVALS_CHIP5_CTX, artifacts=(BEAM_CHIP5_PAIRED,),
    value=_paired("cortex", "rows_context_differs"), stated=30, places=0))

_EVALS_CHIP5_LABELS = "(3 of\n1099 facts; `quoted` fired on 11)"
CLAIMS.append(Claim(
    id="evals-chip5-labels-constraint", doc=EVALS, needle=_EVALS_CHIP5_LABELS,
    artifacts=(BEAM_CHIP5_LABELS,),
    value=lambda d: d["distortion_tolerance"]["constraint"],
    stated=3, places=0))
CLAIMS.append(Claim(
    id="evals-chip5-labels-quoted", doc=EVALS, needle=_EVALS_CHIP5_LABELS,
    artifacts=(BEAM_CHIP5_LABELS,),
    value=lambda d: d["authority"]["quoted"], stated=11, places=0))

# docs/guide/security-posture.md's merge-queue row: the ONE distinct-model
# (shadow Opus + Fable) two-vote accept pairing, from the panel's own
# merge_gate_table — 6/6, the number the CHANGELOG's "Evidence honesty"
# note (2026-09-02 entry) says is the fair one to quote unqualified.
CLAIMS.append(Claim(
    id="security-merge-queue-two-vote-accept-n", doc=SECURITY,
    needle="measured 6/6 on one distinct-model pairing of the 2026-09-02 panel",
    artifacts=(PANEL_0902,),
    value=lambda d: d["merge_gate_table"][
        "A4_two_vote_accept_mean_ge0.6_not_lowdiff"]["n"],
    stated=6, places=0))
CLAIMS.append(Claim(
    id="security-merge-queue-two-vote-accept-bad", doc=SECURITY,
    needle="measured 6/6 on one distinct-model pairing of the 2026-09-02 panel",
    artifacts=(PANEL_0902,),
    value=lambda d: len(d["merge_gate_table"][
        "A4_two_vote_accept_mean_ge0.6_not_lowdiff"]["bad"]),
    stated=0, places=0))

# ── offline routing analysis (2026-09-04, evals/README.md) ───────────────
# Lever-4 question: is a query-shape router worth building, or is the gain
# already in the commit-gated cascade? Answered offline from three
# already-judged runs — no new answer or judge calls — so every number in
# that section is a re-aggregation of ONE artifact, pinned here.
ROUTER = RESULTS + "router-offline-20260904.json"


def _r_arm(ds: str, arm: str, field: str):
    return lambda d: d["datasets"][ds]["arms"][arm][field]


def _r_pol(ds: str, key: str, field: str):
    return lambda d: d["datasets"][ds]["policies"][key][field]


def _r_verdict(ds: str, field: str):
    return lambda d: d["verdict"][ds][field]


# (needle, dataset, kind, key, stated score / cost / ratio) — one table row
# each, pinned on all three published columns so a partial edit fails.
_ROUTER_ROWS = [
    # LongMemEval, 500 questions
    ("| cortex only | 0.416 | 158 | 2.629 |",
     "LME-500", "arms", "cortex", 0.416, 158, 2.629),
    ("| hybrid (facts + top-k) | 0.664 | 842 | 0.789 |",
     "LME-500", "arms", "hybrid", 0.664, 842, 0.789),
    ("| **rag — best single arm** | **0.688** | 1210 | 0.569 |",
     "LME-500", "arms", "rag", 0.688, 1210, 0.569),
    ("| cascade (shipped policy) | 0.690 | 883 | 0.782 |",
     "LME-500", "arms", "cascade", 0.690, 883, 0.782),
    ("| oracle by type (arms + cascade) | 0.712 | 893 | 0.797 |",
     "LME-500", "policies", "oracle_by_type[with_cascade]",
     0.712, 893, 0.797),
    ("| oracle per question (ceiling) | 0.778 | 419 | 1.857 |",
     "LME-500", "policies", "oracle_per_question[base]", 0.778, 419, 1.857),
    ("| router via predicted type | 0.686 | 1002 | 0.685 |",
     "LME-500", "policies", "router_via_type[base|logreg]",
     0.686, 1002, 0.685),
    ("| two-stage: cascade, then router | 0.690 | 883 | 0.782 |",
     "LME-500", "policies", "two_stage[tree_d3|acc]", 0.690, 883, 0.782),
    ("| two-stage, token-greedy labels | 0.656 | 667 | 0.983 |",
     "LME-500", "policies", "two_stage[tree_d3|cheap]", 0.656, 667, 0.983),
    # BEAM 100K, 400 questions (cost in context CHARACTERS)
    ("| cortex only | 0.283 | 2 207 | 0.513 |",
     "BEAM-400", "arms", "cortex", 0.283, 2207, 0.513),
    ("| cascade | 0.552 | 14 294 | 0.154 |",
     "BEAM-400", "arms", "cascade", 0.552, 14294, 0.154),
    ("| hybrid | 0.623 | 24 398 | 0.102 |",
     "BEAM-400", "arms", "hybrid", 0.623, 24398, 0.102),
    ("| refind | 0.627 | 41 757 | 0.060 |",
     "BEAM-400", "arms", "refind", 0.627, 41757, 0.060),
    ("| **rag — best single arm** | **0.642** | 22 158 | 0.116 |",
     "BEAM-400", "arms", "rag", 0.642, 22158, 0.116),
    ("| oracle by type (arms + cascade) | 0.683 | 22 861 | 0.120 |",
     "BEAM-400", "policies", "oracle_by_type[with_cascade]",
     0.683, 22861, 0.120),
    ("| oracle by type (+ the no-memory arm) | 0.688 | 22 635 | 0.122 |",
     "BEAM-400", "policies", "oracle_by_type[with_nomem]",
     0.688, 22635, 0.122),
    ("| oracle per question (ceiling) | 0.789 | 17 672 | 0.179 |",
     "BEAM-400", "policies", "oracle_per_question[with_nomem]",
     0.789, 17672, 0.179),
    ("| router via predicted type | 0.620 | 27 780 | 0.089 |",
     "BEAM-400", "policies", "router_via_type[base|logreg]",
     0.620, 27780, 0.089),
    ("| two-stage: cascade, then router | 0.554 | 14 364 | 0.154 |",
     "BEAM-400", "policies", "two_stage[tree_d3|acc]", 0.554, 14364, 0.154),
    # LongMemEval knowledge-update, 78 questions (ceiling-v38)
    ("| cortex only | 0.667 | 97 | 6.894 |",
     "LME-KU78", "arms", "cortex", 0.667, 97, 6.894),
    ("| hybrid | 0.846 | 731 | 1.157 |",
     "LME-KU78", "arms", "hybrid", 0.846, 731, 1.157),
    ("| cascade | 0.846 | 389 | 2.173 |",
     "LME-KU78", "arms", "cascade", 0.846, 389, 2.173),
    ("| **rag — best single arm** | **0.859** | 1184 | 0.725 |",
     "LME-KU78", "arms", "rag", 0.859, 1184, 0.725),
    ("| oracle per question (ceiling) | 0.962 | 318 | 3.021 |",
     "LME-KU78", "policies", "oracle_per_question[base]", 0.962, 318, 3.021),
    ("| two-stage: cascade, then router | 0.846 | 382 | 2.212 |",
     "LME-KU78", "policies", "two_stage[tree_d3|acc]", 0.846, 382, 2.212),
]

for _needle, _ds, _kind, _key, _score, _cost, _ratio in _ROUTER_ROWS:
    _get = _r_arm if _kind == "arms" else _r_pol
    _slug = f"router-{_ds.lower()}-{_kind}-{_key}".replace(" ", "")
    CLAIMS.append(Claim(
        id=f"{_slug}-score", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_get(_ds, _key, "score"), stated=_score, places=3))
    CLAIMS.append(Claim(
        id=f"{_slug}-cost", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_get(_ds, _key, "cost"), stated=float(_cost), places=0))
    CLAIMS.append(Claim(
        id=f"{_slug}-ratio", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_get(_ds, _key, "score_per_1k_tokens"), stated=_ratio,
        places=3))

# The no-memory arm serves nothing, so its ratio column reads "n/a".
CLAIMS.append(Claim(
    id="router-beam-nomem-score", doc=EVALS,
    needle="| no memory | 0.181 | 0 | n/a |", artifacts=(ROUTER,),
    value=_r_arm("BEAM-400", "nomem", "score"), stated=0.181, places=3))
CLAIMS.append(Claim(
    id="router-beam-nomem-cost", doc=EVALS,
    needle="| no memory | 0.181 | 0 | n/a |", artifacts=(ROUTER,),
    value=_r_arm("BEAM-400", "nomem", "cost"), stated=0.0, places=0))

# The sanity gate the whole section rests on: the script must reproduce
# each source run's own published per-arm table before anything else in it
# means anything.
CLAIMS.append(Claim(
    id="router-sanity-lme500-exact", doc=EVALS,
    needle="from the rows: LME-500 reproduces its summary exactly (max score delta",
    artifacts=(ROUTER,),
    value=lambda d: d["datasets"]["LME-500"]["sanity_vs_summary"][
        "max_score_delta"], stated=0.0, places=4))

# The verdict paragraph and the deltas quoted around the tables.
_ROUTER_SCALARS = [
    ("router-lme-oracle-gain",
     "The oracle-by-type bound is **+0.024** over the best single arm, at 316",
     _r_verdict("LME-500", "oracle_by_type_gain"), 0.024, 3),
    ("router-lme-oracle-cost-saved",
     "The oracle-by-type bound is **+0.024** over the best single arm, at 316",
     lambda d: abs(d["verdict"]["LME-500"]["oracle_by_type_cost_delta"]),
     316.0, 0),
    ("router-lme-realizable-gain",
     "fewer tokens. The best realizable router is **+0.002**, and it is the",
     _r_verdict("LME-500", "realizable_gain"), 0.002, 3),
    ("router-lme-two-stage-commits",
     "the 193 questions where cortex commits and rag on the other 307, landing on",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "two_stage[logreg|acc]"]["arm_share"]["cortex(commit)"], 193, 0),
    ("router-lme-two-stage-rag",
     "the 193 questions where cortex commits and rag on the other 307, landing on",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "two_stage[logreg|acc]"]["arm_share"]["rag"], 307, 0),
    ("router-lme-single-stage-best-score",
     "single-stage one ties the best single arm at 0.688 on 1205 tokens, and the",
     _r_pol("LME-500", "router[base|tree_d3|acc]", "score"), 0.688, 3),
    ("router-lme-single-stage-best-cost",
     "single-stage one ties the best single arm at 0.688 on 1205 tokens, and the",
     _r_pol("LME-500", "router[base|tree_d3|acc]", "cost"), 1205, 0),
    ("router-lme-with-cascade-score",
     "two variants free to pick the cascade as well tie at 0.678, on 1005 and",
     _r_pol("LME-500", "router[with_cascade|tree_d3|acc]", "score"),
     0.678, 3),
    ("router-lme-with-cascade-cost-tree",
     "two variants free to pick the cascade as well tie at 0.678, on 1005 and",
     _r_pol("LME-500", "router[with_cascade|tree_d3|acc]", "cost"), 1005, 0),
    ("router-lme-with-cascade-cost-logreg",
     "1009 tokens, agreeing with the oracle-by-type choice on 0.226 of questions.",
     _r_pol("LME-500", "router[with_cascade|logreg|acc]", "cost"), 1009, 0),
    ("router-lme-router-agreement",
     "1009 tokens, agreeing with the oracle-by-type choice on 0.226 of questions.",
     _r_pol("LME-500", "router[with_cascade|logreg|acc]",
            "agree_with_oracle_by_type"), 0.226, 3),
    ("router-beam-oracle-gain",
     "Here the oracle-by-type bound is larger — **+0.046** — but it costs 477",
     _r_verdict("BEAM-400", "oracle_by_type_gain"), 0.046, 3),
    ("router-beam-oracle-cost-added",
     "Here the oracle-by-type bound is larger — **+0.046** — but it costs 477",
     _r_verdict("BEAM-400", "oracle_by_type_cost_delta"), 477.0, 0),
    ("router-beam-realizable-gain",
     "realizable router recovers **+0.008** of that, also at more cost. The",
     _r_verdict("BEAM-400", "realizable_gain"), 0.008, 3),
    ("router-beam-cascade-cost",
     "alone scores 0.283 there, so committing to it costs 0.09.",
     lambda d: (d["verdict"]["BEAM-400"]["best_single_score"]
                - d["verdict"]["BEAM-400"]["cascade_score"]), 0.09, 2),
    ("router-lme-typepred",
     "LME-500 and 0.652 on BEAM-400 by 5-fold CV, against majority baselines of",
     lambda d: d["datasets"]["LME-500"]["type_predictability"]["logreg"][
         "cv_accuracy"], 0.654, 3),
    ("router-beam-typepred",
     "LME-500 and 0.652 on BEAM-400 by 5-fold CV, against majority baselines of",
     lambda d: d["datasets"]["BEAM-400"]["type_predictability"]["logreg"][
         "cv_accuracy"], 0.652, 3),
    ("router-lme-typepred-majority",
     "0.266 and 0.100. The gap is not in the classifier. It is that",
     lambda d: d["datasets"]["LME-500"]["type_predictability"]["logreg"][
         "majority_baseline"], 0.266, 3),
    ("router-beam-typepred-majority",
     "0.266 and 0.100. The gap is not in the classifier. It is that",
     lambda d: d["datasets"]["BEAM-400"]["type_predictability"]["logreg"][
         "majority_baseline"], 0.100, 3),
    ("router-lme-collapse-rag",
     "models collapse: 493/500 rag under accuracy-first tie-breaking on",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "router[base|tree_d3|acc]"]["arm_share"]["rag"], 493, 0),
    ("router-lme-collapse-cortex",
     "LME-500, or 475/500 cortex under cost-first, which trades 0.25 accuracy",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "router[base|tree_d3|cheap]"]["arm_share"]["cortex"], 475, 0),
    ("router-robustness-agree",
     "choice agrees on **two**:",
     lambda d: d["robustness"]["type_pairs"]["n_agree"], 2, 0),
    ("router-robustness-pairs",
     "choice agrees on **two**:",
     lambda d: d["robustness"]["type_pairs"]["n_pairs"], 4, 0),
    ("router-verdict-lme-realizable",
     "(LongMemEval, at 327 fewer tokens) and +0.008 (BEAM, at 671 MORE chars).",
     _r_verdict("LME-500", "realizable_gain"), 0.002, 3),
    ("router-verdict-lme-tokens-saved",
     "(LongMemEval, at 327 fewer tokens) and +0.008 (BEAM, at 671 MORE chars).",
     lambda d: abs(d["verdict"]["LME-500"]["realizable_cost_delta"]),
     327.0, 0),
    ("router-verdict-beam-chars-added",
     "(LongMemEval, at 327 fewer tokens) and +0.008 (BEAM, at 671 MORE chars).",
     _r_verdict("BEAM-400", "realizable_cost_delta"), 671.0, 0),
    ("router-ceiling-gain-lme",
     "the best single arm — say the channels genuinely disagree and a *perfect*",
     _r_verdict("LME-500", "ceiling_gain"), 0.090, 3),
    ("router-ceiling-gain-beam",
     "the best single arm — say the channels genuinely disagree and a *perfect*",
     _r_verdict("BEAM-400", "ceiling_gain"), 0.147, 3),
    ("router-cascade-unbeaten",
     "at 0.690/883 tokens, which the best router matches exactly and no router",
     _r_arm("LME-500", "cascade", "score"), 0.690, 3),
]

for _cid, _needle, _value, _stated, _places in _ROUTER_SCALARS:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_value, stated=float(_stated), places=_places))

# The KU-78 per-question ceiling is quoted beside the rag-union figure on
# the SAME rows, so the union is pinned against the rows themselves — the
# guide's 0.949 is a different run and a two-channel union, and the doc
# says so.
CLAIMS.append(Claim(
    id="router-ku78-two-channel-union", doc=EVALS,
    needle="against 0.936 for the rag∪cortex union on the same rows.",
    artifacts=(V38_ROWS_JSONL,),
    value=lambda rows: (sum(1 for r in rows
                            if r["rag_correct"] or r["cortex_correct"])
                        / len(rows)),
    stated=0.936, places=3))

# The "best cross-validated router" table row is a MAXIMUM over
# configurations, and the fold-local tie-break fix (2026-09-04, review of
# #260) moved which configuration attains it on LongMemEval without moving
# the number. So the row pins against the verdict block, which names the
# winner, instead of a policy key that can stop being the winner.
def _r_best_ratio(ds: str, chars: bool):
    def value(d):
        v = d["verdict"][ds]
        tokens = v["best_realizable_cost"] / (d["chars_per_token"]
                                              if chars else 1.0)
        return v["best_realizable_score"] / (tokens / 1000.0)
    return value


for _ds, _needle, _chars, _score, _cost, _ratio in (
    ("LME-500", "| best cross-validated router | 0.690 | 883 | 0.782 |",
     False, 0.690, 883, 0.782),
    ("BEAM-400", "| best cross-validated router | 0.651 | 22 829 | 0.114 |",
     True, 0.651, 22829, 0.114),
):
    _slug = f"router-{_ds.lower()}-best-realizable"
    CLAIMS.append(Claim(
        id=f"{_slug}-score", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_r_verdict(_ds, "best_realizable_score"), stated=_score,
        places=3))
    CLAIMS.append(Claim(
        id=f"{_slug}-cost", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_r_verdict(_ds, "best_realizable_cost"), stated=float(_cost),
        places=0))
    CLAIMS.append(Claim(
        id=f"{_slug}-ratio", doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_r_best_ratio(_ds, _chars), stated=_ratio, places=3))


# The select-the-best disclosure counts the cross-validated configurations
# the maximum is taken over. It understated BEAM by a third until the
# review of #260, so the counts are read out of the artifact, not recalled.
def _r_cv_configs(ds: str):
    return lambda d: float(sum(
        1 for k in d["datasets"][ds]["policies"]
        if k.startswith(("router[", "router_via_type[", "two_stage["))))


for _cid, _ds, _needle, _n in (
    ("router-cv-configs-lme500", "LME-500",
     "is **16** configurations on LongMemEval-500 and on the 78-question "
     "slice —", 16),
    ("router-cv-configs-ku78", "LME-KU78",
     "is **16** configurations on LongMemEval-500 and on the 78-question "
     "slice —", 16),
    ("router-cv-configs-beam", "BEAM-400",
     "two candidate sets each — and **22** on BEAM-400, which has three "
     "because", 22),
):
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(ROUTER,),
        value=_r_cv_configs(_ds), stated=float(_n), places=0))


# ── the same numbers where the CHANGELOG states them ──
# The entry restates ~15 figures in prose. Every comparable prior entry
# (aggp1-*, ev2-*, beamev-*, aggserve-*, c2op-*, lit-v6-*) pins its
# CHANGELOG copy separately from the evals/README one, so that a rewrite of
# either doc alone fails the guard. This section did not, until the review
# of #260.
_ROUTER_CHANGELOG = [
    ("router-cl-lme-realizable-gain",
     "and so does the oracle bound. Realizable gains: **+0.002** on "
     "LongMemEval",
     _r_verdict("LME-500", "realizable_gain"), 0.002, 3),
    ("router-cl-lme-tokens-saved",
     "(500 q, at 327 fewer tokens) and **+0.008** on BEAM 100K (400 q, at "
     "671",
     lambda d: abs(d["verdict"]["LME-500"]["realizable_cost_delta"]),
     327, 0),
    ("router-cl-beam-realizable-gain",
     "(500 q, at 327 fewer tokens) and **+0.008** on BEAM 100K (400 q, at "
     "671",
     _r_verdict("BEAM-400", "realizable_gain"), 0.008, 3),
    ("router-cl-beam-chars-added",
     "MORE context chars). A router with perfect knowledge of the question "
     "type",
     _r_verdict("BEAM-400", "realizable_cost_delta"), 671, 0),
    ("router-cl-lme-oracle-gain",
     "reaches +0.024 (LongMemEval, under the bar) and +0.046 (BEAM, at more",
     _r_verdict("LME-500", "oracle_by_type_gain"), 0.024, 3),
    ("router-cl-beam-oracle-gain",
     "reaches +0.024 (LongMemEval, under the bar) and +0.046 (BEAM, at more",
     _r_verdict("BEAM-400", "oracle_by_type_gain"), 0.046, 3),
    ("router-cl-two-stage-commits",
     "configuration is the two-stage one, which serves cortex on the 193",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "two_stage[logreg|acc]"]["arm_share"]["cortex(commit)"], 193, 0),
    ("router-cl-two-stage-rag",
     "questions where cortex commits and rag on the other 307, landing "
     "exactly",
     lambda d: d["datasets"]["LME-500"]["policies"][
         "two_stage[logreg|acc]"]["arm_share"]["rag"], 307, 0),
    ("router-cl-cascade-score",
     "on the shipped 0.690 / 883 tokens. Question type IS predictable from",
     _r_arm("LME-500", "cascade", "score"), 0.690, 3),
    ("router-cl-cascade-cost",
     "on the shipped 0.690 / 883 tokens. Question type IS predictable from",
     _r_arm("LME-500", "cascade", "cost"), 883, 0),
    ("router-cl-lme-typepred",
     "surface text (0.654 and 0.652 by 5-fold CV against 0.266 / 0.100",
     lambda d: d["datasets"]["LME-500"]["type_predictability"]["logreg"][
         "cv_accuracy"], 0.654, 3),
    ("router-cl-beam-typepred",
     "surface text (0.654 and 0.652 by 5-fold CV against 0.266 / 0.100",
     lambda d: d["datasets"]["BEAM-400"]["type_predictability"]["logreg"][
         "cv_accuracy"], 0.652, 3),
    ("router-cl-lme-typepred-majority",
     "surface text (0.654 and 0.652 by 5-fold CV against 0.266 / 0.100",
     lambda d: d["datasets"]["LME-500"]["type_predictability"]["logreg"][
         "majority_baseline"], 0.266, 3),
    ("router-cl-beam-typepred-majority",
     "surface text (0.654 and 0.652 by 5-fold CV against 0.266 / 0.100",
     lambda d: d["datasets"]["BEAM-400"]["type_predictability"]["logreg"][
         "majority_baseline"], 0.100, 3),
    ("router-cl-ceiling-lme",
     "- Per-question ceilings say the channels do genuinely disagree: 0.778",
     _r_pol("LME-500", "oracle_per_question[base]", "score"), 0.778, 3),
    ("router-cl-ceiling-beam",
     "(LongMemEval-500), 0.789 (BEAM-400), 0.962 (the 78-question",
     _r_pol("BEAM-400", "oracle_per_question[with_nomem]", "score"),
     0.789, 3),
    ("router-cl-ceiling-ku78",
     "(LongMemEval-500), 0.789 (BEAM-400), 0.962 (the 78-question",
     _r_pol("LME-KU78", "oracle_per_question[base]", "score"), 0.962, 3),
    ("router-cl-robustness-agree",
     "- Only 2 of the 4 question types the two benchmarks share agree on a "
     "best",
     lambda d: d["robustness"]["type_pairs"]["n_agree"], 2, 0),
    ("router-cl-robustness-pairs",
     "- Only 2 of the 4 question types the two benchmarks share agree on a "
     "best",
     lambda d: d["robustness"]["type_pairs"]["n_pairs"], 4, 0),
    # the AFTER values of the cross-validation fix, as the entry states them
    ("router-cl-postfix-router-score",
     'had predicted "cascade" on 500 of 500 rows, to 0.678 / 1009; that',
     _r_pol("LME-500", "router[with_cascade|logreg|acc]", "score"),
     0.678, 3),
    ("router-cl-postfix-router-cost",
     'had predicted "cascade" on 500 of 500 rows, to 0.678 / 1009; that',
     _r_pol("LME-500", "router[with_cascade|logreg|acc]", "cost"), 1009, 0),
    ("router-cl-postfix-best-score",
     "maximum passed to the two-stage variant at the same 0.690 / 883. On "
     "the",
     _r_verdict("LME-500", "best_realizable_score"), 0.690, 3),
    ("router-cl-postfix-best-cost",
     "maximum passed to the two-stage variant at the same 0.690 / 883. On "
     "the",
     _r_verdict("LME-500", "best_realizable_cost"), 883, 0),
    ("router-cl-postfix-ku78-score",
     "to 0.846 (gain -0.013); no published figure quotes it. BEAM's "
     "published",
     _r_verdict("LME-KU78", "best_realizable_score"), 0.846, 3),
    ("router-cl-postfix-ku78-gain",
     "to 0.846 (gain -0.013); no published figure quotes it. BEAM's "
     "published",
     _r_verdict("LME-KU78", "realizable_gain"), -0.013, 3),
]

for _cid, _needle, _value, _stated, _places in _ROUTER_CHANGELOG:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(ROUTER,),
        value=_value, stated=float(_stated), places=_places))

CLAIMS.append(Claim(
    id="router-cl-ku78-two-channel-union", doc=CHANGELOG,
    needle="knowledge-update slice, three channels; 0.936 for rag∪cortex "
           "on the same",
    artifacts=(V38_ROWS_JSONL,),
    value=lambda rows: (sum(1 for r in rows
                            if r["rag_correct"] or r["cortex_correct"])
                        / len(rows)),
    stated=0.936, places=3))
# ── the 2026-09-04 retrieval-telemetry / replay campaign (evals/README) ──
# The telemetry review's headline is a COUNT, not an accuracy: how many
# logged retrieval events carry a downstream label. It is pinned like any
# other published number because the Phase-1 go/no-go rests on it, and a
# rerun against a later bank would move it silently otherwise.
TELEMETRY_REVIEW = RESULTS + "retrieval-telemetry-review-20260904.json"
RETRIEVAL_REPLAY = RESULTS + "retrieval-replay-20260904.json"
GRAPH_ABLATION = RESULTS + "graph-ablation-20260904.json"

# -- retrieval-pool probe (2026-09-04) ------------------------------------
# A retrieval PROXY, not a judged number - but it is published as a table,
# so it is backed like any other. Every cell reports the same recall, which
# is exactly the kind of "did anyone actually run this?" claim the two
# audits found unbacked; the latency column is pinned too, because a rerun
# that moves it must move the doc.
POOL_PROBE = RESULTS + "retrieval-pool-probe-20260904.json"


def _pool(mult: int, fusion: str, rerank: str, field: str):
    def read(doc):
        cell = next(c for c in doc["cells"]
                    if c["multiplier"] == mult and c["fusion"] == fusion
                    and c["reranker"] == rerank)
        return float(cell[field])
    return read


_POOL_ROWS = [
    # (multiplier, fusion, reranker, verbatim README row, recall, churn, ms)
    (1, "weighted_sum", "off",
     "| 1 | weighted_sum | off | 0.700 | 0.300 | \u2014 (baseline) | 52 ms |",
     0.700, None, 52),
    (1, "weighted_sum", "on",
     "| 1 | weighted_sum | on  | 0.700 | 0.300 | 0.000 | 112 ms |",
     0.700, 0.000, 112),
    (1, "rrf", "off",
     "| 1 | rrf | off | 0.700 | 0.300 | 0.183 | 55 ms |", 0.700, 0.183, 55),
    (1, "rrf", "on",
     "| 1 | rrf | on  | 0.700 | 0.300 | 0.183 | 214 ms |", 0.700, 0.183, 214),
    (4, "weighted_sum", "off",
     "| 4 | weighted_sum | off | 0.700 | 0.300 | 0.283 | 48 ms |",
     0.700, 0.283, 48),
    (4, "weighted_sum", "on",
     "| 4 | weighted_sum | on  | 0.700 | 0.300 | 0.283 | 373 ms |",
     0.700, 0.283, 373),
    (4, "rrf", "off",
     "| 4 | rrf | off | 0.700 | 0.300 | 0.317 | 75 ms |", 0.700, 0.317, 75),
    (4, "rrf", "on",
     "| 4 | rrf | on  | 0.700 | 0.300 | 0.333 | 560 ms |",
     0.700, 0.333, 560),
]

for _m, _f, _r, _needle, _recall, _churn, _ms in _POOL_ROWS:
    _slug = f"{_m}-{_f}-{_r}"
    CLAIMS.append(Claim(
        id=f"pool-probe-{_slug}-recall", doc=EVALS, needle=_needle,
        artifacts=(POOL_PROBE,), value=_pool(_m, _f, _r, "recall_at_6"),
        stated=_recall, places=3))
    CLAIMS.append(Claim(
        id=f"pool-probe-{_slug}-stale", doc=EVALS, needle=_needle,
        artifacts=(POOL_PROBE,), value=_pool(_m, _f, _r, "stale_leak"),
        stated=0.300, places=3))
    CLAIMS.append(Claim(
        id=f"pool-probe-{_slug}-latency", doc=EVALS, needle=_needle,
        artifacts=(POOL_PROBE,), value=_pool(_m, _f, _r, "latency_ms"),
        stated=_ms, places=0))
    if _churn is not None:
        CLAIMS.append(Claim(
            id=f"pool-probe-{_slug}-churn", doc=EVALS, needle=_needle,
            artifacts=(POOL_PROBE,),
            value=_pool(_m, _f, _r, "churn_vs_shipped"),
            stated=_churn, places=3))


# -- candidate-pool JUDGED verdict (2026-09-04) ---------------------------
# The probe above is a proxy; this is the run that decided the knobs. Three
# summaries (control, multiplier 4 + rrf, multiplier 4 + weighted_sum) plus
# the two paired comparisons that carry the deltas, p-values and per-
# question win/loss counts — a p-value gets its own artifact, so the pairs
# files are cited for those and never the summaries.
POOL_CTL = RESULTS + "longmemeval-ku-oracle-qwen-27b-pool-ctl.summary.json"
POOL_M4RRF = RESULTS + "longmemeval-ku-oracle-qwen-27b-pool-m4rrf.summary.json"
POOL_M4SUM = RESULTS + "longmemeval-ku-oracle-qwen-27b-pool-m4sum.summary.json"
POOL_RRF_PAIRS = RESULTS + "compare-pool-m4rrf-pairs.json"
POOL_SUM_PAIRS = RESULTS + "compare-pool-m4sum-pairs.json"


def _pool_arm(arm: str, field: str):
    return lambda d: float(d["arms"][arm][field])


def _pool_paired(arm: str, field: str):
    return lambda d: float(d["paired"]["a_vs_b"][arm][field])


# (arm, verbatim README row, ctl acc, ctl tokens,
#  rrf acc, rrf tokens, rrf delta, rrf p, rrf wins, rrf losses,
#  sum acc, sum tokens, sum delta, sum p, sum wins, sum losses)
_POOL_JUDGED = [
    ("rag",
     "| naive RAG (top-6 turns) | 0.859 @ 1184.1 tok | 0.744 @ 1793.0 "
     "(-0.115, p 0.0506, 4W/13L) | 0.782 @ 1643.0 (-0.077, p 0.1071, "
     "2W/8L) |",
     0.859, 1184.1, 0.744, 1793.0, -0.115, 0.0506, 4, 13,
     0.782, 1643.0, -0.077, 0.1071, 2, 8),
    ("cortex",
     "| cortex facts only | 0.667 @ 96.7 tok | 0.667 @ 96.7 (0.000, p 1.0, "
     "0W/0L) | 0.667 @ 96.7 (0.000, p 1.0, 0W/0L) |",
     0.667, 96.7, 0.667, 96.7, 0.000, 1.0, 0, 0,
     0.667, 96.7, 0.000, 1.0, 0, 0),
    ("hybrid",
     "| hybrid (facts + top-3 turns) | 0.897 @ 1289.7 tok | 0.833 @ 1898.6 "
     "(-0.064, p 0.1265, 1W/6L) | 0.872 @ 1748.6 (-0.026, p 0.6194, "
     "1W/3L) |",
     0.897, 1289.7, 0.833, 1898.6, -0.064, 0.1265, 1, 6,
     0.872, 1748.6, -0.026, 0.6194, 1, 3),
    ("cascade",
     "| commit-gated cascade | 0.846 @ 389.4 tok | 0.846 @ 598.7 (0.000, "
     "p 1.0, 1W/1L) | 0.859 @ 544.5 (+0.013, p 1.0, 2W/1L) |",
     0.846, 389.4, 0.846, 598.7, 0.000, 1.0, 1, 1,
     0.859, 544.5, 0.013, 1.0, 2, 1),
]

for (_arm, _needle, _c_acc, _c_tok, _r_acc, _r_tok, _r_d, _r_p, _r_w, _r_l,
     _s_acc, _s_tok, _s_d, _s_p, _s_w, _s_l) in _POOL_JUDGED:
    for _tag, _art, _acc, _tok in (("ctl", POOL_CTL, _c_acc, _c_tok),
                                   ("m4rrf", POOL_M4RRF, _r_acc, _r_tok),
                                   ("m4sum", POOL_M4SUM, _s_acc, _s_tok)):
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-acc", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_arm(_arm, "accuracy"),
            stated=_acc, places=3))
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-tokens", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_arm(_arm, "context_tokens"),
            stated=_tok, places=1))
    for _tag, _art, _d, _pv, _w, _l in (
            ("m4rrf", POOL_RRF_PAIRS, _r_d, _r_p, _r_w, _r_l),
            ("m4sum", POOL_SUM_PAIRS, _s_d, _s_p, _s_w, _s_l)):
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-delta", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_paired(_arm, "delta"),
            stated=_d, places=3))
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-p", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_paired(_arm, "p"),
            stated=_pv, places=4))
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-wins", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_paired(_arm, "wins"),
            stated=_w, places=0))
        CLAIMS.append(Claim(
            id=f"pool-judged-{_tag}-{_arm}-losses", doc=EVALS, needle=_needle,
            artifacts=(_art,), value=_pool_paired(_arm, "losses"),
            stated=_l, places=0))

# The CHANGELOG states the same verdict in prose; its numbers are pinned
# separately because a reader meets them there first (the retire-at-the-
# old-site rule cuts both ways - a claim gets guarded wherever it is made).
for _cid, _arm, _art, _val, _stated, _places in [
    ("changelog-pool-rrf-rag-ctl", "rag", POOL_CTL,
     _pool_arm("rag", "accuracy"), 0.859, 3),
    ("changelog-pool-rrf-rag", "rag", POOL_M4RRF,
     _pool_arm("rag", "accuracy"), 0.744, 3),
    ("changelog-pool-rrf-rag-delta", "rag", POOL_RRF_PAIRS,
     _pool_paired("rag", "delta"), -0.115, 3),
    ("changelog-pool-rrf-rag-p", "rag", POOL_RRF_PAIRS,
     _pool_paired("rag", "p"), 0.0506, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG,
        needle="takes naive RAG from 0.859 to 0.744 (-0.115, p 0.0506)",
        artifacts=(_art,), value=_val, stated=_stated, places=_places))

for _cid, _art, _val, _stated, _places in [
    ("changelog-pool-rrf-hybrid-ctl", POOL_CTL,
     _pool_arm("hybrid", "accuracy"), 0.897, 3),
    ("changelog-pool-rrf-hybrid", POOL_M4RRF,
     _pool_arm("hybrid", "accuracy"), 0.833, 3),
    ("changelog-pool-rrf-hybrid-delta", POOL_RRF_PAIRS,
     _pool_paired("hybrid", "delta"), -0.064, 3),
    ("changelog-pool-rrf-hybrid-p", POOL_RRF_PAIRS,
     _pool_paired("hybrid", "p"), 0.1265, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG,
        needle="from 0.897 to 0.833 (-0.064, p 0.1265)",
        artifacts=(_art,), value=_val, stated=_stated, places=_places))

for _cid, _needle, _art, _val, _stated, _places in [
    ("changelog-pool-sum-rag", "takes RAG to 0.782 (-0.077, p 0.1071)",
     POOL_M4SUM, _pool_arm("rag", "accuracy"), 0.782, 3),
    ("changelog-pool-sum-rag-delta", "takes RAG to 0.782 (-0.077, p 0.1071)",
     POOL_SUM_PAIRS, _pool_paired("rag", "delta"), -0.077, 3),
    ("changelog-pool-sum-rag-p", "takes RAG to 0.782 (-0.077, p 0.1071)",
     POOL_SUM_PAIRS, _pool_paired("rag", "p"), 0.1071, 4),
    ("changelog-pool-sum-hybrid", "and hybrid to 0.872",
     POOL_M4SUM, _pool_arm("hybrid", "accuracy"), 0.872, 3),
    ("changelog-pool-sum-hybrid-delta", "(-0.026, p 0.6194)",
     POOL_SUM_PAIRS, _pool_paired("hybrid", "delta"), -0.026, 3),
    ("changelog-pool-sum-hybrid-p", "(-0.026, p 0.6194)",
     POOL_SUM_PAIRS, _pool_paired("hybrid", "p"), 0.6194, 4),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle,
        artifacts=(_art,), value=_val, stated=_stated, places=_places))

# The zero noise floor: the cortex arm is identical in all three runs, and
# both docs lean on that to call the other deltas real.
for _tag, _art in (("ctl", POOL_CTL), ("m4rrf", POOL_M4RRF),
                   ("m4sum", POOL_M4SUM)):
    CLAIMS.append(Claim(
        id=f"changelog-pool-cortex-floor-{_tag}", doc=CHANGELOG,
        needle="is 0.667 in all three runs, 0W/0L",
        artifacts=(_art,), value=_pool_arm("cortex", "accuracy"),
        stated=0.667, places=3))

# The configuration guide's gated-off bullet quotes the same two RAG
# deltas as absolute costs; pinned there too, because that is where an
# operator deciding whether to flip the knob actually reads them.
for _cid, _needle, _art, _stated in [
    ("config-guide-pool-rrf-rag-delta",
     "multiplier 4 cost naive RAG 0.115 accuracy under",
     POOL_RRF_PAIRS, 0.115),
    ("config-guide-pool-sum-rag-delta",
     "`rrf` and 0.077 under `weighted_sum`",
     POOL_SUM_PAIRS, 0.077),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CONFIG_GUIDE, needle=_needle, artifacts=(_art,),
        # The guide quotes the loss as a positive cost; the artifact
        # carries the signed delta.
        value=lambda d: -_pool_paired("rag", "delta")(d),
        stated=_stated, places=3))

# ── the recall fan-out caps (2026-09-04) ─────────────────────────────────
# CPU-only paired run on a restored copy of the live bank: the same 20
# relational questions with the search caps off and on. The claim that
# matters is the honesty one — no expected target the uncapped walk found
# was lost — so `targets_lost` is pinned as a count beside the speedups.
FANOUT_CAP = RESULTS + "recall-fanout-cap-20260904.json"
_FANOUT_SEARCHES = "mean\n    89.15 → 12.40 and max 205 → 19"
_FANOUT_WALL = "recall wall mean 25.25 s → 4.166 s and\n    max 57.67 s → 7.51 s"
_FANOUT_CHARS = "served characters mean 178,110 → 77,546"
_FANOUT_LOSS = "20/20 in both arms with **no target lost**"
for _cid, _needle, _val, _stated, _places in [
    ("fanout-searches-before", _FANOUT_SEARCHES,
     lambda d: d["before"]["summary"]["searches_issued"]["mean"], 89.15, 2),
    ("fanout-searches-after", _FANOUT_SEARCHES,
     lambda d: d["after"]["summary"]["searches_issued"]["mean"], 12.40, 2),
    ("fanout-searches-max-before", _FANOUT_SEARCHES,
     lambda d: d["before"]["summary"]["searches_issued"]["max"], 205, 0),
    ("fanout-searches-max-after", _FANOUT_SEARCHES,
     lambda d: d["after"]["summary"]["searches_issued"]["max"], 19, 0),
    ("fanout-wall-before", _FANOUT_WALL,
     lambda d: d["before"]["summary"]["recall_wall_s"]["mean"], 25.25, 3),
    ("fanout-wall-after", _FANOUT_WALL,
     lambda d: d["after"]["summary"]["recall_wall_s"]["mean"], 4.166, 3),
    ("fanout-wall-max-before", _FANOUT_WALL,
     lambda d: d["before"]["summary"]["recall_wall_s"]["max"], 57.67, 2),
    ("fanout-wall-max-after", _FANOUT_WALL,
     lambda d: d["after"]["summary"]["recall_wall_s"]["max"], 7.51, 2),
    ("fanout-chars-before", _FANOUT_CHARS,
     lambda d: d["before"]["summary"]["recall_served_chars"]["mean"],
     178110.3, 1),
    ("fanout-chars-after", _FANOUT_CHARS,
     lambda d: d["after"]["summary"]["recall_served_chars"]["mean"],
     77546.4, 1),
    ("fanout-hits-before", _FANOUT_LOSS,
     lambda d: d["before"]["summary"]["recall_expected_hits"], 20, 0),
    ("fanout-hits-after", _FANOUT_LOSS,
     lambda d: d["after"]["summary"]["recall_expected_hits"], 20, 0),
    ("fanout-targets-lost", _FANOUT_LOSS,
     lambda d: len(d["targets_lost"]), 0, 0),
    ("fanout-n", "20 relational questions — the twelve",
     lambda d: d["n_questions"], 20, 0),
    ("fanout-texts-before", "2,116 texts → 558",
     lambda d: d["structural_identity"]["texts_total_before"], 2116, 0),
    ("fanout-texts-after", "2,116 texts → 558",
     lambda d: d["structural_identity"]["texts_total_after"], 558, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=CHANGELOG, needle=_needle, artifacts=(FANOUT_CAP,),
        value=_val, stated=_stated, places=_places))

# The same run's evals/README table, plus the structural-identity row that
# says WHY no target was lost: the caps bound searches, not expansion.
_FANOUT_README_SEARCHES = "searches issued  mean     89.15      12.40"
_FANOUT_README_WALL = "recall wall (s)  mean     25.25        4.166"
_FANOUT_README_TEXTS = "2,116 texts before, 558 after"
_FANOUT_README_STRUCT = ("identical on all 20 questions"
                         "\n  (`structural_identity`)")
for _cid, _needle, _val, _stated, _places in [
    ("fanout-readme-searches-before", _FANOUT_README_SEARCHES,
     lambda d: d["before"]["summary"]["searches_issued"]["mean"], 89.15, 2),
    ("fanout-readme-searches-after", _FANOUT_README_SEARCHES,
     lambda d: d["after"]["summary"]["searches_issued"]["mean"], 12.40, 2),
    ("fanout-readme-wall-before", _FANOUT_README_WALL,
     lambda d: d["before"]["summary"]["recall_wall_s"]["mean"], 25.25, 3),
    ("fanout-readme-wall-after", _FANOUT_README_WALL,
     lambda d: d["after"]["summary"]["recall_wall_s"]["mean"], 4.166, 3),
    ("fanout-readme-texts-before", _FANOUT_README_TEXTS,
     lambda d: d["structural_identity"]["texts_total_before"], 2116, 0),
    ("fanout-readme-texts-after", _FANOUT_README_TEXTS,
     lambda d: d["structural_identity"]["texts_total_after"], 558, 0),
    ("fanout-readme-entities-identical", _FANOUT_README_STRUCT,
     lambda d: d["structural_identity"][
         "questions_with_different_entity_count"], 0, 0),
    ("fanout-readme-edges-identical", _FANOUT_README_STRUCT,
     lambda d: d["structural_identity"][
         "questions_with_different_edge_count"], 0, 0),
    ("fanout-readme-part-of-arrivals", "1,046 of the 1,763 added",
     lambda d: d["after"]["summary"]["arrivals_total"]["via_part_of"],
     1046, 0),
    ("fanout-readme-added-arrivals", "1,046 of the 1,763 added",
     lambda d: d["after"]["summary"]["arrivals_total"]["added"], 1763, 0),
    ("fanout-readme-search-hits", "found 18 of the 20 targets",
     lambda d: d["after"]["summary"]["search_expected_hits"], 18, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(FANOUT_CAP,),
        value=_val, stated=_stated, places=_places))

# The retrieval guide restates the same run's headline pair; a restatement
# is a claim like any other.
_FANOUT_GUIDE_BEFORE = "mean of 89.15 searches and 25.25 s per call (max 205 and\n57.67 s)"
_FANOUT_GUIDE_AFTER = "call to 12.40 searches and 4.166 s"
for _cid, _needle, _val, _stated, _places in [
    ("fanout-guide-searches-before", _FANOUT_GUIDE_BEFORE,
     lambda d: d["before"]["summary"]["searches_issued"]["mean"], 89.15, 2),
    ("fanout-guide-wall-before", _FANOUT_GUIDE_BEFORE,
     lambda d: d["before"]["summary"]["recall_wall_s"]["mean"], 25.25, 3),
    ("fanout-guide-searches-max-before", _FANOUT_GUIDE_BEFORE,
     lambda d: d["before"]["summary"]["searches_issued"]["max"], 205, 0),
    ("fanout-guide-wall-max-before", _FANOUT_GUIDE_BEFORE,
     lambda d: d["before"]["summary"]["recall_wall_s"]["max"], 57.67, 2),
    ("fanout-guide-searches-after", _FANOUT_GUIDE_AFTER,
     lambda d: d["after"]["summary"]["searches_issued"]["mean"], 12.40, 2),
    ("fanout-guide-wall-after", _FANOUT_GUIDE_AFTER,
     lambda d: d["after"]["summary"]["recall_wall_s"]["mean"], 4.166, 3),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=RETRIEVAL_GUIDE, needle=_needle, artifacts=(FANOUT_CAP,),
        value=_val, stated=_stated, places=_places))

# The hit CHANNEL — the power of the targets_lost check. Only a target
# carried by `texts` could be lost (the entity sets are identical before
# and after by construction), so the entity/texts split is what says
# whether a clean `targets_lost` means anything.
_FANOUT_CHANNELS_CH = "**3 of the 20** arrived on `texts` (17 on `entity`)"
_FANOUT_CHANNELS_EV = "17 targets arrived on `entity` (where the check has no"
for _cid, _doc, _needle, _val, _stated in [
    ("fanout-channel-texts-changelog", CHANGELOG, _FANOUT_CHANNELS_CH,
     lambda d: d["after"]["summary"]["hit_channels"]["texts"], 3),
    ("fanout-channel-entity-changelog", CHANGELOG, _FANOUT_CHANNELS_CH,
     lambda d: d["after"]["summary"]["hit_channels"]["entity"], 17),
    ("fanout-channel-texts-evals", EVALS, _FANOUT_CHANNELS_EV,
     lambda d: d["before"]["summary"]["hit_channels"]["texts"], 3),
    ("fanout-channel-entity-evals", EVALS, _FANOUT_CHANNELS_EV,
     lambda d: d["before"]["summary"]["hit_channels"]["entity"], 17),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(FANOUT_CAP,),
        value=_val, stated=_stated, places=0))
_TELEM_NEEDLE = "| **events with any downstream signal** | **1** (0.074%) |"
CLAIMS.append(Claim(
    id="telemetry-labelled-events", doc=EVALS, needle=_TELEM_NEEDLE,
    artifacts=(TELEMETRY_REVIEW,),
    value=lambda d: d["labels"]["events_with_any_downstream_signal"],
    stated=1, places=0))
CLAIMS.append(Claim(
    id="telemetry-logged-events", doc=EVALS,
    needle="| logged events | 1349 |", artifacts=(TELEMETRY_REVIEW,),
    value=lambda d: d["events"]["n_events"], stated=1349, places=0))
CLAIMS.append(Claim(
    id="telemetry-explicit-reinforcements", doc=EVALS,
    needle="| `entries.explicit_reinforcements`, bank-wide sum | **0** |",
    artifacts=(TELEMETRY_REVIEW,),
    value=lambda d: d["bank"]["entries_explicit_reinforcements_total"],
    stated=0, places=0))


def _replay(arm: str, metric: str):
    return lambda d: d["results"]["logged-top1"]["arms"][arm][metric]


def _dig(d: dict, path: tuple[str, ...]):
    for k in path:
        d = d[k]
    return d


for _arm, _needle, _mrr, _h1 in [
    ("shipped",
     "| `shipped` (deployed config) | 0.784 | 0.668 | 0.888 | 0.948 |",
     0.784, 0.668),
    ("bm25_off",
     "| `bm25_off` | 0.689 | 0.544 | 0.812 | 0.920 |",
     0.689, 0.544),
    ("rerank_on",
     "| `rerank_on` | 0.606 | 0.368 | 0.852 | 0.948 |",
     0.606, 0.368),
]:
    CLAIMS.append(Claim(
        id=f"replay-{_arm}-mrr", doc=EVALS, needle=_needle,
        artifacts=(RETRIEVAL_REPLAY,), value=_replay(_arm, "mrr"),
        stated=_mrr, places=3))
    CLAIMS.append(Claim(
        id=f"replay-{_arm}-hit1", doc=EVALS, needle=_needle,
        artifacts=(RETRIEVAL_REPLAY,), value=_replay(_arm, "hit@1"),
        stated=_h1, places=3))

# The graph shape and the recall-vs-search price. The published cost
# ratios are the headline of lever 6, so both are pinned; the hit-rate
# column is deliberately NOT pinned as a quality claim (both arms are at
# the ceiling, which the prose says outright).
_GRAPH_SHAPE_NEEDLE = "| entities | 5504 |"
for _cid, _path, _stated in [
    ("graph-entities", ("graph_shape", "entities"), 5504),
    ("graph-edges-live", ("graph_shape", "edges_live"), 4020),
    ("graph-dead-weight", ("graph_shape", "dead_weight_entities", "count"),
     421),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS,
        needle=(_GRAPH_SHAPE_NEEDLE if _cid == "graph-entities"
                else "| edges (live / all versions) | 4020 / 4247 |"
                if _cid == "graph-edges-live"
                else "| dead weight (only `part-of` edges, no current fact)"
                     " | 421 |"),
        artifacts=(GRAPH_ABLATION,),
        value=(lambda p: lambda d: _dig(d, p))(_path),
        stated=_stated, places=0))

_REL = ("ablation", "relational_questions", "summary")
CLAIMS.append(Claim(
    id="graph-recall-chars-ratio", doc=EVALS,
    needle="27× the characters at 74× the wall time",
    artifacts=(GRAPH_ABLATION,),
    value=lambda d: _dig(d, _REL + ("chars_ratio_recall_over_search",)),
    stated=27, places=0))
CLAIMS.append(Claim(
    id="graph-recall-time-ratio", doc=EVALS,
    needle="27× the characters at 74× the wall time",
    artifacts=(GRAPH_ABLATION,),
    value=lambda d: _dig(d, _REL + ("time_ratio_recall_over_search",)),
    stated=74, places=0))
CLAIMS.append(Claim(
    id="graph-arrivals-added", doc=EVALS,
    needle="Of the 524\nentities `recall` added beyond its seeds",
    artifacts=(GRAPH_ABLATION,),
    value=lambda d: _dig(d, _REL + ("arrivals_total", "added")),
    stated=524, places=0))
# One needle PER ROW. Until the 2026-09-04 review these four claims all
# carried the `unlinked` row's text, so three of them verified a row that
# does not state their number: the hub/part-of/domain counts could each be
# edited to anything and stay green.
for _cid, _key, _needle, _stated in [
    ("hub", "via_hub",
     "| touches a hub (degree >= p95 = 5) | 520 | 99.2% |", 520),
    ("part-of", "via_part_of",
     "| only `part-of` edges | 225 | 42.9% |", 225),
    ("domain", "via_domain",
     "| at least one domain relation | 299 | 57.1% |", 299),
    ("unlinked", "unlinked",
     "| unlinked (came from the re-query, not an edge) | 0 | 0% |", 0),
]:
    CLAIMS.append(Claim(
        id=f"graph-arrivals-{_cid}", doc=EVALS, needle=_needle,
        artifacts=(GRAPH_ABLATION,),
        value=(lambda k: lambda d: _dig(d, _REL + ("arrivals_total", k)))(_key),
        stated=_stated, places=0))
# The hub threshold the "touches a hub" row names, from the same run.
CLAIMS.append(Claim(
    id="graph-hub-degree-p95", doc=EVALS,
    needle="| touches a hub (degree >= p95 = 5) | 520 | 99.2% |",
    artifacts=(GRAPH_ABLATION,), value=lambda d: d["hub_degree_p95"],
    stated=5, places=0))


# The per-arm MEDIAN latency. Added 2026-09-04 in pre-merge review: the
# table shipped 0.234 / 0.101 / 0.394 s, none of which the artifact
# carries, because nothing pinned the latency column while MRR and hit@1
# beside it were pinned. The ~165 ms BM25 cost in the prose below the
# table is the difference of the first two, so pinning both pins it.
_LAT = "median_latency_s"
for _arm, _needle, _stated in [
    ("shipped",
     "| `shipped` (deployed config) | 0.784 | 0.668 | 0.888 | 0.948 |"
     " 0.305 s |", 0.305),
    ("bm25_off",
     "| `bm25_off` | 0.689 | 0.544 | 0.812 | 0.920 | 0.140 s |", 0.140),
    ("rerank_on",
     "| `rerank_on` | 0.606 | 0.368 | 0.852 | 0.948 | 0.694 s |", 0.694),
]:
    CLAIMS.append(Claim(
        id=f"replay-{_arm}-median-latency", doc=EVALS, needle=_needle,
        artifacts=(RETRIEVAL_REPLAY,), value=_replay(_arm, _LAT),
        stated=_stated, places=3))

# `memory_recall`'s per-call wall time. The docs and the harness comment
# both quoted "~2.5 minutes per call" until the 2026-09-04 pre-merge
# review; the artifact says 32.4 s / 44.3 s mean, 73.0 s worst case. The
# figure sizes every run of this harness, so it is pinned at both means.
_LOG = ("ablation", "logged_queries", "summary")
_WALL_NEEDLE = "| mean wall time | 0.44 s | 32.4 s | 0.39 s | 44.3 s |"
for _cid, _path, _stated in [
    ("relational", _REL + ("recall", "mean_wall_s"), 32.4),
    ("logged", _LOG + ("recall", "mean_wall_s"), 44.3),
]:
    CLAIMS.append(Claim(
        id=f"graph-recall-mean-wall-{_cid}", doc=EVALS, needle=_WALL_NEEDLE,
        artifacts=(GRAPH_ABLATION,),
        value=(lambda p: lambda d: _dig(d, p))(_path),
        stated=_stated, places=1))
CLAIMS.append(Claim(
    id="graph-recall-max-wall", doc=EVALS,
    needle="max\n73.0 s), and small enough that the hit-rate column is a",
    artifacts=(GRAPH_ABLATION,),
    value=lambda d: max(q["recall"]["wall_s"] for q in
                        d["ablation"]["relational_questions"]["per_question"]),
    stated=73.0, places=1))


# ── the rest of the 2026-09-04 campaign's published numbers ──────────────
# Added in the same pre-merge review that fixed the arrivals needles: the
# three sections above published ~50 figures and pinned 20 of them, so a
# rerun against a later bank could have moved the unpinned ones silently.
# Everything a reader would quote back is pinned here; per-day event
# counts, PR/schema references and arithmetic restated in prose are not.

# -- telemetry review -----------------------------------------------------
_SESS_NEEDLE = "| distinct sessions / episodes | 60 / 101 |"
_SERVED_LEN_NEEDLE = ("| served-list length: mean / mode | 4.94 / 5 "
                      "(146 events served exactly 1; 0 served nothing) |")
_PARAMS_NEEDLE = "| `params` coverage (v32+) | 790 / 1349 (58.6%) |"
_FACTS_NEEDLE = ("| `served_facts` coverage (v34+) | 160 / 1349 (11.9%), "
                 "798 facts |")
_JOIN_NEEDLE = ("| served entry ids that still resolve in `entries` | "
                "6666 of 6666 (no dangling ids) |")
_SLOTS_NEEDLE = "| `slot_reads` | 605 slots, 807 serves — all serve-side |"
_USES_NEEDLE = ("| `retrieval_uses` rows | 1 (`used_via=get`, served rank 0, "
                "72 s after the serve) |")
for _cid, _needle, _val, _stated, _places in [
    ("telemetry-sessions", _SESS_NEEDLE,
     lambda d: d["events"]["distinct_sessions"], 60, 0),
    ("telemetry-episodes", _SESS_NEEDLE,
     lambda d: d["events"]["distinct_episodes"], 101, 0),
    ("telemetry-signal-pct", _TELEM_NEEDLE,
     lambda d: d["labels"]["events_with_any_downstream_signal_pct"],
     0.074, 3),
    ("telemetry-uses-rows", _USES_NEEDLE, lambda d: d["uses"]["n_uses"],
     1, 0),
    ("telemetry-uses-rank", _USES_NEEDLE,
     lambda d: d["uses"]["detail"][0]["served_rank"], 0, 0),
    ("telemetry-uses-latency", _USES_NEEDLE,
     lambda d: d["uses"]["detail"][0]["latency_s"], 72.0, 0),
    ("telemetry-mean-served-len", _SERVED_LEN_NEEDLE,
     lambda d: d["events"]["mean_served_len"], 4.94, 2),
    ("telemetry-mode-served-len", _SERVED_LEN_NEEDLE,
     lambda d: int(max(d["events"]["served_len_distribution"].items(),
                       key=lambda kv: kv[1])[0]), 5, 0),
    ("telemetry-served-len-1", _SERVED_LEN_NEEDLE,
     lambda d: d["events"]["served_len_distribution"]["1"], 146, 0),
    ("telemetry-zero-result-events", _SERVED_LEN_NEEDLE,
     lambda d: d["events"]["zero_result_events"], 0, 0),
    ("telemetry-params-rows", _PARAMS_NEEDLE,
     lambda d: d["events"]["params_coverage"]["rows"], 790, 0),
    ("telemetry-params-pct", _PARAMS_NEEDLE,
     lambda d: d["events"]["params_coverage"]["pct"], 58.6, 1),
    # The prose below the table rounds the same coverage to a whole 59%.
    ("telemetry-params-pct-prose",
     "join, and 59% of rows carry the ranking-knob snapshot",
     lambda d: d["events"]["params_coverage"]["pct"], 59, 0),
    ("telemetry-served-facts-rows", _FACTS_NEEDLE,
     lambda d: d["events"]["served_facts_coverage"]["rows"], 160, 0),
    ("telemetry-served-facts-pct", _FACTS_NEEDLE,
     lambda d: d["events"]["served_facts_coverage"]["pct"], 11.9, 1),
    ("telemetry-served-facts-total", _FACTS_NEEDLE,
     lambda d: d["events"]["served_facts_coverage"]["total_facts_served"],
     798, 0),
    ("telemetry-id-join-resolved", _JOIN_NEEDLE,
     lambda d: d["labels"]["served_id_join"]["served_ids_still_in_entries"],
     6666, 0),
    ("telemetry-id-join-dangling", _JOIN_NEEDLE,
     lambda d: d["labels"]["served_id_join"]["served_ids_dangling"], 0, 0),
    ("telemetry-slot-read-rows", _SLOTS_NEEDLE,
     lambda d: d["bank"]["slot_reads_rows"], 605, 0),
    ("telemetry-slot-read-serves", _SLOTS_NEEDLE,
     lambda d: d["bank"]["slot_reads_total"], 807, 0),
    # The go/no-go sentence: how far short of the Phase-1 label floor.
    ("telemetry-shortfall",
     "so Phase 1 is 299 labelled events short of its own",
     lambda d: d["verdict"]["shortfall"], 299, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(TELEMETRY_REVIEW,),
        value=_val, stated=_stated, places=_places))

# -- offline replay -------------------------------------------------------
# The run size, which bounds how much the arm table can claim.
_REPLAY_RUN_NEEDLE = "250 sampled events, top_k=6"
CLAIMS.append(Claim(
    id="replay-sample-size", doc=EVALS, needle=_REPLAY_RUN_NEEDLE,
    artifacts=(RETRIEVAL_REPLAY,),
    value=lambda d: d["results"]["logged-top1"]["n_cases"],
    stated=250, places=0))
CLAIMS.append(Claim(
    id="replay-top-k", doc=EVALS, needle=_REPLAY_RUN_NEEDLE,
    artifacts=(RETRIEVAL_REPLAY,), value=lambda d: d["top_k"],
    stated=6, places=0))
# hit@3 / hit@6 complete the arm table. hit@6 carries the "the reranker
# only re-orders the same six" reading, so it is a claim, not decoration.
for _arm, _needle, _h3, _h6 in [
    ("shipped",
     "| `shipped` (deployed config) | 0.784 | 0.668 | 0.888 | 0.948 |",
     0.888, 0.948),
    ("bm25_off", "| `bm25_off` | 0.689 | 0.544 | 0.812 | 0.920 |",
     0.812, 0.920),
    ("rerank_on", "| `rerank_on` | 0.606 | 0.368 | 0.852 | 0.948 |",
     0.852, 0.948),
]:
    CLAIMS.append(Claim(
        id=f"replay-{_arm}-hit3", doc=EVALS, needle=_needle,
        artifacts=(RETRIEVAL_REPLAY,), value=_replay(_arm, "hit@3"),
        stated=_h3, places=3))
    CLAIMS.append(Claim(
        id=f"replay-{_arm}-hit6", doc=EVALS, needle=_needle,
        artifacts=(RETRIEVAL_REPLAY,), value=_replay(_arm, "hit@6"),
        stated=_h6, places=3))

# -- graph shape ----------------------------------------------------------
_DEG_NEEDLE = "| degree p50 / p95 / max | 1 / 5 / 132 |"
_NOEDGE_NEEDLE = "| entities with no live edge at all | 1156 (21%) |"
for _cid, _needle, _val, _stated, _places in [
    ("graph-edges-all-versions",
     "| edges (live / all versions) | 4020 / 4247 |",
     lambda d: d["graph_shape"]["edges_all_versions"], 4247, 0),
    ("graph-degree-p50", _DEG_NEEDLE,
     lambda d: d["graph_shape"]["degree_p50"], 1, 0),
    ("graph-degree-p95", _DEG_NEEDLE,
     lambda d: d["graph_shape"]["degree_p95"], 5, 0),
    ("graph-degree-max", _DEG_NEEDLE,
     lambda d: d["graph_shape"]["degree_max"], 132, 0),
    ("graph-part-of-share",
     "| `part-of` share of live edges | 19.0% |",
     lambda d: d["graph_shape"]["part_of_share"] * 100, 19.0, 1),
    ("graph-no-live-edge", _NOEDGE_NEEDLE,
     lambda d: d["graph_shape"]["entities_with_no_live_edge"], 1156, 0),
    ("graph-no-live-edge-pct", _NOEDGE_NEEDLE,
     lambda d: (d["graph_shape"]["entities_with_no_live_edge"]
                / d["graph_shape"]["entities"] * 100), 21, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(GRAPH_ABLATION,),
        value=_val, stated=_stated, places=_places))

# The 13-relation breakdown is published as one wrapped prose sentence, so
# each relation gets the fragment that states its own count as its needle
# (`hosts` straddles the line break, hence the embedded newline).
for _rel, _needle, _stated in [
    ("prefers", "`prefers` 929", 929),
    ("part-of", "`part-of` 765", 765),
    ("uses", "`uses` 736", 736),
    ("configures", "`configures` 272", 272),
    ("depends-on", "`depends-on` 272", 272),
    ("related-to", "`related-to` 181", 181),
    ("implements", "`implements` 181", 181),
    ("avoids", "`avoids` 162", 162),
    ("tests", "`tests` 148", 148),
    ("runs-on", "`runs-on` 139", 139),
    ("stores-data-in", "`stores-data-in` 116", 116),
    ("hosts", "`hosts`\n70", 70),
    ("superseded-by", "`superseded-by` 49", 49),
]:
    CLAIMS.append(Claim(
        id=f"graph-edges-{_rel}", doc=EVALS, needle=_needle,
        artifacts=(GRAPH_ABLATION,),
        value=(lambda r: lambda d:
               d["graph_shape"]["edges_by_relation"][r])(_rel),
        stated=_stated, places=0))


def _comparator(term: str):
    return lambda d: next(
        r["entries_mentioning"] for r in
        d["graph_shape"]["comparator_coverage"]["detail"]
        if r["term"] == term)


# The comparator gap. The finding is that these two clear the threshold and
# still have no node, so both entry counts and the threshold are
# load-bearing.
for _cid, _needle, _val, _stated in [
    ("graph-comparator-naive-rag", "`naive rag` (16 entries)",
     _comparator("naive rag"), 16),
    ("graph-comparator-titans", "`titans`\n  (21 entries)",
     _comparator("titans"), 21),
    ("graph-comparator-threshold", "are mentioned in five or more entries",
     lambda d: d["graph_shape"]["comparator_coverage"]["threshold_entries"],
     5),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(GRAPH_ABLATION,),
        value=_val, stated=_stated, places=0))

# -- recall vs search -----------------------------------------------------
# The run size first: at n=8 / n=4 the hit rows are a ceiling, which is the
# caveat the prose leans on, so both the n and the hit rates are pinned.
_N_NEEDLE = ("8 of the 30 relational questions and 4 logged queries — the "
             "run size")
_CHARS_NEEDLE = "| mean served chars | 6932 | 184641 | 6649 | 74186 |"
_HIT_NEEDLE = "| expected entity/entry found | 8/8 | 8/8 | 4/4 | 4/4 |"
_ONLY_NEEDLE = "| recall-only hits | — | 0 | — | 0 |"
_LOG_RATIO_NEEDLE = "(11× / 114× on the logged set)"
for _cid, _needle, _path, _stated, _places in [
    ("ablation-n-relational", _N_NEEDLE, _REL + ("n",), 8, 0),
    ("ablation-n-logged", _N_NEEDLE, _LOG + ("n",), 4, 0),
    ("ablation-chars-rel-search", _CHARS_NEEDLE,
     _REL + ("search", "mean_served_chars"), 6932, 0),
    ("ablation-chars-rel-recall", _CHARS_NEEDLE,
     _REL + ("recall", "mean_served_chars"), 184641, 0),
    ("ablation-chars-log-search", _CHARS_NEEDLE,
     _LOG + ("search", "mean_served_chars"), 6649, 0),
    ("ablation-chars-log-recall", _CHARS_NEEDLE,
     _LOG + ("recall", "mean_served_chars"), 74186, 0),
    ("ablation-wall-rel-search", _WALL_NEEDLE,
     _REL + ("search", "mean_wall_s"), 0.44, 2),
    ("ablation-wall-log-search", _WALL_NEEDLE,
     _LOG + ("search", "mean_wall_s"), 0.39, 2),
    ("ablation-hit-rel-search", _HIT_NEEDLE,
     _REL + ("search", "expected_hit_rate"), 1.0, 2),
    ("ablation-hit-rel-recall", _HIT_NEEDLE,
     _REL + ("recall", "expected_hit_rate"), 1.0, 2),
    ("ablation-hit-log-search", _HIT_NEEDLE,
     _LOG + ("search", "expected_hit_rate"), 1.0, 2),
    ("ablation-hit-log-recall", _HIT_NEEDLE,
     _LOG + ("recall", "expected_hit_rate"), 1.0, 2),
    ("ablation-recall-only-rel", _ONLY_NEEDLE,
     _REL + ("recall_only_hits",), 0, 0),
    ("ablation-recall-only-log", _ONLY_NEEDLE,
     _LOG + ("recall_only_hits",), 0, 0),
    ("graph-recall-chars-ratio-logged", _LOG_RATIO_NEEDLE,
     _LOG + ("chars_ratio_recall_over_search",), 11, 0),
    ("graph-recall-time-ratio-logged", _LOG_RATIO_NEEDLE,
     _LOG + ("time_ratio_recall_over_search",), 114, 0),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=EVALS, needle=_needle, artifacts=(GRAPH_ABLATION,),
        value=(lambda p: lambda d: _dig(d, p))(_path),
        stated=_stated, places=_places))


# ── the agent-side token ledger (2026-09-04) ──────────────────────────────
# evals/README.md's "Agent-side token ledger" section publishes what an MCP
# client pays per call, before and after the payload cuts. Unlike the
# accuracy tables above this needs no GPU — it is pure counting — so every
# cell is pinnable, and every cell is pinned.
# Promoted 2026-09-04 to r3 after the pre-merge review, twice over. r1
# measured the lean fact_get projection while it still dropped
# source_entries, and picked its widest slots from a 2,000-row prefix of
# the fact dump. r2 fixed both and then measured `superseded_by_text`
# truncated — a field with no recovery path, whose truncation was
# REMOVED before merge, so r2's headline priced a payload this repo does
# not ship. r3 prices the shipped one. r1 and r2 stay committed as
# pre-review records and are deliberately cited by nothing.
LEDGER = RESULTS + "agent-token-ledger-20260904-r3.json"


def _ledger_manifest(tier: str, key: str):
    return lambda d: d["manifest"][tier][key]


def _ledger_search(scope: str, arm: str, part: str, stat: str = "mean"):
    return lambda d: d[scope]["aggregate"][arm][part][stat]


_LEDGER_MANIFEST_ROWS = [
    ("minimal", "| tool manifest, `minimal` tier (9 tools) | 7,015 | 1,753 |",
     7015, 1753),
    ("core", "| tool manifest, `core` tier (22 tools) | 14,076 | 3,519 |",
     14076, 3519),
    ("full", "| tool manifest, `full` tier (35 tools) | 22,719 | 5,679 |",
     22719, 5679),
]
for _tier, _needle, _chars, _toks in _LEDGER_MANIFEST_ROWS:
    CLAIMS.append(Claim(
        id=f"ledger-manifest-{_tier}-chars", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,), value=_ledger_manifest(_tier, "chars"),
        stated=_chars, places=0))
    CLAIMS.append(Claim(
        id=f"ledger-manifest-{_tier}-tokens", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,), value=_ledger_manifest(_tier, "approx_tokens"),
        stated=_toks, places=0))

# RAW chars, not the JSON encoding the other cells count: the hook writes
# this block into the session as plain text (2026-09-04 review finding —
# the README published the 7,644 JSON size for a non-JSON surface).
_LEDGER_SESSION = ("| served session-start block (`MEMORY_LOOP_BLOCK`) "
                   "| 7,492 | 1,873 |")
CLAIMS.append(Claim(
    id="ledger-session-block-chars", doc=EVALS, needle=_LEDGER_SESSION,
    artifacts=(LEDGER,), value=lambda d: d["session_start_block"]["raw_chars"],
    stated=7492, places=0))
CLAIMS.append(Claim(
    id="ledger-session-block-tokens", doc=EVALS, needle=_LEDGER_SESSION,
    artifacts=(LEDGER,),
    value=lambda d: d["session_start_block"]["raw_approx_tokens"],
    stated=1873, places=0))
CLAIMS.append(Claim(
    id="ledger-manifest-full-split", doc=EVALS,
    needle="(full tier: 14,523 + 8,196)", artifacts=(LEDGER,),
    value=_ledger_manifest("full", "description_chars"),
    stated=14523, places=0))
CLAIMS.append(Claim(
    id="ledger-manifest-full-params", doc=EVALS,
    needle="(full tier: 14,523 + 8,196)", artifacts=(LEDGER,),
    value=_ledger_manifest("full", "param_description_chars"),
    stated=8196, places=0))

# The two before/after tables. `search` is the tool default (top_k=8);
# `search_narrow` is top_k=3, the only place cut (b) can show.
_LEDGER_SEARCH_ROWS = [
    ("total", "total_chars", "| **total** | **14,745** | **9,951** | **−33%** |",
     14745, 9951),
    ("entries", "entries_chars", "| entries block | 12,637 | 7,842 | −38% |",
     12637, 7842),
    ("text", "entries_text_chars",
     "| — entry `text` | 9,464 | 4,550 | −52% |", 9464, 4550),
    # The exempted field, published as its own row (2026-09-04 review
    # finding): it is a sixth of the "before" payload, and leaving it
    # inside "entries block" left ~2,400 chars unaccounted for between
    # the block total and text + metadata. Identical in both arms
    # BECAUSE it is exempt — that identity is the pin on the exemption.
    ("superseded", "entries_superseded_text_chars",
     "| — `superseded_by_text` | 2,406 | 2,406 | — |", 2406, 2406),
    ("meta", "entries_other_chars",
     "| — entry metadata | 767 | 887 | +16% |", 767, 887),
    ("cortex", "cortex_chars", "| cortex block | 1,853 | 1,853 | — |",
     1853, 1853),
    ("tokens", "total_approx_tokens",
     "| approx tokens | 3,686 | 2,487 | −33% |", 3686, 2487),
]
for _slug, _part, _needle, _before, _after in _LEDGER_SEARCH_ROWS:
    CLAIMS.append(Claim(
        id=f"ledger-search-{_slug}-before", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,), value=_ledger_search("search", "before", _part),
        stated=_before, places=0))
    CLAIMS.append(Claim(
        id=f"ledger-search-{_slug}-after", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,), value=_ledger_search("search", "after", _part),
        stated=_after, places=0))

_LEDGER_NARROW_ROWS = [
    ("total", "total_chars", "| **total** | **6,870** | **4,290** | **−38%** |",
     6870, 4290),
    ("text", "entries_text_chars",
     "| entry `text` | 3,537 | 1,712 | −52% |", 3537, 1712),
    ("superseded", "entries_superseded_text_chars",
     "| `superseded_by_text` | 931 | 931 | — |", 931, 931),
    ("cortex", "cortex_chars",
     "| cortex block (5 facts → 3) | 1,853 | 1,107 | −40% |", 1853, 1107),
]
for _slug, _part, _needle, _before, _after in _LEDGER_NARROW_ROWS:
    CLAIMS.append(Claim(
        id=f"ledger-narrow-{_slug}-before", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,),
        value=_ledger_search("search_narrow", "before", _part),
        stated=_before, places=0))
    CLAIMS.append(Claim(
        id=f"ledger-narrow-{_slug}-after", doc=EVALS, needle=_needle,
        artifacts=(LEDGER,),
        value=_ledger_search("search_narrow", "after", _part),
        stated=_after, places=0))

_LEDGER_MEDIANS = "Median total 15,325 → 9,613; p90 18,886 → 12,583."
for _cid, _arm, _stat, _stated in [
    ("median-before", "before", "median", 15325),
    ("median-after", "after", "median", 9613),
    ("p90-before", "before", "p90", 18886),
    ("p90-after", "after", "p90", 12583),
]:
    CLAIMS.append(Claim(
        id=f"ledger-search-{_cid}", doc=EVALS, needle=_LEDGER_MEDIANS,
        artifacts=(LEDGER,),
        value=_ledger_search("search", _arm, "total_chars", _stat),
        stated=_stated, places=0))

_LEDGER_FACT = ("`memory_fact_get`, over the five widest current slots in "
                "the bank: **2,175 →\n1,296 chars** mean (median 2,281 → 1,128)")
for _cid, _arm, _stat, _stated in [
    ("mean-before", "before", "mean", 2175),
    ("mean-after", "after", "mean", 1296),
    ("median-before", "before", "median", 2281),
    ("median-after", "after", "median", 1128),
]:
    CLAIMS.append(Claim(
        id=f"ledger-factget-{_cid}", doc=EVALS, needle=_LEDGER_FACT,
        artifacts=(LEDGER,),
        value=(lambda a, s: lambda d: d["fact_get"]["aggregate"][a][s])(
            _arm, _stat),
        stated=_stated, places=0))

# The cap's justification — the reason 600 is 600 rather than a round guess.
_LEDGER_CAP = ("Served entry `text` runs mean **1,180** chars, median 1,149, "
               "p90 1,794 over\nthe 120 entries the 15 queries returned. A "
               "600-char cap therefore clips 88%")
# The cap the run priced, read from ``McpConfig`` rather than restated in
# the harness (2026-09-04 review finding), so a default change re-prices
# the artifact instead of desynchronising it from this page.
CLAIMS.append(Claim(
    id="ledger-entry-text-cap", doc=EVALS, needle=_LEDGER_CAP,
    artifacts=(LEDGER,),
    value=lambda d: d["search"]["entry_text"]["entry_text_chars"],
    stated=600, places=0))
for _cid, _get, _stated in [
    ("mean", lambda d: d["search"]["entry_text"]["raw_chars"]["mean"], 1180),
    ("median", lambda d: d["search"]["entry_text"]["raw_chars"]["median"], 1149),
    ("p90", lambda d: d["search"]["entry_text"]["raw_chars"]["p90"], 1794),
    ("n", lambda d: d["search"]["entry_text"]["entries"], 120),
]:
    CLAIMS.append(Claim(
        id=f"ledger-entry-text-{_cid}", doc=EVALS, needle=_LEDGER_CAP,
        artifacts=(LEDGER,), value=_get, stated=_stated, places=0))
CLAIMS.append(Claim(
    id="ledger-entry-text-share-over-600", doc=EVALS, needle=_LEDGER_CAP,
    artifacts=(LEDGER,),
    value=lambda d: d["search"]["entry_text"]["share_over_cap"],
    stated=0.883, places=2))

# memory_recall's call amplification — the finding the ledger surfaced and
# deliberately did NOT fix.
_LEDGER_RECALL = ("A 3-hop `memory_recall` issues **35 `service.search` "
                  "calls on average** and\nup to **66** on a single question")
CLAIMS.append(Claim(
    id="ledger-recall-mean-searches", doc=EVALS, needle=_LEDGER_RECALL,
    artifacts=(LEDGER,),
    value=lambda d: d["recall"]["aggregate"]["service_search_calls"]["mean"],
    stated=35, places=0))
CLAIMS.append(Claim(
    id="ledger-recall-max-searches", doc=EVALS, needle=_LEDGER_RECALL,
    artifacts=(LEDGER,),
    value=lambda d: d["recall"]["aggregate"]["service_search_calls"]["max"],
    stated=66, places=0))
_LEDGER_RECALL_SIZE = ("4,243 chars mean against 10,349 for the same walk "
                       "with `verbose=True`")
CLAIMS.append(Claim(
    id="ledger-recall-compact-chars", doc=EVALS, needle=_LEDGER_RECALL_SIZE,
    artifacts=(LEDGER,),
    value=lambda d: d["recall"]["aggregate"]["chars"]["mean"],
    stated=4243, places=0))
CLAIMS.append(Claim(
    id="ledger-recall-verbose-chars", doc=EVALS, needle=_LEDGER_RECALL_SIZE,
    artifacts=(LEDGER,),
    value=lambda d: sum(r["verbose_chars"] for r in
                        d["recall"]["per_question"]) / len(
                            d["recall"]["per_question"]),
    stated=10349, places=0))
CLAIMS.append(Claim(
    id="ledger-bank-entries", doc=EVALS,
    needle="bank, 1,316 entries, `preset: flat`", artifacts=(LEDGER,),
    value=lambda d: d["bank"]["entries"], stated=1316, places=0))

# The narrow arm's validity condition (2026-09-04 review finding): the
# cortex slice only equals a real top_k=3 call while _pin_constraint_facts
# is a no-op, which holds exactly while no current fact carries a
# distortion_tolerance label. It shipped as a hand-checked sentence with
# no artifact field behind it; the run counts it now.
_LEDGER_LABELS = "carries **0 of 5,509** labelled current facts"
CLAIMS.append(Claim(
    id="ledger-facts-labelled", doc=EVALS, needle=_LEDGER_LABELS,
    artifacts=(LEDGER,), value=lambda d: d["bank"]["facts_labelled"],
    stated=0, places=0))
CLAIMS.append(Claim(
    id="ledger-facts-current", doc=EVALS, needle=_LEDGER_LABELS,
    artifacts=(LEDGER,), value=lambda d: d["bank"]["facts_current"],
    stated=5509, places=0))
# ── token-matched rag arms (2026-09-04, feat/rag-lite-arms) ──────────────
# Three runs, published in the CHANGELOG, evals/README.md and the runbook.
# Every arm mean and every context-token mean below is read straight out of
# the run's own summary; the paired deltas come from the within-run pairing
# artifact, which is itself regenerated byte-exactly by
# tests/test_beam_within_run_pairs.py.
RUNBOOK_RL = "docs/runbooks/raglite-runs-20260904.md"
RL_V38_SUM = (RESULTS +
              "longmemeval-ku-oracle-qwen-27b-raglite-v38.summary.json")
RL_ALL_SUM = (RESULTS + "longmemeval-all-oracle-qwen-27b-"
                        "raglite-all-fresh.summary.json")
RL_SMOKE_SUM = RESULTS + "beam-100K-qwen-27b-raglite-smoke.summary.json"
RL_ALL_PAIRS = (RESULTS + "longmemeval-all-oracle-qwen-27b-"
                          "raglite-all-fresh.arms-vs-rag.json")
BEAM_CHIP12_PAIRS = (RESULTS +
                     "beam-100K-qwen-27b-chip12-b16.arms-vs-rag.json")


def _arm_metric(arm: str, key: str):
    return lambda d: d["arms"][arm][key]


# The BEAM token costs the runbook sizes its budget off, and the CHANGELOG
# quotes. Read from the pairing artifact, which is where the control's own
# cost lives (the control is not an entry under "arms").
_CHIP12_NEEDLE_RUNBOOK = ("rag serves **5,539** tokens/question, hybrid "
                          "6,099, cortex **551**")
for _cid, _val, _stated in [
    ("raglite-chip12-rag-tokens",
     lambda d: d["control_context_tokens_mean"], 5539),
    ("raglite-chip12-hybrid-tokens",
     _arm_metric("hybrid", "context_tokens_mean"), 6099),
    ("raglite-chip12-cortex-tokens",
     _arm_metric("cortex", "context_tokens_mean"), 551),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=RUNBOOK_RL, needle=_CHIP12_NEEDLE_RUNBOOK,
        artifacts=(BEAM_CHIP12_PAIRS,), value=_val, stated=_stated, places=0))

_CHIP12_NEEDLE_CL = "rag control served 5,539 tokens/question against the cortex arm's 551"
CLAIMS.append(Claim(
    id="raglite-chip12-rag-tokens-changelog", doc=CHANGELOG,
    needle=_CHIP12_NEEDLE_CL, artifacts=(BEAM_CHIP12_PAIRS,),
    value=lambda d: d["control_context_tokens_mean"], stated=5539, places=0))
CLAIMS.append(Claim(
    id="raglite-chip12-cortex-tokens-changelog", doc=CHANGELOG,
    needle=_CHIP12_NEEDLE_CL, artifacts=(BEAM_CHIP12_PAIRS,),
    value=_arm_metric("cortex", "context_tokens_mean"), stated=551, places=0))

# Run A — LongMemEval KU oracle, 78 questions, rebuilt onto ceiling-v38.
# Needle is the runbook's table row, which carries accuracy AND tokens.
for _arm, _needle, _acc, _tok in [
    ("rag", "| `rag` (control, 6 turns) | 0.859 | 1184.1 | |", 0.859, 1184.1),
    ("hybrid", "| `hybrid` | 0.846 | 731.3 | |", 0.846, 731.3),
    ("cascade", "| `cascade` (derived) | 0.846 | 389.4 | |", 0.846, 389.4),
    ("rag2", "| `rag2` | 0.551 | 429.7 | |", 0.551, 429.7),
    ("ragb400", "| `ragb400` | 0.500 | 309.0 | 17 of 78 rows over budget |",
     0.500, 309.0),
    ("ragb100",
     "| `ragb100` | 0.333 | 219.2 | 36 of 78 over budget; = `rag1` on 74 of 78 |",
     0.333, 219.2),
    ("rag1", "| `rag1` | 0.321 | 217.1 | |", 0.321, 217.1),
    ("cortex", "| `cortex` | 0.667 | 96.7 | |", 0.667, 96.7),
]:
    CLAIMS.append(Claim(
        id=f"raglite-v38-{_arm}-accuracy", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_V38_SUM,), value=_arm_metric(_arm, "accuracy"),
        stated=_acc, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-v38-{_arm}-tokens", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_V38_SUM,), value=_arm_metric(_arm, "context_tokens"),
        stated=_tok, places=1))

# Run B — BEAM 100K, 2 chats. BEAM reports `score`, not `accuracy`.
for _arm, _needle, _score, _tok in [
    ("hybrid", "| `hybrid` | 0.5629 | 3635 | |", 0.5629, 3635),
    ("rag", "| `rag` (control) | 0.4462 | 3158 | |", 0.4462, 3158),
    ("rag2", "| `rag2` | 0.3396 | 1188 | |", 0.3396, 1188),
    ("cortex", "| `cortex` | 0.2956 | 468 | |", 0.2956, 468),
    ("rag1", "| `rag1` | 0.2750 | 496 | |", 0.2750, 496),
    ("ragb600", "| `ragb600` | 0.2600 | 584 | 12 of 40 rows over budget |",
     0.2600, 584),
]:
    CLAIMS.append(Claim(
        id=f"raglite-smoke-{_arm}-score", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_SMOKE_SUM,), value=_arm_metric(_arm, "score"),
        stated=_score, places=4))
    CLAIMS.append(Claim(
        id=f"raglite-smoke-{_arm}-tokens", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_SMOKE_SUM,), value=_arm_metric(_arm, "context_tokens"),
        stated=_tok, places=0))

# Run C — the whole benchmark, fresh extraction, 500 questions, six types.
for _arm, _needle, _acc, _tok in [
    ("hybrid", "| `hybrid` | 0.730 | 1229.3 | |", 0.730, 1229.3),
    ("cascade", "| `cascade` (derived) | 0.692 | 843.7 | |", 0.692, 843.7),
    ("rag", "| `rag` (control, 6 turns) | 0.690 | 1124.2 | |", 0.690, 1124.2),
    ("ragb400", "| `ragb400` | 0.460 | 312.3 | 98 of 500 rows over budget |",
     0.460, 312.3),
    ("rag2", "| `rag2` | 0.458 | 432.5 | |", 0.458, 432.5),
    ("rag1", "| `rag1` | 0.316 | 206.3 | |", 0.316, 206.3),
    ("cortex", "| `cortex` | 0.310 | 96.5 | |", 0.310, 96.5),
]:
    CLAIMS.append(Claim(
        id=f"raglite-all-{_arm}-accuracy", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_SUM,), value=_arm_metric(_arm, "accuracy"),
        stated=_acc, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-all-{_arm}-tokens", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_SUM,), value=_arm_metric(_arm, "context_tokens"),
        stated=_tok, places=1))

# Run C's paired column — a delta, a CI and a permutation p each need the
# artifact that computed them; an aggregate of means cannot justify a p.
for _arm, _needle, _delta, _ci, _p, _p_places, _w, _l in [
    ("hybrid", "| `hybrid` | **+0.040** | 0.031 | 0.015 | 41 / 21 |",
     0.040, 0.031, 0.015, 3, 41, 21),
    ("cascade", "| `cascade` | +0.002 | 0.022 | 1.00 | 16 / 15 |",
     0.002, 0.022, 1.00, 2, 16, 15),
    ("ragb400", "| `ragb400` | \u2212" "0.230 | 0.041 | 0.0001 | 9 / 124 |",
     -0.230, 0.041, 0.0001, 4, 9, 124),
    ("rag2", "| `rag2` | \u2212" "0.232 | 0.042 | 0.0001 | 13 / 129 |",
     -0.232, 0.042, 0.0001, 4, 13, 129),
    ("rag1", "| `rag1` | \u2212" "0.374 | 0.045 | 0.0001 | 8 / 195 |",
     -0.374, 0.045, 0.0001, 4, 8, 195),
    ("cortex", "| `cortex` | \u2212" "0.380 | 0.048 | 0.0001 | 16 / 206 |",
     -0.380, 0.048, 0.0001, 4, 16, 206),
]:
    CLAIMS.append(Claim(
        id=f"raglite-all-paired-{_arm}-delta", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_PAIRS,), value=_arm_metric(_arm, "delta_vs_control"),
        stated=_delta, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-all-paired-{_arm}-ci", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_PAIRS,), value=_arm_metric(_arm, "ci95_halfwidth"),
        stated=_ci, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-all-paired-{_arm}-p", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_PAIRS,), value=_arm_metric(_arm, "perm_p"),
        stated=_p, places=_p_places))
    CLAIMS.append(Claim(
        id=f"raglite-all-paired-{_arm}-wins", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_PAIRS,), value=_arm_metric(_arm, "wins"),
        stated=_w, places=0))
    CLAIMS.append(Claim(
        id=f"raglite-all-paired-{_arm}-losses", doc=RUNBOOK_RL, needle=_needle,
        artifacts=(RL_ALL_PAIRS,), value=_arm_metric(_arm, "losses"),
        stated=_l, places=0))

# The headline of the whole exercise: the fact spine at ~97 tokens against
# one-turn RAG at ~206, paired arm-vs-arm rather than each against the rag
# control. Stated in the runbook, evals/README.md and the CHANGELOG.
def _pair_cr(key):
    return lambda d: d["pairs"]["cortex-rag1"][key]


for _doc, _needle in (
    (RUNBOOK_RL, "**`cortex` \u2212 `rag1` = \u2212"
                 "0.006 \u00b1 0.049, p 0.87 (77 W / 80 L / 343 ties).**"),
    (EVALS, "**cortex \u2212 rag1 = \u2212" "0.006 \u00b1 0.049, p 0.87**"),
    (CHANGELOG, "**\u2212" "0.006 \u00b1 0.049, p 0.87**"),
):
    CLAIMS.append(Claim(
        id=f"raglite-cortex-vs-rag1-delta-{_doc[-12:]}", doc=_doc,
        needle=_needle, artifacts=(RL_ALL_PAIRS,), value=_pair_cr("delta"),
        stated=-0.006, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-cortex-vs-rag1-ci-{_doc[-12:]}", doc=_doc,
        needle=_needle, artifacts=(RL_ALL_PAIRS,),
        value=_pair_cr("ci95_halfwidth"), stated=0.049, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-cortex-vs-rag1-p-{_doc[-12:]}", doc=_doc,
        needle=_needle, artifacts=(RL_ALL_PAIRS,), value=_pair_cr("perm_p"),
        stated=0.87, places=2))

# The overshoot the runbook and README correct the plan with: the budget arm
# served 2.2x its name and was rag1 on 74 of 78 rows. Counted off the rows,
# because that is the only place it is recorded per question.
RL_V38_ROWS = RESULTS + "longmemeval-ku-oracle-qwen-27b-raglite-v38.jsonl"
RL_ALL_ROWS = (RESULTS +
               "longmemeval-all-oracle-qwen-27b-raglite-all-fresh.jsonl")
RL_SMOKE_ROWS = RESULTS + "beam-100K-qwen-27b-raglite-smoke.jsonl"


def _over(arm: str, budget: int):
    return lambda rows: sum(1 for r in rows
                            if r[f"{arm}_context_tokens"] > budget)


def _same_context(a: str, b: str):
    return lambda rows: sum(1 for r in rows
                            if r["contexts"][a] == r["contexts"][b])


_OVERSHOOT_NEEDLE = ("100-token budget, 36 of 78 rows exceeded it (mean "
                     "388.5 tokens on those\n  rows); at 400, 98 of 500 did")
for _cid, _art, _val, _stated in [
    ("raglite-overshoot-v38-b100", RL_V38_ROWS, _over("ragb100", 100), 36),
    ("raglite-overshoot-all-b400", RL_ALL_ROWS, _over("ragb400", 400), 98),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=RUNBOOK_RL, needle=_OVERSHOOT_NEEDLE, artifacts=(_art,),
        value=_val, stated=_stated, places=0))
CLAIMS.append(Claim(
    id="raglite-overshoot-v38-b400", doc=RUNBOOK_RL,
    needle="| `ragb400` | 0.500 | 309.0 | 17 of 78 rows over budget |",
    artifacts=(RL_V38_ROWS,), value=_over("ragb400", 400), stated=17,
    places=0))
CLAIMS.append(Claim(
    id="raglite-overshoot-smoke-b600", doc=RUNBOOK_RL,
    needle="| `ragb600` | 0.2600 | 584 | 12 of 40 rows over budget |",
    artifacts=(RL_SMOKE_ROWS,), value=_over("ragb600", 600), stated=12,
    places=0))
for _cid, _doc, _needle in [
    ("raglite-b100-is-rag1-runbook", RUNBOOK_RL,
     "byte-identical to `rag1` on 74\nof the 78 rows"),
    ("raglite-b100-is-rag1-evals", EVALS,
     "byte-identical context to `rag1` on 74 of them"),
    ("raglite-b100-is-rag1-changelog", CHANGELOG,
     "context to `rag1` on 74 of 78 rows"),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(RL_V38_ROWS,),
        value=_same_context("ragb100", "rag1"), stated=74, places=0))

# The split-recovery figure both the README and the runbook quote for why
# rag_lite_rebuild.py re-ingests instead of slicing the persisted block.
CEILING_V38_ROWS = RESULTS + "longmemeval-ku-oracle-qwen-27b-ceiling-v38.jsonl"
for _cid, _doc, _needle in [
    ("raglite-split-recovery-evals", EVALS,
     "back into turns recovers it for only 6 of the 78 `ceiling-v38` rows"),
    ("raglite-split-recovery-runbook", RUNBOOK_RL,
     "contain blank lines, so only **6 of the 78** rows split into the 6 turns"),
]:
    CLAIMS.append(Claim(
        id=_cid, doc=_doc, needle=_needle, artifacts=(CEILING_V38_ROWS,),
        value=lambda rows: sum(
            1 for r in rows if len(r["contexts"]["rag"].split("\n\n")) == 6),
        stated=6, places=0))


# The same three runs restated in the CHANGELOG and evals/README.md. A
# needle in the runbook does not guard a second copy of the number on
# another page, and the retire-at-the-old-site rule exists because exactly
# that went wrong once already.
_CL_V38 = [
    ("rag", "rag 0.859 @ 1184.1 tokens", 0.859, 1184.1),
    ("hybrid", "hybrid 0.846 @ 731.3", 0.846, 731.3),
    ("cascade", "cascade 0.846 @ 389.4", 0.846, 389.4),
    ("cortex", "cortex 0.667 @ 96.7", 0.667, 96.7),
    ("rag2", "rag2 0.551 @ 429.7", 0.551, 429.7),
    ("ragb400", "ragb400 0.500 @ 309.0", 0.500, 309.0),
    ("ragb100", "ragb100 0.333 @ 219.2", 0.333, 219.2),
    ("rag1", "rag1 0.321 @ 217.1", 0.321, 217.1),
]
_CL_ALL = [
    ("hybrid", "hybrid 0.730 @ 1229.3 tokens", 0.730, 1229.3),
    ("cascade", "cascade 0.692 @", 0.692, None),
    ("rag", "rag 0.690 @ 1124.2", 0.690, 1124.2),
    ("ragb400", "ragb400 0.460 @ 312.3", 0.460, 312.3),
    ("rag2", "rag2 0.458 @ 432.5", 0.458, 432.5),
    ("rag1", "rag1 0.316 @ 206.3", 0.316, 206.3),
    ("cortex", "cortex 0.310 @ 96.5", 0.310, 96.5),
]
for _slug, _art, _rows in (("v38", RL_V38_SUM, _CL_V38),
                           ("all", RL_ALL_SUM, _CL_ALL)):
    for _arm, _needle, _acc, _tok in _rows:
        CLAIMS.append(Claim(
            id=f"raglite-cl-{_slug}-{_arm}-accuracy", doc=CHANGELOG,
            needle=_needle, artifacts=(_art,),
            value=_arm_metric(_arm, "accuracy"), stated=_acc, places=3))
        if _tok is not None:
            CLAIMS.append(Claim(
                id=f"raglite-cl-{_slug}-{_arm}-tokens", doc=CHANGELOG,
                needle=_needle, artifacts=(_art,),
                value=_arm_metric(_arm, "context_tokens"), stated=_tok,
                places=1))

for _arm, _needle, _score, _tok in [
    ("hybrid", "hybrid 0.5629 @ 3635 tokens", 0.5629, 3635),
    ("rag", "rag 0.4462 @ 3158", 0.4462, 3158),
    ("rag2", "rag2 0.3396 @ 1188", 0.3396, 1188),
    ("cortex", "cortex 0.2956 @ 468", 0.2956, 468),
    ("rag1", "rag1 0.2750 @", 0.2750, None),
    ("ragb600", "ragb600 0.2600 @ 584", 0.2600, 584),
]:
    CLAIMS.append(Claim(
        id=f"raglite-cl-smoke-{_arm}-score", doc=CHANGELOG, needle=_needle,
        artifacts=(RL_SMOKE_SUM,), value=_arm_metric(_arm, "score"),
        stated=_score, places=4))
    if _tok is not None:
        CLAIMS.append(Claim(
            id=f"raglite-cl-smoke-{_arm}-tokens", doc=CHANGELOG,
            needle=_needle, artifacts=(RL_SMOKE_SUM,),
            value=_arm_metric(_arm, "context_tokens"), stated=_tok, places=0))

# The CHANGELOG's paired line, and evals/README.md's copy of the same six
# deltas. Both wrap, so each needle is one whole line of its page.
_CL_PAIRED_A = "over all 500 rows (10k sign-flip permutations, seed 0): hybrid **+0.040**"
_CL_PAIRED_B = "\u00b1 0.031 (p 0.015, 41W/21L), cascade +0.002 \u00b1 0.022, ragb400 \u2212" "0.230 \u00b1"
_CL_PAIRED_C = "0.041, rag2 \u2212" "0.232 \u00b1 0.042, rag1 \u2212" "0.374 \u00b1 0.045, cortex \u2212" "0.380 \u00b1 0.048."
_EV_PAIRED_A = "rows, hybrid is **+0.040 \u00b1 0.031 (p 0.015, 41 W / 21 L)** and cascade"
_EV_PAIRED_B = "(ragb400 \u2212" "0.230 \u00b1 0.041, rag2 \u2212" "0.232 \u00b1 0.042, rag1 \u2212" "0.374 \u00b1 0.045, cortex"
for _doc, _hyb, _casc, _rest in (
    (CHANGELOG, _CL_PAIRED_A, _CL_PAIRED_B, _CL_PAIRED_C),
    (EVALS, _EV_PAIRED_A, _EV_PAIRED_A, _EV_PAIRED_B),
):
    _tag = "cl" if _doc == CHANGELOG else "ev"
    CLAIMS.append(Claim(
        id=f"raglite-{_tag}-paired-hybrid-delta", doc=_doc, needle=_hyb,
        artifacts=(RL_ALL_PAIRS,),
        value=_arm_metric("hybrid", "delta_vs_control"), stated=0.040,
        places=3))
    CLAIMS.append(Claim(
        id=f"raglite-{_tag}-paired-cascade-delta", doc=_doc, needle=_casc,
        artifacts=(RL_ALL_PAIRS,),
        value=_arm_metric("cascade", "delta_vs_control"), stated=0.002,
        places=3))
    for _arm, _delta, _ci in (("ragb400", -0.230, 0.041),
                              ("rag2", -0.232, 0.042),
                              ("rag1", -0.374, 0.045),
                              ("cortex", -0.380, 0.048)):
        CLAIMS.append(Claim(
            id=f"raglite-{_tag}-paired-{_arm}-delta", doc=_doc, needle=_rest,
            artifacts=(RL_ALL_PAIRS,),
            value=_arm_metric(_arm, "delta_vs_control"), stated=_delta,
            places=3))
        CLAIMS.append(Claim(
            id=f"raglite-{_tag}-paired-{_arm}-ci", doc=_doc, needle=_rest,
            artifacts=(RL_ALL_PAIRS,),
            value=_arm_metric(_arm, "ci95_halfwidth"), stated=_ci, places=3))

# evals/README.md's own arm-mean sentence and its budget-arm paragraph.
_EV_MEANS_A = "\u2212" "0.380 \u00b1 0.048). Arm means and costs on that run: hybrid 0.730 @ 1229.3"
_EV_MEANS_B = "rag2 0.458 @ 432.5, rag1 0.316 @ 206.3, cortex 0.310 @ 96.5."
for _arm, _needle, _acc, _tok in [
    ("hybrid", _EV_MEANS_A, 0.730, 1229.3),
    ("rag2", _EV_MEANS_B, 0.458, 432.5),
    ("rag1", _EV_MEANS_B, 0.316, 206.3),
    ("cortex", _EV_MEANS_B, 0.310, 96.5),
]:
    CLAIMS.append(Claim(
        id=f"raglite-ev-all-{_arm}-accuracy", doc=EVALS, needle=_needle,
        artifacts=(RL_ALL_SUM,), value=_arm_metric(_arm, "accuracy"),
        stated=_acc, places=3))
    CLAIMS.append(Claim(
        id=f"raglite-ev-all-{_arm}-tokens", doc=EVALS, needle=_needle,
        artifacts=(RL_ALL_SUM,), value=_arm_metric(_arm, "context_tokens"),
        stated=_tok, places=1))

_EV_BUDGET = "**219.2** tokens, overshot on 36 of the 78 `raglite-v38` rows, and produced a"
CLAIMS.append(Claim(
    id="raglite-ev-b100-tokens", doc=EVALS, needle=_EV_BUDGET,
    artifacts=(RL_V38_SUM,), value=_arm_metric("ragb100", "context_tokens"),
    stated=219.2, places=1))
CLAIMS.append(Claim(
    id="raglite-ev-b100-overshoot", doc=EVALS, needle=_EV_BUDGET,
    artifacts=(RL_V38_ROWS,), value=_over("ragb100", 100), stated=36,
    places=0))
# evals/README.md's leak-free sentence beside the arm means: the headline
# figures span all 500 rows, and the summary's own leak_check block is
# where the 475-row reads live. Pinned so the two can never drift apart.
_EV_LEAKFREE = "unleaked rows, **rag 0.6947, hybrid 0.7326, cortex 0.3158**."
for _arm, _stated in (("rag", 0.6947), ("hybrid", 0.7326),
                      ("cortex", 0.3158)):
    CLAIMS.append(Claim(
        id=f"raglite-ev-all-{_arm}-leak-free", doc=EVALS,
        needle=_EV_LEAKFREE, artifacts=(RL_ALL_SUM,),
        value=(lambda a: lambda d: d["leak_check"]["arms"][a]["leak_free"])(
            _arm),
        stated=_stated, places=4))
CLAIMS.append(Claim(
    id="raglite-ev-all-leak-free-n", doc=EVALS,
    needle="own `leak_check` block and are not the headline figures: over the 475",
    artifacts=(RL_ALL_SUM,),
    value=lambda d: d["leak_check"]["arms"]["rag"]["n_leak_free"],
    stated=475, places=0))
CLAIMS.append(Claim(
    id="raglite-ev-all-leaked-n", doc=EVALS,
    needle="leak check flags as naming their own gold answer included, so every arm is",
    artifacts=(RL_ALL_SUM,), value=lambda d: d["leak_check"]["n_leaked"],
    stated=25, places=0))

CLAIMS.append(Claim(
    id="raglite-ev-b100-cortex-target", doc=EVALS,
    needle="`ragb100` \u2014 sized to match the cortex arm's 96.7 tokens \u2014 served a mean",
    artifacts=(RL_V38_SUM,), value=_arm_metric("cortex", "context_tokens"),
    stated=96.7, places=1))

_EV_LAND = "Read the arm's measured `context_tokens` and its `budget_overshoot_rows`, never"
_EV_LAND2 = "its name. `ragb400` does land (309.0 served on the 78-question run, 312.3 on"
CLAIMS.append(Claim(
    id="raglite-ev-b400-v38-tokens", doc=EVALS, needle=_EV_LAND2,
    artifacts=(RL_V38_SUM,), value=_arm_metric("ragb400", "context_tokens"),
    stated=309.0, places=1))
CLAIMS.append(Claim(
    id="raglite-ev-b400-all-tokens", doc=EVALS, needle=_EV_LAND2,
    artifacts=(RL_ALL_SUM,), value=_arm_metric("ragb400", "context_tokens"),
    stated=312.3, places=1))
CLAIMS.append(Claim(
    id="raglite-ev-b600-tokens", doc=EVALS,
    needle="budget \u2014 `ragb600` served 584.",
    artifacts=(RL_SMOKE_SUM,), value=_arm_metric("ragb600", "context_tokens"),
    stated=584, places=0))
CLAIMS.append(Claim(
    id="raglite-ev-b100-vs-rag1-accuracy", doc=EVALS,
    needle="byte-identical context to `rag1` on 74 of them (accuracies 0.333 vs 0.321)",
    artifacts=(RL_V38_SUM,), value=_arm_metric("ragb100", "accuracy"),
    stated=0.333, places=3))
CLAIMS.append(Claim(
    id="raglite-ev-rag1-accuracy-v38", doc=EVALS,
    needle="byte-identical context to `rag1` on 74 of them (accuracies 0.333 vs 0.321)",
    artifacts=(RL_V38_SUM,), value=_arm_metric("rag1", "accuracy"),
    stated=0.321, places=3))

# The CHANGELOG's overshoot sentence.
CLAIMS.append(Claim(
    id="raglite-cl-overshoot-v38", doc=CHANGELOG,
    needle="overshot on 36 of 78 rows and `ragb400` on 98 of 500",
    artifacts=(RL_V38_ROWS,), value=_over("ragb100", 100), stated=36,
    places=0))
CLAIMS.append(Claim(
    id="raglite-cl-overshoot-all", doc=CHANGELOG,
    needle="overshot on 36 of 78 rows and `ragb400` on 98 of 500",
    artifacts=(RL_ALL_ROWS,), value=_over("ragb400", 400), stated=98,
    places=0))
CLAIMS.append(Claim(
    id="raglite-cl-b100-tokens", doc=CHANGELOG,
    needle="219.2 tokens against its 100-token name and producing a byte-identical",
    artifacts=(RL_V38_SUM,), value=_arm_metric("ragb100", "context_tokens"),
    stated=219.2, places=1))
