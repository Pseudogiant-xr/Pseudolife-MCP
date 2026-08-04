"""Differential inertness comparison between two extract-only LME runs.

The committed producer for ``inert-diff-*-verdict.json`` claims (a gate
that cannot be reproduced is not a gate). Given two runs of the same
question set — e.g. pre-change code vs post-change code, same prompt,
same reproducible server — this compares, per question:

  1. contexts (rag / cortex / hybrid) — byte equality (fact lines render
     dates at day granularity, so same-day runs are byte-stable);
  2. consolidation tallies (``extract_seconds`` dropped — wall-clock);
  3. dumped fact banks — semantic equality after dropping wall-clock
     fields, keeping entity / attribute / value / kind / status /
     confidence / support and the ordered history chain. Bank dumps are
     gitignored, so the bank leg only runs when both roots still hold
     them (fresh runs); the committed jsonl artifacts carry the
     context + tally legs durably.

First used 2026-08-04 to clear the schema-v28 deploy gate: 14 questions
(12 temporal-reasoning + 2 supersession-heavy KU) through e1c4954a
(pre-v28) and master, zero differences — the deploy-time substitute for
a full ladder-conformance rung on an inert-path change (spec
2026-08-03-aggregation-aware-recall-design.md amendment).

Usage:
  python evals/inert_diff.py --old-root <worktree> --old-tag inert-old-0804 \
      --new-root . --new-tag inert-new-0804
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from pathlib import Path

VOLATILE = {"asserted_at", "age", "tx_time", "valid_time", "hlc_phys",
            "hlc_logical", "stale", "last_confirmed", "writer_id",
            "session_id", "version", "id", "db_id", "embedding"}


def rows_by_qid(root: Path, tag: str) -> dict:
    matches = glob.glob(str(root / "evals" / "results" / f"*{tag}.jsonl"))
    if len(matches) != 1:
        raise SystemExit(f"expected one artifact for {tag}, got {matches}")
    out = {}
    for line in Path(matches[0]).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["question_id"]] = r
    return out


def banks_by_qid(root: Path, tag: str) -> dict:
    out = {}
    for p in glob.glob(str(root / "evals" / "results" / "banks"
                           / f"*{tag}*" / "*.json.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        out[payload["question_id"]] = payload["facts"]
    return out


def norm_fact(f: dict) -> str:
    return json.dumps({k: v for k, v in f.items() if k not in VOLATILE},
                      sort_keys=True, ensure_ascii=False)


def tally(row: dict) -> dict:
    t = dict(row.get("consolidation") or {})
    t.pop("extract_seconds", None)          # wall-clock, not semantic
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old-root", required=True)
    ap.add_argument("--old-tag", required=True)
    ap.add_argument("--new-root", required=True)
    ap.add_argument("--new-tag", required=True)
    args = ap.parse_args()
    old_root, new_root = Path(args.old_root), Path(args.new_root)

    old_rows = rows_by_qid(old_root, args.old_tag)
    new_rows = rows_by_qid(new_root, args.new_tag)
    old_banks = banks_by_qid(old_root, args.old_tag)
    new_banks = banks_by_qid(new_root, args.new_tag)
    qids = sorted(old_rows)
    if qids != sorted(new_rows):
        raise SystemExit(f"qid sets differ: {qids} vs {sorted(new_rows)}")
    banks_compared = bool(old_banks) and bool(new_banks)

    diffs = []
    for qid in qids:
        o, n = old_rows[qid], new_rows[qid]
        for arm in ("rag", "cortex", "hybrid"):
            if o["contexts"].get(arm) != n["contexts"].get(arm):
                diffs.append(f"{qid}: context[{arm}] differs")
        if tally(o) != tally(n):
            diffs.append(f"{qid}: consolidation tallies differ "
                         f"{tally(o)} vs {tally(n)}")
        if banks_compared:
            ob = sorted(norm_fact(f) for f in old_banks.get(qid, []))
            nb = sorted(norm_fact(f) for f in new_banks.get(qid, []))
            if ob != nb:
                only_old = [f for f in ob if f not in set(nb)]
                only_new = [f for f in nb if f not in set(ob)]
                diffs.append(f"{qid}: bank differs ({len(only_old)} "
                             f"only-old, {len(only_new)} only-new)")
                diffs.extend(f"    only-old: {f[:200]}" for f in only_old[:3])
                diffs.extend(f"    only-new: {f[:200]}" for f in only_new[:3])

    if diffs:
        print(f"NOT INERT — {len(diffs)} difference(s) over "
              f"{len(qids)} questions:")
        print("\n".join(diffs))
        return 1
    n_facts = sum(len(v) for v in new_banks.values())
    legs = ("3 contexts + tallies"
            + (f" + {n_facts} bank facts each side" if banks_compared
               else " (banks absent — jsonl legs only)"))
    print(f"INERT: zero differences over {len(qids)} questions ({legs})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
