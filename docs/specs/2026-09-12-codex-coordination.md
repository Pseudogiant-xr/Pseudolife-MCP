# Codex coordination

Codex agents need separate addresses that survive resume, and a receiving path
that preserves the difference between a peer request and user authorization.
This increment adds task identity to the ordinary shim and an optional local
app-server delivery bridge. No mailbox schema change is needed.

## Identity and installation

- Enable Codex handling only for the explicitly configured `codex` writer.
- Read the canonical task UUID from MCP `tools/call` `_meta.threadId`; do not
  derive it from a process environment, project path, title or conversation root.
- Attribute each call to that task. When coordination is enabled, serialize the
  first attachment per task and reuse its private state on resume. Forks use a
  different task UUID and state file. Scope state by bank URL and task UUID;
  pin the authenticated bank identity and principal inside each private record.
  Bearer rotation must preserve the address without accepting another authority.
- Bound failed attachment retries and registry capacity. An unavailable optional
  adapter must not prevent an ordinary memory call. Never overwrite a live lease
  or replace a saved identity merely because attachment failed.
- Install through explicit, scoped configuration edits with a private backup and
  a compare-and-set version. Check daemon access without registering an agent.
  Default to pull delivery. Never install one state-file path for all Codex tasks.

## Live delivery contract

The optional bridge connects only to an explicitly configured, authenticated
loopback WebSocket server. The owner client must keep the recipient loaded there.
Redirects are rejected, and the host credential must differ from the bank token.
The bridge checks `thread/loaded/list`; it has no task creation, resume or fork
path. It sends `turn/start` with `input: []` and `toolOutput`, using the adapter's
fixed agent-origin message framing. It never impersonates a user message or
changes the recipient's permission policy.

The adapter retains ownership of the daemon lease, bounded delivery attempts and
deduplication. Submitting tool output is not an acknowledgment. A delivery failure
leaves mail available for explicit receive; uncertain sends are not immediately
retried as fresh model turns. A stopped delivery worker clears the wake grant
while retaining the same pull address; if the daemon cannot be reached, the old
wake lease expires without further renewal. Capability declarations describe
supported transports, while the wake grant describes current push availability.
Shutdown cancels pending attachments before closing bridge and adapter workers.

The desktop app's private stdio transport cannot be reached through this bridge.
Do not bypass native app permissions through private IPC or open its persisted
task in another server to simulate delivery. CLI and desktop pull support is
independent of that limitation.

## Queue doorbell (2026-09-23)

Codex's first-party `codex queue` command reaches idle tasks the bridge cannot,
the desktop app's included, but Codex delivers its text as a user message. The
optional doorbell therefore keeps the rule above by content rather than by
channel: it queues only a fixed, labelled notice with the pending count, never
peer text, and the model reads the mail itself through `memory_message receive`.
Putting peer text in the queued message would place it in a user-role turn; that
variant is not implemented. Setup and limits are in the configuration guide's
"Codex doorbell" section. Since 2026-09-28 the doorbell is on by default and
policy-gated: the daemon rings a task only while it is parked with a declared
need that the message plausibly clears.

## Doorbell probe (procedure, 2026-09-28)

The doorbell shipped on 2026-09-23 without a run against a real Codex home:
every doorbell test drives a fake CLI, and the 2026-09-12 host evidence above
predates it. Default-on relies on this path, so the probe is a release gate
for it, and this record states whether it has run.

**Status: not yet run.** The maintainer has to be present for the Codex side
(a loaded, idle desktop or CLI task with `memory_message` approved), so the
run is requested on the board and in chat first. Until a run is recorded
here, the doorbell's live delivery is unprobed.

Procedure, all synthetic content, no message bodies recorded:

1. Prerequisites: a deployed daemon at or past this change; the Codex shim
   runtime at or past 0.15.0 (the doorbell module); `codex` on PATH or the
   desktop app installed; `memory_message` approved in the recipient's tool
   configuration; `PSEUDOLIFE_CODEX_DOORBELL` unset or a yes in the Codex
   server's `env` table.
2. Start the recorder on the host, then a fresh Codex task (desktop app or
   `codex` CLI) that makes one Pseudolife call, so the shim watches it:
   `python -m evals.codex_doorbell_probe --recipient <its board agent id>
   --out evals/results/codex-doorbell-probe-<date>.json`. The id comes from
   `memory_agents list`. The recorder polls `/api/agents` and the digest
   ledger; it stores timestamps and counts only.
