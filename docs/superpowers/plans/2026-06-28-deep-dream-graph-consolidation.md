# Deep Dream — Graph Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manual, session-driven "deep dream" that consolidates the knowledge graph — deterministic tiered-autonomy self-clean plus semantic cross-session link discovery — without ever auto-polluting the live retrieval path.

**Architecture:** A new pure module (`graph_consolidation.py`) computes the self-clean decisions (re-score / hard-type-violation / exact-duplicate) and the discovery candidates (entity context centroids → near-pair cosine → scope/edge/dup filters). A `service.deep_dream(apply=...)` orchestration runs Steps A (self-clean) + B (candidate gen) inside the daemon with dry-run/apply safety. Discovered links are proposed by in-session Opus subagents (Step C), gated by the existing `edge_confidence`, and stored in a new `edge_proposals` table — never in `edges` — surfaced as a `graph_review` finding and promoted only on human confirm via two new Atlas mutations.

**Tech Stack:** Python 3.11, Postgres 16 + pgvector, psycopg, numpy, networkx; pytest (PG-backed integration tests skip without Postgres). MCP over HTTP (daemon), web routes (Atlas), Gemma/Opus extractor seam.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-28-deep-dream-graph-consolidation-design.md` is authoritative.
- **Graph-only scope.** No cortex reconciliation, no MIRAS changes, no incremental-dream changes.
- **Discovered links are proposal-only, always** — written to `edge_proposals`, never to `edges`, regardless of any autonomy setting.
- **Supersede-not-delete** for all destructive self-clean actions (reversible, audited). Entity merge uses the existing `merge_entity` op.
- **Auto-apply only the provably-safe class:** hard type-violations (both endpoints confidently typed via `infer_type` AND violating `TYPE_CONSTRAINTS`) + exact-duplicate entities (token-set Jaccard `== 1.0`). Everything softer stays in the live Atlas review queue.
- **Hard-type-violation detection is structural** via `is_hard_type_violation()`, never a float compare against `0.175` or `min_relation_confidence`.
- **No new DB connection.** Deep dream is a `service` method under the daemon's existing lock — never a standalone `psycopg`/`PostgresStorage()` construction (the backfill did that *because* it ran outside the daemon).
- **Backup-first on apply is a runbook step** (`ops/backup.ps1` on the Windows host), NOT shelled out from the containerized daemon.
- **Config defaults (verbatim):** `min_similarity = 0.55`, `top_k_candidates = 50`, `max_context_snippets = 3`, `auto_apply_safe = True`.
- **Closed-vocabulary relations:** proposals resolve through `resolve_relation`; unknown → `related-to`. Reuse `edge_confidence` as the gate on every proposal.
- **Branch-first.** Create a feature branch before Task 1; do not implement on `master`.
- **Schema bump:** `SCHEMA_META_VERSION` 16 → 17 (additive `edge_proposals` table). `tests/test_*` that assert the version must be updated in the same task.

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
cd /c/Users/<user>/ClaudeCode/Pseudolife-MCP
git checkout master && git pull
git checkout -b feat/deep-dream-graph-consolidation
```

No test. Commit nothing yet.

---

### Task 1: `is_hard_type_violation` shared predicate

**Files:**
- Modify: `pseudolife_memory/memory/relation_quality.py` (append after `edge_confidence`)
- Test: `tests/test_relation_quality.py` (append)

**Interfaces:**
- Consumes: existing `infer_type`, `TYPE_CONSTRAINTS` in the same module.
- Produces: `is_hard_type_violation(src: str, relation: str, dst: str) -> bool` — True iff both endpoints are confidently typed (`infer_type` not None) AND the relation has a `TYPE_CONSTRAINTS` entry AND the pair violates it. This is the exact condition under which `edge_confidence` applies its `0.25` penalty.

- [ ] **Step 1: Write the failing tests**

In `tests/test_relation_quality.py`:

```python
from pseudolife_memory.memory.relation_quality import is_hard_type_violation


def test_hard_violation_when_both_typed_and_incompatible():
    # user=person, windows 11=runtime; runs-on src must be service/process/... not person
    assert is_hard_type_violation("user", "runs-on", "windows 11") is True


def test_no_violation_when_compatible():
    # daemon=service, docker=runtime: runs-on service->runtime is allowed
    assert is_hard_type_violation("daemon", "runs-on", "docker") is False


def test_no_violation_when_an_endpoint_is_untyped():
    # an arbitrary junk endpoint is None-typed -> neutral, never a hard violation
    assert is_hard_type_violation("zxqw blob", "runs-on", "docker") is False


def test_no_violation_for_unconstrained_relation():
    # related-to has no TYPE_CONSTRAINTS entry -> never a hard violation
    assert is_hard_type_violation("user", "related-to", "windows 11") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_relation_quality.py -k hard_violation -v`
Expected: FAIL with `ImportError: cannot import name 'is_hard_type_violation'`.

- [ ] **Step 3: Implement the predicate**

Append to `pseudolife_memory/memory/relation_quality.py`:

```python
def is_hard_type_violation(src: str, relation: str, dst: str) -> bool:
    """True iff BOTH endpoints are confidently typed AND the pair violates this
    relation's TYPE_CONSTRAINTS — the exact structural condition under which
    edge_confidence() applies its 0.25 penalty. Unknown types are never a
    violation (neutral). The single source of truth for 'this edge is junk we
    can auto-supersede', shared by edge_confidence and the deep-dream self-clean."""
    constraint = TYPE_CONSTRAINTS.get(relation)
    if not constraint:
        return False
    st, dt = infer_type(src), infer_type(dst)
    if not (st and dt):
        return False
    src_ok, dst_ok = constraint
    return st not in src_ok or dt not in dst_ok
```

Then refactor `edge_confidence` to reuse it (DRY — keep behavior identical):

```python
def edge_confidence(src: str, relation: str, dst: str) -> float:
    """Deterministic per-edge confidence. 0.70 clean / 0.45 related-to /
    0.175 known type-violation. Unknown types never penalize."""
    base = 0.45 if relation == "related-to" else 0.70
    if is_hard_type_violation(src, relation, dst):
        base *= 0.25
    return round(base, 3)
```

- [ ] **Step 4: Run to verify pass (and no regression on edge_confidence)**

