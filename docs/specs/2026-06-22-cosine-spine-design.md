# Cosine spine — strip the neural memory layer (v0.5) — design

**Date:** 2026-06-22 · **Status:** approved (design), pending plan
**Background:** `docs/2026-06-21-neural-memory-investigation.md` (F1 root cause) ·
**Archive of the removed machinery:** branch `archive/neural-memory-titans`

## Goal

Remove the MIRAS **test-time-trained MLP neural memory** (per-store training + the
neural retrieval blend + the HOPE-style chained read) from the shipped server.
Keep the **8-tier band structure as plain cosine vector stores** — surprise
routing, promotion, recency, eviction, and the dream cursor all unchanged. The
result is the honest "spine": **cortex (slot facts + dream consolidation) + a
recency-tiered cosine episodic bank**. The neural machinery is preserved verbatim
on `archive/neural-memory-titans` for the separate TITANS/HOPE research track.

**Why:** the F1 eval proved the neural blend *hurts* retrieval (pure cosine beats
the shipped `w=0.6` blend at every scale; MLP-only ≈ random; `cos(M(x),x)≈0.4`),
and the root cause is a regime mismatch (TITANS/HOPE are end-to-end-trained
sequence models; here the memory is a self-reconstruction autoencoder over frozen
embeddings with no training loop). The MLP also costs a forward+backward+optimizer
step on **every** store for an output that's only used by the harmful blend.

**Non-goals:** changing the cortex/world/lessons/graph/dream/episode layers (all
untouched); reducing the tier *count* (kept at 8 — a separate, measurable
follow-up); building the TITANS toy (separate research track).

## The one load-bearing change: surprise metric

The store gate (`surprise_threshold`) currently uses the MLP:
`band.compute_surprise(x) = 1 − cos(M(x), x)` (self-reconstruction error).
With the MLP gone, replace it with **novelty surprise**:

```
compute_surprise(x) = 1 − max_j cos(x, x_j)   over the band's existing entries
                    = 1.0                      when the band is empty
```

This reuses the band's existing cosine pattern matrix (cheap), and it is what the
gate was always meant to express — *"don't re-store something we already hold."*
`cms.store` keeps `overall_surprise = min(per_band_surprise)`, so "already known
in any tier → gated" is preserved. The MCP default `surprise_threshold = 0.2`
carries over (store unless ≥0.8 cosine-similar to an existing entry); flagged
re-tunable. `surprise_score` stored on each entry now means novelty-at-store-time;
promotion (which reads `surprise_score`/`access_count`) is unaffected in shape.

## Components & changes

### `memory/miras/band.py`
- **Remove:** `self.memory` (MLP), `self.objective`, `self.update_rule`,
  `update_memory`, `contrastive_update`, `_effective_neural_weight`,
  `neural_blend_weight`/`neural_warmup_updates`, eta/theta gate plumbing,
  the neural half of `retrieve`.
- `retrieve` → pure cosine: `scores = pattern_matrix @ query`, top-k (the existing
  `exact_scores`).
- `compute_surprise` → novelty (formula above).
- `store` → append + eviction; **no training step**. Keep `update_count` only if a
  promotion-cadence counter needs it (promotion fires on logical turns, not MLP
  updates — verify and keep that counter).
- Keep: `entries`, `max_entries` capacity + eviction, promotion thresholds,
  `surprise_ema` (telemetry), the pattern matrix.

### Delete (preserved on the archive branch)
- `memory/miras/objectives.py`, `memory/miras/update_rules.py`,
  `memory/miras/modules.py`.
- `protocols.py`: drop the `MemoryModule` / `RetentionObjective` / `UpdateRule`
  ABCs (keep any band/retention protocol still used).

### `memory/miras/presets.py` + `utils/config.py`
- `MIRASBandSpec`: drop `hidden_dim`, `learning_rate`, `memory_module`,
  `update_rule`, `objective`, `objective_p`, `weight_decay`. Keep `name`,
  `max_entries`, `update_interval`, `promotion_access_count`,
  `promotion_surprise`, `retention_policy`.
- `MIRASConfig`: drop `chain_residual` (HOPE chained read removed). Keep `preset`
  + `bands`. Presets become plain band specs (sizes / intervals / capacities /
  promotion / eviction policy). Keep the `continuum` (8-tier) and `custom` presets;
  retire the neural-experiment presets (`moneta`/`yaad`/`memora`) or collapse them
  to aliases of `continuum` — decide in the plan (lean: keep `continuum` + `custom`,
  alias the rest with a deprecation note).
