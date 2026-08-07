"""Run T gate evaluation for the evlora campaign.

Reads the artifacts the campaign launcher produced and scores gates
T1-T3 of docs/superpowers/specs/2026-08-07-evlora-antisuppression-design.md:

  T1a  ladder: e4b-v3 gold_recoverable within 0.02 of e4b-v2, and
       stale_leak not worse than v2 by more than 0.02.
  T1b  KU-oracle cortex: v3 replicate mean within 0.02 of v2's
       (5 replicates each; the paired compare artifact is recorded).
  T2   quantity smoke passed.
  T3   capacity spot passed (student >= half of the Opus-covered
       instances).

Writes evals/results/evlora-runT-verdict.json; exit 0 only if every
gate passes. Fails LOUDLY on missing artifacts or unexpected shapes -
a silent skip would read as a pass at wake.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
TAG = "evlora-0807"
MARGIN = 0.02


def load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"GATE ERROR: missing artifact {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def rung_metric(d: dict, key: str, name: str) -> float:
    if key in d:
        return float(d[key])
    for sub in ("metrics", "result"):
        if isinstance(d.get(sub), dict) and key in d[sub]:
            return float(d[sub][key])
    sys.exit(f"GATE ERROR: no '{key}' in ladder artifact {name}")


def main() -> int:
    v2l = load(RESULTS / "e4b-v2.json")
    v3l = load(RESULTS / "e4b-v3.json")
    gold_v2 = rung_metric(v2l, "gold_recoverable", "e4b-v2.json")
    gold_v3 = rung_metric(v3l, "gold_recoverable", "e4b-v3.json")
    stale_v2 = rung_metric(v2l, "stale_leak", "e4b-v2.json")
    stale_v3 = rung_metric(v3l, "stale_leak", "e4b-v3.json")
    t1a = (gold_v3 >= gold_v2 - MARGIN) and (stale_v3 <= stale_v2 + MARGIN)

    def agg_for(extractor: str) -> dict:
        hits = sorted(RESULTS.glob(
            f"*oracle*{extractor}*{TAG}*.agg.json"))
        if not hits:
            sys.exit(f"GATE ERROR: no replicate agg for {extractor}")
        return json.loads(hits[0].read_text(encoding="utf-8"))

    agg2, agg3 = agg_for("e4b-v2"), agg_for("e4b-v3")
    cor2 = float(agg2["arms"]["cortex"]["mean"])
    cor3 = float(agg3["arms"]["cortex"]["mean"])
    n2, n3 = agg2["n_replicates"], agg3["n_replicates"]
    if n2 < 5 or n3 < 5:
        sys.exit(f"GATE ERROR: expected 5 replicates, got v2={n2} v3={n3}")
    t1b = cor3 >= cor2 - MARGIN

    t2 = bool(load(
        RESULTS / "events-quantity-smoke-evlora-t2-e4b-v3.json")["pass"])
    t3d = load(RESULTS / "evlora-capacity-spot-e4b-v3.json")
    t3 = bool(t3d["pass"])

    verdict = {
        "date": "2026-08-07",
        "preregistration":
            "docs/superpowers/specs/2026-08-07-evlora-antisuppression-design.md",
        "tag": TAG,
        "gates": {
            "T1a_ladder": {
                "gold_recoverable": {"v2": gold_v2, "v3": gold_v3},
                "stale_leak": {"v2": stale_v2, "v3": stale_v3},
                "margin": MARGIN, "pass": t1a,
                "artifacts": ["evals/results/e4b-v2.json",
                              "evals/results/e4b-v3.json"],
            },
            "T1b_ku_cortex": {
                "v2_mean": cor2, "v3_mean": cor3,
                "replicates": {"v2": n2, "v3": n3},
                "margin": MARGIN, "pass": t1b,
                "compare_artifact":
                    "evals/results/compare-evlora-t1-cortex-pairs.json",
            },
            "T2_quantity_smoke": {"pass": t2},
            "T3_capacity_spot": {
                "student": t3d["student_covered_total"],
                "opus": t3d["opus_covered_total"], "pass": t3},
        },
        "pass": t1a and t1b and t2 and t3,
    }
    out = RESULTS / "evlora-runT-verdict.json"
    out.write_text(json.dumps(verdict, indent=1), encoding="utf-8")
    for g, d in verdict["gates"].items():
        print(f"{g}: {'PASS' if d['pass'] else 'FAIL'}")
    print(f"Run T -> {'PASS' if verdict['pass'] else 'FAIL'} -> {out.name}")
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
