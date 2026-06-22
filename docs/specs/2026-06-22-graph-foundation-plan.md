# Graph Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Postgres `entities` hub the single source of truth for the
knowledge graph with NetworkX as a derived read-model behind a swappable
`GraphStore` port, and remove Apache AGE entirely.

**Architecture:** Today the graph lives in three places — Postgres relational
tables (canonical), NetworkX (`graph.py`, derive-on-read), and an Apache AGE
mirror (read by one Cypher tool). This plan excises AGE, then routes all
service-level graph access through a new `GraphStore` Protocol whose default
implementation (`PostgresNetworkxGraphStore`) wraps the existing Postgres edge
methods + `graph.py`. Entities stay pinned to Postgres (facts/lessons/world FK
into them); only edges + traversal + the relation registry sit behind the port,
so a future AGE/graph-DB backend is a contained swap.

**Tech Stack:** Python 3.11+, Postgres (pgvector), psycopg, NetworkX, FastMCP,
pytest.

## Global Constraints

- Work on branch `feat/graph-foundation` (already checked out; the design spec is
  committed there).
- Run tests with: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest`
  (offline env is required for determinism; all models are cached locally).
- PG-backed tests use the `pg_conn` / `pg_url` fixtures from `tests/pg_fixtures`
  and **skip cleanly** when no test Postgres is reachable — that is expected, not
  a failure.
- Single-writer cortex: do not add a second writer path. All graph writes stay on
  the service under its existing coarse lock.
- End every commit message with the trailer:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- Entities are pinned to Postgres — never move `ensure_entity` / `find_entity` /
  `add_alias` onto the `GraphStore` port. The port owns edges, the relation
  registry, and traversal only.
- No new third-party dependencies.

---

### Task 1: Excise Apache AGE from the Python package

Remove every AGE code path. After this task the service writes/reads the graph
straight through Postgres + `graph.py` (no port yet, no AGE). Behavior of the 5
kept graph tools is unchanged; the `memory_graph_query` Cypher tool and `age-sync`
CLI are gone.

**Files:**
- Delete: `pseudolife_memory/storage/age.py`
- Delete: `ops/migrate_v04.py` (spent one-time AGE-graph-rename migration; its only
  reason to exist was managing the AGE graph name, now moot — it imports the
  deleted `AgeGraph`)
- Modify: `pseudolife_memory/service.py` (remove `self._age`, AGE init block,
  `_age_mirror`, the `_age_mirror` call-sites, `graph_cypher`, `age_sync`)
- Modify: `pseudolife_memory/mcp_server.py:1145-1171` (delete `memory_graph_query`)
- Modify: `pseudolife_memory/cli.py:6,29-44,47` (delete the `age-sync` mode)
- Modify: `pseudolife_memory/storage/schema.py:252-258,278` (drop the AGE
  extension probe; `ensure_schema` no longer returns `age_available`)
- Modify: `pseudolife_memory/storage/postgres.py:131` (`SET search_path TO public`
  — drop `, ag_catalog`)
- Modify: `pseudolife_memory/utils/config.py` (remove the `graph.name` field used
  only for the AGE graph name — see Step 7)
- Test: `tests/test_graph.py` (delete the AGE-gated tests; add a no-AGE guard)

**Interfaces:**
- Consumes: existing `PostgresStorage` graph methods (`ensure_entity`,
  `find_entity`, `add_alias`, `load_relations`, `upsert_relation`, `upsert_edge`,
  `supersede_edge`, `load_graph`) — all unchanged.
- Produces: a service with no AGE attributes/methods; `ensure_schema(conn)`
  returns `{}` (no `age_available` key).

- [ ] **Step 1: Write the failing guard test**

In `tests/test_graph.py`, add at the end:

```python
def test_no_age_imports_remain():
    """AGE is removed — no module should import it or call cypher()."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "pseudolife_memory"
    offenders = []
    for p in root.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "storage.age" in text or "AgeGraph" in text or ".cypher(" in text:
            offenders.append(p.name)
    assert offenders == [], f"AGE references remain in: {offenders}"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph.py::test_no_age_imports_remain -v`
Expected: FAIL (AGE references still present in service.py / age.py).

- [ ] **Step 3: Delete the AGE module and the spent migration**

```bash
git rm pseudolife_memory/storage/age.py ops/migrate_v04.py
```

- [ ] **Step 4: Remove AGE from `service.py`**

- Delete line 269 `self._age = None  # ...`.
- Delete the AGE init block in `_ensure_init` (lines 371-379, the
  `if self._storage.capabilities.get("age_available"):` ... `AGE init failed` block).
- Delete `_age_mirror` (lines 563-571).
- In `_ensure_entity_graph` (the helper ending ~line 561) delete the line
  `self._age_mirror(lambda: self._age.upsert_entity(n, entity.strip()))` so it ends
  at `self._storage.ensure_entity(n, display=entity.strip())`.
- In `_resolve_or_create_entity` delete line 2129
  (`self._age_mirror(lambda: self._age.upsert_entity(n, name.strip(), etype))`).
- In `graph_relate` delete the `self._age_mirror(lambda: self._age.upsert_edge(...))`
  block (≈2182-2183).
- In `graph_unrelate` delete the `self._age_mirror(lambda: self._age.remove_edge(...))`
  block (≈2213-2214).
- In `_link_lesson_graph` delete the three `_age_mirror` lines (1390, 1397, 1400).
- Delete the whole `graph_cypher` method (2372-2396) and the whole `age_sync`
  method (2398-2406).
- In `_assert_public_search_path` (282-299): keep the `public`-present check; you
  may simplify the `$user`/AGE wording in the docstring/comments but **keep the
  function** (it is still valid search_path hygiene).

- [ ] **Step 5: Remove the `memory_graph_query` tool**

In `mcp_server.py`, delete the entire `@mcp.tool()` `def memory_graph_query(...)`
block (lines 1145-1171).

- [ ] **Step 6: Remove the `age-sync` CLI mode**

In `cli.py`: delete the `* ``pseudolife-mcp age-sync`` ...` docstring line (6);
delete the `elif mode == "age-sync":` branch (29-44); update the usage string (47)
to `"unknown mode {mode!r}; use: serve | embedded | (no arg = shim)"`.

- [ ] **Step 7: Drop the AGE extension probe + graph-name config + search_path**

- `storage/schema.py`: in `ensure_schema`, delete the `age_available = True` /
  `try: CREATE EXTENSION ... age ... except` block (252-258) and change the final
  `return {"age_available": age_available}` (278) to `return {}`. Update the
  docstring (239-245) to drop the AGE sentence.
- `storage/postgres.py:131`: change `self.conn.execute("SET search_path TO public, ag_catalog")`
  to `self.conn.execute("SET search_path TO public")`. Update the comment block
  (125-130) to drop the AGE-schema reasoning (keep the "pin to public" point).
- `tests/pg_fixtures.py:75`: same change — `SET search_path TO public, ag_catalog`
  → `SET search_path TO public`; trim the AGE-graph-schema sentence from the
  comment (70-74), keep the "pin to public before truncate" rationale.
- `utils/config.py`: `GraphConfig` (lines 470-476) exists only for the AGE graph
  name. Delete the whole `class GraphConfig` (470-476), the
  `graph: GraphConfig = field(default_factory=GraphConfig)` line (507), and the
  loader block `if "graph" in raw: config.graph = _dict_to_dataclass(GraphConfig, raw["graph"])`
  (618-619). Then confirm nothing else references it:
  `rg "config\.graph|GraphConfig" pseudolife_memory` returns nothing (the AGE init
  that used `self.config.graph.name` was removed in Step 4).

- [ ] **Step 8: Delete the AGE-gated tests**

In `tests/test_graph.py`: delete the AGE test section (the tests guarded by an
"AGE extension installed" skip, and the docstring bullet "AGE tests, skipped when
the extension isn't installed" on line 7). Keep all pure-logic, storage-CRUD, and
service-level (non-AGE) tests. In the `ensure_schema`/search_path test (~160-165)
remove the assertions that reference the AGE graph schema; keep the
public-search-path assertion.

- [ ] **Step 9: Run the guard test + full graph suite**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph.py -v`
Expected: PASS (including `test_no_age_imports_remain`; no AGE tests collected).

- [ ] **Step 10: Run the whole suite**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest`
Expected: PASS (AGE-dependent tests gone; everything else green). Investigate any
failure that mentions `age`, `_age`, `cypher`, or `graph_query` and fix the stray
reference.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "refactor(graph): remove Apache AGE (mirror, cypher tool, age-sync)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Add the `GraphStore` port + default Postgres/NetworkX impl

Introduce the swap point. Additive only — nothing calls it yet.

**Files:**
- Create: `pseudolife_memory/memory/graph_store.py`
- Test: `tests/test_graph_store.py`

**Interfaces:**
- Consumes: `PostgresStorage` (`load_graph`, `load_relations`, `upsert_relation`,
  `upsert_edge`, `supersede_edge`) and `pseudolife_memory.graph`
  (`build_subgraph`, with relation registry shaped `{name: {transitive, inverse_of}}`).
- Produces:
  - `class GraphStore(Protocol)` with: `upsert_edge(src_id:int, relation:str,
    dst_id:int, *, confidence:float=0.8, origin:str|None=None) -> dict`;
    `supersede_edge(src_id:int, relation:str, dst_id:int) -> bool`;
    `load_relations() -> list[dict]`; `upsert_relation(name:str, description:str,
    *, src_type=None, dst_type=None, transitive=False, inverse_of=None) -> None`;
    `subgraph(root_id:int, *, depth:int=1, to_id:int|None=None) -> dict`.
  - `class PostgresNetworkxGraphStore` implementing it, constructed as
    `PostgresNetworkxGraphStore(storage)`.
  - `subgraph(...)` returns `{"nodes": set[int], "edges": [{src,relation,dst,
    derived,via,confidence,origin}], "paths": [[int,...]], "entities": {id: {id,
    canonical,display,etype}}, "aliases": {id: [str,...]}}`.

- [ ] **Step 1: Write the failing contract test**

Create `tests/test_graph_store.py`:

```python
"""Backend-agnostic contract for the GraphStore port. The default
PostgresNetworkxGraphStore must pass; any future backend must pass the same.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture
def graph_store(pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
    return PostgresNetworkxGraphStore(PostgresStorage(pg_url))


def test_upsert_and_subgraph_returns_edge(graph_store):
    st = graph_store._st
    a = st.ensure_entity("svc-a")
    b = st.ensure_entity("host-b")
    graph_store.upsert_edge(a, "runs-on", b, confidence=0.8)
    sub = graph_store.subgraph(a, depth=1)
    pairs = {(e["src"], e["relation"], e["dst"]) for e in sub["edges"]}
    assert (a, "runs-on", b) in pairs
    assert a in sub["nodes"] and b in sub["nodes"]


def test_subgraph_derives_transitive(graph_store):
    st = graph_store._st
    a = st.ensure_entity("a-pkg")
    b = st.ensure_entity("b-pkg")
    c = st.ensure_entity("c-pkg")
    graph_store.upsert_edge(a, "depends-on", b)
    graph_store.upsert_edge(b, "depends-on", c)
    sub = graph_store.subgraph(a, depth=3)
    derived = {(e["src"], e["dst"]) for e in sub["edges"] if e["derived"]}
    assert (a, c) in derived  # transitive depends-on closure


def test_supersede_hides_edge(graph_store):
    st = graph_store._st
    a = st.ensure_entity("x-svc")
    b = st.ensure_entity("y-svc")
    graph_store.upsert_edge(a, "uses", b)
    assert graph_store.supersede_edge(a, "uses", b) is True
    sub = graph_store.subgraph(a, depth=1)
    base = {(e["src"], e["relation"], e["dst"])
            for e in sub["edges"] if not e["derived"]}
    assert (a, "uses", b) not in base
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph_store.py -v`
Expected: FAIL with `ModuleNotFoundError: ...graph_store` (skips instead if no
test Postgres — start the test PG, see `tests/pg_fixtures`).

- [ ] **Step 3: Implement `graph_store.py`**

Create `pseudolife_memory/memory/graph_store.py`:

```python
"""GraphStore — the swappable graph backend port (design 2026-06-22).

The Postgres `entities` table is the source of truth and is shared with
facts/lessons/world (they FK into it), so entities are NOT part of this port.
The port owns edges, the relation registry, and traversal/derivation — the
parts a future AGE / graph-DB backend could own. The default impl wraps the
existing PostgresStorage edge methods + the NetworkX derivation in
``pseudolife_memory.graph``.
"""
from __future__ import annotations

from typing import Any, Protocol

from pseudolife_memory.graph import build_subgraph


class GraphStore(Protocol):
    def upsert_edge(self, src_id: int, relation: str, dst_id: int, *,
                    confidence: float = 0.8, origin: str | None = None) -> dict: ...

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool: ...

    def load_relations(self) -> list[dict]: ...

    def upsert_relation(self, name: str, description: str, *,
                        src_type: str | None = None, dst_type: str | None = None,
                        transitive: bool = False,
                        inverse_of: str | None = None) -> None: ...

    def subgraph(self, root_id: int, *, depth: int = 1,
                 to_id: int | None = None) -> dict: ...


class PostgresNetworkxGraphStore:
    """Default GraphStore: Postgres edge tables + NetworkX derive-on-read."""

    def __init__(self, storage) -> None:
        self._st = storage

    # ── writes (delegate to the hub's edge/relation tables) ─────────────
    def upsert_edge(self, src_id: int, relation: str, dst_id: int, *,
                    confidence: float = 0.8, origin: str | None = None) -> dict:
        return self._st.upsert_edge(src_id, relation, dst_id,
                                    confidence=confidence, origin=origin)

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool:
        return self._st.supersede_edge(src_id, relation, dst_id)

    def load_relations(self) -> list[dict]:
        return self._st.load_relations()

    def upsert_relation(self, name: str, description: str, *,
                        src_type: str | None = None, dst_type: str | None = None,
                        transitive: bool = False,
                        inverse_of: str | None = None) -> None:
        self._st.upsert_relation(name, description, src_type=src_type,
                                 dst_type=dst_type, transitive=transitive,
                                 inverse_of=inverse_of)

    # ── reads / traversal (NetworkX derive-on-read over the hub) ────────
    def subgraph(self, root_id: int, *, depth: int = 1,
                 to_id: int | None = None) -> dict[str, Any]:
        g = self._st.load_graph()
        registry = {r["name"]: {"transitive": r["transitive"],
                                "inverse_of": r["inverse_of"]}
                    for r in self._st.load_relations()}
        edges = [{"src": e["src_id"], "relation": e["relation"],
                  "dst": e["dst_id"], "confidence": e["confidence"],
                  "origin": e["origin"]} for e in g["edges"]]
        sub = build_subgraph(edges, registry, root_id, depth=depth, to=to_id)
        return {
            "nodes": sub["nodes"], "edges": sub["edges"], "paths": sub["paths"],
            "entities": {e["id"]: e for e in g["entities"]},
            "aliases": g["aliases"],
        }
```

- [ ] **Step 4: Run the contract test to verify it passes**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph_store.py -v`
Expected: PASS (or skip if no test PG — then run once against the compose PG to
confirm green before moving on).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/graph_store.py tests/test_graph_store.py
git commit -m "feat(graph): add swappable GraphStore port + Postgres/NetworkX impl

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Route the service's graph access through the port

Replace the service's direct `storage.upsert_edge` / inline `build_subgraph`
calls with the `GraphStore`. Behavior is identical, so existing service graph
tests are the regression net.

**Files:**
- Modify: `pseudolife_memory/service.py` (construct `self._graph` in `_ensure_init`;
  route `graph_relate`, `graph_unrelate`, `relation_define`, `graph_neighborhood`,
  `_link_lesson_graph` through it)
- Test: `tests/test_graph.py` (existing service-level tests — unchanged, must stay
  green)

**Interfaces:**
- Consumes: `PostgresNetworkxGraphStore` from Task 2.
- Produces: `self._graph: GraphStore | None` on the service, constructed when
  storage is present.

- [ ] **Step 1: Confirm the existing service graph tests pass first (baseline)**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph.py -k "relate or neighbor or alias or relation" -v`
Expected: PASS (these are the behaviors this task must preserve).

- [ ] **Step 2: Construct the port in `_ensure_init`**

In `service.py` `__init__`, add beside the other attrs: `self._graph = None  # GraphStore`.
In `_ensure_init`, right after `self._storage = PostgresStorage(self._db_url)` and
the `_assert_public_search_path()` call, add:

```python
from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
self._graph = PostgresNetworkxGraphStore(self._storage)
```

- [ ] **Step 3: Route the write paths through `self._graph`**

- `graph_relate`: replace `registry = {r["name"]: r for r in st.load_relations()}`
  source with `self._graph.load_relations()`, and replace
  `edge = st.upsert_edge(src_e["id"], resolved, dst_e["id"], confidence=confidence, origin=origin)`
  with `edge = self._graph.upsert_edge(src_e["id"], resolved, dst_e["id"], confidence=confidence, origin=origin)`.
  (Entity resolution via `self._resolve_or_create_entity` stays — entities are
  pinned to the hub.)
- `graph_unrelate`: replace `st.load_relations()` with `self._graph.load_relations()`
  and `st.supersede_edge(...)` with `self._graph.supersede_edge(...)`.
- `relation_define`: replace `registry = {r["name"]: r for r in st.load_relations()}`
  with `self._graph.load_relations()` and `st.upsert_relation(...)` with
  `self._graph.upsert_relation(...)`.
- `_link_lesson_graph`: replace its `st.upsert_edge(tid, relation, oid, confidence=0.7, origin="action")`
  with `self._graph.upsert_edge(tid, relation, oid, confidence=0.7, origin="action")`.
  (The `ensure_entity` calls stay on `st` — entities are pinned.)

- [ ] **Step 4: Route `graph_neighborhood` read through `self._graph.subgraph`**

Replace the body from `g = st.load_graph()` through the `sub = G.build_subgraph(...)`
+ `by_id = {...}` lines with:

```python
            reg_for_view = self._graph.subgraph(
                root["id"], depth=depth, to_id=to_id)
            sub = {"nodes": reg_for_view["nodes"],
                   "edges": reg_for_view["edges"],
                   "paths": reg_for_view["paths"]}
            by_id = reg_for_view["entities"]
            aliases = reg_for_view["aliases"]
```

Then update the two later references that read `g["aliases"]` to use `aliases`
(line ~2339 `"aliases": g["aliases"].get(nid, [])` → `aliases.get(nid, [])`). The
cortex-facts attachment and the `_disp` / `out_edges` formatting below stay exactly
as-is (they already read `by_id` and `sub`). Remove the now-unused `registry = {...}`
build and the `edges = [...]` build that fed `build_subgraph` (the port does that
now), and the `from pseudolife_memory import graph as G` import is still needed for
`G.norm_name` / `G.MAX_DEPTH` — keep it.

- [ ] **Step 5: Run the service graph tests**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest tests/test_graph.py -v`
Expected: PASS — identical neighborhoods, derived edges, paths, and relate/alias
behavior as the Step 1 baseline.

- [ ] **Step 6: Run the whole suite**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/service.py
git commit -m "refactor(graph): route service graph access through GraphStore port

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Ops — drop-AGE migration + pgvector-only Postgres image

Code no longer needs AGE; now make the deployment match. These are deploy/ops
steps verified by hand against the live stack, not unit tests (back up first,
recreate only the daemon/PG, never `down -v`).

**Files:**
- Create: `ops/migrate_drop_age.py`
- Modify: `ops/Dockerfile.pg`

**Interfaces:**
- Consumes: nothing new.
- Produces: a Postgres with no `age` extension / `pseudolife_graph`. (Note:
  `daemon.py` `_health` already reports `SCHEMA_META_VERSION` — the fresh-eyes F5
  fix is already in the tree, so no /health change is needed. Dropping AGE is not
  a relational-schema change, so `SCHEMA_META_VERSION` is NOT bumped.)

- [ ] **Step 1: Write the drop-AGE migration script**

Create `ops/migrate_drop_age.py`:

```python
"""One-time: drop the AGE graph + extension from an existing bank.

Edges live in the relational `edges` table (the source of truth), so dropping
the AGE graph is zero data loss. Run while the Postgres image still has the AGE
binary (apache/age), THEN switch the image (Step 3). Back up first.
"""
from __future__ import annotations

import os

import psycopg


def main() -> None:
    dsn = os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP EXTENSION IF EXISTS age CASCADE")
        print("migrate_drop_age: dropped AGE extension + graph (if present)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Back up, then run the migration against the live bank**

Run (PowerShell): `pwsh ops/backup.ps1`
Then: `.venv\Scripts\python.exe ops/migrate_drop_age.py`
Expected: prints "dropped AGE extension + graph (if present)". Verify the bank is
intact: `psql` → `SELECT count(*) FROM public.facts;` and `... FROM public.entries;`
return the pre-migration counts; `\dn` shows no `pseudolife_graph` / `ag_catalog`.

- [ ] **Step 3: Switch the Postgres image off Apache AGE**

In `ops/Dockerfile.pg`, replace the `FROM apache/age:release_PG16_1.5.0` base with
a pgvector-only Postgres 16 base (`FROM pgvector/pgvector:pg16`) and delete the
AGE-specific build/comment lines. Keep everything pgvector/locale-related.
Rebuild + recreate **only** the PG container (data is on the external volume;
never `down -v`):

```bash
docker compose -f ops/docker-compose.yml --env-file ops/.env build pseudolife-pg
docker compose -f ops/docker-compose.yml --env-file ops/.env up -d pseudolife-pg
```

Expected: PG starts; `SELECT extname FROM pg_extension;` lists `vector`, not
`age`. (If PG fails to start complaining about missing `age`, Step 2 was skipped —
the volume still has AGE catalog objects; run Step 2 on the old image first.)

- [ ] **Step 4: Rebuild + recreate the daemon, verify health + graph**

The daemon image must be rebuilt to pick up the Task 1-3 code changes:

```bash
docker compose -f ops/docker-compose.yml --env-file ops/.env build pseudolife-daemon
docker compose -f ops/docker-compose.yml --env-file ops/.env up -d pseudolife-daemon
```

Then probe `http://127.0.0.1:8765/health`: HTTP 200 with `"schema"` =
`SCHEMA_META_VERSION` (already wired — no code change). Smoke a graph round-trip
over MCP: `memory_graph_relate("a-svc","runs-on","b-host")` then
`memory_graph("a-svc")` returns the edge.

- [ ] **Step 5: Commit**

```bash
git add ops/migrate_drop_age.py ops/Dockerfile.pg
git commit -m "ops(graph): drop-AGE migration + pgvector-only Postgres image

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Docs + CHANGELOG + final verification

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `README.md` (remove `memory_graph_query` / AGE / Cypher / `age-sync`
  mentions; note the graph is Postgres + NetworkX behind a `GraphStore` port)
- Modify: `pseudolife_memory/mcp_server.py` (module docstring/tool count if it
  enumerates tools or says "42 tools")

**Interfaces:**
- Consumes: nothing new.
- Produces: docs consistent with the AGE-free graph.

- [ ] **Step 1: Update CHANGELOG**

Add an Unreleased entry:

```markdown
### Changed
- Graph layer: single source of truth (Postgres `entities` hub + NetworkX
  read-model) behind a swappable `GraphStore` port. Apache AGE removed.

### Removed
- `memory_graph_query` (raw read-only Cypher) MCP tool and the `pseudolife-mcp
  age-sync` CLI mode. Multi-hop queries are served by `memory_graph`
  (neighborhood + derived/inverse edges + shortest path). The Postgres image no
  longer requires the Apache AGE extension.
```

- [ ] **Step 2: Scrub README + mcp_server docstring**

Search `README.md` and `mcp_server.py` for `AGE`, `Cypher`, `memory_graph_query`,
`age-sync` and remove/rewrite those passages. Where a tool count is stated, reduce
it by one (the removed Cypher tool). Describe the graph as "Postgres + NetworkX
behind a `GraphStore` interface; no AGE/Cypher dependency."

- [ ] **Step 3: Final full-suite run**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv\Scripts\python.exe -m pytest`
Expected: PASS. Also run the grep gate manually:
`rg -i "ag_catalog|AgeGraph|cypher|age-sync|age_available" pseudolife_memory` →
no live code paths (docstrings already scrubbed).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md README.md pseudolife_memory/mcp_server.py
git commit -m "docs(graph): document AGE removal + GraphStore foundation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Success criteria (from the design)

1. Full suite green; `tests/test_graph_store.py` contract test passes.
2. `rg -i 'ag_catalog|AgeGraph|cypher|age-sync|age_available'` over
   `pseudolife_memory/` returns no live code paths.
3. Daemon image builds + boots + all 5 graph tools work against a Postgres with no
   AGE extension.
4. `memory_graph` returns identical neighborhoods / derived edges / paths as before.
