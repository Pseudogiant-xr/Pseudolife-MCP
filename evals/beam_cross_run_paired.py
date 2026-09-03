#!/usr/bin/env python
"""Cross-run paired comparison of two BEAM artifacts on the same questions.

Joins rows on (chat_id, type, index) and, per arm present in both runs,
reports the paired per-row delta (B minus A) with its standard error and a
95% CI, the count of rows whose score moved, and the count of rows whose
SERVED CONTEXT differs byte-for-byte. The rag arm is the identical-input
control: raw turns never touch the cortex, so a non-zero rag delta is
instrument noise and bounds every other arm's claim. Also reports, per
chat, whether any served context differed, so a recall-side change can be
traced to the chats it actually touched.

    python evals/beam_cross_run_paired.py --a <baseline.jsonl> --b <new.jsonl> \
        --out <verdict.json> [--label-a chip12-b16 --label-b chip5-b16]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
from pathlib import Path


def load(path: Path) -> dict[tuple, dict]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {(r["chat_id"], r["type"], r["index"]): r for r in rows}


def paired(pairs: list[tuple[dict, dict]], arm: str) -> dict:
    ds = [b[f"{arm}_score"] - a[f"{arm}_score"] for a, b in pairs]
    n = len(ds)
    mean = sum(ds) / n
    var = sum((d - mean) ** 2 for d in ds) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else 0.0
    moved = sum(1 for d in ds if d != 0)
    up = sum(1 for d in ds if d > 0)
    ctx_diff = sum(1 for a, b in pairs
                   if a.get("contexts", {}).get(arm) != b.get("contexts", {}).get(arm))
    return {"n": n,
            "mean_a": round(sum(a[f"{arm}_score"] for a, _ in pairs) / n, 4),
            "mean_b": round(sum(b[f"{arm}_score"] for _, b in pairs) / n, 4),
            "delta_mean": round(mean, 4), "delta_se": round(se, 4),
            "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)],
            "rows_moved": moved, "rows_up": up, "rows_down": moved - up,
            "rows_context_differs": ctx_diff}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", required=True, help="baseline run jsonl")
    ap.add_argument("--b", required=True, help="new run jsonl")
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    pa, pb = Path(args.a), Path(args.b)
    A, B = load(pa), load(pb)
    keys = sorted(set(A) & set(B))
    pairs = [(A[k], B[k]) for k in keys]
    arms = [arm for arm in ("rag", "cortex", "hybrid", "refind", "nomem")
            if pairs and f"{arm}_score" in pairs[0][0] and f"{arm}_score" in pairs[0][1]]
    out = {
        "what": "cross-run paired comparison on identical questions (B minus A)",
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "a": {"file": pa.name, "label": args.label_a or pa.stem, "rows": len(A)},
        "b": {"file": pb.name, "label": args.label_b or pb.stem, "rows": len(B)},
        "paired_rows": len(keys),
        "unpaired": {"only_a": len(set(A) - set(B)), "only_b": len(set(B) - set(A))},
        "control_note": ("rag is the identical-input control (raw turns, no cortex); "
                         "its delta is the instrument-noise floor for every other arm"),
        "arms": {arm: paired(pairs, arm) for arm in arms},
        "types": {},
        "chats_with_context_diff": {},
    }
    by_type: dict[str, list] = {}
    for a, b in pairs:
        by_type.setdefault(a["type"], []).append((a, b))
    for t, tp in sorted(by_type.items()):
        out["types"][t] = {arm: paired(tp, arm) for arm in arms}
    by_chat: dict[str, dict] = {}
    for a, b in pairs:
        c = by_chat.setdefault(a["chat_id"], {arm: 0 for arm in arms})
        for arm in arms:
            if a.get("contexts", {}).get(arm) != b.get("contexts", {}).get(arm):
                c[arm] += 1
    out["chats_with_context_diff"] = {c: v for c, v in sorted(by_chat.items(), key=lambda kv: (0, int(kv[0]), "") if kv[0].isdigit() else (1, 0, kv[0]))
                                      if any(v.values())}
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("paired_rows", "unpaired", "arms", "chats_with_context_diff")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
