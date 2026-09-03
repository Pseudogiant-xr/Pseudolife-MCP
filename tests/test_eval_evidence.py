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
