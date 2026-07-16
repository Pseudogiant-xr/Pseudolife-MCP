# Deep-Dream Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deep dream trustworthy on real data — stop the auto-merge class from collapsing distinct entities, and restore a real similarity gradient to discovery candidates.

**Architecture:** Two surgical fixes to the pure module `graph_consolidation.py`: (1) the auto-merge identity test uses a *full* token set (no short-token drop) so discriminators like `a`/`b`, `pg`, `C`/`B` survive; (2) candidate generation requires an entity have ≥2 distinct mentioning entries and drops pairs whose supporting-entry sets are identical. One new config knob and a 2-line `service.deep_dream` wiring change carry the second fix's new return value through.

**Tech Stack:** Python 3.12, numpy; pytest (PG-backed tests skip without Postgres, but a live test PG on 127.0.0.1:5433 is reachable here so they RUN).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-29-deep-dream-hardening-design.md` is authoritative.
- **Only the auto-merge path tightens.** Do NOT touch `graph_review.duplicate_candidates` / `graph_review._token_set` — the fuzzy detector keeps its short-token drop for recall in the *manual* queue. The new full-token-set helper is local to `graph_consolidation.py` and used ONLY by `exact_duplicate_pairs`.
- **Mention-scan + text tokenization keep `_token_set`.** Only the entity *identity* test for auto-merge switches to the full token set; the fuzzy text/display matching in `entity_context_vectors` stays on `_token_set`.
- **No change** to storage, schema, MCP tools, Atlas routes, the `edge_proposals` table, or the dry-run/apply/lock structure.
- **Config default verbatim:** `min_entity_mentions: int = 2`.
- **Branch-first.** Create a feature branch before Task 1; do not implement on `master`.
- The pure-module tests live in `tests/test_graph_consolidation.py`; the PG-backed orchestration tests in `tests/test_deep_dream.py`. Run with `.venv/Scripts/python.exe -m pytest <file> -v`.

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
cd /c/Users/<user>/ClaudeCode/Pseudolife-MCP
git checkout master && git pull --ff-only
git checkout -b feat/deep-dream-hardening
```

No test. Commit nothing yet.

---

### Task 1: Fix 1 — full-token-set auto-merge identity

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (add `import re`, a `_full_token_set` helper, and swap `_token_set` → `_full_token_set` inside `exact_duplicate_pairs` only)
- Test: `tests/test_graph_consolidation.py` (append regression tests)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_full_token_set(name: str) -> frozenset[str]` — every alphanumeric token, lowercased, NO length filter. `exact_duplicate_pairs` keeps its existing signature `(entities, edges) -> list[tuple[int,int]]` and behavior, only its identity test changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_consolidation.py`:

```python
def test_exact_duplicate_pairs_keeps_short_discriminators():
    # Distinct entities whose ONLY difference is a token graph_review._token_set
    # would drop (<=2 chars, no digit) must NOT be auto-merged.
    cases = [
        ("Extractor", "pg+extractor"),          # 'pg' dropped by the old filter
        ("heuristic bug (a)", "heuristic bug (b)"),  # 'a'/'b'
        ("Phase-2 Option B", "Phase-2 Option C"),    # 'B'/'C'
    ]
    for da, db in cases:
        ents = [
            {"id": 1, "canonical": da.lower(), "display": da, "etype": None},
            {"id": 2, "canonical": db.lower(), "display": db, "etype": None},
        ]
        assert gc.exact_duplicate_pairs(ents, []) == [], f"should not merge {da!r}/{db!r}"


def test_exact_duplicate_pairs_still_merges_quote_artifacts():
    # Pairs differing only by non-alphanumeric noise (quotes, extra spaces) ARE
    # the same entity and must still auto-merge.
    ents = [
        {"id": 1, "canonical": "fixture devserver", "display": "fixture devserver", "etype": None},
        {"id": 2, "canonical": "'fixture devserver'", "display": "'fixture devserver'", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == [(2, 1)]  # equal degree -> higher id folds into lower
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k "short_discriminators or quote_artifacts" -v`
Expected: `test_exact_duplicate_pairs_keeps_short_discriminators` FAILS (the old `_token_set` drops `pg`/`a`/`b`/`B`/`C`, so the pairs merge and the result is non-empty). `test_..._quote_artifacts` already PASSES (guard).

- [ ] **Step 3: Implement the full-token-set helper + swap**

