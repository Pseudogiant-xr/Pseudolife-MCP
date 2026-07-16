# Small-model supersession + tunable abstention guard — design spec

Status: **proposed (design)**, pending review + plan.
Target: **Pseudolife-MCP** `origin/master`. Two small, independent, data-driven
knobs that close the two gaps the 2026-06-18 extractor-ladder sweep exposed
(`evals/README.md` §Findings). Both are additive and behaviour-preserving at
their default values. Touches `pseudolife_memory/memory/cortex.py`,
`pseudolife_memory/service.py`, `pseudolife_memory/mcp_server.py`,
`pseudolife_memory/utils/config.py`, `evals/ladder_sweep.py`, docs, tests.

Related: [pluggable-LLM-extraction design](2026-06-18-pluggable-llm-extraction-design.md)
(the ladder this builds on) and `evals/README.md` (the sweep that produced the
two findings below).

## 1. Problem

The extractor-ladder sweep cleared every LLM rung on gold-recovery and tokens,
but surfaced two residual gaps — both *downstream* of extraction quality:

**(a) Small models don't supersede.** Only `Qwen3.6-27B` drove `stale_leak → 0.0`.
Every smaller rung (Gemma E2B/E4B, Qwen-A3B) plateaued at `stale_leak 0.1`,
`superseded=0`. Root cause is **not** bad extraction — the claims are correct —
it's **slot identity**. `CortexStore.write_fact` keys a fact on the *exact*
normalised `(entity, attribute)` pair (`cortex.py:175`). When a small model
paraphrases the entity or attribute between the `initial` turn and the `update`
turn — `payments-db host` then `payments database host`, or `api-gateway request`
then `api-gateway` — the two claims land on **sibling slots** instead of the same
slot, so the update never supersedes the stale value. The big model wins only
because it happens to name the slot identically twice. We are one paraphrase away
from a stale leak on every model that isn't 27B.

**(b) Abstention is cortex-guard-limited, not floor-limited.** The abstention
sub-sweep showed `false_abstain = 0.0` at every `search_confidence_floor`, but
`abstain_recall` plateaus at 0.33. The binding constraint is the **hardcoded**
`min_score=0.3` cortex guard at `mcp_server.py:244`: any topically-adjacent
current fact scoring ≥0.3 gets surfaced as a `cortex` hit, and a surfaced cortex
hit unconditionally suppresses `low_confidence` (`mcp_server.py:269`). So an
"entity-present / attribute-absent" near-miss (e.g. asking for `payments-db
password` when only its `host` is known, score ~0.735) reads as "we have the
answer" and blocks the abstention the operator asked for. The lever for more
abstention recall is the guard threshold — and it's currently a literal, not a
knob.

## 2. Goals / non-goals

**Goals**
- Make supersession **robust to entity/attribute paraphrase** so small,
  cheap extractors retire stale values the way 27B does — without forcing
  fuzzy merges onto deliberate writes.
- Make the **abstention guard tunable** (config knob) so the
  abstain-recall ↔ false-abstain trade-off is the operator's to set, with a
  calibrated recommendation from `evals/`.
- Keep both **behaviour-preserving at default** — a bank that upgrades and
  changes nothing in config behaves exactly as today.
- **Calibrate, don't guess** — ship defaults the `evals/` sweep justifies, the
  same posture as the sidecar/abstention defaults already in the tree.

**Non-goals**
- No fuzzy matching on the **deliberate** write path (`memory_fact_set`, direct
  `cortex_write`, store-time auto-promote) — those stay exact-keyed. Fuzzy slot
  resolution is **dream-only**, where claims are agent-tier guesses being
  consolidated, not user assertions.
- No change to the provenance/contender policy — the resolver fixes *which slot*
  a dreamed claim lands on; the existing guard still decides supersede-vs-contend.
- No new extractor code, no new MCP tools, no schema-destructive migration.
- No prompt-engineering the small models into naming slots consistently — that's
  brittle and model-specific; we fix it structurally in the store.

