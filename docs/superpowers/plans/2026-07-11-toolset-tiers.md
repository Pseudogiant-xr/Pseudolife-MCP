# Session-Scoped Toolset Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-client default tool tiers (Desktop=minimal, Code=core) with a `memory_toolset` ladder tool that expands a session's visible toolset on demand, plus a docstring trim pinned by per-tier manifest budgets.

**Architecture:** All tools always register with FastMCP; a replacement lowlevel `tools/list` handler filters by the session's resolved tier (session override → writer map → env default). A new native-async `memory_toolset` tool steps the session's tier up/down and emits `tools/list_changed`. Pure tier logic lives in a new `pseudolife_memory/toolset_tiers.py`; wiring stays in `mcp_server.py`.

**Tech Stack:** Python 3.12, mcp SDK 1.27.2 (pinned; behaviors verified 2026-07-11), pytest. Test venv: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest`.

**Spec:** `docs/superpowers/specs/2026-07-11-toolset-tiers-design.md`

## Global Constraints

- Tiers, ordered: `minimal` ⊂ `core` ⊂ `full`. Minimal = `memory_search`, `memory_store`, `memory_fact_get`, `memory_fact_set`, `memory_outcome`, `memory_session_title`, `memory_toolset`.
- Envs: `PSEUDOLIFE_MCP_TOOLSET` = default tier (unset/unknown → `full`, unknown warns); `PSEUDOLIFE_MCP_TIER_MAP` = `writer:tier` CSV (malformed entries logged + skipped).
- Visibility only — calls to hidden tools are NEVER blocked.
- Session tier state: TTL 12h, keyed by `x-pl-session` else `mcp-session-id` else `"__global__"`.
- Manifest budgets (chars, sum of visible descriptions): per-tool ≤ 1600; minimal ≤ 4500; core ≤ 9500; full ≤ 15500.
- Cortex Console (REST) untouched. No service.py changes.
- Every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Tier logic module

**Files:**
- Create: `pseudolife_memory/toolset_tiers.py`
- Test: `tests/test_toolset_tiers.py`

**Interfaces:**
- Produces: `TIERS: tuple[str, ...]`, `rank(tier: str) -> int`, `normalize_tier(value: str | None, *, warn_context: str = "") -> str`, `parse_tier_map(raw: str | None) -> dict[str, str]`, `step(tier: str, delta: int, floor: str = "minimal") -> str`, `SessionTierState` (`.get(key: str | None) -> str | None`, `.set(key: str | None, tier: str) -> None`), `resolve_tier(writer: str | None, session_key: str | None, *, state, tier_map, default_tier) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_toolset_tiers.py
"""Tier logic unit tests — pure module, no MCP/embedder."""
from __future__ import annotations

import pytest

from pseudolife_memory.toolset_tiers import (
    TIERS, SessionTierState, normalize_tier, parse_tier_map, rank,
    resolve_tier, step,
)


def test_tier_order_and_rank():
    assert TIERS == ("minimal", "core", "full")
    assert rank("minimal") < rank("core") < rank("full")


def test_normalize_tier_lenient():
    assert normalize_tier("core") == "core"
    assert normalize_tier(" FULL ") == "full"
    assert normalize_tier(None) == "full"          # unset -> full (today's default)
    assert normalize_tier("") == "full"
    assert normalize_tier("bogus") == "full"       # unknown warns -> full


def test_parse_tier_map_happy_and_malformed():
    m = parse_tier_map("claude-desktop:minimal, Claude-Code:CORE")
    assert m == {"claude-desktop": "minimal", "claude-code": "core"}
    # malformed entries skipped, never fatal
    assert parse_tier_map("nocolon, :core, x:bogus, ok:full") == {"ok": "full"}
    assert parse_tier_map(None) == {}
    assert parse_tier_map("") == {}


def test_step_ladder_and_floor():
    assert step("minimal", +1) == "core"
    assert step("core", +1) == "full"
    assert step("full", +1) == "full"                      # top no-op
    assert step("full", -1, floor="minimal") == "core"
    assert step("core", -1, floor="core") == "core"        # floors at default
    assert step("minimal", -1, floor="minimal") == "minimal"


def test_session_state_ttl_and_none_key():
    s = SessionTierState(ttl_s=0.0)   # everything instantly stale
    s.set("a", "full")
    assert s.get("a") is None
    s2 = SessionTierState()
    s2.set(None, "core")              # None key -> global bucket
    assert s2.get(None) == "core"
    assert s2.get("other") is None


def test_resolve_tier_precedence():
    state = SessionTierState()
    kw = dict(state=state, tier_map={"claude-desktop": "minimal"}, default_tier="core")
    # env default when nothing else matches
    assert resolve_tier(None, "s1", **kw) == "core"
    # writer map beats default (case/space-insensitive writer)
    assert resolve_tier(" Claude-Desktop ", "s1", **kw) == "minimal"
    # session override beats writer map
    state.set("s1", "full")
    assert resolve_tier("claude-desktop", "s1", **kw) == "full"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_toolset_tiers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pseudolife_memory.toolset_tiers'`

- [ ] **Step 3: Write the module**

