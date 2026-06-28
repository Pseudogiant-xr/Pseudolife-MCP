# Deep-Dream Hardening (Phase 2 / "C.1") — Design

**Status:** design · **Date:** 2026-06-29 · **Author:** Claude (Opus 4.8) + user

## 1. Problem & motivation

The first live dry-run of the deep dream (`memory_deep_dream(apply=False)`, 2026-06-28, on the
801-entity / 337-edge bank) surfaced two real-data gaps that the clean synthetic test fixtures
never exercised. Caught by the dry-run-first discipline before any `apply`:

1. **Auto-merge false positives (safety blocker).** `graph_consolidation.exact_duplicate_pairs`
   decides "exact duplicate" by `graph_review._token_set` equality, but `_token_set` *drops tokens
   ≤2 chars without a digit*. Short **discriminators** therefore vanish, so genuinely distinct
   entities look identical and would be **destructively** merged (`merge_entity` deletes the folded
   entity). Observed would-merges that are wrong:
   - `Extractor` ↔ `pg+extractor` (`pg` dropped — Gemma sidecar ≠ the Postgres+extractor pair)
   - `heuristic bug (b)` ↔ `heuristic bug (a)` (`(a)`/`(b)` dropped — two different bugs)
   - `Phase-2 Option C` ↔ `Phase-2 Option B` (`C`/`B` dropped — two different decision branches)

   Legit merges in the same batch (must be preserved): quote-artifacts like
   `'fixture devserver'` ↔ `fixture devserver`.

2. **Candidate degeneracy (quality, not safety).** All 50 discovery candidates scored cosine
   **exactly 1.000**. `entity_context_vectors` collapses to a *single entry's embedding* for
   sparsely-mentioned entities, so two entities mentioned in the same memory get identical vectors
   → 1.0. "Semantic near-pair across sessions" degrades into "co-mentioned in one memory," and the
   similarity score carries no ranking signal (e.g. `MIRAS bands ↔ quality angle`). Discovery is
   proposal-only + human-gated, so this is not a safety risk — but the discovery half isn't
   delivering its intended cross-context signal.

The 4 type-violation supersedes were correct. But the tool applies supersede **and** merge together
under `auto_apply_safe`, so the safe half can't be applied alone — both must be trustworthy before
any `apply`.

## 2. Goal & scope

**Goal:** make the deep dream trustworthy on real data — the auto-merge class genuinely
provably-safe, and the discovery similarity a real gradient — so a future `apply` is safe and
discovery is useful.

**In scope:**
- `pseudolife_memory/memory/graph_consolidation.py` (the pure module): Fix 1 + Fix 2.
- `DeepDreamConfig` (`utils/config.py`): one new knob.
- `service.deep_dream` wiring: pass the new config + the new return value through (≈2 lines).
- Tests (incl. the two real false-positive cases as regression fixtures).

**Out of scope (non-goals):** no change to the orchestration's lock/dry-run/apply structure, the
`edge_proposals` table, the MCP tools, the Atlas routes, or the proposal-only safety model. No
change to the *fuzzy* duplicate detector (`graph_review.duplicate_candidates`, Jaccard ≥ 0.6) — it
intentionally keeps `_token_set`'s noise-dropping for recall in the *manual* review queue. Only the
**auto-merge** path tightens.

## 3. Fix 1 — exact-merge identity (full token set)

Add a local helper and use it *only* in the auto-merge path:

```python
import re
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

def _full_token_set(name: str) -> frozenset[str]:
    """Every alphanumeric token, lowercased, NO length filter — so short
    discriminators (a/b, pg, id, py, version letters) are retained. This is the
    identity test for the AUTO-MERGE class; the fuzzy duplicate detector keeps
    graph_review._token_set (which drops short tokens for recall)."""
    return frozenset(t for t in _WORD_SPLIT.split(str(name).lower()) if t)
```

`exact_duplicate_pairs` swaps `_token_set(e["display"])` → `_full_token_set(e["display"])`. Nothing
else in the function changes (degree-based fold direction, tie-break, sort all unchanged).

Outcome on the observed cases:

| pair | full token sets | auto-merged? |
|---|---|---|
| `Extractor` / `pg+extractor` | `{extractor}` / `{pg,extractor}` | no ✓ |
| `bug (a)` / `bug (b)` | `{…,a}` / `{…,b}` | no ✓ |
| `Option C` / `Option B` | `{…,c}` / `{…,b}` | no ✓ |
| `'fixture devserver'` / `fixture devserver` | `{fixture,devserver}` / same | **yes** ✓ (quotes are non-alnum, split away) |
| `graph_review` / `graph_review.py` | `{graph,review}` / `{graph,review,py}` | no — drops to the manual queue (conservative, correct for an auto-apply class) |

Precision-over-recall is the right call for an auto-apply class: borderline granularity pairs
(`module` vs `module.py`, `table` vs `table.id`) fall through to the *manual* Atlas merge queue
rather than being merged unsupervised.

