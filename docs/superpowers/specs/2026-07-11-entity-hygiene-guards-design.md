# Entity hygiene guards + graph cleanup — design

**Date:** 2026-07-11
**Status:** approved (user, 2026-07-11)
**Origin:** 2026-07-11 deep-dream review-queue curation (64 findings → 8). Every
defect class below was observed in and hand-rejected from that queue; this design
turns the hand-rejections into write-time guards, then sweeps the existing corpus
with the same rules.

## Goals

1. Stop the dream write path from minting four observed junk-entity classes
   (slot-key leaks, metric readings, comma lists, resolvable compounds).
2. Stop the three merge-proposal producers from pairing entities that differ by
   model size / quant / version tokens (9 such proposals rejected by hand).
3. Stop the cross-project gate from filing information-free `related-to`
   proposals (4/4 rejected by hand).
4. Correct audit attribution for agent-made review decisions taken over REST.
5. Clean the existing graph with the same classifiers (one rule set for gate and
   sweep), including the two known name messes (`pg+extractor`, `GND`).

Non-goals: extractor/prompt changes, retrieval changes, schema migrations, any
change to `memory_graph_relate` (explicit writes stay ungated), Console UI work.

## 1. Slot-key folding (write path)

**Problem:** fact slot keys leak into the graph as entity names —
`2026-07-11-known-facts-window.delivered-components` (3 merged by hand today).

**Rule:** in `Service._resolve_or_create_entity` (service.py), on the
create-miss path only: if `norm_name(name)` exactly equals an existing fact
slot key (`entity_norm || '.' || attribute_norm` over current rows in `facts`),
resolve to the slot's *entity* (alias-aware find / create of the entity name)
instead of creating a node named after the whole key.

- Exact whole-string match only. No dot-splitting heuristics, so
  `psycopg/transaction.py`, `CortexStore.vocab_ranked`, `ops/update.ps1` are
  unaffected (they are not slot keys).
- New storage helper `find_fact_slot_by_key(norm: str) -> {entity, attribute} | None`
  (single indexed lookup; Postgres and fixture implementations).
- Applies to every caller of `_resolve_or_create_entity` (dream triples,
  `graph_propose_links`, consolidation) — the fold happens at the single choke
  point, and only when the entity does not already exist.
- Log at debug: `entity folded to slot owner (slot-key): <name> -> <entity>`.

## 2. New junk-name classes

Home: `pseudolife_memory/memory/graph_consolidation.py`, extending
`junk_name_reason` (write gate) and `junk_entities` (detection sweep), which
already share vocabulary (`concat-artifact`, `bare-number`, …).

### 2a. `metric-reading` — write gate + detection

Name is 2–3 whitespace tokens; final token is a bare decimal (`0.8`, `13.1`) or
decimal range (`0.7-0.8`); every other token is lowercase (letters, digits,
`_`/`-` allowed, no uppercase). Examples caught: `stale 0.8`, `stale 0.0`,
`stale_leak 0.7-0.8`. Examples passed: `CUDA Toolkit 13.1` (uppercase token),
`Gemma 4 E4B` (final token not a decimal), `pseudolife-daemon:0.2.0` (one
token).

Accepted trade-off: `python 3.12`-style names are blocked; version-qualified
entities are themselves an anti-pattern (version belongs in a fact at the
`python` entity). Detection side: same predicate, degree ≤ 1 like the existing
weak classes.

### 2b. `list-artifact` — write gate + detection

Splitting on commas *outside parentheses* yields ≥ 2 non-empty segments.
Catches `data/, ops/.env, *.pt`. Passes `User (HAMO9, pseudogiant92@gmail.com)`
(comma is parenthesized). Detection side: degree-agnostic like
`concat-artifact` (a list name is junk no matter how connected it got).

### 2c. `compound-artifact` — detection ONLY (never a write-time drop)

Name contains `/` or `+` such that both sides (after strip) each independently
resolve — via `norm_name` against existing entities *or aliases* — to a
different existing entity. Catches `memory_lesson_search/world_search`,
`pg+extractor` (`pg` service + `extractor` alias). Passes paths (`ops/backup.ps1`:
`ops` and `backup.ps1` both resolving is possible but requires *both* to exist;
see guard below), `C++` (empty right side).

Guards: only the FIRST separator occurrence is split (no multi-way explosion);
exempt when either side carries a dot-extension (e.g. `.ps1`, `.py`, `.yml`) so
file paths never match; write path never drops on this class — it files a
`junk_candidate` proposal for Atlas via the deep-dream detection pass only.

## 3. Variant-token merge block

**Problem:** token-overlap and cosine dedup repeatedly propose merges across
different models/quants: E4B↔E2B, 26B↔E4B, Q4_K_XL↔Q4_K_M (9 rejected today).

**Helper:** `variant_tokens(name) -> frozenset[str]` in
`graph_consolidation.py`, extracting (case-insensitive, word-bounded):

- size tokens: `E\d+B`, `\d+(\.\d+)?[MBK]?B` (`26B`, `4B`, `270M`)
- quant tokens: `Q\d(_K)?(_(XS|S|M|L|XL))?`, `q\d_\d`, `UD-Q\w+`
- dotted versions: `\d+\.\d+(\.\d+)*` when attached to a name token
  (`:0.2.0`, `3.6`)

