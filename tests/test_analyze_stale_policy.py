"""analyze_stale_policy: pure decision-logic tests — no GPU, no network.

Pins the preregistered gate arithmetic and the fallback ladder from
docs/superpowers/specs/2026-08-09-serving-side-staleness-design.md:

1. efficacy — stale rate <= 0.2 AND paired sign-flip vs flags_visible
   p < 0.05;
2. no-harm — fresh answer rate within 0.02 of flags_visible AND the
   structural byte-identity flag true in every replicate;
3. recovery (quarantine only) — >= 0.9 on the last-recorded question set;
4. control — evergreen answer rate 1.0.

Fallback ladder: quarantine ships iff gates 1-3 pass; else demote competes
on gates 1-2; else nothing ships and the annotate default stands.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import analyze_stale_policy as asp  # noqa: E402

VOLATILE = [f"vol-{i}" for i in range(10)]
SLOW = [f"slow-{i}" for i in range(10)]


def _arm(name, stale_unq, fresh_unq=True):
    rows = []
    for e in VOLATILE:
        rows.append({"entity": e, "freshness_class": "volatile",
                     "unqualified": stale_unq})
    for e in SLOW:
        rows.append({"entity": e, "freshness_class": "slow",
                     "unqualified": fresh_unq})
    return rows


def _replicate(demote_unq=False, quar_unq=False, recovery=1.0,
               structural=True, control_rate=1.0, fresh_unq=True):
    n_rec = 10
    rec_hits = round(recovery * n_rec)
    return {"study_h3": {
        "fresh_payloads_identical": {"demote": structural,
                                     "quarantine": structural},
        "arms": {
            "flags_visible": _arm("flags_visible", True, fresh_unq),
            "policy_demote": _arm("policy_demote", demote_unq, fresh_unq),
            "policy_quarantine": _arm("policy_quarantine", quar_unq,
                                      fresh_unq),
            "control_evergreen": _arm("control_evergreen", control_rate == 1.0),
        },
        "recovery": [{"entity": e, "recovered": i < rec_hits}
                     for i, e in enumerate(VOLATILE)],
        "summary": {
            "flags_visible": {"stale_answer_rate": 1.0,
                              "fresh_answer_rate": 1.0},
            "policy_demote": {"stale_answer_rate": float(demote_unq),
                              "fresh_answer_rate": 1.0},
            "policy_quarantine": {"stale_answer_rate": float(quar_unq),
                                  "fresh_answer_rate": 1.0},
            "control_evergreen": {"stale_answer_rate": control_rate,
                                  "fresh_answer_rate": control_rate},
            "recovery_rate": recovery,
        },
    }}


def test_quarantine_wins_when_all_gates_pass():
    verdict = asp.analyze([_replicate() for _ in range(3)])
    q = verdict["arms"]["policy_quarantine"]
    assert q["gate1_efficacy"]["pass"] is True
    assert q["gate1_efficacy"]["permutation_p_two_sided"] < 0.05
    assert q["gate2_no_harm"]["pass"] is True
    assert verdict["gate3_recovery"]["pass"] is True
    assert verdict["gate4_control"]["pass"] is True
    assert verdict["decision"]["winner"] == "quarantine"


def test_recovery_failure_falls_back_to_demote():
    verdict = asp.analyze([_replicate(recovery=0.5) for _ in range(3)])
    assert verdict["gate3_recovery"]["pass"] is False
    assert verdict["decision"]["winner"] == "demote"
    assert "gate3" in verdict["decision"]["why"]


def test_no_arm_clears_efficacy_means_no_ship():
    # Both policies leave the stale rate at the flags-visible level.
    verdict = asp.analyze(
        [_replicate(demote_unq=True, quar_unq=True) for _ in range(3)])
    assert verdict["decision"]["winner"] is None
    assert verdict["decision"]["default_stands"] == "annotate"


def test_structural_violation_fails_no_harm_even_with_good_rates():
    verdict = asp.analyze([_replicate(structural=False) for _ in range(3)])
    assert verdict["arms"]["policy_quarantine"]["gate2_no_harm"]["pass"] is False
    assert verdict["decision"]["winner"] is None


def test_efficacy_bar_is_02_not_just_beating_flags():
    # Stale rate 0.3: clearly better than 1.0 but above the 0.2 bar —
    # per prereg, merely tying/beating the textual flags is not enough.
    reps = []
    for _ in range(3):
        r = _replicate()
        arms = r["study_h3"]["arms"]
        for i, row in enumerate(arms["policy_quarantine"]):
            if row["freshness_class"] == "volatile":
                row["unqualified"] = i % 10 < 3
        r["study_h3"]["summary"]["policy_quarantine"]["stale_answer_rate"] = 0.3
        reps.append(r)
    verdict = asp.analyze(reps)
    assert verdict["arms"]["policy_quarantine"]["gate1_efficacy"]["pass"] is False
