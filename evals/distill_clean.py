"""Cleaning pass over distill-extract.jsonl (audit: 2026-07-06).

The 50-row stratified hand audit found three label pathologies worth
filtering before SFT; everything else (including prompt-compliant world
facts and genuine empty rows) is kept verbatim:

1. ECHO KEYS (~10.6% of claims): vocab-hint feedback loops stack the
   entity into the attribute ("user . user.user-user-activity"). The
   student must not learn these; and once a damaged key enters the hint
   stream the teacher keeps echoing it. Claims whose attribute contains
   "user-user" or starts with "user." are dropped.
2. SPAM FILLS: degenerate rows where the teacher filled many hinted slots
   with one identical irrelevant value (one row had 40+ copies of the same
   sentence at confidence 0.1). Any value shared by >=5 claims within one
   row has ALL its claims dropped.
3. MEGA-ROWS: outputs with >30 claims are assistant-listicle scrapes that
   teach very long generations — the exact behaviour that made Qwen3.5-4B
   overrun the 4096-token cap on CPU. Rows still above 30 claims after
   the two filters above are dropped whole.

Rows whose claims all came from filters 1-2 are dropped entirely (keeping
them would mislabel fact-rich sessions as empty).

    PYTHONPATH=. python evals/distill_clean.py
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
SRC = DATA / "distill-extract.jsonl"
DST = DATA / "distill-extract-clean.jsonl"
MAX_CLAIMS = 30
SPAM_VALUE_MIN = 5


def clean_row(row: dict) -> dict | None:
    target = json.loads(row["messages"][-1]["content"])
    claims = target["claims"]
    if not claims:                      # genuine empty rows stay
        return row
    vals = Counter(str(c["value"]) for c in claims)
    spam_values = {v for v, n in vals.items() if n >= SPAM_VALUE_MIN}
    kept = [c for c in claims
            if str(c["value"]) not in spam_values
            and "user-user" not in c["attribute"]
            and not c["attribute"].startswith("user.")]
    if not kept:                        # everything was poison -> drop row
        return None
    if len(kept) > MAX_CLAIMS:          # listicle scrape -> drop row
        return None
    if len(kept) == len(claims):
        return row
    row["messages"][-1]["content"] = json.dumps(
        {"claims": kept}, ensure_ascii=False)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--dst", type=Path, default=DST)
    args = ap.parse_args()
    rows = [json.loads(l) for l in args.src.open(encoding="utf-8")]
    out, dropped_rows, dropped_claims = [], 0, 0
    for r in rows:
        before = len(json.loads(r["messages"][-1]["content"])["claims"])
        cleaned = clean_row(r)
        if cleaned is None:
            dropped_rows += 1
            dropped_claims += before
            continue
        after = len(json.loads(cleaned["messages"][-1]["content"])["claims"])
        dropped_claims += before - after
        out.append(cleaned)
    with args.dst.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    total = sum(len(json.loads(r["messages"][-1]["content"])["claims"])
                for r in out)
    empty = sum(1 for r in out
                if not json.loads(r["messages"][-1]["content"])["claims"])
    print(f"{len(rows)} rows -> {len(out)} kept ({dropped_rows} dropped); "
          f"claims kept {total} ({dropped_claims} dropped); "
          f"empty rows {empty} ({empty / len(out):.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
