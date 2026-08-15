"""Smoke evidence for the flat-migration consumer review (edge case 4).

The 2026-08-14 consumer inventory (preregistration doc, gate G-E4) claims
three hard breaks a naive one-band migration would cause. Claims about
breakage get smoke evidence, not opinion — each check below runs the real
code (in-memory, toy embeddings, no Postgres, no embedder) and records
what actually happens. Writes a small tracked artifact.

    python evals/flat_migration_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

RESULTS = Path(__file__).resolve().parent / "results"
DIM = 8


def _cfg(bands):
    from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec
    cfg = MemoryConfig(embedding_dim=DIM)
    cfg.miras.preset = "custom"
    cfg.miras.bands = [MIRASBandSpec(**b) for b in bands]
    return cfg


def _flat_cms(cap=5):
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    return ContinuumMemorySystem(_cfg([{
        "name": "flat", "max_entries": cap,
        "update_interval": 1_000_000_000,
        "promotion_access_count": 1_000_000_000,
        "promotion_surprise": 1.1, "retention_policy": "balanced"}]))


def _two_band_cms(cap=5):
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    return ContinuumMemorySystem(_cfg([
        {"name": "working", "max_entries": cap, "update_interval": 1,
         "promotion_access_count": 2, "promotion_surprise": 0.4,
         "retention_policy": "balanced"},
        {"name": "slow", "max_entries": cap, "update_interval": 50,
         "promotion_access_count": 4, "promotion_surprise": 0.7,
         "retention_policy": "surprise_heavy"}]))


def _store_n(cms, n, prefix="turn"):
    import torch
    g = torch.Generator().manual_seed(0)
    for i in range(n):
        emb = torch.randn(DIM, generator=g)
        cms.store(f"{prefix} {i} unique content", emb, source="user")


def total(cms):
    return sum(len(b.entries) for b in cms.bands)


def main() -> int:
    checks = {}

    # ── Check 1: capacity eviction under n=1 destroys; under n>1 demotes ──
    # cms.py:2091 — `band_idx + 1 < len(self.bands)` can never hold with
    # one band, so the flow falls through to the delete path.
    flat = _flat_cms(cap=5)
    _store_n(flat, 8)
    banded = _two_band_cms(cap=5)
    _store_n(banded, 8)
    checks["evict_destroys_under_flat"] = {
        "flat_stored": 8, "flat_resident": total(flat),
        "banded_stored": 8, "banded_resident": total(banded),
        "claim_holds": total(flat) == 5 and total(banded) == 8,
        "cite": "cms.py:2091 — demotion cascade needs a next band; "
                "n=1 restores pre-2026-07-25 delete-on-evict",
    }

    # ── Check 2: legacy bands= filter silently returns empty ──────────────
    import torch
    q = torch.randn(DIM, generator=torch.Generator().manual_seed(1))
    r_ok = flat.retrieve(q, top_k=3)
    r_legacy = flat.retrieve(q, top_k=3, bands=["working", "instant"])
    checks["legacy_band_filter_silently_empty"] = {
        "unfiltered_hits": len(r_ok.entries),
        "legacy_filter_hits": len(r_legacy.entries),
        "raised": False,   # reaching this line means no exception
        "claim_holds": len(r_ok.entries) > 0 and not r_legacy.entries,
        "cite": "cms.py:737,758-765 — no validation of band names; "
                "documented usage (cms.py:673-675) goes dark",
    }

    # ── Check 3: file-mode state restore is keyed by band NAME ────────────
    # cms.py:1911 — a saved 'working'/'slow' state loaded into a config
    # whose only band is 'flat' matches nothing and restores nothing.
    import tempfile
    state_dir = tempfile.mkdtemp(prefix="e4smoke_")
    banded.save(state_dir)
    fresh_flat = _flat_cms(cap=50)
    fresh_flat.load(state_dir)
    checks["state_restore_loses_all_entries"] = {
        "saved_entries": total(banded), "restored_entries": total(fresh_flat),
        "claim_holds": total(fresh_flat) == 0,
        "cite": "cms.py:1907-1920 — bands matched by name, silent skip",
    }

    # ── Check 4: promotion chain is structurally dead under n=1 ───────────
    checks["promotion_dead_under_flat"] = {
        "flat_consolidation_events": len(getattr(
            flat, "_consolidation_events", [])),
        "claim_holds": not getattr(flat, "_consolidation_events", []),
        "cite": "cms.py:519 — range(len(bands)-1) is empty; "
                "update_interval/promotion_* become dead config",
    }

    ok = all(c["claim_holds"] for c in checks.values())
    out = {"all_claims_hold": ok, "checks": checks}
    dst = RESULTS / "abl25-e4-flat-migration-smoke.json"
    dst.write_text(json.dumps(out, indent=2), encoding="utf-8")
    for name, c in checks.items():
        print(f"{'PASS' if c['claim_holds'] else 'FAIL'}  {name}")
    print(f"wrote {dst}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
