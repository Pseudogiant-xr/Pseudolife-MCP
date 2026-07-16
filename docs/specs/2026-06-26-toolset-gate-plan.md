# Toolset Gate + Redundancy Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink the MCP tool surface by folding 2 redundant tools away and adding a `PSEUDOLIFE_MCP_TOOLSET=full|core` gate (default `full`, non-breaking) that exposes a ~15-tool core set when opted in.

**Architecture:** All tools live in `pseudolife_memory/mcp_server.py` and register on a module-level `mcp = FastMCP(...)` via `@mcp.tool()`. We replace that decorator with a thin `@_tool(core=…)` wrapper that records each tool's tier in `_TOOL_TIERS` and registers it unless we're in `core` mode and the tool isn't core. Two redundant tools collapse into existing ones: `memory_trace` → `memory_search(explain=True)` (which calls the existing `service.trace`), and `get_neighbors` → a new `relation_filter` arg on `memory_graph`.

**Tech Stack:** Python 3.10+ (`mcp.server.fastmcp.FastMCP`), pytest + pytest-asyncio, Markdown.

## Global Constraints

- Run tests with the offline env (project gotcha): `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. Interpreter: `.venv/Scripts/python.exe`.
- **Default behaviour unchanged:** `PSEUDOLIFE_MCP_TOOLSET` defaults to `full`; in full mode every tool registers exactly as today (minus the 2 folded ones).
- No new dependencies. Env var only (no `config.yaml` knob).
- The Cortex Console (REST over `service.*`) must not be touched — only the MCP tool surface changes.
- Spec: `docs/specs/2026-06-26-toolset-gate-design.md`.
- Commit style: conventional commits; end the body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Out of scope: a 3rd `standard` tier; family-dispatcher consolidation; removing any tool beyond `memory_trace`/`get_neighbors`; live-daemon redeploy.

---

### Task 1: Fold `memory_trace` into `memory_search(explain=True)`

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` — `memory_search` (def at line 169, body ends ~276); delete `memory_trace` (decorator line 279, def 280–322).
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: existing `service.search(...)` and `service.trace(...)`.
- Produces: `memory_search(..., explain: bool = False)`. When `explain=True`, the returned dict additionally carries a `"trace"` key (the same dict the old `memory_trace` returned under `"trace"`). When `explain=False`, the response is unchanged (no `"trace"` key).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` (it already drives tools via `mcp.call_tool`; mirror the existing dispatch tests' fixture usage — use the same `warm`/service setup the other dispatch tests in this file use):

```python
def test_search_explain_attaches_trace_and_default_does_not(tmp_path):
    from pseudolife_memory import mcp_server
    _fresh_service(mcp_server, tmp_path)          # same helper the other dispatch tests use
    mcp_server.service.store("the gadget port is 8080", source="notes")

    plain = asyncio.run(mcp_server.mcp.call_tool("memory_search", {"query": "gadget port"}))
    explained = asyncio.run(mcp_server.mcp.call_tool(
        "memory_search", {"query": "gadget port", "explain": True}))

    plain_d = _tool_json(plain)                   # same unwrap helper the file already uses
    explained_d = _tool_json(explained)
    assert "trace" not in plain_d
    assert "trace" in explained_d and isinstance(explained_d["trace"], dict)


def test_memory_trace_tool_is_gone():
    from pseudolife_memory import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "memory_trace" not in names
```

(If `tests/test_mcp_server.py` has no `_fresh_service` / `_tool_json` helpers, reuse whatever the existing dispatch tests in that file already use to build a service and decode a `call_tool` result — do not invent new infrastructure.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py::test_search_explain_attaches_trace_and_default_does_not tests/test_mcp_server.py::test_memory_trace_tool_is_gone -v`
Expected: FAIL — `memory_search` has no `explain` param (unexpected keyword) and `memory_trace` still registered.

- [ ] **Step 3: Add the `explain` param to `memory_search` and attach the trace**

In `pseudolife_memory/mcp_server.py`, add `explain: bool = False,` to `memory_search`'s signature (after `bm25`), and just before `return result` (currently line 276) insert:

```python
    if explain:
        trace_out = service.trace(
            query=query, top_k=top_k, sources=sources, bands=bands,
            episodes=episodes, tags=tags, rerank=rerank, bm25=bm25,
        )
        result["trace"] = trace_out.get("trace")
```

Also add one line to the `memory_search` docstring Args block:

```
        explain: When True, also run the ranking tracer and attach a
            ``trace`` dict (per-tier candidates, multipliers, drop reasons) —
            the debug view formerly exposed as ``memory_trace``.
```

