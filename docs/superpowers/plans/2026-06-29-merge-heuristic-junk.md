# Merge-heuristic tightening + A<->B junk rule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the deep dream from proposing single-token-subset merges and merges *into* `A<->B` artifact names, and surface those artifacts as junk so they can be cleaned.

**Architecture:** All logic lives in the pure, DB-free module `pseudolife_memory/memory/graph_consolidation.py`. A shared `_is_concat_artifact` detector gates both the merge classifier (`_name_contains`) and the junk classifier (`junk_entities`). Unit tests are pure (plain dicts, no Postgres) in `tests/test_graph_consolidation.py`. The existing live `A<->B` nodes are cleaned afterward via a backup-first `memory_deep_dream(apply=True)` + auto-accept of the resulting `concat-artifact` junk proposals (reuses the tested delete path).

**Tech Stack:** Python 3, pytest. Run tests with the repo venv: `./.venv/Scripts/python.exe -m pytest`.

## Global Constraints

- No new dependencies; `graph_consolidation.py` stays pure/DB-free (imports only `re`, `numpy`, and existing `pseudolife_memory.*` helpers).
- Follow existing test conventions in `tests/test_graph_consolidation.py`: entity dicts `{"id","canonical","display","etype"}`, the `_edge(eid, s, rel, d, conf, origin="agent")` helper, `gc.<func>` calls.
- Merge fold direction and `merge_min_similarity=0.90` default are unchanged.
- Run pytest via `./.venv/Scripts/python.exe -m pytest` (Windows; the venv has torch/pgvector).
- Work on branch `feat/merge-heuristic-junk` (already holds the design spec commit).

---

### Task 1: `_is_concat_artifact` detector

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (add near the `_JUNK_STOPWORDS` / `_BARE_NUMBER` block, ~line 162)
- Test: `tests/test_graph_consolidation.py`

**Interfaces:**
- Produces: `_is_concat_artifact(name: str) -> bool` — True when `name` contains a relation separator (`<->`, `<-->`, `↔`, `->`, `→`) with non-empty text on both sides.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_consolidation.py`:

```python
def test_is_concat_artifact_detects_relation_separators():
    for name in ["memory_recall<->recall.py", "schema v8 <-> schema 11",
                 "a ↔ b", "x -> y", "Phase 1 plan<->Phase 2 plan"]:
        assert gc._is_concat_artifact(name) is True, name


def test_is_concat_artifact_ignores_plain_names():
    for name in ["memory_graph", "Atlas Review queue", "claude-code", "4090/Qwen3.6-27B"]:
        assert gc._is_concat_artifact(name) is False, name


def test_is_concat_artifact_requires_nonempty_both_sides():
    assert gc._is_concat_artifact("<-> y") is False   # empty left
    assert gc._is_concat_artifact("x <->") is False   # empty right
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k is_concat_artifact -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_is_concat_artifact'`.

- [ ] **Step 3: Implement `_is_concat_artifact`**

In `graph_consolidation.py`, just below `_BARE_NUMBER = re.compile(r"^\d+$")`:

```python
# A relation separator captured into an entity name (extraction artifact), e.g.
# "memory_recall<->recall.py". Longest arrow first so "<->" isn't split as "->".
_ARROW = re.compile(r"<-+>|↔|->|→")


def _is_concat_artifact(name: str) -> bool:
    """True if `name` is two names joined by a relation arrow (<->, ->, ↔, →) —
    a captured-relation extraction artifact. Requires non-empty text on both
    sides, so a name merely starting/ending with an arrow char is not caught."""
    parts = [p.strip() for p in _ARROW.split(str(name))]
    return len(parts) >= 2 and sum(1 for p in parts if p) >= 2
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k is_concat_artifact -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "feat(deep-dream): _is_concat_artifact detector for A<->B extraction artifacts"
```

---

### Task 2: tighten the merge classifier (`_name_contains` + `partition_candidates`)

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py:165-174` (`_name_contains`)
- Test: `tests/test_graph_consolidation.py` (add new tests; UPDATE the existing `test_partition_candidates_merge_vs_link`)

**Interfaces:**
- Consumes: `_is_concat_artifact` (Task 1), `_full_token_set`, `norm_name` (existing).
- Produces: `_name_contains(a, b)` returns `"token-subset"` / `"substring"` only when the smaller token set has ≥2 tokens and neither name is a concat-artifact; else `None`. `partition_candidates` is unchanged but inherits the new behavior.

- [ ] **Step 1: Write the failing unit tests + the new partition tests**

Append to `tests/test_graph_consolidation.py`:

```python
def test_name_contains_requires_two_contained_tokens():
    assert gc._name_contains("Atlas Review", "Atlas Review queue") == "token-subset"
    assert gc._name_contains("memory_graph", "Graph") is None       # {graph} = 1 token
    assert gc._name_contains("bank", "live bank") is None           # {bank} = 1 token