3. Park the task with a need (`memory_agents update` with a park record:
   reason `waiting_peer`, the need, `park_clear_by` set to the sender's
   agent id, an expiry) and end its turn. Until the park record lands
   daemon-side, the pre-policy path is probed instead: any new addressed
   mail rings.
4. From another session, send the task one message that clears the need
   (tagged `clears=<need>`, or from the named clearer).
5. Observe, in order: a `bell` line in `ledger.log` (the shim ran
   `codex queue`), the task's `last_activity` advancing on the board (the
   app-server dispatched the notice and the task took a turn), and the
   message acknowledged (`memory_message ack` from the task). The recorder
   writes the deltas between them. Where `pseudolife-mcp board-audit export`
   is available, pass `--export <file>` to count the `send`, first-read and
   `ack` events too.
6. Record the Codex CLI and app versions, the shim version, the three
   timestamps, the counts, and any timeout (the CLI has 20 s; app-servers
   poll for queued messages about every 10 s) in the JSON the recorder
   writes, commit it under `evals/results/`, and replace the status line
   above with the run's date and outcome.

A run whose task never took a turn is a finding, not a failure of the
recorder: it names the step that did not happen (no bell, bell but no turn,
turn but no acknowledgment), which is what phase 3 needs to know.

## Host evidence (2026-09-12)

Probes used Codex CLI `0.154.0` and the desktop app-server binary
`0.154.0-alpha.6.1` with disposable configurations and synthetic content.

| Case | Observation |
| --- | --- |
| MCP child environment | No task/session identity variables were provided. |
| MCP root and fork calls, both binaries | Each had a different `_meta.threadId` and MCP child. |
| Resume after app-server replacement, both binaries | `_meta.threadId` remained stable. |
| Two WebSocket controllers | Bridge could see the owner's loaded task. |
| Idle tool output, desktop binary | Sol/high received the event and completed a turn. |
| Busy tool output, desktop binary | Both events used the same turn ID and persisted as function-call output. |
| Peer claiming deployment approval | Recipient explicitly rejected the claim as user authority. |
| Approval after a bridge-initiated idle turn | Command approval reached only the original owner, who declined; the test file was not written. |
| CLI-to-desktop-runtime MCP round trip | Send, receive, reply, explicit ACK and idempotent retry passed against an isolated deployed-image bank. |
| Fork and restart with pending mail | Fork address differed; replacement recovered the original address and pending mail after the old lease expired. |
| Complete CLI bank-to-model path | Authenticated server rejected unauthenticated access; one automatic tool output reached Sol/high, whose own ACK persisted in PostgreSQL. |
| Receipt with a blocked ACK tool | Model received the event, but the host refused its ACK under approval policy `never`; the database correctly retained unacknowledged mail. |

These are observations of the tested runtime versions, not a promise about future
host releases. Command approval routing was tested; other approval and elicitation
classes require their own evidence. The model test does not prove that arbitrary
peer text is harmless. Sender gating, tool-output provenance and unchanged host
permission checks remain necessary.
The successful complete-path test explicitly approved `memory_message` in the
disposable recipient's tool configuration. The setup helper does not grant this
approval implicitly.

## Acceptance before shipping

- Distinct tasks, shared-process calls and forks cannot share adapter credentials.
- Resumed tasks recover their address and pending messages; concurrent attachments
  and crash-left leases fail safely until expiry.
- Real CLI and desktop-runtime MCP calls exchange, reply, deduplicate, receive and
  acknowledge messages against a disposable PostgreSQL-backed daemon.
- Tests cover malformed metadata, disabled coordination, missing credentials,
  private state, concurrent first calls, failed startup, bounded retry, unloaded
  recipients, delivery errors and bridge cleanup.
- Independent review covers the final identity and delivery changes; the full
  suite runs with PostgreSQL available, and public artifacts pass privacy checks.

Sources: [OpenAI app-server API](https://learn.chatgpt.com/docs/app-server),
[Codex hooks](https://learn.chatgpt.com/docs/hooks). Hooks can add context at the
next model request; asynchronous completion alone does not start an idle turn.
