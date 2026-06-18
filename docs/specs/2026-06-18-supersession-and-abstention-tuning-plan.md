# Small-model supersession + tunable abstention guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let small/cheap dream extractors retire stale facts despite entity/attribute paraphrase, and make the cortex abstention guard a tunable config knob — both behaviour-preserving at their defaults.

**Architecture:** Two independent additive changes. (A) A *dream-path-only* slot resolver maps a paraphrased claim's `(entity, attribute)` onto an existing current slot by **value-free slot-embedding** cosine, so the normal exact-key write path supersedes instead of forking a sibling; the deliberate write path stays exact-keyed. Backed by a new persisted `CortexRecord.slot_embedding` (schema v8, additive, lazy-backfilled). (B) The hardcoded `min_score=0.3` cortex guard in `memory_search` becomes `CortexConfig.guard_min_score` (default `0.3` = today). Both calibrated via additive `evals/` sub-sweeps.

**Tech Stack:** Python 3, PyTorch tensors (cosine over injected embeddings), pytest (PG-backed fixtures skip cleanly without a server), dev-only `evals/ladder_sweep.py`.

**Spec:** [docs/specs/2026-06-18-supersession-and-abstention-tuning-design.md](2026-06-18-supersession-and-abstention-tuning-design.md)

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pseudolife_memory/utils/config.py` | config dataclasses | +2 `CortexConfig` fields (auto-parsed via `_dict_to_dataclass`) |
| `pseudolife_memory/memory/cortex.py` | slot-keyed store | `slot_embedding` field, schema v7→v8, `resolve_slot()`, save/load round-trip |
| `pseudolife_memory/service.py` | service driver | `cortex_write` passes slot embedding; `_resolve_dream_slot` helper; `dream_run` resolves before write |
| `pseudolife_memory/mcp_server.py` | MCP tool layer | guard `min_score` reads `cc.guard_min_score` |
| `evals/ladder_sweep.py` | dev-only sweep | guard axis on `--abstain`; new `--supersede` sub-sweep |
| `evals/README.md`, `README.md`, `CHANGELOG.md` | docs | knobs + sub-sweep findings |
| `tests/test_cortex.py` | cortex unit | config defaults, slot_embedding round-trip, `resolve_slot` |
| `tests/test_dream.py` | dream integration | paraphrase supersession (on) vs sibling fork (off) |
| `tests/test_abstain.py` | guard unit | `guard_min_score` passed through |

Order: config → store (schema + resolver) → service wiring → mcp guard → evals → calibration → docs → full verify. Each task is independently committable and TDD-first.

---

### Task 1: Config knobs (`CortexConfig.guard_min_score`, `dream_slot_match_threshold`)

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (`CortexConfig`, ~line 352-361)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_cortex.py`)

```python
def test_cortex_config_new_knobs_default_and_parse():
    from pseudolife_memory.utils.config import CortexConfig, _dict_to_dataclass
    c = CortexConfig()
    assert c.guard_min_score == 0.3            # default = today's hardcoded guard
    assert c.dream_slot_match_threshold == 0.0  # default = off (exact-key only)
    parsed = _dict_to_dataclass(
        CortexConfig,
        {"guard_min_score": 0.65, "dream_slot_match_threshold": 0.9},
    )
    assert parsed.guard_min_score == 0.65
    assert parsed.dream_slot_match_threshold == 0.9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cortex.py::test_cortex_config_new_knobs_default_and_parse -v`
Expected: FAIL (`AttributeError: 'CortexConfig' object has no attribute 'guard_min_score'`)

- [ ] **Step 3: Add the fields** to `CortexConfig` (after `reinforce_rate`):

