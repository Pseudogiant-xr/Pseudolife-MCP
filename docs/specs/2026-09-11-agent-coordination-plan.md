# Agent awareness and direct messaging — implementation plan

Build agent discovery and a durable addressed mailbox so independent sessions can
coordinate without the operator copying messages between apps. Prove the Claude
Code channel connection before depending on live delivery; keep authenticated
retrieval available when a host cannot receive events.

- **Date:** 2026-09-11
- **Status:** local implementation, final full suite, live Claude behavioral checks and controlled overhead experiment complete; startup/resume limitations documented. Not deployed.
- **Design:** [experimental design](2026-09-11-agent-coordination-design.md).
- **Baseline:** `e88cbcf4`; work on `codex/agent-coordination` in an isolated worktree.
- **Scope:** awareness and addressed messaging. Resource claims are a separate plan.

## Implementation checkpoint

The channel transport, awareness, authenticated mailbox, two model tools, async
receive waits, private shim adapter and offline restore recovery are implemented.
The full test suite passes with bench PostgreSQL available and no PostgreSQL
availability skips. Targeted protocol, ownership, real-Postgres concurrency and
two-adapter integration tests also pass. Independent review fixes cover attachment fencing, client disconnect,
bounded workers and adapter startup cancellation, redacted errors, retention and HLC restart ordering.
Schema DDL stays independent of driver imports so concurrent daemon health and
warmup initialization cannot create a partial-driver import race. Existing
retrieval ranking and extraction behavior are unchanged; no GPU regression gate
or extraction ladder was needed for this increment.

A live Claude Code 2.1.267 experiment exercised the real shim and disposable
daemon: idle and busy delivery, two-agent request/reply/acknowledgment, opt-out,
ordinary pull, crash replay, and refusal of fabricated approval and source tags.
Database state and tool traces distinguish emission from model acknowledgment.
The first resumed channel turn exposed a host-local unavailable-tool failure;
an ordinary prompt recovered the unchanged connection and pending message.
That startup behavior remains a documented preview limitation, not a passing
unattended-recovery guarantee. Checked items below have implementation or
experiment evidence; unchecked items remain acceptance work. Adapter
startup has a three-second cancellation deadline followed by bounded cleanup,
not a proven three-second wall-clock ceiling for the entire shim startup.

The shared-resource experiment used an explicit pre-action awareness check:
the receiver deferred a scripted overlapping-file action, while two agents on
disjoint files continued concurrently. This demonstrates the checkpoint, not
automatic conflict detection. The sender made an unnecessary empty receive while
waiting in the two-model exchange, so the no-model-polling target was not fully
met. A forced crash replayed the same unacknowledged message after lease expiry;
interrupted synthetic work was rerun, so external actions still require their
own idempotency policy.

Omitting the host's development-channel flag produced a transport attempt but
no model receipt. Explicit receive recovered that message. Wake opt-out produced
no event, and a daemon restart left ordinary receive/reply/acknowledgment usable
after background delivery stopped. These observations are why queued, attempted
and acknowledged remain separate states. Live tests also exposed a host that
keeps structured tool output while dropping extra text blocks; coordination
hints must reach both representations.

A disposable full `pg_dump`/`pg_restore` experiment also passed: pending mail
survived restore; offline recovery revoked old credentials; selective rebind
retained the address and mail; receive and acknowledgment worked with wake off.
A controlled ASGI adapter experiment also completed across disabled, pull and
channel modes with no duplicate deliveries. Its ordinary-call control uses
synthetic stats, and its acknowledgments come from the test harness. It does not
resolve model behavior, host wake latency or real store/search overhead.

A separate loopback socket experiment exercised the actual daemon, CPU embedder,
PostgreSQL and stdio shim with identical store/search inputs in all three modes.
Result counts and ordinary response sizes matched, health remained responsive,
and the observed warm-path latency stayed within the experimental target. This
was one short run in fixed arm order, with harness acknowledgments; it supports
no speedup or general latency guarantee. Its frozen source preceded the final
failure-cleanup and structured-hint fixes, so it measures the successful ordinary
path, not the revised failure path. Tagged traces and summaries are retained
with the experiment artifacts.

## Execution rules

- Check the current branch, upstream changes and other-session status before work.
  Preserve unrelated edits. Rebase only this feature worktree when appropriate.
- For every behavior change, write the test and observe the intended RED before
  implementing. For identity/delivery guards, disable the guard once and observe
  the relevant test fail before relying on it.
- Use independent review for the identity, persistence and adapter contracts.
  Main session owns integration and memory writes.
- Read live config for deployment claims. All probes use a disposable daemon and
  synthetic messages; do not use the memory bank as a test fixture.