Run: `.venv/Scripts/python -m pytest tests/test_relation_quality.py -v`
Expected: PASS (new tests + all existing `edge_confidence`/`infer_type` tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/relation_quality.py tests/test_relation_quality.py
git commit -m "feat(relation-quality): is_hard_type_violation shared predicate"
```

---

### Task 2: `graph_consolidation` — self-clean classifiers

**Files:**
- Create: `pseudolife_memory/memory/graph_consolidation.py`
- Test: `tests/test_graph_consolidation.py` (create)

**Interfaces:**
- Consumes: `edge_confidence`, `is_hard_type_violation` (Task 1); `graph_review._token_set`; `pseudolife_memory.graph.degree_counts`. Edge dicts have `id, src_id, relation, dst_id, confidence, origin` (from `load_graph`). Entity dicts have `id, canonical, display, etype`.
- Produces:
  - `rescore_edges(edges, entities) -> list[tuple[int, float]]` — `(edge_id, new_conf)` for agent edges whose recomputed `edge_confidence` differs from stored.
  - `hard_violation_edges(edges, entities) -> list[dict]` — the agent edge dicts that are hard type-violations (for auto-supersede).
  - `exact_duplicate_pairs(entities, edges) -> list[tuple[int, int]]` — `(from_id, into_id)`; fold the lower-degree entity into the higher-degree one (tie-break: fold higher id into lower id).

- [ ] **Step 1: Write the failing tests**

`tests/test_graph_consolidation.py`:

```python
from pseudolife_memory.memory import graph_consolidation as gc

ENTS = [
    {"id": 1, "canonical": "daemon", "display": "daemon", "etype": None},
    {"id": 2, "canonical": "docker", "display": "docker", "etype": None},
    {"id": 3, "canonical": "user", "display": "user", "etype": None},
    {"id": 4, "canonical": "windows 11", "display": "Windows 11", "etype": None},
]


def _edge(eid, s, rel, d, conf, origin="agent"):
    return {"id": eid, "src_id": s, "relation": rel, "dst_id": d,
            "confidence": conf, "origin": origin}


def test_rescore_only_changes_agent_edges_that_differ():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.6),          # clean -> should become 0.70
        _edge(11, 3, "runs-on", 4, 0.6),          # violation -> should become 0.175
        _edge(12, 1, "related-to", 2, 0.45),      # already correct -> omitted
        _edge(13, 1, "runs-on", 2, 0.6, "user"),  # non-agent -> omitted
    ]
    out = dict(gc.rescore_edges(edges, ENTS))
    assert out == {10: 0.70, 11: 0.175}


def test_hard_violation_edges_flags_only_typed_violations():
    edges = [
        _edge(10, 1, "runs-on", 2, 0.7),   # daemon(service)->docker(runtime): OK
        _edge(11, 3, "runs-on", 4, 0.175), # user(person)->windows(runtime): violation
        _edge(12, 1, "related-to", 4, 0.45),  # unconstrained relation: never a violation
    ]
    ids = [e["id"] for e in gc.hard_violation_edges(edges, ENTS)]
    assert ids == [11]


def test_exact_duplicate_pairs_folds_lower_degree_into_higher():
    ents = [
        {"id": 1, "canonical": "gemma sidecar", "display": "Gemma sidecar", "etype": None},
        {"id": 2, "canonical": "gemma sidecar", "display": "gemma  sidecar", "etype": None},
        {"id": 3, "canonical": "unrelated", "display": "unrelated thing", "etype": None},
    ]
    # entity 1 has an edge (degree 1), entity 2 has none -> fold 2 into 1
    edges = [_edge(10, 1, "related-to", 3, 0.45)]
    assert gc.exact_duplicate_pairs(ents, edges) == [(2, 1)]


def test_exact_duplicate_pairs_ignores_non_identical_token_sets():
    ents = [
        {"id": 1, "canonical": "schema v8", "display": "schema v8", "etype": None},
        {"id": 2, "canonical": "schema 11", "display": "schema 11", "etype": None},
    ]
    assert gc.exact_duplicate_pairs(ents, []) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_graph_consolidation.py -v`
Expected: FAIL with `ModuleNotFoundError: ... graph_consolidation`.

- [ ] **Step 3: Implement the classifiers**

Create `pseudolife_memory/memory/graph_consolidation.py`:

```python
"""Pure graph-consolidation logic for the deep dream (DB-free, unit-testable like
graph_insight.py / graph_review.py). Two halves: deterministic SELF-CLEAN
classifiers (re-score / hard-type-violation / exact-duplicate) and semantic
CANDIDATE generation for cross-session link discovery. The service supplies
edges / entities / entries / embeddings / scope-map and persists the decisions."""
from __future__ import annotations

import numpy as np

from pseudolife_memory.graph import degree_counts
from pseudolife_memory.memory.graph_review import _token_set
from pseudolife_memory.memory.relation_quality import (
    edge_confidence, is_hard_type_violation,
)


def _disp(entities: list[dict]) -> dict[int, str]:
    return {e["id"]: e["display"] for e in entities}


# --- Step A: self-clean classifiers -------------------------------------------

def rescore_edges(edges: list[dict], entities: list[dict]) -> list[tuple[int, float]]:
    """(edge_id, new_conf) for every agent edge whose recomputed edge_confidence
    differs from what's stored. Mirrors ops/backfill_edge_confidence.py, but pure."""
    disp = _disp(entities)
    out: list[tuple[int, float]] = []
    for e in edges:
        if e.get("origin") != "agent":
            continue
        new = edge_confidence(disp.get(e["src_id"], ""), e["relation"],
                              disp.get(e["dst_id"], ""))
        if round(float(e.get("confidence", 0.0)), 3) != new:
            out.append((e["id"], new))
    return out


def hard_violation_edges(edges: list[dict], entities: list[dict]) -> list[dict]:
    """Agent edges that are hard type-violations (both endpoints confidently typed
    AND incompatible) — the auto-supersede bucket."""
    disp = _disp(entities)
    return [e for e in edges
            if e.get("origin") == "agent"
            and is_hard_type_violation(disp.get(e["src_id"], ""), e["relation"],
                                       disp.get(e["dst_id"], ""))]


