# Session identity contract — design

**Status**: approved (interactive brainstorm + parallel research) · **Date**: 2026-07-18
**Predecessor**: the 2026-07-18 episode-keying diagnosis (memory bank), the
MCP-client-identity research report (same date; key citations inlined below).
**Scope**: sub-project 1 of 3 — the daemon-side identity contract plus the
Claude Code full fix. Sub-project 2 (research) is DONE and folded in;
sub-project 3 (ChatGPT/OAuth-principal adapter work) remains gated.

## Problem

`session_key` is today the transport's `mcp-session-id` header
(`writer_context.py:60`), designed on the assumption it is stable per Claude
Code session. Measured reality: it is stable per **connection** — the Claude
desktop app multiplexes successive and concurrent sessions over one
connection, so three different workstreams shared one key (episodes
`67e7c4cd`/`26bbe87c`/`f1ec5353`), `memory_episode_end` popped another
workstream's root (observed 2026-07-18), and auto-outcome inference can
assemble cross-project contexts. 82 of 88 keys are unique — sharing is the
desktop-app/concurrent-chip-session case, which is exactly the user's growing
workflow, and "devs with many concurrent sessions" generally.

The protocol is deleting the ground under the current design: the MCP
2026-07-28 revision (SEP-2567 "Sessionless", Final; SEP-2575 stateless)
**removes `Mcp-Session-Id` and protocol sessions entirely**, with normative
text that a connection "is not a conversation or session" and that
cross-request state "MUST be referenced by an explicit identifier the client
passes on each request." No client/conversation-identity SEP exists; Claude
clients send no per-conversation header (claude-code #41836, open); ChatGPT
connectors expose only the OAuth subject. Ecosystem prior art (Graphiti
`group_id`, Letta `agent_id`, mem0 `user_id`) keys durable state on explicit
out-of-band identifiers — never on `Mcp-Session-Id`.

## Decisions from brainstorm

1. **Full fix including concurrency** (user choice): the stdio shim — which
   already sets a per-process `X-PL-Session` — becomes the way concurrent
   sessions get correct attribution; the daemon additionally gains
   client-agnostic tiers so every transport improves.
2. **All-users scope** (user choice, overriding the contained option): the
   installer/plugin default flips to the shim; the README/i18n cascade at the
   next release cut is accepted. The contract must also serve non-Claude
   clients — hence the tool-argument handle tier, the only mechanism that
   works for ChatGPT (no per-conversation transport identity exists there).
3. **Spec-blessed pattern adopted**: daemon-minted episode handles threaded
   as tool arguments (the SEP-2567 "basket_id" pattern), advertised through
   the existing briefing hook so the common Claude Code case pays no new
   model-discipline cost.

## The identity contract

One resolution chokepoint (extending `writer_context.py` /
`resolve_writer`), evaluated per request, strict precedence:

| tier | source | scope | notes |
|---|---|---|---|
| 1 | `X-PL-Session` header | per shim process = per session | shim already sends it; any integrator can |
| 2 | explicit `episode` tool argument | per call | daemon-minted handle; universal across clients |
| 3 | hook-registered active session | machine-scoped pointer | SessionStart hook registers Claude's `session_id` |
| 4 | `mcp-session-id` header | per connection | legacy fallback; dies 2026-07-28 — annotate as such |
| 5 | none | — | writer id + idle-gap sessionization (reaper), the documented floor |

The resolved identity becomes `session_key` everywhere it is used today
(episode open/attribution, writer stamping). `session_key` is free text — no
schema change anywhere in this design.

Precedence rationale for the one disagreement case that matters: when a
shim-headed request also carries an `episode` handle, the header wins for
*identity* because it is infrastructure-asserted per process, while a handle
is model-supplied and can be confused between concurrent sessions' briefings
— but the write still lands in the named episode (attribution) with the
header identity stamped. Identity and target episode are separable; the
tiers rank identity only.

### Tier 2 mechanics — the episode handle

- The daemon already mints episode ids. Write-path tools `memory_store`,
  `memory_outcome`, and `memory_fact_set` gain an optional `episode`
  parameter (string, an existing OPEN root episode id or its unambiguous
  prefix ≥8 chars). Valid handle → the write attributes to that episode and
  the call's resolved identity becomes that episode's `session_key` for the
  rest of the request. Unknown/closed handle → the write proceeds under the
  next tier and the result carries
  `"episode_warning": "unknown or closed episode handle"` — never a hard
  failure (a stale handle must not lose a memory).
- `memory_episode_start` already returns the id; its docstring documents the
  handle use.

### Tier 3 mechanics — hook registration

- The plugin's SessionStart hook script starts reading its stdin JSON
  (Claude Code documents `session_id` as a common field for all hooks) and
  calls the existing endpoint as
  `GET /api/hook/session-start?session_id=<id>&source=<startup|resume|clear|compact>`.
- The daemon, on seeing a `session_id`: opens (or resumes, for
  `source=resume` with a known id) the session root episode keyed by that id
  immediately — no longer lazily on first store — sets the **machine-scoped
  active-session pointer** to it, and includes in the returned briefing text
  one line advertising the handle: the episode id and the instruction to
  pass `episode=<id>` on writes when running concurrent sessions.
- A SessionEnd hook is added to the plugin: calls
  `POST /api/hook/session-end` with the same `session_id`; the daemon closes
  that session's root (dream fires per existing close semantics) and clears
  the pointer **only if it still points at that session**.
- Pointer semantics are last-start-wins and machine-scoped (loopback
  single-user daemon). Documented limitation: two concurrent *unheaded,
  handle-less* sessions still misattribute to the newer — the shim (tier 1)
  or handles (tier 2) are the concurrency answers. LAN/multi-writer setups
  are directed to tier 1/2 in docs; the pointer is not per-writer in this
  design (YAGNI until a real multi-writer deployment exists).
- Hook calls remain fail-open: a hook that can't reach the daemon changes
  nothing (briefing already behaves this way).

### Ownership guard — `episode_end` and the reaper

`end_session` today pops the newest open root under a key. New semantics:
close only a root whose `session_key` equals the caller's resolved identity;
no match → `{closed: null, reason: "no owned open session"}` no-op. The
reaper keeps closing idle roots (it legitimately closes *any* idle root) but
uses the same helper so behavior stays single-sourced. The observed pop
actually travelled through `memory_episode_end`'s fallthrough (no open
sub-episode → it closed a session root): that fallthrough gets the same
ownership check. Regression test reproduces the observed cross-workstream
pop through BOTH paths and asserts the no-op.

