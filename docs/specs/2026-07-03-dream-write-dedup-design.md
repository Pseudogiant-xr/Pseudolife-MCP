# Dream near-duplicate correction — design

Date: 2026-07-03
Status: approved (user, 2026-07-03)
Origin: extractor-side dedup follow-up to the external-findings program; the
bank's dominant graph-quality problem is extractor over-extraction of
naming-variant entities (2026-07-02 cleanup: ~149 merges, ~612 junk deletions).

## Goal

Two-tier near-duplicate handling, replacing "accumulate until quarterly
cleanup":

1. **Tier 1 — detect at birth (sidecar, 2B):** when the dream commit path
   mints a NEW entity whose name is near-duplicate to an existing one, file
   an `entity_proposals` merge row immediately. The sidecar never merges.
2. **Tier 2 — correct in `/dream deep` (capable model):** the Step-C driver
   triages pending merge proposals with snippet evidence and may APPLY
   confident merges via the existing `accept_merge`, dismiss confirmed-distinct
   pairs, and leave uncertain ones for Atlas.

Human review is post-hoc, not gating: pre-apply snapshot (exists), a decision
audit trail (new), per-item reversibility via carried aliases (exists), and
the normal Atlas queue for everything uncertain.

## Tier 1 — write-time detector

- New pure function in `pseudolife_memory/memory/graph_review.py`:
  `near_duplicate_names(name, existing, *, min_jaccard=0.6, dismissed)` →
  scored matches of a candidate name against existing canonicals AND aliases,
  reusing `_token_set` + Jaccard from `duplicate_candidates`. `dismissed`
  (sorted canonical-name pairs) suppresses human-settled distinct pairs.
- `MemoryService._resolve_or_create_entity` returns whether it CREATED the
  node. Only dream-side mint sites react: `_link_dream_relations` (extracted
  relations) and the dream-claim auto-promote subject path. Explicit
  `graph_relate` / `memory_fact_set` writes are untouched.
- On a created node with matches: insert `entity_proposals` rows
  (`kind="merge"`, `score`=jaccard, `reason`="write-dedup: <detail>") via the
  existing `insert_entity_proposal`. The partial unique index
  `entity_proposals_merge_uq` already dedupes re-proposals (any status), and
  a previously rejected pair therefore never re-files.
- Advisory only: any detector failure logs and continues; a dream write is
  never blocked.
- Config: `memory.dream.write_dedup_min_jaccard` (default 0.6; `0` disables
  the detector).

## Tier 2 — capable-model triage in `/dream deep`

- The deep-dream Step-C payload (`memory_dream(action="deep")` response)
  gains `merge_proposals`: pending merge rows enriched with, per side:
  display/canonical, etype, project scopes, edge count, and up to k snippets
  (same snippet machinery as link candidates, incl. the mention-scan
  fallback), plus the name-similarity score and reason.
- Driver update (`examples/commands/dream.md` `/dream deep` + the runbook):
  per pair the model must choose (a) `memory_graph_review(action="accept_merge",
  id=…)` when the evidence shows the same referent (naming-layer variants),
  (b) `memory_graph_review(action="dismiss_pair", src=…, dst=…)` +
  `reject_entity` on the proposal when clearly distinct, or (c) leave pending
  for Atlas. Judgment criteria live in the driver prompt; the code adds no
  auto-apply heuristics.
- Merge direction: fold the LOWER-degree entity into the higher-degree one
  (proposal rows already carry `entity_id` → `into_id`; the enrichment
  orients them so `into` is the higher-degree side).

## Audit trail

- Additive columns on `entity_proposals`: `decided_by TEXT`,
  `decided_at DOUBLE PRECISION` (nullable; schema version bump, additive
  only — no data migration). `set_entity_proposal_status` gains the two
  fields; MCP-driven decisions stamp `decided_by="agent"` (writer/session id
  appended when available), Console-driven ones `"human"`.
- `/api/graph/review` response gains `recent_merges`: the newest N (default
  20) decided merge proposals (accepted or rejected), each with names,
  decision, decider, and timestamp. Atlas Review renders it as a read-only
  "recent merge decisions" list under the queue — the human's after-the-fact
  window onto what the model applied.
- Wholesale undo remains the deep-dream pre-apply snapshot; per-item undo
  uses the carried alias (`graph_merge` already re-points aliases to the
  survivor) plus the audit row identifying what was folded.

## Out of scope

- No auto-fold anywhere in code — application is always the capable model
  (via MCP) or the human (via Atlas).
- No extractor-prompt vocabulary steering (revisit only with evidence).
- No daemon deploy in this program (separate gated rebuild).

## Testing

- Unit: `near_duplicate_names` (variant catch incl. token-identical
  `graph_review`/`graph_review.py`, dismissed suppression, threshold, alias
  matching).
- PG service: dream relation minting a near-dup files exactly one proposal;
  re-runs and rejected pairs don't re-file; exact/alias resolution files
  nothing; detector disabled at `0`.
- PG service: deep response carries enriched `merge_proposals`; accept_merge
  stamps `decided_by`/`decided_at`; `recent_merges` surfaces it.
- Tool-description budget stays ≤1,600/tool, ≤18k total.

## Rollout

One branch (`feat/dream-write-dedup`), TDD per component, whole-branch
review, merge to master, push; deploy is a separate user-gated rebuild.
