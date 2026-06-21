# Cosine spine (v0.5) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove the MIRAS test-time-trained neural memory (MLP + neural retrieval blend + HOPE chained read) from the shipped server; keep the 8 bands as plain **cosine** vector stores with a **novelty** surprise gate. The machinery is preserved on `archive/neural-memory-titans`.

**Architecture:** Bands become append+evict cosine stores. `band.retrieve` ranks by `pattern_matrix @ query`. `band.compute_surprise` becomes `1 − max cos(x, existing)`. The cortex / world / lessons / graph / dream / episode layers are untouched. `MemoryEntry`/`RetrievalResult` stay; the MLP modules/objectives/update-rules and the dead legacy `MemoryMLP`/`TitansMemoryBank` are deleted.

**Tech Stack:** Python 3.11/3.12, torch (CPU), PostgreSQL (psycopg), pytest, Docker.

**Spec:** `docs/specs/2026-06-22-cosine-spine-design.md` · **Rationale:** `docs/2026-06-21-neural-memory-investigation.md`

**Test invocation:** `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest <args>`

---

## File Structure

- `memory/miras/band.py` — **rewrite** to a cosine store (novelty surprise, cosine retrieve, no MLP).
- `memory/cms.py` — build plain bands; drop MLP update, chained read, weight persistence, neural stats.
- `memory/contrastive.py` — keep suppression; drop the band-MLP contrastive step.
- `memory/titans_memory.py` — keep `MemoryEntry`/`RetrievalResult`; delete `MemoryMLP` + `TitansMemoryBank`.
- `memory/miras/objectives.py`, `update_rules.py`, `modules.py` — **delete**.
- `memory/miras/protocols.py` — drop `MemoryModule`/`RetentionObjective`/`UpdateRule` ABCs (keep what's still used).
- `memory/miras/presets.py`, `utils/config.py` — strip neural fields/knobs; presets → plain specs.
- `memory/miras/retention.py` — keep eviction strategies; drop the MLP-coupled `elastic_net` L1.
- `README.md`, `CHANGELOG.md`, `pyproject.toml` — reframe + version 0.5.0.
- Tests — delete neural-only tests; adjust band/cms/config/service/write-through/episode tests.

---

## Task 1: Band → cosine store (+ cms wiring) — the atomic core

**Files:** Modify `memory/miras/band.py`, `memory/cms.py`, `memory/contrastive.py`; Test `tests/test_band_cosine.py` (new).

- [ ] **Step 1: Write failing tests** — `tests/test_band_cosine.py`:
```python
import torch
from pseudolife_memory.memory.miras.band import MIRASBand

def _band(**kw):
    # construct via cms's builder OR the new minimal signature; keep this helper
    # aligned with the post-Task-1 MIRASBand.__init__.
    return MIRASBand(name="t", max_entries=100, update_interval=1,
                     promotion_access_count=2, promotion_surprise=0.5,
                     retention_policy="balanced")

def test_compute_surprise_novelty():
    b = _band(); e = torch.nn.functional.normalize(torch.randn(384), dim=0)
    assert b.compute_surprise(e) == 1.0                       # empty band -> max novelty
    b.store("x", e, source="t", surprise=1.0)
    assert b.compute_surprise(e) < 0.05                       # exact dup -> ~0
    e2 = torch.nn.functional.normalize(torch.randn(384), dim=0)
    assert b.compute_surprise(e2) > 0.3                       # novel -> high

def test_retrieve_pure_cosine():
    b = _band()
    a = torch.nn.functional.normalize(torch.randn(384), dim=0)
    z = torch.nn.functional.normalize(torch.randn(384), dim=0)
    b.store("A", a, source="t", surprise=1.0)
    b.store("Z", z, source="t", surprise=1.0)
    res = b.retrieve(a, top_k=1)
    assert res.entries[0].text == "A"

def test_store_does_no_training():
    b = _band()
    assert not hasattr(b, "memory") and not hasattr(b, "update_rule")
```

- [ ] **Step 2: Run → FAIL** (`MIRASBand.__init__` still requires objective/module; `memory` attr exists).

- [ ] **Step 3: Implement** `band.py`:
  - `__init__(self, name, max_entries, update_interval, promotion_access_count, promotion_surprise, retention_policy="balanced", device=None)` — drop `objective`/`update_rule`/`memory_module`/`hidden_dim`/`learning_rate`/`weight_decay`/`neural_blend_weight`/`neural_warmup_updates`. Build the retention/eviction policy from `retention_policy`. Keep `entries`, `_pattern_matrix`/`_dirty`, `surprise_ema`, promotion counters.
  - Delete `memory` (MLP), `objective`, `update_rule`, `update_memory`, `contrastive_update`, `_effective_neural_weight`, eta/theta plumbing.
  - `compute_surprise(self, embedding)`: normalize; if no entries → `1.0`; else rebuild pattern matrix if dirty, `1.0 - float((pattern_matrix @ x).max())` clamped to `[0,1]`.
  - `retrieve(self, query_embedding, top_k)`: normalize query, `scores = pattern_matrix @ query`, top-k → `RetrievalResult` (bump `access_count` as before).
  - `store(...)`: append entry (with `surprise_score`), mark `_dirty`, evict via retention if over `max_entries`. No training.
  - Keep `get_state_dict`/`load_state_dict` for ENTRIES only (drop weights/optimizer keys; tolerant of legacy keys on load).
- [ ] **Step 3b:** `cms.py`:
  - `_build_bands`: construct `MIRASBand` with the new signature from each spec (no objective/module/rule). Remove `b.neural_blend_weight=`/`b.neural_warmup_updates=` (cms.py:137-138).
  - `store`: keep `per_band_surprise = [b.compute_surprise(e) ...]` (now novelty); remove any `update_memory` call.
  - Remove the chained-read block (cms.py ~830-851, 1008-1010) and its `_trace["chain_residual"]` entries; remove `band.memory.init_weights()` (cms.py:1529).
  - `memory_stats` (cms.py ~1624-1641): drop `objective`/`update_rule`/`memory_module`/`base_lr` per-band fields and the top-level `chain_residual`. Keep name/size/capacity/update_interval/hit_rate/hit_count/retention_policy.
  - Persistence: stop saving MLP weights/optimizer; keep entry state + version chain. (Full back-compat load test in Task 2.)
- [ ] **Step 3c:** `contrastive.py` `_contrast_band`: keep `_suppress_entry`; replace the `band.contrastive_update(...)` call with a no-op (the negative-gradient step is gone with the MLP). Leave a comment.
- [ ] **Step 4: Run → PASS** `tests/test_band_cosine.py`; then `…/pytest tests/test_cms.py tests/test_band*.py tests/test_contrastive*.py -q` (adjust those tests as needed — see Task 6 for the test-churn sweep; fix obvious breakages here).
- [ ] **Step 5: Commit** `feat(memory): bands become cosine stores — novelty surprise, cosine retrieve, no MLP`

---

## Task 2: Persistence back-compat (legacy weights ignored)

**Files:** Modify `memory/cms.py` (load path) if needed; Test `tests/test_cms_legacy_load.py` (new).

- [ ] **Step 1: Failing test** — build a CMS, hand-craft a state dict shaped like the legacy format (entries + a per-band `weights`/`optimizer` block), `load_state_dict`, assert entries load and no error; assert no `memory` attr appears.
- [ ] **Step 2: Run → FAIL** (loader trips on the weights block).
- [ ] **Step 3: Implement** — make the band/cms `load_state_dict` ignore unknown legacy keys (`weights`, `optimizer`, `update_count`, gate buffers). Entries-only hydrate.
- [ ] **Step 4: Run → PASS**; `…/pytest tests/test_write_through.py -q`.
- [ ] **Step 5: Commit** `fix(persistence): tolerant load of legacy MLP-weight state (entries only)`

---

## Task 3: Config + presets + retention cleanup

**Files:** Modify `utils/config.py`, `memory/miras/presets.py`, `memory/miras/retention.py`; Test: adjust `tests/test_phase0_config.py`, delete `tests/test_phase0_neural_ramp.py`.

- [ ] **Step 1:** Update/curate tests — remove assertions on `neural_blend_weight`/`neural_warmup_updates`/per-band `objective`/`update_rule`/`memory_module`; delete `test_phase0_neural_ramp.py` (feature removed). Add a test that `AppConfig().memory.miras.preset == "continuum"` still yields 8 bands with the kept fields.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement:**
  - `MIRASBandSpec`: keep `name`, `max_entries`, `update_interval`, `promotion_access_count`, `promotion_surprise`, `retention_policy`. Drop `hidden_dim`, `learning_rate`, `memory_module`, `update_rule`, `objective`, `objective_p`, `weight_decay`.
  - `MIRASConfig`: drop `chain_residual` + the `preset_chain_residual` wiring in `__post_init__`. Keep `preset`/`bands`.
  - `MemoryConfig`: remove `neural_blend_weight`, `neural_warmup_updates` (dataclass + the `load_config` lines ~577-578).
  - `presets.py`: rewrite specs to the kept fields. Keep `continuum` (8 bands) + `custom`; alias `titans`/`moneta`/`yaad`/`memora` → `continuum` with a deprecation note (or drop — pick the smaller diff). Remove `preset_chain_residual`.
  - `retention.py`: keep `balanced`/`recency_heavy`/`surprise_heavy` eviction; remove `elastic_net` (remap any preset use → `balanced`) and any `l1_coef` plumbing.
- [ ] **Step 4: Run → PASS**; `…/pytest tests/test_phase0_config.py -q`.
- [ ] **Step 5: Commit** `refactor(config): drop neural MIRAS knobs; bands keep size/cadence/eviction only`

---

## Task 4: Delete dead neural code

**Files:** Delete `memory/miras/objectives.py`, `update_rules.py`, `modules.py`; Modify `memory/titans_memory.py`, `memory/miras/protocols.py`, `memory/miras/__init__.py`; Delete `tests/test_objectives*.py`, `tests/test_update_rules*.py`, `tests/test_modules*.py` (whichever exist).

- [ ] **Step 1:** Grep to confirm no live imports remain: `grep -rn "miras.objectives\|miras.update_rules\|miras.modules\|MemoryMLP\|TitansMemoryBank\|build_objective\|build_update_rule\|build_module" pseudolife_memory/` → only the files being deleted.
- [ ] **Step 2: Implement:**
  - `titans_memory.py`: delete `MemoryMLP` and `TitansMemoryBank` classes + their imports (`SGDMomentumUpdate`, MLP3Module). Keep `MemoryEntry`, `RetrievalResult`, and any still-imported helpers.
  - Delete `objectives.py`, `update_rules.py`, `modules.py`.
  - `protocols.py`: remove `MemoryModule`/`RetentionObjective`/`UpdateRule` ABCs; keep protocols still referenced (e.g., a retention/eviction protocol if used).
  - `miras/__init__.py`: drop exports of the deleted symbols.
  - Delete the corresponding test files.
- [ ] **Step 3: Run** `…/pytest -q` collection (import check): `…/pytest --collect-only -q` must succeed (no import errors).
- [ ] **Step 4: Run** the memory suites green: `…/pytest tests/test_cms.py tests/test_band_cosine.py tests/test_phase0_config.py tests/test_write_through.py tests/test_service.py -q`.
- [ ] **Step 5: Commit** `refactor(memory): delete dead neural machinery (objectives/update_rules/modules + legacy MemoryMLP/TitansMemoryBank)`

---

## Task 5: Docs + version

**Files:** Modify `README.md`, `CHANGELOG.md`, `pyproject.toml`.

- [ ] **Step 1:** README — reframe the headline + architecture + capabilities table from "8-tier neural memory" to "recency-tiered **cosine** episodic bank + canonical-fact cortex + dream consolidation." Note the neural research is archived on `archive/neural-memory-titans`; link `docs/2026-06-21-neural-memory-investigation.md`. Fix any `memory_stats`/config snippets that show neural fields.
- [ ] **Step 2:** CHANGELOG `[Unreleased]` → a `## [0.5.0]` entry: removed the neural blend/MLP, bands now cosine stores w/ novelty surprise, `memory_stats` shape change, rationale (F1). `pyproject.toml` `0.4.0 → 0.5.0`.
- [ ] **Step 3: Commit** `docs: v0.5 cosine spine — reframe off neural memory; CHANGELOG; version 0.5.0`

---

## Task 6: Full verification + finish branch

- [ ] **Step 1: Test-churn sweep** — run the full suite; fix every remaining failure from removed neural fields (`tests/test_episodes.py`, `tests/test_schema_v6.py`, `tests/test_service.py` memory_stats asserts, any band/cms spec construction in tests). Delete tests that only covered removed behavior.
- [ ] **Step 2: Full suite** — `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest -q`. Expected: all green.
- [ ] **Step 3: Retrieval sanity** — `…/python evals/neural_blend_bench.py --n 120` still runs; note the harness now exercises only the OFF (cosine) path meaningfully (ON==OFF since the blend is gone). Optionally simplify/retire the harness (it lives on the archive branch regardless) — decide here; minimal: leave it, it still works against the cosine store.
- [ ] **Step 4:** Finish per superpowers:finishing-a-development-branch (merge to master + push, per the user's established no-PR flow). **Deploy is a separate backup-first step WITH the user** (code-only: `ops/backup.ps1` → rebuild daemon image → recreate → verify `memory_stats` counts intact + healthy + store/search round-trip; the orphaned `cms_state.pt` weights file is ignored).
- [ ] **Step 5: Commit** any final test fixes: `test: align suite with the cosine spine (remove neural-layer assertions)`

---

## Notes for the executor
- **Task 1 is the big atomic one** (band + cms can't be split — the system won't run with band half-converted). Take it carefully; everything after is cleanup that keeps green.
- **`MemoryEntry`/`RetrievalResult` are load-bearing** (imported across the whole package) — keep them in `titans_memory.py`.
- **Surprise semantics change** (reconstruction-error → novelty): keep `surprise_threshold=0.2`; if the gate behaves oddly in tests, re-tune rather than revert.
- **Reversibility:** the full machinery is on `archive/neural-memory-titans`; don't worry about preserving it inline.
