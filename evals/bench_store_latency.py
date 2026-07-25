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
    """Real texts + embeddings from the committed replay dumps."""
    import torch
    texts, embs = [], []
    for f in sorted(glob.glob(str(DUMPS / "*.json.gz"))):
        if "_abs" in Path(f).name:
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


def run_one(n: int) -> float:
    """Median store latency (ms) with the bank hydrated to n entries."""
    import torch
    import torch.nn.functional as F
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.storage.sync import hydrate_cms
    from pseudolife_memory.utils.config import MemoryConfig

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

    cms = ContinuumMemorySystem(MemoryConfig())
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
    return statistics.median(ts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", type=int, nargs="+",
                    default=[500, 5250, 9000])
    ap.add_argument("--out", type=Path,
                    default=RESULTS / "store-latency-by-bank-size.json")
    ap.add_argument("--_child", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._child is not None:
        print(json.dumps({"resident": args._child,
                          "median_ms": run_one(args._child)}))
        return 0

    rows = []
    for n in args.sizes:
        proc = subprocess.run(
            [sys.executable, __file__, "--_child", str(n)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"child failed for n={n}:\n{proc.stderr[-2000:]}")
        rows.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        print(f"resident={rows[-1]['resident']:6}  "
              f"median store {rows[-1]['median_ms']:8.1f} ms")

    args.out.write_text(json.dumps(
        {"warmup_stores": WARMUP, "samples": SAMPLES,
         "corpus": "longmemeval-s replay dumps (real MiniLM embeddings)",
         "rows": rows}, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
