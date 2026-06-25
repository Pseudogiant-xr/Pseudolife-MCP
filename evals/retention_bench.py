"""Eviction-dynamics bench for ``retention_boost`` (provenance-as-link Phase 2).

Drives the REAL ``MIRASBand`` + production ``RetentionPolicy`` under capacity
pressure to measure how ``retention_boost`` protects reinforced episodes versus
how it respects recency. Dev-only, CPU, deterministic, no Postgres.

Run: HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe evals/retention_bench.py
Writes: evals/results/retention.json
"""
from __future__ import annotations

import json
from pathlib import Path

import torch

import pseudolife_memory.memory.miras.band as bandmod
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.utils.config import MIRASBandSpec

DIM = 8
CAP = 50                 # band capacity
N_TOTAL = 200            # 4x capacity -> heavy, sustained eviction
REINFORCED_EVERY = 4     # every 4th entry is "reinforced" (got used)
REINFORCE_LEVEL = 5      # reinforcements on a reinforced entry
SOURCE = "agent_action"  # neutral source weight (1.0) so the boost is isolated
GRID = [0.0, 0.25, 0.5, 1.0, 2.0, 4.0]

# Deterministic "now" pinned past every entry timestamp so age = NOW - i is stable.
_NOW = float(N_TOTAL + 1)


def _run_one(retention_boost: float) -> dict:
    spec = MIRASBandSpec(name="bench", max_entries=CAP,
                         retention_policy="balanced")
    band = build_band(spec, embedding_dim=DIM, device="cpu",
                      retention_boost=retention_boost)
    reinforced, fresh_unreinforced = set(), set()
    for i in range(N_TOTAL):
        band.store(text=f"e{i}", embedding=torch.zeros(DIM), source=SOURCE, surprise=0.0)
        e = band.entries[-1]
        e.db_id = i
        e.timestamp = float(i)           # higher i = newer
        # access_count correlated with recency (newer entries were used more),
        # so the base eviction score has a real recency/usage gradient to
        # compete against the reinforcement boost.
        e.access_count = i
        if i % REINFORCED_EVERY == 0:
            e.reinforcements = REINFORCE_LEVEL
            reinforced.add(i)
        else:
            fresh_unreinforced.add(i)

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
    # Pin the eviction clock for determinism (band._evict_one calls now_seconds).
    orig = bandmod.now_seconds
    bandmod.now_seconds = lambda: _NOW
    try:
        return [_run_one(b) for b in GRID]
    finally:
        bandmod.now_seconds = orig


def main() -> None:
    rows = run_bench()
    out = Path(__file__).resolve().parent / "results" / "retention.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
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