```python
# pseudolife_memory/toolset_tiers.py
"""Session-scoped toolset tiers (spec: docs/superpowers/specs/2026-07-11).

Visibility model: every tool registers with FastMCP; the transport's
tools/list handler (mcp_server._wire_transport_tiering) filters by the
session's resolved tier. Ordering: minimal ⊂ core ⊂ full. Resolution:
session override (memory_toolset) → writer map (PSEUDOLIFE_MCP_TIER_MAP)
→ env default (PSEUDOLIFE_MCP_TOOLSET). Visibility is a token lever, not
a security boundary — hidden tools stay callable; auth is the bearer token.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("pseudolife-mcp.tiers")

TIERS: tuple[str, ...] = ("minimal", "core", "full")
_RANK = {t: i for i, t in enumerate(TIERS)}

# Sessions are transient (Claude conversations); 12h comfortably outlives
# one and lets abandoned entries lapse without a reaper thread.
SESSION_TTL_S = 12 * 3600.0


def rank(tier: str) -> int:
    return _RANK[tier]


def normalize_tier(value: str | None, *, warn_context: str = "") -> str:
    """Lenient tier parse: unset -> full (the historical default); unknown
    values warn and fall back to full rather than hiding tools by surprise."""
    v = (value or "").strip().lower()
    if v in _RANK:
        return v
    if v:
        ctx = f" ({warn_context})" if warn_context else ""
        logger.warning("unknown toolset tier %r%s — falling back to 'full'", value, ctx)
    return "full"


def parse_tier_map(raw: str | None) -> dict[str, str]:
    """Parse PSEUDOLIFE_MCP_TIER_MAP ("writer:tier,writer:tier"). Malformed
    entries are logged and skipped — a config typo must never take the
    daemon down or hide tools unpredictably."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        writer, sep, tier = part.partition(":")
        writer = writer.strip().lower()
        tier = tier.strip().lower()
        if not sep or not writer or tier not in _RANK:
            logger.warning("tier-map entry %r malformed (want writer:tier) — skipped", part)
            continue
        out[writer] = tier
    return out


def step(tier: str, delta: int, floor: str = "minimal") -> str:
    """One rung up/down the ladder, clamped to [floor, full]."""
    i = max(_RANK[floor], min(len(TIERS) - 1, _RANK[tier] + delta))
    return TIERS[i]


class SessionTierState:
    """TTL'd session-tier overrides. Thread-safe: read on the event loop
    (tools/list) and written from tool handlers. Lazy expiry — no reaper."""

    _GLOBAL = "__global__"

    def __init__(self, ttl_s: float = SESSION_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._m: dict[str, tuple[str, float]] = {}

    def get(self, key: str | None) -> str | None:
        k = key or self._GLOBAL
        now = time.monotonic()
        with self._lock:
            row = self._m.get(k)
            if row is None:
                return None
            tier, ts = row
            if now - ts > self._ttl:
                del self._m[k]
                return None
            return tier

    def set(self, key: str | None, tier: str) -> None:
        k = key or self._GLOBAL
        now = time.monotonic()
        with self._lock:
            self._m[k] = (tier, now)
            if len(self._m) > 256:  # opportunistic sweep keeps the dict bounded
                cut = now - self._ttl
                for stale in [s for s, (_, ts) in self._m.items() if ts < cut]:
                    del self._m[stale]


def resolve_tier(writer: str | None, session_key: str | None, *,
                 state: SessionTierState, tier_map: dict[str, str],
                 default_tier: str) -> str:
    """Session override → writer map → default."""
    override = state.get(session_key)
    if override is not None:
        return override
    if writer:
        mapped = tier_map.get(writer.strip().lower())
        if mapped:
            return mapped
    return default_tier
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_toolset_tiers.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/toolset_tiers.py tests/test_toolset_tiers.py
git commit -m "feat(tiers): tier-logic module — ladder, writer map, TTL session state

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Registration migrates to tiers (visibility model)

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (the `_TOOLSET`/`_TOOL_TIERS`/`_should_register`/`_tool` block at ~lines 91–133, plus every `@_tool(...)` decorator)
- Test: `tests/test_mcp_server.py` (gate tests), `tests/test_release_ux.py:100` (core-loop test — verify only)

**Interfaces:**
- Consumes: `toolset_tiers.normalize_tier`, `parse_tier_map`, `rank`, `SessionTierState` (Task 1).
- Produces: `_TOOL_TIERS: dict[str, str]` (name → tier); `_DEFAULT_TIER: str`; `_TIER_MAP: dict[str, str]`; `_SESSION_TIERS: SessionTierState`; `_tool(*, tier: str = "full")`; `_visible_tool_names(tier: str) -> set[str]`. `_should_register` and the registration gate are REMOVED — all tools always register.

- [ ] **Step 1: Write the failing tests** — replace `test_should_register_gate_logic` and `test_core_tier_membership_is_exactly_the_core_set` in `tests/test_mcp_server.py`, and redefine the expected sets:

```python
# Replace the _EXPECTED_CORE block (test_mcp_server.py ~lines 172-198) with:

_EXPECTED_MINIMAL = sorted([
    # The 7-tool eager surface for minimal-tier clients (Claude Desktop).
    # memory_toolset joins in the gate-tool task; until then 6.
    "memory_store", "memory_search", "memory_fact_get", "memory_fact_set",
    "memory_outcome", "memory_session_title",
])

