# Relation-Extraction Eval (Phase 2 / B1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dev-only benchmark that scores the dream relation-extraction path on a hand-labeled corpus (so "is the model strong enough at relations?" becomes a number), plus two `graph_review.py` analyzer fixes.

**Architecture:** A standalone `evals/relation_extraction_bench.py` (corpus + pure scorer + CLI) that calls each extractor rung's `extract_relations` directly — no DB, no embedder. It reuses `ladder_sweep`'s rung registry/probe/extractor-factory, the builtin relation vocab, and `graph.norm_name`. The Opus-4.8 ceiling rung is produced in-session by subagents (a `--emit-prompts` artifact fed to them), not an API call. Two `graph_review.py` regex/tokenizer fixes are independent pure-code changes.

**Tech Stack:** Python 3.10+, stdlib only (no new deps). `pytest` for the scorer + analyzer-fix unit tests. Reuses `pseudolife_memory.memory.dream`, `pseudolife_memory.storage.postgres._BUILTIN_RELATIONS`, `pseudolife_memory.graph.norm_name`, and `evals/ladder_sweep.py`.

## Global Constraints

- **No new dependencies** — stdlib + what `pseudolife_memory` already imports. The bench, like `memcot_bench.py`/`lesson_synthesis_bench.py`, is **dev-only**: not added to the installed package, not run by default in CI. Its *pure* parts (scorer, corpus integrity) ARE unit-tested under `tests/`.
- **Do NOT modify the live extractor path:** no changes to `dream.py` prompts, `OpenAICompatExtractor`, `_link_dream_relations`, or `extract_relations=True`. This spec only *measures*.
- **Score the raw model triples**, not the post-storage graph (isolate extraction from the linking layer).
- **Reuse, don't duplicate:** import `RUNGS`, `probe`, `make_extractor` from `ladder_sweep`; `_relations_prompt` + the `extract_relations` shape from `dream`; `_BUILTIN_RELATIONS` from `storage.postgres`; `norm_name` from `graph`.
- **UTF-8 stdout:** call `sys.stdout.reconfigure(encoding="utf-8")` in `main()` (table glyphs + unicode entity names on Windows), guarded in `try/except` like `ladder_sweep`.
- **Eval-module import pattern (for tests):** `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))` then `import relation_extraction_bench as rb` (there is no `evals/__init__.py`).
- **Entity match is alias-aware via `norm_name`**; relation match is exact.

---

### Task 1: Fix `_token_set` so version/number tokens stop collapsing into false duplicates

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py:21-22`
- Test: `tests/test_graph_review.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `duplicate_candidates(entities, *, min_jaccard=0.6)` behavior change — entities differing only in a numeric/version token (`schema v8` vs `schema 11`) no longer flagged; genuine phrasing dups still flagged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_review.py`:

```python
from pseudolife_memory.memory.graph_review import duplicate_candidates, test_artifacts


def _ents(*names):
    return [{"id": i, "display": n} for i, n in enumerate(names)]


def _pairs(findings):
    return {frozenset(f["entities"]) for f in findings}


def test_version_and_phase_numbers_not_collapsed():
    ents = _ents("schema v8", "schema 11", "schema 15->16",
                 "Phase 1 plan", "Phase 2 plan",
                 "Atlas Stage 1", "Atlas Stage 2")
    assert duplicate_candidates(ents) == []


def test_genuine_phrasing_duplicate_still_flagged():
    ents = _ents("memcot_bench.py", "memcot bench")
    assert frozenset({"memcot_bench.py", "memcot bench"}) in _pairs(duplicate_candidates(ents))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_graph_review.py::test_version_and_phase_numbers_not_collapsed -v`
Expected: FAIL — `schema v8`/`schema 11` (and the others) currently collapse to one token and are flagged, so `duplicate_candidates(ents)` is non-empty.

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/memory/graph_review.py`, change `_token_set` (currently drops every token of length ≤ 2, discarding `v8`, `11`, `1`, `2`):

```python
def _token_set(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower())
            if len(t) > 2 or any(c.isdigit() for c in t)}
```

