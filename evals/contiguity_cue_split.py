"""Cue-gated re-read of the aggp1-variants-0803 retrieval-knob run.

The 2026-08-03 aggregation-aware-recall Phase 1 gates (PR #93, verdict
``evals/results/agg-recall-phase1-verdict.json``) applied four retrieval
knobs to EVERY query and all four lost on the weak-type slice
(multi-session + temporal-reasoning).  Contiguity lost hardest
(-0.147).  This asks the follow-up question the verdict never did:
would contiguity have helped if it had fired only where the engine's own
aggregation/temporal CUE detector says the query is asking about order
or counts?

The run persisted per-arm contexts, judged verdicts and context-token
counts for every question, so the gated policy is computable offline:
a gated arm serves the vanilla ``hybrid`` context where the cue is off
and the variant context where it is on, and each of those verdicts was
already judged.  No new answer or judge calls are made here, and none
can be -- this re-reads committed rows.

Honest framing (repeated in evals/README.md and the artifact):

* Single replicate, 2026-08-03, on the retired Qwen3.6 judge.  Every
  number inherits that instrument.
* A composite of two already-judged arms is not a run.  A gated knob
  that looked promising here would still need its own judged run before
  shipping.
* The ``hybrid_tl`` arm is a built-in noise control: the timeline
  channel is ALREADY cue-gated inside the engine
  (``cms.py``'s ``timeline_fired``), so on cue-off rows its context is
  byte-identical to ``hybrid`` and any verdict disagreement there is
  pure answerer/judge noise.  That disagreement rate bounds what the
  other splits can claim.

Detectors are IMPORTED from ``pseudolife_memory.memory.cms`` -- never
re-implemented -- so the fire rates below describe the shipped
predicates and drift with them.  ``_perm_p`` is imported from
``evals/compare_arms.py`` (the same sign-flip statistic the Phase-1
verdict used; ``evals/beam_within_run_pairs.py`` does not exist on this
branch).

Usage::

    python evals/contiguity_cue_split.py --out evals/results/<tag>.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "evals"))

from compare_arms import _perm_p  # noqa: E402
from context_format import MEMS_HEADER  # noqa: E402
from pseudolife_memory.memory.cms import (  # noqa: E402
    has_aggregation_cue,
    has_date_cue,
    has_temporal_cue,
)

DEFAULT_ROWS = (REPO / "evals" / "results"
                / "longmemeval-all-oracle-qwen-27b-aggp1-variants-0803.jsonl")

BASE_ARM = "hybrid"
CONTROL_ARM = "rag"
VARIANTS = ("hybrid_ctg", "hybrid_tl", "hybrid_enum", "hybrid_all")

# The Phase-1 gate slice: the two types every knob was meant to rescue.
WEAK_TYPES = ("multi-session", "temporal-reasoning")

# Which detector gates which arm.  ``any`` mirrors the engine's own
# chronicle-serving gate in service.py (aggregation OR temporal OR
# explicit date); the individual predicates are reported beside it so a
# reader can see whether a narrower gate would have done better.
CUE_NAMES = ("temporal", "aggregation", "date", "any")
PRIMARY_CUE = "any"

# The served-context layout: facts, then the memory turns.  The header
# literal is IMPORTED from ``context_format`` rather than re-typed --
# ``test_answerability_probe.py::test_hybrid_header_literals_have_exactly
# _one_home`` enforces the single home, because a drifted copy here would
# silently parse every context as zero blocks.  Turn blocks are re-split
# on their date header rather than on blank lines: assistant turns
# contain blank lines, and splitting on those inflates the diff.
_TURN_RE = re.compile(
    r"(?=^\[\d{4}/\d{2}/\d{2} \([A-Za-z]{3}\) \d{2}:\d{2}\] "
    r"(?:user|assistant): )",
    re.MULTILINE,
)


def cue_flags(question: str) -> dict[str, bool]:
    """The engine's own cue predicates over one question text."""
    temporal = has_temporal_cue(question)
    aggregation = has_aggregation_cue(question)
    date = has_date_cue(question)
    return {
        "temporal": temporal,
        "aggregation": aggregation,
        "date": date,
        "any": temporal or aggregation or date,
    }


def mem_blocks(context: str) -> list[str]:
    """The memory turns a served context carried, in order."""
    if MEMS_HEADER not in context:
        return []
    mems = context.split(MEMS_HEADER, 1)[1]
    return [b.strip() for b in _TURN_RE.split(mems) if b.strip()]