def exact_duplicate_pairs(entities: list[dict], edges: list[dict]) -> list[tuple[int, int]]:
    """(from_id, into_id) for entity pairs with token-set-IDENTICAL displays
    (Jaccard == 1.0). Fold the lower-degree entity into the higher-degree one
    (preserve the more-connected node); tie-break folds the higher id into the
    lower id (deterministic)."""
    deg = degree_counts(edges)
    toks = [(e["id"], _token_set(e["display"])) for e in entities]
    pairs: list[tuple[int, int]] = []
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a_id, a = toks[i]
            b_id, b = toks[j]
            if not a or not b or a != b:
                continue
            da, db = deg.get(a_id, 0), deg.get(b_id, 0)
            if da > db or (da == db and a_id < b_id):
                into, frm = a_id, b_id
            else:
                into, frm = b_id, a_id
            pairs.append((frm, into))
    pairs.sort()
    return pairs
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_graph_consolidation.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "feat(graph-consolidation): self-clean classifiers (rescore/violations/dups)"
```

---

### Task 3: `graph_consolidation` — candidate generation

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py`
- Test: `tests/test_graph_consolidation.py` (append)

**Interfaces:**
- Consumes: entity dicts (`id, canonical, display`), entry dicts (`id, text, embedding` — `embedding` is a `np.ndarray`), `traces_by_entity: dict[str, list[int]]` (entity canonical → entry ids), `scope_map: dict[int, list[str]]`, edge dicts.
- Produces:
  - `entity_context_vectors(entities, entries, traces_by_entity) -> dict[int, np.ndarray]` — per-entity L2-normalized mean embedding; trace entries primary, token-mention scan fallback, entities with neither are omitted.
  - `candidate_pairs(vectors, edges, entities, scope_map, *, min_similarity=0.55, top_k=50) -> list[dict]` — `[{src_id, dst_id, src, dst, similarity}]`, sorted by similarity desc; drops pairs that already have an edge (either direction), exact-dup pairs, or are in disjoint non-empty scopes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_consolidation.py`:

```python
import numpy as np


def _vec(*xs):
    return np.asarray(xs, dtype=np.float32)


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
    # alpha has a trace to entry 100; beta has none -> mention-scan finds entry 101
    vecs = gc.entity_context_vectors(ents, entries, {"alpha": [100]})
    assert set(vecs) == {1, 2}                 # ghost omitted (no trace, no mention)
    assert np.allclose(vecs[1], _vec(1, 0))    # alpha from its trace entry
    assert np.allclose(vecs[2], _vec(0, 1))    # beta from the mention scan


def test_candidate_pairs_filters_edges_scope_and_threshold():
    ents = [
        {"id": 1, "canonical": "a", "display": "a", "etype": None},
        {"id": 2, "canonical": "b", "display": "b", "etype": None},
        {"id": 3, "canonical": "c", "display": "c", "etype": None},
        {"id": 4, "canonical": "d", "display": "d", "etype": None},
    ]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0), 3: _vec(1, 0), 4: _vec(0, 1)}
    edges = [{"id": 9, "src_id": 1, "relation": "related-to", "dst_id": 3,
              "confidence": 0.45, "origin": "agent"}]
    scope = {1: ["pseudolife"], 2: ["pseudolife"], 3: ["gw2-reshade"], 4: ["pseudolife"]}
    out = gc.candidate_pairs(vectors, edges, ents, scope, min_similarity=0.55, top_k=50)
    pairs = {(c["src_id"], c["dst_id"]) for c in out}
    # 1-2 kept (sim 1.0, same scope, no edge). 1-3 dropped (edge exists).
    # 2-3 dropped (disjoint scope). 1-4 / 2-4 dropped (sim 0 < 0.55).
    assert pairs == {(1, 2)}
    assert out[0]["similarity"] == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_graph_consolidation.py -k "context_vectors or candidate_pairs" -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'entity_context_vectors'`.

- [ ] **Step 3: Implement candidate generation**

Append to `pseudolife_memory/memory/graph_consolidation.py`:

```python
# --- Step B: candidate generation ---------------------------------------------

def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def entity_context_vectors(entities: list[dict], entries: list[dict],
                           traces_by_entity: dict[str, list[int]]) -> dict[int, np.ndarray]:
    """Per-entity context vector = L2-normalized mean of its mentioning entries'
    embeddings. Trace entries are the primary source; entities without traces fall
    back to a token-mention scan; entities with neither are omitted (we don't guess)."""
    by_id = {e["id"]: e for e in entries}
    entry_tokens = [(e["id"], _token_set(e.get("text", ""))) for e in entries]
    out: dict[int, np.ndarray] = {}
    for ent in entities:
        ids = list(traces_by_entity.get(ent["canonical"], []))
        if not ids:
            want = _token_set(ent["display"])
            if want:
                ids = [eid for eid, toks in entry_tokens if want <= toks]
        embs = [by_id[i]["embedding"] for i in ids if i in by_id]
        if not embs:
            continue
        out[ent["id"]] = _l2(np.mean(np.stack(embs), axis=0))
    return out


def candidate_pairs(vectors: dict[int, np.ndarray], edges: list[dict],
                    entities: list[dict], scope_map: dict[int, list[str]], *,
                    min_similarity: float = 0.55, top_k: int = 50) -> list[dict]:
    """Unlinked, scope-coherent, semantically-near entity pairs — the link
    candidates. Drops pairs that already have an edge (either direction), exact
    duplicates (a Step-A merge), or that sit in disjoint non-empty project scopes."""
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
            su, sv = set(scope_map.get(u, [])), set(scope_map.get(v, []))
            if su and sv and not (su & sv):       # disjoint, both attributed
                continue
            sim = float(np.dot(vectors[u], vectors[v]))
            if sim < min_similarity:
                continue
            scored.append({"src_id": u, "dst_id": v, "src": disp.get(u, str(u)),
                           "dst": disp.get(v, str(v)), "similarity": round(sim, 4)})
    scored.sort(key=lambda c: (-c["similarity"], c["src_id"], c["dst_id"]))
    return scored[:top_k]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_graph_consolidation.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "feat(graph-consolidation): semantic candidate generation"