_EXPECTED_CORE = sorted(_EXPECTED_MINIMAL + [
    "memory_fact_resolve", "memory_graph", "memory_recall",
    "memory_graph_relate", "memory_world_search", "memory_world_set",
    "memory_lesson_search", "document_search", "document_ingest",
    "memory_stats", "memory_get", "memory_episode_start", "memory_episode_end",
])


def test_all_tools_register_regardless_of_toolset_env(tmp_path: Path, monkeypatch) -> None:
    """Visibility model: PSEUDOLIFE_MCP_TOOLSET no longer gates registration."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "core")
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    assert len(asyncio.run(mod.mcp.list_tools())) == len(mod._TOOL_TIERS)
    assert mod._DEFAULT_TIER == "core"


def test_visible_tool_names_per_tier() -> None:
    from pseudolife_memory import mcp_server as mod
    assert sorted(mod._visible_tool_names("minimal")) == _EXPECTED_MINIMAL
    assert sorted(mod._visible_tool_names("core")) == _EXPECTED_CORE
    assert mod._visible_tool_names("full") == set(mod._TOOL_TIERS)


def test_tier_map_env_parsed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TIER_MAP", "claude-desktop:minimal,claude-code:core")
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    assert mod._TIER_MAP == {"claude-desktop": "minimal", "claude-code": "core"}
```

Delete `test_should_register_gate_logic` (the function it tests is going away) and `test_core_tier_membership_is_exactly_the_core_set` (superseded by `test_visible_tool_names_per_tier`).

- [ ] **Step 2: Run to verify failures**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q -k "register or visible or tier_map"`
Expected: FAIL — `AttributeError: ... has no attribute '_visible_tool_names'` (and the core-env registration count mismatch, 19 != 32).

- [ ] **Step 3: Migrate mcp_server.py**

Replace the gate block (currently `_TOOLSET`, `_TOOL_TIERS`, `_should_register`, `_tool`) with:

```python
from pseudolife_memory.toolset_tiers import (
    SessionTierState, normalize_tier, parse_tier_map, rank as _tier_rank,
)

# Session-scoped toolset tiers (spec 2026-07-11). All tools register; the
# transport tools/list handler filters per session. PSEUDOLIFE_MCP_TOOLSET
# is the DEFAULT tier (was: a registration gate); PSEUDOLIFE_MCP_TIER_MAP
# maps writer ids (X-PL-Writer / daemon default) to default tiers. The
# Cortex Console is unaffected (REST calls service.*, not MCP tools).
_DEFAULT_TIER = normalize_tier(os.environ.get("PSEUDOLIFE_MCP_TOOLSET"),
                               warn_context="PSEUDOLIFE_MCP_TOOLSET")
_TIER_MAP = parse_tier_map(os.environ.get("PSEUDOLIFE_MCP_TIER_MAP"))
_SESSION_TIERS = SessionTierState()
_TOOL_TIERS: dict[str, str] = {}


def _visible_tool_names(tier: str) -> set[str]:
    r = _tier_rank(tier)
    return {n for n, t in _TOOL_TIERS.items() if _tier_rank(t) <= r}


def _tool(*, tier: str = "full"):
    """Record the tool's tier and register it (always — tiers gate
    visibility in tools/list, not existence)."""
    def deco(fn):
        _TOOL_TIERS[fn.__name__] = tier
        mcp.tool()(_async_offload(fn))
        return fn  # module attr stays the plain sync fn (tests / Console)
    return deco
```

Then migrate every decorator (mechanical, names exact):

- `@_tool(core=True)` → `@_tool(tier="minimal")` for: `memory_store`, `memory_search`, `memory_fact_get`, `memory_fact_set`, `memory_outcome`, `memory_session_title`.
- `@_tool(core=True)` → `@_tool(tier="core")` for: `memory_stats`, `memory_get`, `memory_fact_resolve`, `memory_world_set`, `memory_world_search`, `memory_lesson_search`, `memory_episode_start`, `memory_episode_end`, `memory_graph_relate`, `memory_graph`, `memory_recall`, `document_ingest`, `document_search`.
- `@_tool()` (no argument) → unchanged (defaults to `tier="full"`): `memory_recent`, `memory_supersede`, `memory_reinforce`, `memory_history`, `memory_forget`, `memory_dream`, `memory_graph_review`, `memory_episode_summary`, `memory_consolidation_candidates`, `memory_consolidate`, `memory_graph_unrelate`, `memory_alias`, `memory_relation_define`.

Also drop the stale `# core memory_fact_get returns source_entries ids —` comment format if it breaks the one-line decorator, keeping the comment above the decorator.

- [ ] **Step 4: Run the file's suite**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py tests/test_release_ux.py tests/test_tool_consolidation.py -q`
Expected: PASS (the release-ux core-loop test passes because core tools still register; consolidation tests pass because full registration is now unconditional).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tiers): registration migrates to tier visibility model — all tools always register

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Transport wiring — filtered tools/list, cache prefill, listChanged capability

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (new `_wire_transport_tiering()` + call at module bottom, near `_flush_on_exit` registration)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_visible_tool_names`, `_SESSION_TIERS`, `_TIER_MAP`, `_DEFAULT_TIER` (Task 2); `writer_context._http_writer_session` (existing); `toolset_tiers.resolve_tier`.
- Produces: `_resolve_session_tier() -> str` (reads request headers; used by Task 4 too); `_wire_transport_tiering() -> None` (idempotent, called at import).

- [ ] **Step 1: Write the failing tests** (append to tests/test_mcp_server.py):

```python
# ---------------------------------------------------------------------------
# Session-scoped tier visibility at the transport (spec 2026-07-11)
# ---------------------------------------------------------------------------

from types import SimpleNamespace


class _FakeReqCtx:
    """Bind fake HTTP headers into the SDK's request_ctx for one test."""
    def __init__(self, headers: dict[str, str]):
        self._headers = headers
        self._token = None
    def __enter__(self):
        from mcp.server.lowlevel.server import request_ctx
        self._token = request_ctx.set(
            SimpleNamespace(request=SimpleNamespace(headers=self._headers)))
        return self
    def __exit__(self, *exc):
        from mcp.server.lowlevel.server import request_ctx
        request_ctx.reset(self._token)


