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

from pseudolife_memory.memory.dream import OpenAICompatExtractor  # noqa: E402

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
# The variant constructions build on the OP-LESS control prompt — which was
# the shipped prompt until the 2026-08-01 hold reversal put the v5 block into
# dream._SYSTEM_PROMPT. Anchoring on the committed control file keeps every
# VARIANTS entry byte-identical to what its gate measured (pinned by
# test_op_prompt_artifact.py) regardless of what ships.
_BASE = (Path(__file__).parent / "prompts"
         / "ku_control_prompt_opless.txt").read_text(encoding="utf-8")

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

# The v5 count rule, shared verbatim by every variant that carries it so a
# later variant cannot drift from what the count gate measured.
_V5_COUNT_BLOCK = (
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
)

# The chronicle-events rule (2026-08-03 aggregation-aware-recall design,
# Phase 2): dated occurrences become first-class records beside claims.
# Same rhetorical shape as the count rule — one rule plus one single-event
# inline example, no second Output block (the v3->v5 lesson). Dates resolve
# against dates visible in the note text (LongMemEval turns carry a
# [session date] stamp); exact calendar days only, null over invention.
_EVENTS_BLOCK = (
    "ALSO EXTRACT EVENTS: when a note describes something that HAPPENED "
    "at a stated or inferable time (a trip taken, a purchase, an "
    "adoption, a start or an end — an occurrence, not a standing fact), "
    'add it to a separate top-level "events" array beside "claims": '
    '{"events":[{"description":..,"actor":..,"date":"YYYY-MM-DD",'
    '"date_phrase":<the note\'s own words about when>,"source":<number '
    "of the note>}]}. Resolve date from dates written in the note "
    "(including a leading [date] stamp); exact calendar days only — when "
    "the note's words cannot pin an exact day, set date to null and keep "
    "date_phrase verbatim. Never invent a date. For example, the note "
    "[7] [2023/05/14 (Sun) 10:02] user: we finally adopted the kitten "
    "yesterday! — yields the single event "
    '{"description":"adopted a kitten","actor":"user",'
    '"date":"2023-05-13","date_phrase":"yesterday","source":7} inside '
    "the one events array. Facts still go in claims; one note can yield "
    "both.\n"
)

# The epistemic-stance rule (2026-08-12 stance+span-gate design, Feature A:
# hedges survive consolidation as a labelled field, arXiv:2608.06953). Same
# rhetorical shape as the count rule — one rule plus one single-claim inline
# example, no second Output block (the v3->v5 lesson). The example slot is
# deliberately NOT the v0 example's deploy-target so the two never compete.
_STANCE_BLOCK = (
    "HEDGES GO IN A STANCE FIELD: when the note itself hedges a fact "
    '("probably", "might", "unconfirmed", "not final", "per the '
    'runbook"), keep the value CLEAN and put the note\'s own hedge '
    'words in a "stance" field on that claim — never inside the value, '
    "and never invent a stance the note does not carry; a plainly "
    "stated fact has no stance field. For example, the note [6] I "
    "think we'll switch the database to Postgres 18, not final yet — "
    "yields the single claim "
    '{"entity":"database","attribute":"planned version",'
    '"value":"Postgres 18","stance":"not final yet","confidence":0.6,'
    '"source":6} inside the one claims array.\n'
)

# v10 stance rule (2026-08-13 iteration, from the sgku bank-diff
# forensics): v8's block measured KU cortex −0.115 NOT through stance
# semantics but by diluting the one-slot/current-value anchor — slot
# explosions, key-format drift, frozen updates (spec gate-outcomes
# section). v10 differs in exactly two ways: (1) an explicit "a hedged
# update is STILL an update" sentence tying stance to the existing
# consolidation rule instead of standing beside it; (2) the worked
# example REUSES the v0 example's own deploy-target slot as a later
# hedged update — demonstrating same-key consolidation under a hedge,
# where v8's example deliberately used a fresh slot and thereby taught
# slot-minting next to hedges.
_STANCE_V10_BLOCK = (
    "HEDGES GO IN A STANCE FIELD: when the note itself hedges a fact "
    '("probably", "might", "unconfirmed", "not final", "per the '
    'runbook"), keep the value CLEAN and put the note\'s own hedge '
    'words in a "stance" field on that claim — never inside the value, '
    "and never invent a stance the note does not carry; a plainly "
    "stated fact has no stance field. A hedged update is STILL an "
    "update: use the same entity and attribute as the fact it changes "
    "and emit only the CURRENT value, exactly as for a plain fact. For "
    "example, a later note [6] we'll probably move the deploy target "
    "again, to eu-west-1 next quarter — updates the deploy target slot "
    "from the earlier example to the single claim "
    '{"entity":"deploy target","attribute":"environment",'
    '"value":"eu-west-1","stance":"probably","confidence":0.6,'
    '"source":6} inside the one claims array.\n'
)