- Record experiment launches, expected first output and final outcomes in status
  memory. If any work is deliberately left unattended, create and verify the
  required 15-minute heartbeat and persist its ledger.
- No production release or deployment is implied by a passing local prototype.

## Phase 0 — prove the channel and freeze the interface

**Files:** `pseudolife_memory/shim.py`, `pseudolife_memory/cli.py`, proposed
`pseudolife_memory/channel.py`; `tests/test_shim.py`, proposed `tests/test_channel.py`.

- [x] Recheck current SDK and Claude channel docs; record versions and the public
  serving/frame APIs used. Reproduce default and `auto` negotiation in fixtures.
- [x] RED: optional channel mode advertises `claude/channel`, uses an earlier
  handshake, and never advertises permission relay. Ordinary mode still supports
  the existing modern negotiation and tool-list notifications.
- [x] RED: a labeled custom notification can be serialized after initialization
  alongside tool responses; startup failure and disconnect clean up the worker.
- [x] Implement the smallest wire prototype with a fake inbox. Prefer the public
  handshake-only runner for channel mode. Do not globally patch protocol constants
  or downgrade the daemon/upstream client.
- [x] Keep diagnostics on stderr and free of credentials, message bodies and
  socket variables. Channel mode does not depend on the peer inbox protocol.
- [ ] Verify an explicitly opted-in disposable Claude session registers the channel
  and acknowledges a synthetic event. The host's preview dialog/organization gate
  is real; record blocked availability instead of bypassing it.
- [x] RED: policy/protocol rejection and a successful stream write alone cannot
  mark the endpoint as confirmed live. Record observed registration and explicit
  agent acknowledgment separately from configured capability.
- [x] Verify host resume identity: what stable session key is available to this
  adapter, and how does a reconnect prove it owns its old mailbox? Test separate
  simultaneous sessions, not just repeated starts in one project directory.
- [x] Draft the two tool input schemas; measure descriptions, parameter descriptions
  and total serialized manifest sizes for all tiers. Fit existing budgets or
  document a measured opt-in allowance in review before implementing the surface.
- [x] Freeze bounded message size, rate, queue, page, retention/retry and poll
  limits with explicit rationale and tests. No unbounded queues or retries.

**Exit:** actual protocol/channel acknowledgment evidence, or a precise unsupported
case and a pull-only development path. A socket write is not host acceptance.
The native-pipe route is not a required dependency or an automatic fallback.

## Phase 1 — expose awareness using existing state

**Files:** `service.py::session_briefing`, `memory/briefing.py`, `web/session_hook.py`,
`mcp_server.py`; existing episode/briefing tests plus proposed
`tests/test_coordination_awareness.py`.

- [x] RED: two open roots are visible to each other; the caller is excluded;
  closed roots and bounded unknown-scope sessions are labeled correctly.
- [x] RED: a title or episode handle does not establish principal identity; absent
  activity evidence returns unknown, not a fabricated liveness timestamp.
- [x] Add bounded awareness to the briefing and a refresh operation. Existing roots lack trusted project/task
  scope, so awareness reports unknown scope and omits status bodies. Registered
  agents support exact project/task filters; no new ranking or extraction policy.
- [x] Keep last reported activity distinct from adapter availability. Do not alter
  episode lifetime or force empty sessions to write memories to stay discoverable.
- [x] Provide instructions to refresh before shared-resource work and on resume.
  Keep the static user-prompt hook; no extra daemon round-trip on every user turn.
- [x] Test disabled/cold-bank behavior and ordinary briefing output stability.

**Exit:** useful peer discovery with no schema change and no exclusivity promise.

## Phase 2 — authenticated registration and durable mailbox

**Files:** `coordination.py` and `storage/coordination.py`,
`storage/schema.py`, `storage/postgres.py`, `service.py`, `web/api.py`, proposed
`web/coordination.py`, `utils/config.py`, `transfer_cli.py`.

- [x] Enumerate register, update, detach, resume, revoke, send, receive, attempt,
  ack, expire, prune, restart and restore paths before building derived state.
- [x] RED: validated REST principal reaches the new coordination handler; unknown
  bearer/instance credentials are rejected. Do not reuse fail-open attribution as
  authorization or weaken the existing browser/Origin checks.
- [x] RED: two instances sharing a principal cannot impersonate each other using
  routing IDs, task keys or episode handles. Adapter-injected instance credentials
  never appear in model arguments, return values, logs or portable exports.
- [x] Implement registration, hashed instance credentials, ownership validation,
  operator allowlist and explicit wake configuration. A trusted adapter owns the
  private resume state; labels are not recovery credentials.