```python
    reinforce_rate: float = 0.34
    # Cortex guard for memory_search abstention: a current fact must score >= this
    # to be surfaced (and to suppress low_confidence). Default 0.3 = today.
    guard_min_score: float = 0.3
    # Dream-path slot resolver: a paraphrased dreamed claim adopts an existing
    # current slot when its value-free slot embedding cosine >= this. <=0 disables
    # (exact-key only = today's behaviour). Positive = the cosine floor.
    dream_slot_match_threshold: float = 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cortex.py::test_cortex_config_new_knobs_default_and_parse -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py tests/test_cortex.py
git commit -m "feat(cortex): add guard_min_score + dream_slot_match_threshold config knobs"
```

---

### Task 2: Schema v8 — `CortexRecord.slot_embedding` + write/save/load round-trip

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (`SCHEMA_VERSION`, `CortexRecord`, `write_fact`, `_insert`, `_contend`, `save`, `load`)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cortex.py`)

```python
def test_write_fact_stores_slot_embedding():
    store = CortexStore()
    res = store.write_fact(Slot("svc", "port", "8080"), _unit(1), slot_embedding=_unit(7))
    assert res.record.slot_embedding is not None
    assert torch.allclose(res.record.slot_embedding, _unit(7))


def test_slot_embedding_round_trips_and_legacy_loads_none():
    from pseudolife_memory.memory.cortex import SCHEMA_VERSION
    assert SCHEMA_VERSION == 8
    store = CortexStore()
    store.write_fact(Slot("a", "b", "c"), _unit(1), slot_embedding=_unit(5))
    store.write_fact(Slot("d", "e", "f"), _unit(2))  # no slot_embedding (legacy-like)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cortex.pt"
        store.save(p)
        store2 = CortexStore()
        store2.load(p)
    r1 = store2.lookup("a", "b")
    r2 = store2.lookup("d", "e")
    assert r1.slot_embedding is not None and torch.allclose(r1.slot_embedding, _unit(5))
    assert r2.slot_embedding is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_cortex.py::test_write_fact_stores_slot_embedding tests/test_cortex.py::test_slot_embedding_round_trips_and_legacy_loads_none -v`
Expected: FAIL (`write_fact() got an unexpected keyword argument 'slot_embedding'`)

- [ ] **Step 3: Implement.** In `cortex.py`:

Bump the version:

```python
SCHEMA_VERSION = 8
```

Add the field to `CortexRecord` (after `embedding`):

```python
    embedding: torch.Tensor | None = None
    # Value-free slot embedding (entity+attribute) for paraphrase-robust dream
    # slot resolution; None on legacy (pre-v8) records, lazily backfilled.
    slot_embedding: torch.Tensor | None = None
```

In `write_fact`, after `emb = embedding.detach()...clone()` add and thread it through:

```python
        emb = embedding.detach().to("cpu", torch.float32).clone()
        semb = (slot_embedding.detach().to("cpu", torch.float32).clone()
                if slot_embedding is not None else None)
```

Add `slot_embedding: torch.Tensor | None = None` to the `write_fact` keyword args, pass `slot_embedding=semb` into **both** `_insert(...)` calls (the `inserted` branch and the `superseded` branch's `new = self._insert(...)`) and into `self._contend(...)`.

`_insert`: add `slot_embedding: torch.Tensor | None = None` param and set `slot_embedding=slot_embedding` on the `CortexRecord`.

`_contend`: add `slot_embedding=None` param, set it on the contender `CortexRecord`, and update the `write_fact` call site to pass `slot_embedding=semb`.

`save`: add to the per-record dict:

```python
                    "embedding": r.embedding,
                    "slot_embedding": r.slot_embedding,
```

`load`: add to the `CortexRecord(...)` construction:

```python
                embedding=d.get("embedding"),
                slot_embedding=d.get("slot_embedding"),
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_cortex.py -v`
Expected: PASS (all cortex tests, including the existing ones)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py
git commit -m "feat(cortex): persist value-free slot_embedding (schema v8, additive)"
```

---

