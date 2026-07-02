# External Findings Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship features G (betweenness god-nodes), A (fact_get ranked candidates), D (edge provenance tags) from `docs/specs/2026-07-03-external-findings-design.md`.

**Architecture:** All three are read-time/additive enrichments: G re-ranks an existing pure-topology function, A adds a miss-path enrichment to the cortex lookup pipeline, D derives a display tag from fields edges already carry. No schema changes, no new MCP tools.

**Tech Stack:** Python 3.11, pytest, networkx, torch (embeddings), vanilla JS (Cortex Console).

## Global Constraints

- Branch: `feat/external-findings-wave1` (already created; spec committed).
- No schema migrations.
- Tool-description budget: ≤1,600 chars per tool, ≤18,000 total (`tests/test_tool_consolidation.py`).
- Every enrichment degrades gracefully — failure/absence returns the un-enriched result, never an error.
- Test command: `python -m pytest tests/ -q` from repo root (PG-marked tests skip without a local Postgres; that is fine).
- Commit after each task; message style `feat(scope): …` / `test(scope): …`.

---

### Task 1: G — Betweenness god-node ranking

**Files:**
- Modify: `pseudolife_memory/memory/graph_insight.py` (`god_nodes` ~line 143, `suggest_questions` ~line 237, `build_digest` ~line 300)
- Modify: `pseudolife_memory/service.py` (`_refresh_graph_insight`, the `build_digest` call ~line 3414 — verify `betweenness_sample=cfg.betweenness_sample` is passed; add if missing)
- Modify: `pseudolife_memory/web/fixtures.py` (~line 368 — add `betweenness` to the god_nodes fixture rows)
- Test: `tests/test_graph_insight.py`

**Interfaces:**
- Produces: `god_nodes(edges, entities, *, top_n=10, exclude_etypes=(), betweenness_sample=200) -> list[dict]` where each item is `{"entity_id": int, "display": str, "degree": int, "betweenness": float}` ranked by betweenness desc, degree desc, id asc.
- Produces: module-private `_betweenness(g: nx.Graph, sample: int) -> dict[int, float]` (reused by `suggest_questions`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_insight.py`)

```python
def test_god_nodes_ranks_by_betweenness_over_degree():
    # Two triangles (1-2-3, 5-6-7) joined through bridge node 4.
    # Node 4 has degree 2 (lower than 3 and 5, which have degree 3) but the
    # highest betweenness: all 9 cross-triangle shortest paths pass through it.
    edges = [
        {"src_id": 1, "dst_id": 2}, {"src_id": 2, "dst_id": 3},
        {"src_id": 1, "dst_id": 3},
        {"src_id": 5, "dst_id": 6}, {"src_id": 6, "dst_id": 7},
        {"src_id": 5, "dst_id": 7},
        {"src_id": 3, "dst_id": 4}, {"src_id": 4, "dst_id": 5},
    ]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 8)]
    gods = gi.god_nodes(edges, entities, top_n=3)
    assert gods[0]["entity_id"] == 4
    assert gods[0]["degree"] == 2
    assert all("betweenness" in g for g in gods)
    bcs = [g["betweenness"] for g in gods]
    assert bcs == sorted(bcs, reverse=True)


def test_god_nodes_degree_tiebreak_when_betweenness_zero():
    # Two disjoint edges: every node has betweenness 0 -> fall back to
    # degree desc then id asc, so order is deterministic.
    edges = [{"src_id": 1, "dst_id": 2}, {"src_id": 3, "dst_id": 4}]
    entities = [{"id": i, "display": f"e{i}", "etype": None} for i in range(1, 5)]
    gods = gi.god_nodes(edges, entities, top_n=4)
    assert [g["entity_id"] for g in gods] == [1, 2, 3, 4]
    assert all(g["betweenness"] == 0.0 for g in gods)
```

Note: the existing `test_god_nodes_ranks_by_degree` (star graph) still passes —
the star hub also has top betweenness. Do not modify it.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_graph_insight.py -q -k god_nodes`
Expected: 2 failures — `KeyError: 'betweenness'` / wrong first item.

- [ ] **Step 3: Implement**

In `graph_insight.py`, add after `_undirected`:

```python
def _betweenness(g: nx.Graph, sample: int) -> dict[int, float]:
    """Betweenness centrality; k-sampled above ``sample`` nodes (0 = exact)."""
    if not g.number_of_edges():
        return {}
    k = sample if (sample and g.number_of_nodes() > sample) else None
    return nx.betweenness_centrality(g, k=k, seed=42)
```

Replace `god_nodes` with:

```python
def god_nodes(edges: list[dict], entities: list[dict], *, top_n: int = 10,
              exclude_etypes: tuple[str, ...] = (),
              betweenness_sample: int = 200) -> list[dict]:
    """Top connector entities. Ranked by betweenness centrality (bridges that
    hold communities together — their loss has the widest blast radius),
    with degree as tiebreak; ``degree`` is still reported per node."""
    deg = degree_counts(edges)
    bc = _betweenness(_undirected(edges), betweenness_sample)
    excluded = {e["id"] for e in entities if e.get("etype") in exclude_etypes}
    disp = {e["id"]: e["display"] for e in entities}
    ranked = sorted(
        ((eid, d) for eid, d in deg.items() if eid not in excluded),
        key=lambda kv: (-bc.get(kv[0], 0.0), -kv[1], kv[0]),
    )
    return [{"entity_id": eid, "display": disp.get(eid, str(eid)),
             "degree": d, "betweenness": round(bc.get(eid, 0.0), 4)}
            for eid, d in ranked[:top_n]]
```

In `suggest_questions`, replace the inline betweenness block (`k = betweenness_sample if …` / `bc = nx.betweenness_centrality(g, k=k, seed=42)`) with `bc = _betweenness(g, betweenness_sample)`.

In `build_digest`, pass the sample through: `"god_nodes": god_nodes(edges, entities, top_n=god_top_n, betweenness_sample=betweenness_sample),`.

In `service.py` `_refresh_graph_insight`, confirm the `build_digest(...)` call passes `betweenness_sample=cfg.betweenness_sample`; add it if absent.

In `web/fixtures.py`, add a plausible `"betweenness": 0.42`-style field to each god_nodes fixture row (keep existing fields).

- [ ] **Step 4: Run the full graph-insight + fixture tests**

Run: `python -m pytest tests/test_graph_insight.py tests/test_fixture_contract.py tests/test_graph.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_insight.py pseudolife_memory/service.py pseudolife_memory/web/fixtures.py tests/test_graph_insight.py
git commit -m "feat(graph-insight): rank god-nodes by betweenness centrality (bridges), degree tiebreak"
```

---

### Task 2: A — `memory_fact_get` ranked candidates on miss

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (add `candidates_for` after `search`, ~line 524)
- Modify: `pseudolife_memory/service.py` (add `cortex_candidates` after `cortex_contenders`, ~line 1355)
- Modify: `pseudolife_memory/mcp_server.py` (`memory_fact_get`, ~line 341)
- Test: `tests/test_cortex.py`, `tests/test_cortex_service.py`

**Interfaces:**
- Consumes: `CortexStore.search(query_embedding, top_k, min_score) -> list[tuple[CortexRecord, float]]` (exists), `CortexRecord.key -> (norm_entity, norm_attribute)` (exists).
- Produces: `CortexStore.candidates_for(entity, attribute, query_embedding=None, *, top_k=5, min_score=0.35) -> list[dict]`, items `{"entity", "attribute", "value", "score": float|None, "why": "same_entity"|"similar_slot"}`.
- Produces: `MemoryService.cortex_candidates(entity, attribute, top_k=5) -> list[dict]` (alias-aware, embeds the query, degrades to same-entity-only without an embedder).

