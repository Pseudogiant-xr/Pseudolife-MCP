"""Bank-drift analyzer — judge-free leading indicators for prompt
interference with slot naming and update consolidation.

Born from the sgku bank-diff forensics (2026-08-13): the v8 stance
prompt's KU regression was visible WITHOUT a judge as slot-count
explosions, separator/format drift in slot keys, and frozen updates.
This analyzer turns those observations into numbers over the
per-question bank dumps ``longmemeval_bench.py`` already writes
(``evals/results/banks/<dir>/<qid>.json.gz``), so any future prompt arm
gets the cheap tripwire BEFORE the expensive judged comparison.

Per question present in both directories:
- slot counts (reference vs candidate) and their ratio;
- key-set Jaccard over normalized keys (case/separator-collapsed);
- format-drift pairs: distinct candidate keys that COLLIDE after
  normalization (``past travel experience`` vs
  ``past-travel-experience``) — same fact minted under two spellings;
- update-loss proxy: reference slots whose history shows >=2 values
  (an observed supersession) where the candidate's matching normalized
  key never superseded (history <= 1) — the frozen-update signature.

Aggregates + per-question rows land in the ``--out`` artifact
(benches persist by default). No thresholds are enforced here — the
consumer is a human or a prereg gate that names its own bar.

    PYTHONPATH=. python evals/analyze_bank_drift.py \
        --reference oracle-qwen-27b-sgku-v5 \
        --candidate oracle-qwen-27b-sgku-v8 \
        --out evals/results/bank-drift-sgku-v5-vs-v8.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
BANKS_DIR = RESULTS_DIR / "banks"

_NORM_RE = re.compile(r"[^0-9a-z]+")


def _norm_key(entity: str, attribute: str) -> str:
    e = _NORM_RE.sub(" ", (entity or "").casefold()).strip()
    a = _NORM_RE.sub(" ", (attribute or "").casefold()).strip()
    return f"{e}.{a}"


def _load(bank_dir: Path, qid_file: Path) -> list[dict]:
    with gzip.open(qid_file, "rt", encoding="utf-8") as f:
        return json.load(f)["facts"]


def _slots(facts: list[dict]) -> dict[str, dict]:
    """Normalized key -> {raw_keys: set, max_history: int}. Member rows
    collapse onto their slot key like scalars — an exploded list shows
    up as many RAW keys or many same-key rows either way."""
    out: dict[str, dict] = {}
    for f in facts:
        nk = _norm_key(f.get("entity", ""), f.get("attribute", ""))
        rec = out.setdefault(nk, {"raw_keys": set(), "max_history": 0})
        rec["raw_keys"].add(f"{f.get('entity')}.{f.get('attribute')}")
        rec["max_history"] = max(rec["max_history"],
                                 len(f.get("history") or []))
    return out


def analyze_question(ref: list[dict], cand: list[dict]) -> dict:
    rs, cs = _slots(ref), _slots(cand)
    rkeys, ckeys = set(rs), set(cs)
    inter = rkeys & ckeys
    union = rkeys | ckeys
    fmt_drift = sum(1 for k, v in cs.items() if len(v["raw_keys"]) > 1)
    frozen = sorted(
        k for k in inter
        if rs[k]["max_history"] >= 2 and cs[k]["max_history"] <= 1)
    return {
        "ref_slots": len(ref), "cand_slots": len(cand),
        "slot_ratio": (len(cand) / len(ref)) if ref else None,
        "key_jaccard": (len(inter) / len(union)) if union else 1.0,
        "format_drift_keys": fmt_drift,
        "frozen_update_keys": frozen,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference", required=True,
                    help="bank dir name under evals/results/banks/")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    ref_dir = BANKS_DIR / args.reference
    cand_dir = BANKS_DIR / args.candidate
    rows = {}
    for qf in sorted(ref_dir.glob("*.json.gz")):
        cf = cand_dir / qf.name
        if not cf.exists():
            continue
        qid = qf.name.replace(".json.gz", "")
        rows[qid] = analyze_question(_load(ref_dir, qf), _load(cand_dir, cf))

    n = len(rows)
    ratios = [r["slot_ratio"] for r in rows.values() if r["slot_ratio"]]
    out = {
        "reference": args.reference, "candidate": args.candidate,
        "n_questions": n,
        "aggregates": {
            "mean_slot_ratio": sum(ratios) / len(ratios) if ratios else None,
            "explosions_over_2x": sum(1 for r in ratios if r > 2.0),
            "mean_key_jaccard": (sum(r["key_jaccard"]
                                     for r in rows.values()) / n
                                 if n else None),
            "format_drift_total": sum(r["format_drift_keys"]
                                      for r in rows.values()),
            "frozen_update_total": sum(len(r["frozen_update_keys"])
                                       for r in rows.values()),
        },
        "questions": rows,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    a = out["aggregates"]
    print(f"[bank-drift] {args.reference} vs {args.candidate}: "
          f"n={n} slot_ratio={a['mean_slot_ratio']:.2f} "
          f"explosions>2x={a['explosions_over_2x']} "
          f"jaccard={a['mean_key_jaccard']:.2f} "
          f"fmt_drift={a['format_drift_total']} "
          f"frozen={a['frozen_update_total']}")
    print(f"[bank-drift] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
