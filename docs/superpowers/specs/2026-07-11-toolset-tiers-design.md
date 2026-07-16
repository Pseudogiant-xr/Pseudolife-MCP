# Session-scoped toolset tiers — design

**Date:** 2026-07-11
**Status:** approved (user), pending implementation plan
**Prereqs:** compact-by-default payloads (5b5e2eb), env-overridable toolset (28b1002)

## Problem

Eager-loading MCP clients (Claude Desktop via the shim, other clients, public
users) pull the entire tool manifest into context at session start: ~16.7k
chars (~5–6k tokens) on `full`, ~3–3.5k tokens on `core`. Claude Code defers
schemas client-side, so it is already cheap — but the daemon serves both kinds
of client from one process, and today the tier is a process-global,
registration-time switch (`PSEUDOLIFE_MCP_TOOLSET`). There is no way for
Desktop to start lean and request more tools mid-session, and no way to give
different clients different defaults.

## Goals

1. Per-client default tiers: Claude Desktop starts `minimal`, Claude Code
   starts `core`, configurable per deployment.
2. Runtime expansion: an agent can request the next tier up mid-session via a
   tool call (`memory_toolset`), scoped to its own session only.
3. Shrink the eager cost of every tier (docstring/schema trim on the worst
   offenders), with regression tests pinning the budgets.
4. Shipped defaults preserve today's behavior for anyone not opting in.

## Non-goals

- Blocking calls to tools outside the visible tier. Visibility is a token
  lever; the bearer token remains the security boundary.
- Persisting a session's expanded tier across daemon restarts.
- Per-tool (rather than per-tier) visibility control.
- Cortex Console changes (REST is unaffected throughout).

## Design

### Tier model

Three ordered tiers: `minimal` ⊂ `core` ⊂ `full`. Registration-time
declaration moves from `_tool(core: bool)` to `_tool(tier: str = "full")`;
existing `core=True` tools map to `tier="core"` except the seven promoted to
`minimal`:

- `memory_search`, `memory_store`, `memory_fact_get`, `memory_fact_set`,
  `memory_outcome`, `memory_session_title`, and the new `memory_toolset`.

**All tools always register** with FastMCP regardless of tier. The tier gates
*visibility* in `tools/list`, not existence. `_TOOL_TIERS` becomes
`dict[str, str]` (name → tier).

### Per-session tier resolution

A session's visible tier resolves in order:

1. **Session override** — set by `memory_toolset(expand|collapse)`, stored in
   a TTL dict keyed by the same session id the episode system uses
   (`x-pl-session` header from the shim, else `mcp-session-id`). Entries
   expire on the existing idle-reaper cadence (piggyback its sweep or a
   lazy-expiry check at read; decided in the plan).
2. **Writer map** — new env `PSEUDOLIFE_MCP_TIER_MAP`, a comma-separated
   `writer:tier` list (e.g. `claude-desktop:minimal,claude-code:core`).
   Writer id comes from the `X-PL-Writer` header (the shim sends it; see
   `writer_context.py`), falling back to the daemon's default writer.
3. **Env default** — `PSEUDOLIFE_MCP_TOOLSET` (unchanged name), now meaning
   "default tier" rather than "registration gate". Shipped default: `core`
   (as deployed since b387db8). Unknown values → `full` with a warning
   (today's lenient behavior).

### The gate tool: `memory_toolset`

`memory_toolset(action: Literal["expand", "collapse", "status"])`,
tier=`minimal` (always visible).

- `expand`: step up one rung (minimal→core→full). At `full`: no-op with a
  message.
- `collapse`: step down one rung; floor is the session's *default* tier
  (resolution steps 2–3), never below it.
- `status`: `{current, default, ladder, adds}` where `adds` names what each
  higher rung would add (grouped, not 25 raw names).

Return shape always includes `visible_tools_added` / `removed` (names) so an
agent whose client ignores `tools/list_changed` can still call the new tools
immediately (calls are not gated).

The docstring is the discoverability surface for weak models. It must say, in
one or two lines: "core adds graph/recall/world/lessons/documents; full adds
supersede/forget/history/dream/graph-review/admin" — and that expansion is
session-scoped and free.

After a successful expand/collapse the handler emits
`tools/list_changed` on the session (`ServerSession.send_tool_list_changed`,
verified present in mcp 1.27.2). `memory_toolset` is registered as a native
async tool (not through `_async_offload`) because it must touch the session
from the handler task; its body is trivial (dict ops) so it cannot block the
loop.

### tools/list override

Re-register the lowlevel handler after FastMCP construction:
`mcp._mcp_server.list_tools()(filtered_handler)` — verified on mcp 1.27.2
that this *replaces* `request_handlers[ListToolsRequest]`.

The handler:
1. Reads the session's writer + session id from `request_ctx` (verified bound
   for all requests, including `tools/list`).
2. Resolves the tier (above) and returns only tools whose tier ≤ resolved.
3. **Keeps the SDK's `_tool_cache` fed with the full tool set**, not the
   filtered one — the lowlevel call path consults that cache for call-time
   validation, and hidden tools must remain callable. (The SDK's generated
   wrapper caches whatever the handler returns, so the handler updates the
   cache itself before returning the filtered list.)

Outside a request context (embedded stdio mode, direct `mcp.list_tools()` in
tests), resolution falls through to the env default — stdio single-user mode
keeps today's behavior.

### Capability advertisement

