# Toolset gate + redundancy reduction — design

**Date:** 2026-06-26 · **Status:** approved (design), pending plan
**Background:** full-review item **P1.5**; fresh-eyes review **F4** ("shrink/gate
the 42-tool surface before it grows further" — it has since grown to **48**).

## Goal

Cut the agent-facing MCP tool surface two ways: **remove genuine redundancy**, and
**gate** the bulk of the surface behind an opt-in tier so a default deployment can
expose a lean set without losing any capability. The default stays the **full**
surface (no behaviour change for the existing single-user daemon); `core` is a
documented opt-in for weak-model / public / token-conscious deployments.

## Decision

**Reduce + gate**, default `full`. Confirmed against the code and the
recall-hub-gating design (`docs/specs/2026-06-24-recall-hub-gating-design.md:133`):

- `get_neighbors` — its own spec calls it *"overlaps `memory_graph` purely as a
  clearer affordance … no new graph logic."* → **remove**.
- `memory_trace` — *"same envelope as `memory_search`"* + a `trace` dict (it calls a
  separate `service.trace()`). → **fold** into `memory_search(explain=True)`.
- `memory_path` — the same spec says it is *"**not** a wrapper over
  `graph_neighborhood`'s `to=` branch"*; it is a targeted bidirectional BFS that
  reaches long shortest paths without the hub-explosion that raising
  `memory_graph`'s depth cap would cause. → **keep** (it is not redundant), gate it.

## Part A — Reduce (48 → 46, zero capability loss)

1. **Fold `memory_trace` → `memory_search(explain: bool = False)`.** When `explain`
   is true, `memory_search` additionally calls the existing `service.trace(...)` and
   attaches its `trace` dict to the normal (cortex-first) search result. When false,
   the response is byte-identical to today (no `trace` key). Delete the
   `memory_trace` tool.
2. **Remove `get_neighbors`; add `relation_filter` to `memory_graph`.**
   `memory_graph(entity, depth=1, include_facts=True, to=None, relation_filter=None)`
   — when `relation_filter` is set, keep only edges whose relation contains that
   substring (case-insensitive), reproducing `get_neighbors`'s one extra over a
   plain `memory_graph(depth=1)`.
3. **Keep `memory_path`** unchanged.

## Part B — Gate

A single deployment switch, read once at import:

```
PSEUDOLIFE_MCP_TOOLSET = full | core      # default: full
```

Registration goes through a thin wrapper that marks each tool's tier and, in
`core` mode, registers only core tools:

```python
_TOOLSET = os.environ.get("PSEUDOLIFE_MCP_TOOLSET", "full").strip().lower()

def _tool(*, core: bool = False):
    """Register a tool unless we're in core mode and it's not core-tier."""
    def deco(fn):
        if _TOOLSET != "core" or core:
            return mcp.tool()(fn)
        return fn  # left callable (tests / Console-via-service) but not exposed
    return deco
```

Every `@mcp.tool()` becomes `@_tool()` (full-only) or `@_tool(core=True)` (core).
The tier lives **at the tool definition**, so it cannot drift from a separate list
(the failure mode that broke `test_all_tools_registered`). `full` mode registers
all 46; `core` registers only the core set below. The Cortex Console is unaffected
(it calls `service.*` over REST, not MCP tools), and `core` mode loses no
capability — operators who need a gated tool flip the env to `full`.

## Core membership (~15)

Read/write across all four memory layers + graph + recall + docs + health:

| Layer | Core tools |
|---|---|
| Associative | `memory_store`, `memory_search` |
| Cortex | `memory_fact_get`, `memory_fact_set`, `memory_fact_resolve` |
| Graph / recall | `memory_graph`, `memory_recall`, `memory_graph_relate` |
| World | `memory_world_search`, `memory_world_set` |
| Lessons | `memory_lesson_search`, `memory_outcome` |
| Docs | `document_search`, `document_ingest` |
| Health | `memory_stats` |

Everything else is `full`-only: the `*_forget` / `memory_delete` / `memory_supersede`
hygiene tools; the dream internals (`dream_pull/commit/status/run`); the "list-all"
introspection (`memory_facts`, `memory_world_facts`, `memory_lessons`,
`memory_list_sources`, `memory_list_tags`, `memory_recent`); episodes
(`episode_start/end/list/summary`); manual consolidation
(`consolidation_candidates`, `consolidate`); graph admin (`relation_define`,
`graph_unrelate`, `alias`); `memory_path`; graph-insight (`digest`, `communities`);
engram (`memory_get`, `memory_reinforce`); `memory_history`; and `memory_save`.

## Affected files

- `pseudolife_memory/mcp_server.py` — the `_TOOLSET` read + `_tool` wrapper; convert
  all `@mcp.tool()` decorators; `memory_search` gains `explain`; `memory_graph` gains
  `relation_filter`; delete `memory_trace` and `get_neighbors`.
- `tests/test_mcp_server.py` — `test_all_tools_registered` becomes **toolset-aware**:
  assert the full set (46) by default, plus a `core`-mode test (set the env +
  `importlib.reload(mcp_server)`) asserting exactly the core set registers.
- `README.md` — tools table: drop the `memory_trace` / `get_neighbors` rows, note
  `memory_search(explain=…)` and `memory_graph(relation_filter=…)`, annotate each
  tool's tier; rewrite the "Weak-model deployments" note to point at
  `PSEUDOLIFE_MCP_TOOLSET=core`.
- `ops/docker-compose.yml` — a documented (commented) `PSEUDOLIFE_MCP_TOOLSET`
  line; **no default change** (stays `full`).
- `CHANGELOG.md`.

## Out of scope

- A third `standard` tier — two tiers (`full` / `core`) only; revisit if needed.
- Aggressive family-dispatcher consolidation (one `memory_admin` over the forgets,
  etc.) — bigger, more breaking; not now.
- Removing any tool beyond `memory_trace` and `get_neighbors`.
- A config-file (`config.yaml`) knob — env var only, matching the other
  `PSEUDOLIFE_MCP_*` deployment switches.
- The live-daemon redeploy to adopt any of this — a separate, backup-first ops step.

## Testing

- `memory_search(explain=True)` returns a `trace` dict equal to the old
  `memory_trace`'s; `explain=False` (default) returns no `trace` key (byte-identical
  to today).
- `memory_graph(relation_filter="runs-on")` returns only matching edges; the old
  `get_neighbors` behaviour is reproduced.
- `test_all_tools_registered` (full) lists exactly the 46 expected names (no
  `memory_trace` / `get_neighbors`); the new core-mode test lists exactly the ~15
  core names.
- Full suite green under the HF-offline env.

## Success criteria (verifiable)

1. Default (`full`): `mcp.list_tools()` returns 46 tools (48 − `memory_trace` −
   `get_neighbors`); behaviour unchanged except the two folds.
2. `PSEUDOLIFE_MCP_TOOLSET=core`: only the ~15 core tools register.
3. `memory_search(explain=True)` reproduces the old `memory_trace` trace;
   `memory_graph(relation_filter=…)` reproduces `get_neighbors`.
4. `test_all_tools_registered` is toolset-aware; full suite green.
