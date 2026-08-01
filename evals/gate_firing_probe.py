"""Literal-gate firing-rate probe — real extractions, real corpora.

The literal-faithfulness gate shipped default ``log`` because it never
fired on any measured bench arm (``evals/results/literal-fidelity-
verdict.json``): every corpus was small and saturated. This probe measures
the firing rate at scale — the shipped extraction prompt over LongMemEval
haystack sessions (production note shape, ``[date] role: content``), each
claim checked with ``literal_violations`` under BOTH corpus scopes:

  * ``batch``  — the shipped default (union of the session's notes)
  * ``source`` — the cited note only (the stricter scope the design doc
    rejected for its derived-sum/cross-note false-drop classes)

The scope gap on real data is the measured cost of the batch default; the
batch firing rate is the evidence the ``enforce`` decision was waiting on.
Unjudged, so the fast server config is acceptable for a 27B teacher run;
the shipped sidecar at :8081 is the decision-relevant extractor.

    PYTHONPATH=. python evals/gate_firing_probe.py --out-tag e4b-ft
    PYTHONPATH=. python evals/gate_firing_probe.py \
        --extractor-url http://127.0.0.1:1234/v1 --model teacher \
        --out-tag qwen-27b

Writes ``evals/results/gate-firing-<tag>.json`` (refuses to overwrite).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.memory.dream import (                     # noqa: E402
    OpenAICompatExtractor, hard_literals, literal_violations,
)

DATA_DIR = Path(__file__).resolve().parent / "data"
DATASET = DATA_DIR / "longmemeval_s_cleaned.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
MAX_EXAMPLES = 60


def _parse_date(raw: str) -> datetime:
    cleaned = re.sub(r"\s*\(\w+\)\s*", " ", raw or "").strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return datetime.min


def iter_sessions(limit: int):
    """Distinct haystack sessions in stable order, KU haystacks excluded
    (keeps every KU eval clean forever, same guard as distill_datagen)."""
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    ku = [q for q in data if q["question_type"] == "knowledge-update"]
    forbidden = {sid for q in ku for sid in q["haystack_session_ids"]}
    seen: set[str] = set()
    n = 0
    for q in sorted((q for q in data
                     if q["question_type"] != "knowledge-update"),
                    key=lambda q: q["question_id"]):
        sessions = sorted(
            zip(q["haystack_dates"], q["haystack_session_ids"],
                q["haystack_sessions"]),
            key=lambda tpl: _parse_date(tpl[0]))
        for date, sid, session in sessions:
            if sid in forbidden or sid in seen:
                continue
            seen.add(sid)
            notes = [f"[{date}] {t['role']}: {t['content'].strip()}"
                     for t in session if (t.get("content") or "").strip()]
            if not notes:
                continue
            yield sid, notes
            n += 1
            if n >= limit:
                return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--extractor-url", default="http://127.0.0.1:8081/v1")
    ap.add_argument("--model", default="extractor")
    ap.add_argument("--limit-sessions", type=int, default=400)
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--max-tokens", type=int, default=2048,
                    help="shipped daemon default (dreaming.md)")
    args = ap.parse_args()

    out_path = RESULTS_DIR / f"gate-firing-{args.out_tag}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists — pick a fresh --out-tag "
                         "(never overwrite a canonical result file)")

    ex = OpenAICompatExtractor(args.extractor_url, args.model,
                               max_tokens=args.max_tokens,
                               timeout_seconds=600.0)
    tally = {"sessions": 0, "failures": 0, "claims": 0, "claims_cited": 0,
             "claims_with_gateable_literals": 0,
             "flagged_batch": 0, "flagged_source_only": 0}
    examples: list[dict] = []
    for sid, notes in iter_sessions(args.limit_sessions):
        try:
            claims = ex.extract(notes, vocab=[])
        except Exception as exc:  # noqa: BLE001 — probe records, not dies
            tally["failures"] += 1
            print(f"  extract failed for {sid}: {str(exc)[:120]}", flush=True)
            continue
        tally["sessions"] += 1
        batch_text = "\n".join(notes)
        for c in claims:
            value = str(c.get("value", ""))
            tally["claims"] += 1
            src = c.get("source")
            cited = isinstance(src, int) and 0 <= src < len(notes)
            if cited:
                tally["claims_cited"] += 1
            if not hard_literals(value):
                continue
            tally["claims_with_gateable_literals"] += 1
            bad_batch = literal_violations(value, batch_text)
            bad_source = (literal_violations(value, notes[src])
                          if cited else [])
            if bad_batch:
                tally["flagged_batch"] += 1
            elif bad_source:
                # passes the shipped batch scope, would drop under source
                # scope — the measured false-drop cost of the strict scope.
                tally["flagged_source_only"] += 1
            if (bad_batch or bad_source) and len(examples) < MAX_EXAMPLES:
                examples.append({
                    "session": sid,
                    "slot": f"{c.get('entity')}.{c.get('attribute')}",
                    "value": value[:200],
                    "violations_batch": bad_batch,
                    "violations_source_only": bad_source if not bad_batch else [],
                })
        if tally["sessions"] % 20 == 0:
            print(f"[{tally['sessions']}/{args.limit_sessions}] "
                  f"claims={tally['claims']} "
                  f"flagged_batch={tally['flagged_batch']} "
                  f"source_only={tally['flagged_source_only']}", flush=True)

    g = max(tally["claims_with_gateable_literals"], 1)
    out = {
        "probe": "gate-firing",
        "extractor_url": args.extractor_url,
        "model": args.model,
        "max_tokens": args.max_tokens,
        "dataset": "longmemeval_s_cleaned (non-KU haystack sessions)",
        **tally,
        "flagged_batch_share_of_gateable": round(tally["flagged_batch"] / g, 4),
        "source_only_share_of_gateable": round(
            tally["flagged_source_only"] / g, 4),
        "examples": examples,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "examples"},
                     indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