- [ ] **Step 4: Delete the `memory_trace` tool**

Remove the `@mcp.tool()` decorator (line 279) and the entire `def memory_trace(...)` function (through its `return service.trace(...)` and trailing blank lines, ~280–323).

- [ ] **Step 5: Update the `test_all_tools_registered` tripwire**

In `tests/test_mcp_server.py`, remove the `"memory_trace",` entry from the expected list in `test_all_tools_registered`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: PASS (incl. `test_all_tools_registered`, now 47 tools).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tools): fold memory_trace into memory_search(explain=True)

memory_search gains explain=False; explain=True attaches the service.trace
ranking-trace dict. memory_trace tool removed (-1). Per
docs/specs/2026-06-26-toolset-gate-design.md (P1.5).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Remove `get_neighbors`; add `relation_filter` to `memory_graph`

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` — `memory_graph` (def 1112–1142); delete `get_neighbors` (decorator 1145, def 1146–~1161).
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: existing `service.graph_neighborhood(entity, depth, include_facts, to)`.
- Produces: `memory_graph(entity, depth=1, include_facts=True, to=None, relation_filter=None)`. When `relation_filter` is set, only edges whose `relation` contains that substring (case-insensitive) are returned. `get_neighbors` no longer exists.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py` (reuse the file's existing service/dispatch helpers):

```python
def test_graph_relation_filter_keeps_only_matching_edges(tmp_path):
    from pseudolife_memory import mcp_server
    _fresh_service(mcp_server, tmp_path)
    s = mcp_server.service
    s.graph_relate("svc-a", "runs-on", "jvm-21")
    s.graph_relate("svc-a", "uses", "redis")

    out = _tool_json(asyncio.run(mcp_server.mcp.call_tool(
        "memory_graph", {"entity": "svc-a", "relation_filter": "runs-on"})))
    rels = {e["relation"] for e in out["edges"]}
    assert rels == {"runs-on"}


def test_get_neighbors_tool_is_gone():
    from pseudolife_memory import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "get_neighbors" not in names
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py::test_graph_relation_filter_keeps_only_matching_edges tests/test_mcp_server.py::test_get_neighbors_tool_is_gone -v`
Expected: FAIL — `memory_graph` rejects `relation_filter`; `get_neighbors` still registered.

- [ ] **Step 3: Add `relation_filter` to `memory_graph`**

In `pseudolife_memory/mcp_server.py`, change `memory_graph`'s signature to add `relation_filter: str | None = None,` (after `to`), and replace its body `return service.graph_neighborhood(...)` (lines 1140–1142) with:

```python
    out = service.graph_neighborhood(
        entity=entity, depth=depth, include_facts=include_facts, to=to,
    )
    if relation_filter and out.get("edges"):
        rf = relation_filter.lower()
        out = dict(out)
        out["edges"] = [e for e in out["edges"]
                        if rf in str(e.get("relation", "")).lower()]
    return out
```

Add to the `memory_graph` docstring Args block:

```
        relation_filter: Optional case-insensitive substring; keep only edges
            whose relation contains it (e.g. "runs-on"). Replaces the former
            get_neighbors convenience tool.
```

- [ ] **Step 4: Delete the `get_neighbors` tool**

Remove the `@mcp.tool()` decorator (line 1145) and the entire `def get_neighbors(...)` function (through its `return out` and trailing blank lines).

- [ ] **Step 5: Update the `test_all_tools_registered` tripwire**

In `tests/test_mcp_server.py`, remove the `"get_neighbors",` entry from the expected list.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: PASS (`test_all_tools_registered` now 46 tools).

- [ ] **Step 7: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tools): drop get_neighbors; add relation_filter to memory_graph