```

---

### Task 4: `edge_proposals` schema + storage

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (add table to schema string; bump `SCHEMA_META_VERSION` 16→17)
- Modify: `pseudolife_memory/storage/postgres.py` (add proposal + traces methods)
- Modify: `tests/test_*` asserting the schema version (find with grep below)
- Test: `tests/test_edge_proposals.py` (create, PG-backed)

**Interfaces:**
- Produces (on `PostgresStorage`):
  - `set_edge_confidence(edge_id: int, confidence: float) -> None`
  - `traces_by_entity_norm() -> dict[str, list[int]]`
  - `insert_proposal(src_id, relation, dst_id, confidence, similarity, rationale, source, now) -> int | None` (ON CONFLICT DO NOTHING → None on duplicate)
  - `pending_proposals() -> list[dict]` (status='pending', joined to entity displays)
  - `get_proposal(proposal_id) -> dict | None`
  - `set_proposal_status(proposal_id, status) -> bool`

- [ ] **Step 1: Add the table + bump the version**

In `pseudolife_memory/storage/schema.py`, after the `edges` CREATE TABLE block (line ~97), add:

```sql
CREATE TABLE IF NOT EXISTS edge_proposals (
  id BIGSERIAL PRIMARY KEY,
  src_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation TEXT NOT NULL REFERENCES relations(name),
  dst_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  similarity REAL,
  rationale TEXT,
  source TEXT NOT NULL DEFAULT 'deep-dream',
  created_at DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  UNIQUE (src_id, relation, dst_id)
);
```

Change `SCHEMA_META_VERSION = 16` to `SCHEMA_META_VERSION = 17` (line 18).

- [ ] **Step 2: Update the version-assertion test(s)**

Run: `git grep -n "SCHEMA_META_VERSION == 16\|== 16\|meta_version" tests/`
For each hit asserting `16`, change to `17`. (Known: `tests/test_*` carries a `test_meta_version_is_current`-style assertion — update it.)

- [ ] **Step 3: Write the failing storage tests**

`tests/test_edge_proposals.py` — the `storage` fixture is copied verbatim from `tests/test_graph.py:110-116` (the canonical PG storage fixture; PG tests skip cleanly without a server because `tests/pg_fixtures.py` does `pytest.importorskip("psycopg")` and `pg_url` skips when no server is reachable):

```python
import time
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)
from pseudolife_memory.storage.postgres import PostgresStorage


@pytest.fixture()
def storage(pg_conn, pg_url):
    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _two_entities(st):
    a = st.ensure_entity("alpha", display="alpha")
    b = st.ensure_entity("beta", display="beta")
    return a, b


def test_insert_then_pending_then_accept(storage):
    a, b = _two_entities(storage)
    pid = storage.insert_proposal(a, "related-to", b, 0.45, 0.91, "why", "deep-dream", time.time())
    assert pid is not None
    pend = storage.pending_proposals()
    assert len(pend) == 1 and pend[0]["src"] == "alpha" and pend[0]["dst"] == "beta"
    assert storage.set_proposal_status(pid, "accepted") is True
    assert storage.pending_proposals() == []


def test_insert_is_idempotent_on_triple(storage):
    a, b = _two_entities(storage)
    first = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    dup = storage.insert_proposal(a, "related-to", b, 0.45, 0.9, "x", "deep-dream", time.time())
    assert first is not None and dup is None


def test_traces_by_entity_norm_returns_dict(storage):
    assert isinstance(storage.traces_by_entity_norm(), dict)
```

> `edge_proposals` does not need adding to `tests/pg_fixtures.py`'s `_ALL_TABLES` truncate list — it has an FK to `entities`, so `TRUNCATE entities ... CASCADE` clears it automatically.

- [ ] **Step 4: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_edge_proposals.py -v`
Expected: FAIL (`AttributeError: ... insert_proposal`) — or SKIP if Postgres is unavailable (then verify against the live test DB per existing convention).

- [ ] **Step 5: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, near `supersede_edge` / `traces_for_slot`:

```python
def set_edge_confidence(self, edge_id: int, confidence: float) -> None:
    self.conn.execute("UPDATE edges SET confidence = %s WHERE id = %s",
                      (float(confidence), edge_id))
    self.conn.commit()

def traces_by_entity_norm(self) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for ent_norm, entry_id in self.conn.execute(
        "SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id"
    ).fetchall():
        out.setdefault(ent_norm, []).append(entry_id)
    return out

def insert_proposal(self, src_id: int, relation: str, dst_id: int,
                    confidence: float, similarity: float | None,
                    rationale: str | None, source: str, now: float) -> int | None:
    row = self.conn.execute(
        "INSERT INTO edge_proposals "
        "(src_id, relation, dst_id, confidence, similarity, rationale, source, created_at, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
        "ON CONFLICT (src_id, relation, dst_id) DO NOTHING RETURNING id",
        (src_id, relation, dst_id, float(confidence),
         similarity, rationale, source, now),
    ).fetchone()
    self.conn.commit()
    return int(row[0]) if row else None

def pending_proposals(self) -> list[dict]:
    cols = ("id", "src_id", "relation", "dst_id", "confidence", "similarity",
            "rationale", "source", "created_at", "status")
    rows = self.conn.execute(
        "SELECT p.id, p.src_id, p.relation, p.dst_id, p.confidence, p.similarity, "
        "       p.rationale, p.source, p.created_at, p.status, s.display, d.display "
        "FROM edge_proposals p "
        "JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id "
        "WHERE p.status = 'pending' ORDER BY p.confidence DESC, p.id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r[:10]))
        d["src"], d["dst"] = r[10], r[11]
        out.append(d)
    return out

def get_proposal(self, proposal_id: int) -> dict | None:
    cols = ("id", "src_id", "relation", "dst_id", "confidence", "similarity",
            "rationale", "source", "created_at", "status")
    r = self.conn.execute(
        f"SELECT {', '.join(cols)} FROM edge_proposals WHERE id = %s", (proposal_id,)
    ).fetchone()
    return dict(zip(cols, r)) if r else None

def set_proposal_status(self, proposal_id: int, status: str) -> bool:
    cur = self.conn.execute(
        "UPDATE edge_proposals SET status = %s WHERE id = %s", (status, proposal_id))
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_edge_proposals.py -v`
Expected: PASS (or SKIP without Postgres, then verify on the live test DB).
Also run: `.venv/Scripts/python -m pytest tests/ -k "meta_version" -v` → PASS at 17.

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/storage/postgres.py tests/test_edge_proposals.py tests/
git commit -m "feat(storage): edge_proposals table (schema v17) + proposal/traces methods"
```

---

### Task 5: `graph_review` `proposed_link` finding + `review()` wiring

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py`
- Modify: `pseudolife_memory/service.py` (`graph_review` passes pending proposals)
- Test: `tests/test_graph_review.py` (append)