In `pseudolife_memory/memory/graph_consolidation.py`, add `import re` near the top imports (after `import numpy as np`):

```python
import re
```

Add the helper just below `_disp` (before the Step A section):

```python
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _full_token_set(name: str) -> frozenset[str]:
    """Every alphanumeric token, lowercased, with NO length filter — short
    discriminators (a/b, pg, id, py, version letters) are retained. This is the
    identity test for the AUTO-MERGE class; graph_review._token_set (which drops
    short tokens for recall) is kept for the fuzzy duplicate detector and the
    mention scan."""
    return frozenset(t for t in _WORD_SPLIT.split(str(name).lower()) if t)
```

In `exact_duplicate_pairs`, change the token-set construction line from:

```python
    toks = [(e["id"], _token_set(e["display"])) for e in entities]
```

to:

```python
    toks = [(e["id"], _full_token_set(e["display"])) for e in entities]
```

Nothing else in `exact_duplicate_pairs` changes.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -v`
Expected: PASS — the two new tests plus all existing `exact_duplicate_pairs` tests (`folds_lower_degree_into_higher` with `"Gemma sidecar"`/`"gemma  sidecar"` → both `{gemma,sidecar}` still merge; `ignores_non_identical_token_sets` `"schema v8"`/`"schema 11"` → `{schema,v8}`≠`{schema,11}` still empty; `equal_degree_folds_higher_id_into_lower` `"dup thing"`/`"dup  thing"` → both `{dup,thing}` still `(9,5)`).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "fix(deep-dream): full-token-set auto-merge identity (keep short discriminators)"
```

---

### Task 2: Fix 2 — candidate gradient (min mentions + identical-set drop)

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (`entity_context_vectors`, `candidate_pairs`)
- Modify: `pseudolife_memory/utils/config.py` (`DeepDreamConfig`: add `min_entity_mentions`)
- Modify: `pseudolife_memory/service.py:2889-2892` (unpack the new return; pass `mentions` + `min_mentions`)
- Test: `tests/test_graph_consolidation.py` (add two tests; update two existing tests for the new shapes)

**Interfaces:**
- Consumes: `DeepDreamConfig.min_entity_mentions` (new), `_token_set` (unchanged, for the mention scan).
- Produces:
  - `entity_context_vectors(entities, entries, traces_by_entity, *, min_mentions: int = 2) -> tuple[dict[int, np.ndarray], dict[int, frozenset[int]]]` — `(vectors, mentions)`. An entity is included only if it has ≥ `min_mentions` distinct mentioning entries (that have embeddings); `mentions[id]` is the frozenset of those entry ids.
  - `candidate_pairs(vectors, edges, entities, scope_map, mentions, *, min_similarity=0.55, top_k=50) -> list[dict]` — new 5th positional `mentions`; drops a pair when `mentions[u] == mentions[v]`.

- [ ] **Step 1: Write the failing tests + update the two existing ones**

In `tests/test_graph_consolidation.py`, REPLACE `test_entity_context_vectors_trace_primary_then_mention_fallback` (it now unpacks the tuple and pins `min_mentions=1` because it tests *source selection*, not the threshold):

```python
def test_entity_context_vectors_trace_primary_then_mention_fallback():
    ents = [
        {"id": 1, "canonical": "alpha", "display": "alpha", "etype": None},
        {"id": 2, "canonical": "beta", "display": "beta", "etype": None},
        {"id": 3, "canonical": "ghost", "display": "ghost", "etype": None},
    ]
    entries = [
        {"id": 100, "text": "alpha runs nightly", "embedding": _vec(1, 0)},
        {"id": 101, "text": "beta and alpha discussed", "embedding": _vec(0, 1)},
    ]
    # min_mentions=1: this test checks SOURCE selection (trace vs scan), not the threshold.
    vecs, mentions = gc.entity_context_vectors(ents, entries, {"alpha": [100]}, min_mentions=1)
    assert set(vecs) == {1, 2}                 # ghost omitted (no trace, no mention)
    assert np.allclose(vecs[1], _vec(1, 0))    # alpha from its trace entry
    assert np.allclose(vecs[2], _vec(0, 1))    # beta from the mention scan
    assert mentions[1] == frozenset({100}) and mentions[2] == frozenset({101})
```

