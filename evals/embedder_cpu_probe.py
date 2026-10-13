"""CPU deployment cost of one embedder_recall.py arm, as the daemon runs it.

The daemon embeds on CPU, so a retrieval win is only worth a swap if the
model's CPU cost is acceptable. This loads one arm on CPU (bf16 when the
CPU has native bf16, matching EmbeddingConfig.cpu_dtype="auto"; fp32
otherwise or with --fp32) and measures what a deploy would feel:

- load seconds and peak / steady RSS (process working set)
- single-query encode latency (median and p90 over --repeats, with the
  arm's query prefix: the per-search cost)
- document throughput on real LoCoMo turns (batch 64: the store and
  re-embed cost)

One arm per process so RSS is not polluted by the previous model. Run it
only while no full test suite holds the machine (its numbers would be
noise, and it would slow the suite).

    CUDA_VISIBLE_DEVICES=-1 python evals/embedder_cpu_probe.py qwen3-0.6b \
        --out runs/cpu/qwen3-0.6b.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("arm")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--docs", type=int, default=512)
    ap.add_argument("--max-seq-length", type=int, default=512)
    ap.add_argument("--fp32", action="store_true")
    args = ap.parse_args()

    import psutil
    import torch
    from sentence_transformers import SentenceTransformer

    import embedder_recall as er
    import embedder_stamp

    label, repo, qprefix, dprefix = er.CANDIDATES[args.arm]
    # The maintainer's CPU has native bf16 (avx512_bf16), where the daemon's
    # cpu_dtype="auto" resolves to bf16; --fp32 measures the fallback.
    dtype = torch.float32 if args.fp32 else torch.bfloat16
    kwargs = dict(er.MODEL_KWARGS.get(args.arm, {}))
    mk = dict(kwargs.pop("model_kwargs", {}) or {})
    mk["dtype"] = dtype
    proc = psutil.Process()

    t0 = time.perf_counter()
    model = SentenceTransformer(repo, device="cpu",
                                truncate_dim=er.TRUNCATE_DIM.get(args.arm),
                                model_kwargs=mk, **kwargs)
    model.max_seq_length = min(int(model.max_seq_length or 512),
                               args.max_seq_length)
    load_s = time.perf_counter() - t0
    enc = er.ENCODE_KWARGS.get(args.arm, {})

    turns = [t for q in er.load_locomo(10 ** 6)[:1] for t in q["turns"]]
    turns = (turns * (args.docs // max(1, len(turns)) + 1))[:args.docs]
    queries = [q["question"] for q in er.load_locomo(args.repeats + 5)]

    # warmup
    model.encode([qprefix + queries[0]], normalize_embeddings=True, **enc)
    lat = []
    for q in queries[:args.repeats]:
        t = time.perf_counter()
        model.encode([qprefix + q], normalize_embeddings=True, **enc)
        lat.append((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    model.encode([dprefix + d for d in turns], batch_size=64,
                 normalize_embeddings=True, **enc)
    doc_s = time.perf_counter() - t
    mem = proc.memory_info()

    lat.sort()
    row = {"arm": label, "key": args.arm, "model": repo,
           "embedder": embedder_stamp.describe_model(model, device="cpu"),
           "dtype": str(dtype).replace("torch.", ""),
           "cpu_capability": str(getattr(torch.backends.cpu,
                                         "get_cpu_capability",
                                         lambda: "?")()),
           "torch_threads": torch.get_num_threads(),
           "max_seq_length": int(model.max_seq_length),
           "dim": model.get_sentence_embedding_dimension(),
           "params_m": round(sum(p.numel() for p in model.parameters())
                             / 1e6, 1),
           "load_seconds": round(load_s, 2),
           "query_ms_median": round(statistics.median(lat), 1),
           "query_ms_p90": round(lat[int(0.9 * (len(lat) - 1))], 1),
           "docs": len(turns),
           "docs_per_second": round(len(turns) / doc_s, 1),
           "rss_mb_steady": round(mem.rss / 2 ** 20),
           "rss_mb_peak": round(getattr(mem, "peak_wset", mem.rss)
                                / 2 ** 20)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(row, indent=2), encoding="utf-8")
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