async def _transport_list(mod) -> list:
    import mcp.types as mtypes
    handler = mod.mcp._mcp_server.request_handlers[mtypes.ListToolsRequest]
    result = await handler(None)
    return result.root.tools


def _reload_tiered(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_WRITER_ID", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def test_transport_list_filters_by_writer_map(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="core",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal")
    with _FakeReqCtx({"x-pl-writer": "claude-desktop", "x-pl-session": "d1"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("minimal")
    # No headers (stdio/tests) -> env default tier
    with _FakeReqCtx({}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")


def test_transport_list_env_writer_fallback(tmp_path: Path, monkeypatch) -> None:
    """Direct-HTTP Claude Code sends no X-PL-Writer; the daemon's default
    writer id (PSEUDOLIFE_WRITER_ID) feeds the tier map instead."""
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="full",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-code:core",
                         PSEUDOLIFE_WRITER_ID="claude-code")
    with _FakeReqCtx({"x-pl-session": "c1"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")


def test_hidden_tools_stay_callable(tmp_path: Path, monkeypatch) -> None:
    """Visibility is not a call gate: a full-tier tool dispatches fine in a
    minimal-default deployment."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="minimal")
    _invoke("memory_store", {"text": "hidden-call probe", "source": "t"})
    out = _invoke("memory_recent", {"n": 1})       # memory_recent is full-tier
    assert out["count"] == 1


def test_initialization_advertises_list_changed(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch)
    opts = mod.mcp._mcp_server.create_initialization_options()
    assert opts.capabilities.tools.listChanged is True


def test_tool_cache_prefilled_with_full_set(tmp_path: Path, monkeypatch) -> None:
    """Hidden tools keep call-time input validation: the SDK tool cache is
    fed the FULL registry, not the filtered view."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="minimal")
    with _FakeReqCtx({"x-pl-session": "m1"}):
        asyncio.run(_transport_list(mod))
    assert set(mod.mcp._mcp_server._tool_cache) == set(mod._TOOL_TIERS)
```

- [ ] **Step 2: Run to verify failures**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q -k "transport or hidden or list_changed or cache_prefilled"`
Expected: FAIL — the un-replaced FastMCP handler returns all tools for every case (`test_transport_list_filters_by_writer_map` asserts minimal), and `listChanged is False`.

- [ ] **Step 3: Implement wiring** (mcp_server.py, after `memory_toolset`'s future home — concretely, just above the `# ── Consolidated lifecycle verbs` section or at module end before `_flush_on_exit`):

```python
def _resolve_session_tier() -> str:
    """Tier for the CURRENT request: session override → writer map → env
    default. Writer falls back to the daemon's default id so direct-HTTP
    clients (no X-PL-Writer header) still match the tier map. Safe outside
    a request (returns the env default)."""
    from pseudolife_memory.toolset_tiers import resolve_tier
    from pseudolife_memory.writer_context import _http_writer_session
    writer, session = _http_writer_session()
    return resolve_tier(
        writer or os.environ.get("PSEUDOLIFE_WRITER_ID"), session,
        state=_SESSION_TIERS, tier_map=_TIER_MAP, default_tier=_DEFAULT_TIER,
    )


def _wire_transport_tiering() -> None:
    """Replace the transport tools/list handler with the tier-filtered view
    and advertise tools.listChanged (the SDK default omits it, verified on
    mcp 1.27.2). The raw handler bypasses the SDK's caching wrapper — that
    wrapper CLEARS the tool cache and refills it with whatever the handler
    returns, which would strip hidden tools of call-time validation. We
    feed the cache the full registry instead."""
    import mcp.types as mtypes
    from mcp.server.lowlevel.server import NotificationOptions

    server = mcp._mcp_server

    async def _filtered_list(_req) -> mtypes.ServerResult:
        tools = await FastMCP.list_tools(mcp)   # full registry, mcp.types.Tool
        server._tool_cache.update({t.name: t for t in tools})
        names = _visible_tool_names(_resolve_session_tier())
        return mtypes.ServerResult(mtypes.ListToolsResult(
            tools=[t for t in tools if t.name in names]))

    server.request_handlers[mtypes.ListToolsRequest] = _filtered_list

    _orig = server.create_initialization_options

    def _init_opts(notification_options=None, experimental_capabilities=None):
        return _orig(
            notification_options=notification_options
            or NotificationOptions(tools_changed=True),
            experimental_capabilities=experimental_capabilities,
        )

    server.create_initialization_options = _init_opts


_wire_transport_tiering()
```

Note: `FastMCP.list_tools(mcp)` (unbound call) rather than `mcp.list_tools()` — same thing today, but explicit that we want the *registry* view, not the transport view we are replacing.

- [ ] **Step 4: Run the tests**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q`
Expected: all pass, including the older `test_all_tools_registered` (it calls `mod.mcp.list_tools()` directly — the registry view, unfiltered).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tiers): tier-filtered tools/list at the transport + listChanged capability + full-cache prefill

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The gate tool — `memory_toolset`

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (new tool, placed right after `memory_stats`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_resolve_session_tier` (Task 3), `_SESSION_TIERS`, `_TIER_MAP`, `_DEFAULT_TIER`, `_visible_tool_names` (Task 2); `toolset_tiers.step`; `writer_context._http_writer_session`; `mcp.server.fastmcp.Context`.
- Produces: tool `memory_toolset(action)` returning `{current, default, ladder, adds}` (status) or `{changed, current, previous, visible_tools_added, visible_tools_removed, list_changed_sent}` (expand/collapse).

- [ ] **Step 1: Write the failing tests** (append to tests/test_mcp_server.py):

```python
def test_memory_toolset_ladder_and_status(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="core",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal")
    with _FakeReqCtx({"x-pl-writer": "claude-desktop", "x-pl-session": "lad1"}):
        st = _invoke("memory_toolset", {"action": "status"})
        assert st["current"] == "minimal" and st["default"] == "minimal"
        assert st["ladder"] == ["minimal", "core", "full"]
        assert set(st["adds"]) == {"core", "full"}

        up = _invoke("memory_toolset", {"action": "expand"})
        assert up["changed"] is True and up["current"] == "core"
        assert "memory_recall" in up["visible_tools_added"]
        assert up["list_changed_sent"] is False   # no live transport session here

        up2 = _invoke("memory_toolset", {"action": "expand"})
        assert up2["current"] == "full"
        top = _invoke("memory_toolset", {"action": "expand"})
        assert top["changed"] is False            # already at the top

        down = _invoke("memory_toolset", {"action": "collapse"})
        assert down["current"] == "core"
        down2 = _invoke("memory_toolset", {"action": "collapse"})
        assert down2["current"] == "minimal"
        floor = _invoke("memory_toolset", {"action": "collapse"})
        assert floor["changed"] is False          # floored at the session default

        # And the transport list follows the override
        names = {t.name for t in asyncio.run(_transport_list(mod))}
        assert names == mod._visible_tool_names("minimal")


def test_memory_toolset_expansion_is_session_scoped(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="minimal")
    with _FakeReqCtx({"x-pl-session": "sA"}):
        _invoke("memory_toolset", {"action": "expand"})
    with _FakeReqCtx({"x-pl-session": "sA"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")
    with _FakeReqCtx({"x-pl-session": "sB"}):   # untouched session stays minimal
        names_b = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names_b == mod._visible_tool_names("minimal")


def test_memory_toolset_is_minimal_tier_and_registered() -> None:
    from pseudolife_memory import mcp_server as mod
    assert mod._TOOL_TIERS["memory_toolset"] == "minimal"
    assert "memory_toolset" in {t.name for t in asyncio.run(mod.mcp.list_tools())}


def test_list_changed_attempted_on_change_not_on_noop(tmp_path: Path, monkeypatch) -> None:
    """Spec test item 4: the notification fires on a tier change and is NOT
    attempted on a no-op (expand at full / collapse at floor)."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="core")
    calls = []

    async def _spy(ctx):
        calls.append(True)
        return True

    monkeypatch.setattr(mod, "_notify_list_changed", _spy)
    with _FakeReqCtx({"x-pl-session": "n1"}):
        out = _invoke("memory_toolset", {"action": "expand"})   # core -> full
        assert out["changed"] is True and calls == [True]
        noop = _invoke("memory_toolset", {"action": "expand"})  # already full
        assert noop["changed"] is False and calls == [True]     # no second send
```

Also update `test_all_tools_registered` (add `"memory_toolset"` to the expected list) and `_EXPECTED_MINIMAL` (add `"memory_toolset"`; `_EXPECTED_CORE` inherits it).

- [ ] **Step 2: Run to verify failures**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q -k toolset`
Expected: FAIL — `Unknown tool: memory_toolset`.

- [ ] **Step 3: Implement** (after `memory_stats` in mcp_server.py):

```python
_TIER_ADDS = {
    "core": "graph + recall, world facts, lessons, documents, stats, "
            "episodes, memory_get/fact_resolve",
    "full": "supersede/forget/history/reinforce, recent, dream + "
            "graph-review, aliases, consolidation, relation-define",
}


async def _notify_list_changed(ctx: Context) -> bool:
    """Best-effort tools/list_changed. False when there is no live
    transport session (tests, embedded stdio) — the memory_toolset result
    names the newly visible tools, and calls are ungated regardless."""
    try:
        await ctx.session.send_tool_list_changed()
        return True
    except Exception:  # noqa: BLE001
        return False


async def memory_toolset(
    action: Literal["expand", "collapse", "status"],
    ctx: Context,
) -> dict[str, Any]:
    """Adjust THIS session's visible toolset, one tier at a time
    (minimal → core → full; session-scoped, free, instant). Core adds
    graph/recall, world facts, lessons, documents; full adds
    supersede/forget/history, dream and graph-review admin. ``status``
    reports the ladder. Hidden tools remain callable by exact name.
    """
    from pseudolife_memory.toolset_tiers import TIERS, step
    from pseudolife_memory.writer_context import _http_writer_session

    writer, session = _http_writer_session()
    default_tier = (_TIER_MAP.get((writer or os.environ.get(
        "PSEUDOLIFE_WRITER_ID") or "").strip().lower()) or _DEFAULT_TIER)
    current = _resolve_session_tier()

    if action == "status":
        return {"current": current, "default": default_tier,
                "ladder": list(TIERS), "adds": _TIER_ADDS}

    new = step(current, +1 if action == "expand" else -1,
               floor="minimal" if action == "expand" else default_tier)
    if new == current:
        return {"changed": False, "current": current,
                "reason": ("already at full" if action == "expand"
                           else f"already at this session's floor ({default_tier})")}

    _SESSION_TIERS.set(session, new)
    before, after = _visible_tool_names(current), _visible_tool_names(new)
    out: dict[str, Any] = {
        "changed": True, "current": new, "previous": current,
        "visible_tools_added": sorted(after - before),
        "visible_tools_removed": sorted(before - after),
    }
    out["list_changed_sent"] = await _notify_list_changed(ctx)
    return out


# Native async registration: the handler must touch the transport session
# (send_tool_list_changed), so it skips the _async_offload thread hop — its
# body is dict ops only and cannot block the event loop.
_TOOL_TIERS["memory_toolset"] = "minimal"
mcp.tool()(memory_toolset)
```

Add `from mcp.server.fastmcp import Context` next to the existing `FastMCP` import at the top of the file.

- [ ] **Step 4: Run the tests**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q`
Expected: all pass (including the async-offload test: native async tools count as async).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(tiers): memory_toolset gate — session-scoped tier ladder with list_changed

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Docstring trim + per-tier manifest budgets

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (docstrings only: `memory_search`, `memory_store`, `memory_outcome`, `memory_fact_get`, `memory_fact_set`, `memory_forget`, `memory_dream`, `memory_graph_review`)
- Test: `tests/test_tool_consolidation.py` (`test_descriptions_are_terse` → per-tier budgets)

**Interfaces:**
- Consumes: `_visible_tool_names` (Task 2).
- Produces: nothing new — budget regression tests.

- [ ] **Step 1: Replace `test_descriptions_are_terse`** in tests/test_tool_consolidation.py:

```python
def test_descriptions_fit_tier_budgets(tmp_path: Path, monkeypatch) -> None:
    """The manifest is eager agent context for non-deferring clients; each
    tier's visible descriptions must fit its budget (spec 2026-07-11)."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)

    tools = asyncio.run(mod.mcp.list_tools())
    sizes = {t.name: len(t.description or "") for t in tools}
    fat = [(n, s) for n, s in sizes.items() if s > 1600]
    assert fat == [], f"over-long tool descriptions: {fat}"
    budgets = {"minimal": 4500, "core": 9500, "full": 15500}
    for tier, cap in budgets.items():
        total = sum(sizes[n] for n in mod._visible_tool_names(tier))
        assert total <= cap, f"{tier} manifest {total} chars exceeds {cap}"
```

- [ ] **Step 2: Run to verify failure**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_tool_consolidation.py::test_descriptions_fit_tier_budgets -q`
Expected: FAIL — minimal ≈ 5.4k > 4500 (and full over 15500).

- [ ] **Step 3: Replace the eight docstrings** with these (verbatim; prose-only change, no signature or Args semantics touched):

`memory_search` (~1,120 chars):

```python
    """Retrieve memories relevant to a query — associative recall plus
    canonical facts.

    Call at the start of a task and whenever prior context may apply.
    Canonical cortex facts arrive under ``cortex`` AHEAD of the associative
    ``entries`` — they are the current, deduped answer (``contested: true``
    means a conflict awaits ``memory_fact_resolve``). ``low_confidence=True``
    means no confident match: prefer to abstain. On a superseded entry,
    prefer its ``superseded_by_text``.

    Args:
        query: Natural-language description; specific beats vague.
        top_k: Max results (default 8).
        sources / bands / episodes / tags: Optional filters (AND across
            kinds, OR within a list).
        min_score: Override the 0.25 relevance floor.
        disable_recency_boost: True for state probes where recency bias
            is unwelcome.
        rerank / bm25: Tri-state config overrides; ``bm25=True`` helps
            exact-keyword queries, ``rerank=True`` cross-encodes (~200ms).
        explain: Attach a ranking ``trace`` (implies verbose).
        verbose: Full per-entry metadata; default entries are compact
            ``{id, text, source, tags, score}`` + supersession when set.

    Returns: ``{query, count, entries, cortex, low_confidence}``.
    """
```

`memory_store` (~640 chars):

```python
    """Store one durable fact, decision, or observation in associative
    memory. Use proactively when you learn something worth keeping — one
    claim per call. Near-duplicates are dropped by the surprise gate
    (``stored=False``, ``reason="below_surprise_threshold"`` — a feature,
    not an error). The background dream pass distils canonical facts
    later; for a fact you want canonical NOW use ``memory_fact_set``.

    Args:
        text: The claim to remember.
        source: Stable per-project/topic tag for later filtering.
        tags: Optional labels, e.g. ``["decision", "blocker"]``.
        origin: Who asserted it — ``"user"`` / ``"action"`` (a tool
            confirmed it) / ``"agent"`` (you concluded it).

    Returns: ``{stored, surprise, reason, cortex_promoted}``.
    """
```

`memory_outcome` (~590 chars):

```python
    """Record a procedural outcome signal — what worked, what failed, or
    what the user corrected — the moment it lands. The dream synthesises
    signals into durable lessons surfaced at future session starts;
    logging outcomes is how you stop repeating mistakes.

    Args:
        task: Kind of task, in stable wording ("deploy engine to host").
        outcome: ``success`` | ``failure`` | ``correction``.
        about: The tool/approach concerned (makes lessons traversable).
        detail: What worked / what the dead-end was.
        polarity: ``+`` do-this | ``-`` avoid; usually omit (inferred).

    Returns: ``{recorded, signal_id, task, outcome}``; needs Postgres.
    """
```

`memory_fact_get` (~540 chars):

```python
    """Look up the one CURRENT canonical value at an ``(entity, attribute)``
    slot. One value per slot, no stale duplicates; matching is case- and
    separator-insensitive. A null record means the slot is EMPTY, not that
    the topic is unknown — ``memory_search`` still finds associative
    context.

    Returns: ``{record | null, contenders}`` (+ ``entity_ref`` when the
    entity has a graph node). Non-empty ``contenders`` = unsettled
    conflict (see ``memory_fact_resolve``); on an empty slot,
    ``candidates`` lists nearby current slots — ranked leads, not answers.
    """
```

`memory_fact_set` (~560 chars):

```python
    """Assert a canonical fact NOW — insert, confirm, or correct a slot.

    A new value at an existing slot supersedes the old one (kept as
    history). A write conflicting with a higher-tier fact (e.g.
    user-stated) is parked as a contender (``action="contested"``, the
    winner under ``current``) — check with the human, then settle via
    ``memory_fact_resolve``.

    Args:
        origin: ``"user"`` / ``"action"`` / ``"agent"`` (default) — who
            asserts it. Use ``"user"`` for things the human told you.
        confidence: 0..1, default 0.8.

    Returns: ``{action: inserted|confirmed|superseded|contested, ...record}``.
    """
```

`memory_forget` (~660 chars):

```python
    """Hard-delete from one memory store. Cleanup for junk/test data — no
    audit trail. For "now wrong, keep history" use ``memory_fact_set``
    (facts) or ``memory_supersede`` (memories) instead.

    Scopes:
        ``memory``: entries matching ``text`` / ``substring`` / ``source``
            / ``episode`` / ``tag`` (at least one; filters OR-combine —
            ANY match deletes, unlike memory_search's AND).
        ``fact``: canonical slots — ``entity`` required; omit
            ``attribute`` to purge the whole entity.
        ``world``: world facts — ``entity`` (+ optional ``attribute``).
        ``lesson``: pass the task as ``entity``, the aspect as
            ``attribute``.

    Returns: ``{deleted_count | removed, ...}``; ``{error}`` on bad input.
    """
```

`memory_dream` (~940 chars):

```python
    """Drive the dream — consolidation of recent memories into canonical
    facts and graph structure.

    Actions:
        ``status``: backlog + whether a sweep would fire. Read-only.
        ``pull``: unconsolidated memories (oldest-first, up to ``limit``)
            — read them, write slot-shaped facts via ``memory_fact_set``,
            then commit.
        ``commit``: advance the dream cursor to ``cursor`` (newest
            timestamp from the pull).
        ``run``: one server-side dream with the configured extractor
            (loop until ``pulled=0`` to drain).
        ``deep``: full-corpus graph consolidation. Dry-run by default;
            ``apply=true`` snapshots the graph tables first (refuses if it
            can't). Settle returned candidates via ``memory_graph_review``;
            ``snippets=false`` omits evidence.

    Returns: per-action dict; ``{error}`` on a bad action or missing cursor.
    """
```

`memory_graph_review` (~830 chars):

```python
    """Work the graph review queue — deep-dream proposals that need a
    verdict before they touch the real graph.

    Actions:
        ``list``: pending findings/proposals (optional ``scope`` filter).
        ``propose``: submit link proposals ``[{src, relation, dst,
            similarity?, rationale?}]`` — stored for review, never written
            directly.
        ``dismiss_pair``: mark ``src``/``dst`` as genuinely distinct — the
            pair stops resurfacing as a duplicate candidate.
        ``accept_link`` / ``reject_link``: settle an edge proposal by
            ``proposal_id``.
        ``accept_merge``: fold a near-duplicate entity into its twin.
        ``accept_junk``: delete an over-extraction artifact entity.
        ``reject_entity``: keep the entity; dismiss its proposal.

    Returns: per-action dict; ``{error}`` on a bad action or missing input.
    """
```

- [ ] **Step 4: Run budgets + full MCP tests; iterate wording ONLY if a budget still fails** (tighten the same eight docstrings further — do not touch other tools without noting it in the commit message):

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/test_tool_consolidation.py tests/test_mcp_server.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_tool_consolidation.py
git commit -m "feat(tiers): docstring trim + per-tier manifest budgets (minimal 4.5k / core 9.5k / full 15.5k chars)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Ops + docs + full suite

**Files:**
- Modify: `ops/docker-compose.yml` (env block, next to `PSEUDOLIFE_MCP_TOOLSET`), `README.md` (Toolset tiers section), `CHANGELOG.md` ([Unreleased])

**Interfaces:** none (config + copy).

- [ ] **Step 1: compose** — under the existing `PSEUDOLIFE_MCP_TOOLSET: ${PSEUDOLIFE_MCP_TOOLSET:-core}` line add:

```yaml
      # Per-writer default tiers, "writer:tier" CSV (e.g.
      # "claude-desktop:minimal,claude-code:core"). Empty = everyone gets
      # the PSEUDOLIFE_MCP_TOOLSET default. Any session can step its own
      # tier up/down at runtime via the memory_toolset tool.
      PSEUDOLIFE_MCP_TIER_MAP: ${PSEUDOLIFE_MCP_TIER_MAP:-}
```

- [ ] **Step 2: README** — replace the "**Toolset tiers.**" paragraph with:

```markdown
**Toolset tiers.** Three visibility tiers — `minimal` (7 tools: the
recall/capture loop + the gate), `core` (20: + graph/recall, world facts,
lessons, documents, episodes), `full` (33) — filtered per session at
`tools/list`; hidden tools stay callable by name. Defaults:
`PSEUDOLIFE_MCP_TOOLSET` (shipped: `core`) sets the baseline;
`PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal,claude-code:core"` sets
per-client defaults by writer id. Any session can step its own tier up or
down at runtime with `memory_toolset(action="expand"|"collapse"|"status")`
— the daemon emits `tools/list_changed`, and clients that ignore it can
still call the tools named in the result. Eager-loading clients (Claude
Desktop) start at ~1.5k tokens of manifest on `minimal`; clients that
defer schemas client-side (Claude Code) barely notice tiers at all.
```

- [ ] **Step 3: CHANGELOG** — new [Unreleased] section on top:

```markdown
### Added (2026-07-11 — session-scoped toolset tiers)
- **Three visibility tiers** (`minimal` ⊂ `core` ⊂ `full`) filtered per
  session at `tools/list`; all tools always register and hidden tools stay
  callable (core mode previously *unregistered* them — calls now succeed).
- **`memory_toolset(action)`** — expand/collapse THIS session's tier one
  rung at a time (floor = the session's default), `status` for the ladder;
  emits `tools/list_changed` (capability now advertised).
- **`PSEUDOLIFE_MCP_TIER_MAP`** — per-writer default tiers
  (`claude-desktop:minimal,claude-code:core`); `PSEUDOLIFE_MCP_TOOLSET`
  becomes the default tier rather than a registration gate.
- **Docstring trim + manifest budgets** — per-tier char caps pinned by
  tests (minimal ≤4.5k, core ≤9.5k, full ≤15.5k; per-tool 1.6k).
```

- [ ] **Step 4: Full suite**

Run: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\.venv\Scripts\python.exe -m pytest tests/ -q`
Expected: all pass (~915+).

- [ ] **Step 5: Commit**

```bash
git add ops/docker-compose.yml README.md CHANGELOG.md
git commit -m "docs+ops(tiers): TIER_MAP env, README tiers section, CHANGELOG

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Deploy + live verification (main session, not a subagent)

**Files:**
- Modify: `C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\ops\.env` (machine-local, gitignored)

- [ ] **Step 1:** Merge the worktree branch to master (`git push . <branch>:master` from the worktree if the main checkout is busy on another branch; plain ff-merge otherwise).
- [ ] **Step 2:** Edit `ops/.env`: replace the `PSEUDOLIFE_MCP_TOOLSET=full` line (added 2026-07-11) with `PSEUDOLIFE_MCP_TIER_MAP=claude-desktop:minimal,claude-code:core` (baseline stays the compose default `core`).
- [ ] **Step 3:** Deploy: `& C:\Users\HAMO9\ClaudeCode\PseudoLife-MCP\ops\update.ps1 -Tag pre-toolset-tiers` — expect backup, rollback tag, daemon-only rebuild, `Healthy. schema=21`.
- [ ] **Step 4:** Verify from this session (a direct-HTTP `claude-code` client): the deferred tool list should show the core set; call `memory_toolset(status)` → `{current: "core"}`; `expand` → full-tier names in `visible_tools_added`.
- [ ] **Step 5:** Ask the user to open Claude Desktop and confirm: (a) the pseudolife server lists 7 tools; (b) asking Desktop's Claude to run `memory_toolset(expand)` grows the visible list (tests `list_changed` honoring). Record the observed behavior in README if Desktop ignores the notification.
- [ ] **Step 6:** `memory_store` the deploy record + `memory_outcome` success/failure honestly.