get_neighbors was a self-described affordance over memory_graph(depth=1);
its one extra (relation_filter) moves onto memory_graph (-1). memory_path
kept (distinct bidirectional BFS). Per
docs/specs/2026-06-26-toolset-gate-design.md (P1.5).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Add the `PSEUDOLIFE_MCP_TOOLSET` gate

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` — add the gate (near `mcp = FastMCP(...)`, line 80); convert every `@mcp.tool()` to `@_tool()` / `@_tool(core=True)`.
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces:
  - `_should_register(toolset: str, core: bool) -> bool` — pure: `True` unless `toolset == "core" and not core`.
  - `_TOOL_TIERS: dict[str, bool]` — maps every tool's name → its `core` flag (populated regardless of registration).
  - `_tool(*, core: bool = False)` — decorator: records the tier, registers via `mcp.tool()` when `_should_register(_TOOLSET, core)`.
  - `_TOOLSET: str` — `os.environ["PSEUDOLIFE_MCP_TOOLSET"]` lower-cased, default `"full"`.
- The 15 **core** tools (everything else is full-only): `memory_store`, `memory_search`, `memory_fact_get`, `memory_fact_set`, `memory_fact_resolve`, `memory_graph`, `memory_recall`, `memory_graph_relate`, `memory_world_search`, `memory_world_set`, `memory_lesson_search`, `memory_outcome`, `document_search`, `document_ingest`, `memory_stats`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
_EXPECTED_CORE = sorted([
    "memory_store", "memory_search", "memory_fact_get", "memory_fact_set",
    "memory_fact_resolve", "memory_graph", "memory_recall", "memory_graph_relate",
    "memory_world_search", "memory_world_set", "memory_lesson_search",
    "memory_outcome", "document_search", "document_ingest", "memory_stats",
])


def test_should_register_gate_logic():
    from pseudolife_memory.mcp_server import _should_register
    assert _should_register("full", core=False) is True
    assert _should_register("full", core=True) is True
    assert _should_register("core", core=True) is True
    assert _should_register("core", core=False) is False


def test_core_tier_membership_is_exactly_the_core_set():
    from pseudolife_memory import mcp_server
    core_names = sorted(n for n, is_core in mcp_server._TOOL_TIERS.items() if is_core)
    assert core_names == _EXPECTED_CORE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py::test_should_register_gate_logic tests/test_mcp_server.py::test_core_tier_membership_is_exactly_the_core_set -v`
Expected: FAIL — `_should_register` / `_TOOL_TIERS` don't exist yet.

- [ ] **Step 3: Add the gate primitives**

In `pseudolife_memory/mcp_server.py`, immediately after `mcp = FastMCP("Pseudolife Memory")` (line 80), add:

```python
# Tool-surface tier gate (P1.5). Default "full" = every tool registers (no
# behaviour change). "core" registers only the core-tier set — a lean opt-in
# for weak-model / public / token-conscious deployments. The Cortex Console is
# unaffected (it calls service.* over REST, not MCP tools).
_TOOLSET = os.environ.get("PSEUDOLIFE_MCP_TOOLSET", "full").strip().lower()
_TOOL_TIERS: dict[str, bool] = {}


def _should_register(toolset: str, core: bool) -> bool:
    """Register a tool unless we're in core mode and it's not core-tier."""
    return toolset != "core" or core


def _tool(*, core: bool = False):
    """Replacement for @mcp.tool() that records the tool's tier and gates
    registration on PSEUDOLIFE_MCP_TOOLSET."""
    def deco(fn):
        _TOOL_TIERS[fn.__name__] = core
        if _should_register(_TOOLSET, core):
            return mcp.tool()(fn)
        return fn  # left callable (tests / Console-via-service); not exposed
    return deco
```

(Confirm `import os` is already present near the top of the module — it is used by the existing config; if not, add it.)

- [ ] **Step 4: Convert all decorators**

Replace every `@mcp.tool()` line in `pseudolife_memory/mcp_server.py` with `@_tool()`. Then, for exactly the 15 core functions listed in this task's Interfaces block, change their decorator to `@_tool(core=True)`. (Search each `def <name>(` and edit the decorator directly above it.)

