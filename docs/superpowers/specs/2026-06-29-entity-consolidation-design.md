# Entity Consolidation (Entity-Quality SP-1) — Design

**Status:** design · **Date:** 2026-06-29 · **Author:** Claude (Opus 4.8) + user

## 0. Program context & decomposition

The first full deep-dream run (2026-06-29) revealed that this bank's dominant graph-quality
problem is **entity quality**, not missing links: discovery surfaced mostly (a) over-granular
**synonym** entities (`daemon`/`live daemon`/`daemon-only`, `4090/Qwen3.6-27B` variants,
`Console`/`Cortex Console`) and (b) **junk** over-extraction artifacts (`LIVE`, `2`, `merged`). The
existing token-Jaccard `duplicate_candidates` detector is a *poor synonym finder* — `daemon` vs
`live daemon` share only one token (Jaccard 0.5, below the 0.6 cutoff, missed) — whereas the deep
dream's **semantic cosine** caught them at 0.98.

The user chose to address entity quality as a decomposed **two-sub-project program**:

- **SP-1 — Downstream consolidation (this spec):** clean the *existing* graph. Surface
  near-duplicate **merges** (semantic signal) and **junk** prunes (structural) into the Atlas
  review queue, review-gated, reusing the deep-dream / `graph_review` / Atlas-mutation machinery.
  *Built first.*
- **SP-2 — Upstream prevention (separate follow-up spec):** stop the Gemma extractor minting
  near-dups + junk in the first place (prompt tightening / a post-extraction entity-name filter,
  measured by extending the relation/entity eval with entity-quality metrics). Connects to the
  bespoke-model roadmap thread. *Built second; out of scope here.*

## 1. Goal & success criteria

A review-gated entity-consolidation pass that surfaces the two entity-cleanup classes the deep
dream revealed, so the operator can clean the live graph per-item from Atlas:

1. **Near-duplicate merges** — synonym entity pairs (semantic cosine + name-containment) become
   `merge_candidate` proposals → Atlas → `graph_merge` on accept.
2. **Junk prunes** — over-extraction artifacts (bare numbers / very short / status-words, weakly
   connected) become `junk_candidate` proposals → Atlas → `graph_delete_entity` on accept.
3. **Nothing auto-applies.** Both classes are proposals persisted in a new `entity_proposals`
   table; they never touch `entities` until a human accepts in Atlas. (This respects the
   "semantic similarity over-groups distinct facts → DO NOT APPLY" precedent from the hermes
   `cortex_dedup` dry-run.)

### Non-goals
- No upstream extractor change (that is SP-2).
- No auto-merge from the semantic signal — only the *exact-duplicate* (full-token-set identical)
  class keeps auto-merging in the existing self-clean; semantic near-dups are proposal-only.
- No removal of the existing token-Jaccard `duplicate_candidates` finding — it catches lexical /
  quote dups the semantic signal may not; the new `merge_candidate` is complementary.

## 2. Approach (chosen: extend the deep dream + an entity_proposals table)

The deep dream already computes the semantic signal (`entity_context_vectors`) and the near-pairs
(`candidate_pairs`). SP-1 routes those pairs by intent and adds the junk analyzer, flowing into the
**same Atlas-review + confirm-gated-mutation model as `edge_proposals`**:

```
memory_deep_dream  (Step B extended; dry-run previews / apply persists)
  near-pairs (entity_context_vectors → candidate_pairs)  ─ partition by NAME-CONTAINMENT:
     ├─ MERGE candidate (name-containment + sim >= merge_min_similarity) → entity_proposals(kind='merge')
     └─ LINK candidate  (distinct names)                                → Step-C link flow (unchanged)
  junk analyzer (structural: bare-number / too-short / status-word + low degree)
                                                                         → entity_proposals(kind='junk')

entity_proposals (NEW additive table, schema v18) — never touches entities until accept

graph_review  →  two NEW findings read from entity_proposals(pending): merge_candidate, junk_candidate
                 (existing token-Jaccard duplicate_candidates kept — complementary)

Atlas confirm-gated mutations (reuse primitives):
   accept-merge → graph_merge(from, into) + mark accepted
   accept-junk  → graph_delete_entity      + mark accepted
   reject-entity-proposal → mark rejected
```

The merge tier slots cleanly into the deep dream's tiered-autonomy philosophy: **exact-duplicate
auto-merges** (existing), **semantic near-dup proposes** (new), **junk proposes** (new). Merges and
junk are deterministic, so the **deep-dream apply persists them directly** (the dry-run previews
counts/samples); only link candidates need the Step-C model.

