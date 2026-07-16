# Entity Hygiene Guards + Graph Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the dream write path and merge-proposal producers from minting the four junk-entity classes and cross-variant merge pairings observed in the 2026-07-11 review-queue curation, then sweep the live graph with the same rules.

**Architecture:** Pure name-shape classifiers live in `pseudolife_memory/memory/graph_consolidation.py` (next to the existing `junk_name_reason` vocabulary) and are consumed by the three merge-proposal producers, the entity-create choke point (`_resolve_or_create_entity`), and the deep-dream detection sweep. No extractor/prompt changes, no schema migrations. Spec: `docs/superpowers/specs/2026-07-11-entity-hygiene-guards-design.md`.

**Tech Stack:** Python 3.11+, pytest (PG-backed fixtures `pg_conn`/`pg_url` from `tests/conftest.py`), psycopg3, existing daemon REST API for the live-ops tasks.

## Global Constraints

- TDD every code task: failing test first, minimal implementation, suite green.
- Do NOT touch extractor prompts, retrieval, or `memory_graph_relate` (explicit writes stay ungated).
- Match module style: module-level compiled regexes with one-line comments, docstrings citing the motivating incident date.
- Variant/junk guards block **merge proposals and entity creation only** — never link proposals, never explicit human merges.
- Tests that need Postgres use the existing `storage` / `svc` fixtures in `tests/test_graph_write_gating.py` (they take `pg_conn`, `pg_url`).
- Run tests with: `python -m pytest <file>::<test> -v` from the repo root.
- Tasks 11–13 are **main-session live-ops** (deploy, curation, ladder) — do NOT dispatch them to subagents. Task 13 (ladder) is BLOCKED until the user explicitly says go (GPU in use).

---

### Task 1: `variant_tokens` / `variant_conflict` helpers

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (after the `_STATUS_SHARD` constant, ~line 190)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Produces: `variant_tokens(name: str) -> frozenset[str]` and `variant_conflict(a: str, b: str) -> bool`, importable as `from pseudolife_memory.memory.graph_consolidation import variant_conflict`. Tasks 4, 5, 6 consume `variant_conflict`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_graph_write_gating.py` (top imports already include `junk_name_reason` from `graph_consolidation`; extend that import line):

```python
from pseudolife_memory.memory.graph_consolidation import (
    junk_name_reason, variant_tokens, variant_conflict)


def test_variant_tokens_extract_size_quant_version():
    assert variant_tokens("Gemma 4 E4B") == frozenset({"e4b"})
    toks = variant_tokens("gemma-4-26B_q4_0-it.gguf")
    assert "26b" in toks and "q4-0" in toks
    assert variant_tokens("pseudolife-daemon:0.2.0") == frozenset({"0.2.0"})
    assert variant_tokens("plain name") == frozenset()


def test_variant_conflict_blocks_cross_model_pairs():
    # the 9 merge proposals hand-rejected on 2026-07-11
    assert variant_conflict("Gemma-4-E4B-QAT (UD-Q4_K_XL)",
                            "gemma-4-E2B-it-qat-UD-Q4_K_XL")
    assert variant_conflict("gemma-E4B Q4_K_M", "Gemma-4-E4B-QAT (UD-Q4_K_XL)")
    assert variant_conflict("gemma-4-26B", "Gemma 4 E4B")
    assert variant_conflict("Qwen3.5-4B", "Qwen3.6-27B")


