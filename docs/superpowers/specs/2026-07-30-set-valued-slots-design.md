# Set-valued cortex slots (C2) — design

**Date:** 2026-07-30
**Status:** approved design, pre-implementation
**Schema:** v25 → v26

## Problem

A cortex slot holds exactly one current value per `(entity, attribute)`.
Facts whose truth is a *collection* — restaurants tried, bikes owned,
pending hardware upgrades — cannot be expressed: each new mention either
supersedes the previous member (destroying it) or mints a sibling slot
(fragmenting the key space, the disease the 2026-06-19 single-writer
decision fought). Measured consequence: on the LongMemEval-KU oracle
slice, 48/78 questions are count/aggregate ("how many X…") and the fact
spine holds the answer for none of the counting-over-members cases
(2026-07-30 ceiling-e2e diagnosis). The cascade result made fact
*coverage* the binding constraint on end-to-end accuracy; ranking-side
work is a measured dead-end (B1, `bm25-ab-confirmation.json`).

## Goals and success criteria (agreed 2026-07-30)

- **Product-first:** correct membership state across sessions — add,
  remove, convert, audit — for real agent workloads.
- **Full lifecycle in v1:** members join and leave; membership history
  is queryable as of a time.
- **Writers:** both the dream extractor (claim-level `op`) and explicit
  MCP tools.
- KU-oracle is the *proxy gate*, not the target: movement on the
  count/aggregate class without regression elsewhere.

## Data model

`CortexRecord` gains `kind: "scalar" | "member"` (default `"scalar"`).
A slot is a set iff its records are members. No new record class — the
existing lifecycle, provenance, HLC and embedding fields apply per
member.

Status vocabulary gains **`removed`** (alongside `current` /
`superseded` / `retired` / `contested`):

| membership event | record change |
|---|---|
| member joins | new row, `kind='member'`, `status='current'` |
| member leaves | row → `status='removed'`, timestamp in `superseded_at` |
| member re-joins | new `current` row (old `removed` row stays as audit) |

Counting = current members. "How many before T" = members with
`asserted_at < T` (and not removed before T). Former members = the
`removed` rows. Nothing is deleted, matching the supersession spine.

### Postgres (schema v26)

- `facts` gains two columns: `kind TEXT NOT NULL DEFAULT 'scalar'` and
  `value_norm TEXT` (normalized member text, populated on write;
  scalar rows may leave it NULL). Existing rows are scalars; no
  backfill.
- `facts_slot_current_uq` is replaced by two partial unique indexes:
  - scalars (unchanged shape):
    `(entity_norm, attribute_norm) WHERE status='current' AND kind='scalar'`
  - members:
    `(entity_norm, attribute_norm, value_norm) WHERE status='current' AND kind='member'`
  (`value_norm` is the normalized member text; added as a stored column
  for the index.)
- DDL is additive; the index swap is `DROP INDEX` + two `CREATE INDEX`
  in one migration step.

### Member identity (dedup)

The DB enforces exact-normalized uniqueness only. At write time, a
candidate member is first compared against current members of the slot
with the same normalized-text + embedding-similarity treatment slot
resolution already uses; a match above threshold is a **confirm**
(bump `last_confirmed`, merge provenance), not a new member. This is
the mitigation for extractor paraphrase-minting ("Seoul Garden" vs
"Seoul Garden restaurant") — the Stage-1.5 key-discipline lesson
applied to members.

### Kind-conflict rules (fixed, no discretion at runtime)

- **Member-add to a scalar slot:** the scalar's current value is
  superseded (audited, `superseded_by_value = "(converted to set)"`)
  and re-minted as the first member. One-way conversion, logged.
- **Scalar write to a set slot:** tools path → rejected with an error
  naming `memory_set_add` / `memory_set_remove`; extractor path →
  logged and dropped. No silent flattening in either direction.

## Write surfaces

### MCP tools

- `memory_set_add(entity, attribute, member)` — add/confirm a member.
- `memory_set_remove(entity, attribute, member)` — remove (audited).
- Reads use the existing surface: `memory_fact_get` / `cortex_lookup`
  return `{kind: "set", members: [...], removed: [...]}` for set slots.

String-only parameters (the known client-side anyOf-stringification
bug rules out list/dict params). Tier placement follows the existing
toolset ladder; add/remove sit with `memory_fact_set`.

### Dream extractor

The claim JSON gains one optional key: `"op": "add" | "remove"`
(absent = scalar supersede, today's behaviour). The extraction prompt
documents when to use it (collection membership, not value updates).
This is a dream-write-path change: **the ladder re-run rule applies**,
plus the window-echo / stale-leak checks.

## Retrieval and serving

A set slot surfaces as **one entry per slot** everywhere:

```
entity — attribute: m1; m2; m3; m4 (4 members)
```

with removed members appended the way superseded values already are
("former members: …"). One line per set keeps context-token growth flat
in set size.

- **Ranking:** the slot's dense score is the **max member cosine**;
  member rows already carry embeddings, so no new embedding machinery.
- **`svc.history()`** on a set slot returns the membership timeline
  (adds and removals, HLC-ordered) — the set analogue of the
  "earlier values" garnish the bench serves.
- **`evals/rebuild_contexts.py`** receives the same composition, pinned
  by extending
  `tests/test_cortex_bm25.py::test_rebuild_fact_ranking_matches_service_fusion`
  (the lockstep guard added 2026-07-30 after the gate passed a channel
  it never executed).

## Evaluation gates

Exact pass/fail wording is pre-registered at plan stage, before any
GPU run. The gates:

1. **Ladder re-run** (dream-write-path rule) — stale-leak 0.0 required.
2. **KU-oracle fresh e2e**, set-capable prompt vs current prompt,
   paired per question: movement on the count/aggregate class, no
   overall regression; cascade reported alongside the three arms.
3. **Deterministic membership probe** (new, unit-level): seeded
   add / remove / re-add / convert / dedup scenarios asserting exact
   store state — product correctness independent of any LLM.
4. **Regression gate** (`evals/regression_gate.ps1`) as always.

## Migration and deployment

- Schema v26 four-place checklist: `SCHEMA_META_VERSION`, README
  capabilities table + configuration-guide DSN/version rows, version-pin
  tests (existing pins + new `tests/test_schema_v26.py`), CHANGELOG
  mention of v26.
- Deploy via `ops/update.ps1` (backup → rollback tag → daemon-only
  rebuild → health). Live verify: psql inspection of the two new index
  shapes, then an MCP `memory_set_add` → `memory_fact_get` roundtrip
  through the daemon.

## Risks

1. **Extractor op-discipline** (top risk): extractors mint rather than
   reuse identities (Stage-1.5). Mitigations: member fuzzy dedup at
   write; measured, not trusted — ladder + e2e gates decide.
2. **Member explosion:** soft cap of 100 current members per slot;
   adds beyond the cap are logged and dropped.
3. **Serving regressions:** one-line composition is designed
   token-neutral; verified in the e2e gate, not assumed.

## Out of scope (v1)

- As-of-time *query surface* beyond `history()` (the data supports it;
  no dedicated tool yet).
- Set-valued world facts and lessons (cortex only in v1).
- Graph edges per member (members are values, not entities, until a
  measured need appears).
