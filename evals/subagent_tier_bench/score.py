"""Score subagent tier bench runs.

usage: python score.py <results-dir> [--briefs briefs.json] [--out <tagged>.json]

<results-dir> holds one answer per run, named ``<brief>.<model>.<rep>.md``
(the last two dot-separated parts are the model and the replicate; the
brief id may hold dots), and beside each a ``.json`` with ``{"tokens": N,
"seconds": S, "tool_uses": T}`` copied from the harness's per-agent usage
line. A run reports two metrics, kept separate: recall, the fraction of its
brief's key regexes that match (case-insensitive, DOTALL), and traps, the
trap regexes that match. Neither is adjusted by the other. Each miss is
listed so it can be read. The output is one table per model with mean
recall, trap count, tokens, tool uses and seconds, and the per-run rows.

--out writes the scored table; name it for the round, never an existing
canonical file. A missing or empty results directory, an answer without
metadata, or a brief id not in briefs.json is an error (exit 2), not an
empty table.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
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


def load_runs(results: Path, briefs: dict) -> list[dict]:
    if not results.is_dir():
        raise SystemExit(f"score: {results} is not a directory")
    runs = []
    for path in sorted(results.glob("*.md")):
        parts = path.stem.rsplit(".", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            raise SystemExit(f"score: {path.name} is not named <brief>.<model>.<rep>.md")
        brief_id, model, rep = parts
        if brief_id not in briefs:
            raise SystemExit(f"score: {path.name} names a brief not in briefs.json: {brief_id}")
        meta_path = path.with_suffix(".json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit(f"score: {path.name} has no metadata file {meta_path.name}") from None
        for field in ("tokens", "seconds"):
            if not isinstance(meta.get(field), (int, float)):
                raise SystemExit(f"score: {meta_path.name} lacks a numeric {field!r}")
        meta.setdefault("tool_uses", None)
        runs.append({"brief": brief_id, "model": model, "rep": int(rep), **meta,
                     **score(path.read_text(encoding="utf-8"), briefs[brief_id])})
    if not runs:
        raise SystemExit(f"score: no <brief>.<model>.<rep>.md answers under {results}")
    return runs


def summarize(runs: list[dict]) -> dict:
    by_model = defaultdict(list)
    for r in runs:
        by_model[r["model"]].append(r)
    summary = {}
    for model, rows in sorted(by_model.items()):
        tool_uses = [r["tool_uses"] for r in rows if isinstance(r["tool_uses"], (int, float))]
        summary[model] = {
            "runs": len(rows),
            "recall_mean": round(statistics.mean(r["recall"] for r in rows), 3),
            "recall_min": round(min(r["recall"] for r in rows), 3),
            "traps_total": sum(len(r["traps"]) for r in rows),
            "tokens_mean": round(statistics.mean(r["tokens"] for r in rows)),
            "tokens_total": round(sum(r["tokens"] for r in rows)),
            "tool_uses_mean": round(statistics.mean(tool_uses), 1) if tool_uses else None,
            "seconds_mean": round(statistics.mean(r["seconds"] for r in rows)),
            "answer_chars_mean": round(statistics.mean(r["chars"] for r in rows)),
        }
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("results", type=Path)
    p.add_argument("--briefs", type=Path, default=HERE / "briefs.json")
    p.add_argument("--out", type=Path, default=None, help="scored table to write (a new, tagged file)")
    args = p.parse_args(argv)
    briefs = {b["id"]: b for b in json.loads(args.briefs.read_text(encoding="utf-8"))["briefs"]}
    runs = load_runs(args.results, briefs)
    summary = summarize(runs)
    print(f"{'model':8s} {'runs':>4s} {'recall':>7s} {'min':>5s} {'traps':>5s} {'tokens':>8s} "
          f"{'tools':>5s} {'sec':>5s} {'chars':>6s}")
    for model, s in summary.items():
        tools = "-" if s["tool_uses_mean"] is None else f"{s['tool_uses_mean']:.1f}"
        print(f"{model:8s} {s['runs']:4d} {s['recall_mean']:7.3f} {s['recall_min']:5.2f} "
              f"{s['traps_total']:5d} {s['tokens_mean']:8d} {tools:>5s} {s['seconds_mean']:5d} "
              f"{s['answer_chars_mean']:6d}")
    print()
    for r in sorted(runs, key=lambda r: (r["brief"], r["model"], r["rep"])):
        flag = " TRAP:" + "; ".join(r["traps"]) if r["traps"] else ""
        print(f"{r['brief']:16s} {r['model']:8s} rep{r['rep']} {r['hit']:2d}/{r['of']:<2d} "
              f"{r['tokens']:7.0f} tok {r['seconds']:4.0f} s{flag}")
        for m in r["missed"]:
            print(f"    missed: {m}")
    if args.out:
        if args.out.exists():
            print(f"score: {args.out} exists; name a new file for this round", file=sys.stderr)
            return 2
        args.out.write_text(json.dumps({"summary": summary, "runs": runs}, indent=1) + "\n",
                            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
