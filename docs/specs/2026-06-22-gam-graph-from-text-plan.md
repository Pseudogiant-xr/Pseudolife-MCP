# GAM #2 graph-from-text — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the dream to extract `(src, relation, dst)` triples from recent memory text and write them into the `GraphStore`-backed graph, so `memory_graph` works on ingested free text.

**Architecture:** A new **separate** `extract_relations()` extractor call (the bench winner over a combined facts+relations call: Gemma E2B F1 0.75 vs 0.54) returns `RelationClaim`s; the dream resolves them against the closed relation registry (`related-to` fallback), resolves entities alias-aware (pinned to the Postgres hub), and upserts edges via `self._graph.upsert_edge` with `origin="agent"`. Dream-only (single-writer), populate-only (no retrieval changes), best-effort (relation failures never break fact consolidation).

**Tech Stack:** Python 3.11+, Postgres (pgvector), psycopg, NetworkX, FastMCP, pytest.

## Global Constraints

- Work on branch `feat/gam-graph-from-text` (already checked out; spec + bench committed there).
- Run tests with: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest` (offline env required for determinism).
- PG-backed tests use the `pg_conn`/`pg_url`/`svc` fixtures (separate `pseudolife_memory_test` DB; safe). Skip cleanly when no test PG is up.
- Single-writer: graph-from-text writes ONLY from the dream, under the service's coarse lock. The LLM call itself runs OUTSIDE the lock (it's a slow network call); only the DB writes are locked.
- Closed vocab: extracted relations resolve via `graph.resolve_relation`; unknown → `related-to` (kept, never coined as a new predicate). No registry expansion (bench: related-to share 0.05).
- `related-to` is the catch-all; the extractor prompt lists the 8 graph builtins (NOT `prefers`/`avoids`, which are lesson-only).
- No new third-party dependencies.
- End every commit message with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

### Task 1: `extract_relations()` + `RelationClaim` (the winning "separate" shape)

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (add `RelationClaim`, the relations prompt, and `OpenAICompatExtractor.extract_relations`)
- Test: `tests/test_dream.py`

**Interfaces:**
- Consumes: existing `ExtractorError`, `OpenAICompatExtractor` (`self.base_url`/`self.model`/`self.api_key`/`self.max_tokens`/`self.timeout`).
- Produces:
  - `RelationClaim = TypedDict("src": str, "relation": str, "dst": str, "confidence": float)`.
  - `OpenAICompatExtractor.extract_relations(self, texts: list[str], relations: list[tuple[str, str]]) -> list[RelationClaim]` — `relations` is `(name, description)` pairs seeding the prompt. Raises `ExtractorError` on call/parse failure; returns `[]` on a genuine empty.

- [ ] **Step 1: Write the failing parse test**

In `tests/test_dream.py`, add a relations payload helper next to `_chat_payload` and two tests:

```python
def _chat_relations_payload(relations):
    return json.dumps({"choices": [{"message": {
        "content": json.dumps({"relations": relations})}}]})


def test_openai_extractor_parses_relations():
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    payload = _chat_relations_payload([
        {"src": "checkout-service", "relation": "runs-on", "dst": "host-1",
         "confidence": 0.8}])
    with _stub_server(lambda: (200, payload)) as base_url:
        rels = OpenAICompatExtractor(base_url, "m").extract_relations(
            ["whatever"], [("runs-on", "src executes on host dst")])
    assert rels == [{"src": "checkout-service", "relation": "runs-on",
                     "dst": "host-1", "confidence": 0.8}]


def test_openai_extractor_relations_raises_on_malformed():
    from pseudolife_memory.memory.dream import ExtractorError, OpenAICompatExtractor

    bad = json.dumps({"choices": [{"message": {"content": "not json"}}]})
    with _stub_server(lambda: (200, bad)) as base_url:
        with pytest.raises(ExtractorError):
            OpenAICompatExtractor(base_url, "m").extract_relations(
                ["x"], [("runs-on", "d")])
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py::test_openai_extractor_parses_relations -v`
Expected: FAIL with `AttributeError: 'OpenAICompatExtractor' object has no attribute 'extract_relations'`.

- [ ] **Step 3: Implement in `dream.py`**

Add the `RelationClaim` TypedDict next to `LessonClaim`:

```python
class RelationClaim(TypedDict):
    src: str
    relation: str
    dst: str
    confidence: float