`QAT` is deliberately NOT a variant token — it appeared on both sides of a
legitimate accepted merge (mp#102) and would cause false blocks.

**Rule:** `variant_conflict(a, b)` = both names yield non-empty variant-token
sets and the sets differ. On conflict, the pair is hard-blocked from becoming a
merge proposal in all three producers:

1. `near_duplicate_names` (graph_review.py) — used by write-dedup
   (`_propose_write_dedup`).
2. `_propose_dream_alias_candidates` (service.py) — cosine alias post-pass.
3. `partition_candidates` / `_name_contains` (graph_consolidation.py) — deep
   dream; a variant-conflicted pair falls through to the LINK candidate list
   (it may still be related — e.g. quant-of — just never a merge).

Block merges only; never blocks link proposals or explicit human merges via
REST/Console.

## 4. Cross-project `related-to` bar

At the dream triple write path's cross-project gate
(service.py ≈1667): only file the edge proposal when the resolved relation is
**typed** (not the `related-to` fallback). If `resolve_relation` fell back to
`related-to` AND the scopes are disjoint, drop with a debug log
(`dream relation dropped (cross-project-untyped)`). Rationale: a vague relation
across disjoint projects carries no information — 4/4 such proposals were
rejected in today's curation. Same-scope behavior unchanged.

## 5. `decided_by` passthrough (REST)

routes.py currently binds review verdict endpoints without `decided_by`, so the
service defaults (`"human"`) mislabel agent decisions made over REST (today's
15 merge accepts + 35 rejects are logged `decided_by=human`).

Change: `/api/graph/accept-entity-merge` and
`/api/graph/reject-entity-proposal` pass `b.get("decided_by", "human")` to the
service (verified: both service methods take `decided_by`;
`graph_accept_entity_junk` (service.py:4270) does not — leave that route
unchanged). Console JS sends nothing → unchanged. Agent callers send
`{"decided_by": "agent"}`. Validate to the literal set `{"human", "agent"}`
(fall back to `"human"` on anything else).

## 6. Curation stage (live bank; after code deploy)

Runbook executed by the agent against the live daemon. Safety first: fresh
Postgres backup via `ops/backup.ps1`, plus the graph-table snapshot produced by
the `memory_dream(action="deep", apply=true)` run in step 4. Order:

1. **`pg+extractor` retirement:** re-point real edges — `pseudolife-daemon
   uses/depends-on pg+extractor` becomes edges to `pg service` and
   `pseudolife-extractor`; drop `Cortex Console v2 Phase 0 uses pg+extractor`;
   delete the entity (removes its generic `extractor` alias with it).
2. **GND split:** the current `GND` node's provenance is entirely GND Share →
   rename display to `GND Share` (keep id, no data loss). Enshrouded-server
   facts, if any exist on the node, move to a fresh `GND (Enshrouded server)`
   entity first (check `memory_graph`/facts before renaming).
3. **Scope pass:** for each of the ~299 unattributed entities, infer project
   from `entity_sources` majority; assign via `/api/graph/assign-scope`. No
   clear majority → leave unattributed.
4. **Periphery sweep:** with the new detectors deployed, run a deep-dream
   dry-run preview, then `apply=true` (this produces the graph snapshot and
   files the proposals); batch-judge resulting junk/merge proposals in the same style as
   the 2026-07-11 curation — auto-act on mechanical classes (accept junk for
   `list-artifact`/`metric-reading`/`concat-artifact`, dismiss variant pairs),
   leave judgment calls pending in Atlas.

All REST verdicts in this stage send `decided_by=agent` (requires §5 deployed).

## 7. Testing & rollout

- TDD per guard: new cases in `tests/test_graph_write_gating.py` (slot-key
  fold, metric-reading, list-artifact, cross-project bar),
  `tests/test_graph_review.py` (variant block in `near_duplicate_names`,
  compound-artifact detection), `tests/test_deep_dream.py` (partition block),
  plus a routes test for `decided_by` passthrough. Include the survivor cases
  from §2 (CUDA Toolkit 13.1, User (…) parenthesized comma, paths) as
  regression guards.
- Full suite green locally; backup-first deploy via `ops/update.ps1` with a
  rollback tag.
- **Ladder re-run:** REQUIRED by standing rule (dream-write-path change) but
  **deferred — GPU in use; runs only on the user's explicit go.** Tracked as a
  blocked final task; the deploy may precede the ladder since all changes are
  name-shape gates, not extractor behavior, and the rollback tag covers a
  regression.

## Risks

- Slot-key fold false positives: only if a *legitimate* entity name equals an
  existing slot key verbatim — accepted; the fold targets exactly that shape.
- `metric-reading` blocks lowercase `tool 3.12`-style names — accepted
  (anti-pattern; version-in-name).
- Compound detection resolving path segments: mitigated by file-extension
  exemption and detection-only handling (Atlas reviews, nothing silently
  dropped).
- Ladder regression window between deploy and the gated re-run: mitigated by
  rollback tag + the changes being deterministic name gates.
