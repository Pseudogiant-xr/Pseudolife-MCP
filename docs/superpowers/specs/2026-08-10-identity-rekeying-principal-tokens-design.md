# Identity re-keying: per-principal tokens — design

**Status**: approved (interactive brainstorm) · **Date**: 2026-08-10
**Predecessor**: the 2026-07-18 session identity contract
(`2026-07-18-session-identity-contract-design.md`), the 2026-07-11 toolset
tiers design (`2026-07-11-toolset-tiers-design.md`), and the 2026-08-10 MCP
SDK v2 migration-readiness assessment (memory bank; verdict WAIT with this
redesign approved to start on v1).
**Scope**: caller-identity and tier-keying rework, implementable entirely on
the v1 SDK pin (`mcp>=1.0.0,<2`) and forward-compatible with the MCP
2026-07-28 stateless core. The SDK v2 port itself is out of scope.

## Problem

Three related defects, one root cause — state keyed on connection identity:

1. **Tier state violates the incoming protocol.** `SessionTierState`
   (`toolset_tiers.py`) keys tier overrides on the resolved session id, and
   its fallback comment assumes "HTTP transports always carry a session id."
   The MCP 2026-07-28 tools spec states the advertised tool set "MUST NOT
   vary per-connection or as a side effect of other requests on the
   connection," and the stateless core removes `Mcp-Session-Id` entirely.
   The permitted axis is explicit: the set "MAY vary by the authorization
   presented on the request."
2. **The identity signal degenerates under stateless.** With one shared
   bearer token (`PSEUDOLIFE_MCP_TOKEN`), the only header a 2026-era direct
   client is guaranteed to send — `Authorization` — carries no information:
   every client resolves to the same identity, so per-client tier defaults
   and writer attribution have nothing protocol-durable to key on.
3. **Writer identity is client-asserted.** `X-PL-Writer` is set by whoever
   connects; nothing binds it to the credential presented.

## Decisions from brainstorm (user-ratified 2026-08-10)

1. **Multi-token**: per-principal bearer tokens; the token maps to a
   principal that carries writer identity and default tier.
2. **Principal-only tier keying**: `SessionTierState` re-keys from session
   id to principal. Accepted consequence: a `memory_toolset` override
   applies to every concurrent session presenting the same token. Tiers are
   a context-budget lever, not a security boundary (unchanged from the
   2026-07-11 design), and expansion is monotone-up and TTL'd.
3. **Demote-but-keep tier 4**: the `mcp-session-id` fallback in the session
   identity contract stays in place for v1 clients (hookless direct-HTTP
   installs still benefit); it is annotated as legacy and dies naturally at
   the SDK v2 port. No code removal in this design.

## Design

### Token → principal map

- New env `PSEUDOLIFE_MCP_TOKENS`: comma-separated `token:principal`
  entries (`"a1b2…:claude-desktop,c3d4…:hermes"`). Parsing follows the
  `parse_tier_map` convention — malformed entries log and skip, never take
  the daemon down.
- `PSEUDOLIFE_MCP_TOKEN` (singular) remains fully supported and maps to the
  reserved principal `default`. Existing installs change nothing. Both may
  be set; the plural map is consulted first.
- Principal names share the writer-id namespace (lowercased). This makes
  `PSEUDOLIFE_MCP_TIER_MAP` (`writer:tier`) automatically become the
  principal→tier map with no config migration.
- Token comparison moves to `hmac.compare_digest` while we are in the gate
  code (the current `==` is not constant-time; incidental hardening, not a
  driver).
