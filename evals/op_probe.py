#!/usr/bin/env python
"""Extractor op-adoption probe (dev-only, unjudged).

The C2 gate found the ceiling extractor never adopts the claim-level
``op`` field from an appended prompt block (0/78 banks, 0/4 direct probe;
``evals/results/c2-gate-verdict.json``), so the block was held out of the
shipped prompt. This probe iterates PROMPT FORMATS cheaply — a fixed note
battery with known membership/decoy structure, one extractor call per
variant, no judge — to find a format the extractor demonstrably adopts
before anything expensive is pre-registered.

Scores per variant:
  * adoption   — fraction of expected-op claims carrying the correct op
                 ("add"/"remove" on membership notes).
  * decoy_ok   — fraction of decoy value-update claims correctly WITHOUT
                 a membership op (plain claim, or op:"set" under variants
                 whose schema requires an op on every claim).
  * valid_json — extractor returned parseable claims for the battery.

Output is never judged — the fast server config is acceptable here; the
winning variant must be re-confirmed on the reproducible config before
any gate run. Writes ``evals/results/op-probe-<tag>.json`` (benches
persist by default).

Usage (repo root, extractor at :1234):
  PYTHONPATH=. python evals/op_probe.py --tag fast
  PYTHONPATH=. python evals/op_probe.py --tag q8 --variants v1,v3
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.memory.dream import (  # noqa: E402
    OpenAICompatExtractor, _SYSTEM_PROMPT)

# ── note battery ──────────────────────────────────────────────────────────
# (text, expected) — expected maps a value-substring to the op it must
# carry: "add" | "remove" | None (decoy: must NOT carry a membership op).
BATTERY: list[tuple[str, dict[str, str | None]]] = [
    ("user: Tried a new Korean place tonight — Seoul Garden. Third Korean "
     "restaurant I've tried in town now.",
     {"seoul garden": "add"}),
    ("user: Picked up a gravel bike this weekend, so the stable is road, "
     "commuter, and now gravel.",
     {"gravel": "add"}),
    ("user: Added 'fix the fence' to the weekend list, alongside 'clean "
     "gutters'.",
     {"fence": "add", "gutters": "add"}),
    ("user: Finally finished 'The Nightingale' — adding it to the books "
     "I've read this year.",
     {"nightingale": "add"}),
    ("user: Sold the road bike today. Still keeping the gravel and the "
     "commuter.",
     {"road bike": "remove"}),
    ("user: Crossed 'clean gutters' off the weekend list — done.",
     {"gutters": "remove"}),
    ("user: I switched jobs last month — I'm at Meridian Labs now, not "
     "Northwind.",
     {"meridian": None}),
    ("user: We moved from Austin to Portland in the spring.",
     {"portland": None}),
    ("user: Bumped the deploy target from staging to prod-eu.",
     {"prod-eu": None}),
    # Count-update decoys — the mechanism the c2op-guard gate measured
    # (count/total updates re-routed into op:"add", freezing the total;
    # evals/results/c2op-guard-verdict.json). A running total that moved
    # must land as a PLAIN claim carrying the new number, never op:"add".
    ("user: Spotted a Northern Flicker today — that brings me to 32 "
     "different bird species seen at my local park, up from 27.",
     {"32": None}),
    ("user: My Instagram just crossed 1300 followers, up from 1250 last "
     "month.",
     {"1300": None}),
    ("user: Watched three more Crash Course videos this week, so that's 15 "
     "in the past few weeks total.",
     {"15": None}),
    ("user: Added two shows tonight, which makes 25 titles on my to-watch "
     "list now.",
     {"25": None}),
]

# ── prompt variants ───────────────────────────────────────────────────────
_BASE = _SYSTEM_PROMPT  # shipped, measurement-clean, no op mention

_V0_FAILED_BLOCK = (
    "When a note adds or removes an item from a COLLECTION the user "
    'maintains (restaurants tried, bikes owned, pending tasks), add an '
    '"op":"add" or "op":"remove" field to that claim instead of a plain '
    "supersede. op is ONLY for collection membership — a value that simply "
    "changed (a new job, a moved city) stays a plain claim with no op. "
    "Example. Notes: [3] tried Rosa's Diner tonight. [4] sold the road bike, "
    'no longer biking to work. Output: {"claims":['
    '{"entity":"user","attribute":"restaurants tried","value":"Rosa\'s '
    'Diner","op":"add","confidence":0.8,"source":3},'
    '{"entity":"user","attribute":"bikes owned","value":"road bike",'
    '"op":"remove","confidence":0.8,"source":4}]}\n'
)

VARIANTS: dict[str, str] = {
    # The block that failed the C2 gate — the control arm of this probe.
    "v0-appended-block": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + 'Return {"claims":[]} if nothing qualifies.'),

    # op REQUIRED on every claim; "set" is the plain-fact value. Adoption
    # becomes structural: the schema line and both worked examples carry it.
    "v1-required-op": (
        "You consolidate numbered notes into canonical facts. Extract "
        'durable, current-state facts as JSON: {"claims":[{"entity":..,'
        '"attribute":..,"value":..,"op":"set"|"add"|"remove",'
        '"confidence":0..1,"source":<number of the note the fact came '
        'from>}]}. op is "set" for a normal fact or an updated value, '
        '"add" when the note adds an item to a collection the user '
        'maintains (restaurants tried, bikes owned, pending tasks), '
        '"remove" when an item leaves one. One slot per real fact; skip '
        "narrative, opinions, and obsolete states. When several notes "
        "state or update the SAME fact, use one consistent entity and "
        "attribute and emit only the CURRENT value. Reuse existing slot "
        "keys when they fit.\n"
        "Example. Notes: [1] we moved the deploy target from staging to "
        "prod-eu. [2] tried Rosa's Diner tonight. [3] sold the road bike. "
        'Output: {"claims":['
        '{"entity":"deploy target","attribute":"environment",'
        '"value":"prod-eu","op":"set","confidence":0.9,"source":1},'
        '{"entity":"user","attribute":"restaurants tried",'
        '"value":"Rosa\'s Diner","op":"add","confidence":0.8,"source":2},'
        '{"entity":"user","attribute":"bikes owned","value":"road bike",'
        '"op":"remove","confidence":0.8,"source":3}]}\n'
        'Return {"claims":[]} if nothing qualifies.'),

    # op optional, but woven into the PRIMARY worked example instead of a
    # trailing block (models mimic the first example's shape).
    "v2-integrated-example": _BASE.replace(
        "Example. Notes: [1] we moved the deploy target from staging to "
        "prod-eu. ",
        'A claim may carry "op":"add" or "op":"remove" when a note adds or '
        "removes an item from a collection the user maintains; plain value "
        "changes carry no op.\n"
        "Example. Notes: [1] we moved the deploy target from staging to "
        "prod-eu, and I tried Rosa's Diner tonight. ").replace(
        '{"entity":"deploy target","attribute":"environment",'
        '"value":"prod-eu","confidence":0.9,"source":1},',
        '{"entity":"deploy target","attribute":"environment",'
        '"value":"prod-eu","confidence":0.9,"source":1},'
        '{"entity":"user","attribute":"restaurants tried",'
        '"value":"Rosa\'s Diner","op":"add","confidence":0.8,"source":1},'),

    # v0 block + count exclusion — the targeted fix for the mechanism the
    # c2op-guard gate isolated: under v0 the extractor re-routes count/total
    # UPDATES into op:"add" member claims, freezing the stated total at its
    # first value. The rule names counts as never-members and shows the
    # count-update shape as a worked example.
    "v3-count-exclusion": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK +
        "COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a note "
        "states or updates how many of something the user has (a running "
        "count, a total, a follower number, a quantity), emit a plain claim "
        'whose value is the NEW number, with no "op" field — even when the '
        "note also mentions the item that changed the count. Example. "
        "Notes: [5] saw a Northern Flicker today, that makes 32 species at "
        'the park now. Output: {"claims":[{"entity":"user","attribute":'
        '"bird species seen at park","value":"32","confidence":0.9,'
        '"source":5}]}\n'
        'Return {"claims":[]} if nothing qualifies.'),

    # v3 recovered 5/7 frozen-total banks in targeted extraction but its
    # THIRD standalone example object induced multi-object JSON output
    # (Extra-data parse retries). v4 carries the same rule sentence with the
    # count-update case folded INTO the v0 block's existing example — same
    # object count as v0, no new output shape shown.
    "v4-count-exclusion-integrated": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK.replace(
            "a value that simply "
            "changed (a new job, a moved city) stays a plain claim with no "
            "op. ",
            "a value that simply "
            "changed (a new job, a moved city) stays a plain claim with no "
            "op. COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a "
            "note states or updates how many of something the user has (a "
            "running count, a total, a follower number), emit a plain claim "
            'whose value is the NEW number, with no "op" field — even when '
            "the note also names the item that changed the count. ",
        ).replace(
            "[4] sold the road bike, "
            "no longer biking to work. ",
            "[4] sold the road bike, "
            "no longer biking to work. [5] saw a Northern Flicker today, "
            "that makes 32 species at the park for me now. ",
        ).replace(
            '{"entity":"user","attribute":"bikes owned","value":"road bike",'
            '"op":"remove","confidence":0.8,"source":4}]}\n',
            '{"entity":"user","attribute":"bikes owned","value":"road bike",'
            '"op":"remove","confidence":0.8,"source":4},'
            '{"entity":"user","attribute":"bird species seen at park",'
            '"value":"32","confidence":0.9,"source":5}]}\n',
        ) + 'Return {"claims":[]} if nothing qualifies.'),

    # v3's dedicated example drove its recovery (5/7 frozen-total banks vs
    # v4's 4/7) but its standalone Output block induced multi-object JSON.
    # v5 keeps the dedicated count example, rendered as a single claim
    # object inside the one claims array — no second Output block shown.
    "v5-count-exclusion-claim-example": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK +
        "COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a note "
        "states or updates how many of something the user has (a running "
        "count, a total, a follower number, a quantity), emit a plain claim "
        'whose value is the NEW number, with no "op" field — even when the '
        "note also names the item that changed the count. For example, the "
        "note [5] saw a Northern Flicker today, that makes 32 species at "
        "the park now — yields the single claim "
        '{"entity":"user","attribute":"bird species seen at park",'
        '"value":"32","confidence":0.9,"source":5} inside the one claims '
        'array, and NO "op":"add" claim for Northern Flicker.\n'
        'Return {"claims":[]} if nothing qualifies.'),
}


def score(claims: list[dict], expected: dict[str, str | None],
          required_op: bool) -> tuple[int, int, int, int]:
    """(adopt_hit, adopt_total, decoy_hit, decoy_total) for one note."""
    a_hit = a_tot = d_hit = d_tot = 0
    for substr, want in expected.items():
        matches = [c for c in claims
                   if substr in str(c.get("value", "")).lower()]
        got_ops = {c.get("op") for c in matches}
        if want in ("add", "remove"):
            a_tot += 1
            if matches and got_ops == {want}:
                a_hit += 1
        else:
            d_tot += 1
            ok = {"set"} if required_op else {None, "set"}
            if matches and got_ops <= ok:
                d_hit += 1
    return a_hit, a_tot, d_hit, d_tot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    args = ap.parse_args()

    out = {"tag": args.tag, "battery_notes": len(BATTERY), "variants": {}}
    for name in args.variants.split(","):
        prompt = VARIANTS[name]
        ex = OpenAICompatExtractor(args.url, "op-probe", max_tokens=4096,
                                   system_prompt=prompt)
        a_hit = a_tot = d_hit = d_tot = 0
        per_note = []
        for i, (note, expected) in enumerate(BATTERY):
            try:
                claims = ex.extract([f"[{i}] {note}"], vocab=[])
            except Exception as e:  # noqa: BLE001 — probe records, not dies
                per_note.append({"note": i, "error": str(e)[:200]})
                continue
            claims = [c if isinstance(c, dict) else c.__dict__ for c in claims]
            h = score(claims, expected, required_op=name.startswith("v1"))
            a_hit += h[0]; a_tot += h[1]; d_hit += h[2]; d_tot += h[3]
            per_note.append({"note": i, "claims": claims})
        out["variants"][name] = {
            "adoption": round(a_hit / a_tot, 3) if a_tot else None,
            "decoy_ok": round(d_hit / d_tot, 3) if d_tot else None,
            "adopt_frac": f"{a_hit}/{a_tot}", "decoy_frac": f"{d_hit}/{d_tot}",
            "per_note": per_note,
        }
        print(f"{name:24s} adoption {a_hit}/{a_tot}  decoy_ok {d_hit}/{d_tot}")

    path = Path(__file__).parent / "results" / f"op-probe-{args.tag}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