- [x] RED: two processes sharing resume state cannot hold active attachments at
  once. Same-attachment resume is idempotent; detach/expiry permits a new generation
  and fences stale waits and attempt reporting. Include pre-emission checks.
- [x] Add `coordination_agents` and `coordination_messages` using additive,
  idempotent DDL. Episode deletion does not delete either table's history.
- [x] RED: sends allocate recipient sequence and commit atomically; pagination
  cannot skip a deliberately delayed earlier commit. Test real Postgres concurrency.
- [x] RED: identical request-key retries return the original ID; changed payloads
  conflict; default expiry remains stable across retries.
- [x] RED: receive is non-destructive, ack is explicit/recipient-only/idempotent,
  reply references are checked, and wrong-mailbox cursors fail.
- [x] RED: restart, empty-episode prune/reaper and disconnect preserve pending mail.
  Expiry withholds bodies while deduplication metadata survives the retry window.
- [x] Classify both tables in `BENCH_RESET_TABLES` and portable-export exclusions.
  Verify full backup/restore includes mail. Exercise a restore runbook that starts
  with delivery disabled and reattaches adapters explicitly; do not claim automatic
  detection of an arbitrary database restore.
- [x] Add an operator-only recovery operation, excluded from model tools, clearing
  restored attachment/wake state and revoking restored instance credentials before
  re-enablement. Test old running adapters cannot resume with revoked credentials;
  retained mail can be deliberately rebound. Normal restart must preserve recovery.
- [x] Keep all database critical sections short, through the established storage
  transaction discipline. No direct model or bridge SQL mutation paths.

### Schema checklist in the same implementation change

- [x] Re-read current `SCHEMA_META_VERSION` (37 at planning baseline); choose the
  next unused version only now. Add no per-version test file.
- [x] Update schema declaration, `tests/test_schema_version.py::CURRENT_SCHEMA`,
  README capability row, configuration DSN/current schema/history rows,
  `[Unreleased]` dated changelog and `docs/atlas/atlas.json` schema/storage cards.
- [x] Update the two current-schema literal assertions in
  `tests/test_migrate_embeddings.py`; leave its historical migration pins intact.
- [x] Update reset and portable-transfer table rosters; run
  `test_bench_reset_tables.py`, `test_transfer_cli.py`, relevant DDL-shape tests,
  schema/version/Atlas/release currency tests.
- [x] Regenerate agent-readable documentation after guide changes.

## Phase 3 — MCP tools and async delivery endpoints

**Files:** `mcp_server.py`, `writer_context.py` if needed for validated bindings,
`web/api.py`, proposed `web/coordination.py`, `coordination.py`;
`test_mcp_server.py`, `test_web.py`, `test_daemon_http.py`,
`test_tool_consolidation.py`, `test_toolset_tiers.py`, new coordination tests.

- [x] RED: model tools cannot claim a sender or operate on a peer's mailbox; both
  MCP and REST paths resolve the same principal/instance ownership.
- [x] Implement `memory_agents` and `memory_message` per the design. Immediate,
  bounded model receive; scalar message-ID acknowledgment; one recipient per send.
- [x] RED: coordinating traffic and credentials never enter bands, cortex, graph,
  dream input, session digest input or memory entry counts through these APIs.
- [x] Add bounded unread hints without implicit acknowledgment. Manual promotion
  remains an explicit normal memory write with message provenance.
- [x] RED: dedicated async long polling closes the read/register-wait race, cancels
  on client disconnect and does not hold the service lock, DB transaction or an
  unbounded executor worker while waiting.
- [x] RED: simultaneous long polls do not stall health or normal store/search;
  missed in-memory signals recover by rereading committed rows.
- [x] Check tool annotations for action-based mutations; a mixed-action tool is
  not globally read-only. Re-run description and parameter-budget tests.

## Phase 4 — wire the Claude adapter and authenticated retrieval fallback

**Files:** `shim.py`, `channel.py`, `cli.py`, adapter-private state handling;
`tests/test_shim.py`, `tests/test_channel.py`, coordination integration tests.

- [x] Bind one registered instance to the existing shim session; ordinary opted-in
  shim clients can send/receive with injected credentials without live events.
- [x] Channel mode polls with an async client independently of ordinary MCP calls.
  Reconnects use bounded backoff; stop/cancel releases waiters and transport tasks.
- [x] Only addressed messages to an opted-in recipient produce events. A board
  update, receipt or background activity heartbeat never wakes a model.
- [x] RED: raw message text cannot override sender metadata or channel instructions;
  no permission-relay capability, automatic approval or authority promotion.
