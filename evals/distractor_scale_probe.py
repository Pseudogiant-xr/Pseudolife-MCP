"""Distractor-scale probe — does accumulation degrade retrieval? (2026-08-15)

Preregistered offline analysis; the contract is
``docs/superpowers/specs/2026-08-15-distractor-scale-probe-preregistration.md``
— read that first for the design rationale. This module implements it
exactly: no new arms, metrics, or statistics beyond what the spec states.

Pure CPU replay over the existing v25 flat-band dumps
(``evals/results/banks/s-qwen-27b-ablbands-flat/``, 78 knowledge-update
questions). For each question, five synthetic pools are built by
concatenating the question's own dump with 0/2/6/14/30 *other* questions'
dumps (a fixed, RNG-free rotation by sorted question_id, wrap-around), then
re-selected through the G0-validated offline mirror
(``band_ablation.select_topk``, flat policy, recency off, BM25 on). The 1x
arm (own haystack only) doubles as the perfect-sweep oracle.

Usage (repo root, venv python; CPU-only, no GPU/judge needed):

    python evals/distractor_scale_probe.py

Writes ``evals/results/distractor-scale-probe-2026-08-15.json`` and prints
the three preregistered gate verdicts (G-D1 quality, G-D2 latency, G-D3
sanity).
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))          # evals/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from band_ablation import _evidence_texts, _paired_permutation_p, select_topk  # noqa: E402

SPEC = "docs/superpowers/specs/2026-08-15-distractor-scale-probe-preregistration.md"
DUMP_DIR = Path(__file__).resolve().parent / "results" / "banks" / "s-qwen-27b-ablbands-flat"
OUT_PATH = Path(__file__).resolve().parent / "results" / "distractor-scale-probe-2026-08-15.json"

# arm label -> K (number of *other* questions' dumps pooled in, in the fixed
# rotation) — table from the spec's "Design" section.
SCALES: list[tuple[str, int]] = [
    ("1x", 0), ("3x", 2), ("7x", 6), ("15x", 14), ("31x", 30),
]

# ── preregistered gate thresholds ──────────────────────────────────────────
G_D1_SCALE = "15x"                 # primary drop check; 31x is the fallback
G_D1_FALLBACK_SCALE = "31x"
G_D1_ABS_DROP = 0.05
G_D1_ALPHA = 0.05
G_D2_SCALE = "15x"                 # "≤15x (~7k entries)" — check the ceiling
G_D2_THRESHOLD_S = 1.0
G_D3_SCALE = "1x"
G_D3_MIN_HIT_RATE = 0.5

LIVE_BANK_ENTRIES = 682
LIVE_BANK_GROWTH_PER_DAY = 10.0


def load_dumps() -> dict[str, dict]:
    dumps: dict[str, dict] = {}
    for p in sorted(DUMP_DIR.glob("*.json.gz")):
        qid = p.name[: -len(".json.gz")]
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            dumps[qid] = json.load(fh)
    return dumps


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares slope/intercept (y = slope*x + intercept), no numpy
    dependency for this tiny 5-point fit."""
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    slope = num / den if den else 0.0
    intercept = my - slope * mx
    return slope, intercept