*Alternative considered:* `norm_name` string equality — equivalent on these cases but order- and
separator-sensitive; the token set is simpler and sufficient.

## 4. Fix 2 — candidate gradient (min mentions + identical-set drop)

Two guards, both config-tunable, targeting the two ways a 1.000 collision arises.

**4a. `min_entity_mentions` (default 2).** `entity_context_vectors` omits any entity supported by
fewer than `min_mentions` *distinct* mentioning entries. A centroid-of-one isn't a "context"; this
removes the single-entry collisions that dominate the degeneracy. (Entry ids are de-duplicated
first — a single entry traced under multiple attributes counts once.)

**4b. Identical-mention-set drop.** A pair whose two entities have an *identical* supporting-entry
set is pure co-occurrence in the same documents, not independent cross-context similarity — drop it.

Implementing 4b requires the per-entity mention-sets at pairing time, so `entity_context_vectors`
returns them alongside the vectors:

```python
def entity_context_vectors(entities, entries, traces_by_entity, *, min_mentions=2
        ) -> tuple[dict[int, np.ndarray], dict[int, frozenset[int]]]:
    """Returns (vectors, mentions). An entity is included only if it has
    >= min_mentions distinct mentioning entries (with embeddings)."""
    ...
    # per entity: ids = dedup(traces or mention-scan); valid = [i for i in ids if i in by_id]
    # if len(set(valid)) < min_mentions: continue
    # vectors[id] = _l2(mean(embeddings(valid)));  mentions[id] = frozenset(valid)

def candidate_pairs(vectors, edges, entities, scope_map, mentions, *,
                    min_similarity=0.55, top_k=50) -> list[dict]:
    """... drops a pair when mentions[u] == mentions[v] (identical support)."""
```

`service.deep_dream` updates to:

```python
vectors, mentions = gc.entity_context_vectors(
    entities, entries, traces, min_mentions=cfg.min_entity_mentions)
candidates = gc.candidate_pairs(
    vectors, edges, entities, scope_map, mentions,
    min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
```

`DeepDreamConfig` gains `min_entity_mentions: int = 2`.

**Caveat (documented, accepted):** on a sparse bank many entities have only one mentioning entry,
so the `≥2` threshold may reduce candidate *volume* substantially — possibly to near-zero on the
current bank. That is honest (weak cross-context signal is better surfaced as *few* candidates than
50 spurious 1.0s); the threshold is a config knob to relax if desired. This is a recall/precision
lever, not a correctness issue.

## 5. Components & data flow

```
graph_consolidation.py
  _full_token_set(name)                      [NEW helper]
  exact_duplicate_pairs(...)                 [_token_set -> _full_token_set]
  entity_context_vectors(..., min_mentions)  [dedup ids; >=min_mentions gate; return (vectors, mentions)]
  candidate_pairs(..., mentions, ...)        [NEW param; drop identical-mention-set pairs]

service.deep_dream                           [unpack (vectors, mentions); pass mentions + min_mentions]
utils/config.DeepDreamConfig                 [+ min_entity_mentions = 2]
```

No storage, schema, tool, route, or safety-model change.

## 6. Testing

- **Fix 1 — `exact_duplicate_pairs` regression cases (the real false positives):** assert
  `Extractor`/`pg+extractor`, `bug (a)`/`bug (b)`, `Option C`/`Option B` are NOT returned; assert a
  quote-artifact pair (`'fixture devserver'`/`fixture devserver`) IS returned; assert
  `graph_review`/`graph_review.py` is NOT (full-set differs by `py`).
- **Fix 2a — `min_entity_mentions`:** an entity with one mentioning entry is omitted at the default
  `min_mentions=2`; an entity with two distinct entries is included. The existing trace-vs-fallback
  source-selection test is updated to call with `min_mentions=1` (it tests *source*, not the
  threshold).
- **Fix 2b — identical-mention-set drop:** two entities with the same mention-set produce no
  candidate; two with overlapping-but-distinct sets and high similarity still do. (The existing
  `candidate_pairs` filter test is updated to pass a `mentions` dict with distinct sets so the new
  drop doesn't interfere with the edge/scope/threshold assertions.)
- **Return-shape:** `entity_context_vectors` returns a 2-tuple; the two existing Task-3 tests are
  updated to unpack it.
- Full suite stays green.

## 7. Rollout

Standard daemon-only redeploy (`ops/update.ps1`: backup → rollback-tag → rebuild → `/health`) — no
schema change, so the upgrade is pure code. Then **re-run `memory_deep_dream(apply=False)`** and
confirm: the three false-positive merges are gone from `would_merge`, and candidate similarities
show a real gradient (no longer all 1.000). Only after that clean dry-run do we consider an `apply`.
Cortex reconciliation stays on hold until the deep dream is trustworthy here.
