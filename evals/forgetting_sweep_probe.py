"""Forgetting sweep — is a swept bank better than a wide one? (2026-09-05)

Preregistered offline analysis; the contract is
``docs/superpowers/specs/2026-09-05-forgetting-sweep-preregistration.md``
— read that first. This module implements it exactly: no arms, metrics or
statistics beyond what the spec states.

The 2026-08-15 distractor-scale probe measured what accumulation costs
(evidence-in-top-6 0.830 at 1x → 0.597 at 15x) and what a *perfect* sweep
could recover, and left its own follow-up open: "no experiment forced
eviction and asked which victims to pick; none asked whether NOT evicting
(a wider bank) beats it."

This probe holds that construction fixed — same 78 knowledge-update
dumps, same fixed rotation, same offline mirror
(``band_ablation.select_topk``, flat policy, recency off, BM25 on) — and
varies only the sweep applied to the pool before selection. Six arms:
``none`` (the control, which must reproduce the 2026-08-15 artifact
exactly), the three shipped retention policies scored through the real
``RetentionPolicy.source_weighted_score``, a seeded ``random`` floor, and
an evidence-preserving ``oracle`` ceiling. Two per-question capacities:
the 1x pool size (``C1``) and the 3x pool size (``C3``).

Usage (repo root, venv python; CPU-only, no GPU/judge/daemon needed):

    python evals/forgetting_sweep_probe.py

Writes ``evals/results/forgetting-sweep-probe-20260905.json`` and refuses
to overwrite it without ``--force``.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from band_ablation import (  # noqa: E402
    _evidence_texts, _paired_permutation_p, select_topk,
)
from distractor_scale_probe import (  # noqa: E402
    SCALES, load_dumps, mean, median,
)

SPEC = "docs/superpowers/specs/2026-09-05-forgetting-sweep-preregistration.md"
CONTROL_ARTIFACT = "evals/results/distractor-scale-probe-2026-08-15.json"
DEFAULT_OUT = (Path(__file__).resolve().parent / "results"
               / "forgetting-sweep-probe-20260905.json")

# Which band-state dumps the control was actually produced from.
# `distractor_scale_probe.DUMP_DIR` names `s-qwen-27b-ablbands-flat`, but on
# a tree carrying both replays that directory holds the RETIRED 384-d MiniLM
# dump (July) and does not reproduce the published 2026-08-15 numbers — 19 of
# 30 checked cells disagree, and no select_topk knob (bm25 / recency / slot
# channel) closes the gap. The v25 replay the artifact came from is 1024-d and
# lives in a sibling directory whose suffix is machine-local, so the dumps are
# identified by backbone dimension instead of by name: with the 1024-d dumps,
# 40 of 40 checked cells reproduce exactly.
V25_EMBEDDING_DIM = 1024
DUMP_DIR_GLOB = "s-qwen-27b-ablbands-flat*"
N_QUESTIONS = 78

POLICY_ARMS = ("balanced", "recency_heavy", "surprise_heavy")
ARMS = ("none",) + POLICY_ARMS + ("random", "oracle")
CAPACITIES = ("C1", "C3")       # per question: the 1x and 3x pool sizes

# ── preregistered gate thresholds ──────────────────────────────────────────
GATE_SCALE = "15x"              # the distractor probe's own primary scale
GATE_CAPACITY = "C1"
ABS_DELTA_BAR = 0.05            # G-F1 / G-F2 "pays" bar, absolute
ALPHA = 0.05
G_F4_MIN_HIT_RATE = 0.5         # inherited G-D3 sanity floor, at 1x

# Fields the control arm must reproduce from the 2026-08-15 artifact.
# Latency is excluded: it is machine- and load-dependent by construction.
CONTROL_FIELDS = ("n_pool_entries", "evidence_in_top6", "evidence_in_top3",
                  "any_evidence_served", "rank_first_evidence")

CAVEATS = [
    "access_count = 0 for every entry (never dumped; bumped live only by the "
    "served-results path, cms.py:1573). Consequence: recency_heavy degenerates "
    "to superseded-first then oldest-inserted-first, and balanced collapses "
    "onto surprise_heavy's ordering.",
    "surprise_score reconstructed as 1 - max cosine to the prior entries of "
    "the entry's OWN dump (1.0 for the first), i.e. MIRASBand.compute_surprise "
    "verbatim. Exact for these dumps: one flat band, flat_cap 5250 vs 396-550 "
    "stored (no eviction during the replay), turns_stored == entry count in all "
    "78 (the novelty gate rejected nothing). Per-dump, not per-pool: the "
    "concatenated "
    "pool never existed live, so per-dump surprise is the only value that was "
    "ever real.",
    "reinforcements = 0 and retention_boost = 0.0 (never dumped). The MTT term "
    "retention_boost * log1p(reinforcements) vanishes at 0 reinforcements "
    "regardless of the boost, so the daemon's retention_boost=1.0 would change "
    "nothing here.",
    "timestamp = the entry's dumped ts; now = the dump's search_time (the "
    "'wall' regime select_topk runs under). Inert given access_count = 0: age "
    "reaches every named policy only as a divisor under access_count.",
    "source = 'bench' on every entry, which is absent from "
    "_default_source_weights and falls back to the 1.0 multiplier. The "
    "per-source retention tiering does no work on this corpus and is untested "
    "by this probe.",
    "Ties evict the earliest-inserted entry, matching _evict_one's "
    "first-minimum rule. Under access_count = 0 this IS recency_heavy's "
    "behaviour, and the pool construction places the anchor's own dump first, "
    "so that arm deletes the anchor's own turns first - a construction "
    "artifact, not a policy verdict.",
    "Corpus property (forgetting-sweep-corpus-props-20260905.json): 247 of 286 "
    "gold-evidence entries (0.8636) carry superseded_at against a 0.7341 base "
    "rate over 38086 entries. source_weighted_score multiplies a superseded "
    "entry by 0.05, below every live entry, so all three shipped policies "
    "delete gold evidence first on this corpus. (The preregistration first "
    "stated 0.629/0.457 - those were read off the retired 384-d replay before "
    "the dump-directory problem was found; the direction is unchanged and the "
    "gap is wider.)",
    "Distractors are foreign haystacks (inherited from 2026-08-15): maximally "
    "off-topic, hence the easiest possible material for a sweep to drop. A "
    "sweep that cannot beat 'none' here will not beat it on realistic "
    "near-duplicate chatter.",
    "Retrieval proxy, not a judged verdict: evidence-in-top-6 is what reaches "
    "the served window, not what an answerer then gets right.",
    "Single embedder/backbone (v25, 1024-d), like every conclusion from this "
    "dump family. Both capacities are aggressive (7% and 20% of the 15x pool); "
    "nothing here speaks to a capacity set just below the accumulated size.",
]


# ──────────────────────────────────────────────────────────────────────────
# Which dumps
# ──────────────────────────────────────────────────────────────────────────

def dump_dir_signature(d: Path) -> dict:
    """What a candidate dump directory is, for selection and for the error
    message when selection fails.

    Three facts decide it: the backbone dimension (the v25 replay is 1024-d,
    the retired July MiniLM one 384-d), the preset, and whether the replay
    ever evicted — ``turns_stored`` above the entry count means a
    capacity-scaled arm, whose surprise reconstruction would NOT be exact
    (substitution 2 in the spec depends on nothing having been evicted).
    """
    files = sorted(d.glob("*.json.gz"))
    sig: dict = {"n_dumps": len(files), "dim": None, "preset": None,
                 "evicted": None}
    if len(files) != N_QUESTIONS:
        return sig
    try:
        with gzip.open(files[0], "rt", encoding="utf-8") as fh:
            dump = json.load(fh)
    except (OSError, ValueError):
        return sig
    sig["dim"] = len(dump.get("query_emb") or [])
    sig["preset"] = dump.get("band_preset")
    stored = dump.get("turns_stored")
    resident = sum(len(b["entries"]) for b in dump.get("bands") or [])
    sig["evicted"] = None if stored is None else stored != resident
    return sig


def _is_v25_flat(sig: dict) -> bool:
    return (sig["n_dumps"] == N_QUESTIONS and sig["dim"] == V25_EMBEDDING_DIM
            and sig["preset"] == "flat" and sig["evicted"] is False)


def resolve_dump_dir(explicit: Path | None,
                     banks_root: Path | None = None) -> Path:
    """Pick the v25 replay the 2026-08-15 control was produced from.

    Selection is by content, not by directory name — see the
    ``V25_EMBEDDING_DIM`` comment. An explicit ``--dumps`` always wins, so a
    tree with a differently-shaped layout is one flag away from running.
    """
    if explicit is not None:
        if not Path(explicit).is_dir():
            raise SystemExit(f"--dumps directory does not exist: {explicit}")
        return Path(explicit)
    root = banks_root or (Path(__file__).resolve().parent / "results" / "banks")
    candidates = [d for d in sorted(Path(root).glob(DUMP_DIR_GLOB)) if d.is_dir()]
    sigs = {d: dump_dir_signature(d) for d in candidates}
    matches = [d for d in candidates if _is_v25_flat(sigs[d])]
    if len(matches) == 1:
        return matches[0]
    listing = "\n".join(
        f"  {d.name}: {sigs[d]['n_dumps']} dumps, {sigs[d]['dim']}-d, "
        f"preset={sigs[d]['preset']}, evicted={sigs[d]['evicted']}"
        for d in candidates
    ) or "  (none)"
    raise SystemExit(
        f"cannot identify the v25 band-state dumps under {root} "
        f"({N_QUESTIONS} dumps, {V25_EMBEDDING_DIM}-d, preset=flat, no "
        f"eviction).\nCandidates:\n{listing}\n"
        "The dumps are gitignored — copy or link them from the main checkout, "
        "or name one with --dumps.")


# ──────────────────────────────────────────────────────────────────────────
# Sweep mechanics
# ──────────────────────────────────────────────────────────────────────────

def reconstruct_surprise(entries: list[dict]) -> list[float]:
    """``MIRASBand.compute_surprise`` replayed over a dump's entries.

    ``1 - max cosine`` against the entries already resident, clamped into
    [0, 1], with 1.0 for the first entry (band.py:100-109 returns 1.0 for
    an empty band). Magnitudes are irrelevant — the real method
    normalises, so contradiction-decayed embeddings score identically.
    """
    import numpy as np  # noqa: PLC0415

    n = len(entries)
    if n == 0:
        return []
    emb = np.asarray([e["emb"] for e in entries], dtype=np.float32)
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    emb = emb / norms
    sims = emb @ emb.T
    out = [1.0] * n
    for i in range(1, n):
        out[i] = max(0.0, min(1.0, 1.0 - float(sims[i, :i].max())))
    return out


def eviction_scores(entries: list[dict], surprises: list[float], now: float,
                    policy_name: str) -> list[float]:
    """Per-entry ``RetentionPolicy.source_weighted_score`` — the shipped
    function, not a local copy. Lower is evicted first.

    The dumps carry no ``access_count`` / ``reinforcements``; both are the
    ``MemoryEntry`` defaults (0), documented as substitutions 1 and 3 in
    the spec.
    """
    from pseudolife_memory.memory.miras.retention import build_policy  # noqa: PLC0415

    policy = build_policy(policy_name)
    return [
        policy.source_weighted_score(
            SimpleNamespace(
                source=e["source"],
                reinforcements=0,
                superseded_at=e["superseded_at"],
                timestamp=e["ts"],
                access_count=0,
                surprise_score=s,
            ),
            now,
        )
        for e, s in zip(entries, surprises)
    ]


def cell_seed(question_id: str, scale: str, capacity: str, arm: str) -> int:
    """Deterministic per-cell RNG seed.

    CRC32 of the cell key rather than ``hash()``: Python randomises string
    hashing per process, which would make the ``random`` floor
    unreproducible between runs without any in-process check noticing.
    """
    key = f"{question_id}|{scale}|{capacity}|{arm}".encode("utf-8")
    return zlib.crc32(key)


def _keep_indices_by_score(scores: list[float], capacity: int) -> list[int]:
    """Indices surviving repeated ``_evict_one`` down to ``capacity``.

    ``_evict_one`` (band.py:140-151) pops ``min(range(len(scores)), ...)``
    — the FIRST minimum, i.e. the lowest insertion ordinal on a tie. The
    scores do not depend on which other entries are resident, so N pops is
    exactly "drop the N lowest by ``(score, ordinal)``". One sort, same
    result.
    """
    order = sorted(range(len(scores)), key=lambda i: (scores[i], i))
    return sorted(order[len(scores) - capacity:])


def sweep(entries: list[dict], surprises: list[float], capacity: int, arm: str,
          *, now: float, evidence: set[str], seed: int) -> list[dict]:
    """Reduce ``entries`` to ``capacity`` under ``arm``, preserving order.

    Order is load-bearing: ``select_topk`` tie-breaks on insertion ordinal,
    so a sweep that reordered the survivors would change the ranking by
    itself.
    """
    if arm not in ARMS:
        raise ValueError(f"unknown sweep arm {arm!r}. Available: {list(ARMS)}")
    if arm == "none" or len(entries) <= capacity:
        return list(entries)

    if arm in POLICY_ARMS:
        keep = _keep_indices_by_score(
            eviction_scores(entries, surprises, now, arm), capacity)
    elif arm == "random":
        rng = random.Random(seed)
        keep = sorted(rng.sample(range(len(entries)), capacity))
    elif arm == "oracle":
        protected = [i for i, e in enumerate(entries) if e["text"] in evidence]
        if len(protected) >= capacity:
            keep = sorted(protected[:capacity])
        else:
            rest = [i for i in range(len(entries)) if i not in set(protected)]
            rng = random.Random(seed)
            keep = sorted(protected
                          + rng.sample(rest, capacity - len(protected)))
    else:                                          # pragma: no cover - ARMS
        raise ValueError(arm)
    return [entries[i] for i in keep]


# ──────────────────────────────────────────────────────────────────────────
# Measurement
# ──────────────────────────────────────────────────────────────────────────

def _measure(dump: dict, pool_entries: list[dict], evidence: set[str]) -> dict:
    """One cell: re-select through the mirror and score the spec's metrics."""
    from pseudolife_memory.memory.bm25 import BM25Index  # noqa: PLC0415

    synth = {
        "question": dump["question"],
        "query_emb": dump["query_emb"],
        "search_time": dump["search_time"],
        "question_ts": dump["question_ts"],
        "bands": [{"name": "flat", "depth": 0, "entries": pool_entries}],
    }
    t0 = time.perf_counter()
    selected = select_topk(synth, "flat", "wall", recency="off", bm25=True)
    select_ms = (time.perf_counter() - t0) * 1000.0

    wrapped = [SimpleNamespace(text=e["text"]) for e in pool_entries]
    t0b = time.perf_counter()
    BM25Index(wrapped, k1=1.5, b=0.75).score(dump["question"], top_k=20)
    bm25_ms = (time.perf_counter() - t0b) * 1000.0

    selected_set = set(selected)
    rank = None
    for i, t in enumerate(selected):
        if t in evidence:
            rank = i + 1
            break
    survived = {e["text"] for e in pool_entries} & evidence
    return {
        "n_pool_entries": len(pool_entries),
        "evidence_in_top6": round(len(selected_set & evidence) / len(evidence), 4),
        "evidence_in_top3": round(len(set(selected[:3]) & evidence) / len(evidence), 4),
        "any_evidence_served": bool(selected_set & evidence),
        "rank_first_evidence": rank,
        "evidence_survival": round(len(survived) / len(evidence), 4),
        "select_topk_latency_ms": round(select_ms, 3),
        "bm25_latency_ms": round(bm25_ms, 3),
    }