- The bind-safety rule in `daemon.py` ("non-loopback bind requires a
  token") treats *any* configured token — singular or map — as satisfying
  the requirement.

### Resolution seam

Principal resolution lives in `writer_context.py`, the existing chokepoint,
not in the ASGI gate: handlers read the request's `Authorization` header via
the same best-effort request-context path used for `X-PL-*` today, and map
it through the token table. Rationale: the ASGI gate runs outside the SDK's
handler task and cannot portably hand values across that boundary on v1; the
header read is already proven there, and on v2 the identical logic reads
`ctx.headers` — the seam survives the port unchanged.

Precedence for **writer identity**:

1. Explicit override (`set_writer_context`) — unchanged, strongest.
2. Named principal from the token map — new. When the presented token
   resolves to a named principal, that principal IS the writer id;
   `X-PL-Writer` is ignored (it becomes a shim-internal detail).
3. Single-token / no-token installs (`default` principal): `X-PL-Writer`,
   then `PSEUDOLIFE_WRITER_ID`, exactly as today — bit-for-bit
   backward-compatible.

**Session identity is untouched.** The five-tier contract from 2026-07-18
continues to rank `session_key` exactly as specified; this design changes
who the *writer* is and what the *tier state* keys on, not how episodes
resolve. The two axes stay separable, as the parent spec requires.

### Tier keying

- `SessionTierState` → `PrincipalTierState`: same TTL'd map, same lazy
  sweep, keyed by principal name. The `__global__` bucket remains as the
  floor for the `default` principal and key-less callers (embedded stdio,
  tests), so the degenerate single-token case behaves as one shared tier —
  which it already effectively was for direct-HTTP clients.
- `resolve_tier` becomes: principal override (`memory_toolset`) → tier map
  (`PSEUDOLIFE_MCP_TIER_MAP`, keys now principals) → env default
  (`PSEUDOLIFE_MCP_TOOLSET`). The session-key parameter is deleted.
- `_wire_transport_tiering`'s `_filtered_list` resolves the principal from
  the request context (same seam as above) instead of the session tier.
  The three-point FastMCP internals hook is NOT reworked here — that is
  v2-port work; only the key it filters by changes.
- `memory_toolset` mutations scope to the caller's principal.

### Episode-handle promotion (tier 2)

The SessionStart briefing line changes from advertising the episode handle
"when running concurrent sessions" to instructing that writes SHOULD always
pass `episode=<id>`. Tier 3 (active-session pointer) remains the forgiving
fallback; tier 5 (idle-gap reaper) remains the floor. No mechanics change —
the handle parameter, validation, and warn-and-degrade behavior are already
specified in the parent contract.

### Watch item (recorded, not designed): list-changed signaling

`memory_toolset` today fires `tools/list_changed`. Stateless 2026-era
clients hold no notification channel; the likely replacement is cache-hint
expiry (`tools/list` with `cacheScope: "private"`, `ttlMs: 0`). Nothing in
this design may *depend* on the notification being delivered — the tier
change must be effective on the next `tools/list` regardless. Resolve the
client-behavior question at the scheduled 2026-08-20 SDK v2 re-check.

## Error handling

- Unknown/absent bearer with any token configured → 401, as today.
- A token present in the map with an empty/malformed principal → entry
  skipped at parse time (logged); that token then simply does not
  authenticate. A misconfigured entry must fail closed, not fall through to
  `default`.
- Principal resolution inside handlers is fail-open downward (matching the
  identity contract): resolution errors degrade to the `default` principal
  path; no identity error may fail a memory write or a `tools/list`.

## Testing

- Unit: token-map parsing (malformed entries, duplicates — first wins,
  logged); `compare_digest` gate with both singular and map tokens; writer
  precedence (override > named principal > X-PL-Writer/default); principal
  tier resolution (override > map > default) and TTL expiry; `default`
  principal behaves exactly as the current single-token path (regression
  pin).
- Integration: two clients with different tokens receive different
  `tools/list` results after one calls `memory_toolset`; the other's view
  is unchanged. Single-token install: `memory_toolset` still affects the
  (shared) view — documents decision 2's accepted consequence.
- Existing suites: `test_toolset_tiers.py` re-keys; `test_session_identity.py`
  must pass unchanged (session-axis untouched — this is the load-bearing
  assertion that the two axes stayed separable).
- Live verify after deploy: mint a second token for one client, confirm
  distinct writer stamping in Postgres and independent tier views.

## Ops / docs

- `ops/docker-compose.yml` + `ops/.env` example: `PSEUDOLIFE_MCP_TOKENS`
  documented beside the existing `PSEUDOLIFE_MCP_TOKEN`.
- `docs/guide/configuration.md`: new env row; tier-map row reworded from
  "writer" to "principal (writer)".
- Installer scripts unchanged (they configure single-token installs, which
  keep working); a short "per-client tokens" section is added to the LAN
  docs where `X-PL-Writer` is currently recommended.
- CHANGELOG entry under `[Unreleased]` per shipping checklist.

## Out of scope (recorded)

- The SDK v2 port itself (pin flip, FastMCP→MCPServer, `Context` threading,
  shim rework, tiering-hook rebuild on middleware) — gated on the WAIT
  triggers from the 2026-08-10 assessment.
- OAuth-principal adapter work (sub-project 3 of the parent contract);
  the token map is deliberately shaped so an OAuth subject can later slot
  in as another principal source.
- Tier-4 removal (explicitly deferred to the v2 port).
- Token rotation tooling beyond "edit env, restart daemon."