**Alternative rejected:** a lighter session-driven path (classify in the deep-dream output, act via
`graph_merge` in-session like Step C; junk as a transient live `graph_review` analyzer). Rejected
because the user wants these in the **persistent, re-openable Atlas review queue** for recurring
cleanup, not a transient per-session triage.

## 3. The two analyzers (pure, in `graph_consolidation.py`)

### 3a. Merge partition
`partition_candidates(pairs, entities, edges, *, merge_min_similarity) -> (merges, links)`. Takes
the near-pairs `candidate_pairs` already returns (each `{src_id, dst_id, similarity, ...}`) and
splits them:

A pair is a **merge candidate** iff `similarity >= merge_min_similarity` **AND** the names assert
identity:
- **token-subset:** `_full_token_set(a) <= _full_token_set(b)` or vice-versa
  (`daemon{daemon} <= live daemon{live,daemon}`; `Console <= Cortex Console`;
  `4090 27B {4090,27b} <= 4090/Qwen3.6-27B {4090,qwen3,6,27b}`), or
- **norm-substring:** `norm_name(a)` is a substring of `norm_name(b)` or vice-versa.

Otherwise → **link candidate** (returned for the existing Step-C path). Deliberately
precision-biased toward "link": an ambiguous shared-head pair (`BESPOKE extraction model` /
`bespoke model (OPSD)` — neither a subset) falls to *link*, a safe miss (still hand-mergeable, never
a false auto-merge). `merge_min_similarity` (default `0.90`) guards against coincidental
name-containment of unrelated things (`test` ⊆ `test harness` at low cosine stays a non-merge).
Fold direction = lower-degree into higher-degree (same rule as `exact_duplicate_pairs`); tie-break
folds higher id into lower id. Returns merge tuples carrying `(from_id, into_id, similarity, reason)`
and the remaining link pairs unchanged.

### 3b. Junk detector
`junk_entities(entities, edges, *, max_degree=1) -> [{"entity_id": int, "reason": str}]`. Flags an
entity only when its display is unambiguously a non-entity **and** it is weakly connected
(`degree <= max_degree`, default 1):
- `reason="bare-number"` — display matches `^\d+$` (`2`)
- `reason="too-short"` — stripped display length ≤ 2
- `reason="status-word"` — `display.strip().lower()` in `_JUNK_STOPWORDS` (a curated constant:
  `live, merged, done, fixed, current, ok, pending, wip, todo, n/a, none, null`)

The degree guard is the conservative safety: a well-connected node with a short name is left alone.
Both analyzers are proposal-only — they never delete or merge; they populate `entity_proposals`.

## 4. Storage — `entity_proposals` (additive, schema v18)

```sql
CREATE TABLE IF NOT EXISTS entity_proposals (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,                                                    -- 'merge' | 'junk'
  entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,  -- merge: 'from' (deleted); junk: the entity
  into_id   BIGINT REFERENCES entities(id) ON DELETE CASCADE,           -- merge: 'into' (kept); junk: NULL
  score REAL, reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',                               -- pending | accepted | rejected
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_merge_uq ON entity_proposals
  (LEAST(entity_id, into_id), GREATEST(entity_id, into_id)) WHERE kind='merge';
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_junk_uq  ON entity_proposals
  (entity_id) WHERE kind='junk';
```

`entity_id` is the actionable target deleted on accept for *both* kinds (symmetry). Partial unique
indexes dedupe per-kind and sidestep the NULL-`into_id` uniqueness gotcha. `SCHEMA_META_VERSION`
17→18; the three hard-coded `== 17` test assertions (`test_schema_v13`, `test_schema_v16` — function
renamed again, `test_temporal_stamp`) bump to `== 18`.

Storage methods on `PostgresStorage` (mirroring the `edge_proposals` set):
- `insert_entity_proposal(kind, entity_id, into_id, score, reason, now) -> int | None`
  (`ON CONFLICT DO NOTHING` against the partial indexes → `None` on duplicate)
- `pending_entity_proposals() -> list[dict]` (status='pending', joined to entity displays)
- `get_entity_proposal(id) -> dict | None`
- `set_entity_proposal_status(id, status) -> bool`

## 5. Service, deep-dream wiring, graph_review, mutations

