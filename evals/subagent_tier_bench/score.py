"""Score subagent tier bench runs.

usage: python score.py <results-dir> [--briefs briefs.json] [--out results.json]

<results-dir> holds one answer per run, named ``<brief>.<model>.<rep>.md``,
and beside each a ``.json`` with ``{"tokens": N, "seconds": S}`` as the
harness reported them. A run scores the fraction of its brief's key regexes
that match (case-insensitive, DOTALL) minus the number of trap regexes
that match; each is also listed so a miss can be read. The output is one
table per model with mean recall, trap count, tokens and seconds, and the
per-run rows, written to --out and printed.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def score(answer: str, brief: dict) -> dict:
    flags = re.IGNORECASE | re.DOTALL
    hit = [k for k in brief["keys"] if re.search(k, answer, flags)]
    miss = [k for k in brief["keys"] if k not in hit]
    traps = [t for t in brief.get("traps", []) if re.search(t, answer, flags)]
    return {"recall": len(hit) / len(brief["keys"]), "hit": len(hit), "of": len(brief["keys"]),
            "missed": miss, "traps": traps, "chars": len(answer)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("results", type=Path)
    p.add_argument("--briefs", type=Path, default=HERE / "briefs.json")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    briefs = {b["id"]: b for b in json.loads(args.briefs.read_text(encoding="utf-8"))["briefs"]}
    runs = []
    for path in sorted(args.results.glob("*.md")):
        brief_id, model, rep = path.stem.split(".")
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        s = score(path.read_text(encoding="utf-8"), briefs[brief_id])
        runs.append({"brief": brief_id, "model": model, "rep": int(rep), **meta, **s})
    by_model = defaultdict(list)
    for r in runs:
        by_model[r["model"]].append(r)
    summary = {}
    for model, rows in sorted(by_model.items()):
        summary[model] = {
            "runs": len(rows),
            "recall_mean": round(statistics.mean(r["recall"] for r in rows), 3),
            "recall_min": round(min(r["recall"] for r in rows), 3),
            "traps_total": sum(len(r["traps"]) for r in rows),
            "tokens_mean": round(statistics.mean(r["tokens"] for r in rows)),
            "tokens_total": sum(r["tokens"] for r in rows),
            "seconds_mean": round(statistics.mean(r["seconds"] for r in rows)),
            "answer_chars_mean": round(statistics.mean(r["chars"] for r in rows)),
        }
    print(f"{'model':8s} {'runs':>4s} {'recall':>7s} {'min':>5s} {'traps':>5s} {'tokens':>8s} {'sec':>5s} {'chars':>6s}")
    for model, s in summary.items():
        print(f"{model:8s} {s['runs']:4d} {s['recall_mean']:7.3f} {s['recall_min']:5.2f} "
              f"{s['traps_total']:5d} {s['tokens_mean']:8d} {s['seconds_mean']:5d} {s['answer_chars_mean']:6d}")
    print()
    for r in sorted(runs, key=lambda r: (r["brief"], r["model"], r["rep"])):
        flag = " TRAP:" + "; ".join(r["traps"]) if r["traps"] else ""
        print(f"{r['brief']:16s} {r['model']:8s} rep{r['rep']} {r['hit']:2d}/{r['of']:<2d} "
              f"{r['tokens']:7d} tok {r['seconds']:4d} s{flag}")
        for m in r["missed"]:
            print(f"    missed: {m}")
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "runs": runs}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
