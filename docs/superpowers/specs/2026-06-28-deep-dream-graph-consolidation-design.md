# Deep Dream — Graph Consolidation (Phase 2 / "C") — Design

**Status:** design · **Date:** 2026-06-28 · **Author:** Claude (Opus 4.8) + user

## 1. Problem & motivation

The incremental dream (`service.dream_run`) is **window-local**: each pass pulls only
unconsolidated entries past a cursor and extracts facts/relations/lessons from *that one
batch*. Two consequences:

1. **No cross-session linking.** Two entities discussed in different sessions are never seen
   together by the extractor, so a genuine relationship between them can never be discovered.
   The knowledge graph only ever links things that co-occurred in a single dream window.
2. **No retroactive self-clean.** The graph accumulates the extractor's defects (type-violating
   edges, near-duplicate entities, the vague `related-to` catch-all). Phase-2 "A" gave us a real
   per-edge confidence (`edge_confidence`) and a one-off backfill, but there is no *periodic*
   pass that re-scores, prunes the clear junk, and keeps the graph honest as the lexicon evolves.

Phase-2 "A" (the relation-confidence repair, shipped + deployed + backfilled 2026-06-28) is the
foundation this builds on: it gives a real confidence signal to gate both halves on. "C" is the
**deep dream** — a manual, full-corpus consolidation pass over the knowledge graph that (a)
self-cleans and (b) discovers cross-session links, both gated on `edge_confidence`.

This spec is **graph-focused by deliberate scope** (§2). The entry point and module names are
chosen so a future **cortex reconciliation** pass slots in as a sibling without re-architecting;
MIRAS is explicitly out (§2, §9).

## 2. Goal, scope & success criteria

**Goal:** a manual, session-driven deep dream that consolidates the knowledge graph in two
halves — deterministic **self-clean** and semantic **cross-session link discovery** — without
ever auto-polluting the live retrieval path.

**Success criteria:**
1. Re-running the pass re-scores all live agent edges via `edge_confidence` (idempotent) and
   auto-applies only a narrow, provably-safe cleanup class, leaving the soft class for per-item
   review.
2. The pass surfaces unlinked entity pairs that are semantically related across *different*
   sessions — links the window-local incremental dream structurally cannot find.
3. Discovered links never enter `edges` (hence never enter recall/traversal) until a human
   confirms them in the Atlas Review queue.
4. The whole pass is dry-run-first; `apply` is backup-gated; all destructive actions are
   supersede-not-delete (reversible).

**In scope:** the knowledge graph (entities + edges).

**Out of scope (non-goals):**
- **Cortex reconciliation** (contested-fact resolution, stale-slot pruning) — a natural *next*
  sibling pass; named for, but not built here.
- **MIRAS band hygiene.** The bands are the raw associative stream, deliberately lossy-by-decay,
  already self-deduped at write time by the surprise gate and aged out by retention. Retroactive
  band merging/pruning would fight that model and break engram provenance (the provenance-as-link
  cross-index keys on entry ids). If MIRAS ever needs tending, that is a retention/decay-tuning
  lever, not a consolidation pass. **Explicitly excluded, not merely deferred.**
- No new scheduler (manual trigger only), no GPU dependency, no change to the incremental dream.

## 3. Approach (chosen: manual, session-driven; tiered-autonomy self-clean; proposal-only discovery)

Decisions locked during brainstorming:

- **Execution mode — manual, session-driven.** The operator triggers the pass. The daemon does
  the deterministic work (self-clean + candidate generation); the one model-dependent step
  (proposing a relation for a candidate pair) runs via in-session subagents (frontier Opus, the
  user's included usage), reusing the bench's `--emit-prompts → subagents → results` pattern.
  Chosen over a headless scheduled job (locked to the weak CPU sidecar, no human present) and over
  a served-GPU pass (operational weight: homelab GPU + daemon→GPU reachability). Manual fits the
  "occasional" cadence, needs no infra, and keeps the operator present to review what it queues.
- **Self-clean autonomy — tiered.** Auto-apply only a narrow provably-safe class (hard
  type-violations; exact-duplicate entities); queue everything softer for per-item review. Chosen
  over propose-only (too timid for the obvious junk) and full autonomy (crosses into unsupervised
  deletion against the per-item-review discipline + the bulk-delete lesson).
- **Discovery signal — semantic context-similarity.** An entity's "meaning" is the centroid of
  the entries that mention it; candidate links are unlinked pairs whose contexts are similar.
  Chosen over provenance co-occurrence (only finds *within-session* gaps — misses the
  cross-session links that are the entire point) and over pure graph-structure (flags
  under-connected regions, not *which two entities* to link). Provenance/scope is retained as a
  *precision filter* on the semantic candidates (§5.B), not as the generator.
- **Discovery write policy — proposal-only, always.** Discovered links are inherently softer than
  cleanup and are the easiest way to re-pollute the graph, so they are *never* auto-written
  regardless of the self-clean autonomy tier. They land in a separate `edge_proposals` table and
  reach `edges` only on human confirm.

## 4. Architecture & module structure

```
memory_deep_dream  (manual trigger: MCP tool / ops script / Atlas button)
  │
  ├─ Step A · SELF-CLEAN   (daemon, deterministic, dry-run/apply)
  │     re-score every live agent edge via edge_confidence (non-destructive)
  │     AUTO-APPLY safe class: supersede hard type-violations; merge exact-dup entities
  │     soft class stays in the live Atlas Review queue (graph_review) — unchanged
  │     → applied/queued audit summary
  │
  ├─ Step B · CANDIDATE GEN  (daemon, deterministic)
  │     entity vector = centroid of its mentioning entries' band embeddings
  │     near-pair cosine search → filters (existing-edge / exact-dup / scope)
  │     → candidates payload (pairs + context snippets + relation registry)
  │
  └─ Step C · LINK PROPOSAL  (session/subagents, frontier Opus)
        propose/reject a typed relation per pair → gate (resolve_relation + edge_confidence)
        → graph_propose_links(survivors) → edge_proposals table (NOT edges)
        → Atlas Review `proposed_link` → accept-proposal / reject-proposal
```

**New code:**
- `pseudolife_memory/memory/graph_consolidation.py` — pure, DB-free, unit-testable (same shape as
  `graph_insight.py` / `graph_review.py`). Houses the self-clean classifier (which edges/entities
  are auto-safe vs queued) and the candidate scorer (centroid build + near-pair search + filters).
  The service supplies edges/entities/embeddings/scope-map; the module returns decisions.
- `edge_proposals` table (additive, schema bump) — discovered links, isolated from `edges` (§5.C).
- Orchestration entry point beside `dream_run` in `service.py` + an MCP tool + `ops/deep_dream.py`.
- Two confirm-gated mutations (`accept-proposal` / `reject-proposal`) + a `proposed_link`
  `graph_review` finding.

**Rejected structural alternative:** folding this into `dream_run` as a `deep=True` flag. The
incremental dream is headless / idle-triggered / window-local; the deep dream is manual /
full-corpus / session-orchestrated, with a different model and safety profile. A separate entry
point keeps both clean.

## 5. Components in detail

### A. Self-clean (deterministic, daemon-side)

Three actions, dry-run-first → apply (the backfill discipline, now a logged lesson):

1. **Re-score** every live agent edge (`origin='agent' AND superseded_at IS NULL`) through
   `edge_confidence`. Non-destructive `UPDATE`; runs every pass so the live `dubious_edges` view
   stays honest as the lexicon evolves. (Identical recompute to `ops/backfill_edge_confidence.py`.)
2. **Auto-apply the provably-safe class** (the tiered-autonomy buckets):
   - **Hard type-violations** — edges where both endpoints are *confidently* typed via
     `infer_type` *and* the pair violates `TYPE_CONSTRAINTS` (the exact structural condition under
     which `edge_confidence` applies its `0.25` penalty → `0.175`). Detected by a small structural
     predicate, **not** a float compare against `0.175` (fragile) or `min_relation_confidence`
     (default `0.0`, which would make this bucket a no-op). The `User runs-on Windows 11` shape
     hand-pruned in Phase 1. A reusable `is_hard_type_violation(src, relation, dst) -> bool` lands
     in `relation_quality.py` so the self-clean and `edge_confidence` share one definition.
   - **Exact-duplicate entities** — pairs with token-set-identical displays (`graph_review`
     `duplicate_candidates` Jaccard `== 1.0`) resolving to distinct ids. Unambiguously one entity.
   - Both are **supersede, not delete**: set `superseded_at` + a reason string. Reversible,
     audited, matches the park/supersede philosophy. (Entity merge reuses the existing Atlas merge
     storage op, which re-points edges then supersedes the loser.)
3. **Leave the soft class in the existing live queue.** Related-to prunes (0.45), fuzzy
   duplicates (Jaccard 0.6–0.99), orphans, unattributed, test-artifacts already surface live in
   the Atlas Review queue via `graph_review`. The deep dream adds no separate queue for these;
   re-scoring just sharpens that view. They remain per-item, human-confirmed.

`auto_apply_safe` (config, default `True`) gates bucket (2); it only acts under `apply`, never
under `dry_run`.

### B. Candidate generation (deterministic, daemon-side)

1. **Per-entity context vector** — average the embeddings of the entries that mention the entity:
   - **Primary:** its trace entries (`traces_for_slot` across all the entity's attributes) — the
     exact entries that grounded its cortex facts.
   - **Fallback** for graph-only entities (relation endpoints with no cortex slot → no traces): a
     token-mention scan of entry texts for the entity's display/aliases, centroid of those.
   - Entities with neither traces nor textual mentions are **skipped** — uncharacterizable, so we
     don't guess.
2. **Near-pair search** — cosine over entity vectors; keep pairs above `min_similarity` (default
   `0.55`), top `top_k_candidates` (default `50`). Both deliberately conservative — each survivor
   costs a Step-C model call and precision protects the graph.
3. **Filters** — drop a candidate pair if: an edge already exists in either direction (we want
   *missing* links only); it is an exact-duplicate pair (that's a Step-A merge); or the two
   entities are in **different project scopes** (`entity_sources_map`; cross-project links like
   `gw2-reshade ↔ pseudolife` are the noise Atlas separates — same-scope or both-unattributed
   only).
4. **Output** — per surviving pair: the two displays + up to `max_context_snippets` highest-signal
   context snippets + the relation registry, in the bench's `--emit-prompts` shape, written to a
   candidates payload (`evals/results/deep-dream-candidates.json` or equivalent). The daemon does
   **not** call a model.

### C. Link proposal + proposal storage

**Proposal (session/subagents).** The session reads the candidates payload and dispatches
subagents (Opus, batched — several pairs per subagent). Each subagent proposes one closed-vocab
relation per pair or rejects it. Every proposal is run through the **same deterministic gate
production uses** — `resolve_relation` → closed vocab, `edge_confidence(src, relation, dst)`,
dropped if below floor or a hard type-violation. The model proposes; the gate disposes (this is
what absorbs Opus's known over-extraction bias).

**Storage — `edge_proposals` (additive, schema bump).** Survivors are written here, **never to
`edges`**:

```
edge_proposals(
  id            -- pk
  src_id, relation, dst_id
  confidence    -- from edge_confidence
  similarity    -- from Step B (why it was a candidate)
  rationale     -- model's one-line justification
  source        -- 'deep-dream'
  created_at
  status        -- 'pending' | 'accepted' | 'rejected'
  UNIQUE(src_id, relation, dst_id)
)
```

Because proposals live outside `edges`, `memory_graph` / recall / traversal never see them — zero
retrieval pollution until confirmed. (Writing them as low-confidence edges instead would dump them
straight into the traversal path, re-creating the mess we just cleaned.)

**Review surface (reuses Atlas).** `graph_review` gains one finding type, `proposed_link`, reading
`edge_proposals(status='pending')`; it appears in the existing Atlas Review queue beside
duplicates/orphans/dubious-edges, each row showing relation, confidence, similarity, rationale.
Two confirm-gated mutations mirror the existing `assign-scope`/`merge`/`unrelate`/`delete-entity`
set:
- **accept-proposal** → `upsert_edge(origin='agent', confidence=proposal.confidence)`, mark row
  `accepted`.
- **reject-proposal** → mark row `rejected`.

No new write primitive beyond the table + these two mutations; promotion to a real edge is a
deliberate per-item human act.

## 6. Data flow & entry points

```
1. memory_deep_dream(dry_run=True)         [daemon]
     A · self-clean PREVIEW → would-supersede / would-merge sets + new conf distribution
     B · candidate gen      → top-K scope-coherent near-pairs + context snippets
     → audit + candidates payload. No writes.

2. operator eyeballs the preview, then:
   memory_deep_dream(apply=True)            [daemon]
     ops/backup.ps1 → A auto-applies safe class (supersede/merge) + re-scores
     → refreshed candidates payload

3. session dispatches subagents over the payload (Step C, Opus)   [session]
     propose/reject → gate → survivors → graph_propose_links(...)  [daemon] → edge_proposals

4. Atlas Review → `proposed_link` rows → accept-proposal / reject-proposal   [operator]
```

A+B are deterministic and daemon-side with dry-run/apply safety; C is session-orchestrated (no
scheduler, frontier quality on included usage); acceptance is the existing Atlas confirm flow.

**Config** — new `memory.deep_dream` namespace: `min_similarity` (0.55), `top_k_candidates` (50),
`max_context_snippets` (3), `auto_apply_safe` (True; acts only under apply). Reuses
`min_relation_confidence` + `TYPE_CONSTRAINTS`. All knobs, no magic numbers.

## 7. Safety, rollback, idempotency

- **Backup-first on apply** (`ops/backup.ps1`), per the bank-wipe lesson. **Supersede-not-delete**
  throughout (reversible, audited).
- **No separate DB connection.** Unlike the one-off backfill script (plain `psycopg` +
  lock-timeouts *because* it ran outside the daemon), the deep dream is a service method running
  *inside* the daemon under its existing lock. It mirrors `_dream_extract_relations`: heavy
  model-free compute (centroids, cosine) outside the lock, DB writes inside it.
- **Proposals isolated from `edges`** → no retrieval impact before confirm.
- **Idempotent:** re-score is pure; re-merging merged entities and re-superseding superseded edges
  are no-ops; candidate gen is deterministic; `edge_proposals` `UNIQUE(src_id, relation, dst_id)`
  + skip-if-already-pending prevents re-run pileup.
- **Failure isolation:** B/C failures never roll back A's committed self-clean; Step C is
  best-effort (like the existing relation extraction).
- **Cost:** candidate gen is `O(entities²)` cosine — fine at the current few-hundred-entity scale.
  If the bank ever grows large, swap in ANN (YAGNI now, noted).

## 8. Testing

- **`graph_consolidation` pure unit tests (the bulk):** self-clean classifier (auto-safe vs
  queued) over fixture edges — assert a `0.175` both-typed violation is auto-safe, a `0.45`
  related-to is queued, an unknown-typed edge is neither; centroid builder + near-pair search +
  the three filters (existing-edge, exact-dup, scope) over fixture entities/embeddings/edges;
  deterministic ordering.
- **`edge_proposals` storage** — PG-backed suite (skips without Postgres): insert → accept
  (promotes to a real `edges` row, marks `accepted`) → reject; UNIQUE/idempotency.
- **`graph_review` `proposed_link` finding + the two mutations** — service test + fixture stubs +
  web-route dispatch tests (the Atlas Stage-3 pattern).
- **Orchestration entry point** — `dry_run` asserts *no* edge/proposal mutation; `apply` asserts
  the safe class committed and the soft class untouched.
- **Gate reuse** — feed Step C's gate a known type-violation, assert rejection (reuses the
  relation bench's labeled cases).

## 9. Risks & caveats

- **Discovery precision is the main risk.** Mitigations stack: conservative `min_similarity` +
  `top_k`, the deterministic `edge_confidence` gate on every proposal, scope filtering, and
  proposal-only storage (human confirm). Over-proposing wastes review time but cannot pollute the
  graph.
- **Context-vector quality varies.** Trace-grounded entities get strong vectors; fallback
  mention-scan entities get weaker ones; uncharacterizable entities are skipped (a recall gap we
  accept over guessing). Tunable later by improving the fallback.
- **Self-clean autonomy.** The auto-applied class is deliberately narrow (hard type-violations +
  exact dups). Anything ambiguous stays in the human queue. Dry-run-first + backup + supersede make
  even a mistaken auto-apply reversible.
- **`O(entities²)` candidate gen** is acceptable now, a known future scaling item.

## 10. Relationship to prior phases & the future cortex sibling

- **Builds on Phase-2 "A":** `edge_confidence` + `TYPE_CONSTRAINTS` are the gate both halves reuse;
  the backfill recompute becomes Step A's re-score.
- **Reuses Atlas Stage 3:** the review queue, the confirm-gated mutation pattern, `graph_review`,
  `graphview`.
- **Reuses the relation bench:** the `--emit-prompts → subagents → results` flow for Step C; the
  labeled cases for the gate test.
- **Future cortex reconciliation** slots in as a sibling pass under the same `memory_deep_dream`
  entry point (contested-fact resolution + stale-slot pruning), reusing this pass's dry-run/apply +
  review-queue scaffolding. Named for here; not built here.