- [x] Implement explicit receive acknowledgment and replies. Do not equate frame
  submission with acceptance; retain queued/unacknowledged rows on silent drops.
- [x] RED: repeated notification/receive paths share IDs and do not multiply
  logical messages; bounded retries cannot create endless wake/reply loops.
- [x] Track attachment-local attempts separately from durable acknowledgments:
  an unacknowledged first page cannot starve later mail or cause unbounded wakes.
  Reconnect replays pending mail in a fresh, bounded pass.
- [x] For direct HTTP clients without instance injection, return an actionable
  unsupported-binding result for owned mailbox operations. Do not relax auth to
  make the demonstration pass. Shared-shim subagents remain one endpoint.
- [x] Investigate Codex receive integration independently. Until proven, document
  Codex-to-Claude live feasibility and Claude-to-Codex explicit retrieval only.
  Do not infer Hermes support or absence from another host's API.

## Phase 5 — controlled host experiment and release readiness

**Files:** `ops/coordination_probe.py`, `evals/coordination_bench.py`, synthetic fixtures, tagged result
artifacts; `docs/guide/providers.md`, `episodes.md`, `configuration.md`,
`security-posture.md`, README and plugin/installer docs when actually applicable.

- [x] Build a reusable probe that always writes a redacted JSONL trace and summary.
  Include protocol, SDK/host versions, capability registration, message IDs,
  enqueue/attempt/ack times and failures. Do not log secrets or real project data.
- [x] Execute every design acceptance case against disposable Postgres. Compare
  identical disabled/pull/channel workloads; record p95 overhead, context bytes,
  calls, wakes and duplicates. Include a same-input control for model comparisons.
- [x] Run opted-in busy/idle/disconnected/resumed receiver cases. The first output
  must be affirmatively verified within two minutes; a process handle is not proof.
- [x] Test legitimate review requests and attempts to fabricate user permission.
  Record behavioral limits as well as deterministic gate results.
- [x] Review the feature independently, folding actionable findings before commit.
  Re-review any identity, persistence or delivery contract changed during the fold.
- [x] Run the required full suite with bench Postgres available and no PG-availability
  skips. Run the retrieval regression gate if ranking/serving changes actually occur;
  run the ladder only if extraction/dream behavior changes. Neither is assumed away.
- [x] Update public capability/non-goal wording to describe what passed, preview
  launch requirements, unsupported hosts, wake costs and retrieval fallback.
  Do not advertise live cross-app delivery from a one-sided demonstration.
- [x] Regenerate `llms.txt`/`llms-full.txt`, run currency/PII guards and inspect diffs.
  No production version cut is part of this feature implementation automatically.
- [ ] If deployment is authorized, use `ops/update.ps1` with backup/rollback, then
  verify real enqueue/receive/ack and the enabled adapter against the daemon.
  Follow the release-procedure skill for any subsequent publish/version work.

## Validation commands and exit-code discipline

For the planning documents alone:

```powershell
python ops/gen_llms_txt.py
python ops/gen_llms_txt.py --check
python -m pytest tests/test_llms_txt.py -q
git diff --check
```

For implementation, use the available project Python runtime, explicitly verify
the bench on `127.0.0.1:5433`, and preserve pytest's exit status. The path below is
outside the repository so logs cannot accidentally become public artifacts:

```powershell
$env:HF_HUB_OFFLINE = '1'
$coordLog = Join-Path $env:TEMP 'pseudolife-coordination-pytest.log'
python -m pytest tests/ *> $coordLog
$coordExit = $LASTEXITCODE
Get-Content -LiteralPath $coordLog -Tail 60
Write-Output "pytest exit: $coordExit"
exit $coordExit
```

Do not point test configuration at the live bank. Use the per-process disposable
database fixtures; do not truncate another session's database or kill its tests.

## Checkpoints and fallback decisions

| Checkpoint | Proceed when | If it fails |
| --- | --- | --- |
| Channel wire/host probe | Opt-in host actually registers and acknowledges | Keep pull path; record unsupported protocol/preview/API seam. |
| Identity and persistence | Ownership, restart and ordering tests pass | Stop mail integration and fix these contracts first. |
| Manifest and overhead | Measured interface fits a reviewed budget; bounded overhead | Reduce new surface/traffic, not existing safety contracts. |
| Host lifecycle | Busy/idle/reconnect and opt-out behaviors are demonstrated | Ship only the proven capability with precise limitations. |
| Release readiness | Review, required suite, migration/docs and live checks complete | Keep feature experimental/off; no completion claim. |

No blocked host probe justifies silently changing the plan to reverse-engineer a
private protocol, editing global host settings or presenting another app's tool
as an external daemon API. Resolve the exact limitation and report it.
