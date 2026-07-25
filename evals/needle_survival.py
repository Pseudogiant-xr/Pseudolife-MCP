"""Do the continuum's evictions discard the ANSWER EVIDENCE?

The write-side ablation established that the 8-band continuum drops 31.1%
of stored turns where a flat band of the same total capacity drops none
(``longmemeval-ku-*-wabl-survival.json``). Survival rate alone cannot say
whether that costs anything: discarding 31% of filler is free, discarding
the needle is fatal. LongMemEval marks its evidence turns with
``has_answer``, so the eviction rate ON NEEDLES is directly measurable and
comparable to the base rate.

It is not free. Needles are evicted at **1.21x** the base rate, because
eviction and promotion both rank on novelty (``1 - max cos``) and
knowledge-update evidence is by construction a *restatement* of an
attribute already mentioned — hence unsurprising, hence preferentially
destroyed.

Reads the band dumps written by ``band_ablation.py replay`` (gitignored —
they are hundreds of MB of embeddings), and writes a small tracked JSON so
the published numbers have committed evidence:

    python evals/needle_survival.py --dataset s --extractor qwen-27b
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
DATA = Path(__file__).resolve().parent / "data"


def needle_texts(q: dict) -> set[str]:
    """Stored-text form of every ``has_answer`` turn, matching the exact
    string ``band_ablation.cmd_replay`` stores."""
    out: set[str] = set()
    for date, session in zip(q["haystack_dates"], q["haystack_sessions"]):
        for turn in session:
            if str(turn.get("has_answer", "False")).lower() != "true":
                continue
            content = (turn.get("content") or "").strip()
            if content:
                out.add(f"[{date}] {turn['role']}: {content}")
    return out


def dump_texts(path: str) -> set[str]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {e["text"] for b in json.load(fh)["bands"] for e in b["entries"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="s")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    ds, ex = args.dataset, args.extractor
    cont_dir = RESULTS / "banks" / f"{ds}-{ex}-ablbands"
    flat_dir = RESULTS / "banks" / f"{ds}-{ex}-ablbands-flat"
    src = DATA / f"longmemeval_{ds}_cleaned.json"
    if not cont_dir.is_dir() or not flat_dir.is_dir():
        raise SystemExit(
            f"band dumps missing ({cont_dir} / {flat_dir}) — run "
            f"`band_ablation.py replay` for both presets first")

    questions = {q["question_id"]: q
                 for q in json.loads(src.read_text(encoding="utf-8"))}

    rows: list[dict] = []
    cont_all = flat_all = cont_n = flat_n = 0
    for f in sorted(glob.glob(str(cont_dir / "*.json.gz"))):
        if "_abs" in Path(f).name:
            continue
        qid = Path(f).name.split(".")[0]
        q = questions.get(qid)
        flat_f = flat_dir / Path(f).name
        if q is None or not flat_f.exists():
            continue
        cont, flat = dump_texts(f), dump_texts(str(flat_f))
        # Only needles that actually reached the store — the flat arm never
        # evicts, so its dump IS the ingested set.
        needles = needle_texts(q) & flat
        if not needles:
            continue
        kept = len(needles & cont)
        cont_all += len(cont)
        flat_all += len(flat)
        cont_n += kept
        flat_n += len(needles)
        rows.append({"question_id": qid, "turns_ingested": len(flat),
                     "continuum_survivors": len(cont),
                     "needles": len(needles), "needles_survived": kept})

    base = 1 - cont_all / flat_all
    needle = 1 - cont_n / flat_n
    lost = sum(1 for r in rows if r["needles_survived"] < r["needles"])
    payload = {
        "dataset": ds, "extractor": ex, "n_questions": len(rows),
        "turns_ingested": flat_all, "continuum_survivors": cont_all,
        "base_eviction_rate": base,
        "needles_total": flat_n, "needles_survived": cont_n,
        "needle_eviction_rate": needle,
        "needle_vs_base_ratio": needle / base if base else None,
        "questions_losing_a_needle": lost,
        "questions_losing_a_needle_frac": lost / len(rows) if rows else None,
        "per_question": rows,
    }
    out = args.out or RESULTS / f"longmemeval-ku-{ds}-{ex}-needle-survival.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"{len(rows)} questions with ingested evidence turns")
    print(f"all turns    : {cont_all}/{flat_all} survived -> {100 * base:.1f}% evicted")
    print(f"NEEDLE turns : {cont_n}/{flat_n} survived -> {100 * needle:.1f}% evicted")
    print(f"ratio        : {needle / base:.2f}x the base rate")
    print(f"questions losing >=1 needle: {lost}/{len(rows)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