```

Add the prompt builder near `_LESSON_SYSTEM_PROMPT`:

```python
_RELATIONS_PROMPT_HEAD = (
    "You extract durable RELATIONSHIPS between named entities from notes, as "
    'JSON: {"relations":[{"src":..,"relation":..,"dst":..}]}. Use ONLY these '
    "relation names:\n"
)
_RELATIONS_PROMPT_TAIL = (
    "\nIf a real connection fits none of the specific ones, use 'related-to'. "
    "src and dst are entity names (services, hosts, tools, components). Skip "
    "opinions, chit-chat, and anything with no entity-to-entity relationship. "
    'Return {"relations":[]} if nothing qualifies.'
)


def _relations_prompt(relations: list[tuple[str, str]]) -> str:
    body = "\n".join(f"- {n}: {d}" for n, d in relations)
    return _RELATIONS_PROMPT_HEAD + body + _RELATIONS_PROMPT_TAIL
```

Add the method on `OpenAICompatExtractor` (after `extract_lessons`):

```python
    def extract_relations(self, texts: list[str],
                          relations: list[tuple[str, str]]) -> list[RelationClaim]:
        """Extract (src, relation, dst) triples from ``texts`` via the same
        endpoint. ``relations`` are (name, description) pairs seeding the closed
        vocabulary. Raises ExtractorError on failure (vs a genuine empty [])."""
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _relations_prompt(relations)},
                    {"role": "user", "content": "\n\n".join(texts)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("relations", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"extract_relations failed: {exc}") from exc
        out: list[RelationClaim] = []
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            src = str(r.get("src", "")).strip()
            rel = str(r.get("relation", "")).strip()
            dst = str(r.get("dst", "")).strip()
            if not (src and rel and dst):
                continue
            try:
                conf = max(0.0, min(1.0, float(r.get("confidence", 0.6))))
            except (TypeError, ValueError):
                conf = 0.6
            out.append(RelationClaim(src=src, relation=rel, dst=dst,
                                     confidence=conf))
        return out
```

- [ ] **Step 4: Run to verify pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py -k relations -v`
Expected: both `..._parses_relations` and `..._relations_raises_on_malformed` PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/dream.py tests/test_dream.py
git commit -m "feat(dream): extract_relations() + RelationClaim (separate relations call)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: config + `_link_dream_relations` writer + `_dream_extract_relations` helper

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (`DreamConfig`: add two fields)
- Modify: `pseudolife_memory/service.py` (add `_link_dream_relations` + `_dream_extract_relations`)
- Test: `tests/test_dream.py`

**Interfaces:**
- Consumes: `RelationClaim`-shaped dicts from Task 1; `graph.norm_name` / `graph.resolve_relation`; `self._graph.load_relations()` / `self._graph.upsert_edge`; `self._resolve_or_create_entity`; `self.config.memory.dream`.
- Produces:
  - `DreamConfig.extract_relations: bool = True`, `DreamConfig.relation_confidence: float = 0.6`.
  - `MemoryService._link_dream_relations(self, relations: list[dict]) -> int` — caller holds the lock; resolves vocab + entities, drops self-loops, upserts edges; returns edges written.
  - `MemoryService._dream_extract_relations(self, extractor, texts: list[str]) -> int` — gated + best-effort; runs the LLM call unlocked, the writes locked; returns edges written (0 on disabled/no-fn/failure).

- [ ] **Step 1: Write the failing test**

```python
class _RelStubExtractor:
    """Stub extractor exposing extract + extract_relations for dream tests."""
    def __init__(self, claims=None, relations=None, fail_relations=False):
        self._claims = claims or []
        self._relations = relations or []
        self._fail = fail_relations
    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]
    def extract_relations(self, texts, relations):
        if self._fail:
            from pseudolife_memory.memory.dream import ExtractorError
            raise ExtractorError("boom")
        return [dict(r) for r in self._relations]


def test_dream_extract_relations_populates_graph(svc):
    n = svc._dream_extract_relations(_RelStubExtractor(relations=[
        {"src": "checkout-service", "relation": "runs_on", "dst": "host-1"},
        {"src": "Acme", "relation": "no-such-rel", "dst": "Beta"},   # -> related-to
        {"src": "loop", "relation": "uses", "dst": "loop"},          # self-loop dropped
    ]), ["some text"])
    assert n == 2
    g = svc.graph_neighborhood("checkout-service", depth=1)
    edges = {(e["src"], e["relation"], e["dst"]) for e in g["edges"]}
    assert ("checkout-service", "runs-on", "host-1") in edges  # normalized relation
    g2 = svc.graph_neighborhood("acme", depth=1)
    assert any(e["relation"] == "related-to" for e in g2["edges"])  # fallback kept


def test_dream_extract_relations_failure_is_isolated(svc):
    # A relations failure must not raise — returns 0, leaves the dream intact.
    assert svc._dream_extract_relations(
        _RelStubExtractor(relations=[], fail_relations=True), ["x"]) == 0


def test_dream_extract_relations_disabled(svc):
    svc.config.memory.dream.extract_relations = False
    assert svc._dream_extract_relations(_RelStubExtractor(relations=[
        {"src": "a-svc", "relation": "uses", "dst": "b-svc"}]), ["x"]) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py::test_dream_extract_relations_populates_graph -v`
Expected: FAIL with `AttributeError: ... '_dream_extract_relations'` (skips if no test PG — start the compose PG).

- [ ] **Step 3: Add the config fields**

In `pseudolife_memory/utils/config.py`, `DreamConfig` (after `extractor_timeout_seconds`, ~line 301):

```python
    # GAM #2 graph-from-text: the dream also extracts (src,relation,dst) triples
    # into the graph (separate extract_relations call — the bench winner). Edges
    # are dream-inferred, so a modest confidence below explicit graph_relate (0.8)
    # and lessons (0.7).
    extract_relations: bool = True
    relation_confidence: float = 0.6
```

- [ ] **Step 4: Add the writer + helper in `service.py`**

Add both methods next to `_link_lesson_graph` (~line 1359):

```python
    def _link_dream_relations(self, relations: list[dict]) -> int:
        """Upsert dream-extracted (src,relation,dst) edges. Closed-vocab
        (resolve_relation; unknown -> related-to), entities resolved alias-aware
        and pinned to the Postgres hub, self-loops dropped, origin='agent'.
        Caller holds the lock; no-op in file mode. Returns edges written."""
        if self._storage is None or not relations:
            return 0
        from pseudolife_memory import graph as G
        known = [r["name"] for r in self._graph.load_relations()]
        conf = float(self.config.memory.dream.relation_confidence)
        n = 0
        for r in relations:
            raw_src, raw_dst = str(r.get("src", "")), str(r.get("dst", ""))
            src_n, dst_n = G.norm_name(raw_src), G.norm_name(raw_dst)
            if not src_n or not dst_n or src_n == dst_n:
                continue
            resolved, _ = G.resolve_relation(known, str(r.get("relation", "")))
            relation = resolved or "related-to"
            src_e = self._resolve_or_create_entity(raw_src)
            dst_e = self._resolve_or_create_entity(raw_dst)
            self._graph.upsert_edge(src_e["id"], relation, dst_e["id"],
                                    confidence=conf, origin="agent")
            n += 1
        return n

    def _dream_extract_relations(self, extractor, texts: list[str]) -> int:
        """Gated, best-effort graph-from-text for one dream batch: run the LLM
        relations call UNLOCKED (slow network), then write edges LOCKED. A
        failure logs and returns 0 — it must never break fact consolidation or
        drop claims (relations are best-effort, like lessons)."""
        cfg = self.config.memory.dream
        rel_fn = getattr(extractor, "extract_relations", None)
        if not (cfg.extract_relations and rel_fn is not None and texts):
            return 0
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return 0
                registry = [(r["name"], r["description"])
                            for r in self._graph.load_relations()
                            if r["name"] not in ("prefers", "avoids")]
            rels = rel_fn(texts, registry)
            with self._lock:
                return self._link_dream_relations(rels)
        except Exception as exc:  # noqa: BLE001 — best-effort; never break the dream
            logger.warning("dream relation extraction failed (%s); claims kept",
                           exc)
            return 0
```

- [ ] **Step 5: Run to verify pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py -k "extract_relations or relations_failure or relations_disabled" -v`
Expected: the three new tests PASS.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): _link_dream_relations writer + gated best-effort helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: wire into `dream_run`

**Files:**
- Modify: `pseudolife_memory/service.py` (`dream_run`, ~line 1622)
- Test: `tests/test_dream.py`

**Interfaces:**
- Consumes: `self._dream_extract_relations` (Task 2); the existing `dream_run` body.
- Produces: `dream_run` return dict gains a `"relations": int` key; relations populated after `dream_commit`.

- [ ] **Step 1: Write the failing integration test**

```python
def test_dream_run_populates_relations_end_to_end(svc):
    svc.store("checkout-service runs on host-1 and uses redis", source="notes")
    out = svc.dream_run(_RelStubExtractor(
        claims=[{"entity": "checkout-service", "attribute": "role",
                 "value": "payments", "confidence": 0.6}],
        relations=[{"src": "checkout-service", "relation": "runs-on",
                    "dst": "host-1"},
                   {"src": "checkout-service", "relation": "uses",
                    "dst": "redis"}]))
    assert out["claims"] == 1
    assert out["relations"] == 2
    g = svc.graph_neighborhood("checkout-service", depth=1)
    edges = {(e["src"], e["relation"], e["dst"]) for e in g["edges"]}
    assert ("checkout-service", "runs-on", "host-1") in edges
    assert ("checkout-service", "uses", "redis") in edges


def test_dream_run_relations_failure_keeps_claims(svc):
    svc.store("the relay port is 4001", source="notes")
    out = svc.dream_run(_RelStubExtractor(
        claims=[{"entity": "relay", "attribute": "port", "value": "4001",
                 "confidence": 0.6}],
        fail_relations=True))
    assert out["claims"] == 1 and out["relations"] == 0     # claim kept
    assert svc.cortex_lookup("relay", "port") is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py::test_dream_run_populates_relations_end_to_end -v`
Expected: FAIL (`KeyError: 'relations'`).

- [ ] **Step 3: Wire `dream_run`**

In `service.py` `dream_run`, after `self.dream_commit(newest)` and before `lessons = self.synthesize_lessons(extractor)`, add the relations call; then add `relations_n` to the return dict:

```python
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        relations_n = self._dream_extract_relations(extractor, texts)
        lessons = self.synthesize_lessons(extractor)
        return {"pulled": len(entries), "claims": len(claims),
                "cursor": newest, "relations": relations_n, **tally,
                "lessons": lessons}
```

(The empty-entries early-return branch is unchanged — no texts, no relations.)

- [ ] **Step 4: Run to verify pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py -k "dream_run_populates_relations or relations_failure_keeps" -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): wire graph-from-text relations into dream_run

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: full suite + end-to-end multi-hop verification

**Files:**
- Test: `tests/test_dream.py` (one multi-hop integration test)

**Interfaces:**
- Consumes: everything above.
- Produces: a test proving the dream-populated graph answers a multi-hop query (the Tier-B capability) via derived transitive edges.

- [ ] **Step 1: Write the multi-hop test**

```python
def test_dream_relations_enable_multihop(svc):
    # depends-on is transitive: A->B->C should yield a DERIVED A->C edge,
    # i.e. multi-hop works on graph populated purely from ingested text.
    svc.store("mobile-app depends on graphql-gateway; graphql-gateway "
              "depends on user-service", source="notes")
    svc.dream_run(_RelStubExtractor(relations=[
        {"src": "mobile-app", "relation": "depends-on", "dst": "graphql-gateway"},
        {"src": "graphql-gateway", "relation": "depends-on", "dst": "user-service"}]))
    g = svc.graph_neighborhood("mobile-app", depth=3)
    derived = {(e["src"], e["dst"]) for e in g["edges"] if e["derived"]}
    assert ("mobile-app", "user-service") in derived  # transitive multi-hop
```

- [ ] **Step 2: Run the new test**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_dream.py::test_dream_relations_enable_multihop -v`
Expected: PASS.

- [ ] **Step 3: Run the whole suite**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest`
Expected: PASS (the known `test_retire_by_writer_supersedes_only_that_writer` PG-lock flake may appear; it's pre-existing and passes in isolation).

- [ ] **Step 4: Commit**

```bash
git add tests/test_dream.py
git commit -m "test(dream): multi-hop over text-populated graph (Tier-B capability)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Out of scope / follow-ons (NOT in this plan)

- **Live deploy** of graph-from-text to the running daemon (rebuild + recreate) — gated/outward-facing, run separately with the user (mirrors GAM #1 Task 4).
- **Tier-B multi-hop benchmark on realistic ingested data** + recording the live `related-to` share — best measured post-deploy against the real bank; the unit-level multi-hop test (Task 4) proves the capability.
- **Relation prompt-tuning** to close the pair-recall→F1 gap (0.85→0.75 = relation mislabeling), re-measured via `evals/relations_bench.py`.
- Auto-surfacing graph neighbors in retrieval/prefetch (the deferred slice); the two-tier episodic/semantic GAM (#3).

## Success criteria (from the spec)

1. `evals/relations_bench.py` ran; `separate` won on the Gemma floor (DONE — recorded in the design spec).
2. The dream populates graph edges from text — `memory_graph` returns ingested relations + derived edges (Task 4 multi-hop test).
3. Full suite green; relations failure-isolation covered (Tasks 2-3).
4. (Post-deploy follow-on) Tier-B multi-hop recall lifts above the pre-change baseline on the live bank.