### Task 3: `CortexStore.resolve_slot()` — value-free slot matcher

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (new method in the read-path section, near `search`)
- Test: `tests/test_cortex.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_cortex.py`)

```python
def test_resolve_slot_matches_above_threshold_only():
    store = CortexStore()
    store.write_fact(Slot("payments-db", "host", "x"), _unit(1), slot_embedding=_unit(10))
    store.write_fact(Slot("cache", "ttl", "y"), _unit(2), slot_embedding=_unit(20))
    store.write_fact(Slot("no-slotemb", "attr", "z"), _unit(3))  # slot_embedding None

    # exact-vector query clears a high floor -> adopt that slot
    assert store.resolve_slot(_unit(10), 0.9) == ("payments-db", "host")
    # dissimilar query (random unit vec) clears nothing -> None
    assert store.resolve_slot(_unit(99), 0.9) is None
    # threshold <= 0 disables resolution -> None
    assert store.resolve_slot(_unit(10), 0.0) is None
    # records without a slot_embedding are never matched
    assert store.resolve_slot(_unit(3), 0.9) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_cortex.py::test_resolve_slot_matches_above_threshold_only -v`
Expected: FAIL (`AttributeError: 'CortexStore' object has no attribute 'resolve_slot'`)

- [ ] **Step 3: Implement** `resolve_slot` (add after `search` in `cortex.py`):

```python
    def resolve_slot(
        self, slot_embedding: torch.Tensor, threshold: float,
    ) -> tuple[str, str] | None:
        """Best current slot whose value-free ``slot_embedding`` matches
        ``slot_embedding`` at cosine >= ``threshold`` — for paraphrase-robust dream
        resolution. Returns the canonical ``(entity, attribute)`` or ``None``.
        ``threshold <= 0`` disables (returns ``None``). Records without a stored
        slot embedding are ignored."""
        if threshold is None or float(threshold) <= 0.0:
            return None
        cands = [
            r for r in self.records
            if r.status == "current" and r.slot_embedding is not None
        ]
        if not cands:
            return None
        q = slot_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in cands])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        best = max(range(len(cands)), key=lambda i: sims[i])
        if sims[best] >= float(threshold):
            return (cands[best].entity, cands[best].attribute)
        return None
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_cortex.py::test_resolve_slot_matches_above_threshold_only -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/cortex.py tests/test_cortex.py
git commit -m "feat(cortex): resolve_slot value-free slot matcher"
```

---

### Task 4: Service wiring — slot embedding on write + dream-path resolver

**Files:**
- Modify: `pseudolife_memory/service.py` (`cortex_write` ~line 1008; new `_resolve_dream_slot`; `dream_run` loop ~line 1288)
- Test: `tests/test_dream.py`

> **Locking note:** `self._lock` is a plain `Lock()` (non-reentrant, [service.py:182](../../pseudolife_memory/service.py)). `_resolve_dream_slot` acquires the lock for its read+backfill and **releases it before** `dream_run` calls `cortex_write` (which re-acquires). Never nest the lock.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dream.py`, in the PG-backed section after `test_dream_run_promotes_and_advances_cursor`)

```python
class _StubExtractor:
    """Returns a fixed claim list regardless of input (drives dream_run)."""
    def __init__(self, claims):
        self._claims = claims
    def extract(self, texts, vocab):
        return [dict(c) for c in self._claims]


def test_dream_resolves_paraphrased_slot_and_supersedes(svc):
    svc.config.memory.cortex.dream_slot_match_threshold = 0.3  # on
    svc.store("payments-db host is db-prod-1", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments-db", "attribute": "host",
        "value": "db-prod-1", "confidence": 0.6, "origin": "agent"}]))
    svc.store("payments database host is db-prod-2", source="notes")
    out = svc.dream_run(_StubExtractor([{
        "entity": "payments database", "attribute": "host",
        "value": "db-prod-2", "confidence": 0.6, "origin": "agent"}]))
    # paraphrased entity resolved onto the existing slot -> supersede, not fork
    assert out["superseded"] >= 1
    cur = svc.cortex_lookup("payments-db", "host")
    assert cur is not None and "db-prod-2" in cur["value"]
    assert svc.cortex_lookup("payments database", "host") is None  # no sibling slot