## Shim as installer default (Claude Code full fix)

- `ops/install.sh` / `ops/install.ps1` register the stdio shim
  (`claude mcp add` with the shim command the pip package already ships)
  instead of `--transport http`. `--transport http` remains as an explicit
  opt-out, and the installer auto-falls back to HTTP (with the hook tier
  still active) when no suitable host Python/pipx is found — never a
  hard-fail install.
- The daemon stays containerized; the shim is a thin per-session host
  process proxying to it (existing, tested code path — `shim.py` sets
  `X-PL-Session`/`X-PL-Writer` per process).
- Docs currency: README transport story ("HTTP-first" framing becomes
  "shim-first for session identity; HTTP for single-session/simple
  setups"), configuration.md shim + LAN sections, episodes.md keying
  description, examples/CLAUDE.memory.md if it mentions transport. README
  narrative change ⇒ bump `docs/i18n/README.source.md` version and re-run
  translation subagents at the next release cut (accepted cost).

## Error handling

- All identity resolution is fail-open downward through the tiers; no tier
  error may fail a memory write.
- Handle validation errors warn-and-degrade (above), never raise.
- Hook endpoints keep the "never break a session start" contract
  (`session_hook.py` module docstring) — errors log and return 200/empty.
- The pointer lives in `meta` (`active_session_pointer`: `{session_id, ts}`)
  — no DDL; survives daemon restarts; cleared by SessionEnd or overwritten
  by the next SessionStart.

## Testing

- Unit: precedence order across all five tiers (header beats handle beats
  pointer beats mcp-session-id); handle validation (open/closed/unknown/
  prefix); pointer set/overwrite/clear + clear-only-if-owner; end_session
  ownership guard (the observed-pop reproduction); reaper unaffected.
- Integration: shim round-trip asserts distinct `X-PL-Session` per process
  (extend existing shim test); hook endpoint with `session_id` opens the
  episode and the briefing text carries the handle line.
- Live verify after deploy: run two concurrent sessions (main + chip);
  confirm distinct episodes, correct `episode_end` targeting, and that the
  briefing shows the handle.

## Out of scope (recorded)

- MCP SDK upgrade for the sessionless/initialize-removal protocol revision
  (forced eventually by SEP-2575) — its own follow-up project.
- ChatGPT/OAuth-principal namespace work and any multi-tenant identity
  (sub-project 3; the tier-2 handle mechanism is deliberately already
  client-universal so sub-project 3 builds on, not into, this contract).
- Per-writer pointer semantics for LAN multi-writer banks.
- Back-migration of historical blended episodes.
- Content-based sessionization heuristics.
