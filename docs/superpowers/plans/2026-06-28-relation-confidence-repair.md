# Relation-Confidence Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the constant `0.6` edge confidence with a deterministic, on-box per-edge confidence that penalizes type-violations and the vague `related-to` catch-all, so `graph_review`'s dubious detector finally discriminates.

**Architecture:** A new pure module `pseudolife_memory/memory/relation_quality.py` (`TYPE_CONSTRAINTS` + `infer_type` lexicon + `edge_confidence`) is called by `_link_dream_relations` per edge; the dev bench imports the same `TYPE_CONSTRAINTS`; a dry-run-first ops script backfills existing edges.

**Tech Stack:** Python 3.10+, stdlib only (`re`). `pytest`. Touches `pseudolife_memory/memory/`, `service.py`, `utils/config.py`, `graph_review.py`, `evals/`, `ops/`.

## Global Constraints

- **No new dependencies** — stdlib `re` only.
- **No change to the extractor prompt or `extract_relations`** — confidence is computed at *link* time, not requested from the model.
- **Confidence is deterministic/structural** — no model calls, no randomness; `infer_type` returns a type or `None` (unknown), and **`unknown` never penalizes** (recall-preserving).
- **Confidence values (verbatim):** clean specific edge `0.70`; `related-to` `0.45`; known type-violation `0.175` (= `0.70 * 0.25`). `graph_review` keeps `_DUBIOUS_CONF = 0.6`.
- **`TYPE_CONSTRAINTS` is the single source of truth** — the bench imports it; do not duplicate the table.
- **Non-destructive by default** — `min_relation_confidence` defaults to `0.0` (write everything); the backfill is dry-run-first, backup-first, `UPDATE`-only (no DDL, no `PostgresStorage()` constructor), per the live-bank lesson.
- **Run tests via the project venv:** `.venv/Scripts/python.exe -m pytest <args>` (Windows). PG-backed tests skip cleanly without Postgres — that is expected.

---

### Task 1: `relation_quality` module — `TYPE_CONSTRAINTS` + `infer_type`

**Files:**
- Create: `pseudolife_memory/memory/relation_quality.py`
- Test: `tests/test_relation_quality.py` (create)

**Interfaces:**
- Consumes: nothing (pure, stdlib `re`).
- Produces: `TYPE_CONSTRAINTS: dict[str, tuple[set[str], set[str]]]`; `infer_type(name: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_relation_quality.py`:

```python
import pytest
from pseudolife_memory.memory.relation_quality import infer_type, TYPE_CONSTRAINTS


@pytest.mark.parametrize("name,expected", [
    ("user", "person"), ("the user", "person"),
    ("schema 11", "concept"), ("schema v8", "concept"), ("11", "concept"),
    ("0.2.0", "concept"), ("master", "concept"),
    ("docker compose -f ops/docker-compose.yml up -d", "concept"),
    ("config.yaml", "file"), ("ops/backup.ps1", "file"),
    ("postgres", "datastore"), ("pg", "datastore"), ("chromadb", "datastore"),
    ("docker", "runtime"), ("docker-desktop", "runtime"), ("windows 11", "runtime"),
    ("pseudolife-daemon", "service"), ("daemon", "service"),
    ("gemma 4 e2b sidecar", "service"), ("live daemon", "service"),
    ("memory_recall", "tool"),
    ("cortex console", None), ("networkx", None), ("nightly backup folder", None),
])
def test_infer_type(name, expected):
    assert infer_type(name) == expected


def test_constraints_cover_structural_relations():
    assert set(TYPE_CONSTRAINTS) == {"runs-on", "hosts", "stores-data-in", "part-of"}
    assert TYPE_CONSTRAINTS["runs-on"][1] == {"runtime", "host"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_quality.py -v`
Expected: FAIL — `ModuleNotFoundError: ... relation_quality`.

- [ ] **Step 3: Write minimal implementation**

Create `pseudolife_memory/memory/relation_quality.py`:

