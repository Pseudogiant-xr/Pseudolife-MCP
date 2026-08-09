"""Staleness-policy eval (Study H3) analysis: preregistered gates + verdict.

Preregistration: docs/superpowers/specs/2026-08-09-serving-side-staleness-design.md

Reads N ``--study h3`` replicate artifacts from ``retention_interval_eval.py``
and applies the four gates exactly as preregistered:

1. **efficacy** — the arm's stale-answer rate <= 0.2 (decisively under the
   ~0.5 flags-visible floor; merely beating the textual flags is not worth a
   contract change) AND paired sign-flip permutation vs flags_visible
   p < 0.05 (10k draws, seed 0 — the compare_arms convention);
2. **no-harm** — fresh-fact answer rate within 0.02 of flags_visible AND the
   render-time structural byte-identity flag true in every replicate;
3. **recovery** (quarantine only) — the answerer recovers the quarantined
   value >= 0.9 on the explicit last-recorded question set;
4. **control** — evergreen-control answer rate unchanged at 1.0.

Fallback ladder: quarantine ships iff gates 1-3 pass; else demote competes
on gates 1-2; else nothing ships and the ``annotate`` default stands.

    PYTHONPATH=. python evals/analyze_stale_policy.py \
        --replicates evals/results/retention-interval-stalepol-0809-r*.json \
        --out evals/results/stale-policy-verdict.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_retention import paired_permutation_p  # noqa: E402

PREREG = "docs/superpowers/specs/2026-08-09-serving-side-staleness-design.md"
POLICY_ARMS = {"policy_demote": "demote", "policy_quarantine": "quarantine"}
EFFICACY_BAR = 0.2
FRESH_TOLERANCE = 0.02
RECOVERY_BAR = 0.9


def _volatile(rows: list[dict]) -> dict[str, dict]:
    return {r["entity"]: r for r in rows
            if r["freshness_class"] == "volatile"}


def analyze(replicates: list[dict]) -> dict:
    """Pure gate arithmetic over parsed replicate payloads."""
    h3s = [r["study_h3"] for r in replicates]

    arms_out: dict[str, dict] = {}
    for arm, policy in POLICY_ARMS.items():
        diffs: list[int] = []       # visible - policy per (fact, replicate)
        stale_rates, fresh_rates, vis_fresh_rates = [], [], []
        structural, other_rates = [], []
        for h3 in h3s:
            vis = _volatile(h3["arms"]["flags_visible"])
            pol = _volatile(h3["arms"][arm])
            diffs.extend(int(vis[e]["unqualified"]) - int(pol[e]["unqualified"])
                         for e in vis)
            stale_rates.append(h3["summary"][arm]["stale_answer_rate"])
            fresh_rates.append(h3["summary"][arm]["fresh_answer_rate"])
            vis_fresh_rates.append(
                h3["summary"]["flags_visible"]["fresh_answer_rate"])
            structural.append(bool(h3["fresh_payloads_identical"][policy]))
            other_rates.append(
                h3["summary"][arm].get("answered_other_rate", 0.0))

        p = paired_permutation_p(diffs)
        stale_rate = sum(stale_rates) / len(stale_rates)
        fresh_gap = abs(sum(fresh_rates) - sum(vis_fresh_rates)) / len(h3s)
        arms_out[arm] = {
            "stale_answer_rate_mean": round(stale_rate, 3),
            "stale_answer_rates": stale_rates,
            # Confound diagnostic (not a gate): how often the arm's "win"
            # was the answerer serving a DIFFERENT fact's value. A stale
            # rate bought by degradation is not compliance — report it
            # beside the effect, per the control-arm discipline.
            "answered_other_rate_mean": round(
                sum(other_rates) / len(other_rates), 3),
            "gate1_efficacy": {
                "pass": stale_rate <= EFFICACY_BAR and p < 0.05,
                "bar": EFFICACY_BAR,
                "paired_units": len(diffs),
                "discordant": sum(1 for d in diffs if d != 0),
                "permutation_p_two_sided": p,
            },
            "gate2_no_harm": {
                "pass": fresh_gap <= FRESH_TOLERANCE and all(structural),
                "fresh_gap": round(fresh_gap, 4),
                "structural_byte_identity": structural,
            },
        }

    recovery_rates = [h3["summary"]["recovery_rate"] for h3 in h3s]
    recovery_mean = sum(recovery_rates) / len(recovery_rates)
    control_rates = [h3["summary"]["control_evergreen"]["stale_answer_rate"]
                     for h3 in h3s]

    gate3 = {"pass": recovery_mean >= RECOVERY_BAR, "bar": RECOVERY_BAR,
             "recovery_rate_mean": round(recovery_mean, 3),
             "recovery_rates": recovery_rates}
    gate4 = {"pass": all(c == 1.0 for c in control_rates),
             "control_rates": control_rates}

    q = arms_out["policy_quarantine"]
    d = arms_out["policy_demote"]
    if (q["gate1_efficacy"]["pass"] and q["gate2_no_harm"]["pass"]
            and gate3["pass"] and gate4["pass"]):
        decision = {"winner": "quarantine",
                    "why": "gates 1-4 pass for value-quarantine"}
    elif (d["gate1_efficacy"]["pass"] and d["gate2_no_harm"]["pass"]
            and gate4["pass"]):
        decision = {"winner": "demote",
                    "why": "quarantine failed (gate3 recovery or another "
                           "gate); demote clears gates 1-2 per the fallback "
                           "ladder"}
    else:
        decision = {"winner": None, "default_stands": "annotate",
                    "why": "no policy arm cleared the preregistered gates — "
                           "serving-side rendering does not close the "
                           "answerer-discretion gap; route to client-side "
                           "briefing per prereg"}

    return {
        "preregistration": PREREG,
        "n_replicates": len(replicates),
        "arms": arms_out,
        "gate3_recovery": gate3,
        "gate4_control": gate4,
        "decision": decision,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--replicates", type=Path, nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    replicates = [json.loads(p.read_text(encoding="utf-8"))
                  for p in args.replicates]
    verdict = analyze(replicates)
    verdict["inputs"] = [p.name for p in args.replicates]
    args.out.write_text(json.dumps(verdict, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    print(json.dumps({"decision": verdict["decision"],
                      "q_p": verdict["arms"]["policy_quarantine"]
                                    ["gate1_efficacy"]
                                    ["permutation_p_two_sided"]}),
          flush=True)
    print(f"verdict -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