- `MemoryConfig`: remove `neural_blend_weight`, `neural_warmup_updates`.

### `memory/miras/retention.py`
- Keep eviction strategies (`balanced` / `recency_heavy` / `surprise_heavy`) that
  pick which entry to drop at capacity. Remove the MLP-coupled `elastic_net` L1
  path (it only fed the update wrapper). Any preset referencing `elastic_net`
  remaps to `balanced`.

### `memory/cms.py`
- `_build_bands`: construct plain bands (no objective/update-rule/module).
- `store`: surprise via the bands' novelty `compute_surprise` (interface
  unchanged); **no MLP update**; remove the chained-read branch.
- Persistence: stop writing/reading MLP **weights**/optimizer state. Keep **entry**
  persistence + the state-version compatibility chain. `save_weights` becomes a
  no-op (or is removed and its callers updated).
- `memory_stats`: per-band dict loses `objective` / `update_rule` /
  `retention_policy` (keep `retention_policy` if still meaningful) /
  `memory_module` / `base_lr` / `update_rule`. Keep `name`, `size`, `capacity`,
  `update_interval`, `hit_rate`, `hit_count`.

### `service.py` / `mcp_server.py`
- `_persist_all`: in PG mode the MLP weights were "the only file artifact" — after
  the strip there is no weights file; entries are already transactional in PG, so
  the save path simplifies (weights step removed). File mode still persists
  entries via the CMS state.
- No tool signatures change. `memory_stats` output shape changes (documented).

### Persistence back-compat (must not break the live bank)
- **PG mode (the live deployment):** entries live in Postgres; the daemon hydrates
  from PG on start. The existing `cms_state.pt` held only MLP weights → after the
  strip it's unused; a **tolerant loader ignores any legacy weights block**. No
  data migration; entries untouched.
- **File mode:** the `.pt` holds entries; keep loading them, ignore the legacy
  weights/optimizer block. Add a back-compat test loading an old-format state.

### Docs / version
- `README.md`: reframe the headline from "8-tier neural memory" to
  "recency-tiered **cosine** episodic bank + canonical-fact cortex + dream
  consolidation." Capabilities table + architecture section updated. Link the
  investigation doc and the archive branch for the neural research track.
- `CHANGELOG.md`: the strip, the surprise-metric change, the `memory_stats` shape
  change, the F1 rationale.
- `pyproject.toml`: `0.4.0 → 0.5.0` (architecture simplification).

## Testing

- **Delete:** `tests/test_objectives*`, `tests/test_update_rules*`,
  `tests/test_modules*`, and the neural-blend/`update_memory`/`chain_residual`
  tests in `tests/test_band*` / `tests/test_cms*` (whatever exists).
- **Add:**
  - `band.compute_surprise` novelty: empty band → 1.0; exact duplicate → ~0.0;
    novel vector → high.
  - store gate: a near-duplicate is gated (`was_stored=False`); a novel item stored.
  - `band.retrieve` ranks by pure cosine (gold in top-k).
  - persistence back-compat: load a legacy state with a weights block → entries
    load, weights ignored, no error.
- **Adjust:** `test_cms`, `test_band`, `test_write_through`, `test_service`
  (memory_stats fields), and any test asserting per-band neural fields.
- **Full suite green** before finishing.

## Success criteria
- Full suite green.
- Retrieval recall ≥ current (this *is* the proven-better cosine path; spot-check
  with `evals/neural_blend_bench.py` OFF condition).
- No MLP work on store (store path has no backward pass); no weights file in PG mode.
- Live deploy: `memory_stats` entry counts intact, daemon healthy, a store+search
  round-trip works.

## Risks & mitigations
- **Surprise-gate semantics shift** (reconstruction-error → novelty): more
  sensible, but changes which items gate. Mitigation: explicit gate tests; keep
  `surprise_threshold=0.2`, note re-tunable.
- **Legacy `.pt` load**: tolerant loader + back-compat test.
- **Test churn**: large but mechanical; handle task-by-task in the plan.
- **Reversibility**: full machinery on `archive/neural-memory-titans`; a future
  neural layer re-attaches additively to the unchanged cosine-store substrate.

## Deploy
Code-only change (no DB schema/migration). After merge: `ops/backup.ps1` →
rebuild daemon image → recreate container → verify `memory_stats` counts intact +
healthy + store/search round-trip. The orphaned `cms_state.pt` weights file on the
live volume is harmless (loader ignores it); may be deleted.