```python
"""Deterministic, on-box relation quality scoring — the single source of truth
for structural relation type rules. Pure (stdlib only); no model calls.

`infer_type` returns a coarse entity type or None (unknown). `edge_confidence`
computes a per-edge confidence that penalizes type-violations + the vague
`related-to` catch-all. `unknown` types are always NEUTRAL (never penalize), so a
correctly-extracted edge whose entities we can't type keeps full confidence.
"""
from __future__ import annotations

import re

# Allowed (src_types, dst_types) per STRUCTURAL relation. The single source of
# truth — evals/relation_extraction_bench.py imports this. depends-on / uses /
# configures / related-to are intentionally absent (any->any, no type penalty).
TYPE_CONSTRAINTS: dict[str, tuple[set[str], set[str]]] = {
    "runs-on":        ({"service", "process", "component", "tool", "file"}, {"runtime", "host"}),
    "hosts":          ({"runtime", "host"}, {"service", "process", "component"}),
    "stores-data-in": ({"service", "process", "tool"}, {"datastore", "file"}),
    "part-of":        ({"component", "service", "file", "datastore"}, {"component", "service"}),
}

_CMD_PREFIXES = ("docker compose", "docker ", "git ", "pip ", "npm ", "curl ",
                 "kubectl ", "psql ", "pg_dump", "wsl ")
_FILE_EXT = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sql", ".ps1",
             ".sh", ".gguf", ".toml", ".ini", ".cfg", ".fx")
_PERSON = {"user", "the user", "i", "me", "admin", "operator"}
_RUNTIME = {"docker", "docker-desktop", "windows", "windows 11", "windows box",
            "windows host", "linux", "wsl", "host", "vm", "kubernetes", "k8s",
            "container", "4090", "gpu", "cpu", "dx11", "dx12"}
_DATASTORE = {"postgres", "postgresql", "pg", "chromadb", "redis", "valkey",
              "sqlite", "kafka", "rabbitmq", "bank"}


def infer_type(name: str) -> str | None:
    """Coarse entity type, or None if we can't confidently type it. Order matters:
    command-strings/concepts before the runtime glob; file-suffix before tool."""
    n = (name or "").strip().lower()
    if not n:
        return None
    # concept / non-entity — FIRST so "docker compose -f ..." != runtime
    if any(n.startswith(p) for p in _CMD_PREFIXES):
        return "concept"
    if re.fullmatch(r"v?\d+(?:\.\d+)*", n):                 # "11", "v8", "0.2.0"
        return "concept"
    if n.startswith("schema") or n in ("branch", "master", "main"):
        return "concept"
    # file (by extension) before tool (identifier)
    if n.endswith(_FILE_EXT):
        return "file"
    if n in _PERSON:
        return "person"
    if n in _RUNTIME or n.startswith(("docker-", "windows")):
        return "runtime"
    if n in _DATASTORE or n.endswith("-db") or "database" in n:
        return "datastore"
    if "daemon" in n or n.endswith(("-service", "-server", "-worker")) \
            or "sidecar" in n or n == "gateway":
        return "service"
    if (re.fullmatch(r"[a-z][a-z0-9_]*", n) and "_" in n) or n.endswith("()"):
        return "tool"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_quality.py -v`
Expected: PASS (all parametrized cases + the constraints test).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/relation_quality.py tests/test_relation_quality.py
git commit -m "feat(graph): relation_quality module — TYPE_CONSTRAINTS + infer_type lexicon"
```

---

### Task 2: `edge_confidence`

**Files:**
- Modify: `pseudolife_memory/memory/relation_quality.py`
- Test: `tests/test_relation_quality.py` (extend)

**Interfaces:**
- Consumes: `TYPE_CONSTRAINTS`, `infer_type` (Task 1).
- Produces: `edge_confidence(src: str, relation: str, dst: str) -> float`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_quality.py`:

