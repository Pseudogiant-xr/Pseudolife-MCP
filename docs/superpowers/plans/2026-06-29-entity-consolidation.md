# Entity Consolidation (SP-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface near-duplicate synonym **merges** and **junk** over-extraction entities from the live graph into the Atlas review queue, review-gated, never auto-applied.

**Architecture:** Two new pure analyzers in `graph_consolidation.py` (`partition_candidates` splits the deep dream's near-pairs into merge-vs-link by similarity + name-containment; `junk_entities` flags artifacts structurally). A new additive `entity_proposals` table (schema v18) persists merge/junk candidates; the deep-dream `apply` populates it (non-destructive). `graph_review` gains `merge_candidate` + `junk_candidate` findings; three confirm-gated mutations promote via the existing `merge_entity` / `delete_entity` storage ops. Mirrors the `edge_proposals` build one-to-one.

**Tech Stack:** Python 3.12, Postgres 16 + pgvector, psycopg, numpy; pytest (a live test PG on 127.0.0.1:5433 is reachable so PG tests RUN).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-29-entity-consolidation-design.md` is authoritative.
- **Nothing auto-applies.** Merge + junk candidates are proposals in `entity_proposals`; they touch `entities` only on a human-confirmed Atlas accept. No auto-merge from the semantic signal (only the existing exact-duplicate full-token-set class auto-merges).
- **Only new code paths change.** Do NOT modify the existing token-Jaccard `duplicate_candidates`, the self-clean (`rescore_edges`/`hard_violation_edges`/`exact_duplicate_pairs`), or the link-candidate (Step C) flow. The new `merge_candidate` is complementary to `duplicate_candidates`.
- **Persisting proposals is NOT gated by `auto_apply_safe`** — that flag governs only the destructive auto-supersede/auto-merge; populating the review queue happens on any `apply`.
- **Lock discipline:** all new service methods run under the non-reentrant `self._lock` using raw `self._storage.*` (NEVER call another service method that re-acquires the lock — e.g. accept-merge calls `self._storage.merge_entity(...)` directly, not `self.graph_merge(...)`, to avoid deadlock).
- **Config defaults verbatim:** `merge_min_similarity: float = 0.90`, `junk_max_degree: int = 1`.
- **Schema bump:** `SCHEMA_META_VERSION` 17→18 (additive `entity_proposals`); the three hard-coded `== 17` test assertions bump to `== 18`.
- **Branch-first.** Create a feature branch before Task 1; do not implement on `master`.
- Pure tests: `tests/test_graph_consolidation.py`. PG-backed: `tests/test_entity_proposals.py` (new), `tests/test_deep_dream.py`, `tests/test_web.py`, `tests/test_mcp_server.py`. Run with `.venv/Scripts/python.exe -m pytest <file> -v`.

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
cd /c/Users/<user>/ClaudeCode/PseudoLife-MCP
git checkout master && git pull --ff-only
git checkout -b feat/entity-consolidation
```

No test. Commit nothing yet.

---

### Task 1: Pure analyzers — `partition_candidates` + `junk_entities`

**Files:**
- Modify: `pseudolife_memory/memory/graph_consolidation.py`
- Test: `tests/test_graph_consolidation.py` (append)

**Interfaces:**
- Consumes: `_full_token_set` (exists), `degree_counts` (imported), `_disp` (exists); add `from pseudolife_memory.graph import norm_name`.
- Produces:
  - `partition_candidates(pairs, entities, edges, *, merge_min_similarity=0.90) -> tuple[list[dict], list[dict]]` → `(merges, links)`. Each `pair` is a `candidate_pairs` dict (`{src_id, dst_id, src, dst, similarity, ...}`). A merge = `{from_id, into_id, from, into, similarity, reason}` (fold lower-degree into higher-degree; tie folds higher id into lower id). `links` = the input pairs that weren't merges (unchanged).
  - `junk_entities(entities, edges, *, max_degree=1) -> list[dict]` → `[{entity_id, display, reason}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_consolidation.py`:

```python
def test_partition_candidates_merge_vs_link():
    ents = [
        {"id": 1, "canonical": "daemon", "display": "daemon", "etype": None},
        {"id": 2, "canonical": "live daemon", "display": "live daemon", "etype": None},
        {"id": 3, "canonical": "track a (recall)", "display": "Track A (recall)", "etype": None},
        {"id": 4, "canonical": "track b (insight)", "display": "Track B (insight)", "etype": None},
    ]
    # entity 1 has an edge (degree 1) so 'live daemon' folds into 'daemon'.
    edges = [_edge(99, 1, "related-to", 3, 0.45)]
    pairs = [
        {"src_id": 1, "dst_id": 2, "src": "daemon", "dst": "live daemon", "similarity": 0.99},
        {"src_id": 3, "dst_id": 4, "src": "Track A (recall)", "dst": "Track B (insight)", "similarity": 0.98},
    ]
    merges, links = gc.partition_candidates(pairs, ents, edges, merge_min_similarity=0.90)
    assert [(m["from_id"], m["into_id"]) for m in merges] == [(2, 1)]   # name-contained -> merge
    assert merges[0]["reason"] == "token-subset"
    assert [(p["src_id"], p["dst_id"]) for p in links] == [(3, 4)]      # distinct names -> link


def test_partition_candidates_below_threshold_is_link():
    ents = [
        {"id": 1, "canonical": "test", "display": "test", "etype": None},
        {"id": 2, "canonical": "test harness", "display": "test harness", "etype": None},
    ]
    # name-contained, but low similarity -> NOT a merge (guard against coincidental containment).
    pairs = [{"src_id": 1, "dst_id": 2, "src": "test", "dst": "test harness", "similarity": 0.70}]
    merges, links = gc.partition_candidates(pairs, ents, [], merge_min_similarity=0.90)
    assert merges == []
    assert len(links) == 1


def test_junk_entities_flags_artifacts_not_real():
    ents = [
        {"id": 1, "canonical": "2", "display": "2", "etype": None},          # bare number
        {"id": 2, "canonical": "live", "display": "LIVE", "etype": None},     # status word
        {"id": 3, "canonical": "ok", "display": "ok", "etype": None},        # too short AND status
        {"id": 4, "canonical": "daemon", "display": "daemon", "etype": None}, # real entity
        {"id": 5, "canonical": "merged", "display": "merged", "etype": None}, # status word, BUT high degree
    ]
    edges = [_edge(10, 5, "related-to", 4, 0.45), _edge(11, 5, "related-to", 1, 0.45)]  # entity 5 degree 2
    out = {j["entity_id"]: j["reason"] for j in gc.junk_entities(ents, edges, max_degree=1)}
    assert out == {1: "bare-number", 2: "status-word", 3: "too-short"}  # 4 real, 5 well-connected
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -k "partition or junk_entities" -v`
Expected: FAIL (`AttributeError: ... partition_candidates` / `junk_entities`).

- [ ] **Step 3: Implement the analyzers**

In `pseudolife_memory/memory/graph_consolidation.py`, update the imports line:

```python
from pseudolife_memory.graph import degree_counts, norm_name
```

Append at the end of the file:

```python
# --- SP-1: entity consolidation (merge + junk surfacing) ----------------------

_JUNK_STOPWORDS = frozenset({
    "live", "merged", "done", "fixed", "current", "ok", "pending", "wip",
    "todo", "n/a", "none", "null",
})
_BARE_NUMBER = re.compile(r"^\d+$")


def _name_contains(a: str, b: str) -> str | None:
    """A reason if one display asserts identity with the other, else None.
    token-subset (every token of one is in the other) OR norm-name substring."""
    ta, tb = _full_token_set(a), _full_token_set(b)
    if ta and tb and (ta <= tb or tb <= ta):
        return "token-subset"
    na, nb = norm_name(a), norm_name(b)
    if na and nb and (na in nb or nb in na):
        return "substring"
    return None


def partition_candidates(pairs: list[dict], entities: list[dict], edges: list[dict], *,
                         merge_min_similarity: float = 0.90,
                         ) -> tuple[list[dict], list[dict]]:
    """Split near-pairs into MERGE candidates (high sim + name-containment) and the
    remaining LINK candidates. Merge fold direction: lower-degree into higher-degree
    (tie folds higher id into lower id), matching exact_duplicate_pairs."""
    deg = degree_counts(edges)
    disp = _disp(entities)
    merges: list[dict] = []
    links: list[dict] = []
    for p in pairs:
        reason = (_name_contains(p["src"], p["dst"])
                  if float(p.get("similarity", 0.0)) >= merge_min_similarity else None)
        if reason is None:
            links.append(p)
            continue
        u, v = p["src_id"], p["dst_id"]
        du, dv = deg.get(u, 0), deg.get(v, 0)
        if du > dv or (du == dv and u < v):
            into, frm = u, v
        else:
            into, frm = v, u
        merges.append({"from_id": frm, "into_id": into,
                       "from": disp.get(frm, str(frm)), "into": disp.get(into, str(into)),
                       "similarity": p["similarity"], "reason": reason})
    return merges, links


def junk_entities(entities: list[dict], edges: list[dict], *,
                  max_degree: int = 1) -> list[dict]:
    """Over-extraction artifacts: bare numbers, <=2-char displays, or status-words —
    only when weakly connected (degree <= max_degree). Proposal-only; never deletes."""
    deg = degree_counts(edges)
    out: list[dict] = []
    for e in entities:
        if deg.get(e["id"], 0) > max_degree:
            continue
        d = str(e["display"]).strip()
        if _BARE_NUMBER.match(d):
            reason = "bare-number"
        elif len(d) <= 2:
            reason = "too-short"
        elif d.lower() in _JUNK_STOPWORDS:
            reason = "status-word"
        else:
            continue
        out.append({"entity_id": e["id"], "display": e["display"], "reason": reason})
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_consolidation.py -v`
Expected: PASS (new tests + all existing).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_consolidation.py tests/test_graph_consolidation.py
git commit -m "feat(entity-consolidation): partition_candidates + junk_entities analyzers"
```

---

### Task 2: `entity_proposals` table + storage

**Files:**
- Modify: `pseudolife_memory/storage/schema.py` (add table + 2 partial indexes; bump `SCHEMA_META_VERSION` 17→18)
- Modify: `pseudolife_memory/storage/postgres.py` (4 storage methods)
- Modify: the three `== 17` schema-assertion tests → `== 18`
- Test: `tests/test_entity_proposals.py` (create, PG-backed)

**Interfaces:**
- Produces on `PostgresStorage`:
  - `insert_entity_proposal(kind, entity_id, into_id, score, reason, now) -> int | None` (`ON CONFLICT DO NOTHING` → None on dup)
  - `pending_entity_proposals() -> list[dict]` (status='pending', joined to displays as `entity` + `into`)
  - `get_entity_proposal(id) -> dict | None`
  - `set_entity_proposal_status(id, status) -> bool`

- [ ] **Step 1: Add the table + bump the version**

In `pseudolife_memory/storage/schema.py`, after the `edge_proposals` block (ends line ~111), add:

```sql
CREATE TABLE IF NOT EXISTS entity_proposals (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  into_id BIGINT REFERENCES entities(id) ON DELETE CASCADE,
  score REAL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_merge_uq ON entity_proposals
  (LEAST(entity_id, into_id), GREATEST(entity_id, into_id)) WHERE kind = 'merge';
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_junk_uq ON entity_proposals
  (entity_id) WHERE kind = 'junk';
```

Change `SCHEMA_META_VERSION = 17` → `SCHEMA_META_VERSION = 18` (line 18).

- [ ] **Step 2: Bump the version-assertion tests**

Run: `git grep -n "== 17" tests/`
Change each of these `== 17` → `== 18`: `tests/test_schema_v13.py:26`, `tests/test_schema_v16.py:6` (also rename its function `test_schema_version_is_17` → `test_schema_version_is_18`), `tests/test_temporal_stamp.py` (the `SCHEMA_META_VERSION == 17` assertion). Touch no other `SCHEMA_META_VERSION` references.

- [ ] **Step 3: Write the failing storage tests**

`tests/test_entity_proposals.py` (the `storage` fixture is copied verbatim from `tests/test_graph.py:110-116`):

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


def _ents(st):
    return (st.ensure_entity("daemon", display="daemon"),
            st.ensure_entity("live daemon", display="live daemon"),
            st.ensure_entity("2", display="2"))


def test_merge_proposal_insert_pending_accept(storage):
    a, b, _ = _ents(storage)
    pid = storage.insert_entity_proposal("merge", b, a, 0.99, "token-subset", time.time())
    assert pid is not None
    pend = storage.pending_entity_proposals()
    assert len(pend) == 1
    row = pend[0]
    assert row["kind"] == "merge" and row["entity"] == "live daemon" and row["into"] == "daemon"
    assert storage.set_entity_proposal_status(pid, "accepted") is True
    assert storage.pending_entity_proposals() == []


def test_junk_proposal_insert_and_get(storage):
    _, _, n = _ents(storage)
    pid = storage.insert_entity_proposal("junk", n, None, None, "bare-number", time.time())
    assert pid is not None
    got = storage.get_entity_proposal(pid)
    assert got["kind"] == "junk" and got["entity_id"] == n and got["into_id"] is None


def test_partial_unique_dedupe(storage):
    a, b, n = _ents(storage)
    first = storage.insert_entity_proposal("merge", b, a, 0.99, "x", time.time())
    rev = storage.insert_entity_proposal("merge", a, b, 0.99, "x", time.time())   # order-free dup
    assert first is not None and rev is None
    j1 = storage.insert_entity_proposal("junk", n, None, None, "x", time.time())
    j2 = storage.insert_entity_proposal("junk", n, None, None, "x", time.time())
    assert j1 is not None and j2 is None
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_entity_proposals.py -v`
Expected: FAIL (`AttributeError: ... insert_entity_proposal`).

- [ ] **Step 5: Implement the storage methods**

In `pseudolife_memory/storage/postgres.py`, after `set_proposal_status` (line ~786):

```python
def insert_entity_proposal(self, kind: str, entity_id: int, into_id: int | None,
                           score: float | None, reason: str | None, now: float) -> int | None:
    row = self.conn.execute(
        "INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
        (kind, entity_id, into_id, score, reason, now),
    ).fetchone()
    self.conn.commit()
    return int(row[0]) if row else None

def pending_entity_proposals(self) -> list[dict]:
    cols = ("id", "kind", "entity_id", "into_id", "score", "reason", "status", "created_at")
    rows = self.conn.execute(
        "SELECT p.id, p.kind, p.entity_id, p.into_id, p.score, p.reason, p.status, "
        "       p.created_at, e.display, i.display "
        "FROM entity_proposals p "
        "JOIN entities e ON e.id = p.entity_id "
        "LEFT JOIN entities i ON i.id = p.into_id "
        "WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r[:8]))
        d["entity"], d["into"] = r[8], r[9]
        out.append(d)
    return out

def get_entity_proposal(self, proposal_id: int) -> dict | None:
    cols = ("id", "kind", "entity_id", "into_id", "score", "reason", "status", "created_at")
    r = self.conn.execute(
        f"SELECT {', '.join(cols)} FROM entity_proposals WHERE id = %s", (proposal_id,)
    ).fetchone()
    return dict(zip(cols, r)) if r else None

def set_entity_proposal_status(self, proposal_id: int, status: str) -> bool:
    cur = self.conn.execute(
        "UPDATE entity_proposals SET status = %s WHERE id = %s", (status, proposal_id))
    self.conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_entity_proposals.py tests/test_schema_v13.py tests/test_schema_v16.py tests/test_temporal_stamp.py -v`
Expected: PASS (storage tests + the three version asserts at 18).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/storage/schema.py pseudolife_memory/storage/postgres.py tests/test_entity_proposals.py tests/test_schema_v13.py tests/test_schema_v16.py tests/test_temporal_stamp.py
git commit -m "feat(storage): entity_proposals table (schema v18) + merge/junk proposal methods"
```

---

### Task 3: `graph_review` findings + service wiring

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py`
- Modify: `pseudolife_memory/service.py` (`graph_review` fetches + passes entity proposals)
- Test: `tests/test_graph_review.py` (append)

**Interfaces:**
- Consumes: `pending_entity_proposals()` rows (`{id, kind, entity, into, score, reason, ...}`).
- Produces:
  - `merge_candidates(entity_proposals) -> list[dict]` and `junk_candidates(entity_proposals) -> list[dict]`.
  - `review(edges, entities, entity_sources_map, proposals=None, entity_proposals=None)` — appends both new findings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_review.py`:

```python
def test_merge_and_junk_candidate_findings():
    eprops = [
        {"id": 1, "kind": "merge", "entity": "live daemon", "into": "daemon",
         "score": 0.99, "reason": "token-subset"},
        {"id": 2, "kind": "junk", "entity": "2", "into": None, "reason": "bare-number"},
    ]
    out = gr.review([], [], {}, entity_proposals=eprops)
    types = {f["type"] for f in out["findings"]}
    assert "merge_candidate" in types and "junk_candidate" in types
    mc = next(f for f in out["findings"] if f["type"] == "merge_candidate")
    assert mc["merges"][0]["from"] == "live daemon" and mc["merges"][0]["into"] == "daemon"
    jc = next(f for f in out["findings"] if f["type"] == "junk_candidate")
    assert jc["entities"][0]["entity"] == "2" and jc["entities"][0]["reason"] == "bare-number"


def test_entity_proposals_default_none_no_findings():
    out = gr.review([], [], {})
    assert all(f["type"] not in ("merge_candidate", "junk_candidate") for f in out["findings"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_review.py -k "merge_and_junk or entity_proposals_default" -v`
Expected: FAIL (`TypeError: review() got an unexpected keyword 'entity_proposals'`).

- [ ] **Step 3: Implement findings + wire `review`**

In `pseudolife_memory/memory/graph_review.py`, add (near `proposed_links`):

```python
def merge_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "merge"]
    if not rows:
        return []
    merges = [{"from": p["entity"], "into": p["into"], "similarity": p.get("score"),
               "reason": p.get("reason"), "id": p["id"]} for p in rows]
    return [{"type": "merge_candidate", "severity": "warn", "action": "merge",
             "label": f"{len(merges)} near-duplicate entity merges", "merges": merges}]


def junk_candidates(entity_proposals):
    rows = [p for p in (entity_proposals or []) if p.get("kind") == "junk"]
    if not rows:
        return []
    items = [{"entity": p["entity"], "reason": p.get("reason"), "id": p["id"]} for p in rows]
    return [{"type": "junk_candidate", "severity": "warn", "action": "delete",
             "label": f"{len(items)} junk entities to prune", "entities": items}]
```

Change `review` to accept + append (current signature is `review(edges, entities, entity_sources_map, proposals=None)`):

```python
def review(edges, entities, entity_sources_map, proposals=None, entity_proposals=None):
    findings = (duplicate_candidates(entities) + test_artifacts(entities)
                + dubious_edges(edges, entities) + orphans(edges, entities)
                + unattributed(entities, entity_sources_map)
                + proposed_links(proposals or [])
                + merge_candidates(entity_proposals or [])
                + junk_candidates(entity_proposals or []))
    return {"findings": findings, "counts": {"total": len(findings)}}
```

In `pseudolife_memory/service.py` `graph_review` (line ~2461), fetch + pass (inside the existing lock block, after `proposals = ...`):

```python
            proposals = self._storage.pending_proposals()
            entity_proposals = self._storage.pending_entity_proposals()
```

and the final return:

```python
        return gr.review(edges, entities, src_map, proposals=proposals,
                         entity_proposals=entity_proposals)
```

(The scope-filtering block between is unchanged.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_review.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py pseudolife_memory/service.py tests/test_graph_review.py
git commit -m "feat(graph-review): merge_candidate + junk_candidate findings wired into review()"
```

---

### Task 4: `service.deep_dream` wiring (partition + persist) + config

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (`DeepDreamConfig`: `merge_min_similarity`, `junk_max_degree`)
- Modify: `pseudolife_memory/service.py` (`deep_dream`: partition near-pairs; persist merge + junk on apply)
- Test: `tests/test_deep_dream.py` (append)

**Interfaces:**
- Consumes: `gc.partition_candidates`, `gc.junk_entities` (Task 1); `insert_entity_proposal` (Task 2); `cfg.merge_min_similarity`, `cfg.junk_max_degree`.
- Produces: `deep_dream` dry-run adds `would_merge_propose` + `would_junk`; apply adds `merge_proposed` + `junk_proposed` and persists; `candidates` now contains only LINK candidates.

- [ ] **Step 1: Add the config fields**

In `pseudolife_memory/utils/config.py` `DeepDreamConfig`, after `min_entity_mentions`:

```python
    merge_min_similarity: float = 0.90   # cosine floor for a near-dup MERGE candidate (vs a link)
    junk_max_degree: int = 1             # junk entities must be this weakly connected to be flagged
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_deep_dream.py` (reuses the `svc` PG fixture already in this file):

```python
def test_dry_run_previews_merge_and_junk(svc):
    # Two synonym entities sharing two entries -> a high-sim near-pair name-contained -> merge preview.
    svc.cortex_write("daemon", "role", "serves MCP", support="user")
    svc.cortex_write("daemon", "note", "the daemon runs in docker", support="user")
    svc.cortex_write("live daemon", "role", "serves MCP", support="user")
    svc.cortex_write("live daemon", "note", "the daemon runs in docker", support="user")
    svc.graph_relate("2", "related-to", "daemon", origin="agent")   # 'live daemon' co-mentions
    out = svc.deep_dream(apply=False)
    assert out["dry_run"] is True
    assert "would_merge_propose" in out and "would_junk" in out
    assert svc._storage.pending_entity_proposals() == []            # dry-run writes nothing


def test_apply_persists_entity_proposals(svc):
    svc.cortex_write("daemon", "role", "serves MCP", support="user")
    svc.cortex_write("daemon", "note", "runs in docker", support="user")
    svc.cortex_write("live daemon", "role", "serves MCP", support="user")
    svc.cortex_write("live daemon", "note", "runs in docker", support="user")
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert "merge_proposed" in out and "junk_proposed" in out
```

> Implementer note: these tests assert the *plumbing* (keys present; dry-run writes nothing) rather than exact merge counts, because the count depends on the embedder's cosine on the seeded text. If the embedder yields no ≥0.90 near-pair, `would_merge_propose` may be empty — that is acceptable; the assertion is on the keys + the no-write contract. The exact merge classification is unit-tested in Task 1.

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deep_dream.py -k "previews_merge or persists_entity" -v`
Expected: FAIL (`KeyError: 'would_merge_propose'`).

- [ ] **Step 4: Implement the wiring**

In `pseudolife_memory/service.py` `deep_dream`, replace the candidate/return block (lines ~2891-2921). After `vectors, mentions = ...`:

```python
        near = gc.candidate_pairs(
            vectors, edges, entities, scope_map, mentions,
            min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
        merge_cands, link_cands = gc.partition_candidates(
            near, entities, edges, merge_min_similarity=cfg.merge_min_similarity)
        junk = gc.junk_entities(entities, edges, max_degree=cfg.junk_max_degree)
        candidates = self._attach_candidate_snippets(link_cands, entities, entries,
                                                     traces, cfg.max_context_snippets)

        totals = {"entities": len(entities), "edges": len(edges),
                  "candidates": len(candidates)}
        if not apply:
            return {"dry_run": True, "rescored": len(rescore),
                    "would_supersede": [self._edge_label(e, entities) for e in violations],
                    "would_merge": self._merge_labels(dups, entities),
                    "would_merge_propose": [{"from": m["from"], "into": m["into"],
                                             "similarity": m["similarity"], "reason": m["reason"]}
                                            for m in merge_cands],
                    "would_junk": [{"entity": j["display"], "reason": j["reason"]} for j in junk],
                    "candidates": candidates, "totals": totals}

        import time as _t
        superseded = merged = merge_proposed = junk_proposed = 0
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
            # Non-destructive: populate the review queue regardless of auto_apply_safe.
            for m in merge_cands:
                if self._storage.insert_entity_proposal(
                        "merge", m["from_id"], m["into_id"], m["similarity"], m["reason"], _t.time()) is not None:
                    merge_proposed += 1
            for j in junk:
                if self._storage.insert_entity_proposal(
                        "junk", j["entity_id"], None, None, j["reason"], _t.time()) is not None:
                    junk_proposed += 1
        return {"applied": True, "rescored": len(rescore), "superseded": superseded,
                "merged": merged, "merge_proposed": merge_proposed,
                "junk_proposed": junk_proposed, "candidates": candidates, "totals": totals}
```

(The earlier `rescore`/`violations`/`dups`/`vectors` lines and the snapshot-gap comment are unchanged; only the candidate-onward block changes.)

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deep_dream.py -v`
Expected: PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_deep_dream.py
git commit -m "feat(deep-dream): partition near-pairs + persist merge/junk entity proposals on apply"
```

---

### Task 5: Proposal mutations + MCP tools + routes + fixtures

**Files:**
- Modify: `pseudolife_memory/service.py` (`graph_accept_entity_merge`, `graph_accept_entity_junk`, `graph_reject_entity_proposal`)
- Modify: `pseudolife_memory/mcp_server.py` (3 tools) + `tests/test_mcp_server.py` (registry list)
- Modify: `pseudolife_memory/web/routes.py` (3 routes) + `pseudolife_memory/web/fixtures.py` (stubs + 2 findings)
- Test: `tests/test_deep_dream.py` (append, PG), `tests/test_web.py` (append)

**Interfaces:**
- Consumes: `get_entity_proposal` / `set_entity_proposal_status` (Task 2); existing `self._storage.merge_entity` / `delete_entity`.
- Produces: `graph_accept_entity_merge(id) -> {accepted, from, into}`; `graph_accept_entity_junk(id) -> {accepted, entity}`; `graph_reject_entity_proposal(id) -> {rejected, id}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_deep_dream.py`:

```python
def test_accept_entity_merge_folds(svc):
    a = svc._resolve_or_create_entity("daemon")["id"]
    b = svc._resolve_or_create_entity("live daemon")["id"]
    pid = svc._storage.insert_entity_proposal("merge", b, a, 0.99, "token-subset", __import__("time").time())
    out = svc.graph_accept_entity_merge(pid)
    assert out["accepted"] is True and out["into"] == "daemon"
    assert svc._storage.find_entity("live daemon") is None          # folded away
    assert svc._storage.pending_entity_proposals() == []


def test_accept_entity_junk_deletes(svc):
    n = svc._resolve_or_create_entity("2")["id"]
    pid = svc._storage.insert_entity_proposal("junk", n, None, None, "bare-number", __import__("time").time())
    out = svc.graph_accept_entity_junk(pid)
    assert out["accepted"] is True and out["entity"] == "2"
    assert svc._storage.find_entity("2") is None


def test_reject_entity_proposal(svc):
    n = svc._resolve_or_create_entity("merged")["id"]
    pid = svc._storage.insert_entity_proposal("junk", n, None, None, "status-word", __import__("time").time())
    assert svc.graph_reject_entity_proposal(pid)["rejected"] is True
    assert svc._storage.find_entity("merged") is not None           # NOT deleted on reject
    assert svc._storage.pending_entity_proposals() == []
```

Append to `tests/test_web.py` (module already has `svc` = `FixtureService` + imports `ConsoleRoutes`):

```python
def test_entity_proposal_routes(svc):
    r = ConsoleRoutes(svc)
    assert r.dispatch("POST", "/api/graph/accept-entity-merge", {}, {"id": 1})["accepted"]
    assert r.dispatch("POST", "/api/graph/accept-entity-junk", {}, {"id": 2})["accepted"]
    assert r.dispatch("POST", "/api/graph/reject-entity-proposal", {}, {"id": 3})["rejected"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deep_dream.py -k "accept_entity or reject_entity" tests/test_web.py -k entity_proposal -v`
Expected: FAIL (`AttributeError: ... graph_accept_entity_merge` / unknown route).

- [ ] **Step 3: Implement the service mutations**

In `pseudolife_memory/service.py`, after `graph_reject_proposal` (line ~3000):

```python
def graph_accept_entity_merge(self, proposal_id: int) -> dict[str, Any]:
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        prop = self._storage.get_entity_proposal(proposal_id)
        if prop is None or prop["status"] != "pending" or prop["kind"] != "merge":
            return {"accepted": False, "reason": "not_pending", "id": proposal_id}
        disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
        ok = self._storage.merge_entity(prop["entity_id"], prop["into_id"])
        self._storage.set_entity_proposal_status(proposal_id, "accepted")
    return {"accepted": ok, "from": disp.get(prop["entity_id"]),
            "into": disp.get(prop["into_id"])}

def graph_accept_entity_junk(self, proposal_id: int) -> dict[str, Any]:
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        prop = self._storage.get_entity_proposal(proposal_id)
        if prop is None or prop["status"] != "pending" or prop["kind"] != "junk":
            return {"accepted": False, "reason": "not_pending", "id": proposal_id}
        disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
        ok = self._storage.delete_entity(prop["entity_id"])
        self._storage.set_entity_proposal_status(proposal_id, "accepted")
    return {"accepted": ok, "entity": disp.get(prop["entity_id"])}

def graph_reject_entity_proposal(self, proposal_id: int) -> dict[str, Any]:
    with self._lock:
        self._ensure_init()
        if self._storage is None:
            return dict(self._GRAPH_UNAVAILABLE)
        ok = self._storage.set_entity_proposal_status(proposal_id, "rejected")
    return {"rejected": ok, "id": proposal_id}
```

- [ ] **Step 4: Add MCP tools + registry**

In `pseudolife_memory/mcp_server.py`, after `memory_graph_reject_proposal` (line ~1270):

```python
@_tool()
def memory_graph_accept_entity_merge(proposal_id: int) -> dict[str, Any]:
    """Accept a near-duplicate entity-merge proposal: fold the 'from' entity into the
    'into' entity. Returns {accepted, from, into}."""
    return service.graph_accept_entity_merge(proposal_id)


@_tool()
def memory_graph_accept_entity_junk(proposal_id: int) -> dict[str, Any]:
    """Accept a junk-entity prune proposal: delete the over-extraction artifact entity.
    Returns {accepted, entity}."""
    return service.graph_accept_entity_junk(proposal_id)


@_tool()
def memory_graph_reject_entity_proposal(proposal_id: int) -> dict[str, Any]:
    """Reject a pending entity merge/junk proposal (kept for audit). Returns {rejected}."""
    return service.graph_reject_entity_proposal(proposal_id)
```

In `tests/test_mcp_server.py::test_all_tools_registered`, add the three names to the asserted list (near the existing `memory_graph_*proposal` entries):

```python
        "memory_graph_accept_entity_merge",
        "memory_graph_accept_entity_junk",
        "memory_graph_reject_entity_proposal",
```

- [ ] **Step 5: Add web routes + fixtures**

In `pseudolife_memory/web/routes.py`, after the `reject-proposal` route (line ~175):

```python
        p("/api/graph/accept-entity-merge", lambda q, b: svc.graph_accept_entity_merge(b["id"]))
        p("/api/graph/accept-entity-junk", lambda q, b: svc.graph_accept_entity_junk(b["id"]))
        p("/api/graph/reject-entity-proposal", lambda q, b: svc.graph_reject_entity_proposal(b["id"]))
```

In `pseudolife_memory/web/fixtures.py` `graph_review` findings list, add (and bump `counts.total` 5→7):

```python
            {"type": "merge_candidate", "severity": "warn", "action": "merge",
             "label": "1 near-duplicate entity merges",
             "merges": [{"from": "live daemon", "into": "daemon", "similarity": 0.99,
                         "reason": "token-subset", "id": 1}]},
            {"type": "junk_candidate", "severity": "warn", "action": "delete",
             "label": "1 junk entities to prune",
             "entities": [{"entity": "2", "reason": "bare-number", "id": 2}]},
```

and after `graph_reject_proposal` (line ~425):

```python
    def graph_accept_entity_merge(self, proposal_id):
        return {"accepted": True, "from": "live daemon", "into": "daemon"}

    def graph_accept_entity_junk(self, proposal_id):
        return {"accepted": True, "entity": "2"}

    def graph_reject_entity_proposal(self, proposal_id):
        return {"rejected": True, "id": int(proposal_id)}
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_deep_dream.py tests/test_web.py tests/test_mcp_server.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/mcp_server.py pseudolife_memory/web/routes.py pseudolife_memory/web/fixtures.py tests/test_deep_dream.py tests/test_web.py tests/test_mcp_server.py
git commit -m "feat(entity-consolidation): accept/reject entity-proposal mutations + MCP tools + routes"
```

---

## Self-Review (completed against the spec)

**Spec coverage:**
- §3a `partition_candidates` (similarity + name-containment, fold direction) → Task 1. ✓
- §3b `junk_entities` (bare-number/too-short/status-word + degree guard) → Task 1. ✓
- §4 `entity_proposals` table (v18, partial unique indexes) + storage methods → Task 2. ✓
- §5 `graph_review` findings + `service.graph_review` wiring → Task 3. ✓
- §5 `deep_dream` partition + persist-on-apply (not gated by `auto_apply_safe`) + config → Task 4. ✓
- §5 mutations (accept-merge→`merge_entity`, accept-junk→`delete_entity`, reject) + tools + routes + fixtures → Task 5. ✓
- §6 testing across all tasks. ✓
- Constraint "only auto-merge stays the exact-dup class; semantic is proposal-only" → Tasks 1/4 (semantic never auto-applies). ✓

**Placeholder scan:** none — every code step has complete code + exact commands. The one judgement note (Task 4 count-vs-keys) is flagged explicitly with rationale, not a vague placeholder.

**Type consistency:** `partition_candidates(...) -> (merges, links)` with merge keys `from_id/into_id/from/into/similarity/reason`; `junk_entities(...) -> [{entity_id,display,reason}]`; storage `insert_entity_proposal(kind, entity_id, into_id, score, reason, now)`; `pending_entity_proposals()` rows expose `entity`/`into`; findings read those; `deep_dream` persists `m["from_id"]`/`m["into_id"]`; mutations use `prop["entity_id"]`/`prop["into_id"]`. Consistent across tasks.