(Now `v8`, `11`, `1`, `2`, `4` survive, so `schema v8`={schema,v8} vs `schema 11`={schema,11} → Jaccard 0.33; `Phase 1 plan` vs `Phase 2 plan` → 0.5; both below the 0.6 threshold. `memcot_bench.py`={memcot,bench} vs `memcot bench`={memcot,bench} → 1.0, still flagged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_graph_review.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py tests/test_graph_review.py
git commit -m "fix(graph): keep version/number tokens in dup detector to stop schema-vN false positives"
```

---

### Task 2: Tighten `_TEST_PATTERNS` so legit fixtures/lessons stop matching

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py:12-14`
- Test: `tests/test_graph_review.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: `test_artifacts(entities)` behavior change — `fixture devserver` and the TDD lesson no longer flagged; real `deploy-smoke-*`/`pl-healthcheck-*`/`payments/payments-db` still flagged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph_review.py`:

```python
def test_legit_fixtures_and_lessons_not_flagged():
    ents = _ents("fixture devserver",
                 "TDD pattern: PG service test + fixture stubs + web routes")
    assert test_artifacts(ents) == []


def test_real_test_artifacts_still_flagged():
    ents = _ents("deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db",
                 "Cortex Console")  # a normal entity, must NOT be flagged
    out = test_artifacts(ents)
    assert out and set(out[0]["entities"]) == {
        "deploy-smoke-foo", "pl-healthcheck-probe", "payments/payments-db"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_graph_review.py::test_legit_fixtures_and_lessons_not_flagged -v`
Expected: FAIL — both `fixture devserver` and the TDD lesson match the current `\bfixture\b` alternative.

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/memory/graph_review.py`, replace `_TEST_PATTERNS` (drop the over-broad `\btest-`, `-test\b`, `\bfixture\b` alternatives; keep the specific artifact shapes):

```python
_TEST_PATTERNS = re.compile(
    r"(payments?[-/]|pl-healthcheck|deploy-smoke|smoke[-_]?test|noise[ _-]?agent)",
    re.I)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_graph_review.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py tests/test_graph_review.py
git commit -m "fix(graph): stop test-artifact regex flagging legit fixture/test prose"
```

---

### Task 3: Bench data module — entities, corpus, constraints, relation registry

**Files:**
- Create: `evals/relation_extraction_bench.py`
- Test: `tests/test_relation_bench.py` (create)

**Interfaces:**
- Consumes: `pseudolife_memory.storage.postgres._BUILTIN_RELATIONS`, `pseudolife_memory.graph.norm_name`.
- Produces (used by later tasks): module-level `ENTITIES: dict[str, dict]`, `CORPUS: list[dict]`, `RELATION_CONSTRAINTS: dict[str, tuple[set, set]]`, `RELATION_REGISTRY: list[tuple[str, str]]`, and helpers `alias_index(entities) -> dict[str, str]`, `resolve(name, idx) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_relation_bench.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import relation_extraction_bench as rb  # noqa: E402

_STRUCTURAL = {"runs-on", "hosts", "stores-data-in", "part-of"}
_VOCAB = {n for n, _d in rb.RELATION_REGISTRY}


def test_corpus_endpoints_are_known_entities():
    for note in rb.CORPUS:
        for src, _rel, dst in note["edges"]:
            assert src in rb.ENTITIES, src
            assert dst in rb.ENTITIES, dst


def test_corpus_edges_use_closed_vocab():
    for note in rb.CORPUS:
        for _src, rel, _dst in note["edges"]:
            assert rel in _VOCAB, rel


def test_gold_edges_satisfy_type_constraints():
    idx = rb.alias_index(rb.ENTITIES)
    for note in rb.CORPUS:
        for src, rel, dst in note["edges"]:
            if rel in rb.RELATION_CONSTRAINTS:
                src_ok, dst_ok = rb.RELATION_CONSTRAINTS[rel]
                assert rb.ENTITIES[src]["type"] in src_ok, (src, rel)
                assert rb.ENTITIES[dst]["type"] in dst_ok, (rel, dst)


def test_corpus_covers_all_four_classes():
    has_null = any(note["edges"] == [] for note in rb.CORPUS)
    has_structural = any(note["edges"] for note in rb.CORPUS)
    assert has_null and has_structural
    assert len(rb.CORPUS) >= 15


def test_relation_registry_excludes_lesson_relations():
    names = {n for n, _ in rb.RELATION_REGISTRY}
    assert "prefers" not in names and "avoids" not in names
    assert "related-to" in names and "runs-on" in names


def test_resolve_is_alias_aware():
    idx = rb.alias_index(rb.ENTITIES)
    assert rb.resolve("the daemon", idx) == "pseudolife-daemon"
    assert rb.resolve("PG", idx) == "postgres"
    assert rb.resolve("nonexistent thing", idx) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relation_bench.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'relation_extraction_bench'`.

- [ ] **Step 3: Write minimal implementation**

Create `evals/relation_extraction_bench.py`:

```python
#!/usr/bin/env python
"""Relation-extraction benchmark (dev-only).

Scores the dream graph-from-text path: runs each extractor rung's
``extract_relations`` over a hand-labeled corpus and reports edge precision/
recall/F1 plus four defect-aligned diagnostics (naming consistency, type-
violation rate, related-to share, over-extraction). Unlike ladder_sweep this
needs NO database and NO embedder — extract_relations is a pure model call.

The opus-4.8 ceiling rung is produced in-session by subagents (see README):
  1. ``--emit-prompts`` writes results/relations_corpus_prompts.json
  2. dispatch subagents to extract over those prompts (the user's Claude usage)
  3. collect into results/relations-opus-4.8.json
  4. ``--report`` scores it as the absolute ceiling.
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.graph import norm_name
from pseudolife_memory.storage.postgres import _BUILTIN_RELATIONS

# Relation vocab handed to the model — same as service._dream_extract_relations
# (builtins minus the lesson-only prefers/avoids).
RELATION_REGISTRY = [(n, d) for (n, d, *_rest) in _BUILTIN_RELATIONS
                     if n not in ("prefers", "avoids")]

# canonical name -> {type, aliases}. Types: runtime/host, service/process,
# tool, file, datastore, component, concept, person.
ENTITIES: dict[str, dict] = {
    "pseudolife-daemon":   {"type": "service",   "aliases": ["the daemon", "pseudolife daemon"]},
    "postgres":            {"type": "datastore", "aliases": ["the postgres db", "pg", "postgres 16"]},
    "docker-desktop":      {"type": "runtime",   "aliases": ["docker"]},
    "windows 11":          {"type": "runtime",   "aliases": ["the windows host"]},
    "cortex console":      {"type": "component", "aliases": ["the web console", "the console ui"]},
    "chromadb":            {"type": "datastore", "aliases": ["the reference bank"]},
    "gemma 4 e2b sidecar": {"type": "service",   "aliases": ["the extractor sidecar", "gemma sidecar"]},
    "memory_recall":       {"type": "tool",      "aliases": ["the recall tool"]},
    "networkx":            {"type": "tool",      "aliases": ["the networkx read-model"]},
    "config.yaml":         {"type": "file",      "aliases": ["the config file"]},
    "schema":              {"type": "concept",   "aliases": []},
    "the user":            {"type": "person",    "aliases": ["i", "me"]},
}

# Type constraints for STRUCTURAL relations only. depends-on/uses/configures/
# related-to are intentionally absent (any->any; never a type violation).
RELATION_CONSTRAINTS: dict[str, tuple[set, set]] = {
    "runs-on":        ({"service", "process", "component", "tool", "file"}, {"runtime", "host"}),
    "hosts":          ({"runtime", "host"}, {"service", "process", "component"}),
    "stores-data-in": ({"service", "process", "tool"}, {"datastore", "file"}),
    "part-of":        ({"component", "service", "file", "datastore"}, {"component", "service"}),
}

# Each note: source text + gold closed-vocab edges (possibly empty).
CORPUS: list[dict] = [
    # --- Class 1: clean structural ---
    {"text": "The daemon runs in Docker and persists everything to Postgres.",
     "edges": [("pseudolife-daemon", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "Cortex Console is part of the daemon and uses NetworkX for graph queries.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon"),
               ("cortex console", "uses", "networkx")]},
    {"text": "The extractor sidecar runs on Docker, and the daemon depends on it for consolidation.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop"),
               ("pseudolife-daemon", "depends-on", "gemma 4 e2b sidecar")]},
    {"text": "ChromaDB is part of the daemon's reference bank.",
     "edges": [("chromadb", "part-of", "pseudolife-daemon")]},
    {"text": "memory_recall uses NetworkX to walk the graph.",
     "edges": [("memory_recall", "uses", "networkx")]},
    {"text": "config.yaml configures the daemon.",
     "edges": [("config.yaml", "configures", "pseudolife-daemon")]},
    {"text": "The daemon runs on the Windows 11 host.",
     "edges": [("pseudolife-daemon", "runs-on", "windows 11")]},
    # --- Class 2: canonicalization probes (same entity, varied surface forms) ---
    {"text": "The gemma sidecar runs on Docker too.",
     "edges": [("gemma 4 e2b sidecar", "runs-on", "docker-desktop")]},
    {"text": "The daemon writes the entities table to pg.",
     "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]},
    {"text": "The console ui is part of the daemon.",
     "edges": [("cortex console", "part-of", "pseudolife-daemon")]},
    # --- Class 3: null notes (no entity-to-entity relationship) ---
    {"text": "Honestly the new dashboard looks way cleaner than the old one.", "edges": []},
    {"text": "I spent way too long debugging this yesterday.", "edges": []},
    {"text": "We should probably write more tests at some point.", "edges": []},
    # --- Class 4: type traps ---
    {"text": "The migration touched schema v11 over the weekend.", "edges": []},
    {"text": "The user is on Windows 11.", "edges": []},
    {"text": "The daemon's data ends up in the nightly backup folder.", "edges": []},
]


def alias_index(entities: dict[str, dict]) -> dict[str, str]:
    """norm_name(surface) -> canonical, over canonical names + their aliases."""
    idx: dict[str, str] = {}
    for canon, meta in entities.items():
        for surface in [canon, *meta.get("aliases", [])]:
            idx[norm_name(surface)] = canon
    return idx


def resolve(name: str, idx: dict[str, str]) -> str | None:
    return idx.get(norm_name(name))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_relation_bench.py -v`
Expected: PASS (all six tests).

- [ ] **Step 5: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "feat(evals): relation-extraction bench data — entities, corpus, constraints, registry"
```

---

### Task 4: The defect-aligned scorer

**Files:**
- Modify: `evals/relation_extraction_bench.py`
- Test: `tests/test_relation_bench.py` (extend)

**Interfaces:**
- Consumes: `ENTITIES`, `RELATION_CONSTRAINTS`, `alias_index`, `resolve` (Task 3).
- Produces: `score(predicted, corpus=CORPUS, entities=ENTITIES) -> dict` where `predicted` is a list parallel to `corpus`, each element a list of `(src, relation, dst)` raw strings. Returns keys: `edge_precision`, `edge_recall`, `edge_f1`, `naming_consistency`, `type_violation_rate`, `related_to_share`, `over_extraction_null_edges`, `over_extraction_halluc`, `edges_per_note`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_bench.py`:

```python
def test_perfect_extraction_scores_one():
    corpus = [{"text": "the daemon runs in docker",
               "edges": [("pseudolife-daemon", "runs-on", "docker-desktop")]}]
    pred = [[("pseudolife-daemon", "runs-on", "docker-desktop")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["edge_f1"] == 1.0
    assert m["type_violation_rate"] == 0.0
    assert m["over_extraction_null_edges"] == 0
    assert m["naming_consistency"] == 1.0


def test_type_violation_and_null_spurious_counted():
    corpus = [{"text": "the user is on windows 11", "edges": []}]
    pred = [[("the user", "runs-on", "windows 11")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["type_violation_rate"] == 1.0           # person can't be runs-on src
    assert m["over_extraction_null_edges"] == 1       # edge on a gold-[] note
    assert m["edge_precision"] == 0.0                 # it's a false positive


def test_naming_fragmentation_measured():
    corpus = [{"text": "the daemon writes to postgres",
               "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]},
              {"text": "the daemon writes to pg",
               "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]}]
    pred = [[("pseudolife-daemon", "stores-data-in", "postgres")],
            [("pseudolife-daemon", "stores-data-in", "pg")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    # postgres seen as 2 surface forms, daemon as 1 -> mean 1.5
    assert m["naming_consistency"] == 1.5
    assert m["edge_f1"] == 1.0  # both still resolve to canonical postgres


def test_related_to_share():
    corpus = [{"text": "a and b are connected", "edges": []}]
    pred = [[("pseudolife-daemon", "related-to", "postgres")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["related_to_share"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relation_bench.py::test_perfect_extraction_scores_one -v`
Expected: FAIL — `AttributeError: module 'relation_extraction_bench' has no attribute 'score'`.

- [ ] **Step 3: Write minimal implementation**

Append `score` to `evals/relation_extraction_bench.py`:

```python
def _f1(p: float, r: float) -> float:
    return round(2 * p * r / (p + r), 3) if (p + r) else 0.0


def score(predicted: list[list[tuple]], corpus: list[dict] = CORPUS,
          entities: dict[str, dict] = ENTITIES) -> dict:
    idx = alias_index(entities)
    tp = fp = fn = 0
    total_pred = related_to = 0
    struct_pred = struct_violation = 0
    null_edges = halluc = 0
    naming: dict[str, set] = {}          # canonical -> distinct normalized surfaces

    for note, preds in zip(corpus, predicted):
        gold = {(s, r, d) for (s, r, d) in note["edges"]}
        matched: set = set()
        is_null = not note["edges"]
        note_norm = norm_name(note["text"])
        for (s, r, d) in preds:
            total_pred += 1
            if is_null:
                null_edges += 1
            if r == "related-to":
                related_to += 1
            cs, cd = resolve(s, idx), resolve(d, idx)
            for raw, canon in ((s, cs), (d, cd)):
                if canon is not None:
                    naming.setdefault(canon, set()).add(norm_name(raw))
                elif norm_name(raw) not in note_norm:
                    halluc += 1
            if r in RELATION_CONSTRAINTS and cs and cd:
                struct_pred += 1
                src_ok, dst_ok = RELATION_CONSTRAINTS[r]
                if entities[cs]["type"] not in src_ok or entities[cd]["type"] not in dst_ok:
                    struct_violation += 1
            triple = (cs, r, cd)
            if cs and cd and triple in gold and triple not in matched:
                tp += 1
                matched.add(triple)
            else:
                fp += 1
        fn += len(gold) - len(matched)

    precision = round(tp / (tp + fp), 3) if (tp + fp) else 0.0
    recall = round(tp / (tp + fn), 3) if (tp + fn) else 0.0
    naming_consistency = (round(sum(len(v) for v in naming.values()) / len(naming), 3)
                          if naming else 1.0)
    n_notes = len(corpus) or 1
    return {
        "edge_precision": precision,
        "edge_recall": recall,
        "edge_f1": _f1(precision, recall),
        "naming_consistency": naming_consistency,
        "type_violation_rate": round(struct_violation / struct_pred, 3) if struct_pred else 0.0,
        "related_to_share": round(related_to / total_pred, 3) if total_pred else 0.0,
        "over_extraction_null_edges": null_edges,
        "over_extraction_halluc": halluc,
        "edges_per_note": round(total_pred / n_notes, 2),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_relation_bench.py -v`
Expected: PASS (all tests, including the four new scorer tests).

- [ ] **Step 5: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "feat(evals): defect-aligned relation scorer (edge-F1 + naming/type/related-to/over-extraction)"
```

---

### Task 5: Per-rung prediction + `--rung` driver

**Files:**
- Modify: `evals/relation_extraction_bench.py`
- Test: `tests/test_relation_bench.py` (extend)

**Interfaces:**
- Consumes: `score`, `CORPUS`, `RELATION_REGISTRY` (Tasks 3-4); `ladder_sweep.RUNGS`, `.probe`, `.make_extractor`.
- Produces: `predict_with(extractor, corpus=CORPUS) -> list[list[tuple]]`; `run_rung(name) -> dict`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_bench.py`:

```python
class _StubExtractor:
    """Mimics OpenAICompatExtractor.extract_relations for one fixed note."""
    def extract_relations(self, texts, relations):
        if "runs in docker" in texts[0].lower():
            return [{"src": "the daemon", "relation": "runs-on", "dst": "docker", "confidence": 0.6}]
        return []


def test_predict_with_maps_triples_and_resolves_aliases():
    corpus = [{"text": "the daemon runs in docker",
               "edges": [("pseudolife-daemon", "runs-on", "docker-desktop")]}]
    pred = rb.predict_with(_StubExtractor(), corpus)
    assert pred == [[("the daemon", "runs-on", "docker")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["edge_f1"] == 1.0   # aliases resolve: "the daemon"->daemon, "docker"->docker-desktop
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relation_bench.py::test_predict_with_maps_triples_and_resolves_aliases -v`
Expected: FAIL — `AttributeError: module 'relation_extraction_bench' has no attribute 'predict_with'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/relation_extraction_bench.py` (the `ladder_sweep` import goes at module top with the others; shown here for locality):

```python
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder_sweep import RUNGS, make_extractor, probe  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def predict_with(extractor, corpus: list[dict] = CORPUS) -> list[list[tuple]]:
    """Per-note: call extract_relations, return [(src, relation, dst), ...] strings."""
    out: list[list[tuple]] = []
    for note in corpus:
        triples = extractor.extract_relations([note["text"]], RELATION_REGISTRY)
        out.append([(t["src"], t["relation"], t["dst"]) for t in triples])
    return out


def run_rung(name: str) -> dict:
    rung = RUNGS[name]
    result = {"rung": name, "label": rung["label"]}
    if rung["kind"] == "floor":
        result["status"] = "n/a"   # RegexExtractor has no extract_relations
        return result
    if rung["kind"] != "llm" or not probe(rung["base_url"]):
        result["status"] = "unreachable"
        result["base_url"] = rung.get("base_url")
        return result
    extractor = make_extractor(rung)        # OpenAICompatExtractor (max_tokens=4096, 600s)
    t0 = time.perf_counter()
    predicted = predict_with(extractor)
    secs = round(time.perf_counter() - t0, 1)
    result.update(score(predicted))
    result["extract_seconds"] = secs
    result["predicted"] = predicted          # raw triples = silver labels (§10)
    result["status"] = "ok"
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relation_bench.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "feat(evals): per-rung relation prediction + --rung driver (reuses ladder rungs)"
```

---

### Task 6: `--emit-prompts` for the in-session Opus ceiling rung

**Files:**
- Modify: `evals/relation_extraction_bench.py`
- Test: `tests/test_relation_bench.py` (extend)

**Interfaces:**
- Consumes: `CORPUS`, `RELATION_REGISTRY`; `pseudolife_memory.memory.dream._relations_prompt`.
- Produces: `build_prompts() -> list[dict]` (each `{note_index, system, user}`); `emit_prompts(path)` writes the JSON.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_bench.py`:

```python
def test_build_prompts_uses_the_live_relations_prompt():
    from pseudolife_memory.memory.dream import _relations_prompt
    prompts = rb.build_prompts()
    assert len(prompts) == len(rb.CORPUS)
    assert prompts[0]["user"] == rb.CORPUS[0]["text"]
    assert prompts[0]["system"] == _relations_prompt(rb.RELATION_REGISTRY)
    assert prompts[0]["note_index"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relation_bench.py::test_build_prompts_uses_the_live_relations_prompt -v`
Expected: FAIL — `AttributeError: ... has no attribute 'build_prompts'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/relation_extraction_bench.py`:

```python
import json


def build_prompts() -> list[dict]:
    """Each corpus note paired with the EXACT system prompt + registry the
    headless LLM rungs receive — the single source for the in-session Opus rung."""
    from pseudolife_memory.memory.dream import _relations_prompt
    system = _relations_prompt(RELATION_REGISTRY)
    return [{"note_index": i, "system": system, "user": note["text"]}
            for i, note in enumerate(CORPUS)]


def emit_prompts(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_prompts(), indent=2))
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relation_bench.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "feat(evals): --emit-prompts artifact for the in-session opus-4.8 ceiling rung"
```

---

### Task 7: `--report` aggregation + `main()` CLI wiring

**Files:**
- Modify: `evals/relation_extraction_bench.py`
- Test: `tests/test_relation_bench.py` (extend)

**Interfaces:**
- Consumes: all of the above.
- Produces: `build_report(results: dict[str, dict]) -> list[dict]` (one row per rung, ladder order, with a `gap_to_27b` field on the F1); `report()` (load + print); `main()` argparse entry (`--rung`, `--emit-prompts`, `--report`, `--list`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_bench.py`:

```python
def test_build_report_orders_rungs_and_computes_gap_to_ceiling():
    results = {
        "gemma-e2b": {"rung": "gemma-e2b", "status": "ok", "edge_f1": 0.62,
                      "type_violation_rate": 0.28, "naming_consistency": 1.9,
                      "related_to_share": 0.41, "over_extraction_null_edges": 5,
                      "over_extraction_halluc": 3},
        "qwen-27b": {"rung": "qwen-27b", "status": "ok", "edge_f1": 0.84,
                     "type_violation_rate": 0.04, "naming_consistency": 1.1,
                     "related_to_share": 0.12, "over_extraction_null_edges": 1,
                     "over_extraction_halluc": 0},
    }
    rows = rb.build_report(results)
    names = [r["rung"] for r in rows]
    assert names.index("gemma-e2b") < names.index("qwen-27b")  # ladder order
    e2b = next(r for r in rows if r["rung"] == "gemma-e2b")
    assert e2b["gap_to_27b"] == round(0.62 - 0.84, 3)          # -0.22
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relation_bench.py::test_build_report_orders_rungs_and_computes_gap_to_ceiling -v`
Expected: FAIL — `AttributeError: ... has no attribute 'build_report'`.

- [ ] **Step 3: Write minimal implementation**

Append to `evals/relation_extraction_bench.py`:

```python
import argparse

LADDER_ORDER = ["floor", "gemma-e2b", "gemma-e4b", "qwen-27b", "opus-4.8"]


def _load_results() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if RESULTS_DIR.exists():
        for fp in RESULTS_DIR.glob("relations-*.json"):
            try:
                out[fp.stem.replace("relations-", "")] = json.loads(fp.read_text())
            except Exception:
                pass
    return out


def build_report(results: dict[str, dict]) -> list[dict]:
    ceiling = results.get("qwen-27b", {}).get("edge_f1")
    rows = []
    for name in LADDER_ORDER:
        r = results.get(name)
        if not r or r.get("status") != "ok":
            continue
        row = {k: r.get(k) for k in (
            "rung", "edge_f1", "type_violation_rate", "naming_consistency",
            "related_to_share", "over_extraction_null_edges", "over_extraction_halluc")}
        row["gap_to_27b"] = (round(r["edge_f1"] - ceiling, 3)
                             if ceiling is not None else None)
        rows.append(row)
    return rows


def report() -> None:
    rows = build_report(_load_results())
    hdr = (f"{'rung':<12}{'F1↑':>7}{'type_viol↓':>12}{'naming↓':>9}"
           f"{'rel-to↓':>9}{'null':>6}{'halluc':>8}{'gap_to_27b':>12}")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(f"{r['rung']:<12}{r['edge_f1']:>7}{r['type_violation_rate']:>12}"
              f"{r['naming_consistency']:>9}{r['related_to_share']:>9}"
              f"{r['over_extraction_null_edges']:>6}{r['over_extraction_halluc']:>8}"
              f"{str(r['gap_to_27b']):>12}")
    if not rows:
        print("(no results — run a rung, or add results/relations-opus-4.8.json)")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", choices=list(RUNGS), help="run one rung")
    ap.add_argument("--emit-prompts", action="store_true",
                    help="write results/relations_corpus_prompts.json for the opus rung")
    ap.add_argument("--report", action="store_true", help="aggregate results into the table")
    ap.add_argument("--list", action="store_true", help="list rungs + endpoints")
    args = ap.parse_args()

    if args.list:
        for n in LADDER_ORDER:
            r = RUNGS.get(n, {"label": "in-session subagents (see README)"})
            print(f"  {n:<12} {r.get('label', n):<34} {r.get('base_url', '—')}")
        return 0
    if args.report:
        report(); return 0
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.emit_prompts:
        p = emit_prompts(RESULTS_DIR / "relations_corpus_prompts.json")
        print(f"wrote {p}"); return 0
    if args.rung:
        out = run_rung(args.rung)
        (RESULTS_DIR / f"relations-{args.rung}.json").write_text(json.dumps(out, indent=2))
        print(json.dumps({k: v for k, v in out.items() if k != "predicted"}, indent=2))
        return 0
    ap.print_help(); return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_relation_bench.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Smoke-test the CLI offline (no endpoints needed)**

Run: `python evals/relation_extraction_bench.py --emit-prompts && python evals/relation_extraction_bench.py --report`
Expected: writes `evals/results/relations_corpus_prompts.json`; `--report` prints the header and the "(no results …)" line (no rungs run yet). No traceback.

- [ ] **Step 6: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "feat(evals): --report gap-to-ceiling table + CLI wiring for the relation bench"
```

---

### Task 8: Document the bench + the in-session Opus procedure

**Files:**
- Modify: `evals/README.md` (add a section after the lesson-synthesis section)

**Interfaces:**
- Consumes: nothing (docs).
- Produces: operator-facing run instructions, including the subagent procedure for the opus-4.8 rung.

- [ ] **Step 1: Add the documentation section**

Append to `evals/README.md`:

````markdown
---

# Relation-extraction benchmark (`relation_extraction_bench.py`)

Dev-only. Answers the Phase-2 question the fact-ladder never did: **how good is
the dream graph-from-text path, per extractor model?** Scores each rung's
`extract_relations` over a hand-labeled corpus (`CORPUS`) — edge precision/
recall/F1 plus four defect-aligned diagnostics:

- `naming_consistency` (↓ to 1.0) — surface-form fragmentation (duplicate nodes)
- `type_violation_rate` (↓) — structural edges that violate `(src_type→dst_type)`
- `related_to_share` (↓) — laziness into the `related-to` catch-all
- `over_extraction_null_edges` / `over_extraction_halluc` (↓) — orphan minting

No DB and no embedder — `extract_relations` is a pure model call.

## Rungs

`floor` (n/a — regex has no relation extraction), `gemma-e2b`, `gemma-e4b`
(swap the `:8081` GGUF, as in the fact ladder), `qwen-27b` (LAN 4090, the
sovereign-local ceiling), and `opus-4.8` (the absolute ceiling, produced
in-session — below).

```bash
PYTHONPATH=. python evals/relation_extraction_bench.py --rung gemma-e2b
PYTHONPATH=. python evals/relation_extraction_bench.py --rung qwen-27b
PYTHONPATH=. python evals/relation_extraction_bench.py --report
```

Each rung writes `results/relations-<rung>.json` (including its raw predicted
triples — the silver labels for any future bespoke-model work).

## The opus-4.8 ceiling rung (in-session, no API key)

Produced by Claude Code subagents on your included usage — a **frozen
reference** (regenerate by repeating these steps; not headlessly re-runnable):

1. `PYTHONPATH=. python evals/relation_extraction_bench.py --emit-prompts`
   → `results/relations_corpus_prompts.json` (each note + the exact `system`
   prompt + registry the headless rungs use).
2. In a Claude Code session, dispatch subagents (Opus 4.8) to run the
   extraction over those prompts and return predicted triples as JSON.
3. Collect into `results/relations-opus-4.8.json`, matching the `--rung` output
   shape: `{"rung":"opus-4.8","status":"ok","predicted":[[["src","rel","dst"],…],…], …score keys…}`.
   Re-score by importing `relation_extraction_bench.score(predicted)` and
   merging its keys, so the file carries the same metrics as the headless rungs.
4. `--report` ranks every rung against the `qwen-27b` and `opus-4.8` ceilings
   (`gap_to_27b`). That gap drives the keep-repair vs retrench(C) vs
   bespoke-model decision (see the design doc).
````

- [ ] **Step 2: Commit**

```bash
git add evals/README.md
git commit -m "docs(evals): document the relation-extraction bench + in-session opus rung"
```

---

## Self-Review

**Spec coverage** (design §4 components → tasks):
- Gold corpus + entity registry + type constraints → Task 3 ✓
- `--rung` driver (raw triples) → Task 5 ✓
- Defect-aligned scorer (edge-F1 + 4 diagnostics) → Task 4 ✓
- `--emit-prompts` → Task 6 ✓
- `--report` gap-to-ceiling → Task 7 ✓
- Two `graph_review.py` fixes → Tasks 1–2 ✓
- In-session opus rung procedure → Task 8 (docs) + Task 6 (artifact) ✓
- Testing/isolation (§11): scorer + corpus + analyzer fixes unit-tested; bench dev-only; UTF-8 stdout → Tasks 1–7 ✓

**Placeholder scan:** No "TBD/TODO/handle edge cases". The corpus is fully written (16 notes, all four classes); the `test_corpus_covers_all_four_classes` guard enforces ≥15 notes + null + structural coverage, so it can grow without a placeholder.

**Type consistency:** `score(predicted, corpus, entities)` signature and its return keys are identical across Tasks 4, 5, 7 and every test. `predict_with` returns `list[list[tuple]]` consumed by `score` (Task 5). `RELATION_REGISTRY`/`alias_index`/`resolve` defined in Task 3 are used unchanged in 4–6. `run_rung` writes the same shape the opus procedure (Task 8) reproduces.

**Out-of-scope guard:** No task touches `dream.py` prompts, `_link_dream_relations`, or `extract_relations=True` (design non-goals) — confirmed; the only `pseudolife_memory` edits are the two `graph_review.py` fixes.