def test_name_contains_excludes_concat_artifacts():
    assert gc._name_contains("Phase 2 plan", "Phase 1 plan<->Phase 2 plan") is None


def test_partition_candidates_single_token_subset_is_link_not_merge():
    ents = [
        {"id": 1, "canonical": "bank", "display": "bank", "etype": None},
        {"id": 2, "canonical": "live bank", "display": "live bank", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "bank", "dst": "live bank", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(1, 2)]


def test_partition_candidates_concat_artifact_target_is_not_merged():
    ents = [
        {"id": 1, "canonical": "phase 2 plan", "display": "Phase 2 plan", "etype": None},
        {"id": 2, "canonical": "phase 1 plan<->phase 2 plan",
         "display": "Phase 1 plan<->Phase 2 plan", "etype": None},
    ]
    pairs = [{"src_id": 1, "dst_id": 2, "src": "Phase 2 plan",
              "dst": "Phase 1 plan<->Phase 2 plan", "similarity": 0.99}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []                         # artifact endpoint excluded from merge
    assert len(links) == 1
```

UPDATE the existing `test_partition_candidates_merge_vs_link` (it asserts the now-dropped
single-token `daemon`/`live daemon` merge). Replace its body with a ≥2-token mergeable pair:

```python
def test_partition_candidates_merge_vs_link():
    ents = [
        {"id": 1, "canonical": "atlas review", "display": "Atlas Review", "etype": None},
        {"id": 2, "canonical": "atlas review queue", "display": "Atlas Review queue", "etype": None},
        {"id": 3, "canonical": "track a (recall)", "display": "Track A (recall)", "etype": None},
        {"id": 4, "canonical": "track b (insight)", "display": "Track B (insight)", "etype": None},
    ]
    # entity 1 has an edge (degree 1) so 'Atlas Review queue' folds into 'Atlas Review'.
    edges = [_edge(99, 1, "related-to", 3, 0.45)]
    pairs = [
        {"src_id": 1, "dst_id": 2, "src": "Atlas Review", "dst": "Atlas Review queue", "similarity": 0.99},
        {"src_id": 3, "dst_id": 4, "src": "Track A (recall)", "dst": "Track B (insight)", "similarity": 0.98},
    ]
    merges, links = gc.partition_candidates(pairs, ents, edges, merge_min_similarity=0.90)
    assert [(m["from_id"], m["into_id"]) for m in merges] == [(2, 1)]   # ≥2-token subset -> merge
    assert merges[0]["reason"] == "token-subset"
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(3, 4)]      # distinct names -> link
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k "name_contains or partition_candidates" -q`
Expected: FAIL — `_name_contains` single-token cases still return `"token-subset"`; the concat/single-token partition tests fail; the updated `merge_vs_link` may fail until impl lands.

- [ ] **Step 3: Implement the tightened `_name_contains`**

Replace the body of `_name_contains` (`graph_consolidation.py:165-174`) with:

```python
def _name_contains(a: str, b: str) -> str | None:
    """A reason if one display asserts identity with the other, else None.
    Guards: an A<->B concat artifact is never a merge endpoint (it's junk), and
    the smaller token set must have >=2 tokens — single-token containment (a
    generic word that is a subset of countless names) is too weak to auto-merge."""
    if _is_concat_artifact(a) or _is_concat_artifact(b):
        return None
    ta, tb = _full_token_set(a), _full_token_set(b)
    if min(len(ta), len(tb)) < 2:
        return None
    if ta and tb and (ta <= tb or tb <= ta):
        return "token-subset"
    na, nb = norm_name(a), norm_name(b)
    if na and nb and (na in nb or nb in na):
        return "substring"
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k "name_contains or partition_candidates" -q`
Expected: PASS (all partition + name_contains tests green, including the updated `merge_vs_link`).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "fix(deep-dream): require >=2 contained tokens + exclude A<->B from merge proposals"
```

---

### Task 3: junk rule for concat artifacts (degree-agnostic)

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py:205-224` (`junk_entities`)
- Test: `tests/test_graph_consolidation.py`

**Interfaces:**
- Consumes: `_is_concat_artifact` (Task 1).
- Produces: `junk_entities` additionally flags concat-artifact displays with `reason="concat-artifact"`, regardless of degree; the existing bare-number/too-short/status-word rules stay degree-gated.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph_consolidation.py`:

```python
def test_junk_entities_flags_concat_artifacts_regardless_of_degree():
    ents = [
        {"id": 1, "canonical": "memory_recall<->recall.py",
         "display": "memory_recall<->recall.py", "etype": None},
        {"id": 2, "canonical": "recall.py", "display": "recall.py", "etype": None},
        {"id": 3, "canonical": "memory_recall", "display": "memory_recall", "etype": None},
    ]
    # entity 1 is well-connected (degree 2) yet must still be flagged as an artifact
    edges = [_edge(10, 1, "related-to", 2, 0.45), _edge(11, 1, "related-to", 3, 0.45)]
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "concat-artifact"}   # 2 and 3 are real; flagged despite degree 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k junk_entities_flags_concat -q`
Expected: FAIL — `out == {}` (entity 1 has degree 2 > max_degree, so the current rule skips it).

- [ ] **Step 3: Implement the concat-artifact junk branch**

In `junk_entities` (`graph_consolidation.py`), put the concat check FIRST inside the loop,
before the degree gate:

```python
    for e in entities:
        d = str(e["display"]).strip()
        if _is_concat_artifact(d):
            out.append({"entity_id": e["id"], "display": e["display"],
                        "reason": "concat-artifact"})   # degree-agnostic
            continue
        if deg.get(e["id"], 0) > max_degree:
            continue
        if _BARE_NUMBER.match(d):
            reason = "bare-number"
        elif len(d) <= 2:
            reason = "too-short"
        elif d.lower() in _JUNK_STOPWORDS:
            reason = "status-word"
        else:
            continue
        out.append({"entity_id": e["id"], "display": e["display"], "reason": reason})
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k junk_entities -q`
Expected: PASS — both `test_junk_entities_flags_artifacts_not_real` and the new concat test green.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "feat(deep-dream): junk_entities flags A<->B concat artifacts (degree-agnostic)"
```

---

### Task 4: full regression, then ship + auto-clean (user-present)

**Files:**
- No code changes. Verification + an operational runbook.

- [ ] **Step 1: Run the affected suites green**

Run:
```bash
./.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py tests/test_deep_dream.py tests/test_graph_review.py -q
```
Expected: PASS, no regressions. (`test_deep_dream.py` integration tests use key-presence
assertions, so the `daemon`/`live daemon` behavior change does not break them.)

- [ ] **Step 2: Merge to local master**

```bash
git checkout master && git merge --ff-only feat/merge-heuristic-junk && git branch -d feat/merge-heuristic-junk
```

- [ ] **Step 3: Deploy the daemon (user-present, explicit authorization required)**

```bash
pwsh -NoProfile -File ops/backup.ps1
docker tag pseudolife-daemon:0.2.0 pseudolife-daemon:0.2.0-pre-mergeheuristic
docker compose -f ops/docker-compose.yml up -d --no-deps --build pseudolife-daemon
```
Verify `/health` (schema 18, ok) and that pg/extractor stay up.

- [ ] **Step 4: Auto-clean the existing A<->B nodes (the runbook)**

Via the MCP tools against the live bank:
1. `memory_deep_dream(apply=True)` — the new rule proposes every `A<->B` node as
   `concat-artifact` junk and stops proposing the bad merges.
2. Fetch pending entity proposals; for each with `reason == "concat-artifact"`, call
   `memory_graph_accept_entity_junk(id)`. Log each deleted display name.
3. Open the Atlas review queue and confirm: no `A<->B` nodes remain, the single-token
   and merge-into-artifact proposals are gone, and the legit merges (e.g.
   `Atlas Review → Atlas Review queue`) remain.

- [ ] **Step 5: Commit any doc/CHANGELOG updates**

Add a `### Fixed` CHANGELOG bullet describing the heuristic tightening + `concat-artifact`
junk rule, and commit.

---

## Self-Review

**Spec coverage:**
- Component 1 (detector) → Task 1. ✓
- Component 2 (merge gate: ≥2 contained tokens + artifact exclusion) → Task 2. ✓
- Component 3 (degree-agnostic junk rule) → Task 3. ✓
- Component 4 (backup-first deep-dream apply + auto-accept concat junk) → Task 4 Steps 3–4. ✓
- Test plan → Tasks 1–3 RED/GREEN + Task 4 Step 1 regression. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `_is_concat_artifact(str)->bool`, `_name_contains(a,b)->str|None`, and the
`junk_entities` output dict shape `{"entity_id","display","reason"}` match the existing module
and tests. The updated `test_partition_candidates_merge_vs_link` and the new tests use the
established entity-dict / `_edge` conventions.

**Behavior-change note:** Task 2 intentionally changes `daemon`/`live daemon` (and any
single-token-subset pair) from MERGE to LINK; the existing test is updated in the same task,
and the `test_deep_dream` integration assertions are key-presence (not count) so they stay green.
