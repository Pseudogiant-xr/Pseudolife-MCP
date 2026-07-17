"""Replication + variance tooling over ``longmemeval_bench`` results.

The bench reports single runs as point estimates, but three runs of the
identical sonnet-5-v1 config (byte-identical contexts, temperature 0)
scored cortex 0.808/0.731/0.782 — judge-side noise wider than most
differences being decided. This module makes replication cheap and the
statistics honest:

    spawn    copy a judged JSONL, judge fields stripped, under -rN tags
    run      answer-phase every pending replicate (needs the Qwen endpoint)
    agg      aggregate replicates -> <base>.agg.json with mean +/- std
    compare  paired permutation test between two configs (by question_id)
    copy     strip-copy one file to a new tag (regression-gate fallback)
    gate-check  compare replicate means against the committed baseline
    baseline    (re)establish evals/results/regression_gate.baseline.json

Import-light by design: the bench module (and through it ladder_sweep /
torch) is imported ONLY inside ``cmd_run``. Naming/IO helpers are small
local mirrors of the bench's, kept in lockstep by tests.

Spec: docs/superpowers/specs/2026-07-18-eval-hygiene-sprint-design.md
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("rag", "cortex", "hybrid")
_JUDGE_FIELDS = {f"{arm}_{s}" for arm in ARMS
                 for s in ("response", "correct", "context_tokens")}
_REPLICA_SUFFIX = re.compile(r"-r\d+$")
BASELINE_FLOOR = 0.03
DEFAULT_BASELINE = RESULTS_DIR / "regression_gate.baseline.json"


# ── naming (mirrors longmemeval_bench.out_file — duplicated because that
#    module imports ladder_sweep/torch at module level) ────────────────────
def result_file(dataset: str, extractor: str, tag: str = "",
                results_dir: Path = RESULTS_DIR) -> Path:
    suffix = f"-{tag}" if tag else ""
    return results_dir / f"longmemeval-ku-{dataset}-{extractor}{suffix}.jsonl"


def replicate_tag(base_tag: str, i: int) -> str:
    return f"{base_tag}-r{i}" if base_tag else f"r{i}"


# ── row IO (mirrors the bench's tolerant JSONL semantics) ─────────────────
def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def is_judged(row: dict) -> bool:
    return all(f"{arm}_correct" in row for arm in ARMS)


def strip_judged(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in _JUDGE_FIELDS}
            for r in rows]


def discover(dataset: str, extractor: str, tag: str = "",
             results_dir: Path = RESULTS_DIR) -> dict[str, Path]:
    """Replicate tag -> path: the base file plus strict ``-r<digits>``
    variants. ``arm1`` never matches ``arm1-baseline`` or ``arm1-gate``."""
    base = result_file(dataset, extractor, tag, results_dir)
    stem = base.name.removesuffix(".jsonl")
    found: dict[str, Path] = {}
    if base.exists():
        found[tag] = base
    for p in sorted(results_dir.glob(stem + "-r*.jsonl")):
        rest = p.name.removesuffix(".jsonl")[len(stem):]
        if _REPLICA_SUFFIX.fullmatch(rest):
            found[f"{tag}{rest}" if tag else rest[1:]] = p
    return found


# ── statistics ────────────────────────────────────────────────────────────
def accuracy(rows: list[dict], arm: str) -> float | None:
    judged = [r for r in rows if is_judged(r)]
    if not judged:
        return None
    return sum(bool(r[f"{arm}_correct"]) for r in judged) / len(judged)


def aggregate(rows_by_tag: dict[str, list[dict]]) -> dict:
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    tags = sorted(judged)
    out = {
        "n_replicates": len(tags),
        "replicates": tags,
        "n_questions": len(judged[tags[0]]) if tags else 0,
        "arms": {},
    }
    for arm in ARMS:
        accs = [round(accuracy(judged[t], arm), 4) for t in tags]
        out["arms"][arm] = {
            "accuracies": accs,
            "mean": statistics.fmean(accs) if accs else None,
            "std": statistics.stdev(accs) if len(accs) >= 2
            else None,
        }
    return out


def question_rates(rows_by_tag: dict[str, list[dict]],
                   arm: str) -> dict[str, float]:
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    per_q: dict[str, list[bool]] = {}
    qid_sets = []
    for rows in judged.values():
        qid_sets.append({r["question_id"] for r in rows})
        for r in rows:
            per_q.setdefault(r["question_id"], []).append(
                bool(r[f"{arm}_correct"]))
    if len({frozenset(s) for s in qid_sets}) > 1:
        raise ValueError("question sets differ between replicates")
    return {q: statistics.fmean(v) for q, v in per_q.items()}


def paired_permutation(a_rates: dict[str, float], b_rates: dict[str, float],
                       n: int = 10000, seed: int = 0) -> dict:
    if set(a_rates) != set(b_rates):
        raise ValueError("question sets differ between configs")
    diffs = [a_rates[q] - b_rates[q] for q in sorted(a_rates)]
    observed = statistics.fmean(diffs)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        flipped = statistics.fmean(
            d if rng.random() < 0.5 else -d for d in diffs)
        if abs(flipped) >= abs(observed) - 1e-12:
            hits += 1
    return {"delta": round(observed, 4),
            "p_value": round((hits + 1) / (n + 1), 5),
            "n_questions": len(diffs)}


# ── regression gate ───────────────────────────────────────────────────────
def make_baseline(agg: dict, commit: str,
                  floor: float = BASELINE_FLOOR) -> dict:
    arms = {}
    for arm, a in agg["arms"].items():
        margin = max(floor, 2 * (a["std"] or 0.0))
        arms[arm] = {"mean": a["mean"], "std": a["std"],
                     "margin": round(margin, 4)}
    return {"established_at": datetime.now().isoformat(timespec="seconds"),
            "commit": commit, "n_replicates": agg["n_replicates"],
            "arms": arms}


def gate_verdict(agg: dict, baseline: dict) -> list[str]:
    failures = []
    for arm, b in baseline["arms"].items():
        cur = agg["arms"][arm]["mean"]
        if cur is None or cur < b["mean"] - b["margin"]:
            failures.append(
                f"{arm}: mean {cur} < baseline {b['mean']} - "
                f"margin {b['margin']}")
    return failures


# ── CLI ───────────────────────────────────────────────────────────────────
def cmd_run(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    if not found:
        sys.exit("no result files found")
    pending = [t for t, p in found.items()
               if any(not is_judged(r) for r in load_rows(p))]
    if not pending:
        print("nothing pending — all replicates judged")
        return 0
    print(f"pending replicates: {', '.join(pending)}")
    if args.dry_run:
        return 0
    if args.results_dir != RESULTS_DIR:
        sys.exit("run only operates on the real results dir "
                 "(the bench owns file placement)")
    from longmemeval_bench import report, run_answer  # noqa: PLC0415 — heavy
    for t in pending:
        run_answer(args.dataset, args.extractor, t)
        report(args.dataset, args.extractor, t)
    return 0


def _rates_for(args, extractor: str, tag: str, arm: str) -> tuple[dict[str, float], dict]:
    found = discover(args.dataset, extractor, tag, args.results_dir)
    rows_by_tag = {t: load_rows(p) for t, p in found.items()}
    judged = {t: rows for t, rows in rows_by_tag.items()
              if rows and all(is_judged(r) for r in rows)}
    if len(judged) < 2:
        sys.exit(f"{extractor}/{tag or '(untagged)'}: need >=2 judged "
                 f"replicates, have {len(judged)} — run spawn/run first")
    return question_rates(judged, arm), aggregate(judged)


def cmd_compare(args) -> int:
    b_extractor = args.b_extractor or args.extractor
    a_rates, a_agg = _rates_for(args, args.extractor, args.tag, args.arm)
    b_rates, b_agg = _rates_for(args, b_extractor, args.b_tag, args.arm)
    result = paired_permutation(a_rates, b_rates,
                                n=args.permutations, seed=args.seed)
    result.update({
        "arm": args.arm,
        "a": f"{args.extractor}/{args.tag or '(untagged)'}",
        "b": f"{b_extractor}/{args.b_tag or '(untagged)'}",
        "a_mean": a_agg["arms"][args.arm]["mean"],
        "a_std": a_agg["arms"][args.arm]["std"],
        "b_mean": b_agg["arms"][args.arm]["mean"],
        "b_std": b_agg["arms"][args.arm]["std"],
    })
    print(json.dumps(result))
    return 0


def cmd_gate_check(args) -> int:
    if not args.baseline.exists():
        print(f"no baseline at {args.baseline}\n"
              "establish one on a known-good tree with:\n"
              "  evals\\regression_gate.ps1 -Establish")
        sys.exit(2)
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline["arms"]
    except (ValueError, KeyError):
        print(f"invalid baseline file: {args.baseline}")
        sys.exit(2)
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    failures = gate_verdict(agg, baseline)
    if failures:
        print("REGRESSION GATE: FAIL")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"REGRESSION GATE: PASS ({agg['n_replicates']} replicates vs "
          f"baseline {baseline['commit']})")
    return 0


def cmd_baseline(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    if agg["n_replicates"] < 1:
        sys.exit("no judged replicates to establish a baseline from")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
            text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    baseline = make_baseline(agg, commit, floor=args.floor)
    args.out.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"baseline established at {args.out} (commit {commit}, "
          f"{agg['n_replicates']} replicates)")
    return 0


def _agg_path(base: Path) -> Path:
    # removesuffix, not with_suffix: extractor names contain dots.
    return base.with_name(base.name.removesuffix(".jsonl") + ".agg.json")


def _load_judged_source(args) -> list[dict]:
    src = result_file(args.dataset, args.extractor, args.tag,
                      args.results_dir)
    rows = load_rows(src)
    if not rows:
        sys.exit(f"source not found or empty: {src}")
    if not all(is_judged(r) for r in rows):
        sys.exit(f"source not fully judged: {src}")
    return rows


def cmd_spawn(args) -> int:
    rows = _load_judged_source(args)
    stripped = strip_judged(rows)
    for i in range(2, args.n + 2):
        dst = result_file(args.dataset, args.extractor,
                          replicate_tag(args.tag, i), args.results_dir)
        if dst.exists():
            print(f"exists, kept: {dst.name}")
            continue
        write_rows(dst, stripped)
        print(f"spawned: {dst.name}")
    return 0


def cmd_copy(args) -> int:
    rows = _load_judged_source(args)
    dst = result_file(args.dataset, args.extractor, args.to_tag,
                      args.results_dir)
    if dst.exists():
        sys.exit(f"refusing to overwrite: {dst}")
    write_rows(dst, strip_judged(rows))
    print(f"copied (stripped): {dst.name}")
    return 0


def cmd_agg(args) -> int:
    found = discover(args.dataset, args.extractor, args.tag,
                     args.results_dir)
    if not found:
        sys.exit("no result files found")
    agg = aggregate({t: load_rows(p) for t, p in found.items()})
    if agg["n_replicates"] == 0:
        sys.exit("no fully-judged replicates yet — run the answer phase "
                 "first (replicate.py run)")
    agg["source_files"] = [p.name for p in found.values()]
    base = result_file(args.dataset, args.extractor, args.tag,
                       args.results_dir)
    _agg_path(base).write_text(json.dumps(agg, indent=2), encoding="utf-8")
    label = f"{args.extractor}{f' [{args.tag}]' if args.tag else ''}"
    print(f"\n{args.dataset} / {label} — {agg['n_replicates']} replicates, "
          f"{agg['n_questions']} questions")
    print(f"{'arm':<10}{'mean':>8}{'std':>8}  accuracies")
    for arm in ARMS:
        a = agg["arms"][arm]
        std = f"{a['std']:.4f}" if a["std"] is not None else "-"
        print(f"{arm:<10}{a['mean']:>8.4f}{std:>8}  {a['accuracies']}")
    print(f"wrote {_agg_path(base).name}")
    return 0


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", default="oracle")
    p.add_argument("--extractor", required=True)
    p.add_argument("--tag", default="")
    p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spawn", help="create stripped replicate files")
    _common(p)
    p.add_argument("-n", type=int, default=4,
                   help="replicates to create beyond the original (r2..)")
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("copy", help="strip-copy the base file to a new tag")
    _common(p)
    p.add_argument("--to-tag", required=True)
    p.set_defaults(fn=cmd_copy)

    p = sub.add_parser("agg", help="aggregate replicates -> .agg.json")
    _common(p)
    p.set_defaults(fn=cmd_agg)

    p = sub.add_parser("run", help="answer-phase all pending replicates")
    _common(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("compare", help="paired permutation test A vs B")
    _common(p)
    p.add_argument("--b-extractor", default=None)
    p.add_argument("--b-tag", default="")
    p.add_argument("--arm", choices=ARMS, default="cortex")
    p.add_argument("--permutations", type=int, default=10000)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("gate-check", help="replicate means vs baseline")
    _common(p)
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    p.set_defaults(fn=cmd_gate_check)

    p = sub.add_parser("baseline", help="(re)establish the gate baseline")
    _common(p)
    p.add_argument("--out", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--floor", type=float, default=BASELINE_FLOOR)
    p.set_defaults(fn=cmd_baseline)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
