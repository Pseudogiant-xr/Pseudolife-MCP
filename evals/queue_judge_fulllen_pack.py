"""Rebuild a queue-judge evidence pack with FULL-LENGTH merge snippets.

The 2026-09-02 pack (``evals/data/queue_judge_eval_20260902.json``, private)
carries merge snippets clipped to ``snippet_max_chars`` (240) at BUILD time —
305 of 309 are exactly 240 characters, cut mid-word — so the panel and the
ladder both judged clipped evidence. Every clipped snippet is a strict
prefix of one live ``entries.text`` row, so the same rows can be recovered
at full length by prefix match against the bank the pack was built from
(2026-09-03 probe: 305/305 unique matches, 2 already-full, 2 unmatched).

Usage (read-only against the bank; writes a NEW pack, never overwrites)::

    python evals/queue_judge_fulllen_pack.py \\
        --src evals/data/queue_judge_eval_20260902.json \\
        --out evals/data/queue_judge_eval_20260903_fulllen.json \\
        --dsn postgresql://user:pass@127.0.0.1:5433/dbname

Only ``merges`` rows change: each snippet of exactly ``--clip`` characters
is replaced by the unique entry whose text starts with it; snippets with
no match or several matches are kept as they are and counted in
``provenance.fulllen``. Labels, votes and every other queue are copied
verbatim, so a ladder run on the output differs from the 2026-09-02 run in
evidence length alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dsn", default=os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL", ""))
    ap.add_argument("--clip", type=int, default=240,
                    help="the build-time cap the source pack was clipped at")
    ap.add_argument("--force", action="store_true",
                    help="overwrite --out if it exists (never silent)")
    args = ap.parse_args()
    if not args.dsn:
        print("--dsn (or PSEUDOLIFE_MCP_DATABASE_URL) is required", file=sys.stderr)
        return 2
    if args.out.exists() and not args.force:
        print(f"{args.out} exists; pass --force to replace it", file=sys.stderr)
        return 2

    import psycopg

    raw = args.src.read_bytes()
    pack = json.loads(raw.decode("utf-8"))
    conn = psycopg.connect(args.dsn, connect_timeout=10)
    conn.execute("SET default_transaction_read_only = on")

    counts = {"snippets": 0, "clipped": 0, "recovered": 0, "no_match": 0,
              "ambiguous": 0}
    lengths: list[int] = []

    def recover(snippet: str) -> str:
        counts["snippets"] += 1
        if len(snippet) != args.clip:
            return snippet
        counts["clipped"] += 1
        rows = conn.execute(
            "SELECT text FROM entries WHERE left(text, %s) = %s LIMIT 2",
            (args.clip, snippet)).fetchall()
        if not rows:
            counts["no_match"] += 1
            return snippet
        if len(rows) > 1:
            counts["ambiguous"] += 1
            return snippet
        counts["recovered"] += 1
        lengths.append(len(rows[0][0]))
        return rows[0][0]

    for r in pack["merges"]:
        for side in ("from", "into"):
            s = r.get(side) or {}
            s["snippets"] = [recover(x) for x in (s.get("snippets") or [])]

    lengths.sort()
    n = len(lengths)
    # The 2026-09-02 pack carries its provenance as one prose string; keep
    # it under "original" and attach the rebuild record beside it.
    if not isinstance(pack.get("provenance"), dict):
        pack["provenance"] = {"original": pack.get("provenance")}
    pack["provenance"]["fulllen"] = {
        "rebuilt_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": args.src.name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "clip": args.clip,
        **counts,
        "recovered_lengths": ({"p50": lengths[n // 2], "p90": lengths[int(n * .9)],
                               "p95": lengths[int(n * .95)], "max": lengths[-1]}
                              if n else None),
    }
    args.out.write_text(json.dumps(pack, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    print(json.dumps(pack["provenance"]["fulllen"], indent=1))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