## 3. Feature A — paraphrase-robust dream supersession

### 3.1 Where it runs (and where it doesn't)

Resolution happens **only in the dream path**, between extraction and the write:
`dream_run` already loops the extracted claims and calls `cortex_write` per claim
(`service.py:1288`). We insert a **slot-resolution step** before each write that
maps a dreamed claim's `(entity, attribute)` onto an existing **current** slot
when a confident embedding match exists, then writes to that canonical slot — so
the normal exact-key write path fires `supersede` instead of forking a sibling.

`CortexStore.write_fact` stays exact-keyed and untouched. Deliberate writes
(`memory_fact_set`, `cortex_write`, store-time auto-promote) never go near the
resolver — no surprise fuzzy merges on user-stated facts.

### 3.2 The resolver

For a dreamed claim `(entity, attribute, value)`:

1. **Exact key hit.** If `(_norm_key(entity), _norm_key(attribute))` is already a
   current slot → pass through unchanged (today's behaviour, the `Qwen-27B` path).
2. **Miss → embedding fallback.** Embed the **slot text** `f"{entity} {attribute}"`
   (value *excluded* — value is the thing that's changing) and score it against
   every current record's **slot embedding** by cosine.
3. **Adopt the argmax candidate** iff `cosine ≥ dream_slot_match_threshold`;
   rewrite the claim's `(entity, attribute)` to that record's, then write. The
   exact-key path now matches the canonical slot and supersedes. Below threshold →
   write as a new slot (today's behaviour).

The resolver decides slot **identity** only. The existing provenance guard then
decides the outcome: an agent-tier dreamed update onto a user-tier slot is
**parked as a contender** (correct — an agent guess must not silently overwrite a
user fact); under `protect_provenance=False` (the bench) it's pure newer-wins →
supersede. This is why the benchmark will show `superseded` rise while production
banks see correct contend-vs-supersede behaviour.

### 3.3 Slot embeddings (schema v8, additive)

The stored `rec.embedding` is the **full claim** including the value
(`service.py:1008`), so it is the wrong signal for matching across a value change
— two values for the same slot can sit far apart. We need a **value-free slot
embedding**.

Recommended: add `slot_embedding: torch.Tensor | None` to `CortexRecord`
(`SCHEMA_VERSION 7 → 8`), computed in `cortex_write` from `f"{entity} {attribute}"`
and supplied to `write_fact` via dependency injection (the store stays
embedder-agnostic, as today). Round-trips through the existing `save`/`load`
(torch already persists the value embedding; one more tensor is free). The field
defaults to `None`, so:
- **Backward compatible** — legacy banks load with `slot_embedding=None`; no
  destructive migration (important given the bank-wipe history).
- **Backfill on confirm** — when a `cortex_write` caller (a dream, or
  `memory_fact_set`) confirms a record that predates v8, `write_fact` backfills
  its `slot_embedding` from the supplied value. The resolver deliberately does
  *not* eagerly backfill every current record: that would pull never-dreamed
  auto-promoted "junk" slots (the deterministic floor emits entities like
  *"Update the checkout-service"*) into the fuzzy-match candidate set and expand
  the false-merge surface. Records heal as dreams touch them; an untouched legacy
  slot simply isn't a fuzzy-match candidate until then. *(Implementation note:
  the original draft put the backfill loop in the resolver; it was moved to the
  `write_fact` confirm path during implementation for exactly this precision
  reason.)*

Alternative (no schema bump): recompute all current-slot embeddings on the fly,
once per `dream_run` batch, and reuse across the batch's claims. Cheaper to ship,
but recomputed every dream. Given dreams are off the hot path and current-slot
counts are modest this is viable — but the persisted field is the cleaner,
symmetric design (resolution becomes one matmul, exactly like `cortex.search`),
so it's the recommendation.

### 3.4 Precision controls

The only defence against a **false merge** (collapsing two genuinely different
slots) is the threshold, since small models paraphrase *attributes* too — we
can't hard-require an attribute match. So:
- The threshold is calibrated on a corpus that **includes distractors**:
  same-entity/different-attribute (`payments-db host` vs `payments-db password`)
  and different-entity/same-attribute (`payments-db host` vs `cache-db host`).
  The chosen value sits above the distractor band and below the paraphrase band.
- `dream_slot_match_threshold` is in `[0, 1]`; **`≤ 0.0` disables** fuzzy
  resolution entirely (exact-key only = today's behaviour). A positive value is
  the cosine floor to adopt a fuzzy slot.

### 3.5 Config + default posture

New field on `CortexConfig` (`config.py:339`, parsed via `_dict_to_dataclass`, so
it wires automatically):

```python
dream_slot_match_threshold: float = 0.0   # 0 = off (exact-key only); positive = cosine floor
```

Default ships **off** in this spec (behaviour-preserving). Flipping the default to
the calibrated value is gated on the §5 supersession sub-sweep showing **zero
false-merges on the distractor set** — the same "earn the default with data"
posture as the sidecar. If the sub-sweep is clean we set the default to the
calibrated value in the same change; if not, it stays off pending tuning and the
recommended value is documented.

## 4. Feature B — tunable abstention guard

### 4.1 The knob

Replace the literal at `mcp_server.py:244`:

```python
facts = service.cortex_search(query, top_k=5, min_score=cc.guard_min_score)
```

with a new `CortexConfig` field:

```python
guard_min_score: float = 0.3   # default = today's hardcoded behaviour
```

Raising it makes the cortex block stricter (only high-confidence facts surface)
**and** lets weak topically-adjacent facts stop suppressing abstention (via the
existing `mcp_server.py:269` rule) — so `abstain_recall` rises. Too high risks
suppressing a genuinely-correct-but-modestly-scored fact (`false_abstain`). The
operator (and the calibration sweep) pick the knee. Default `0.3` is exactly
today's behaviour, so an unconfigured bank is unchanged.

Config-only for now (the `memory_search` tool signature stays stable); a per-call
override is a trivial future extension if wanted.

### 4.2 Interaction with `search_confidence_floor`

These are two stages of one abstention decision:
- `guard_min_score` decides **which cortex facts count as an answer** (and thus
  suppress abstention).
- `search_confidence_floor` decides abstention from the **associative** side when
  no cortex fact survives the guard.

They're calibrated as a **pair** (§5). The finding to confirm: with the guard
raised, `search_confidence_floor` regains its intended effect on the
entity-present/attribute-absent near-misses.

## 5. Calibration — `evals/` extensions (dev-only)

Two additive measurements in `evals/ladder_sweep.py`, no new harness:

- **Supersession sub-sweep** (Feature A). On a fixed extractor rung known to
  paraphrase (Gemma E2B), ingest the update-pair corpus *augmented with
  distractor pairs*, then sweep `dream_slot_match_threshold ∈
  {off, 0.80, 0.85, 0.90, 0.95}` and report, per threshold:
  - `superseded` ↑ and `stale_leak` ↓ (the win), and
  - `false_merge` ↓ — distractor slots wrongly collapsed (the cost).
  The shipped default is the lowest threshold that drives `stale_leak` down at
  **`false_merge = 0`**. If none does, default stays off.
- **Guard sub-sweep** (Feature B). Extend the existing `--abstain` sweep to also
  vary `guard_min_score ∈ {0.3, 0.5, 0.65, 0.75, 0.85}` (today it only varies
  `search_confidence_floor`), emitting the `abstain_recall ↔ false_abstain`
  curve over the `(guard_min_score, search_confidence_floor)` grid. Pick the pair
  that maximises `abstain_recall` at `false_abstain ≈ 0`.

`evals/README.md` gains a short row for each, mirroring the existing findings
section.

## 6. Error handling / degradation

- **Resolver embedding failure** (embedder hiccup) → fall back to exact-key write
  (today's behaviour). The resolver must never break a dream — same contract as
  the extractor (`dream_run` already wraps extraction in try/except).
- **No current slots / empty cortex** → nothing to match against → exact-key
  insert. First-ever dream on a fresh bank is unaffected.
- **Legacy records without `slot_embedding`** → not fuzzy-match candidates until
  a `cortex_write` confirms them (then `write_fact` backfills); `resolve_slot`
  skips any record whose `slot_embedding` is `None`, so a missing embedding is
  never a crash, only a (temporary) non-match.
- **Guard knob mis-set too high** → conservative failure: more abstention, never
  fabrication; caught by the §5 sweep before shipping a default.

## 7. Testing

- **Unit (resolver):** stub embedder returning controlled vectors; assert a
  near-duplicate slot (cosine above threshold) is adopted → `superseded`, a
  distractor (below threshold) → new `inserted`, and threshold `≤0` → always
  exact-key (no adoption).
- **Unit (config):** `guard_min_score` / `dream_slot_match_threshold` parse from a
  `memory.cortex` block and default to `0.3` / `0.0`.
- **Integration (dream):** ingest an `initial` + paraphrased `update` pair, run
  `dream_run` with a stub extractor that paraphrases the entity, and assert the
  update **supersedes** (not forks a sibling) with the threshold on, and **forks**
  with it off — proving the path end-to-end without a live model.
- **Integration (abstention):** with a single topically-adjacent fact present,
  assert `guard_min_score=0.3` suppresses `low_confidence` but a raised guard lets
  `low_confidence=True` through, while a genuine cortex answer is never abstained.
- **Schema v8 round-trip:** save/load a record with and without `slot_embedding`;
  assert legacy (`None`) loads clean and a v8 record round-trips the tensor.
- Sweeps themselves live in `evals/` (dev-only, not unit tests).

## 8. Touches

- `pseudolife_memory/memory/cortex.py` — `CortexRecord.slot_embedding`;
  `SCHEMA_VERSION 7→8`; `write_fact` accepts/stores it; `save`/`load` round-trip;
  a current-record slot-embedding accessor for the resolver.
- `pseudolife_memory/service.py` — slot-resolution helper + call it in
  `dream_run` before `cortex_write`; pass `slot_embedding` through `cortex_write`;
  lazy backfill.
- `pseudolife_memory/mcp_server.py` — `min_score=cc.guard_min_score` at the cortex
  guard.
- `pseudolife_memory/utils/config.py` — `CortexConfig.guard_min_score`,
  `CortexConfig.dream_slot_match_threshold` (both auto-parse).
- `evals/ladder_sweep.py` + `evals/README.md` — supersession + guard sub-sweeps.
- `README.md`, `CHANGELOG.md`, tests.

## 9. Deferred / out of scope

- Fuzzy resolution on the deliberate write path (kept exact on purpose).
- Alias-graph-aware slot resolution (resolve via `entity_aliases` before
  embedding) — a plausible precision booster, but additive and separable.
- Re-embedding superseded records' slot embeddings (only `current` needs them).
- Per-call `guard_min_score` / `min_score` override on `memory_search`.

## 10. Decisions (locked at design)

1. Supersession fix lives in the **dream path**, not `write_fact` — deliberate
   writes stay exact-keyed.
2. Match on a **value-free slot embedding**, persisted as `CortexRecord.slot_embedding`
   (schema v8, additive, backfilled on confirm) — not the existing full-claim embedding.
3. Precision rests on a **calibrated threshold** (can't hard-require attribute
   match); distractors are in the calibration corpus.
4. Both knobs ship **behaviour-preserving at default**; flipping
   `dream_slot_match_threshold` on by default is gated on a zero-false-merge
   sub-sweep.
5. Abstention guard becomes `cortex.guard_min_score` (default `0.3` = today),
   calibrated as a **pair** with `search_confidence_floor`.