AGG_MEAN_FIELDS = ("evidence_in_top6", "evidence_in_top3", "evidence_survival")
AGG_MEDIAN_FIELDS = ("select_topk_latency_ms", "bm25_latency_ms")


def _aggregate(rows: list[dict]) -> dict:
    out: dict = {"n_questions": len(rows)}
    for f in AGG_MEAN_FIELDS:
        out[f + "_mean"] = round(mean([r[f] for r in rows]), 4)
    out["any_evidence_served_mean"] = round(
        mean([1.0 if r["any_evidence_served"] else 0.0 for r in rows]), 4)
    ranks = [float(r["rank_first_evidence"]) for r in rows
             if r["rank_first_evidence"] is not None]
    out["rank_first_evidence_median"] = median(ranks) if ranks else None
    out["n_pool_entries_mean"] = round(
        mean([float(r["n_pool_entries"]) for r in rows]), 1)
    out["n_pool_entries_median"] = median([float(r["n_pool_entries"]) for r in rows])
    for f in AGG_MEDIAN_FIELDS:
        out[f + "_median"] = round(median([r[f] for r in rows]), 3)
    return out


def corpus_properties(dumps: dict[str, dict],
                      evidence_by_id: dict[str, set[str]]) -> dict:
    """The corpus fact that explains the result, as a checkable number.

    ``source_weighted_score`` multiplies a superseded entry by 0.05, below
    every live entry — so the shipped policies delete superseded material
    first. On a knowledge-update corpus the gold-evidence turn is
    disproportionately the one a later turn superseded, which is what turns
    "evict the cheapest" into "evict the answer". This measures both rates
    so the mechanism sentence in the docs is backed rather than asserted.
    """
    n_entries = n_superseded = n_evidence = n_evidence_superseded = 0
    for qid, dump in dumps.items():
        evidence = evidence_by_id.get(qid, set())
        for band in dump["bands"]:
            for e in band["entries"]:
                superseded = e["superseded_at"] is not None
                n_entries += 1
                n_superseded += superseded
                if e["text"] in evidence:
                    n_evidence += 1
                    n_evidence_superseded += superseded
    return {
        "n_questions": len(dumps),
        "n_entries": n_entries,
        "n_superseded": n_superseded,
        "superseded_rate": round(n_superseded / n_entries, 4) if n_entries else None,
        "n_evidence_entries": n_evidence,
        "n_evidence_superseded": n_evidence_superseded,
        "evidence_superseded_rate":
            round(n_evidence_superseded / n_evidence, 4) if n_evidence else None,
    }