def test_variant_conflict_allows_same_or_absent_variants():
    assert not variant_conflict("Gemma 4 E4B", "gemma-4-E4B-it base")
    assert not variant_conflict("update.ps1", "ops/update.ps1")
    assert not variant_conflict("Sonnet shim", "evals/sonnet_shim.py")
    # underscore vs hyphen quant forms are the SAME token (norm_name folds _ to -)
    assert not variant_conflict("UD-Q4_K_XL quant", "ud-q4-k-xl quant")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graph_write_gating.py::test_variant_tokens_extract_size_quant_version -v`
Expected: FAIL with `ImportError: cannot import name 'variant_tokens'`

- [ ] **Step 3: Implement the helpers**

In `pseudolife_memory/memory/graph_consolidation.py`, directly after the `_STATUS_SHARD` / `_SENTENCE_TOKENS` block:

```python
# Variant tokens: size / quant / dotted-version markers whose DIFFERENCE means
# two names denote different artifacts (E4B vs E2B, Q4_K_M vs Q4_K_XL) even when
# every other token matches (2026-07-11 curation: 9 such merge proposals
# hand-rejected). "_"/"-" are interchangeable inside tokens (norm_name folds
# both), so custom boundaries treat any non-alphanumeric as a separator.
_VB = r"(?<![A-Za-z0-9])"
_VE = r"(?![A-Za-z0-9])"
_VARIANT_PATTERNS = (
    re.compile(_VB + r"E\d+B" + _VE, re.IGNORECASE),                  # E2B / E4B
    re.compile(_VB + r"\d+(?:\.\d+)?[MK]?B" + _VE, re.IGNORECASE),    # 26B / 4B
    re.compile(_VB + r"Q\d(?:[_-]K)?(?:[_-](?:XS|S|M|L|XL))?" + _VE,  # Q4_K_XL
               re.IGNORECASE),
    re.compile(_VB + r"q\d[_-]\d" + _VE),                             # q4_0
    re.compile(_VB + r"UD[_-]Q[A-Za-z0-9_-]*" + _VE, re.IGNORECASE),  # UD-Q4_K_XL
    re.compile(r"\d+\.\d+(?:\.\d+)*"),                                # 0.2.0 / 3.6
)


def variant_tokens(name: str) -> frozenset[str]:
    """Size / quant / dotted-version markers in ``name``, casefolded with
    ``_`` folded to ``-`` so display and canonical forms compare equal."""
    out: set[str] = set()
    for pat in _VARIANT_PATTERNS:
        for m in pat.finditer(str(name)):
            out.add(m.group(0).casefold().replace("_", "-"))
    return frozenset(out)


def variant_conflict(a: str, b: str) -> bool:
    """True when BOTH names carry variant tokens and the sets differ — such a
    pair is never a merge candidate (it may still be link-related, e.g. a
    quant of a model). Absent-on-either-side never conflicts."""
    ta, tb = variant_tokens(a), variant_tokens(b)
    return bool(ta) and bool(tb) and ta != tb
```

Note `QAT` is deliberately NOT a variant token — it appeared on both sides of a legitimate accepted merge (mp#102).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_graph_write_gating.py -k variant -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_write_gating.py
git commit -m "feat(graph): variant_tokens/variant_conflict — size/quant/version merge guard"
```

---

### Task 2: `metric-reading` + `list-artifact` junk classes

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (`junk_name_reason` ~line 206, `junk_entities` ~line 293, constants block ~line 190)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `junk_name_reason` returns `"metric-reading"` / `"list-artifact"`; `junk_entities` emits the same reasons. The write path (service.py:1652) already calls `junk_name_reason`, so the dream gate picks these up with no service change.

- [ ] **Step 1: Write the failing tests**

```python
def test_junk_name_reason_blocks_metric_readings_and_lists():
    # 2026-07-11 curation classes: metric readings and captured enumerations
    assert junk_name_reason("stale 0.8") == "metric-reading"
    assert junk_name_reason("stale 0.0") == "metric-reading"
    assert junk_name_reason("stale_leak 0.7-0.8") == "metric-reading"
    assert junk_name_reason("data/, ops/.env, *.pt") == "list-artifact"


def test_junk_name_reason_spares_metric_and_list_near_misses():
    assert junk_name_reason("CUDA Toolkit 13.1") is None        # uppercase token
    assert junk_name_reason("Gemma 4 E4B") is None              # non-decimal tail
    assert junk_name_reason("User (jdoe, jdoe@example.com)") is None
    assert junk_name_reason("8-band continuum") is None


def test_junk_entities_flags_metric_readings_and_lists():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    ents = [{"id": 1, "display": "stale 0.8"},
            {"id": 2, "display": "data/, ops/.env, *.pt"}]
    out = junk_entities(ents, [], max_degree=1)
    assert {(j["display"], j["reason"]) for j in out} == {
        ("stale 0.8", "metric-reading"),
        ("data/, ops/.env, *.pt", "list-artifact")}
    # list-artifact is degree-agnostic (like concat-artifact); metric-reading
    # respects the degree cap
    out2 = junk_entities(ents, [], max_degree=-1)
    assert [j["reason"] for j in out2] == ["list-artifact"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graph_write_gating.py -k "metric or list" -v`
