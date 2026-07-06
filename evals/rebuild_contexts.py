"""Rebuild cortex/hybrid contexts offline from dumped fact banks under new
retrieval knobs, emitting a new tagged JSONL ready for ``--phase answer``.

The extract phase persists each question's served contexts at build time, so a
knob change normally needs a full (GPU-expensive) re-extract. But ``--tag
diag`` runs also dump the complete fact bank per question (with history
chains), and cortex search is plain cosine over
``encode_single(f"{entity} {attribute} {value}")`` — so the cortex arm's
context can be rebuilt EXACTLY offline. The rag context is copied verbatim;
the hybrid arm reuses its original raw-memories block verbatim and splices in
the rebuilt fact lines. Judge fields are stripped so the answer phase re-runs.

    python evals/rebuild_contexts.py                  # s/qwen-27b diag -> diag-knobs
    python evals/rebuild_contexts.py --top-k 24 --min-score 0.1

Then:
    python evals/longmemeval_bench.py --dataset s --extractor qwen-27b \
        --tag diag-knobs --phase answer
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from longmemeval_bench import (  # noqa: E402
    ARMS, CORTEX_MIN_SCORE, CORTEX_TOP_K, bank_dir, load_rows, out_file,
    rewrite_rows,
)

_HYBRID_SPLIT = "\n\nRelevant memories:\n"


def rebuild_fact_lines(bank: dict, emb, top_k: int, min_score: float) -> list[str]:
    facts = bank["facts"]
    if not facts:
        return []
    texts = [f"{f['entity']} {f['attribute']} {f['value']}".strip()
             for f in facts]
    mat = emb.encode(texts)                            # (n, d), normalized
    q = emb.encode_single(bank["question"])
    sims = (mat @ q).tolist()
    ranked = sorted((i for i, s in enumerate(sims) if s >= min_score),
                    key=lambda i: sims[i], reverse=True)[:top_k]
    lines = []
    for i in ranked:
        f = facts[i]
        line = (f"{f.get('entity', '')} — {f.get('attribute', '')}: "
                f"{f.get('value', '')}")
        older = [v for v in (f.get("history") or [])[:-1]
                 if v and v != f.get("value")]
        if older:
            line += "  (earlier values, oldest first: " + " -> ".join(older) + ")"
        lines.append(line)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="s")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--src-tag", default="diag",
                    help="tag of the source run (must have dumped banks)")
    ap.add_argument("--out-tag", default="diag-knobs",
                    help="tag for the rebuilt JSONL")
    ap.add_argument("--top-k", type=int, default=CORTEX_TOP_K)
    ap.add_argument("--min-score", type=float, default=CORTEX_MIN_SCORE)
    args = ap.parse_args()

    src = out_file(args.dataset, args.extractor, args.src_tag)
    banks = bank_dir(args.dataset, args.extractor, args.src_tag)
    dst = out_file(args.dataset, args.extractor, args.out_tag)
    rows = load_rows(src)
    if not rows:
        sys.exit(f"no rows in {src}")

    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig
    emb = EmbeddingPipeline(EmbeddingConfig(device="cpu"))

    out_rows = []
    for row in rows:
        bank_path = banks / f"{row['question_id']}.json.gz"
        with gzip.open(bank_path, "rt", encoding="utf-8") as fh:
            bank = json.load(fh)
        fact_lines = rebuild_fact_lines(bank, emb, args.top_k, args.min_score)
        raw_block = row["contexts"]["hybrid"].split(_HYBRID_SPLIT, 1)[-1]
        row["contexts"]["cortex"] = "\n".join(fact_lines)
        row["contexts"]["hybrid"] = ("Known facts:\n" + "\n".join(fact_lines)
                                     + _HYBRID_SPLIT + raw_block)
        for arm in ARMS:                 # strip verdicts -> answer phase re-runs
            for field in ("response", "correct", "context_tokens"):
                row.pop(f"{arm}_{field}", None)
        out_rows.append(row)

    rewrite_rows(dst, out_rows)
    print(f"rebuilt {len(out_rows)} rows -> {dst.name} "
          f"(top_k={args.top_k}, min_score={args.min_score})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