- [ ] **Step 5: Run the new tests + the full registration tripwire**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_mcp_server.py -v`
Expected: PASS — gate-logic + membership tests green; `test_all_tools_registered` still lists all 46 (default `full` registers everything); `test_each_tool_has_non_empty_docstring` still green.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tools): PSEUDOLIFE_MCP_TOOLSET gate (full default, core opt-in)

_tool() wrapper records each tool's tier in _TOOL_TIERS and registers it
unless toolset=core and the tool isn't core. 15 core tools; full default =
no behaviour change. Per docs/specs/2026-06-26-toolset-gate-design.md (P1.5).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Docs — README, compose, CHANGELOG

**Files:**
- Modify: `README.md` (tools table + "Weak-model deployments" note + a core-tier list).
- Modify: `ops/docker-compose.yml` (commented `PSEUDOLIFE_MCP_TOOLSET` line).
- Modify: `CHANGELOG.md`.

**Interfaces:** docs only; no code.

- [ ] **Step 1: Update the README tools table**

In `README.md`'s "Tools exposed" table: delete the `memory_trace` row and the `get_neighbors` row; change the `memory_search` row signature to include `explain?` and the `memory_graph` row to include `relation_filter?`. Then, immediately below the table, add:

```markdown
**Toolset tiers.** Set `PSEUDOLIFE_MCP_TOOLSET=core` to expose only the lean
**core** set (the rest stay available with the default `full`): `memory_store`,
`memory_search`, `memory_fact_get`, `memory_fact_set`, `memory_fact_resolve`,
`memory_graph`, `memory_recall`, `memory_graph_relate`, `memory_world_search`,
`memory_world_set`, `memory_lesson_search`, `memory_outcome`, `document_search`,
`document_ingest`, `memory_stats`. Recommended for weak-model / public /
token-conscious deployments. The Cortex Console is unaffected (it talks REST).
```

- [ ] **Step 2: Update the "Weak-model deployments" note**

In `README.md`, replace the existing "**Weak-model deployments:** expose only …" paragraph (~line 144) with a pointer to the gate:

```markdown
**Weak-model deployments:** set `PSEUDOLIFE_MCP_TOOLSET=core` (above) — it exposes
the curated core set and hides the power/hygiene tools (`*_forget`, `memory_delete`,
`memory_relation_define`, the dream internals, …) that a small model can misuse.
```

- [ ] **Step 3: Add the commented env to compose**

In `ops/docker-compose.yml`, in the `pseudolife-daemon` service's `environment:` block (near `PSEUDOLIFE_WRITER_ID`), add:

```yaml
      # Tool-surface tier: "full" (default, all tools) or "core" (lean ~15-tool
      # set for weak-model / token-conscious clients). Uncomment to opt in.
      # PSEUDOLIFE_MCP_TOOLSET: core
```

- [ ] **Step 4: Add a CHANGELOG entry**

In `CHANGELOG.md`, under the current top release section's `### Changed` (or a new `### Changed` under `[Unreleased]` if you opened one), add:

```markdown
- **Tool-surface gate + redundancy trim.** `PSEUDOLIFE_MCP_TOOLSET=core` exposes a
  lean 15-tool core set (default `full` = unchanged). Folded `memory_trace` into
  `memory_search(explain=True)` and dropped `get_neighbors` (its `relation_filter`
  moved onto `memory_graph`); `memory_path` retained. 48 → 46 tools.
```

- [ ] **Step 5: Verify the docs**

Run: `grep -n "PSEUDOLIFE_MCP_TOOLSET" README.md ops/docker-compose.yml CHANGELOG.md && grep -c "memory_trace\|get_neighbors" README.md`
Expected: the env var appears in all three files; the second grep prints `0` (both removed tool names gone from the README).

- [ ] **Step 6: Commit**

```bash
git add README.md ops/docker-compose.yml CHANGELOG.md
git commit -m "docs(tools): document PSEUDOLIFE_MCP_TOOLSET core tier + tool folds (P1.5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Part A.1 (fold memory_trace) → Task 1. ✓
- Part A.2 (remove get_neighbors, add relation_filter) → Task 2. ✓
- Part A.3 (keep memory_path) → untouched (no task needed). ✓
- Part B (gate, default full, `_tool` wrapper, core membership) → Task 3. ✓
- "test_all_tools_registered toolset-aware / drift-proof" → Task 3 (`_TOOL_TIERS` membership test) + Tasks 1-2 keep the full tripwire current. ✓
- Affected files (README tiers + weak-model note, compose comment, CHANGELOG) → Task 4. ✓
- Success criteria 1 (full=46) → Tasks 1-2 tripwire; 2 (core=15) → Task 3 membership + gate-logic; 3 (explain/relation_filter parity) → Tasks 1-2 behavioural tests; 4 (toolset-aware test, suite green) → Task 3 + each task's run. ✓

**Placeholder scan:** none — every code step shows the code; every test step shows the assertions and the exact run command + expected result. The one soft reference ("reuse the file's existing service/dispatch helpers") is deliberate: `tests/test_mcp_server.py` already has dispatch tests, and inventing parallel fixtures would violate DRY — the implementer must use what's there.

**Type consistency:** `_should_register(toolset: str, core: bool) -> bool`, `_TOOL_TIERS: dict[str, bool]`, and `_tool(*, core: bool=False)` are used identically in Task 3's code and tests. `explain: bool` (Task 1) and `relation_filter: str | None` (Task 2) match between signature, body, and tests. The 15 core names are identical in Task 3's Interfaces, its code step, and `_EXPECTED_CORE`.