`NotificationOptions().tools_changed` defaults to `False` in the SDK and the
streamable-HTTP session manager calls `create_initialization_options()` with
no arguments (both verified). Wrap `mcp._mcp_server.create_initialization_options`
so it always passes `NotificationOptions(tools_changed=True)` — otherwise
clients are told the tool list never changes.

### Docstring/schema trim (bounded)

Measured description lengths (chars, 2026-07-11, full manifest total 16,621):
the six existing minimal picks sum to 4,983 (`memory_search` 1,589,
`memory_store` 940, `memory_outcome` 805, `memory_fact_set` 710,
`memory_fact_get` 698, `memory_session_title` 241); the core set sums to
10,409. Other offenders: `memory_dream` 1,117, `memory_graph_review` 1,007,
`memory_forget` 916. Trim prose only — no parameter changes on hot-path
tools. Regression caps (chars, tool descriptions summed per visible tier,
enforced after the trim pass):

- per-tool: 1,600 (existing test, unchanged)
- `minimal` manifest (7 tools incl. the gate) ≤ 4,500
- `core` manifest ≤ 9,500
- `full` manifest ≤ 15,500

(The existing 18,000 total cap is superseded by the `full` cap. Indicative
per-tool targets to hit minimal's cap: search ≈1,200, store ≈700,
outcome ≈600, fact_get/fact_set ≈550 each, gate ≈450.)

### Ops / config

- `ops/docker-compose.yml`: add
  `PSEUDOLIFE_MCP_TIER_MAP: ${PSEUDOLIFE_MCP_TIER_MAP:-}` (empty = feature
  dormant; everyone gets the `PSEUDOLIFE_MCP_TOOLSET` default). Toolset line
  stays `${PSEUDOLIFE_MCP_TOOLSET:-core}`.
- This machine's `ops/.env`: set
  `PSEUDOLIFE_MCP_TIER_MAP=claude-desktop:minimal,claude-code:core` and
  remove the `PSEUDOLIFE_MCP_TOOLSET=full` override added 2026-07-11 (the
  ladder replaces it).
- Deploy: backup-first daemon rebuild via `ops/update.ps1`.

### Failure modes

- **Client ignores `list_changed`** (or capability handshake fails): the
  `memory_toolset` result lists the newly visible tools; calls are ungated,
  so the agent proceeds. Live-verify with the user's Claude Desktop; document
  the observed behavior in the README.
  - **CONFIRMED FAILED 2026-07-16** (morning-brief scheduled task, runs via
    the desktop shim as writer `claude-desktop` → minimal): "calls are
    ungated" holds only at the wire. Claude harnesses gate tool calls
    *client-side* against their own list ("No such tool available" — the
    call never reaches the daemon), so a hidden tool is effectively
    uncallable. Worse, the shim's per-call upstream connections meant the
    daemon's `list_changed` died on an ephemeral session and the client
    never re-listed. Fixes shipped same day: the shim now advertises
    `tools.listChanged` downstream and re-emits `list_changed` after a
    `memory_toolset` call with `changed: true`
    (tests/test_shim.py::test_shim_forwards_list_changed_on_toolset_expand),
    and this machine's tier map moved `claude-desktop` to `core` so the
    scheduled task's world tools are visible at init.
- **Unknown writer / no headers**: env default tier. Malformed
  `PSEUDOLIFE_MCP_TIER_MAP` entries are logged and skipped, never fatal.
- **Shim reconnects mid-session**: `x-pl-session` is stable per shim session,
  so the override survives; `mcp-session-id` alone (direct HTTP) is stable
  per connection.
- **Daemon restart**: session overrides vanish; sessions restart at their
  default tier. Intentional.
- **Two sessions, same writer**: independent overrides (keyed by session id,
  not writer).

### Back-compat matrix

| Deployment | Before | After |
|---|---|---|
| Shipped compose (no .env) | core, registration-gated | core default tier, expandable via gate |
| `PSEUDOLIFE_MCP_TOOLSET=full` | all 32 registered | full default tier (identical surface) |
| Embedded stdio / file mode | env-gated | env default tier, no session state |
| Cortex Console REST | unaffected | unaffected |

Note one deliberate change: in core mode the trimmed tools were previously
*unregistered* (calls failed); now they are hidden but callable. This is
strictly more permissive for the token holder and removes a failure mode.

## Testing

TDD at the MCP layer (pattern: tests/test_mcp_server.py):

1. Tier-resolution unit tests: session override > writer map > env default;
   malformed map entries skipped; unknown tier warns → full.
2. `tools/list` filtering: fake `request_ctx` headers per case (desktop →
   7 tools incl. gate; code → core set; no headers → env default).
3. Ladder: expand steps minimal→core→full and stops; collapse floors at the
   session default; status reports correctly.
4. `list_changed` emitted on change, not on no-op expand at full.
5. Capability: initialization options advertise `tools.listChanged=true`.
6. Hidden-tool calls still dispatch (call a full-tier tool from a minimal
   session).
7. Manifest budget regressions per tier (char caps above).
8. Existing suites stay green (`_should_register` tests adapt to the
   visibility model; registration-count tests become visibility-count tests).

Live verification (with user): Desktop session starts with 7 tools; asking
Claude in Desktop to expand produces the core set in its tool list (tests
whether Desktop honors `list_changed`); result-fallback works regardless.

## Open items for the plan

- Exact reaper piggyback vs lazy TTL for the session-tier dict.
- Whether `memory_toolset` needs rate/no-op guards (probably not; idempotent).
- README + CHANGELOG copy.
