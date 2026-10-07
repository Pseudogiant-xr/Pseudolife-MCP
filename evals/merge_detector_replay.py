"""Replay labelled merge-proposal verdicts through the filing-time vetoes
and the dream-alias cosine threshold: which false proposals would never
have been filed, and which true duplicates would have been lost?

Input is PRIVATE (it carries entity names from a memory bank) and lives
under the gitignored ``evals/data/``: ``{"rows": [{id, from, into, reason,
similarity, verdict}]}`` where ``verdict`` is accept / reject / leave
(leave = unlabelled). The output artifact is scrubbed: per-row ids,
detector, score, verdict and veto outcome, plus the summary tables — no
names.

Usage:
    python evals/merge_detector_replay.py \\
        [--data evals/data/merge-triage-20260929.json] \\
        [--out evals/results/merge-detector-replay-20260929.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.graph_consolidation import (  # noqa: E402
    variant_conflict,
)
from pseudolife_memory.memory.graph_review import merge_veto  # noqa: E402

DATA = Path(__file__).parent / "data" / "merge-triage-20260929.json"
OUT = Path(__file__).parent / "results" / "merge-detector-replay-20260929.json"
THRESHOLDS = (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9)
VERDICTS = ("accept", "reject", "leave")


def detector(reason: str) -> str:
    return str(reason or "").split(":", 1)[0].strip() or "unknown"


def veto_reason(a: str, b: str) -> str | None:
    """What the alias screen now refuses: the shared name-shape vetoes, or
    a size/quant/version conflict (the check it already applied)."""
    return merge_veto(a, b) or ("variant-conflict" if variant_conflict(a, b)
                                else None)


def tally(rows) -> dict:
    c = collections.Counter(r["verdict"] for r in rows)
    return {v: c.get(v, 0) for v in VERDICTS} | {"n": len(rows)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    doc = json.loads(args.data.read_text(encoding="utf-8"))

    rows = []
    for r in doc["rows"]:
        rows.append({"id": r["id"], "detector": detector(r["reason"]),
                     "similarity": r.get("similarity"),
                     "verdict": r["verdict"],
                     "veto": veto_reason(r["from"], r["into"])})

    by_detector = {}
    for det in sorted({r["detector"] for r in rows}):
        mine = [r for r in rows if r["detector"] == det]
        by_detector[det] = {
            "all": tally(mine),
            "vetoed": tally([r for r in mine if r["veto"]]),
            "passes_veto": tally([r for r in mine if not r["veto"]])}

    alias = [r for r in rows if r["detector"] == "dream-alias" and not r["veto"]]
    curve = []
    for t in THRESHOLDS:
        kept = [r for r in alias if (r["similarity"] or 0) >= t]
        curve.append({"min_cosine": t, "filed": tally(kept),
                      "accepts_lost": sum(1 for r in alias
                                          if r["verdict"] == "accept"
                                          and (r["similarity"] or 0) < t)})

    out = {"built_by": "evals/merge_detector_replay.py",
           "data": args.data.name,
           "labels": doc.get("provenance"),
           "by_detector": by_detector,
           "dream_alias_threshold_curve_after_veto": curve,
           "rows": rows}
    args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"by_detector": by_detector,
                      "curve": curve}, indent=1))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