def load_rows(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _mean_ci(deltas: list[float]) -> dict:
    """Mean paired delta with a 95% Wald interval on the mean.

    Paired binary deltas are in {-1, 0, 1}; the normal approximation on
    their mean is the same one the Phase-1 verdict's deltas carried, and
    the permutation p is the inferential statistic -- the interval is
    reported for magnitude, not for the decision.
    """
    n = len(deltas)
    if n == 0:
        return {"n": 0, "delta": 0.0, "ci_lo": 0.0, "ci_hi": 0.0}
    mean = sum(deltas) / n
    if n > 1:
        var = sum((d - mean) ** 2 for d in deltas) / (n - 1)
        half = 1.96 * math.sqrt(var / n)
    else:
        half = 0.0
    return {"n": n, "delta": round(mean, 4),
            "ci_lo": round(mean - half, 4), "ci_hi": round(mean + half, 4)}


def paired(rows: list[dict], arm: str, base: str = BASE_ARM, *,
           draws: int = 10_000, seed: int = 0) -> dict:
    """Within-run paired comparison of ``arm`` against ``base``."""
    a = [int(bool(r[f"{arm}_correct"])) for r in rows]
    b = [int(bool(r[f"{base}_correct"])) for r in rows]
    deltas = [x - y for x, y in zip(a, b)]
    out = _mean_ci([float(d) for d in deltas])
    n = max(len(rows), 1)
    out["arm_acc"] = round(sum(a) / n, 4) if rows else 0.0
    out["base_acc"] = round(sum(b) / n, 4) if rows else 0.0
    out["wins"] = sum(1 for d in deltas if d > 0)
    out["losses"] = sum(1 for d in deltas if d < 0)
    out["p"] = round(_perm_p([float(d) for d in deltas], draws, seed), 5)
    return out


def gated_correct(row: dict, arm: str, cue: str, base: str = BASE_ARM) -> int:
    """The gated policy's verdict: variant where the cue fired, vanilla
    where it did not.  Both verdicts were judged in the source run."""
    fired = cue_flags(row["question"])[cue]
    return int(bool(row[f"{arm}_correct" if fired else f"{base}_correct"]))


def gated_tokens(row: dict, arm: str, cue: str, base: str = BASE_ARM) -> int:
    fired = cue_flags(row["question"])[cue]
    key = f"{arm}_context_tokens" if fired else f"{base}_context_tokens"
    return int(row[key])


def _acc(vals: list[int]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def gated_arm(rows: list[dict], arm: str, cue: str, *,
              draws: int = 10_000, seed: int = 0) -> dict:
    """The composite arm's accuracy and cost, paired against the vanilla
    hybrid baseline and against the naive-RAG control."""
    g = [gated_correct(r, arm, cue) for r in rows]
    h = [int(bool(r[f"{BASE_ARM}_correct"])) for r in rows]
    c = [int(bool(r[f"{CONTROL_ARM}_correct"])) for r in rows]
    toks = [gated_tokens(r, arm, cue) for r in rows]
    d_h = [float(x - y) for x, y in zip(g, h)]
    d_c = [float(x - y) for x, y in zip(g, c)]
    out = {
        "n": len(rows),
        "gated_acc": _acc(g),
        "hybrid_acc": _acc(h),
        "rag_acc": _acc(c),
        "gated_context_tokens": (round(sum(toks) / len(toks), 1)
                                 if toks else 0.0),
        "hybrid_context_tokens": (
            round(sum(int(r[f"{BASE_ARM}_context_tokens"]) for r in rows)
                  / len(rows), 1) if rows else 0.0),
        "ungated_acc": _acc([int(bool(r[f"{arm}_correct"])) for r in rows]),
        "vs_hybrid": {**_mean_ci(d_h),
                      "p": round(_perm_p(d_h, draws, seed), 5)},
        "vs_rag": {**_mean_ci(d_c),
                   "p": round(_perm_p(d_c, draws, seed), 5)},
    }
    return out


def context_effect(rows: list[dict], arm: str) -> dict:
    """What the variant actually did to the served context: turns added,
    turns displaced out of the top-k window, token delta."""
    added, dropped, tok_d, identical = [], [], [], 0
    for r in rows:
        base_blocks = mem_blocks(r["contexts"][BASE_ARM])
        var_blocks = mem_blocks(r["contexts"][arm])
        base_set, var_set = set(base_blocks), set(var_blocks)
        added.append(len(var_set - base_set))
        dropped.append(len(base_set - var_set))
        tok_d.append(int(r[f"{arm}_context_tokens"])
                     - int(r[f"{BASE_ARM}_context_tokens"]))
        if r["contexts"][arm] == r["contexts"][BASE_ARM]:
            identical += 1
    n = max(len(rows), 1)
    return {
        "n": len(rows),
        "identical_context_rows": identical,
        "mean_turns_added": round(sum(added) / n, 2),
        "mean_turns_displaced": round(sum(dropped) / n, 2),
        "rows_with_any_added": sum(1 for x in added if x),
        "rows_with_any_displaced": sum(1 for x in dropped if x),
        "mean_context_token_delta": round(sum(tok_d) / n, 1),
    }


def noise_floor(rows: list[dict], arm: str) -> dict:
    """Verdict disagreement on rows whose variant context is
    byte-identical to the baseline's -- the measurement floor."""
    same = [r for r in rows
            if r["contexts"][arm] == r["contexts"][BASE_ARM]]
    flips = [r for r in same
             if bool(r[f"{arm}_correct"]) != bool(r[f"{BASE_ARM}_correct"])]
    return {
        "identical_context_rows": len(same),
        "verdict_disagreements": len(flips),
        "disagreement_rate": (round(len(flips) / len(same), 4)
                              if same else 0.0),
    }


def cue_report(rows: list[dict]) -> dict:
    """Fire rates overall and per question type, plus the confusion of
    the primary cue against the weak-type label."""
    types = sorted({r["question_type"] for r in rows})
    overall = {c: sum(1 for r in rows if cue_flags(r["question"])[c])
               for c in CUE_NAMES}
    n = max(len(rows), 1)
    by_type = {}
    for t in types:
        sub = [r for r in rows if r["question_type"] == t]
        m = max(len(sub), 1)
        by_type[t] = {
            "n": len(sub),
            **{c: round(sum(1 for r in sub
                            if cue_flags(r["question"])[c]) / m, 4)
               for c in CUE_NAMES},
        }
    tp = sum(1 for r in rows if r["question_type"] in WEAK_TYPES
             and cue_flags(r["question"])[PRIMARY_CUE])
    fn = sum(1 for r in rows if r["question_type"] in WEAK_TYPES
             and not cue_flags(r["question"])[PRIMARY_CUE])
    fp = sum(1 for r in rows if r["question_type"] not in WEAK_TYPES
             and cue_flags(r["question"])[PRIMARY_CUE])
    tn = sum(1 for r in rows if r["question_type"] not in WEAK_TYPES
             and not cue_flags(r["question"])[PRIMARY_CUE])
    ku = [r for r in rows if r["question_type"] == "knowledge-update"]
    return {
        "n": len(rows),
        "fire_rate": {c: round(overall[c] / n, 4) for c in CUE_NAMES},
        "fire_count": overall,
        "by_type": by_type,
        "confusion_vs_weak_types": {
            "cue": PRIMARY_CUE,
            "weak_types": list(WEAK_TYPES),
            "weak_fired": tp, "weak_missed": fn,
            "strong_fired": fp, "strong_quiet": tn,
            "recall_on_weak": round(tp / max(tp + fn, 1), 4),
            "precision_for_weak": round(tp / max(tp + fp, 1), 4),
            "knowledge_update_fire_rate": round(
                sum(1 for r in ku
                    if cue_flags(r["question"])[PRIMARY_CUE])
                / max(len(ku), 1), 4),
        },
    }


def analyze(rows: list[dict], *, draws: int = 10_000, seed: int = 0) -> dict:
    weak = [r for r in rows if r["question_type"] in WEAK_TYPES]
    types = sorted({r["question_type"] for r in rows})
    out: dict = {
        "source_rows": "evals/results/"
                       "longmemeval-all-oracle-qwen-27b-aggp1-variants-0803"
                       ".jsonl",
        "n": len(rows),
        "draws": draws,
        "seed": seed,
        "base_arm": BASE_ARM,
        "control_arm": CONTROL_ARM,
        "weak_types": list(WEAK_TYPES),
        "primary_cue": PRIMARY_CUE,
        "cue_detectors": {
            "module": "pseudolife_memory.memory.cms",
            "functions": ["has_temporal_cue", "has_aggregation_cue",
                          "has_date_cue"],
            "note": "imported, never re-implemented; 'any' mirrors the "
                    "chronicle-serving gate in service.py",
        },
        "provenance": {
            "run_tag": "aggp1-variants-0803",
            "run_date": "2026-08-03",
            "judge": "qwen3.6 (retired 2026-08-17)",
            "replicates": 1,
            "new_answer_calls": 0,
            "new_judge_calls": 0,
            "caveat": "offline composite of already-judged per-arm "
                      "verdicts; a gated knob would still need its own "
                      "judged run before shipping",
        },
        "cues": cue_report(rows),
        "variants": {},
    }
    for arm in VARIANTS:
        fired = [r for r in rows
                 if cue_flags(r["question"])[PRIMARY_CUE]]
        quiet = [r for r in rows
                 if not cue_flags(r["question"])[PRIMARY_CUE]]
        entry = {
            "noise_control": noise_floor(rows, arm),
            "context_effect": {
                "all": context_effect(rows, arm),
                "cue_fired": context_effect(fired, arm),
            },
            "split": {
                "all": paired(rows, arm, draws=draws, seed=seed),
                "cue_fired": paired(fired, arm, draws=draws, seed=seed),
                "cue_quiet": paired(quiet, arm, draws=draws, seed=seed),
            },
            "split_weak_types": {
                "all": paired(weak, arm, draws=draws, seed=seed),
                "cue_fired": paired(
                    [r for r in weak if cue_flags(r["question"])[PRIMARY_CUE]],
                    arm, draws=draws, seed=seed),
                "cue_quiet": paired(
                    [r for r in weak
                     if not cue_flags(r["question"])[PRIMARY_CUE]],
                    arm, draws=draws, seed=seed),
            },
            "split_by_type": {},
            "gated": {
                "overall": gated_arm(rows, arm, PRIMARY_CUE,
                                     draws=draws, seed=seed),
                "weak_types": gated_arm(weak, arm, PRIMARY_CUE,
                                        draws=draws, seed=seed),
            },
            "gated_by_cue": {
                c: {
                    "overall_acc": _acc(
                        [gated_correct(r, arm, c) for r in rows]),
                    "weak_acc": _acc(
                        [gated_correct(r, arm, c) for r in weak]),
                } for c in CUE_NAMES
            },
        }
        for t in types:
            sub = [r for r in rows if r["question_type"] == t]
            sub_f = [r for r in sub if cue_flags(r["question"])[PRIMARY_CUE]]
            sub_q = [r for r in sub
                     if not cue_flags(r["question"])[PRIMARY_CUE]]
            entry["split_by_type"][t] = {
                "all": paired(sub, arm, draws=draws, seed=seed),
                "cue_fired": paired(sub_f, arm, draws=draws, seed=seed),
                "cue_quiet": paired(sub_q, arm, draws=draws, seed=seed),
            }
        out["variants"][arm] = entry
    out["verdict"] = _verdict(out)
    return out


def _verdict(report: dict) -> dict:
    """Does gating rescue contiguity?  The bar, set before looking: the
    gated composite must be >= vanilla hybrid on the weak types AND not
    worse overall."""
    g = report["variants"]["hybrid_ctg"]["gated"]
    weak_ok = g["weak_types"]["gated_acc"] >= g["weak_types"]["hybrid_acc"]
    overall_ok = g["overall"]["gated_acc"] >= g["overall"]["hybrid_acc"]
    return {
        "arm": "hybrid_ctg",
        "cue": PRIMARY_CUE,
        "weak_types_not_worse": weak_ok,
        "overall_not_worse": overall_ok,
        "rescued": bool(weak_ok and overall_ok),
        "cue_fire_rate": report["cues"]["fire_rate"][PRIMARY_CUE],
        "weak_cue_fired_delta":
            report["variants"]["hybrid_ctg"]["split_weak_types"]
            ["cue_fired"]["delta"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", default=str(DEFAULT_ROWS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--draws", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rows = load_rows(Path(args.rows))
    report = analyze(rows, draws=args.draws, seed=args.seed)
    out = Path(args.out)
    if out.exists():
        raise SystemExit(
            f"{out} exists -- never overwrite a canonical result file; "
            "tag the rerun and promote deliberately")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    v = report["verdict"]
    print(f"cue '{v['cue']}' fires on {v['cue_fire_rate']:.1%} of questions")
    for arm in VARIANTS:
        g = report["variants"][arm]["gated"]
        print(f"{arm:12s} gated overall {g['overall']['gated_acc']:.3f} "
              f"(hybrid {g['overall']['hybrid_acc']:.3f}, "
              f"ungated {g['overall']['ungated_acc']:.3f})  "
              f"weak {g['weak_types']['gated_acc']:.3f} "
              f"(hybrid {g['weak_types']['hybrid_acc']:.3f}, "
              f"ungated {g['weak_types']['ungated_acc']:.3f})")
    print(f"contiguity rescued by gating: {v['rescued']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