```python
from pseudolife_memory.memory.relation_quality import edge_confidence


def test_clean_specific_edge():
    # daemon(service) runs-on docker(runtime) — compatible
    assert edge_confidence("the daemon", "runs-on", "docker") == 0.70


def test_related_to_is_low_base():
    assert edge_confidence("x", "related-to", "y") == 0.45


def test_type_violation_penalized():
    # user(person) is not a valid runs-on src
    assert edge_confidence("user", "runs-on", "windows 11") == 0.175
    # schema(concept) is not a valid runs-on dst
    assert edge_confidence("the daemon", "runs-on", "schema 11") == 0.175
    # command-string is not a valid stores-data-in dst
    assert edge_confidence("the daemon", "stores-data-in",
                           "docker compose -f ops/x.yml up") == 0.175


def test_unknown_type_is_neutral():
    # cortex console (unknown) part-of daemon — src unknown -> no penalty
    assert edge_confidence("cortex console", "part-of", "the daemon") == 0.70


def test_non_structural_relation_never_penalized():
    # 'uses' has no constraint even if types look odd
    assert edge_confidence("user", "uses", "schema 11") == 0.70
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_quality.py::test_clean_specific_edge -v`
Expected: FAIL — `ImportError`/`AttributeError`: no `edge_confidence`.

- [ ] **Step 3: Write minimal implementation**

Append to `pseudolife_memory/memory/relation_quality.py`:

```python
def edge_confidence(src: str, relation: str, dst: str) -> float:
    """Deterministic per-edge confidence. 0.70 clean / 0.45 related-to /
    0.175 known type-violation. Unknown types never penalize."""
    base = 0.45 if relation == "related-to" else 0.70
    constraint = TYPE_CONSTRAINTS.get(relation)
    if constraint:
        st, dt = infer_type(src), infer_type(dst)
        if st and dt:                      # only when BOTH endpoints are typed
            src_ok, dst_ok = constraint
            if st not in src_ok or dt not in dst_ok:
                base *= 0.25
    return round(base, 3)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_quality.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/relation_quality.py tests/test_relation_quality.py
git commit -m "feat(graph): edge_confidence — type-aware deterministic relation confidence"
```

---

### Task 3: Wire into `_link_dream_relations` + `min_relation_confidence` config

**Files:**
- Modify: `pseudolife_memory/utils/config.py:313`
- Modify: `pseudolife_memory/service.py` (`_link_dream_relations`, the `conf = …` line + the `upsert_edge` call)
- Test: `tests/test_graph.py` (extend — PG-backed, skips without Postgres)

**Interfaces:**
- Consumes: `edge_confidence` (Task 2).
- Produces: dream-written edges carry per-edge confidence; `config.memory.dream.min_relation_confidence: float` (default `0.0`).

- [ ] **Step 1: Write the failing test**

First inspect how `tests/test_graph.py` builds a service/storage (look for an existing fixture like `svc`). Append a test that drives the link path. If the suite uses a `svc` fixture with `_link_dream_relations` reachable, use this shape (adjust the fixture name to match the file):

```python
def test_dream_edge_confidence_varies_by_type(svc):
    # a clean edge and a type-violation through the real link path
    rels = [
        {"src": "the daemon", "relation": "runs-on", "dst": "docker-desktop"},
        {"src": "user", "relation": "runs-on", "dst": "windows 11"},
    ]
    svc._link_dream_relations(rels)
    g = svc._storage.load_graph()
    by = {(svc._storage.find_entity_by_id(e["src_id"])["display"],
           e["relation"]): e["confidence"] for e in g["edges"]}
    # clean edge ~0.70, violation ~0.175
    clean = [c for (s, r), c in by.items() if r == "runs-on" and c > 0.5]
    viol = [c for (s, r), c in by.items() if r == "runs-on" and c < 0.5]
    assert clean and abs(clean[0] - 0.70) < 0.01
    assert viol and abs(viol[0] - 0.175) < 0.01
```

