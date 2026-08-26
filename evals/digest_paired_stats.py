"""Paired per-row deltas for the digest-b16 run: hybrid_digest vs hybrid.

Writes <run>.paired.json next to the run jsonl. The rag arm rides along as
the cross-run control reference (identical retrieval width, no digests).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def paired(rows: list[dict], a: str, b: str) -> dict:
    ds = [r[f"{a}_score"] - r[f"{b}_score"] for r in rows]
    n = len(ds)
    mean = sum(ds) / n
    var = sum((d - mean) ** 2 for d in ds) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else 0.0
    return {"n": n, "delta_mean": round(mean, 4), "delta_se": round(se, 4),
            "ci95": [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)]}


def main() -> int:
    src = Path(sys.argv[1])
    rows = [json.loads(x) for x in src.read_text(encoding="utf-8").splitlines()]
    out = {
        "source": src.name,
        "comparison": "hybrid_digest minus hybrid (paired per row)",
        "overall": paired(rows, "hybrid_digest", "hybrid"),
        "types": {},
        "control_note": ("rag arm is the no-digest control at identical "
                         "width; its cross-run movement bounds any "
                         "cross-run claim"),
    }
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for qtype, trows in sorted(by_type.items()):
        out["types"][qtype] = paired(trows, "hybrid_digest", "hybrid")
    dst = src.with_name(src.name.removesuffix(".jsonl") + ".paired.json")
    dst.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
