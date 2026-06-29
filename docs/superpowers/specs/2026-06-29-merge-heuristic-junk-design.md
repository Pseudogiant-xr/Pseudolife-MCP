# Deep-dream merge/junk heuristic tightening — design

- Date: 2026-06-29
- Status: approved (brainstorm); pending spec review
- Area: `pseudolife_memory/memory/graph_consolidation.py` (pure module) + a one-time live runbook

## Problem

The deep-dream entity-consolidation proposes noisy MERGE candidates and misses a
class of artifact entities. Observed on the live bank (2026-06-29):

- **Single-token-subset false merges** — `memory_graph → Graph`, `bank → live bank`,
  `LIVE → live daemon`. The contained side is a single generic token (`{graph}`,
  `{bank}`, `{live}`) that is a subset of countless longer names.
- **Merge-into-artifact** — `Phase 2 plan → Phase 1 plan<->Phase 2 plan`,
  `schema v8 → schema v8 <-> schema 11`. Real entities get absorbed *into*
  concatenated `A<->B` names.
- **`A<->B` artifact nodes** — entities literally named `memory_recall<->recall.py`,
  `schema v8 <-> schema 11`, `Phase 1 plan<->Phase 2 plan`,
  `memory_digest<->memory_communities`, `claude-code<->CLAUDE.md`, `Track A<->Track B`
  exist as graph nodes. They are extraction artifacts: a relation separator was
  captured into an entity name. The current `junk_entities` rule (bare-number /
  ≤2-char / status-word, gated to degree ≤ 1) never catches them — they often have
  edges (they became merge targets).

Root cause: `_name_contains` returns a merge reason whenever one name's full token
set is a subset of the other's (or a norm-name substring), with **no floor** on the
contained side and **no artifact exclusion**.

## Decisions (from brainstorm)

1. **Scope** — fix the generation heuristics AND auto-clean the existing `A<->B`
   nodes on the live bank (no per-item review).
2. **Merge gate** — require the contained (smaller) token set to have **≥ 2 tokens**;
   drop single-token-subset merges.
3. **`A<->B` junk rule** — **degree-agnostic** detection of relation-separator names.

## Design

All logic changes live in the pure, DB-free module `graph_consolidation.py`.

### Component 1 — concat-artifact detector (shared helper)

`_is_concat_artifact(name) -> bool`: true when the display contains a relation
separator with non-empty text on **both** sides. Separator set: `<->`, `<-->` (and
longer `<--->`), `↔`, `→`, ` -> `. Implementation requires a non-space char on each
side of the arrow so a name that merely starts/ends with an arrow char is not caught.

Used by **both** the junk rule (Component 3) and the merge gate (Component 2), and by
the auto-clean runbook's filter (Component 4) — defined once.

### Component 2 — tighten the merge gate (`_name_contains`)

```python
def _name_contains(a, b):
    if _is_concat_artifact(a) or _is_concat_artifact(b):
        return None                       # artifacts are junk, never merge endpoints
    ta, tb = _full_token_set(a), _full_token_set(b)
    if min(len(ta), len(tb)) < 2:         # single-token containment = generic, not a dup
        return None
    if ta and tb and (ta <= tb or tb <= ta):
        return "token-subset"
    na, nb = norm_name(a), norm_name(b)
    if na and nb and (na in nb or nb in na):
        return "substring"
    return None
```

The `≥ 2` floor and the artifact exclusion apply to both the subset and substring
branches via the early returns. Nothing else in `partition_candidates` changes; it
already routes a `None` reason to the LINK bucket.

### Component 3 — junk rule (`junk_entities`)

Add a degree-agnostic concat-artifact check **before** the degree gate:

```python
for e in entities:
    d = str(e["display"]).strip()
    if _is_concat_artifact(d):
        out.append({..., "reason": "concat-artifact"})   # degree-agnostic
        continue
    if deg.get(e["id"], 0) > max_degree:
        continue
    # existing bare-number / too-short / status-word (still degree-gated)
```

### Component 4 — auto-clean the existing `A<->B` nodes (runbook, no new code)

Reuses the tested accept-junk path instead of raw SQL against the live bank (raw
one-off ops risk lock contention with the running daemon — a logged lesson):

1. `ops/backup.ps1`.
2. `memory_deep_dream(apply=True)` on the live bank → the new rule proposes every
   `A<->B` node as `concat-artifact` junk.
3. Fetch pending entity proposals; for each with `reason == "concat-artifact"`, call
   `memory_graph_accept_entity_junk(id)` (unlinks facts → cascades edges/aliases →
   deletes). Log the deleted display names.
4. Verify in the Atlas review queue that the `A<->B` nodes and the bad merges are gone.

## Test plan (TDD)

Pure-function unit tests in the `graph_consolidation` / `deep_dream` suites (the
partition/junk functions take plain dicts — no Postgres needed):

- `_name_contains`: single-token-subset → `None`; ≥2-token subset → `"token-subset"`;
  a concat-artifact endpoint → `None`.
- `partition_candidates`: a single-token pair routes to LINK, not MERGE; a
  concat-artifact target is not merged into.
- `junk_entities`: a concat-artifact name is flagged regardless of degree; the
  existing bare-number / too-short / status-word rules stay degree-gated.
- Regression: existing `test_deep_dream` / `test_graph_review` merge/junk/exact-dup
  tests stay green.

## Risks / mitigations

- **Over-matching the arrow set** (a legit `->` in a name) → require non-empty text on
  both sides; accept residual risk (entities with arrows are almost always artifacts).
- **Auto-clean deletes real nodes** → backup first; reuse the tested accept-junk path;
  log the deleted list; rollback via backup.
- **`≥ 2`-token floor drops a legit single-token rename** → those are exact duplicates
  (Jaccard == 1.0), already handled by Step-A `exact_duplicate_pairs`, not this path.

## Net effect on today's live queue

All ~7 bad merge proposals disappear (single-token ones dropped; `A<->B`-target ones
become junk), the legit merges remain (`Atlas Review → Atlas Review queue`,
`4090 27B → 4090/Qwen3.6-27B`, tool aliases), and the `A<->B` artifact nodes are
removed.
