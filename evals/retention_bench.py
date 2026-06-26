"""Eviction-dynamics bench for ``retention_boost`` (provenance-as-link Phase 2).

Drives the REAL ``MIRASBand`` + production ``RetentionPolicy`` under capacity
pressure to measure how ``retention_boost`` protects reinforced episodes versus
how it respects recency. Dev-only, CPU, deterministic (seeded), no Postgres.

Honest workload (P1.6, docs/specs/2026-06-26-retention-bench-realism-design.md):
  * heavy-tailed reinforcement (a fraction of entries, Pareto counts) — not
    "every 4th, exactly 5";
  * access_count COUPLED to reinforcement + noise — in the real system
    memory_get / memory_reinforce bump access_count, so a reinforced entry
    already carries elevated access (the base access_count/age term partly
    protects it). Crediting that coupling is what makes the re-derived knee
    honest rather than optimistic.
The workload is generated ONCE under a fixed seed and reused across the whole
``GRID``, so the only variable between rows is ``retention_boost``.

Run: HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe evals/retention_bench.py
Writes: evals/results/retention.json
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import torch

import pseudolife_memory.memory.miras.band as bandmod
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.utils.config import MIRASBandSpec

DIM = 8
CAP = 50                  # band capacity
N_TOTAL = 200             # 4x capacity -> heavy, sustained eviction
SOURCE = "agent_action"   # neutral source weight (1.0) so the boost is isolated
GRID = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0]

# Honest-workload knobs.
SEED = 1234
REINFORCED_FRACTION = 0.25   # a quarter of entries ever get reinforced
REINF_PARETO_ALPHA = 1.3     # heavy tail: most reinforced 1-3, a few large
REINF_CAP = 25               # cap a reinforcement count
ACCESS_COUPLE = 1.0          # each reinforcement is ~1 access (get/reinforce bump access_count)
ACCESS_NOISE = 8.0           # gaussian sd on the recency-base access_count

# Deterministic "now" pinned past every entry timestamp so age = NOW - i is stable.
_NOW = float(N_TOTAL + 1)


def _make_workload(rng: random.Random) -> tuple[set[int], dict[int, int], dict[int, int]]:
    """Generate the (reinforced set, reinforcement counts, access_count) once.

    access_count = recency-base (i) + ACCESS_COUPLE * reinforcements + noise,
    floored at 0 — so reinforced entries carry the elevated access the real
    system would have given them.
    """
    n_reinforced = int(N_TOTAL * REINFORCED_FRACTION)
    reinforced = set(rng.sample(range(N_TOTAL), n_reinforced))
    reinf_counts = {
        i: min(REINF_CAP, int(rng.paretovariate(REINF_PARETO_ALPHA)))
        for i in reinforced
    }
    access = {}
    for i in range(N_TOTAL):
        reinf = reinf_counts.get(i, 0)
        base = i + ACCESS_COUPLE * reinf + rng.gauss(0.0, ACCESS_NOISE)
        access[i] = max(0, int(round(base)))
    return reinforced, reinf_counts, access


def _run_one(retention_boost: float, reinforced: set[int],
             reinf_counts: dict[int, int], access: dict[int, int]) -> dict:
    spec = MIRASBandSpec(name="bench", max_entries=CAP,
                         retention_policy="balanced")
    band = build_band(spec, embedding_dim=DIM, device="cpu",
                      retention_boost=retention_boost)
    for i in range(N_TOTAL):
        band.store(text=f"e{i}", embedding=torch.zeros(DIM), source=SOURCE, surprise=0.0)
        e = band.entries[-1]
        e.db_id = i
        e.timestamp = float(i)              # higher i = newer
        e.access_count = access[i]
        e.reinforcements = reinf_counts.get(i, 0)

    fresh_unreinforced = set(range(N_TOTAL)) - reinforced
    survivors = {e.db_id for e in band.entries}
    surv_ts = [float(db_id) for db_id in survivors]
    rs = survivors & reinforced
    us = survivors & fresh_unreinforced
    return {
        "retention_boost": retention_boost,
        "reinforced_total": len(reinforced),
        "reinforced_survived": len(rs),
        "reinforced_survival_rate": len(rs) / max(1, len(reinforced)),
        "unreinforced_total": len(fresh_unreinforced),
        "unreinforced_survived": len(us),
        "unreinforced_survival_rate": len(us) / max(1, len(fresh_unreinforced)),
        # recency-displacement signal: mean recency (timestamp) of survivors.
        # Falls as the boost pins older reinforced entries over fresher ones.
        "mean_survivor_timestamp": (sum(surv_ts) / len(surv_ts)) if surv_ts else 0.0,
    }


def run_bench() -> list[dict]:
    rng = random.Random(SEED)
    reinforced, reinf_counts, access = _make_workload(rng)
    # Pin the eviction clock for determinism (band._evict_one calls now_seconds).
    orig = bandmod.now_seconds
    bandmod.now_seconds = lambda: _NOW
    try:
        return [_run_one(b, reinforced, reinf_counts, access) for b in GRID]
    finally:
        bandmod.now_seconds = orig


def main() -> None:
    rng = random.Random(SEED)
    reinforced, reinf_counts, _ = _make_workload(rng)
    counts = sorted(reinf_counts.values(), reverse=True)
    rows = run_bench()
    out = Path(__file__).resolve().parent / "results" / "retention.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))

    print(f"workload: {len(reinforced)}/{N_TOTAL} reinforced; "
          f"reinforcement counts max={counts[0]} "
          f"mean={sum(counts)/len(counts):.1f} top5={counts[:5]}")
    hdr = (f"{'boost':>6}  {'reinf_surv':>10}  {'unreinf_surv':>12}  "
           f"{'mean_surv_ts':>12}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['retention_boost']:>6}  "
              f"{r['reinforced_survival_rate']:>10.2f}  "
              f"{r['unreinforced_survival_rate']:>12.2f}  "
              f"{r['mean_survivor_timestamp']:>12.1f}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
