# Cortex freshness inference — classify entities, not facts

**Date:** 2026-07-27
**Status:** design, approved for planning
**Schema:** v24 (`entity_kinds`)

## Why

Schema v23 gave cortex facts a `freshness_class`, but nothing sets it, so every
fact in the bank is `evergreen` and nothing ever ages out. This closes that gap.

The work began as "entity canonicalisation" — the presumed root cause of an
agent reading a stale extractor-prompt version. Measuring the live bank first
redirected it. The motivating cluster, all twelve rows:

| Slot | Value | Asserted | Status |
|---|---|---|---|
| `daemon / schema-version` | 12 | 06-24 | **current** |
| `pseudolife-mcp-daemon / schema-version` | 13 | 06-25 | **current** |
| `schema / version` | 19 | 07-02 | **current** |
| `entity-proposals-table / schema-version` | v18 | 06-29 | current |
| `entity-proposal-table / schema-version` | v18 | 06-29 | current |
| `0-9-0-release / schema-version` | v22 | 07-20 | current |
| `pseudolife-mcp / deployed-schema-version` | v23 | 07-27 | current |

Exactly **one** pair is a true alias duplicate (singular/plural). Three are
facts that were true when written and were never retired. The rest are
legitimately scoped — "the schema version as of the 0.9.0 release" is
permanently true and must never be merged with anything.

**The dominant failure is temporal, not nominal.** Canonicalisation would have
fixed one row. Corroborating measurements on the live bank:

- Token-Jaccard clustering flags 200/1004 entities (19.9%) as duplicates, but
  the clusters are dominated by genuinely distinct things — `gemma-4-e2b` vs
  `gemma-4-e4b`, `pr-1-branch`…`pr-6-branch`, `commit-hash-from` vs
  `commit-hash-to`. Genuine alias clusters: roughly 4–6 of 74.
- "Identical value at 2+ slots" finds 44 values, dominated by dates —
  `2026-07-19` at seven slots means seven things happened that day.
- Slot-key-leakage entities (`0-9-0-release-bm25`) stop at 2026-07-25, one day
  before the PR #48–51 root-cause fix. Static legacy (~41), not an ongoing leak.

This independently reproduces the Stage 1.5 finding (23 legitimate merges in
3257 keys) on live data rather than synthetic extractor labels. Entity aliasing
is explicitly **out of scope** here and left to its own evidence.

## Core insight — classify entities, not facts

Whether a fact rots is determined by *what kind of thing it is about*:

- `0-9-0-release / schema-version = v22` — the release is **frozen in time**.
  Permanently true. Evergreen regardless of the attribute name.
- `daemon / schema-version = 12` — the daemon is a **live system**. Volatile.

Identical attribute, opposite class. The attribute name cannot decide it; the
entity's kind can.

```
freshness_class = resolve_class(entity_kind, attribute_norm)
```

Three properties make this the right decomposition:

1. **The hard judgement moves to where it is cheap.** 1005 entities vs 2415
   facts, and 59 frozen entities account for 282 facts — 4.8x amplification.
   One entity carries 92 facts; classify it once, settle 92.
2. **Entity kind is stable.** A release is frozen forever. Classify once.
3. **The steady-state write path needs no model call.** A new fact looks up its
   entity's kind and applies a deterministic rule — no GPU, no shim, no network,
   reproducible. This matters because it runs on every dream, forever.

### Entity kinds

| Kind | Meaning | Fact default |
|---|---|---|
| `artifact` | Frozen in time — a release, commit, dated run, PR, completed programme | always `evergreen` |
| `system` | Live and mutable — daemon, server, repo, deployed model | attribute decides |
| `concept` | Abstract or definitional — a design pattern, policy, lesson | `evergreen` |

Only `system` consults the attribute signal, and only `system` can yield
`volatile`. This structurally guarantees the harmful error direction — a durable
fact silently decaying — cannot reach the 282 artifact facts at all.

The attribute signal is a deliberately conservative, auditable table:
`*-status`, `deployment-*`, `current-*`, `*-version`, `running-*`, `health*`,
`^live`, `state$` → `volatile`; everything else → `evergreen`. The entity kind
is already carrying the load, so this stays dumb on purpose.

### Why not reuse `entities.etype`

`etype` (concept/datastore/file/person/runtime/service/tool) is an *ontological*
axis used for relation type-checking, not a *temporal* one. It is also
effectively unpopulated for this purpose: only **3** cortex entities carry an
etype at all (679 of 1005 are linked to a graph node; almost none are typed),
and 53 of the 54 frozen artifacts have none. Different axis, no coverage — so
`entity_kind` gets its own storage.

## Components

**1. `entity_kinds` table (schema v24)**

```sql
CREATE TABLE IF NOT EXISTS entity_kinds (
  entity_norm TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,          -- artifact | system | concept
  origin      TEXT NOT NULL,          -- model | user | rule
  confidence  REAL,
  decided_at  DOUBLE PRECISION NOT NULL
);
```

Keyed on `entity_norm`, **not** `entity_id`: that is what cortex slots key on,
33% of cortex entities have no graph node, and a graph merge would otherwise
silently retarget the kind.

**2. `freshness.resolve_class(kind, attribute_norm) -> str`** — pure,
deterministic, no I/O. The entire policy in one testable function. Unknown kind
returns `evergreen` (the safe default already shipped in v23).

**3. `evals/classify_entity_kinds.py`** — one-time backfill. Fable 5 through the
existing `evals/sonnet_shim.py` (`--model claude-fable-5`; the shim already
passes `--model` through to `claude -p`). Writes a tagged JSON artifact and
**never** writes to the database. A separate apply step loads the artifact.