REPLACE `test_candidate_pairs_filters_edges_scope_and_threshold` (adds the required `mentions` arg with DISTINCT sets so the identical-set drop doesn't interfere):

```python
def test_candidate_pairs_filters_edges_scope_and_threshold():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
        {"id": 4, "canonical": "d", "display": "d", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0), 4: _vec(0, 1)}
    mentions = {1: frozenset({10}), 2: frozenset({20}),
                3: frozenset({30}), 4: frozenset({40})}   # all distinct
    edges = [{"id": 9, "src_id": 1, "relation": "related-to", "dst_id": 3,
              "confidence": 0.45, "origin": "agent"}]
    scope = {1: ["pseudolife"], 2: ["pseudolife"], 3: ["gw2-reshade"], 4: ["pseudolife"]}
    out = gc.candidate_pairs(vectors, edges, ents, scope, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    # 1-2 kept (sim 1.0, same scope, no edge, distinct mention-sets). 1-3 dropped (edge).
    # 2-3 dropped (disjoint scope). 1-4 / 2-4 dropped (sim 0 < 0.55).
    assert pairs == {(1, 2)}
    assert out[0]["similarity"] == 1.0
```

APPEND two new tests:

```python
def test_entity_context_vectors_min_mentions_gate():
    ents = [
        {"id": 1, "canonical": "one", "display": "one", "etype": None},   # 1 entry
        {"id": 2, "canonical": "two", "display": "two", "etype": None},   # 2 entries
    ]
    entries = [
        {"id": 10, "text": "one only", "embedding": _vec(1, 0)},
        {"id": 20, "text": "two here", "embedding": _vec(1, 0)},
        {"id": 21, "text": "two again", "embedding": _vec(0, 1)},
    ]
    traces = {"one": [10], "two": [20, 21]}
    vecs, mentions = gc.entity_context_vectors(ents, entries, traces)  # default min_mentions=2
    assert set(vecs) == {2}                          # 'one' omitted (only 1 mention)
    assert mentions[2] == frozenset({20, 21})


def test_candidate_pairs_drops_identical_mention_sets():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0)}
    # 1 and 2 share the SAME supporting entries (pure co-occurrence) -> dropped.
    # 3 has a distinct set -> 1-3 and 2-3 survive.
    mentions = {1: frozenset({10, 11}), 2: frozenset({10, 11}), 3: frozenset({12, 13})}
    out = gc.candidate_pairs(vectors, [], ents, {}, mentions,
                             min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    assert pairs == {(1, 3), (2, 3)}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k "context_vectors or candidate_pairs or min_mentions or identical_mention" -v`
Expected: FAIL — `entity_context_vectors` currently returns a dict (tuple-unpack raises / `set(vecs)` wrong), and `candidate_pairs` currently has no `mentions` parameter (TypeError) and no identical-set drop.

- [ ] **Step 3: Implement the two function changes**

In `pseudolife_memory/memory/graph_consolidation.py`, REPLACE `entity_context_vectors` with:

```python
def entity_context_vectors(entities: list[dict], entries: list[dict],
                           traces_by_entity: dict[str, list[int]], *,
                           min_mentions: int = 2,
                           ) -> tuple[dict[int, np.ndarray], dict[int, frozenset[int]]]:
    """Per-entity context vector = L2-normalized mean of its mentioning entries'
    embeddings, plus the set of those entry ids. Trace entries are the primary
    source; entities without traces fall back to a token-mention scan. An entity is
    included only if it has >= min_mentions DISTINCT mentioning entries (with
    embeddings) — a centroid-of-one isn't a context. Returns (vectors, mentions)."""
    by_id = {e["id"]: e for e in entries}
    entry_tokens = [(e["id"], _token_set(e.get("text", ""))) for e in entries]
    vectors: dict[int, np.ndarray] = {}
    mentions: dict[int, frozenset[int]] = {}
    for ent in entities:
        ids = list(traces_by_entity.get(ent["canonical"], []))
        if not ids:
            want = _token_set(ent["display"])
            if want:
                ids = [eid for eid, toks in entry_tokens if want <= toks]
        valid = {i for i in ids if i in by_id}      # distinct entries with embeddings
        if len(valid) < min_mentions:
            continue
        embs = [by_id[i]["embedding"] for i in valid]
        vectors[ent["id"]] = _l2(np.mean(np.stack(embs), axis=0))
        mentions[ent["id"]] = frozenset(valid)
    return vectors, mentions
```

REPLACE `candidate_pairs` with (adds the `mentions` parameter and the identical-set drop; everything else identical):

```python
def candidate_pairs(vectors: dict[int, np.ndarray], edges: list[dict],
                    entities: list[dict], scope_map: dict[int, list[str]],
                    mentions: dict[int, frozenset[int]], *,
                    min_similarity: float = 0.55, top_k: int = 50) -> list[dict]:
    """Unlinked, scope-coherent, semantically-near entity pairs — the link
    candidates. Drops pairs that already have an edge (either direction), exact
    duplicates (a Step-A merge), have IDENTICAL supporting-entry sets (pure
    co-occurrence, not independent similarity), or sit in disjoint non-empty
    project scopes."""
    disp = _disp(entities)
    linked = {frozenset((e["src_id"], e["dst_id"])) for e in edges}
    dup = {frozenset(p) for p in exact_duplicate_pairs(entities, edges)}
    ids = sorted(vectors)
    scored: list[dict] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            u, v = ids[i], ids[j]
            key = frozenset((u, v))
            if key in linked or key in dup:
                continue
            mu, mv = mentions.get(u), mentions.get(v)
            if mu is not None and mu == mv:        # identical support -> co-occurrence
                continue
            su, sv = set(scope_map.get(u, [])), set(scope_map.get(v, []))
            if su and sv and not (su & sv):        # disjoint, both attributed
                continue
            sim = float(np.dot(vectors[u], vectors[v]))
            if sim < min_similarity:
                continue
            scored.append({"src_id": u, "dst_id": v, "src": disp.get(u, str(u)),
                           "dst": disp.get(v, str(v)), "similarity": round(sim, 4)})
    scored.sort(key=lambda c: (-c["similarity"], c["src_id"], c["dst_id"]))
    return scored[:top_k]
```

- [ ] **Step 4: Add the config knob**

In `pseudolife_memory/utils/config.py`, in the `DeepDreamConfig` dataclass, add the field (next to `max_context_snippets`):

```python
    min_entity_mentions: int = 2       # an entity needs >= this many distinct mentioning entries to be candidate-eligible
```

- [ ] **Step 5: Wire `service.deep_dream`**

In `pseudolife_memory/service.py`, REPLACE lines 2889-2892:

```python
        vectors = gc.entity_context_vectors(entities, entries, traces)
        candidates = gc.candidate_pairs(
            vectors, edges, entities, scope_map,
            min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
```

with:

```python
        vectors, mentions = gc.entity_context_vectors(
            entities, entries, traces, min_mentions=cfg.min_entity_mentions)
        candidates = gc.candidate_pairs(
            vectors, edges, entities, scope_map, mentions,
            min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
```

(The following `self._attach_candidate_snippets(...)` line is unchanged.)

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py tests/test_deep_dream.py -v`
Expected: PASS. `test_graph_consolidation.py` covers the unit changes; `test_deep_dream.py` (live PG) exercises `service.deep_dream` end-to-end and would fail if the Step-5 wiring were missed (the tuple-return would break `candidate_pairs`).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_graph_consolidation.py
git commit -m "fix(deep-dream): candidate gradient — min_entity_mentions + identical-mention-set drop"
```

---

## Self-Review (completed against the spec)

**Spec coverage:**
- §3 Fix 1 full-token-set auto-merge identity (keep discriminators, keep quote-artifacts) → Task 1. ✓
- §4a `min_entity_mentions` (default 2) → Task 2 Steps 3-4. ✓
- §4b identical-mention-set drop → Task 2 Steps 1, 3. ✓
- §4 `entity_context_vectors` returns `(vectors, mentions)`; `candidate_pairs` gains `mentions`; `service.deep_dream` wiring → Task 2 Steps 3, 5. ✓
- §6 testing (regression false-positives, quote-artifact guard, min_mentions gate, identical-set drop, updated existing tests, return-shape) → Tasks 1-2. ✓
- "only auto-merge tightens; fuzzy detector + mention scan keep `_token_set`" → Global Constraints + Task 1 (helper local, used only in `exact_duplicate_pairs`). ✓

**Placeholder scan:** none — every code step has complete code and exact commands.

**Type consistency:** `_full_token_set(name)->frozenset[str]`; `entity_context_vectors(...,*,min_mentions=2)->tuple[dict,dict]`; `candidate_pairs(vectors,edges,entities,scope_map,mentions,*,min_similarity,top_k)`; `DeepDreamConfig.min_entity_mentions:int=2`; service call site unpacks `(vectors, mentions)` and passes `mentions`. Consistent across tasks.