- [ ] **Step 1: Write the failing unit tests** (append to `tests/test_cortex.py`; reuse the module's `_unit(seed)` helper and `Slot`)

```python
def test_candidates_for_same_entity_first_recency_ranked():
    s = CortexStore()
    s.write_fact(Slot("server", "port", "8080"), _unit(1), support="user", now=100.0)
    s.write_fact(Slot("server", "host", "hal9000"), _unit(2), support="user", now=200.0)
    s.write_fact(Slot("other", "port", "9090"), _unit(3), support="user", now=300.0)
    got = s.candidates_for("server", "os")  # empty slot, no embedding
    assert [(c["attribute"], c["why"]) for c in got[:2]] == [
        ("host", "same_entity"), ("port", "same_entity")]
    assert all(c["score"] is None for c in got if c["why"] == "same_entity")
    assert all(c["entity"] == "server" for c in got)


def test_candidates_for_similar_slot_via_embedding():
    s = CortexStore()
    e = _unit(7)
    s.write_fact(Slot("daemon", "version", "0.2.0"), e, support="user", now=100.0)
    s.write_fact(Slot("unrelated", "color", "blue"), -e, support="user", now=100.0)
    got = s.candidates_for("no such entity", "ver", query_embedding=e)
    assert got and got[0]["why"] == "similar_slot"
    assert got[0]["entity"] == "daemon" and got[0]["score"] > 0.9
    assert all(c["entity"] != "unrelated" for c in got)  # below 0.35 floor


def test_candidates_for_excludes_queried_slot_and_caps():
    s = CortexStore()
    for i in range(8):
        s.write_fact(Slot("srv", f"attr{i}", f"v{i}"), _unit(i), support="user",
                     now=float(i))
    got = s.candidates_for("srv", "attr0")
    assert len(got) == 5  # top_k cap
    assert all(c["attribute"] != "attr0" for c in got)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cortex.py -q -k candidates`
Expected: 3 failures — `AttributeError: 'CortexStore' object has no attribute 'candidates_for'`.

- [ ] **Step 3: Implement `candidates_for`** (in `cortex.py`, after `search`)

```python
    def candidates_for(
        self,
        entity: str,
        attribute: str,
        query_embedding: "torch.Tensor | None" = None,
        *,
        top_k: int = 5,
        min_score: float = 0.35,
    ) -> list[dict]:
        """Ranked nearby slots for an EMPTY slot lookup — leads, not answers.

        Same-entity current facts first (other attributes, most recently
        asserted/confirmed first, ``score=None``), then embedding-similar
        slots above ``min_score`` (``score`` = cosine). Never includes the
        queried slot itself.
        """
        key_ent, key_attr = _norm_key(entity), _norm_key(attribute)
        same = [r for r in self.current_records()
                if _norm_key(r.entity) == key_ent
                and _norm_key(r.attribute) != key_attr]
        same.sort(key=lambda r: -max(r.asserted_at, r.last_confirmed))
        out = [{"entity": r.entity, "attribute": r.attribute, "value": r.value,
                "score": None, "why": "same_entity"} for r in same[:top_k]]
        if query_embedding is not None and len(out) < top_k:
            seen = {r.key for r in same}
            seen.add((key_ent, key_attr))
            for rec, s in self.search(query_embedding, top_k=top_k * 2,
                                      min_score=min_score):
                if rec.key in seen:
                    continue
                seen.add(rec.key)
                out.append({"entity": rec.entity, "attribute": rec.attribute,
                            "value": rec.value, "score": round(float(s), 4),
                            "why": "similar_slot"})
                if len(out) >= top_k:
                    break
        return out
```

- [ ] **Step 4: Run unit tests**

Run: `python -m pytest tests/test_cortex.py -q -k candidates`
Expected: PASS.

- [ ] **Step 5: Write the failing service-level test** (append to `tests/test_cortex_service.py`, following that file's existing service fixture pattern for construction — copy how its first test builds the service)

```python
def test_fact_get_miss_returns_candidates(svc_fixture_name):
    # Follow the file's existing fixture; store one fact, query an empty
    # slot on the same entity, assert candidates surface.
    svc = svc_fixture_name
    svc.cortex_write("server", "port", "8080", origin="user")
    got = svc.cortex_candidates("server", "nonexistent-attr")
    assert got and got[0]["why"] == "same_entity"
    assert got[0]["attribute"] == "port"
```

(Adapt fixture/method names to the file's real pattern — `cortex_write`'s
signature is visible at `service.py` ~line 1310 region and in
`web/routes.py` `/api/facts/set`.)

- [ ] **Step 6: Implement `cortex_candidates`** (in `service.py`, after `cortex_contenders`)

```python
    def cortex_candidates(self, entity: str, attribute: str,
                          top_k: int = 5) -> list[dict]:
        """Ranked nearby slots for an empty-slot lookup (see
        ``CortexStore.candidates_for``). Alias-aware: same-entity candidates
        are collected for both the queried name and its canonical graph
        alias. Degrades to same-entity-only when the embedder is absent."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            names = [entity]
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(entity))
                if node is not None:
                    canon = node.get("canonical")
                    if canon and norm_name(canon) != norm_name(entity):
                        names.append(canon)
            emb = None
            if self._embedder is not None:
                emb = self._embedder.encode_single(f"{entity} {attribute}")
            out: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for name in names:
                for c in self._cortex.candidates_for(
                        name, attribute, emb, top_k=top_k):
                    k = (c["entity"].lower(), c["attribute"].lower())
                    if k not in seen:
                        seen.add(k)
                        out.append(c)
            out.sort(key=lambda c: (c["why"] != "same_entity",
                                    -(c["score"] or 1.0)))
            return out[:top_k]
```

- [ ] **Step 7: Wire into MCP** (in `mcp_server.py` `memory_fact_get`, after `out` is built and before `entity_ref`)

```python
    if out["record"] is None and not out["contenders"]:
        out["candidates"] = service.cortex_candidates(entity, attribute)
```

Append one sentence to the docstring: `On an empty slot, ``candidates`` lists nearby current slots (same entity first, then similar slots) — ranked leads, not the answer.`

- [ ] **Step 8: Run service tests + tool budget**

Run: `python -m pytest tests/test_cortex_service.py tests/test_tool_consolidation.py -q`
Expected: PASS (docstring growth stays within the 1,600-char/tool budget).

- [ ] **Step 9: Commit**

```bash
git add pseudolife_memory/memory/cortex.py pseudolife_memory/service.py pseudolife_memory/mcp_server.py tests/test_cortex.py tests/test_cortex_service.py
git commit -m "feat(cortex): memory_fact_get returns ranked candidates on empty-slot miss"
```

---

### Task 3: D — Edge provenance tags (EXTRACTED / INFERRED / AMBIGUOUS)

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py` (add `classify_edge`; tag `dubious_edges` rows ~line 59)
- Modify: `pseudolife_memory/service.py` (`graph_neighborhood` edge rows ~line 3345; `_whole_graph` edge comprehension ~line 3253; the edge-proposal rows in `graph_review` — locate with `grep -n "proposed_link" pseudolife_memory/service.py`)
- Modify: `pseudolife_memory/web/static/js/graphview.js` + `pseudolife_memory/web/static/js/atlas_review.js` (render the tag where edge confidence is shown — locate with `grep -n "confidence" <file>`)
- Test: `tests/test_graph_review.py`, `tests/test_graph.py`

**Interfaces:**
- Produces: `classify_edge(edge: dict, *, proposed: bool = False) -> str` returning `"EXTRACTED" | "AMBIGUOUS" | "INFERRED"`.
- Edge dicts in `/api/graph`, `memory_graph`, and review findings gain `"tag"`.

- [ ] **Step 1: Write the failing unit tests** (append to `tests/test_graph_review.py`)

```python
from pseudolife_memory.memory.graph_review import classify_edge


def test_classify_edge_extracted_wins_over_low_confidence():
    assert classify_edge({"origin": "user", "confidence": 0.2}) == "EXTRACTED"
    assert classify_edge({"origin": "action", "confidence": 0.9}) == "EXTRACTED"


def test_classify_edge_ambiguous_on_low_confidence_or_proposed():
    assert classify_edge({"origin": "agent", "confidence": 0.4}) == "AMBIGUOUS"
    assert classify_edge({"origin": "agent", "confidence": 0.9},
                         proposed=True) == "AMBIGUOUS"


def test_classify_edge_inferred_default():
    assert classify_edge({"origin": "agent", "confidence": 0.8}) == "INFERRED"
    assert classify_edge({"origin": None, "confidence": None}) == "INFERRED"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_graph_review.py -q -k classify_edge`
Expected: ImportError — `classify_edge` not defined.

- [ ] **Step 3: Implement** (in `graph_review.py`)

```python
_TAG_AMBIGUOUS_CONF = 0.5


def classify_edge(edge: dict, *, proposed: bool = False) -> str:
    """Three-way provenance tag for an edge (graphify-style).

    EXTRACTED — asserted by a human or a confirming action (origin
    user/action); never demoted by low confidence. AMBIGUOUS — a proposal
    awaiting review, or confidence below 0.5. INFERRED — everything else
    (agent/dream extraction at working confidence)."""
    origin = (edge.get("origin") or "").lower()
    if origin in ("user", "action"):
        return "EXTRACTED"
    conf = edge.get("confidence")
    if proposed or (conf is not None and float(conf) < _TAG_AMBIGUOUS_CONF):
        return "AMBIGUOUS"
    return "INFERRED"
```

Tag `dubious_edges` rows: add `"tag": "AMBIGUOUS"` to each row dict in
`dubious_edges` (they are agent-origin ≤ conf edges by construction).

- [ ] **Step 4: Attach in service responses**

In `graph_neighborhood` (non-derived branch of the edge row loop):

```python
                else:
                    row["confidence"] = round(float(e["confidence"]), 4)
                    if e.get("origin"):
                        row["origin"] = e["origin"]
                    row["tag"] = classify_edge(e)
```

In `_whole_graph`, add `"tag": classify_edge(e),` to the edge comprehension.
Import once at the top of `service.py`: `from pseudolife_memory.memory.graph_review import classify_edge` (follow the file's existing import placement style — check whether graph_review is already imported locally in methods; if the codebase style is method-local imports, import inside both methods instead).

Where edge-proposal rows are serialized for review findings (`grep -n "proposed_link" pseudolife_memory/service.py` and `pseudolife_memory/memory/graph_review.py`), add `"tag": "AMBIGUOUS"` (or `classify_edge(row, proposed=True)`).

- [ ] **Step 5: Response-shape test** (append to `tests/test_graph.py`, following its existing storage/service fixture pattern for graph tests)

```python
def test_graph_neighborhood_edges_carry_tag(existing_graph_fixture):
    # Follow the file's existing pattern for building a service with a
    # small graph; assert every non-derived edge row has a tag in
    # {"EXTRACTED", "INFERRED", "AMBIGUOUS"}.
    ...
```

(Write it against the file's real fixture — the PG-backed graph tests in
`tests/test_graph.py` show the construction; if only PG fixtures exist,
mark it with the same PG marker they use.)

- [ ] **Step 6: Console badge**

In `graphview.js` and `atlas_review.js`, wherever an edge's `confidence`/`origin` is rendered (tooltip / detail panel / review row), append the tag as a short badge, e.g. `` `${edge.tag || ""}` `` with a `tag-badge tag-extracted|tag-inferred|tag-ambiguous` CSS class; add the three classes to the stylesheet in `web/static/css/` (muted green / grey / amber). Keep it text-only if the existing UI has no badge pattern.

- [ ] **Step 7: Run the graph + review + fixture suites**

Run: `python -m pytest tests/test_graph_review.py tests/test_graph.py tests/test_fixture_contract.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py pseudolife_memory/service.py pseudolife_memory/web/static tests/test_graph_review.py tests/test_graph.py
git commit -m "feat(graph): EXTRACTED/INFERRED/AMBIGUOUS provenance tags on edges (API + Atlas)"
```

---

### Task 4: Integration pass — full suite, docs, changelog

**Files:**
- Modify: `CHANGELOG.md`, `README.md` (tools table blurbs for `memory_fact_get` and `memory_graph`; god-nodes description if mentioned)
- Test: whole suite

- [ ] **Step 1: Full suite**

Run: `python -m pytest tests/ -q`
Expected: everything green (PG-marked tests may skip without local Postgres).

- [ ] **Step 2: Docs**

- `CHANGELOG.md`: one entry under Unreleased — betweenness god-nodes; fact_get candidates on miss; edge provenance tags.
- `README.md`: update the `memory_fact_get` row (mention `candidates` on miss) and `memory_graph` row (mention edge `tag`) in the tools table; update any god-node description to the "bridges" framing.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md README.md
git commit -m "docs: wave-1 external findings (betweenness god-nodes, fact_get candidates, edge tags)"
```