- **`service.deep_dream`:** after `candidate_pairs`, call `partition_candidates`; the returned
  *link* pairs become the `candidates` payload (Step C, unchanged); on **apply**, persist the
  *merge* tuples via `insert_entity_proposal(kind='merge', ...)` and the `junk_entities(...)` results
  via `insert_entity_proposal(kind='junk', ...)`. Dry-run returns `would_merge_propose` /
  `would_junk` previews (counts + samples) and writes nothing. **Persisting proposals is NOT gated
  by `auto_apply_safe`** — that flag governs only the *destructive* auto-supersede / auto-merge of
  the self-clean; populating the (non-destructive) review queue happens on any `apply`. Reuses the
  existing in-daemon lock discipline.
- **`graph_review`:** add `merge_candidate` + `junk_candidate` findings reading
  `pending_entity_proposals()` (fetched by the service, passed in like `proposals`); existing
  findings unchanged.
- **Service mutations:** `graph_accept_entity_merge(id)` → read row, `graph_merge(from→into)`,
  mark accepted; `graph_accept_entity_junk(id)` → read row, `graph_delete_entity`, mark accepted;
  `graph_reject_entity_proposal(id)` → mark rejected. Reuse existing `graph_merge` /
  `graph_delete_entity`.
- **MCP tools + web routes + fixtures:** three tools (`memory_graph_accept_entity_merge`,
  `…_accept_entity_junk`, `…_reject_entity_proposal`) added to the registry test; two/three web
  routes (`/api/graph/accept-entity-merge`, `…/accept-entity-junk`, `…/reject-entity-proposal`);
  fixture stubs + the two new findings in `FixtureService.graph_review`.
- **Config:** `DeepDreamConfig` gains `merge_min_similarity: float = 0.90` and
  `junk_max_degree: int = 1`. `_JUNK_STOPWORDS` is a constant in `graph_consolidation.py`.

## 6. Testing

- **Pure — `partition_candidates`:** `daemon`/`live daemon`, `Console`/`Cortex Console`,
  `4090 27B`/`4090/Qwen3.6-27B` → merge; `Track A (…)`/`Track B (…)`, `BESPOKE extraction model`/
  `bespoke model (OPSD)` → link; a high-name-containment but *low-similarity* pair → link (the
  `merge_min_similarity` guard); fold direction lower-degree→higher-degree.
- **Pure — `junk_entities`:** `LIVE`, `2`, `merged` (degree ≤ 1) → flagged with the right reason; a
  status-word with high degree → not flagged; a normal entity → not flagged.
- **PG storage — `entity_proposals`:** insert → pending (joined displays) → accept-merge promotes
  via `graph_merge` (the `from` entity gone, edges re-pointed) → status accepted; accept-junk
  deletes via `graph_delete_entity`; reject marks rejected; partial-unique dedupe (re-insert same
  merge pair in either order → `None`; same junk entity → `None`).
- **`graph_review`:** `merge_candidate` + `junk_candidate` findings appear when proposals pending.
- **Deep-dream integration (PG):** dry-run returns `would_merge_propose`/`would_junk` and writes
  nothing; apply persists the merge + junk proposals (and leaves link candidates in `candidates`).
- **Web route dispatch + tool registry + the `== 18` schema-assertion bumps.**

## 7. Risks & caveats

- **False merges are the main risk.** Mitigations stack: name-containment (not similarity alone) +
  the `merge_min_similarity` floor + **proposal-only / human-confirm** (never auto-merge from
  semantic). The hermes `cortex_dedup` precedent — semantic over-grouping distinct facts — is
  exactly why nothing auto-applies here.
- **Junk false positives** are bounded by the degree guard + the narrow rule set (bare-number /
  ≤2-char / curated stopword). Anything ambiguous is left for SP-2 / the human, not flagged.
- **Recall is partial by design.** Shared-head synonyms without containment (`bespoke model`) fall
  to *link*, and junk beyond the rule set is not flagged — acceptable precision/recall trade for a
  review queue (the human still has the full graph). Widening recall is a tuning follow-up.
- **Merge candidates refresh only on a deep-dream run** (they're persisted by the pass, not a live
  analyzer) — acceptable: the deep dream is the graph-cleanup cadence.

## 8. Relationship to prior work & SP-2

- **Reuses Phase-2 C / C.1:** `entity_context_vectors` / `candidate_pairs` (the semantic signal),
  `_full_token_set`, the dry-run/apply + `edge_proposals` review-queue pattern, the Atlas
  mutation/finding scaffolding.
- **SP-2 (upstream prevention)** is the documented next sub-project: tighten the extractor so the
  graph stays clean, measured by an entity-quality eval. SP-1's `junk_entities` rule set and the
  merge-partition heuristics are reusable signals for an extractor-side post-filter.