def check_out_path(path: Path, force: bool) -> None:
    """Never overwrite a canonical result file on a rerun (house rule)."""
    if path.exists() and not force:
        raise SystemExit(
            f"refusing to overwrite existing artifact: {path}\n"
            "Tag the run with --out, or pass --force if the overwrite is "
            "deliberate.")


# ──────────────────────────────────────────────────────────────────────────
# Control reproduction (gate G-F0)
# ──────────────────────────────────────────────────────────────────────────

def _control_check(per_question: list[dict], repo_root: Path) -> dict:
    """The `none` arm must reproduce the 2026-08-15 artifact exactly."""
    ref_path = repo_root / CONTROL_ARTIFACT
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    ref_by_q = {r["question_id"]: r for r in ref["per_question"]}
    mismatches: list[dict] = []
    n_cells = 0
    for row in per_question:
        rq = ref_by_q.get(row["question_id"])
        if rq is None:
            mismatches.append({"question_id": row["question_id"],
                               "reason": "absent from the control artifact"})
            continue
        for label, _ in SCALES:
            got, want = row["none"][label], rq["scales"][label]
            n_cells += 1
            for f in CONTROL_FIELDS:
                if got[f] != want[f]:
                    mismatches.append({
                        "question_id": row["question_id"], "scale": label,
                        "field": f, "got": got[f], "expected": want[f]})
    return {
        "reference_artifact": CONTROL_ARTIFACT,
        "fields_checked": list(CONTROL_FIELDS),
        "latency_excluded": True,
        "n_cells_checked": n_cells,
        "n_mismatches": len(mismatches),
        "exact_match": not mismatches,
        "mismatches": mismatches[:20],
    }


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def _paired(by_cell: dict, capacity: str, scale: str,
            arm_a: str, arm_b: str, metric: str = "evidence_in_top6") -> dict:
    a = [r[metric] for r in by_cell[(capacity, scale, arm_a)]]
    b = [r[metric] for r in by_cell[(capacity, scale, arm_b)]]
    deltas = [x - y for x, y in zip(a, b)]
    return {"comparison": f"{arm_a} - {arm_b}", "capacity": capacity,
            "scale": scale, "metric": metric,
            "delta_mean": round(mean(deltas), 4),
            "p": round(_paired_permutation_p(deltas), 4)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="artifact path (never overwritten without --force)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing artifact deliberately")
    ap.add_argument("--dumps", type=Path, default=None,
                    help="band-state dump directory (default: the 1024-d v25 "
                         "replay under results/banks, auto-identified)")
    ap.add_argument("--corpus-props", type=Path, default=None,
                    help="write only the corpus-property artifact (superseded "
                         "rates over all entries and over gold evidence) and "
                         "exit; no re-selection, seconds not minutes")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N questions (smoke runs; the control "
                         "gate still checks every question it ran)")
    args = ap.parse_args(argv)

    out_path = args.corpus_props or args.out
    check_out_path(out_path, args.force)          # before any work

    repo_root = Path(__file__).resolve().parents[1]
    t_start = time.perf_counter()
    dump_dir = resolve_dump_dir(args.dumps)
    # Point the inherited loader at the resolved directory rather than
    # reimplementing it — `load_dumps` reads `distractor_scale_probe.DUMP_DIR`.
    import distractor_scale_probe as _dsp  # noqa: PLC0415
    _dsp.DUMP_DIR = dump_dir
    print(f"dumps: {dump_dir.name}", flush=True)

    from longmemeval_bench import load_questions  # noqa: PLC0415 — heavy (torch)

    questions = {q["question_id"]: q for q in load_questions("s")}
    dumps = load_dumps()
    missing = sorted(set(questions) - set(dumps))
    if missing:
        sys.exit(f"missing dumps for {len(missing)} questions: {missing[:5]}")
    extra = sorted(set(dumps) - set(questions))
    if extra:
        sys.exit(f"dumps present with no matching question: {extra[:5]}")

    sorted_ids = sorted(dumps.keys())
    n_ids = len(sorted_ids)
    if n_ids != N_QUESTIONS:
        sys.exit(f"expected {N_QUESTIONS} questions, found {n_ids}")
    id_pos = {qid: i for i, qid in enumerate(sorted_ids)}

    evidence_by_id = {
        qid: _evidence_texts(questions[qid]) & {e["text"] for e in
                                                d["bands"][0]["entries"]}
        for qid, d in dumps.items()
    }

    if args.corpus_props is not None:
        props = dict(corpus_properties(dumps, evidence_by_id),
                     spec=SPEC, dump_dir=dump_dir.name, dataset="s",
                     runtime_s=round(time.perf_counter() - t_start, 1))
        args.corpus_props.parent.mkdir(parents=True, exist_ok=True)
        args.corpus_props.write_text(json.dumps(props, indent=2), encoding="utf-8")
        print(json.dumps(props, indent=2))
        print(f"\nwrote {args.corpus_props}")
        return 0

    print("reconstructing per-dump surprise "
          "(MIRASBand.compute_surprise replay)...", flush=True)
    surprise_by_id = {qid: reconstruct_surprise(dumps[qid]["bands"][0]["entries"])
                      for qid in sorted_ids}

    run_ids = sorted_ids[: args.limit] if args.limit else sorted_ids
    per_question: list[dict] = []
    by_cell: dict[tuple[str, str, str], list[dict]] = {}

    for qi, qid in enumerate(run_ids):
        dump = dumps[qid]
        own_entries = dump["bands"][0]["entries"]
        evidence = evidence_by_id[qid]
        if not evidence:
            sys.exit(f"question {qid} has no gold-evidence turn present in "
                     "its own dump — statistics require 78 paired questions")
        now = dump["search_time"]
        idx = id_pos[qid]

        pools: dict[str, tuple[list[dict], list[float]]] = {}
        for label, k_foreign in SCALES:
            entries = list(own_entries)
            surprises = list(surprise_by_id[qid])
            for j in range(k_foreign):
                fid = sorted_ids[(idx + 1 + j) % n_ids]
                entries.extend(dumps[fid]["bands"][0]["entries"])
                surprises.extend(surprise_by_id[fid])
            pools[label] = (entries, surprises)

        caps = {"C1": len(pools["1x"][0]), "C3": len(pools["3x"][0])}
        row: dict = {"question_id": qid, "n_evidence": len(evidence),
                     "capacities": dict(caps), "none": {}, "cells": {}}

        for label, _ in SCALES:
            entries, surprises = pools[label]
            control = _measure(dump, entries, evidence)
            row["none"][label] = control

        for cap_label in CAPACITIES:
            capacity = caps[cap_label]
            row["cells"][cap_label] = {}
            for label, _ in SCALES:
                entries, surprises = pools[label]
                cell: dict = {}
                swept = len(entries) > capacity
                for arm in ARMS:
                    if arm == "none" or not swept:
                        # A capacity at or above the pool size is a no-op:
                        # reuse the control rather than re-measuring, so the
                        # no-op cells are identical by construction and not
                        # merely by arithmetic.
                        m = dict(row["none"][label])
                    else:
                        kept = sweep(entries, surprises, capacity, arm, now=now,
                                     evidence=evidence,
                                     seed=cell_seed(qid, label, cap_label, arm))
                        m = _measure(dump, kept, evidence)
                    m["swept"] = bool(swept and arm != "none")
                    cell[arm] = m
                    by_cell.setdefault((cap_label, label, arm), []).append(m)
                row["cells"][cap_label][label] = cell

        per_question.append(row)
        print(f"[{qi + 1}/{len(run_ids)}] {qid}  n_ev={len(evidence)}  "
              f"C1={caps['C1']} C3={caps['C3']}  "
              + "  ".join(
                  f"{a[:4]}@15x:"
                  f"{row['cells']['C1']['15x'][a]['evidence_in_top6']:.2f}"
                  for a in ARMS),
              flush=True)

    control = _control_check(per_question, repo_root)
    if not control["exact_match"]:
        print("\nG-F0 CONTROL FAILED — the `none` arm did not reproduce "
              f"{CONTROL_ARTIFACT}. Nothing written.", file=sys.stderr)
        for m in control["mismatches"]:
            print("  " + json.dumps(m), file=sys.stderr)
        return 2

    cells_out: dict = {}
    for cap_label in CAPACITIES:
        cells_out[cap_label] = {}
        for label, _ in SCALES:
            cells_out[cap_label][label] = {
                arm: dict(_aggregate(by_cell[(cap_label, label, arm)]),
                          swept=by_cell[(cap_label, label, arm)][0]["swept"])
                for arm in ARMS
            }

    # ── G-F1: does any shipped sweep pay? ──────────────────────────────────
    g_f1: dict = {"bar": f"delta >= +{ABS_DELTA_BAR} with p < {ALPHA}",
                  "capacity": GATE_CAPACITY, "scale": GATE_SCALE, "arms": {}}
    for arm in POLICY_ARMS:
        t = _paired(by_cell, GATE_CAPACITY, GATE_SCALE, arm, "none")
        t["pays"] = t["delta_mean"] >= ABS_DELTA_BAR and t["p"] < ALPHA
        g_f1["arms"][arm] = t
    g_f1["any_arm_pays"] = any(t["pays"] for t in g_f1["arms"].values())
    g_f1["verdict"] = (
        "a shipped sweep pays at this capacity"
        if g_f1["any_arm_pays"] else
        "no shipped sweep pays — the wider bank wins; do not build a sweep "
        "agent on these eviction scores")

    # ── G-F2: is victim choice worth anything at all? ──────────────────────
    g_f2 = _paired(by_cell, GATE_CAPACITY, GATE_SCALE, "oracle", "none")
    g_f2["bar"] = g_f1["bar"]
    g_f2["pays"] = g_f2["delta_mean"] >= ABS_DELTA_BAR and g_f2["p"] < ALPHA
    g_f2["verdict"] = (
        "victim choice is worth something — the ceiling is the measured delta; "
        "the loss is in the scores, not in forgetting"
        if g_f2["pays"] else
        "victim choice cannot help at this capacity — the finding is about "
        "capacity, not about which victims are picked")

    # ── G-F3: do the shipped scores beat coin-flipping? ────────────────────
    g_f3: dict = {"bar": f"p < {ALPHA}, either direction",
                  "capacity": GATE_CAPACITY, "scale": GATE_SCALE, "arms": {}}
    for arm in POLICY_ARMS:
        t = _paired(by_cell, GATE_CAPACITY, GATE_SCALE, arm, "random")
        t["significant"] = t["p"] < ALPHA
        t["verdict"] = (
            "below the random floor — a finding about source_weighted_score"
            if t["significant"] and t["delta_mean"] < 0 else
            "above the random floor" if t["significant"] else
            "indistinguishable from random")
        g_f3["arms"][arm] = t

    # ── G-F4: inherited sanity floor at 1x ─────────────────────────────────
    hit6_1x = cells_out[GATE_CAPACITY]["1x"]["none"]["evidence_in_top6_mean"]
    g_f4 = {"scale": "1x", "arm": "none",
            "evidence_in_top6_mean": hit6_1x,
            "threshold": G_F4_MIN_HIT_RATE,
            "verdict_pass": hit6_1x >= G_F4_MIN_HIT_RATE,
            "verdict": ("sanity OK — metric can gate" if hit6_1x >= G_F4_MIN_HIT_RATE
                        else "inconclusive — metric too weak to gate")}

    secondary = [
        _paired(by_cell, "C1", "31x", arm, "none") for arm in
        POLICY_ARMS + ("random", "oracle")
    ] + [
        _paired(by_cell, "C3", GATE_SCALE, arm, "none") for arm in
        POLICY_ARMS + ("random", "oracle")
    ]

    ordering = sorted(
        ((arm, cells_out[GATE_CAPACITY][GATE_SCALE][arm]["evidence_in_top6_mean"])
         for arm in ARMS), key=lambda kv: -kv[1])

    out = {
        "spec": SPEC,
        "dataset": "s",
        "dump_dir": dump_dir.name,
        "embedding_dim": len(dumps[sorted_ids[0]]["query_emb"]),
        "n_questions": len(run_ids),
        "partial": bool(args.limit),
        "arms": list(ARMS),
        "capacities": list(CAPACITIES),
        "gate_cell": {"capacity": GATE_CAPACITY, "scale": GATE_SCALE,
                      "metric": "evidence_in_top6"},
        "control": control,
        "cells": cells_out,
        "gates": {"G-F0": {"exact_match": control["exact_match"],
                           "n_cells_checked": control["n_cells_checked"]},
                  "G-F1": g_f1, "G-F2": g_f2, "G-F3": g_f3, "G-F4": g_f4},
        "observed_ordering_at_gate_cell": [
            {"arm": a, "evidence_in_top6_mean": v} for a, v in ordering],
        "secondary_comparisons": secondary,
        "caveats": CAVEATS,
        "per_question": per_question,
        "runtime_s": round(time.perf_counter() - t_start, 1),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"G-F0 (control)  none reproduces {CONTROL_ARTIFACT} exactly: "
          f"{control['exact_match']}  ({control['n_cells_checked']} cells)")
    print(f"G-F4 (sanity)   1x evidence-in-top-6 = {hit6_1x:.4f}  "
          f"-> {g_f4['verdict']}")
    print(f"\nordering at {GATE_CAPACITY}/{GATE_SCALE} (evidence-in-top-6): "
          + "  ".join(f"{a}={v:.4f}" for a, v in ordering))
    for arm, t in g_f1["arms"].items():
        print(f"G-F1 {arm:>14} - none = {t['delta_mean']:+.4f}  "
              f"p={t['p']:.4f}  pays={t['pays']}")
    print(f"G-F1 verdict: {g_f1['verdict']}")
    print(f"G-F2 {'oracle':>14} - none = {g_f2['delta_mean']:+.4f}  "
          f"p={g_f2['p']:.4f}  -> {g_f2['verdict']}")
    for arm, t in g_f3["arms"].items():
        print(f"G-F3 {arm:>14} - random = {t['delta_mean']:+.4f}  "
              f"p={t['p']:.4f}  -> {t['verdict']}")
    print(f"\nwrote {args.out}  ({out['runtime_s']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
