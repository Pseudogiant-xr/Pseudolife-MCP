"""Offline cortex-retrieval replay over dumped fact banks.

The LongMemEval bench (``--tag diag``) persists each question's full fact
bank to ``evals/results/banks/<dataset>-<extractor>-<tag>/<qid>.json.gz``.
Fact embeddings in the live system are ``encode_single(f"{entity}
{attribute} {value}")`` and ``cortex.search`` is plain cosine over them
(cortex.py), so replaying retrieval here under different ``top_k`` /
``min_score`` knobs is EXACT — no re-extraction needed.

    python evals/retrieval_sweep.py                       # default diag banks
    python evals/retrieval_sweep.py --banks s-qwen-27b-diag

For each knob combo, reports: starved% (questions served 0 facts), mean
facts served, and — the metric that matters — gold-hit rate: among banks
whose CURRENT facts contain the gold answer at all, how often the served
top-k includes one of those facts.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")                # CPU only
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

RESULTS_DIR = Path(__file__).resolve().parent / "results"

TOP_KS = (8, 16, 24)
MIN_SCORES = (0.3, 0.25, 0.2, 0.1, 0.0)


def _norm_text(s) -> str:
    return re.sub(r"\s+", " ", str(s).lower().strip())


def load_banks(banks_dir: Path) -> list[dict]:
    banks = []
    for p in sorted(banks_dir.glob("*.json.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            banks.append(json.load(fh))
    return banks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--banks", default="s-qwen-27b-diag",
                    help="subdirectory under evals/results/banks/, "
                         "or an absolute path to a banks directory")
    args = ap.parse_args()
    banks_dir = (Path(args.banks) if Path(args.banks).is_absolute()
                 else RESULTS_DIR / "banks" / args.banks)
    banks = load_banks(banks_dir)
    if not banks:
        sys.exit(f"no bank dumps in {banks_dir}")

    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig

    emb = EmbeddingPipeline(EmbeddingConfig(device="cpu"))

    # Pre-embed once per bank; the knob grid then costs nothing.
    prepared = []
    for b in banks:
        facts = b["facts"]
        if not facts:
            prepared.append({"gold": set(), "sims": [], "n": 0})
            continue
        texts = [f"{f['entity']} {f['attribute']} {f['value']}".strip()
                 for f in facts]
        mat = emb.encode(texts)                       # (n, d), normalized
        q = emb.encode_single(b["question"])
        sims = (mat @ q).tolist()
        ans = _norm_text(b["answer"])
        gold = {i for i, f in enumerate(facts)
                if ans in _norm_text(f.get("value", ""))}
        prepared.append({"gold": gold, "sims": sims, "n": len(facts)})

    with_gold = [p for p in prepared if p["gold"]]
    print(f"{len(banks)} banks from {banks_dir.name}; "
          f"{len(with_gold)} have the gold answer in a current fact "
          f"({len(with_gold) / len(banks):.0%} extraction ceiling)")
    print(f"{'top_k':>6} {'min_score':>10} {'starved%':>9} "
          f"{'mean facts':>11} {'gold-hit%':>10}")
    for top_k in TOP_KS:
        for ms in MIN_SCORES:
            served_counts, gold_hits = [], 0
            for p in prepared:
                ranked = sorted(
                    (i for i, s in enumerate(p["sims"]) if s >= ms),
                    key=lambda i: p["sims"][i], reverse=True)[:top_k]
                served_counts.append(len(ranked))
                if p["gold"] and p["gold"] & set(ranked):
                    gold_hits += 1
            starved = sum(1 for c in served_counts if c == 0) / len(banks)
            mean_served = sum(served_counts) / len(banks)
            gold_rate = gold_hits / max(len(with_gold), 1)
            print(f"{top_k:>6} {ms:>10.2f} {starved:>8.0%} "
                  f"{mean_served:>11.1f} {gold_rate:>9.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
