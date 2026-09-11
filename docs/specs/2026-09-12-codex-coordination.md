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
  different task UUID and state file. Scope state by bank URL and bearer token.
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