def test_dream_threshold_off_forks_sibling(svc):
    svc.config.memory.cortex.dream_slot_match_threshold = 0.0  # off (default)
    svc.store("payments-db host is db-prod-1", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments-db", "attribute": "host",
        "value": "db-prod-1", "confidence": 0.6, "origin": "agent"}]))
    svc.store("payments database host is db-prod-2", source="notes")
    svc.dream_run(_StubExtractor([{
        "entity": "payments database", "attribute": "host",
        "value": "db-prod-2", "confidence": 0.6, "origin": "agent"}]))
    a = svc.cortex_lookup("payments-db", "host")
    b = svc.cortex_lookup("payments database", "host")
    assert a is not None and "db-prod-1" in a["value"]   # NOT superseded
    assert b is not None and "db-prod-2" in b["value"]   # separate sibling slot
```

(`payments-db` normalises to `payments-db`, `payments database` to `payments-database` — genuinely different exact keys, so the off-case truly forks.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_dream.py -k "resolves_paraphrased or threshold_off" -v`
Expected: FAIL — the off-case may pass, but `test_dream_resolves_paraphrased_slot_and_supersedes` fails (no resolver yet → sibling slot, `superseded == 0`, `cortex_lookup("payments database","host")` not None). (PG tests skip cleanly if no test server — if skipped, stand up the test PG per `tests/pg_fixtures.py` before proceeding.)

- [ ] **Step 3: Implement.**

In `cortex_write` ([service.py:1008](../../pseudolife_memory/service.py)), compute and pass the slot embedding:

```python
            claim = f"{entity} {attribute} {value}".strip()
            emb = self._embedder.encode_single(claim)
            slot_emb = self._embedder.encode_single(f"{entity} {attribute}".strip())
            res = self._cortex.write_fact(
                Slot(entity, attribute, value),
                emb,
                slot_embedding=slot_emb,
                confidence=confidence,
                provenance=provenance or (),
                support=support,
                now=now,
            )
```

Add the resolver method (place just above `dream_run`):

```python
    def _resolve_dream_slot(self, entity: str, attribute: str) -> tuple[str, str]:
        """Map a dreamed claim's (entity, attribute) onto an existing current slot
        when a confident value-free slot-embedding match exists, so a paraphrased
        update supersedes instead of forking a sibling. Dream-path only; returns
        the original pair when disabled, on an exact-key hit, or below threshold.
        Never raises — a resolver failure falls back to the original slot."""
        threshold = float(self.config.memory.cortex.dream_slot_match_threshold)
        if threshold <= 0.0:
            return entity, attribute
        try:
            with self._lock:
                self._ensure_init()
                assert self._embedder is not None and self._cortex is not None
                # Exact slot already exists -> let the normal write path supersede.
                if self._cortex.lookup(entity, attribute) is not None:
                    return entity, attribute
                # Lazy-backfill slot embeddings for current records that predate v8.
                for rec in self._cortex.current_records():
                    if rec.slot_embedding is None:
                        rec.slot_embedding = self._embedder.encode_single(
                            f"{rec.entity} {rec.attribute}".strip())
                slot_emb = self._embedder.encode_single(
                    f"{entity} {attribute}".strip())
                match = self._cortex.resolve_slot(slot_emb, threshold)
            return match or (entity, attribute)
        except Exception as exc:  # noqa: BLE001 — resolution must never break a dream
            logger.warning("dream slot resolve failed (%s); using literal slot", exc)
            return entity, attribute
```

In `dream_run` ([service.py:1288](../../pseudolife_memory/service.py)), resolve before each write:

```python
        for c in claims:
            ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
            res = self.cortex_write(
                ent, attr, c["value"],
                confidence=float(c.get("confidence", 0.55)),
                support=c.get("origin", "agent"),
            )
            tally[res["action"]] = tally.get(res["action"], 0) + 1
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_dream.py -v`
Expected: PASS (incl. existing dream tests — backfill persists via `cortex_write`'s `_save_cortex`)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): paraphrase-robust slot resolution before cortex write"
```

---

### Task 5: MCP guard knob — `memory_search` uses `cc.guard_min_score`

**Files:**
- Modify: `pseudolife_memory/mcp_server.py:244`
- Test: `tests/test_abstain.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_abstain.py`, reusing `_reload_mcp_filemode`)

```python
def test_guard_min_score_is_passed_through(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    captured = {}

    def fake_cortex_search(query, top_k=5, min_score=0.0):
        captured["min_score"] = min_score
        return {"entries": []}

    monkeypatch.setattr(mod.service, "search", lambda **kw: {
        "query": kw.get("query", ""), "count": 0, "entries": [],
        "low_confidence": False,
    })
    monkeypatch.setattr(mod.service, "cortex_search", fake_cortex_search)
    mod.service.config.memory.cortex.guard_min_score = 0.65
    mod.memory_search("anything")
    assert captured["min_score"] == 0.65
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_abstain.py::test_guard_min_score_is_passed_through -v`
Expected: FAIL (`captured["min_score"] == 0.3`, the hardcoded value)

- [ ] **Step 3: Implement.** In `mcp_server.py:244` change:

```python
        facts = service.cortex_search(query, top_k=5, min_score=cc.guard_min_score).get("entries", [])
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_abstain.py -v`
Expected: PASS (incl. the two existing guard tests)

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_abstain.py
git commit -m "feat(search): tunable cortex abstention guard (guard_min_score)"
```

---

### Task 6: `evals/` sub-sweeps — guard axis + supersession (dev-only)

**Files:**
- Modify: `evals/ladder_sweep.py` (`run_abstain` guard axis; new `run_supersede` + `measure_supersession`; argparse + dispatch + `report`)

No pytest (dev-only script, mirrors the existing `--abstain`/`--report` pattern). Verified by `--list` + a `floor`-rung smoke run.

- [ ] **Step 1: Add a must-not-merge distractor set** near `UNANSWERABLE`:

```python
# Same-entity/different-attribute and different-entity/same-attribute pairs that
# must remain DISTINCT slots after consolidation. A resolver false-merge collapses
# one of these onto the other -> measured as false_merge in the supersession sweep.
NO_MERGE = [
    {"a": ("payments-db", "host"), "b": ("payments-db", "password"),
     "a_text": "The payments-db host is db-prod-1.",
     "b_text": "The payments-db password is set in the vault under pdb-secret."},
    {"a": ("cache-layer", "engine"), "b": ("search-index", "engine"),
     "a_text": "Our cache-layer uses the redis engine.",
     "b_text": "The search-index uses the lucene engine."},
]
```

- [ ] **Step 2: Add `run_supersede`** (after `run_abstain`). It re-consolidates a fresh service per threshold (consolidation is stateful) and reports the win (`stale_leak` ↓, `superseded` ↑) against the cost (`false_merge`):

```python
def run_supersede(name: str, thresholds=(0.0, 0.80, 0.85, 0.90, 0.95)) -> dict:
    """Feature-A calibration: sweep dream_slot_match_threshold on a paraphrasing
    rung. Reports superseded / stale_leak (win) vs false_merge (cost)."""
    rung = RUNGS[name]
    if rung["kind"] == "llm" and not probe(rung["base_url"]):
        return {"rung": name, "status": "unreachable"}
    import tempfile
    curve = []
    for thr in thresholds:
        with tempfile.TemporaryDirectory(prefix=f"plsup_{name}_",
                                         ignore_cleanup_errors=True) as td:
            svc = build_service(Path(td))
            svc.config.memory.cortex.dream_slot_match_threshold = thr
            ingest(svc)
            for pair in NO_MERGE:                      # add the no-merge slots
                svc.store(pair["a_text"], source="bench")
                svc.store(pair["b_text"], source="bench")
            _, tally = consolidate(svc, make_extractor(rung))
            m = measure_cortex(svc)
            false_merge = 0
            for pair in NO_MERGE:
                a = svc.cortex_lookup(*pair["a"])
                b = svc.cortex_lookup(*pair["b"])
                if a is None or b is None:             # one slot vanished -> merged
                    false_merge += 1
            curve.append({
                "threshold": thr,
                "superseded": tally.get("superseded", 0),
                "stale_leak": m["stale_leak"],
                "gold_recoverable": m["gold_recoverable"],
                "false_merge": false_merge,
            })
    return {"rung": name, "status": "ok", "curve": curve}
```

- [ ] **Step 3: Add the guard axis to `run_abstain`.** Change the signature to add `guards=(0.3, 0.5, 0.65, 0.75, 0.85)`, wrap the existing floor loop in a guard loop, replace the two hardcoded `min_score=0.3` (lines ~415, ~424) with `min_score=g`, and add `"guard": g` to each `curve` row:

```python
def run_abstain(name: str, floors=(0.0, 0.5, 0.65, 0.70, 0.75, 0.80),
                guards=(0.3, 0.5, 0.65, 0.75, 0.85)) -> dict:
    ...
        curve = []
        for g in guards:
            for f in floors:
                svc.config.memory.search_confidence_floor = f
                abst = 0
                for _ent, _attr, q in UNANSWERABLE:
                    r = svc.search(q, top_k=TOP_K)
                    has_cortex = bool(
                        svc.cortex_search(q, top_k=5, min_score=g).get("entries"))
                    if r.get("low_confidence") and not has_cortex:
                        abst += 1
                wrong = 0
                for p in PAIRS:
                    r = svc.search(p["question"], top_k=TOP_K)
                    has_cortex = bool(
                        svc.cortex_search(p["question"], top_k=5,
                                          min_score=g).get("entries"))
                    if r.get("low_confidence") and not has_cortex:
                        wrong += 1
                curve.append({
                    "guard": g, "floor": f,
                    "abstain_recall_unanswerable": round(abst / len(UNANSWERABLE), 3),
                    "false_abstain_answerable": round(wrong / len(PAIRS), 3),
                })
    return {"rung": name, "status": "ok", "curve": curve}
```

- [ ] **Step 4: Wire argparse + dispatch + report.** In `main`: add `ap.add_argument("--supersede", choices=list(RUNGS), help="dream slot-match threshold sub-sweep")`, dispatch it (writing `results/supersede.json`, mirroring how `--abstain` writes `results/abstain.json`), and extend `report()` to print the supersession curve and the guard column in the abstention block.

- [ ] **Step 5: Smoke-verify (no hardware needed)**

Run: `PYTHONPATH=. python evals/ladder_sweep.py --list`
Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --supersede floor`
Expected: both run without error. The `floor` (regex) rung does not paraphrase, so `superseded` stays 0 and `false_merge` is 0 — this only proves the code path; real calibration is Task 7.

- [ ] **Step 6: Commit**

```bash
git add evals/ladder_sweep.py
git commit -m "test(evals): guard-axis abstain sweep + supersession sub-sweep"
```

---

### Task 7: Calibration runs + earn the defaults (HARDWARE-GATED)

**Requires the Gemma 4 E2B sidecar published on `127.0.0.1:8081` (see `evals/README.md` Prerequisites).** A paraphrasing model is required — the `floor` rung cannot exercise supersession. **Pause and confirm with the operator** that the sidecar is up before running; if unreachable, defer this task and ship Tasks 1–6/8 with defaults `guard_min_score=0.3`, `dream_slot_match_threshold=0.0` (off).

- [ ] **Step 1: Run the supersession sub-sweep**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --supersede gemma-e2b`

- [ ] **Step 2: Run the guard sub-sweep**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 python evals/ladder_sweep.py --abstain gemma-e2b`

- [ ] **Step 3: Pick defaults from the data.**
  - **Supersession:** lowest `threshold` that drives `stale_leak` down at **`false_merge == 0`**. If such a threshold exists, set `CortexConfig.dream_slot_match_threshold` default to it; else leave `0.0` (off) and document the recommended value.
  - **Guard:** the `(guard, floor)` pair maximising `abstain_recall_unanswerable` at `false_abstain_answerable ≈ 0`. Set `CortexConfig.guard_min_score` default if it beats `0.3` cleanly; else keep `0.3`.

- [ ] **Step 4: Update tests for any changed default** (Task 1's `test_cortex_config_new_knobs_default_and_parse` and Task 4's threshold-off test must still reflect the shipped defaults).

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_cortex.py tests/test_dream.py -v`
Expected: PASS

- [ ] **Step 6: Commit** (only if defaults changed)

```bash
git add pseudolife_memory/utils/config.py tests/
git commit -m "feat(cortex): set calibrated supersession/guard defaults from evals sweep"
```

---

### Task 8: Docs — README, CHANGELOG, evals/README

**Files:** `README.md`, `CHANGELOG.md`, `evals/README.md`

- [ ] **Step 1: `CHANGELOG.md`** — under `[Unreleased] → Added`:
  - "Paraphrase-robust dream supersession: a dream-path slot resolver maps a paraphrased claim onto its existing slot via a value-free slot embedding, so small extractors retire stale values (config `memory.cortex.dream_slot_match_threshold`, default off). Schema v8 adds an additive, lazy-backfilled `slot_embedding`."
  - "Tunable cortex abstention guard: `memory.cortex.guard_min_score` (default `0.3` = prior behaviour) sets the score a current fact must clear to count as an answer and suppress `low_confidence`."

- [ ] **Step 2: `README.md`** — in the cortex/config section, document both `memory.cortex` knobs (purpose, default, that defaults preserve current behaviour) and point calibration at `evals/`.

- [ ] **Step 3: `evals/README.md`** — add the `--supersede` sub-sweep (metrics: `superseded`, `stale_leak`, `false_merge`; how to read it) and the guard axis on `--abstain`; record the Task-7 findings if run.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md evals/README.md
git commit -m "docs: supersession resolver + tunable abstention guard"
```

---

### Task 9: Full verification

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all green (PG-backed tests pass with a test server, or skip cleanly without). No regressions in `test_cortex*.py`, `test_dream.py`, `test_abstain.py`, `test_mcp_server.py`.

- [ ] **Step 2: Confirm behaviour-preserving defaults.** With no config overrides: `dream_slot_match_threshold` is off (or the calibrated value from Task 7) and `guard_min_score == 0.3`. A fresh-bank dream and a `memory_search` behave exactly as before the change unless the operator opts in.

- [ ] **Step 3: Final commit / branch is ready for the finishing-a-development-branch skill.**

---

## Notes for the implementer
- **DRY:** cosine logic mirrors `CortexStore.search`; reuse the same normalise idiom.
- **YAGNI:** no per-call `min_score` override on the tool, no alias-graph resolution, no re-embedding of superseded records — all deferred in the spec §9.
- **Safety:** schema v8 is additive (None default, lazy backfill) — no destructive migration; legacy banks load unchanged. The resolver and guard are both no-ops at their default values.
- **TDD:** every behaviour change has a failing test first. The dream integration tests need the test Postgres (`tests/pg_fixtures.py`); they skip cleanly without it but should be run against a server before merge.