Expected: 3 FAIL (`junk_name_reason` returns None / reasons missing)

- [ ] **Step 3: Implement**

Constants (with the other regex constants):

```python
_DECIMAL_OR_RANGE = re.compile(r"^\d+\.\d+(?:-\d+(?:\.\d+)?)?$")  # 0.8 / 0.7-0.8
_LOWER_TOKEN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _is_metric_reading(name: str) -> bool:
    """2-3 tokens, decimal/decimal-range tail, all other tokens lowercase — a
    metric READING ("stale 0.8"), not an entity. Any uppercase exempts
    ("CUDA Toolkit 13.1"); accepted trade-off: lowercase "python 3.12"-style
    names are blocked (version belongs in a fact, not the entity name)."""
    toks = str(name).split()
    if not 2 <= len(toks) <= 3 or not _DECIMAL_OR_RANGE.match(toks[-1]):
        return False
    return all(_LOWER_TOKEN.match(t) for t in toks[:-1])


def _split_outside_parens(s: str) -> list[str]:
    parts: list[str] = []
    depth, cur = 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def _is_list_artifact(name: str) -> bool:
    """>=2 non-empty comma-separated segments OUTSIDE parentheses — a captured
    enumeration ("data/, ops/.env, *.pt"), not an entity. A parenthesized
    comma ("User (jdoe, a@b)") does not count."""
    return sum(1 for p in _split_outside_parens(str(name)) if p) >= 2
```

In `junk_name_reason`, after the `_STATUS_SHARD` check and before the `_SENTENCE_TOKENS` check:

```python
    if _is_metric_reading(d):
        return "metric-reading"
    if _is_list_artifact(d):
        return "list-artifact"
```

In `junk_entities`: add a degree-agnostic `list-artifact` branch mirroring the existing `concat-artifact` block (before the `deg.get(...) > max_degree` continue), and a `metric-reading` case inside the degree-capped chain:

```python
        if _is_list_artifact(d):
            out.append({"entity_id": e["id"], "display": e["display"],
                        "reason": "list-artifact"})  # degree-agnostic
            continue
```

and in the degree-capped `elif` chain (after `_JUNK_STOPWORDS`):

```python
        elif _is_metric_reading(d):
            reason = "metric-reading"
```

- [ ] **Step 4: Run the file's full tests**

Run: `python -m pytest tests/test_graph_write_gating.py -v`
Expected: all PASS (including the pre-existing junk-class tests — no regressions)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_write_gating.py
git commit -m "feat(graph): metric-reading + list-artifact junk classes (write gate + detection)"
```

---

### Task 3: `compound-artifact` detection (detection-only, never a write drop)

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` (`junk_entities`), `pseudolife_memory/service.py:4025-4027` (deep-dream call site)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Consumes: `norm_name` (already imported in the module).
- Produces: `junk_entities(entities, edges, *, max_degree=1, known_norms: frozenset[str] | None = None)` — new optional kwarg; reason `"compound-artifact"`. Deep dream (service.py:4027) passes `known_norms` built from all canonicals + aliases.

- [ ] **Step 1: Write the failing test**

```python
def test_junk_entities_flags_resolvable_compounds_only():
    from pseudolife_memory.memory.graph_consolidation import junk_entities
    ents = [{"id": 1, "display": "memory_lesson_search/world_search"},
            {"id": 2, "display": "pg+extractor"},
            {"id": 3, "display": "ops/backup.ps1"},       # extension-exempt
            {"id": 4, "display": "C++"}]                  # empty right side
    known = frozenset({"memory-lesson-search", "world-search", "pg",
                       "extractor", "ops", "backup-ps1"})
    out = junk_entities(ents, [], max_degree=1, known_norms=known)
    reasons = {j["display"]: j["reason"] for j in out}
    assert reasons.get("memory_lesson_search/world_search") == "compound-artifact"
    assert reasons.get("pg+extractor") == "compound-artifact"
    assert "ops/backup.ps1" not in reasons
    assert "C++" not in reasons
    # without known_norms (default) nothing is flagged as compound
    out2 = junk_entities(ents, [], max_degree=1)
    assert all(j["reason"] != "compound-artifact" for j in out2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_write_gating.py::test_junk_entities_flags_resolvable_compounds_only -v`
