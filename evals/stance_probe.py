"""Stance-retention probe — gate 1 of the 2026-08-12 stance+span-gate
design (plus the v9 arm's quote statistics for gate 4).

Matched-note protocol (arXiv:2608.06953): each battery item is a PAIR —
the same fact stated once with the note's own hedge and once plainly.
An arm (prompt variant) extracts claims from each note independently;
the probe scores, per arm:

- ``stance_capture``   — hedged notes whose target claim carries a
  non-empty ``stance`` (the RED arm, shipped v5, is expected near 0.0 —
  the field does not exist in its schema);
- ``false_stance``     — PLAIN notes whose target claim carries a stance
  the note never expressed (inventing hedges is worse than missing them);
- ``value_hedged``     — target values still containing a hedge token
  (the field must RELOCATE hedges, not duplicate them — probe-level view
  of prereg gate 3);
- ``target_recovered`` — notes whose target fact was extracted at all
  (a stance rule that costs recall fails the spirit of gate 2; the
  ladder measures this properly, this is the early tripwire);
- v9 only: ``quote_present`` / ``quote_verified`` and a reason
  histogram from ``span_unbacked`` — the extraction-level half of the
  gate-4 firing audit.

Preregistered thresholds (v8 arm): stance_capture >= 0.60 on hedged
notes AND false_stance <= 0.10 on plain notes. Exit 1 when the measured
arm misses either. Always writes
``evals/results/stance-probe-<tag>.json`` (benches persist by default).

    PYTHONPATH=. python evals/stance_probe.py --tag <date> \
        [--url http://127.0.0.1:1234/v1] [--arms v5,v8,v9]

Server note: judged/measured runs need the reproducible bench server
(dot-source evals/qwen_server.ps1; Start-Qwen — never -Fast).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Hedge tokens for the value-cleanliness check — deliberately the probe's
# own hedge vocabulary, so a value that swallowed the note's hedge is
# caught regardless of which battery item produced it.
HEDGE_TOKENS = (
    "probably", "likely", "might", "may ", "i think", "i believe",
    "not final", "unconfirmed", "not confirmed", "tentatively",
    "provisionally", "planning to", "plan to", "leaning toward",
    "hoping to", "supposedly", "apparently", "per the", "according to",
    "if all goes well", "unless", "still deciding",
)

# (id, hedged note, plain note, target-value substring, hedge words the
# hedged note carries). The pairs differ ONLY in the hedge — the matched
# protocol. Domains and hedge classes deliberately varied.
BATTERY: list[tuple[str, str, str, str, str]] = [
    ("deploy-env",
     "we'll probably move the deploy target to prod-eu next sprint",
     "we moved the deploy target to prod-eu this sprint",
     "prod-eu", "probably"),
    ("db-version",
     "I think we'll switch the database to Postgres 18, not final yet",
     "we switched the database to Postgres 18 today",
     "postgres 18", "not final yet"),
    ("ci-runner",
     "the CI runner might move to the m4 mac mini after the trial",
     "the CI runner now runs on the m4 mac mini",
     "m4 mac mini", "might"),
    ("backup-cadence",
     "per the runbook, backups are supposed to run nightly at 2am",
     "backups run nightly at 2am",
     "2am", "per the runbook"),
    ("api-timeout",
     "we're leaning toward a 45 second timeout for the ingest API",
     "the ingest API timeout is 45 seconds",
     "45", "leaning toward"),
    ("laptop",
     "I'm planning to buy the framework 16 once the refresh lands",
     "I bought the framework 16 yesterday",
     "framework 16", "planning to"),
    ("office-move",
     "the team will likely relocate to the Leeds office in November",
     "the team relocated to the Leeds office in November",
     "leeds", "likely"),
    ("cache-layer",
     "we might adopt redis for the session cache, still deciding",
     "we adopted redis for the session cache",
     "redis", "might / still deciding"),
    ("bike",
     "I'm hoping to sell the road bike this spring",
     "I sold the road bike this spring",
     "road bike", "hoping to"),
    ("monitor-brand",
     "apparently the new monitors are all dell ultrasharps now",
     "the new monitors are all dell ultrasharps",
     "dell", "apparently"),
    ("job-title",
     "Sam is supposedly moving to the platform team next quarter",
     "Sam moved to the platform team this quarter",
     "platform team", "supposedly"),
    ("license",
     "legal thinks we'll settle on Apache-2.0 for the SDK, unconfirmed",
     "the SDK license is Apache-2.0",
     "apache", "unconfirmed"),
    ("gpu-upgrade",
     "if all goes well we'll swap the 4090 for a 5090 in December",
     "we swapped the 4090 for a 5090 in December",
     "5090", "if all goes well"),
    ("meeting-day",
     "standup will probably move to Tuesdays after the reorg",
     "standup moved to Tuesdays after the reorg",
     "tuesday", "probably"),
    ("vendor",
     "procurement is leaning toward Hetzner for the new bare-metal hosts",
     "procurement chose Hetzner for the new bare-metal hosts",
     "hetzner", "leaning toward"),
    ("python-version",
     "we'll likely pin the service to Python 3.13 next cycle",
     "the service is pinned to Python 3.13",
     "3.13", "likely"),
    ("coffee-order",
     "I might switch my usual order to a flat white",
     "my usual order is a flat white now",
     "flat white", "might"),
    ("apartment",
     "we're tentatively taking the flat on Harrogate Road from March",
     "we took the flat on Harrogate Road from March",
     "harrogate road", "tentatively"),
    ("test-framework",
     "the team believes vitest will replace jest, not confirmed yet",
     "vitest replaced jest across the repo",
     "vitest", "not confirmed"),
    ("doctor",
     "the GP thinks it's probably a pulled muscle, tests pending",
     "the GP confirmed it was a pulled muscle",
     "pulled muscle", "probably / tests pending"),
    ("region",
     "the failover region will provisionally be eu-west-2",
     "the failover region is eu-west-2",
     "eu-west-2", "provisionally"),
    ("headphones",
     "I believe the office headphones are the sony xm6, need to check",
     "the office headphones are the sony xm6",
     "xm6", "I believe / need to check"),
    ("sprint-length",
     "we may shorten sprints to one week after the pilot",
     "sprints are one week long since the pilot",
     "one week", "may"),
    ("dns-provider",
     "according to the migration doc, DNS should end up on cloudflare",
     "DNS is on cloudflare now",
     "cloudflare", "according to the migration doc"),
    ("car-service",
     "the garage says the clutch probably needs replacing at 90k",
     "the garage replaced the clutch at 90k",
     "clutch", "probably"),
    ("conference",
     "I'll probably present the memory work at PyCon UK",
     "I presented the memory work at PyCon UK",
     "pycon", "probably"),
    ("storage-quota",
     "ops thinks the bank volume quota will go to 50GB, unless costs bite",
     "the bank volume quota is 50GB",
     "50", "thinks / unless costs bite"),
    ("keyboard",
     "I'm planning to switch to the keychron q3 at the office desk",
     "I switched to the keychron q3 at the office desk",
     "keychron", "planning to"),
    ("release-day",
     "the 0.14 release will likely land on August 29th",
     "the 0.14 release landed on August 29th",
     "august 29", "likely"),
    ("auth-provider",
     "we might consolidate auth on keycloak, evaluation ongoing",
     "we consolidated auth on keycloak",
     "keycloak", "might / evaluation ongoing"),
    ("diet",
     "I think I'll go vegetarian on weekdays, trying it out",
     "I eat vegetarian on weekdays",
     "vegetarian", "I think / trying it out"),
    ("branch-policy",
     "per the contributing guide, feature branches should rebase not merge",
     "feature branches rebase, never merge",
     "rebase", "per the contributing guide"),
    ("printer",
     "facilities will supposedly replace the printer with a brother laser",
     "facilities replaced the printer with a brother laser",
     "brother", "supposedly"),
    ("holiday",
     "we're hoping to spend two weeks in Portugal in September",
     "we spent two weeks in Portugal in September",
     "portugal", "hoping to"),
    ("embedding-model",
     "the next embedding backbone is provisionally qwen3-embedding-4b",
     "the embedding backbone is qwen3-embedding-4b",
     "qwen3-embedding", "provisionally"),
    ("desk",
     "I may move my desk to the window side after Priya leaves",
     "I moved my desk to the window side",
     "window", "may"),
    ("kube-version",
     "the cluster will probably upgrade to kubernetes 1.34 this month",
     "the cluster upgraded to kubernetes 1.34",
     "1.34", "probably"),
    ("insurance",
     "the broker believes the premium will come in around 1200 a year",
     "the premium is 1200 a year",
     "1200", "believes / around"),
    ("logging",
     "we're leaning toward structured JSON logs for the daemon",
     "the daemon writes structured JSON logs",
     "json", "leaning toward"),
    ("pet",
     "we might adopt a second cat once the kitten settles",
     "we adopted a second cat",
     "cat", "might"),
]

ARM_PROMPTS = {
    "v5": "v5-count-exclusion-claim-example",
    "v8": "v8-stance",
    "v9": "v9-stance-quote",
    "v10": "v10-stance-update",
}

CAPTURE_FLOOR = 0.60      # preregistered: v8 stance capture on hedged notes
FALSE_STANCE_CEIL = 0.10  # preregistered: v8 false stance on plain notes


def _target_claims(claims: list[dict], substr: str) -> list[dict]:
    return [c for c in claims if substr in str(c.get("value", "")).lower()]


def run_arm(name: str, prompt: str, url: str, model: str) -> dict:
    from pseudolife_memory.memory.dream import (OpenAICompatExtractor,
                                                span_unbacked)

    ex = OpenAICompatExtractor(
        url, model, max_tokens=4096, timeout_seconds=600.0,
        system_prompt=prompt,
        # Bench determinism: warm-server prompt cache changes temperature-0
        # output (results/warm-cache-probe-0809.json) — same pin as the
        # ladder's extractor.
        extra_body={"cache_prompt": False})
    out = {
        "hedged": {"n": 0, "recovered": 0, "stance": 0, "value_hedged": 0},
        "plain": {"n": 0, "recovered": 0, "stance": 0, "value_hedged": 0},
        "quotes": {"claims": 0, "present": 0, "verified": 0,
                   "reasons": {}},
        "errors": 0,
        "items": [],
    }
    for fid, hedged_note, plain_note, substr, hedge_words in BATTERY:
        for kind, note in (("hedged", hedged_note), ("plain", plain_note)):
            bucket = out[kind]
            bucket["n"] += 1
            try:
                claims = ex.extract([note], vocab=[])
            except Exception as e:  # noqa: BLE001 — probe records, not dies
                out["errors"] += 1
                out["items"].append({"id": fid, "kind": kind,
                                     "error": str(e)[:200]})
                continue
            claims = [c for c in claims if c.get("kind") != "event"]
            targets = _target_claims(claims, substr)
            rec = {"id": fid, "kind": kind, "n_claims": len(claims),
                   "recovered": bool(targets)}
            if targets:
                bucket["recovered"] += 1
                t = targets[0]
                stance = (t.get("stance") or "").strip()
                rec["stance"] = stance or None
                rec["value"] = t.get("value")
                if stance:
                    bucket["stance"] += 1
                low = str(t.get("value", "")).lower()
                if any(h in low for h in HEDGE_TOKENS):
                    bucket["value_hedged"] += 1
                    rec["value_hedged"] = True
            # Quote stats over EVERY claim the arm emitted (span-gate
            # firing audit is claim-level, not target-level).
            if name == "v9":
                for c in claims:
                    out["quotes"]["claims"] += 1
                    q = c.get("quote")
                    if q:
                        out["quotes"]["present"] += 1
                    reason = span_unbacked(q, note)
                    if reason is None:
                        out["quotes"]["verified"] += 1
                    else:
                        out["quotes"]["reasons"][reason] = \
                            out["quotes"]["reasons"].get(reason, 0) + 1
            out["items"].append(rec)
    h, p = out["hedged"], out["plain"]
    out["metrics"] = {
        "stance_capture": (h["stance"] / h["recovered"]
                           if h["recovered"] else 0.0),
        "false_stance": (p["stance"] / p["recovered"]
                         if p["recovered"] else 0.0),
        "hedged_recovered": h["recovered"] / h["n"] if h["n"] else 0.0,
        "plain_recovered": p["recovered"] / p["n"] if p["n"] else 0.0,
        "hedged_value_hedged": (h["value_hedged"] / h["recovered"]
                                if h["recovered"] else 0.0),
    }
    if name == "v9" and out["quotes"]["claims"]:
        q = out["quotes"]
        out["metrics"]["quote_present_rate"] = q["present"] / q["claims"]
        out["metrics"]["quote_verified_rate"] = q["verified"] / q["claims"]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--model", default="Qwen3.6-27B-UD-Q4_K_XL.gguf")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--arms", default="v5,v8,v9")
    ap.add_argument("--gate-arm", default="v8",
                    help="arm the preregistered thresholds gate (exit 1)")
    args = ap.parse_args(argv)

    import op_probe  # noqa: PLC0415 — prompt constructions, pinned

    result = {"tag": args.tag, "url": args.url, "model": args.model,
              "battery_pairs": len(BATTERY), "started_at": time.time(),
              "thresholds": {"stance_capture_min": CAPTURE_FLOOR,
                             "false_stance_max": FALSE_STANCE_CEIL},
              "arms": {}}
    for arm in args.arms.split(","):
        arm = arm.strip()
        prompt = op_probe.VARIANTS[ARM_PROMPTS[arm]]
        print(f"[stance-probe] arm {arm} ({ARM_PROMPTS[arm]}) ...")
        result["arms"][arm] = run_arm(arm, prompt, args.url, args.model)
        m = result["arms"][arm]["metrics"]
        print(f"[stance-probe]   capture={m['stance_capture']:.2f} "
              f"false={m['false_stance']:.2f} "
              f"recovered h/p={m['hedged_recovered']:.2f}"
              f"/{m['plain_recovered']:.2f}")
    result["finished_at"] = time.time()

    gate = result["arms"].get(args.gate_arm)
    ok = None
    if gate:
        m = gate["metrics"]
        ok = (m["stance_capture"] >= CAPTURE_FLOOR
              and m["false_stance"] <= FALSE_STANCE_CEIL)
        result["gate"] = {"arm": args.gate_arm, "passed": ok}
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"stance-probe-{args.tag}.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[stance-probe] wrote {out_path}")
    if ok is not None:
        print(f"[stance-probe] GATE {'PASS' if ok else 'FAIL'} "
              f"(arm {args.gate_arm})")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