**4. Write-time hook** — in `cortex.write_fact`, when `freshness_class` is not
explicitly passed: look up the kind, call `resolve_class`. Dictionary lookup.

**5. Unknown entities need no queue.** A first sighting resolves `evergreen`
and the write proceeds — never blocked, never a model call. The classifier
re-scopes the whole bank on each run, so genuinely new entities are picked up
by the next pass for free. An explicit pending-queue table would be a second
mechanism to keep correct for no gain.

## Scoping — the dominant token lever

Do not classify entities whose kind cannot change an outcome. An entity only
matters if it carries at least one transient-looking attribute; otherwise every
one of its facts resolves `evergreen` whatever its kind. Measured on the live
bank:

| Stage | Items | Note |
|---|---|---|
| Naive: classify every fact | 2423 | |
| Only decision-relevant entities (scoped) | 265 | carry a transient attribute |
| Rule-confident artifacts | 33 | dates / `pr-N` / `commit-*` / `release` |
| Minus rule-confident → needs model | **232** | |

**10.4x reduction before a single model call.** This dominates batch-size
tuning: scoping saves 10x, batch size 20→100 saves ~4x on a base that is now
tiny. Total job cost lands around $1–3 either way.

Measured 2026-07-27 on the live bank; these counts drift as the bank grows.
Reproduce on demand with `python evals/classify_entity_kinds.py --scope-only`
(prints `facts=… scoped=… rule=… model=…`, no model call, no shim needed).

### Batch size

**Batch 50, five calls.** Larger batches degrade for four reasons —
lost-in-the-middle attention, label streaking (the model pattern-matches its own
recent outputs instead of judging fresh), correlated failure (one malformed
response loses every item in the batch), and no retry granularity. Batching also
*helps* here, because this is a comparative judgement and seeing
`0-9-0-release` beside `daemon` makes the distinction salient. 50 keeps every
item in the high-attention zone while preserving that, and five independent
calls give failure isolation.

Cost is already negligible, so the remaining budget buys reliability, not
throughput. **This is measured, not asserted**: the gold set is run at batch-40
(single) versus batch-10 (four calls) and the accuracy delta recorded in the run
artifact.

## Data flow

```
WRITE  fact(entity, attribute, value)
         │  explicit freshness_class?  ──yes──> use it (v23 behaviour)
         │  no
         ├─ entity_kinds[entity_norm] ─ hit ──> resolve_class(kind, attr)
         └─ miss ──> evergreen  +  enqueue entity for classification

READ   effective_confidence / stale  ← unchanged, shipped in v23

BACKFILL (one-time, offline)
   2423 facts → scope to 265 → 232 need judgement → batch 50 → Fable shim
        → artifact JSON
        → gold-set gate (~40 hand-labelled, weighted to the ambiguous 20%)
        → apply → entity_kinds
        → recompute freshness_class for 2423 facts via the SAME resolve_class
```

The backfill recomputes through the same `resolve_class` the write path uses —
one policy, not two implementations that drift apart.

## Error handling and reversibility

- **Every failure defaults to `evergreen`** — shim down, malformed response,
  unknown entity, unparseable batch. The failure mode is "behaves exactly as
  today", never "good data starts decaying".
- **Backfill is reversible.** Backup first, tagged run artifact,
  `UPDATE facts SET freshness_class='evergreen'` restores wholesale.
  `entity_kinds` is additive; dropping it reverts the write path to `evergreen`.
- **Nothing auto-applies.** The classifier writes an artifact; a human-gated
  apply step commits it — the same discipline as entity merges.
- **The gold-set gate is a hard gate.** If Fable misses on the ambiguous subset,
  do not apply; report it and reconsider.

## Testing

- **`resolve_class` truth table**, including the motivating pair: `artifact` +
  `schema-version` → evergreen while `system` + `schema-version` → volatile.
  This single test encodes the whole insight.
- **Write path** — explicit class beats inferred; unknown entity → evergreen and
  enqueued; kind hit → correct class. RED-checked by disabling the lookup.
- **Backfill** — artifact round-trip; malformed response → evergreen; apply is
  idempotent.
- **PG round-trip for v24** — persists and hydrates. Included from the start:
  the equivalent test caught two real bugs during v23 (`freshness_class` missing
  from `_FACT_COLS`, so it was never persisted; and an explicit NULL into a
  NOT NULL DEFAULT column).
- **Gold set committed as a fixture**, so the accuracy claim ships with its
  evidence per the benchmark rule.

## Out of scope

- **Entity aliasing / merging.** Measured yield is ~4–6 genuine clusters against
  high false-merge risk (`e2b`/`e4b`, `pr-N`). Separate decision, separate
  evidence.
- **Legacy slot-key-leakage cleanup** (~41 entities). Root cause already fixed
  2026-07-26; this is a one-time curation pass through the existing Atlas queue.
- **Steady-state extractor model choice.** The write path needs no model call
  under this design, so the question does not arise here.

## Schema v24 checklist

Per the repo convention, a bump touches four places together: `SCHEMA_META_VERSION`
in `storage/schema.py`; the docs (README capabilities table, the DSN row and
version-history table in `docs/guide/configuration.md`); the version-pin tests
(`test_schema_v13.py`, `test_schema_v16.py`, `test_schema_v22.py`,
`test_schema_v23.py`, `test_temporal_stamp.py`, plus a new `test_schema_v24.py`);
and a CHANGELOG mention of `v24`.