Expected: FAIL with `TypeError: junk_entities() got an unexpected keyword argument 'known_norms'`

- [ ] **Step 3: Implement**

Constants + helper in `graph_consolidation.py`:

```python
_COMPOUND_SEP = re.compile(r"[+/]")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,4}$")


def _compound_halves(name: str) -> tuple[str, str] | None:
    """Split at the FIRST ``/`` or ``+``; both halves must be non-empty and
    carry alphanumeric content, and neither may end in a dot-extension (file
    paths are exempt: ``ops/backup.ps1``). Detection-only feeder — a compound
    is junk-PROPOSED, never write-dropped (2026-07-11: ``pg+extractor``)."""
    s = str(name)
    m = _COMPOUND_SEP.search(s)
    if not m:
        return None
    a, b = s[:m.start()].strip(), s[m.end():].strip()
    if not a or not b:
        return None
    if not re.search(r"[A-Za-z0-9]", a) or not re.search(r"[A-Za-z0-9]", b):
        return None
    if _FILE_EXTENSION.search(a) or _FILE_EXTENSION.search(b):
        return None
    return a, b
```

`junk_entities` signature change and check (place the block right after the existing degree-agnostic `concat-artifact` block):

```python
def junk_entities(entities: list[dict], edges: list[dict], *,
                  max_degree: int = 1,
                  known_norms: frozenset[str] | None = None) -> list[dict]:
```

```python
        if known_norms:
            halves = _compound_halves(d)
            if halves:
                na, nb = norm_name(halves[0]), norm_name(halves[1])
                nd = norm_name(d)
                if (na and nb and na != nb and na != nd and nb != nd
                        and na in known_norms and nb in known_norms):
                    out.append({"entity_id": e["id"], "display": e["display"],
                                "reason": "compound-artifact"})  # degree-agnostic
                    continue
```

Call site — in the deep dream (service.py, the method around line 4025), the graph dict is the local `g = self._storage.load_graph()` and `entities, edges = g["entities"], g["edges"]` already exist. Replace the existing `junk = gc.junk_entities(entities, edges, max_degree=cfg.junk_max_degree)` line with:

```python
        from pseudolife_memory.graph import norm_name as _nn
        known_norms = frozenset(
            {e["canonical"] for e in entities}
            | {_nn(a) for als in g["aliases"].values() for a in als})
        junk = gc.junk_entities(entities, edges, max_degree=cfg.junk_max_degree,
                                known_norms=known_norms)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_graph_write_gating.py tests/test_deep_dream.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py pseudolife_memory/service.py tests/test_graph_write_gating.py
git commit -m "feat(graph): compound-artifact junk detection (both halves resolve; detection-only)"
```

---

### Task 4: variant block in `near_duplicate_names` (write-dedup)

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py:34-66`
- Test: `tests/test_graph_review.py`

**Interfaces:**
- Consumes: `variant_conflict` (Task 1). Import INSIDE the function (the module already does function-level `from pseudolife_memory.graph import norm_name` at ~line 45 — follow that pattern to avoid import cycles).
- Produces: unchanged signature; variant-conflicted entities are skipped.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_graph_review.py`:

```python
def test_near_duplicate_names_blocks_variant_conflicts():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 7, "canonical": "gemma-4-e2b-it-qat-ud-q4-k-xl",
                 "display": "gemma-4-E2B-it-qat-UD-Q4_K_XL", "aliases": []}]
    hits = near_duplicate_names("gemma-4-E4B-it-qat-UD-Q4_K_XL", existing,
                                min_jaccard=0.3)
    assert hits == []


def test_near_duplicate_names_still_matches_same_variant():
    from pseudolife_memory.memory.graph_review import near_duplicate_names
    existing = [{"id": 7, "canonical": "ops-update-ps1",
                 "display": "ops/update.ps1", "aliases": []}]
    hits = near_duplicate_names("update.ps1", existing, min_jaccard=0.3)
    assert [h["entity_id"] for h in hits] == [7]
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `python -m pytest tests/test_graph_review.py -k near_duplicate_names_blocks -v`
Expected: FAIL (a hit is returned — token Jaccard is high)

- [ ] **Step 3: Implement**

In `near_duplicate_names`, right after the `dismissed` check inside the loop:

```python
        from pseudolife_memory.memory.graph_consolidation import variant_conflict
        if (variant_conflict(name, e.get("display") or "")
                or variant_conflict(name, e.get("canonical") or "")):
            continue          # size/quant/version mismatch: never a merge