# The provenance-quote rule (2026-08-12 design, Feature B: a claim must
# carry a verbatim span from its cited note — the span gate verifies
# containment at the claim loop). Same shape discipline as above.
_QUOTE_BLOCK = (
    'CITE A QUOTE: every claim also carries a "quote" field — a short '
    "span copied VERBATIM from the note the claim cites, enough to back "
    "the value. Copy the note's exact words; never paraphrase or "
    "abbreviate inside quote. For example, the note [7] the health "
    "probe now polls every 30 seconds — yields the single claim "
    '{"entity":"health probe","attribute":"poll interval",'
    '"value":"every 30 seconds","quote":"the health probe now polls '
    'every 30 seconds","confidence":0.9,"source":7} inside the one '
    "claims array.\n"
)

# The literal-fidelity rule (2026-08-02 design: consolidation must keep
# exact dates/numbers/versions/identifiers verbatim, not round or reword).
_LITERAL_BLOCK = (
    "KEEP LITERALS VERBATIM: when a fact's value contains a date, a "
    "number, a version, or an identifier, copy it EXACTLY as the note "
    "writes it — never round it, re-format it, or leave it out. For "
    "example, the note [6] the security audit is due 2026-09-30 — "
    "yields the single claim "
    '{"entity":"security audit","attribute":"due date",'
    '"value":"2026-09-30","confidence":0.9,"source":6} inside the one '
    'claims array, not "end of September".\n'
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
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK
        + 'Return {"claims":[]} if nothing qualifies.'),

    # v5 + the keep-literals-verbatim rule (dates, numbers, versions,
    # identifiers copied exactly from the note). Same rhetorical shape as
    # the count rule — one sentence plus one single-claim inline example,
    # no second Output block (the v3->v5 lesson), no mention of "op", and
    # "quantity" left to the count rule so the two never compete.
    "v6-literal-fidelity": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK + _LITERAL_BLOCK
        + 'Return {"claims":[]} if nothing qualifies.'),

    # Shipped v5 + the chronicle-events rule (Phase 2 of the 2026-08-03
    # aggregation-aware-recall design). Builds on v5, NOT v6 — the literal
    # rule failed its KU gate and never shipped. The Return-empty line
    # gains the empty events array so both keys are always present.
    "v7-chronicle-events": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK + _EVENTS_BLOCK
        + 'Return {"claims":[],"events":[]} if nothing qualifies.'),

    # Shipped v5 + the stance rule (2026-08-12 design, Feature A). Builds on
    # v5, NOT v6/v7 — neither ever shipped. Incremental by design: a gate
    # failure on this arm attributes to the stance rule alone.
    "v8-stance": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK + _STANCE_BLOCK
        + 'Return {"claims":[]} if nothing qualifies.'),

    # v8 + the quote rule (Feature B). The second increment: v9-vs-v8
    # isolates the quote field's claims tax (the v7 combined-events prompt
    # measured -0.053 on claims for exactly this class of addition).
    "v9-stance-quote": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK + _STANCE_BLOCK + _QUOTE_BLOCK
        + 'Return {"claims":[]} if nothing qualifies.'),

    # The v10 iteration: shipped v5 + the update-anchored stance rule
    # (see _STANCE_V10_BLOCK's comment for the two deliberate deltas vs
    # the KU-failed v8 block).
    "v10-stance-update": _BASE.replace(
        'Return {"claims":[]} if nothing qualifies.',
        _V0_FAILED_BLOCK + _V5_COUNT_BLOCK + _STANCE_V10_BLOCK
        + 'Return {"claims":[]} if nothing qualifies.'),
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
