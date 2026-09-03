#!/usr/bin/env python
"""Regenerate the write-time label heuristic audit (schema v35).

Measures ``pseudolife_memory.memory.labels`` against a bank dump and writes
``evals/results/label-heuristic-audit-<date>.json`` — counts, hit rates and
hand-verdict precision only; NO bank text is written (the live bank carries
identifiers). "Every bench writes a file" (CLAUDE.md); this is that file's
writer, so the numbers quoted in CHANGELOG / labels.py are regenerable
rather than hand-entered.

Inputs (dev-only; the dump is never committed):

  --dsn        read-only SELECT of current entries + facts straight from a
               bank (default: the Docker-tier bench server DSN); or
  --entries / --facts   JSONL dumps ({"text"} / {"entity","attribute","value"});
               --facts alone audits a facts-only dump (a BEAM bank dump has
               no entries table) and reports the entry metrics as null
  --verdicts   the committed hand-verdict file: sha1-12 of
               "entity\\x1fattribute\\x1fvalue" for every fact hit of the
               AUDITED SUPERSET rule that a human judged a genuine rule
               (evals/results/label-heuristic-audit-20260902.verdicts.json)
  --out        artifact path (refuses to overwrite unless --force: tag a
               rerun, promote deliberately)

The audited superset is the deliberately LOOSE prototype rule the
2026-09-02 hand-labelling was done over — strong deontic | rule framing |
never/always/do-not imperative ANYWHERE | attribute name in
{rule, constraint, policy, requirement, invariant, discipline}; text <= 400
chars — reproduced here verbatim so a rerun on the same bank scores the
same 215 hits. Every variant is scored as the fraction of ITS hits whose
verdict hash is in the verdict set; hits with no verdict are reported as
unaudited, never counted as genuine.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pseudolife_memory.memory.labels import (  # noqa: E402
    _FRAMING, _STRONG, infer_authority, infer_distortion_tolerance)

RESULTS = ROOT / "evals" / "results"
DEFAULT_DSN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory"
CAP = 400

# ── the audited superset (prototype v4), verbatim ─────────────────────────
_P_STRONG = re.compile(
    r"\b(must(?:\s+not|n't)?|shall(?:\s+not)?|forbidden|prohibited|non-negotiable|"
    r"no exceptions|under no circumstances|hard rule|standing (?:rule|instruction)|"
    r"mandatory)\b", re.I)
_P_FRAMING = re.compile(r"(?:^|\n)\W*(rule|constraint|policy|invariant|hard rule|"
                        r"standing rule|non-negotiable|mandatory)\s*[:\-—]", re.I)
_P_IMPERATIVE = re.compile(
    r"\b(never|always|do not|don't|do NOT|DO NOT)\s+([A-Za-z][A-Za-z\-]*)", re.I)
_P_NOT_BARE = {"be", "been", "being", "was", "were", "is", "are", "am", "had", "has",
               "have", "did", "does", "do", "a", "an", "the", "to", "of", "in", "on",
               "at", "for", "with", "before", "after", "again", "available", "this",
               "that", "these", "those", "it", "its", "my", "your", "our", "their",
               "any", "all", "one", "once", "ever", "yet", "quite", "fully", "really",
               "actually", "just", "only", "even", "also", "not", "resident", "visible",
               "reached", "gone", "true", "false", "up", "down", "out"}
_P_FIRST_PERSON = re.compile(r"\bi (?:always|never|usually|prefer|like|tend to)\b", re.I)
_P_ATTR = re.compile(r"(^|-)(rules?|constraints?|polic(?:y|ies)|requirements?|"
                     r"invariants?|discipline|prohibition)($|-)")
_LOOSE = re.compile(r"\b(must|never|always|do not|don't|shall|forbidden|prohibited|"
                    r"mandatory)\b", re.I)


def _p_imperative(t: str) -> bool:
    for m in _P_IMPERATIVE.finditer(t):
        v = m.group(2).lower()
        if v in _P_NOT_BARE or v.endswith(("ed", "en", "ing", "ly")):
            continue
        if v.endswith("s") and not v.endswith("ss"):
            continue
        return True
    return False


def superset_hit(value: str, attribute: str | None) -> bool:
    t = value or ""
    if _P_FIRST_PERSON.search(t) or len(t) > CAP:
        return False
    if attribute and _P_ATTR.search(attribute.lower().replace("_", "-").replace(" ", "-")):
        return True
    return bool(_P_FRAMING.search(t) or _P_STRONG.search(t) or _p_imperative(t))


def verdict_key(f: dict) -> str:
    raw = "\x1f".join((f["entity"], f["attribute"], f["value"]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ── inputs ────────────────────────────────────────────────────────────────
def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def dump_bank(dsn: str) -> tuple[list[dict], list[dict]]:
    import psycopg  # noqa: PLC0415 — dev-only path
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT text, source FROM entries WHERE superseded_at IS NULL ORDER BY id")
        entries = [{"text": t, "source": s} for t, s in cur.fetchall()]
        cur.execute("SELECT entity, attribute, value FROM facts WHERE status = 'current'")
        facts = [{"entity": e, "attribute": a, "value": v} for e, a, v in cur.fetchall()]
    return entries, facts


# ── scoring ───────────────────────────────────────────────────────────────
def score(hits: list[dict], genuine: set[str]) -> dict:
    judged = [f for f in hits if verdict_key(f) in genuine]
    return {"fact_hits": len(hits), "judged_genuine": len(judged),
            "precision": round(len(judged) / len(hits), 2) if hits else None}


def audit(entries: list[dict], facts: list[dict], genuine: set[str],
          date: str) -> dict:
    superset = [f for f in facts if superset_hit(f["value"], f["attribute"])]
    audited = {verdict_key(f) for f in superset}
    shipped = [f for f in facts if infer_distortion_tolerance(f["value"]) == "constraint"]
    strong = [f for f in shipped if _STRONG.search(f["value"]) or _FRAMING.search(f["value"])]
    opener = [f for f in shipped if f not in strong]
    imperative_anywhere = [f for f in superset if _p_imperative(f["value"])
                           and not (_P_STRONG.search(f["value"]) or _P_FRAMING.search(f["value"]))]
    attr_only = [f for f in superset
                 if not (_P_FRAMING.search(f["value"]) or _P_STRONG.search(f["value"])
                         or _p_imperative(f["value"]))]
    entry_hits = [e for e in entries if infer_distortion_tolerance(e["text"]) == "constraint"]
    n_entries = len(entries) or None   # facts-only dump: entry rates are null
    unaudited = [f for f in shipped if verdict_key(f) not in audited]
    auth_e = {}
    auth_e_ungated = {}
    for e in entries:
        k = infer_authority(e["text"]); auth_e[str(k)] = auth_e.get(str(k), 0) + 1
        k = infer_authority(e["text"], max_chars=10 ** 9)
        auth_e_ungated[str(k)] = auth_e_ungated.get(str(k), 0) + 1
    auth_f = {}
    for f in facts:
        k = infer_authority(f["value"]); auth_f[str(k)] = auth_f.get(str(k), 0) + 1
    return {
        "date": date,
        "generated_by": "evals/label_heuristic_audit.py",
        "what": ("Precision audit of the auto label heuristics "
                 "(pseudolife_memory/memory/labels.py) against a bank dump; counts and "
                 "hand verdicts only, no bank text."),
        "sample": {"current_entries": len(entries), "current_facts": len(facts)},
        "method": ("Every fact hit of the AUDITED SUPERSET rule (strong deontic | rule "
                   "framing | never/always/do-not imperative anywhere | attribute name in "
                   "{rule,constraint,policy,requirement,invariant,discipline}; value <= 400 "
                   "chars) was hand-labelled genuine-rule vs descriptive; the verdict file "
                   "holds the sha1-12 keys of the genuine ones. Each variant is scored as "
                   "the fraction of ITS hits judged genuine; shipped hits outside the "
                   "audited set are reported as unaudited, never as genuine."),
        "distortion_tolerance_variants": {
            "loose_any_deontic_word_anywhere_no_cap": {
                "entry_hits": sum(1 for e in entries if _LOOSE.search(e["text"])),
                "entry_hit_rate": (round(sum(1 for e in entries if _LOOSE.search(e["text"])) / n_entries, 4)
                                   if n_entries else None),
                "fact_hits": sum(1 for f in facts if _LOOSE.search(f["value"])),
                "note": "rejected: status narratives with one 'must'"},
            "audited_superset_cap400": {**score(superset, genuine), "note": "the hand-labelled set"},
            "imperative_never_always_do_not_anywhere": {**score(imperative_anywhere, genuine), "note": "rejected"},
            "attribute_name_rule_increment": {**score(attr_only, genuine), "note": "rejected"},
            "shipped_strong_or_framing_or_opener_cap400": {
                **score(shipped, genuine),
                "fact_hit_rate": round(len(shipped) / len(facts), 4),
                "unaudited_hits": len(unaudited),
                "entry_hits": len(entry_hits),
                "entry_hit_rate": round(len(entry_hits) / n_entries, 4) if n_entries else None,
                "decomposition": {
                    "strong_deontic_or_framing": score(strong, genuine),
                    "imperative_opener_increment": score(opener, genuine),
                }},
        },
        "authority_heuristic": {
            "gate": "same 400-char rule-sized gate as distortion_tolerance",
            "entries_gated": auth_e, "entries_ungated": auth_e_ungated, "facts": auth_f,
        },
        "blast_radius_note": ("No backfill ships: existing rows stay NULL. These counts are "
                              "what a hypothetical auto-labelling of the dumped bank WOULD pin."),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", default=None, help=f"read-only bank DSN (default {DEFAULT_DSN})")
    ap.add_argument("--entries", help="entries JSONL ({text}) instead of --dsn")
    ap.add_argument("--facts", help="facts JSONL ({entity,attribute,value}) instead of --dsn")
    ap.add_argument("--verdicts", default=str(RESULTS / "label-heuristic-audit-20260902.verdicts.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    ap.add_argument("--force", action="store_true", help="overwrite an existing artifact")
    a = ap.parse_args(argv)
    if a.facts:
        entries = load_jsonl(a.entries) if a.entries else []
        facts = load_jsonl(a.facts)
    else:
        entries, facts = dump_bank(a.dsn or DEFAULT_DSN)
    with open(a.verdicts, encoding="utf-8") as fh:
        genuine = set(json.load(fh)["genuine"])
    out = Path(a.out) if a.out else RESULTS / f"label-heuristic-audit-{a.date.replace('-', '')}.json"
    if out.exists() and not a.force:
        print(f"refusing to overwrite {out} — pass --force or --out <tagged path>", file=sys.stderr)
        return 2
    art = audit(entries, facts, genuine, a.date)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(art, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    s = art["distortion_tolerance_variants"]["shipped_strong_or_framing_or_opener_cap400"]
    print(f"wrote {out}: shipped {s['fact_hits']} fact hits, {s['judged_genuine']} genuine, "
          f"precision {s['precision']}, entries {s['entry_hits']}, unaudited {s['unaudited_hits']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