```

(Move the import to the top of the function next to the existing `norm_name` function-level import — one import, not per-iteration.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_graph_review.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py tests/test_graph_review.py
git commit -m "feat(graph): write-dedup skips variant-conflicted names (E4B vs E2B etc.)"
```

---

### Task 5: variant block in the dream-alias post-pass

**Files:**
- Modify: `pseudolife_memory/service.py` `_propose_dream_alias_candidates` (~line 2210, inside the scoring loop)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Consumes: `variant_conflict` (Task 1); test helpers `_ClaimStub`, `_drain`, `_alias_props` already defined in the test file (~line 309).

- [ ] **Step 1: Write the failing test**

Add next to the existing dream-alias tests in `tests/test_graph_write_gating.py`:

```python
def test_dream_alias_candidate_blocks_variant_conflict(svc):
    """E4B vs E2B names embed nearly identically but denote different models —
    the alias post-pass must not file a merge proposal for them."""
    svc.cortex_write("Gemma 4 E2B extractor sidecar", "version", "e2b",
                     support="user")
    svc.store("sidecar swap note", source="t")
    _drain(svc, _ClaimStub("Gemma 4 E4B extractor sidecar"))
    assert _alias_props(svc) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_write_gating.py::test_dream_alias_candidate_blocks_variant_conflict -v`
Expected: FAIL (one `dream-alias:` proposal is filed)

- [ ] **Step 3: Implement**

In `_propose_dream_alias_candidates`, after the `if pair[0] == pair[1] or pair in dismissed: continue` line:

```python
                    if variant_conflict(disp, target):
                        continue    # size/quant/version mismatch: never a merge
```

with `from pseudolife_memory.memory.graph_consolidation import variant_conflict` added inside the method next to its other local imports.

- [ ] **Step 4: Run the alias tests**

Run: `python -m pytest tests/test_graph_write_gating.py -k alias -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_graph_write_gating.py
git commit -m "feat(dream): alias post-pass skips variant-conflicted pairs"
```

---

### Task 6: variant block in deep-dream `partition_candidates`

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py` `partition_candidates` (~line 275)
- Test: `tests/test_deep_dream.py`

**Interfaces:**
- Consumes: `variant_conflict` (same module).
- Produces: variant-conflicted pairs fall through to the LINK candidate list (they may still be related — quant-of — just never a merge).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deep_dream.py`:

```python
def test_partition_candidates_variant_conflict_stays_link():
    from pseudolife_memory.memory.graph_consolidation import partition_candidates
    pairs = [{"src_id": 1, "dst_id": 2, "src": "gemma-E4B Q4_K_M",
              "dst": "gemma-E4B", "similarity": 0.99}]
    ents = [{"id": 1, "display": "gemma-E4B Q4_K_M"},
            {"id": 2, "display": "gemma-E4B"}]
    merges, links = partition_candidates(pairs, ents, [])
    assert merges == [] and len(links) == 1
    # control: same-variant containment still partitions as a merge
    pairs2 = [{"src_id": 1, "dst_id": 2, "src": "update.ps1",
               "dst": "ops/update.ps1", "similarity": 0.99}]
    ents2 = [{"id": 1, "display": "update.ps1"},
             {"id": 2, "display": "ops/update.ps1"}]
    merges2, links2 = partition_candidates(pairs2, ents2, [])
    assert len(merges2) == 1 and links2 == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_deep_dream.py::test_partition_candidates_variant_conflict_stays_link -v`
Expected: FAIL (the E4B/Q4_K_M pair token-subsets into a merge)

- [ ] **Step 3: Implement**

In `partition_candidates`, extend the reason gate:

```python
        reason = (_name_contains(p["src"], p["dst"])
                  if float(p.get("similarity", 0.0)) >= merge_min_similarity
                  and not variant_conflict(p["src"], p["dst"])
                  else None)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_deep_dream.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_deep_dream.py
git commit -m "feat(graph): deep-dream partition demotes variant-conflicted pairs to links"
```

---

### Task 7: slot-key folding at entity creation

**Files:**
- Modify: `pseudolife_memory/storage/postgres.py` (new query helper, next to the other fact/entity lookups), `pseudolife_memory/service.py` `_resolve_or_create_entity` (~line 3309)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Produces: `PostgresStorage.find_fact_slot_entity(key_norm: str) -> str | None`; `_resolve_or_create_entity` folds slot-key-shaped names to the slot's owner entity on the create-miss path.
- Norm contract: both `graph.norm_name` and cortex `_norm_key` fold `.`/`_`/spaces to `-`, so `norm_name("X.attr") == entity_norm + "-" + attribute_norm` — the lookup joins with a **hyphen**, not a dot.

- [ ] **Step 1: Write the failing tests**

```python
def test_find_fact_slot_entity(storage):
    storage.conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm,"
        " value, status, confidence, asserted_at, last_confirmed)"
        " VALUES (%s,%s,%s,%s,%s,'current',0.9,1.0,1.0)",
        ("2026-07-11-known-facts-window", "delivered-components",
         "2026-07-11-known-facts-window", "delivered-components", "x"))
    assert storage.find_fact_slot_entity(
        "2026-07-11-known-facts-window-delivered-components"
    ) == "2026-07-11-known-facts-window"
    assert storage.find_fact_slot_entity("no-such-slot") is None


def test_resolve_or_create_folds_slot_key_to_owner(svc):
    svc.cortex_write("2026-07-11-known-facts-window", "delivered-components",
                     "gate+config+tests", support="user")
    svc.stats()
    with svc._lock:
        e = svc._resolve_or_create_entity(
            "2026-07-11-known-facts-window.delivered-components")
    assert e["display"] == "2026-07-11-known-facts-window"
    # non-slot dotted names are untouched
    with svc._lock:
        e2 = svc._resolve_or_create_entity("psycopg/transaction.py")
    assert e2["display"] == "psycopg/transaction.py"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_graph_write_gating.py -k slot -v`
Expected: FAIL with `AttributeError: ... has no attribute 'find_fact_slot_entity'`

- [ ] **Step 3: Implement**

`postgres.py` (near the other single-row lookups):

```python
    def find_fact_slot_entity(self, key_norm: str) -> str | None:
        """Display entity of a CURRENT fact whose slot key — entity_norm and
        attribute_norm hyphen-joined, matching graph.norm_name's separator
        folding — equals ``key_norm``. Small table + create-miss-only calls,
        so the unindexed concat scan is fine."""
        row = self.conn.execute(
            "SELECT entity FROM facts WHERE status = 'current' "
            "AND entity_norm || '-' || attribute_norm = %s LIMIT 1",
            (key_norm,)).fetchone()
        return row[0] if row else None
```

`service.py` `_resolve_or_create_entity`, between the `found is not None` block and `eid = st.ensure_entity(...)`:

```python
        # Slot-key fold (2026-07-11): a dreamed name that IS an existing fact
        # slot key ("entity.attribute") resolves to the slot's owner entity
        # instead of minting a node named after the whole key. Exact-match
        # only; recursion terminates because the owner's norm differs from n
        # and cannot itself be a slot key (keys concat two non-empty norms).
        slot_owner = st.find_fact_slot_entity(n)
        if slot_owner is not None and norm_name(slot_owner) != n:
            logger.debug("entity folded to slot owner (slot-key): %r -> %r",
                         name, slot_owner)
            return self._resolve_or_create_entity(slot_owner, etype=etype)
```