def main() -> int:
    t_start = time.perf_counter()
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
    if n_ids != 78:
        sys.exit(f"expected 78 questions, found {n_ids}")
    id_pos = {qid: i for i, qid in enumerate(sorted_ids)}

    # Import the real BM25 index for the separately-timed latency probe (same
    # construction band_ablation.select_topk uses internally for Pool 1.75).
    from pseudolife_memory.memory.bm25 import BM25Index  # noqa: PLC0415

    per_question: list[dict] = []
    # scale -> list of per-question metric dicts, for aggregation
    by_scale: dict[str, dict[str, list[float]]] = {
        label: {"evidence_in_top6": [], "evidence_in_top3": [],
                "any_evidence_served": [], "rank_first_evidence": [],
                "n_pool_entries": [], "select_topk_latency_ms": [],
                "bm25_latency_ms": []}
        for label, _ in SCALES
    }

    for qi, qid in enumerate(sorted_ids):
        dump = dumps[qid]
        q = questions[qid]
        own_entries = dump["bands"][0]["entries"]
        own_texts = {e["text"] for e in own_entries}
        evidence = _evidence_texts(q) & own_texts
        if not evidence:
            sys.exit(f"question {qid} has no gold-evidence turn present in "
                     "its own dump — statistics require 78 paired questions")

        idx = id_pos[qid]
        q_row: dict = {"question_id": qid, "n_evidence": len(evidence),
                       "scales": {}}

        for label, k_foreign in SCALES:
            foreign_ids = [sorted_ids[(idx + 1 + j) % n_ids] for j in range(k_foreign)]
            pool_entries = list(own_entries)
            for fid in foreign_ids:
                pool_entries.extend(dumps[fid]["bands"][0]["entries"])

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
            bm25_idx = BM25Index(wrapped, k1=1.5, b=0.75)
            bm25_idx.score(dump["question"], top_k=20)
            bm25_ms = (time.perf_counter() - t0b) * 1000.0

            selected_set = set(selected)
            hit6 = len(selected_set & evidence) / len(evidence)
            hit3 = len(set(selected[:3]) & evidence) / len(evidence)
            any_served = 1.0 if (selected_set & evidence) else 0.0
            rank = None
            for i, t in enumerate(selected):
                if t in evidence:
                    rank = i + 1
                    break

            by_scale[label]["evidence_in_top6"].append(hit6)
            by_scale[label]["evidence_in_top3"].append(hit3)
            by_scale[label]["any_evidence_served"].append(any_served)
            if rank is not None:
                by_scale[label]["rank_first_evidence"].append(float(rank))
            by_scale[label]["n_pool_entries"].append(float(len(pool_entries)))
            by_scale[label]["select_topk_latency_ms"].append(select_ms)
            by_scale[label]["bm25_latency_ms"].append(bm25_ms)

            q_row["scales"][label] = {
                "n_pool_entries": len(pool_entries),
                "evidence_in_top6": round(hit6, 4),
                "evidence_in_top3": round(hit3, 4),
                "any_evidence_served": bool(any_served),
                "rank_first_evidence": rank,
                "select_topk_latency_ms": round(select_ms, 3),
                "bm25_latency_ms": round(bm25_ms, 3),
            }

        per_question.append(q_row)
        print(f"[{qi + 1}/{n_ids}] {qid}  n_evidence={len(evidence)}  "
              + "  ".join(f"{label}:{q_row['scales'][label]['evidence_in_top6']:.2f}"
                          for label, _ in SCALES), flush=True)

    # ── per-scale aggregates ────────────────────────────────────────────────
    scales_out: dict[str, dict] = {}
    for label, _ in SCALES:
        m = by_scale[label]
        scales_out[label] = {
            "n_questions": len(m["evidence_in_top6"]),
            "evidence_in_top6_mean": round(mean(m["evidence_in_top6"]), 4),
            "evidence_in_top3_mean": round(mean(m["evidence_in_top3"]), 4),
            "any_evidence_served_mean": round(mean(m["any_evidence_served"]), 4),
            "rank_first_evidence_median":
                (median(m["rank_first_evidence"]) if m["rank_first_evidence"] else None),
            "n_pool_entries_mean": round(mean(m["n_pool_entries"]), 1),
            "n_pool_entries_median": median(m["n_pool_entries"]),
            "select_topk_latency_ms_median": round(median(m["select_topk_latency_ms"]), 3),
            "bm25_latency_ms_median": round(median(m["bm25_latency_ms"]), 3),
        }

    # ── G-D1 (quality): 1x vs 15x, sign-flip permutation on evidence-in-top6 ──
    def paired_delta_p(scale_hi: str) -> tuple[list[float], float, float]:
        hi = by_scale[scale_hi]["evidence_in_top6"]
        lo = by_scale["1x"]["evidence_in_top6"]
        deltas = [a - b for a, b in zip(lo, hi)]   # 1x - Nx: positive = accumulation hurts
        return deltas, mean(deltas), _paired_permutation_p(deltas)

    deltas_15, delta_mean_15, p_15 = paired_delta_p(G_D1_SCALE)
    checked_31 = False
    deltas_31 = delta_mean_31 = p_31 = None
    significant_15 = p_15 < G_D1_ALPHA and delta_mean_15 > 0
    if not significant_15:
        checked_31 = True
        deltas_31, delta_mean_31, p_31 = paired_delta_p(G_D1_FALLBACK_SCALE)

    def verdict_for(delta_mean: float, p: float) -> str:
        significant = p < G_D1_ALPHA and delta_mean > 0
        if not significant:
            return "not significant — accumulation not shown to hurt at this scale"
        if delta_mean >= G_D1_ABS_DROP:
            return ("hurts — sweep agent has measured value; recovery ceiling "
                    f"= {delta_mean:.4f}")
        return "measurable but small — sweep is low priority"

    g_d1: dict = {
        "metric": "evidence_in_top6",
        "comparison": "1x - 15x (positive = accumulation hurts)",
        "delta_mean_1x_minus_15x": round(delta_mean_15, 4),
        "p_1x_vs_15x": round(p_15, 4),
        "checked_31x_fallback": checked_31,
    }
    if checked_31:
        g_d1["delta_mean_1x_minus_31x"] = round(delta_mean_31, 4)
        g_d1["p_1x_vs_31x"] = round(p_31, 4)
        g_d1["verdict"] = verdict_for(delta_mean_31, p_31)
        g_d1["verdict_basis"] = "31x (15x not significant)"
    else:
        g_d1["verdict"] = verdict_for(delta_mean_15, p_15)
        g_d1["verdict_basis"] = "15x"

    # ── G-D2 (latency): median BM25 cost at 15x vs 1s threshold ──────────────
    bm25_median_by_scale = {label: scales_out[label]["bm25_latency_ms_median"] for label, _ in SCALES}
    median_at_gate_scale_ms = scales_out[G_D2_SCALE]["bm25_latency_ms_median"]
    g_d2_pass = median_at_gate_scale_ms > (G_D2_THRESHOLD_S * 1000.0)

    fit_xs = [scales_out[label]["n_pool_entries_mean"] for label, _ in SCALES]
    fit_ys = [scales_out[label]["bm25_latency_ms_median"] for label, _ in SCALES]
    slope_ms_per_entry, intercept_ms = linfit(fit_xs, fit_ys)
    if slope_ms_per_entry > 0:
        crossing_n_entries = (G_D2_THRESHOLD_S * 1000.0 - intercept_ms) / slope_ms_per_entry
        days_to_crossing = ((crossing_n_entries - LIVE_BANK_ENTRIES)
                            / LIVE_BANK_GROWTH_PER_DAY)
    else:
        crossing_n_entries = None
        days_to_crossing = None

    g_d2 = {
        "gate_scale": G_D2_SCALE,
        "threshold_s": G_D2_THRESHOLD_S,
        "bm25_latency_ms_median_by_scale": bm25_median_by_scale,
        "median_ms_at_gate_scale": round(median_at_gate_scale_ms, 3),
        "verdict_pass": g_d2_pass,
        "verdict": ("index maintenance justified — median BM25 cost exceeds "
                    f"{G_D2_THRESHOLD_S:.0f}s by {G_D2_SCALE}"
                    if g_d2_pass else
                    "index maintenance not justified at these scales — "
                    f"median BM25 cost stays under {G_D2_THRESHOLD_S:.0f}s "
                    f"through {G_D2_SCALE}"),
        "fitted_ms_per_entry": round(slope_ms_per_entry, 6),
        "fitted_intercept_ms": round(intercept_ms, 3),
        "predicted_crossing_n_entries":
            (round(crossing_n_entries, 1) if crossing_n_entries is not None else None),
        "live_bank_entries": LIVE_BANK_ENTRIES,
        "live_bank_growth_per_day": LIVE_BANK_GROWTH_PER_DAY,
        "predicted_days_to_crossing_live_bank":
            (round(days_to_crossing, 1) if days_to_crossing is not None else None),
    }

    # ── G-D3 (sanity): 1x evidence-in-top6 must be >= 0.5 ────────────────────
    hit6_1x = scales_out[G_D3_SCALE]["evidence_in_top6_mean"]
    g_d3_pass = hit6_1x >= G_D3_MIN_HIT_RATE
    g_d3 = {
        "scale": G_D3_SCALE,
        "evidence_in_top6_mean": hit6_1x,
        "threshold": G_D3_MIN_HIT_RATE,
        "verdict_pass": g_d3_pass,
        "verdict": ("sanity OK — metric can gate" if g_d3_pass else
                    "inconclusive, needs the judged variant — metric too weak to gate"),
    }
    if not g_d3_pass:
        for gate in (g_d1, g_d2):
            gate["verdict"] = "inconclusive (G-D3 sanity failed): " + gate["verdict"]

    interpretation = None
    if g_d3_pass:
        g1_significant = ((g_d1["p_1x_vs_31x"] if checked_31 else g_d1["p_1x_vs_15x"]) < G_D1_ALPHA
                          and (g_d1["delta_mean_1x_minus_31x"] if checked_31
                               else g_d1["delta_mean_1x_minus_15x"]) > 0)
        if not g1_significant and not g_d2_pass:
            interpretation = ("accumulation is free at these scales; the sweep agent "
                              "is premature; revisit at 10x the live bank's size or "
                              "when BM25 latency crosses 200ms live")
        elif g1_significant:
            interpretation = ("the sweep has a measured recovery ceiling; the "
                              "follow-up design (realistic sweep vs oracle sweep) "
                              "inherits this probe's construction with the sweep "
                              "arm replacing the 1x oracle")
        elif g_d2_pass:
            interpretation = "build incremental BM25 index maintenance, not a sweep"

    out = {
        "spec": SPEC,
        "dataset": "s",
        "n_questions": n_ids,
        "scales": scales_out,
        "gates": {"G-D1": g_d1, "G-D2": g_d2, "G-D3": g_d3},
        "interpretation": interpretation,
        "per_question": per_question,
        "bm25_latency_ms": bm25_median_by_scale,
        "runtime_s": round(time.perf_counter() - t_start, 1),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OUT_PATH.exists():
        sys.exit(f"refusing to overwrite existing artifact: {OUT_PATH}")
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"G-D3 (sanity)  1x evidence-in-top6 = {hit6_1x:.4f}  "
          f"(threshold >= {G_D3_MIN_HIT_RATE})  -> {g_d3['verdict']}")
    print(f"G-D1 (quality) [{g_d1['verdict_basis']}]  "
          f"delta(1x-{g_d1['verdict_basis']}) = "
          f"{(g_d1['delta_mean_1x_minus_31x'] if checked_31 else g_d1['delta_mean_1x_minus_15x']):+.4f}"
          f"  p = {(g_d1['p_1x_vs_31x'] if checked_31 else g_d1['p_1x_vs_15x']):.4f}"
          f"  -> {g_d1['verdict']}")
    print(f"G-D2 (latency) median BM25 ms @ {G_D2_SCALE} = "
          f"{median_at_gate_scale_ms:.1f}ms  (threshold {G_D2_THRESHOLD_S * 1000:.0f}ms)"
          f"  -> {g_d2['verdict']}")
    if interpretation:
        print(f"\nInterpretation: {interpretation}")
    print(f"\nwrote {OUT_PATH}  ({out['runtime_s']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