> If `find_entity_by_id` does not exist, assert on raw confidences instead:
> `confs = sorted(e["confidence"] for e in g["edges"] if e["relation"] == "runs-on")`
> then `assert abs(confs[0] - 0.175) < 0.01 and abs(confs[-1] - 0.70) < 0.01`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_dream_edge_confidence_varies_by_type -v`
Expected: FAIL (currently both edges written at constant `0.6`), OR `SKIPPED` if no Postgres — in that case start a test PG via `docker compose -f ops/docker-compose.yml up -d pseudolife-pg` first (the suite targets `pseudolife_memory_test` on port 5433).

- [ ] **Step 3: Write minimal implementation**

In `pseudolife_memory/utils/config.py`, add the field right after `relation_confidence` (line 313):

```python
    relation_confidence: float = 0.6  # legacy default; superseded by edge_confidence()
    # Edges scoring below this at link time are dropped. 0.0 = write everything
    # (non-destructive default); raise (e.g. 0.2) to auto-drop type-violations.
    min_relation_confidence: float = 0.0
```

In `pseudolife_memory/service.py`, `_link_dream_relations`: replace the constant `conf` line and the `upsert_edge` call. Remove `conf = float(self.config.memory.dream.relation_confidence)`; inside the loop compute per-edge confidence from the raw model names and gate on the floor:

```python
        from pseudolife_memory.memory.relation_quality import edge_confidence
        floor = float(self.config.memory.dream.min_relation_confidence)
        n = 0
        for r in relations:
            raw_src, raw_dst = str(r.get("src", "")), str(r.get("dst", ""))
            src_n, dst_n = G.norm_name(raw_src), G.norm_name(raw_dst)
            if not src_n or not dst_n or src_n == dst_n:
                continue
            resolved, _ = G.resolve_relation(known, str(r.get("relation", "")))
            relation = resolved or "related-to"
            conf = edge_confidence(raw_src, relation, raw_dst)
            if conf < floor:
                continue
            src_e = self._resolve_or_create_entity(raw_src)
            dst_e = self._resolve_or_create_entity(raw_dst)
            self._graph.upsert_edge(src_e["id"], relation, dst_e["id"],
                                    confidence=conf, origin="agent")
            n += 1
        return n
```

(Note: `upsert_edge` *bumps* confidence on re-assertion — existing behavior, unchanged here; the computed value is the first-insert floor.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph.py::test_dream_edge_confidence_varies_by_type -v`
Expected: PASS (or SKIPPED without Postgres — then run the full `tests/test_graph.py -v` against the test PG to confirm).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/service.py tests/test_graph.py
git commit -m "feat(graph): dream edges get real edge_confidence + min_relation_confidence floor"
```

---

### Task 4: `graph_review` honest label + discrimination test

**Files:**
- Modify: `pseudolife_memory/memory/graph_review.py:63-65`
- Test: `tests/test_graph_review.py` (extend)

**Interfaces:**
- Consumes: nothing new (the detector already reads `confidence`).
- Produces: clearer finding label; a test proving the detector discriminates now that confidence varies.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph_review.py` (the file from the earlier graph_review work; reuse its `_ents` helper):