**Interfaces:**
- Consumes: `pending_proposals()` rows (dicts with `src, relation, dst, confidence, similarity, rationale`).
- Produces:
  - `proposed_links(proposals: list[dict]) -> list[dict]` — one finding `{type:"proposed_link", severity:"info", action:"review", label, links:[...]}` or `[]`.
  - `review(edges, entities, entity_sources_map, proposals=None)` — appends `proposed_links` when proposals given (backward-compatible default `None`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_review.py`:

```python
from pseudolife_memory.memory import graph_review as gr


def test_proposed_links_finding_shape():
    props = [{"src": "alpha", "relation": "related-to", "dst": "beta",
              "confidence": 0.45, "similarity": 0.91, "rationale": "co-discussed"}]
    out = gr.proposed_links(props)
    assert len(out) == 1
    f = out[0]
    assert f["type"] == "proposed_link" and f["action"] == "review"
    assert f["links"][0]["src"] == "alpha" and f["links"][0]["dst"] == "beta"


def test_proposed_links_empty_when_none():
    assert gr.proposed_links([]) == []


def test_review_includes_proposals_when_passed():
    out = gr.review([], [], {}, proposals=[
        {"src": "a", "relation": "related-to", "dst": "b", "confidence": 0.45}])
    assert any(f["type"] == "proposed_link" for f in out["findings"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_graph_review.py -k proposed -v`
Expected: FAIL (`AttributeError: ... proposed_links`).

- [ ] **Step 3: Implement the finding + wire `review`**

In `pseudolife_memory/memory/graph_review.py`, add:

```python
def proposed_links(proposals):
    if not proposals:
        return []
    links = [{"src": p["src"], "relation": p["relation"], "dst": p["dst"],
              "confidence": p.get("confidence"), "similarity": p.get("similarity"),
              "rationale": p.get("rationale")}
             for p in proposals]
    return [{"type": "proposed_link", "severity": "info", "action": "review",
             "label": f"{len(links)} proposed cross-session links",
             "links": links}]
```

Change `review` to accept proposals:

```python
def review(edges, entities, entity_sources_map, proposals=None):
    findings = (duplicate_candidates(entities) + test_artifacts(entities)
                + dubious_edges(edges, entities) + orphans(edges, entities)
                + unattributed(entities, entity_sources_map)
                + proposed_links(proposals or []))
    return {"findings": findings, "counts": {"total": len(findings)}}
```

In `pseudolife_memory/service.py` `graph_review` (line ~2453), fetch + pass proposals (inside the existing lock block, after `src_map = ...`):

```python
            g = self._storage.load_graph()
            src_map = self._storage.entity_sources_map()
            proposals = self._storage.pending_proposals()
        entities, edges = g["entities"], g["edges"]
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
            entities = [e for e in entities if e["id"] in keep]
            edges = [e for e in edges if e["src_id"] in keep and e["dst_id"] in keep]
        return gr.review(edges, entities, src_map, proposals=proposals)
```

> Only two changes vs. the existing method: fetch `proposals = self._storage.pending_proposals()` inside the lock block, and pass `proposals=proposals` to `gr.review`. The `entities`/`edges` scope-filtering is unchanged.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_graph_review.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py pseudolife_memory/service.py tests/test_graph_review.py
git commit -m "feat(graph-review): proposed_link finding wired into review()"
```

---

### Task 6: `service.deep_dream` orchestration (Steps A + B) + config

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (add `DeepDreamConfig`, wire into `MemoryConfig`)
- Modify: `pseudolife_memory/service.py` (add `deep_dream`)
- Test: `tests/test_deep_dream.py` (create, PG-backed for apply; dry-run assertions)

**Interfaces:**
- Consumes: `graph_consolidation` (Tasks 2–3), storage methods (Task 4), `load_graph`, `entity_sources_map`, `load_entries`, `traces_by_entity_norm`, `supersede_edge`, `merge_entity`, `set_edge_confidence`.
- Produces: `config.memory.deep_dream` (`min_similarity, top_k_candidates, max_context_snippets, auto_apply_safe`); `service.deep_dream(*, apply: bool = False) -> dict` returning `{dry_run|applied, rescored, would_supersede|superseded, would_merge|merged, candidates:[...], totals}`.

- [ ] **Step 1: Add the config dataclass**

In `pseudolife_memory/utils/config.py`, add near `DreamConfig`:

```python
@dataclass
class DeepDreamConfig:
    """Manual full-corpus graph consolidation (Phase-2 'C'). See
    docs/superpowers/specs/2026-06-28-deep-dream-graph-consolidation-design.md."""
    min_similarity: float = 0.55       # cosine floor for a link candidate
    top_k_candidates: int = 50         # max candidate pairs emitted per pass
    max_context_snippets: int = 3      # context snippets per entity in a candidate
    auto_apply_safe: bool = True       # auto-supersede violations + merge exact dups (apply only)
```

Wire it into the memory config aggregate (find `class MemoryConfig` and add a field, mirroring `dream: DreamConfig = field(default_factory=DreamConfig)`):

```python
    deep_dream: DeepDreamConfig = field(default_factory=DeepDreamConfig)
```

- [ ] **Step 2: Write the failing tests**

`tests/test_deep_dream.py` — the `svc` fixture mirrors `tests/test_graph.py:183-206` but is function-scoped (per-test truncate) for count isolation, and adds `edge_proposals` to the truncate list:

```python
import pytest

from tests.pg_fixtures import pg_url  # noqa: F401 (fixture; skips without a server)


@pytest.fixture()
def svc(pg_url, tmp_path_factory):
    """Function-scoped PG-backed service with a wiped graph."""
    import psycopg as _psy
    from pseudolife_memory.storage.schema import ensure_schema
    with _psy.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        conn.commit()
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE edges, edge_proposals, entity_aliases, relations, facts, "
                "world_facts, entries, episodes, entities, meta RESTART IDENTITY CASCADE")
        conn.commit()
        ensure_schema(conn)
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("dd-svc"), database_url=pg_url)


def test_dry_run_writes_nothing(svc):
    svc.graph_relate("user", "runs-on", "windows 11", origin="agent")  # a violation
    before = svc._storage.load_graph()["edges"]
    out = svc.deep_dream(apply=False)
    after = svc._storage.load_graph()["edges"]
    assert out["dry_run"] is True
    assert [e["id"] for e in before] == [e["id"] for e in after]   # nothing superseded


def test_apply_supersedes_violation_and_rescores(svc):
    svc.graph_relate("user", "runs-on", "windows 11", origin="agent")     # violation
    svc.graph_relate("daemon", "runs-on", "docker", origin="agent")       # clean
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert out["superseded"] >= 1
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_deep_dream.py -v`
Expected: FAIL (`AttributeError: ... deep_dream`) or SKIP without Postgres.

- [ ] **Step 4: Implement `deep_dream`**

In `pseudolife_memory/service.py`, beside `dream_run`:

```python
def deep_dream(self, *, apply: bool = False) -> dict[str, Any]:
    """Manual full-corpus graph consolidation. Step A (self-clean) + Step B
    (candidate generation), both deterministic. dry-run (default) computes and
    returns a preview + candidates without writing; apply commits the re-score and
    (when auto_apply_safe) the provably-safe supersede/merge class. Discovered
    links are NOT written here — Step C (subagents) proposes them via
    graph_propose_links. Backup-first on apply is a runbook step, not in-method."""
    from pseudolife_memory.memory import graph_consolidation as gc
    cfg = self.config.memory.deep_dream
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        g = self._storage.load_graph()
        scope_map = self._storage.entity_sources_map()
        traces = self._storage.traces_by_entity_norm()
        entries = self._storage.load_entries()
    entities, edges = g["entities"], g["edges"]

    rescore = gc.rescore_edges(edges, entities)
    violations = gc.hard_violation_edges(edges, entities)
    dups = gc.exact_duplicate_pairs(entities, edges)
    vectors = gc.entity_context_vectors(entities, entries, traces)
    candidates = gc.candidate_pairs(
        vectors, edges, entities, scope_map,
        min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
    candidates = self._attach_candidate_snippets(candidates, entities, entries,
                                                 traces, cfg.max_context_snippets)

    totals = {"entities": len(entities), "edges": len(edges),
              "candidates": len(candidates)}
    if not apply:
        return {"dry_run": True, "rescored": len(rescore),
                "would_supersede": [self._edge_label(e, entities) for e in violations],
                "would_merge": self._merge_labels(dups, entities),
                "candidates": candidates, "totals": totals}

    superseded = merged = 0
    with self._lock:
        for eid, conf in rescore:
            self._storage.set_edge_confidence(eid, conf)
        if cfg.auto_apply_safe:
            for e in violations:
                if self._storage.supersede_edge(e["src_id"], e["relation"], e["dst_id"]):
                    superseded += 1
            for frm, into in dups:
                if self._storage.merge_entity(frm, into):
                    merged += 1
    return {"applied": True, "rescored": len(rescore), "superseded": superseded,
            "merged": merged, "candidates": candidates, "totals": totals}
```

Add the three small helpers (label builders + snippet attach) near `deep_dream`:

```python
def _edge_label(self, e: dict, entities: list[dict]) -> dict:
    disp = {x["id"]: x["display"] for x in entities}
    return {"src": disp.get(e["src_id"], str(e["src_id"])), "relation": e["relation"],
            "dst": disp.get(e["dst_id"], str(e["dst_id"])), "confidence": e.get("confidence")}

def _merge_labels(self, dups: list[tuple[int, int]], entities: list[dict]) -> list[dict]:
    disp = {x["id"]: x["display"] for x in entities}
    return [{"from": disp.get(f, str(f)), "into": disp.get(t, str(t))} for f, t in dups]

def _attach_candidate_snippets(self, candidates, entities, entries, traces, k):
    """Attach up to k context snippets per side, for the Step-C subagent prompt."""
    by_id = {e["id"]: e for e in entries}
    canon = {e["id"]: e["canonical"] for e in entities}
    def snippets(eid):
        ids = traces.get(canon.get(eid, ""), [])[:k]
        return [by_id[i]["text"] for i in ids if i in by_id][:k]
    for c in candidates:
        c["src_snippets"] = snippets(c["src_id"])
        c["dst_snippets"] = snippets(c["dst_id"])
    return candidates
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_deep_dream.py -v`
Expected: PASS (or SKIP without Postgres → verify on live test DB per convention).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_deep_dream.py
git commit -m "feat(service): deep_dream orchestration (self-clean + candidate gen) + config"
```

---

### Task 7: Proposal mutations — service + MCP tools + web routes + fixtures

**Files:**
- Modify: `pseudolife_memory/service.py` (`graph_propose_links`, `graph_accept_proposal`, `graph_reject_proposal`)
- Modify: `pseudolife_memory/mcp_server.py` (3 tools)
- Modify: `pseudolife_memory/web/routes.py` (accept/reject routes)
- Modify: `pseudolife_memory/web/fixtures.py` (stubs + `proposed_link` finding)
- Test: `tests/test_deep_dream.py` (append, PG), `tests/test_web.py` (append, dispatch)

**Interfaces:**
- Consumes: storage proposal methods (Task 4); `edge_confidence`, `resolve_relation`, `_resolve_or_create_entity`, `self._graph.upsert_edge`.
- Produces:
  - `graph_propose_links(proposals: list[dict]) -> dict` — each `{src, relation, dst, similarity?, rationale?}`; resolve relation (closed vocab → `related-to`), compute `edge_confidence`, drop if `is_hard_type_violation`; insert survivors. Returns `{proposed, skipped}`.
  - `graph_accept_proposal(proposal_id: int) -> dict` — read proposal, `upsert_edge(origin='agent', confidence=proposal.confidence)`, mark `accepted`. Returns `{accepted, src, relation, dst}`.
  - `graph_reject_proposal(proposal_id: int) -> dict` — mark `rejected`. Returns `{rejected: bool}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deep_dream.py` (reuses the `svc` PG fixture defined there in Task 6):

```python
def test_propose_then_accept_promotes_to_edge(svc):
    svc._resolve_or_create_entity("alpha"); svc._resolve_or_create_entity("beta")
    out = svc.graph_propose_links([
        {"src": "alpha", "relation": "related-to", "dst": "beta",
         "similarity": 0.9, "rationale": "co-discussed"}])
    assert out["proposed"] == 1
    pid = svc._storage.pending_proposals()[0]["id"]
    acc = svc.graph_accept_proposal(pid)
    assert acc["accepted"] is True
    live = {(e["src_id"], e["relation"], e["dst_id"])
            for e in svc._storage.load_graph()["edges"]}
    a = svc._storage.find_entity("alpha")["id"]
    b = svc._storage.find_entity("beta")["id"]
    assert (a, "related-to", b) in live
    assert svc._storage.pending_proposals() == []


def test_propose_drops_type_violation(svc):
    svc._resolve_or_create_entity("user"); svc._resolve_or_create_entity("windows 11")
    out = svc.graph_propose_links([
        {"src": "user", "relation": "runs-on", "dst": "windows 11"}])
    assert out["proposed"] == 0 and out["skipped"] == 1


def test_reject_marks_rejected(svc):
    svc._resolve_or_create_entity("alpha"); svc._resolve_or_create_entity("beta")
    svc.graph_propose_links([{"src": "alpha", "relation": "related-to", "dst": "beta"}])
    pid = svc._storage.pending_proposals()[0]["id"]
    assert svc.graph_reject_proposal(pid)["rejected"] is True
    assert svc._storage.pending_proposals() == []
```

Append to `tests/test_web.py` (mirrors `test_assign_scope_and_unrelate_routes` at line ~156; the module's `svc` fixture is `FixtureService()`):

```python
def test_accept_reject_proposal_routes(svc):
    r = ConsoleRoutes(svc)
    assert r.dispatch("POST", "/api/graph/accept-proposal", {}, {"id": 1})["accepted"]
    assert r.dispatch("POST", "/api/graph/reject-proposal", {}, {"id": 1})["rejected"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_deep_dream.py -k "propose or accept or reject" tests/test_web.py -k proposal -v`
Expected: FAIL (`AttributeError: ... graph_propose_links` / unknown route).

- [ ] **Step 3: Implement the service methods**

In `pseudolife_memory/service.py`:

```python
def graph_propose_links(self, proposals: list[dict]) -> dict[str, Any]:
    """Ingest Step-C subagent link proposals. Each is gated by the SAME mechanism
    production uses (resolve_relation -> closed vocab; edge_confidence; drop hard
    type-violations) and inserted into edge_proposals — never into edges."""
    from pseudolife_memory import graph as G
    from pseudolife_memory.memory.relation_quality import (
        edge_confidence, is_hard_type_violation)
    import time as _t
    proposed = skipped = 0
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        known = [r["name"] for r in self._graph.load_relations()
                 if r["name"] not in ("prefers", "avoids")]
        for p in proposals:
            src, dst = str(p.get("src", "")), str(p.get("dst", ""))
            resolved, _ = G.resolve_relation(known, str(p.get("relation", "")))
            relation = resolved or "related-to"
            if not src or not dst or G.norm_name(src) == G.norm_name(dst) \
                    or is_hard_type_violation(src, relation, dst):
                skipped += 1
                continue
            se = self._resolve_or_create_entity(src)
            de = self._resolve_or_create_entity(dst)
            conf = edge_confidence(src, relation, dst)
            pid = self._storage.insert_proposal(
                se["id"], relation, de["id"], conf,
                p.get("similarity"), p.get("rationale"), "deep-dream", _t.time())
            if pid is not None:
                proposed += 1
            else:
                skipped += 1
    return {"proposed": proposed, "skipped": skipped}

def graph_accept_proposal(self, proposal_id: int) -> dict[str, Any]:
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        prop = self._storage.get_proposal(proposal_id)
        if prop is None or prop["status"] != "pending":
            return {"accepted": False, "reason": "not_pending", "id": proposal_id}
        self._graph.upsert_edge(prop["src_id"], prop["relation"], prop["dst_id"],
                                confidence=prop["confidence"], origin="agent")
        self._storage.set_proposal_status(proposal_id, "accepted")
        disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
    return {"accepted": True, "src": disp.get(prop["src_id"]),
            "relation": prop["relation"], "dst": disp.get(prop["dst_id"])}

def graph_reject_proposal(self, proposal_id: int) -> dict[str, Any]:
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        ok = self._storage.set_proposal_status(proposal_id, "rejected")
    return {"rejected": ok, "id": proposal_id}
```

- [ ] **Step 4: Add MCP tools**

In `pseudolife_memory/mcp_server.py` (mirror `memory_dream_run`'s `@_tool()` style):

```python
@_tool()
def memory_graph_propose_links(proposals: list[dict]) -> dict[str, Any]:
    """Ingest deep-dream Step-C link proposals (each {src, relation, dst,
    similarity?, rationale?}). Gated by edge_confidence; stored in edge_proposals
    (NOT edges) for review in Atlas. Returns {proposed, skipped}."""
    return service.graph_propose_links(proposals)


@_tool()
def memory_graph_accept_proposal(proposal_id: int) -> dict[str, Any]:
    """Promote a pending edge proposal to a real edge (origin=agent). Returns
    {accepted, src, relation, dst}."""
    return service.graph_accept_proposal(proposal_id)


@_tool()
def memory_graph_reject_proposal(proposal_id: int) -> dict[str, Any]:
    """Reject a pending edge proposal (kept for audit). Returns {rejected}."""
    return service.graph_reject_proposal(proposal_id)
```

> If `mcp_server.py` has a hard-coded registered-tool list checked by `tests/test_*` (the prior phase hit `test_all_tools_registered`), add these three tool names to it in this task.

- [ ] **Step 5: Add web routes**

In `pseudolife_memory/web/routes.py`, after the `merge` route (line ~173):

```python
        p("/api/graph/accept-proposal", lambda q, b: svc.graph_accept_proposal(b["id"]))
        p("/api/graph/reject-proposal", lambda q, b: svc.graph_reject_proposal(b["id"]))
```

- [ ] **Step 6: Add fixture stubs + `proposed_link` finding**

In `pseudolife_memory/web/fixtures.py`, add to the `graph_review` fixture findings list a `proposed_link` entry, and add methods:

```python
            {"type": "proposed_link", "severity": "info", "action": "review",
             "label": "1 proposed cross-session link",
             "links": [{"src": "Track A", "relation": "related-to", "dst": "Track B",
                        "confidence": 0.45, "similarity": 0.9, "rationale": "co-discussed"}]},
```

```python
    def graph_propose_links(self, proposals):
        return {"proposed": len(proposals), "skipped": 0}

    def graph_accept_proposal(self, proposal_id):
        return {"accepted": True, "src": "Track A", "relation": "related-to", "dst": "Track B"}

    def graph_reject_proposal(self, proposal_id):
        return {"rejected": True, "id": int(proposal_id)}
```

- [ ] **Step 7: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_deep_dream.py tests/test_web.py -v`
Expected: PASS (PG tests may SKIP without Postgres → verify on live test DB).

- [ ] **Step 8: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_deep_dream.py tests/test_web.py
git commit -m "feat(deep-dream): proposal mutations (propose/accept/reject) + MCP tools + routes"
```

---

### Task 8: `memory_deep_dream` MCP tool + runbook + docs

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (add `memory_deep_dream`)
- Create: `docs/runbooks/deep-dream.md` (operator + Step-C subagent runbook)
- Modify: `README.md` (one paragraph under the dreaming section) and `evals/README.md` (Step-C prompt reuse note)
- Test: none new (tool is a thin passthrough; covered by Task 6/7). Add a registration assertion if a registered-tool list exists.

**Interfaces:**
- Consumes: `service.deep_dream` (Task 6).
- Produces: MCP tool `memory_deep_dream(apply: bool = False)`.

- [ ] **Step 1: Add the MCP tool**

In `pseudolife_memory/mcp_server.py`:

```python
@_tool()
def memory_deep_dream(apply: bool = False) -> dict[str, Any]:
    """Manual full-corpus GRAPH consolidation (Phase-2 'C'). dry-run (default,
    apply=False) returns a preview — would-supersede / would-merge sets, re-score
    count, and semantic link CANDIDATES (with context snippets) — and writes
    nothing. apply=True commits the re-score and the provably-safe self-clean
    (supersede hard type-violations + merge exact duplicates).

    BACKUP FIRST before apply=True (ops/backup.ps1 on the host). After apply,
    drive Step C from this session: dispatch subagents over `candidates` to
    propose typed relations, then post survivors with memory_graph_propose_links;
    confirm them per-item in the Atlas Review queue (proposed_link findings)."""
    return service.deep_dream(apply=apply)
```

> If a registered-tool list exists (`test_all_tools_registered`), add `memory_deep_dream` to it.

- [ ] **Step 2: Write the runbook**

Create `docs/runbooks/deep-dream.md`:

```markdown
# Deep Dream — operator runbook

Manual, full-corpus graph consolidation. Graph-only (cortex/MIRAS untouched).

## 1. Preview (no writes)
Call `memory_deep_dream(apply=false)`. Review:
- `rescored` — agent edges whose confidence will change.
- `would_supersede` — hard type-violation edges to be auto-superseded.
- `would_merge` — exact-duplicate entity pairs to be merged.
- `candidates` — semantic cross-session link candidates (src/dst + context snippets).

## 2. Apply self-clean (backup first)
On the Windows host: `pwsh ops/backup.ps1`. Then `memory_deep_dream(apply=true)`.
This re-scores + (when `memory.deep_dream.auto_apply_safe`) supersedes violations
and merges exact dups. Supersede-not-delete — reversible.

## 3. Step C — propose links (this session, subagents)
For each `candidate`, dispatch an Opus subagent with the two entity displays + their
`src_snippets`/`dst_snippets` + the relation registry (reuse the
`evals/relation_extraction_bench.py --emit-prompts` prompt shape). The subagent
returns one closed-vocab relation or "reject". Collect survivors as
`[{src, relation, dst, similarity, rationale}]` and call
`memory_graph_propose_links(proposals)`. The gate (edge_confidence +
is_hard_type_violation) drops junk automatically.

## 4. Confirm in Atlas
Open Atlas Review → `proposed_link` findings → accept (promotes to a real edge)
or reject, per item. Nothing reaches `edges`/recall until you accept.
```

- [ ] **Step 3: Docs paragraphs**

Add to `README.md` (dreaming section) one paragraph: the incremental dream is window-local; the deep dream (`memory_deep_dream`) is a manual full-corpus graph pass that self-cleans and proposes cross-session links into the Atlas review queue. Add to `evals/README.md` a note that Step C reuses the bench's `--emit-prompts` shape.

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python -m pytest tests/ -q`
Expected: PASS (PG-backed tests SKIP without Postgres; no new failures vs the pre-existing known-flaky set noted in project memory).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py docs/runbooks/deep-dream.md README.md evals/README.md
git commit -m "feat(deep-dream): memory_deep_dream tool + operator/Step-C runbook + docs"
```

---

## Self-Review (completed against the spec)

**Spec coverage:**
- §3 manual/session-driven → Task 6 (`deep_dream`) + Task 8 (`memory_deep_dream`, Step-C runbook). ✓
- §5.A re-score / hard-violation / exact-dup → Task 2 + Task 6 apply path. ✓
- §5.A structural `is_hard_type_violation` (not float compare) → Task 1, used in Task 2/6/7. ✓
- §5.B centroid + near-pair + filters → Task 3; snippets → Task 6 `_attach_candidate_snippets`. ✓
- §5.C `edge_proposals` table + proposal-only + accept/reject → Task 4 + Task 7. ✓
- §5.C `graph_review` `proposed_link` finding → Task 5. ✓
- §6 config namespace → Task 6. ✓
- §7 no-separate-connection / supersede-not-delete / backup-as-runbook → Tasks 6–8 + Global Constraints. ✓
- §8 testing (pure unit, PG storage, route dispatch, dry-run/apply) → Tasks 1–7. ✓

**Type consistency:** `is_hard_type_violation(src, relation, dst)`, `edge_confidence(src, relation, dst)`, `rescore_edges(edges, entities)`, `candidate_pairs(vectors, edges, entities, scope_map, *, ...)`, `insert_proposal(...)→int|None`, `pending_proposals()→[{src,dst,...}]`, `deep_dream(*, apply)`, `graph_propose_links(proposals)`, `graph_accept_proposal(id)` — names/signatures consistent across tasks.

**Placeholder scan:** no TBD/TODO; every code step carries runnable code; the only "implementer judgement" notes are about reusing an existing pytest PG fixture whose exact name varies — flagged explicitly with how to find it (grep `tests/test_graph.py`), not left vague.

**Known deviation from spec (intentional):** the spec listed an `ops/deep_dream.py` CLI alongside the MCP tool. Dropped (YAGNI): a standalone script would need its own DB connection (the very hazard the Global Constraints forbid), whereas `memory_deep_dream` runs in-daemon under the existing lock and is invoked directly from the session. The runbook covers operator invocation.
