# MCP SDK v2 migration + session identity — design

**Date:** 2026-08-25 · **Status:** ratified (user GO, all go-triggers verified)
· **Supersedes** the transport-session portions of the 2026-07-18 identity
notes; builds on the 2026-08-10 principal re-keying and readiness assessment.

## Problem

Two problems, one root:

1. **Session attribution keys on the transport connection.** `session_key`
   derives from the streamable-HTTP `mcp-session-id` header. Claude Code
   holds one long-lived connection shared by successive *and concurrent*
   sessions, so the key identifies the connection, not the session. Observed
   consequences (2026-07-18, live bank): `episode_end` popping a different
   workstream's root, blended auto-titles, mixed outcome-inference contexts.
2. **The MCP 2026-07-28 revision removes `Mcp-Session-Id` entirely**
   (SEP-2567, stateless core). The header our fallback identity rides on
   ceases to exist; SDK v1 is in maintenance mode.

The sanctioned v2 identity axes are per-request authorization (bearer) and
server-minted handles passed as tool arguments — the latter is exactly the
episode-handle pattern the SessionStart briefing already uses. This design
finishes that pattern and ports the SDK in the same change series.

## Current state (verified against master, 2026-08-25)

Already correct / already shipped, no work needed:

- Toolset tiers are principal-keyed; "the session axis no longer
  participates" (`toolset_tiers.py`, spec 2026-08-10).
- `_resolve_episode_handle` reopens a recently-reaped root within
  `PSEUDOLIFE_SESSION_RESUME_SECONDS` (6 h), so a hook-minted handle
  survives the idle reaper (fix for the 2026-08-10 `episode_warning`
  collapse).
- Store/fact/outcome paths accept `episode=` and attribute to the handle's
  episode even when header identity differs (identity and target episode
  are separable, spec 2026-07-18).

Still connection-keyed (the live bug surface):

- `service.episode_start` — nests the new sub-episode under the *caller's
  session root* resolved from `session_key`; no `episode=` anchor.
- `service.episode_end` — ownership guard compares the candidate leaf's
  `session_key` to the caller's; concurrent sessions *share* that key, so
  the guard admits a foreign close.
- `_ensure_session_episode(session_id)` — a store with no `episode=` lazily
  creates/attaches to the connection-keyed root.
- `writer_context` tier 4 — the `mcp-session-id` legacy fallback.

## Design

### Phase 1 — episode handle becomes the primary session identity (SDK-agnostic)

1. **`episode=` on the episode lifecycle tools.** `memory_episode_start`
   and `memory_episode_end` gain an optional `episode` parameter (the
   session handle the SessionStart hook already advertises). `episode_start`
   nests under the handle's root; `episode_end` pops the open leaf *within
   the handle's subtree* and refuses anything outside it. Resolution reuses
   `_resolve_episode_handle` (prefix match, resume window). Without a
   handle, behavior is unchanged (connection key, then global leaf) — the
   degraded path stays for embedded/stdio/tests.
2. **Ownership guard upgraded.** With a handle, ownership is the handle's
   subtree — a strictly narrower check than the shared connection key.
3. **Retire tier 4.** `writer_context` stops reading `mcp-session-id`.
   Escape hatch `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` restores it for one
   release, logged at WARNING on first use. X-PL-Session (shim, tier 1) is
   untouched — it is client-asserted, not transport state, and remains
   valid under v2.
4. **No new session-inference heuristics.** Idle-gap sessionization and
   writer+key compounding (07-18 candidates) are deliberately not built:
   they guess, the handle asserts. Deliberately NOT shipped: any daemon-side
   attempt to distinguish concurrent sessions without a handle — under v2
   there is nothing to distinguish them by.

### Phase 2 — SDK v2 port

- **2a. Pin + surface rename.** `mcp>=2.1,<3`; `FastMCP` → `MCPServer`;
  `request_ctx` reads → `ctx.headers` (documented v2 API) behind
  `writer_context._http_request_headers`, which stays the single seam.
  Host app runs the v2 session-manager lifespan. Lockfile regenerated and
  freeze-diffed (2026-08 lesson: catch unintended downgrades).
- **2b. Tiering transport hook.** `_wire_transport_tiering`'s three private
  hooks (`request_handlers[ListToolsRequest]` swap, `_tool_cache` feed,
  `create_initialization_options` monkey-patch) collapse into one wrap of
  the low-level `Server` `on_list_tools` handler — stable, non-provisional
  v2 API — filtering by `_resolve_principal_tier()` per request. The
  middleware list stays unused for logic (docs: provisional; observe-only).
- **2c. Shim.** `streamable_http_client` (v2 name), headers carried on the
  `httpx2.AsyncClient`, 2-tuple yield; the per-call-reconnect design is
  re-evaluated — under a stateless protocol a persistent client with
  per-request headers is the natural shape. X-PL-Writer / X-PL-Session
  header contract is unchanged.
- **Protocol compat.** v2 SDK serves pre-2026 revisions from the same
  server; Claude Desktop (older client) continues over the legacy path
  while Claude Code negotiates 2026-07-28. No client is dropped.

## Sequencing and gates

Phase 1 lands first and alone (deployable on v1 — it is pure daemon logic).
Phase 2 follows on the same branch as separate commits. Gates: TDD with
watched RED per behavior change; full suite with bench Postgres; the
dream/extraction paths are untouched so the eval ladder is not required;
live verification after deploy exercises `episode_start(episode=...)` and a
tools/list on both protocol revisions.

## Rollback

Phase 1: `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` restores tier 4; the new
`episode=` params are optional so old hooks keep working. Phase 2: rollback
tag per `ops/update.ps1` standard practice; v1 pin restorable from the tag
since no schema change is involved (schema meta stays 32).
