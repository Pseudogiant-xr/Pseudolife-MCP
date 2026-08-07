"""Run A of the 2026-08-06 events quantity+coverage design: deterministic
extraction smoke for a candidate events prompt.

Feeds synthetic quantity-bearing notes (archetypes drawn from
evals/results/events-coverage-audit-0806.json) through ``extract_events``
under both the shipped v1 prompt and a candidate prompt, then checks:

  1. QUANTITY RETENTION — every seeded quantity string appears verbatim in
     some candidate-extracted event description.
  2. PARITY — every non-quantity probe event extracted under v1 has a
     counterpart under the candidate (keyword match), so the added rule
     does not suppress ordinary occurrences.

Writes ``evals/results/events-quantity-smoke-<tag>.json`` and exits 0 only
if both checks pass. Needs the reproducible qwen server on :1234
(``Start-Qwen`` — judged-adjacent gate output, never ``-Fast``).

Usage (repo root):
  PYTHONPATH=. python evals/events_quantity_smoke.py \
      --candidate evals/prompts/events_pass_v2.txt --tag evq-0806
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
QWEN_URL = "http://127.0.0.1:1234/v1"

# Synthetic notes: each seeds quantities the audit showed the v1 prompt
# strips, plus two quantity-free occurrences for the parity check.
NOTES = [
    "[2024/05/29 (Wed) 09:12] user: I sold 15 jars of jam at the market "
    "today, earning $225!",
    "[2024/05/21 (Tue) 18:40] user: I just completed my first full "
    "marathon in 4h 22min.",
    "[2024/05/03 (Fri) 08:05] user: I missed my train by 5 minutes and "
    "had to take a taxi, which cost me $12.",
    "[2024/04/22 (Mon) 20:15] user: back home — the Japan trip ran from "
    "April 15th to 22nd and I loved every day.",
    "[2024/05/11 (Sat) 16:30] user: did a 5-mile hike at Red Rock Canyon "
    "this morning.",
    "[2024/05/13 (Mon) 10:02] user: we finally adopted the kitten "
    "yesterday!",
    "[2024/05/18 (Sat) 12:00] user: I tried out a new Ethiopian "
    "restaurant in town and loved it.",
]
# Verbatim strings that must survive into candidate event descriptions.
QUANTITIES = ["15 jars", "$225", "4h 22min", "$12", "April 15th to 22nd",
              "5-mile"]
# Quantity-free occurrences: keyword that must appear under BOTH prompts.
PARITY_KEYWORDS = ["kitten", "Ethiopian"]


def _extract(prompt_file: str | None, url: str = QWEN_URL) -> list[dict]:
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    events_prompt = (Path(prompt_file).read_text(encoding="utf-8")
                     if prompt_file else None)
    ex = OpenAICompatExtractor(url, "bench", max_tokens=4096,
                               timeout_seconds=600.0,
                               events_prompt=events_prompt)
    return ex.extract_events(NOTES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True,
                    help="candidate events prompt file (e.g. events_pass_v2.txt)")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--extractor-url", default=QWEN_URL,
                    help="extraction endpoint; default = the qwen bench "
                         "server. Point at :8081 to smoke a sidecar "
                         "candidate (evlora gate T2).")
    args = ap.parse_args()

    v1_events = _extract(None, args.extractor_url)
    cand_events = _extract(args.candidate, args.extractor_url)
    v1_text = " | ".join(e.get("description", "") for e in v1_events)
    cand_text = " | ".join(e.get("description", "") for e in cand_events)

    missing = [q for q in QUANTITIES if q not in cand_text]
    parity_missing = [k for k in PARITY_KEYWORDS
                      if k.lower() in v1_text.lower()
                      and k.lower() not in cand_text.lower()]
    ok = not missing and not parity_missing

    out = {
        "tag": args.tag,
        "candidate": args.candidate,
        "server": args.extractor_url,
        "checks": {
            "quantities_expected": QUANTITIES,
            "quantities_missing_in_candidate": missing,
            "parity_keywords": PARITY_KEYWORDS,
            "parity_missing_in_candidate": parity_missing,
        },
        "v1_events": v1_events,
        "candidate_events": cand_events,
        "pass": ok,
    }
    out_path = RESULTS_DIR / f"events-quantity-smoke-{args.tag}.json"
    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"v1 events: {len(v1_events)}, candidate events: {len(cand_events)}")
    print(f"quantities missing: {missing or 'none'}")
    print(f"parity missing: {parity_missing or 'none'}")
    print(f"{'PASS' if ok else 'FAIL'} — wrote {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