(`norm_name` is already imported at the top of the method.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_graph_write_gating.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/storage/postgres.py pseudolife_memory/service.py tests/test_graph_write_gating.py
git commit -m "feat(graph): fold slot-key-shaped entity names to their fact's owner entity"
```

---

### Task 8: cross-project `related-to` bar

**Files:**
- Modify: `pseudolife_memory/service.py` cross-project gate (~line 1667-1676)
- Test: `tests/test_graph_write_gating.py`

**Interfaces:**
- Consumes: the local `resolved` variable from `G.resolve_relation` (None ⇔ `related-to` fallback) already in scope at the gate.

- [ ] **Step 1: Write the failing test**

```python
def test_cross_project_untyped_relation_dropped(svc):
    svc.stats()
    with svc._lock:
        svc._resolve_or_create_entity("alpha-tool")
        svc._resolve_or_create_entity("beta-tool")
    svc.graph_assign_scope("alpha-tool", "proj-a")
    svc.graph_assign_scope("beta-tool", "proj-b")

    def _cross_count():
        return svc._storage.conn.execute(
            "SELECT count(*) FROM edge_proposals "
            "WHERE source = 'dream-cross-project'").fetchone()[0]

    # untyped fallback across disjoint scopes: dropped, no proposal
    n = svc._link_dream_relations([
        {"src": "alpha-tool", "relation": "correlates-with", "dst": "beta-tool"}])
    assert n == 0 and _cross_count() == 0
    # a TYPED relation across disjoint scopes still files a proposal
    n2 = svc._link_dream_relations([
        {"src": "alpha-tool", "relation": "uses", "dst": "beta-tool"}])
    assert n2 == 0 and _cross_count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_write_gating.py::test_cross_project_untyped_relation_dropped -v`
Expected: FAIL on `_cross_count() == 0` (the untyped pair currently files a proposal)

- [ ] **Step 3: Implement**

At the cross-project gate, inside the `if ss and ds and not (ss & ds):` branch, before `insert_proposal`:

```python
            if ss and ds and not (ss & ds):
                if resolved is None:
                    # Untyped fallback across disjoint projects carries no
                    # information (2026-07-11: 4/4 such proposals rejected).
                    logger.debug("dream relation dropped (cross-project-"
                                 "untyped): %r -> %r", raw_src, raw_dst)
                    continue
                self._storage.insert_proposal(
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_graph_write_gating.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_graph_write_gating.py
git commit -m "feat(dream): cross-project proposals require a typed relation"
```

---

### Task 9: `decided_by` passthrough on REST verdict routes

**Files:**
- Modify: `pseudolife_memory/web/routes.py:194-196`, `pseudolife_memory/web/fixtures.py` (the two fixture methods)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Service.graph_accept_entity_merge(id, *, decided_by)` and `graph_reject_entity_proposal(id, *, decided_by)` (existing signatures). `graph_accept_entity_junk` takes no `decided_by` (service.py:4270) — leave its route unchanged.
- Produces: routes accept optional body field `decided_by` ∈ {"human", "agent"}, default "human".

- [ ] **Step 1: Write the failing test**

First read the harness convention: `sed -n 320,340p tests/test_web.py` — note the fixture name used by the test that dispatches `POST /api/graph/accept-entity-merge` (line ~336) and give the new test the same fixture parameter. Then add:

```python
def test_graph_entity_verdicts_pass_decided_by(<same-fixture-as-line-336>):
    r = <same-fixture-as-line-336>
    out = r.dispatch("POST", "/api/graph/accept-entity-merge", {},
                     {"id": 1, "decided_by": "agent"})
    assert out["decided_by"] == "agent"
    out2 = r.dispatch("POST", "/api/graph/reject-entity-proposal", {},
                      {"id": 3, "decided_by": "bogus"})
    assert out2["decided_by"] == "human"    # invalid values fall back
```

(Replace `<same-fixture-as-line-336>` with the literal fixture name you read — it is a mechanical substitution, the dispatch calls are exact.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py::test_graph_entity_verdicts_pass_decided_by -v`
Expected: FAIL with `KeyError: 'decided_by'`

- [ ] **Step 3: Implement**

`routes.py` — module-level helper + the two route lambdas:

```python
def _decided_by(body: dict) -> str:
    v = body.get("decided_by")
    return v if v in ("human", "agent") else "human"
```

```python
        p("/api/graph/accept-entity-merge",
          lambda q, b: svc.graph_accept_entity_merge(
              b["id"], decided_by=_decided_by(b)))
        p("/api/graph/reject-entity-proposal",
          lambda q, b: svc.graph_reject_entity_proposal(
              b["id"], decided_by=_decided_by(b)))
```

`fixtures.py` — extend the two stub methods to accept and echo the kwarg:

```python
    def graph_accept_entity_merge(self, proposal_id, *, decided_by="human"):
        return {"accepted": True, "id": int(proposal_id),
                "decided_by": decided_by}

    def graph_reject_entity_proposal(self, proposal_id, *, decided_by="human"):
        return {"rejected": True, "id": int(proposal_id),
                "decided_by": decided_by}
```

(Keep any other keys the current fixture returns — check the existing bodies first and only ADD `decided_by`. If the real service methods' return dicts don't include `decided_by`, that is fine — the fixture echo is what the routes test asserts; do NOT change the service returns.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: all PASS (the pre-existing line-336 test must still pass — defaults unchanged)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_web.py
git commit -m "feat(web): decided_by passthrough on entity-merge/reject verdict routes"
```

---

### Task 10: full suite, CHANGELOG, docs

**Files:**
- Modify: `CHANGELOG.md` (Unreleased section), `README.md` (only if it enumerates junk classes — grep first)

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest -q`
Expected: all PASS (was 763+ green before this work; zero failures)

- [ ] **Step 2: CHANGELOG entry**

Add under the Unreleased/newest heading, matching the file's existing bullet style:

```markdown
- Entity hygiene guards (2026-07-11 curation follow-up): slot-key names fold
  to their fact's owner entity; new junk classes `metric-reading`,
  `list-artifact` (write gate + detection) and `compound-artifact`
  (detection-only); variant-token (size/quant/version) conflicts hard-block
  merge proposals in write-dedup, the dream-alias post-pass, and deep-dream
  partition; cross-project dream proposals now require a typed relation;
  REST entity-verdict routes accept `decided_by`.
```

- [ ] **Step 3: README check**

Run: `grep -n "concat-artifact\|junk class" README.md`
If the README documents the junk classes, add the two new write-gate classes in the same list style; otherwise skip.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: changelog + README for entity hygiene guards"
```

---

### Task 11 (MAIN SESSION, live-ops): backup-first deploy

Not a subagent task. Steps:

- [ ] Push master; run `ops/backup.ps1`; deploy via `ops/update.ps1 -Tag pre-entity-hygiene` (backup + rollback tag + daemon-only rebuild + health check — the script does all four).
- [ ] Verify: `curl -s http://127.0.0.1:8765/health` healthy; `memory_stats` responds.

### Task 12 (MAIN SESSION, live-ops): graph curation runbook

Not a subagent task; requires Task 11 deployed. All REST verdicts include `"decided_by": "agent"`. Follow spec §6 exactly:

- [ ] **pg+extractor retirement:** re-point `pseudolife-daemon uses/depends-on pg+extractor` → same relations to `pg service` and `pseudolife-extractor` (via `memory_graph_relate`); `POST /api/graph/unrelate` the `Cortex Console v2 Phase 0 uses pg+extractor` edge; `POST /api/graph/delete-entity {"entity": "pg+extractor"}` (drops the generic `extractor` alias with it).
- [ ] **GND split:** inspect `memory_graph(entity="GND")`; if all provenance is GND Share (expected), rename via the Console/SQL display update to `GND Share`; any Enshrouded-server facts move to a new `GND (Enshrouded server)` entity first.
- [ ] **Scope pass:** for each unattributed entity (the `unattributed` review finding), majority-project from its `entity_sources` rows → `POST /api/graph/assign-scope`; skip when no clear majority.
- [ ] **Periphery sweep:** `memory_dream(action="deep")` preview, then `apply=true` (snapshot!); batch-judge new junk/merge proposals — accept junk for `metric-reading`/`list-artifact`/`concat-artifact`/`compound-artifact`, dismiss variant pairs, leave judgment calls pending in Atlas.
- [ ] Record outcomes in Pseudolife memory (`memory_store` + `memory_outcome`).

### Task 13 (BLOCKED — user go required): extraction-quality ladder re-run

Standing rule: re-run the ladder after ANY dream-write-path change. **GPU is in use — do not start until the user explicitly says go.** When cleared: run the ladder screen per `evals/` docs and compare stale-leak/capture against the 2026-07-03 baseline; regression → rollback tag `pre-entity-hygiene` is the undo.