```python
from pseudolife_memory.memory.graph_review import dubious_edges


def test_dubious_edges_discriminate_by_confidence():
    entities = _ents("a", "b", "c")
    ids = {e["display"]: e["id"] for e in entities}
    edges = [
        {"src_id": ids["a"], "relation": "runs-on", "dst_id": ids["b"],
         "origin": "agent", "confidence": 0.175},   # violation -> flagged
        {"src_id": ids["a"], "relation": "uses", "dst_id": ids["c"],
         "origin": "agent", "confidence": 0.70},      # good -> NOT flagged
    ]
    out = dubious_edges(edges, entities)
    assert out, "low-confidence edge should produce a finding"
    flagged = out[0]["edges"]
    assert len(flagged) == 1 and flagged[0]["confidence"] == 0.175
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_review.py::test_dubious_edges_discriminate_by_confidence -v`
Expected: PASS already on logic (the detector reads confidence) — but verify; if it passes immediately, that confirms the detector now discriminates. (This task's substantive change is the label; the test guards the behavior.) If it FAILS, fix per Step 3.

- [ ] **Step 3: Update the label**

In `pseudolife_memory/memory/graph_review.py`, in `dubious_edges`, change the finding label so it reads honestly now that confidence varies:

```python
    return [{"type": "dubious_edge", "severity": "warn",
             "label": f"{len(rows)} low-confidence / type-suspect edges",
             "edges": rows, "action": "prune"}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph_review.py -v`
Expected: PASS (all, including the new discrimination test).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_review.py tests/test_graph_review.py
git commit -m "refactor(graph): honest dubious-edge label + discrimination test"
```

---

### Task 5: Bench imports the shared `TYPE_CONSTRAINTS` (DRY)

**Files:**
- Modify: `evals/relation_extraction_bench.py` (the `RELATION_CONSTRAINTS` block, ~lines 56-64)
- Test: `tests/test_relation_bench.py` (already exists — must stay green)

**Interfaces:**
- Consumes: `pseudolife_memory.memory.relation_quality.TYPE_CONSTRAINTS` (Task 1).
- Produces: `RELATION_CONSTRAINTS` is now an alias of the shared table.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relation_bench.py`:

```python
def test_bench_constraints_are_the_shared_source():
    from pseudolife_memory.memory.relation_quality import TYPE_CONSTRAINTS
    assert rb.RELATION_CONSTRAINTS is TYPE_CONSTRAINTS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_bench.py::test_bench_constraints_are_the_shared_source -v`
Expected: FAIL — `RELATION_CONSTRAINTS` is currently a local dict literal, not the shared object.

- [ ] **Step 3: Replace the local table with the import**

In `evals/relation_extraction_bench.py`, delete the `RELATION_CONSTRAINTS = {...}` literal block (the 4-relation dict) and replace it with an import + alias near the top imports:

```python
from pseudolife_memory.memory.relation_quality import TYPE_CONSTRAINTS as RELATION_CONSTRAINTS
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_relation_bench.py -v`
Expected: PASS (the new identity test + all existing bench tests — the constraint values are identical, so `score()` behavior is unchanged).

- [ ] **Step 5: Commit**

```bash
git add evals/relation_extraction_bench.py tests/test_relation_bench.py
git commit -m "refactor(evals): bench imports shared TYPE_CONSTRAINTS (measured == enforced)"
```

---

### Task 6: Retroactive backfill script (dry-run-first)

**Files:**
- Create: `ops/backfill_edge_confidence.py`
- Test: `tests/test_backfill_edge_confidence.py` (create — pure recompute, no DB)

**Interfaces:**
- Consumes: `edge_confidence` (Task 2).
- Produces: `recompute_rows(rows) -> list[tuple[int, float]]` (pure); a `--dry-run`(default)/`--apply` CLI.

- [ ] **Step 1: Write the failing test**

Create `tests/test_backfill_edge_confidence.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import backfill_edge_confidence as bf  # noqa: E402


def test_recompute_rows():
    # rows: (id, src_display, relation, dst_display, old_conf)
    rows = [
        (1, "the daemon", "runs-on", "docker-desktop", 0.6),   # clean -> 0.70
        (2, "user", "runs-on", "windows 11", 0.6),             # violation -> 0.175
        (3, "x", "related-to", "y", 0.6),                       # related-to -> 0.45
    ]
    assert bf.recompute_rows(rows) == [(1, 0.70), (2, 0.175), (3, 0.45)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_backfill_edge_confidence.py -v`
Expected: FAIL — `ModuleNotFoundError: backfill_edge_confidence`.

- [ ] **Step 3: Write the script**

Create `ops/backfill_edge_confidence.py`:

```python
#!/usr/bin/env python
"""Recompute confidence for existing agent-origin edges using the same
edge_confidence() the live link path uses, so graph_review's dubious detector is
meaningful for the CURRENT graph. Dry-run by default. Idempotent (pure recompute).

Per the live-bank lesson: BACK UP FIRST (ops/backup.ps1), plain psycopg.connect()
with lock/statement timeouts, idempotent UPDATE only — no DDL, no PostgresStorage().

Usage:
  python ops/backfill_edge_confidence.py            # dry-run: print distribution
  python ops/backfill_edge_confidence.py --apply    # write the new confidences
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from pseudolife_memory.memory.relation_quality import edge_confidence

_SELECT = """
SELECT e.id, s.display, e.relation, d.display, e.confidence
FROM edges e
JOIN entities s ON e.src_id = s.id
JOIN entities d ON e.dst_id = d.id
WHERE e.origin = 'agent' AND e.superseded_at IS NULL
ORDER BY e.id
"""


def recompute_rows(rows: list[tuple]) -> list[tuple[int, float]]:
    """Pure: (id, src, relation, dst, old_conf) -> (id, new_conf)."""
    return [(rid, edge_confidence(src, rel, dst))
            for (rid, src, rel, dst, _old) in rows]


def _dsn() -> str:
    return os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")


def main() -> int:
    apply = "--apply" in sys.argv
    import psycopg
    with psycopg.connect(_dsn(), connect_timeout=5) as conn:
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET statement_timeout = '30s'")
        rows = conn.execute(_SELECT).fetchall()
        updates = recompute_rows(rows)
        changed = [(rid, new) for (rid, _s, _r, _d, old), (rid2, new)
                   in zip(rows, updates) if abs((old or 0) - new) > 1e-6]
        dist = Counter(round(new, 3) for _id, new in updates)
        print(f"agent edges: {len(rows)}; would change: {len(changed)}")
        print("new-confidence distribution:", dict(sorted(dist.items())))
        for rid, new in changed[:15]:
            src, rel, dst = next((s, r, d) for (i, s, r, d, _o) in rows if i == rid)
            print(f"  edge {rid}: {src} --{rel}--> {dst}  -> {new}")
        if not apply:
            print("\n[DRY RUN] re-run with --apply to write")
            return 0
        with conn.cursor() as cur:
            cur.executemany("UPDATE edges SET confidence = %s WHERE id = %s",
                            [(new, rid) for rid, new in changed])
        conn.commit()
        print(f"\napplied {len(changed)} updates")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_backfill_edge_confidence.py -v`
Expected: PASS (the pure `recompute_rows` test; the DB path is not exercised by the unit test).

- [ ] **Step 5: Commit**

```bash
git add ops/backfill_edge_confidence.py tests/test_backfill_edge_confidence.py
git commit -m "feat(ops): dry-run-first backfill of edge confidence for existing agent edges"
```

---

## Self-Review

**Spec coverage** (design §→task):
- §4 `relation_quality` module (`TYPE_CONSTRAINTS`, `infer_type`, `edge_confidence`) → Tasks 1–2 ✓
- §4 wire into `_link_dream_relations` + `min_relation_confidence` → Task 3 ✓
- §4 `graph_review` honest label → Task 4 ✓
- §4 DRY: bench imports `TYPE_CONSTRAINTS` → Task 5 ✓
- §5 lexicon + formula (0.70/0.45/0.175, unknown-neutral, ordering) → Tasks 1–2 (code + tests) ✓
- §6 dry-run-first idempotent backfill → Task 6 ✓
- §8 testing (infer_type buckets, edge_confidence cases, link integration, graph_review discrimination, bench green, backfill pure) → Tasks 1–6 ✓

**Placeholder scan:** No "TBD/handle edge cases". The one conditional (`find_entity_by_id` may not exist) gives an explicit fallback assertion, not a placeholder. PG-backed Task 3 test has an explicit run-the-test-PG instruction.

**Type consistency:** `infer_type(name)->str|None`, `edge_confidence(src,relation,dst)->float`, `TYPE_CONSTRAINTS` shape, and `recompute_rows(rows)->[(id,conf)]` are used identically across Tasks 1, 2, 3, 5, 6 and every test. Confidence constants `0.70/0.45/0.175` are consistent everywhere. `_DUBIOUS_CONF` stays `0.6` (Task 4 unchanged).

**Out-of-scope guard:** No task touches the extractor prompt, `extract_relations`, or `upsert_edge`'s re-assertion bump (noted in Task 3). No cross-session linking (that's "C").
