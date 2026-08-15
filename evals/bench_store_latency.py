"""Store latency against bank size — the write path, not retrieval.

`detect_contradictions` runs over every entry of every band on every
write, so store cost scales with the resident set. Profiling put 94% of a
saturated store there (2026-07-25), against ~4% for the capacity-eviction
path that was assumed to be the problem.

Uses REAL MiniLM embeddings and REAL conversation text from the
LongMemEval `s` band dumps. Random unit vectors sit near zero cosine and
would flatter any similarity-based shortcut badly (99.7% of random pairs
fall below the lowest state-transition floor, against 76.4% of real ones).

    python evals/bench_store_latency.py --sizes 500 5250 9000

Runs one size per invocation of a subprocess so banks don't accumulate in
one process — four 5,250-entry banks in a single run skewed later arms
enough to invert the ranking. Writes a JSON artifact by default; a number
without a committed artifact was never really measured.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
DUMPS = RESULTS / "banks" / "s-qwen-27b-ablbands-flat"
WARMUP = 12
SAMPLES = 25


def corpus(n: int):
    """Real texts + embeddings from the replay dumps. Files touched in
    the last 120 s are skipped — a concurrent replay writes these dumps
    non-atomically and a half-written gzip would crash the load."""
    import torch
    texts, embs = [], []
    cutoff = time.time() - 120
    for f in sorted(glob.glob(str(DUMPS / "*.json.gz"))):
        if "_abs" in Path(f).name or Path(f).stat().st_mtime > cutoff:
            continue
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for e in json.load(fh)["bands"][0]["entries"]:
                texts.append(e["text"])
                embs.append(e["emb"])
                if len(texts) >= n:
                    return texts, torch.tensor(embs, dtype=torch.float32)
    raise SystemExit(
        f"only {len(texts)} entries available in {DUMPS} — need {n}. "
        "Run `band_ablation.py replay --band-preset flat` first.")


def _make_config(preset: str, dim: int):
    """MemoryConfig for one arm. ``continuum`` keeps the shipped 8-band
    preset; ``flat`` is one band at the continuum's total capacity with
    the fast tiers' retention (the same arm definition as
    band_ablation.write_flat_config, minus the YAML round-trip)."""
    from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec

    cfg = MemoryConfig(embedding_dim=dim)
    if preset == "flat":
        total = sum(b.max_entries for b in cfg.miras.bands)
        cfg.miras.preset = "custom"
        cfg.miras.bands = [MIRASBandSpec(
            name="flat", max_entries=total,
            update_interval=1_000_000_000,
            promotion_access_count=1_000_000_000,
            promotion_surprise=1.1,
            retention_policy="balanced")]
    return cfg


def run_one(n: int, preset: str = "continuum", dim: int = 384,
            queries: int = 0) -> dict:
    """Store (and optionally retrieve) latency with the bank hydrated to
    n entries. Returns a dict of medians/p95s in ms."""
    import torch
    import torch.nn.functional as F
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.storage.sync import hydrate_cms

    texts, E = corpus(n + WARMUP + SAMPLES)
    E = F.normalize(E, dim=1)
    probe_t, probe_E = texts[n:], E[n:]

    class _Rows:
        def load_entries(self):
            return [{"id": i, "text": texts[i], "embedding": E[i].tolist(),
                     "surprise": 0.4, "ts": time.time(), "access_count": i % 3,
                     "source": "t", "band": "gone", "superseded_at": None,
                     "superseded_by_text": None, "last_logical_turn": None,
                     "slots": [], "episode_id": None, "episode_title": None,
                     "tags": [], "reinforcements": 0} for i in range(n)]

        def load_episodes(self):
            return []

    # ``--dim`` must match the embeddings in DUMPS: 384 for the legacy
    # committed MiniLM corpus, 1024 for dumps regenerated under
    # embedding-backbone-v25 (Qwen3-Embedding-0.6B). The child asserts.
    if E.shape[1] != dim:
        raise SystemExit(f"--dim {dim} but corpus embeddings are "
                         f"{E.shape[1]}-d — pass the matching --dim")
    cms = ContinuumMemorySystem(_make_config(preset, dim))
    hydrate_cms(cms, _Rows())

    # Warm the per-entry cue cache first — a daemon runs for days, so warm
    # is the steady state and the state under test. The cold pass is one
    # full scan per restart and is reported separately.
    for i in range(WARMUP):
        cms.store(probe_t[i], probe_E[i], source="warmup")
    ts = []
    for i in range(SAMPLES):
        j = WARMUP + i
        t0 = time.perf_counter()
        cms.store(probe_t[j], probe_E[j], source="bench")
        ts.append(1000 * (time.perf_counter() - t0))

    def p95(xs):
        return sorted(xs)[max(0, int(len(xs) * 0.95) - 1)]

    out = {"resident": n, "preset": preset, "dim": dim,
           "n_bands": len(cms.bands),
           "store_median_ms": statistics.median(ts),
           "store_p95_ms": p95(ts)}

    if queries:
        # Read path: real retrieve() over the resident set, query text +
        # embedding taken from held-out corpus rows (identical inputs in
        # both arms). BM25 measured both ways — its per-query index
        # rebuild over the full candidate pool is the dominant lexical
        # cost and is band-structure-independent.
        q_idx = list(range(min(queries, WARMUP + SAMPLES)))
        for bm25_on in (False, True):
            rs = []
            for i in q_idx:
                t0 = time.perf_counter()
                cms.retrieve(probe_E[i], top_k=6, query_text=probe_t[i],
                             bm25=bm25_on)
                rs.append(1000 * (time.perf_counter() - t0))
            key = "retrieve_bm25" if bm25_on else "retrieve_dense"
            out[f"{key}_median_ms"] = statistics.median(rs)
            out[f"{key}_p95_ms"] = p95(rs)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", type=int, nargs="+",
                    default=[500, 5250, 9000])
    ap.add_argument("--preset", choices=("continuum", "flat"),
                    default="continuum")
    ap.add_argument("--dim", type=int, default=384,
                    help="embedding dim of the DUMPS corpus (384 legacy "
                         "MiniLM, 1024 for v25 regenerated dumps)")
    ap.add_argument("--queries", type=int, default=0,
                    help="also measure retrieve() latency over this many "
                         "held-out queries (dense and dense+bm25)")
    ap.add_argument("--out", type=Path,
                    default=RESULTS / "store-latency-by-bank-size.json")
    ap.add_argument("--_child", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._child is not None:
        print(json.dumps(run_one(args._child, preset=args.preset,
                                 dim=args.dim, queries=args.queries)))
        return 0

    rows = []
    for n in args.sizes:
        proc = subprocess.run(
            [sys.executable, __file__, "--_child", str(n),
             "--preset", args.preset, "--dim", str(args.dim),
             "--queries", str(args.queries)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"child failed for n={n}:\n{proc.stderr[-2000:]}")
        rows.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        r = rows[-1]
        extra = (f"  retrieve dense {r['retrieve_dense_median_ms']:6.1f} ms"
                 f" / bm25 {r['retrieve_bm25_median_ms']:6.1f} ms"
                 if args.queries else "")
        print(f"resident={r['resident']:6}  "
              f"median store {r['store_median_ms']:8.1f} ms{extra}")

    args.out.write_text(json.dumps(
        {"warmup_stores": WARMUP, "samples": SAMPLES,
         "preset": args.preset, "dim": args.dim, "queries": args.queries,
         "corpus": f"longmemeval-s replay dumps ({args.dim}-d)",
         "rows": rows}, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
