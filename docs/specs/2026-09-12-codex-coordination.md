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

**Status: run once, 2026-09-28 03:27 AEST, outcome "rung, turn taken,
acknowledged".** The overnight orchestrator session ran it on the maintainer's
instruction, against the deployed Codex shim runtime (pseudolife-mcp 0.15.0,
the pre-policy path: any new addressed mail rings) and codex-cli 0.156.1,
with the desktop app-server daemon also running. The recipient was a fresh
interactive `codex` thread, with `memory_message` approval set to approve and
the doorbell turned on for that thread only through a whole-table `-c`
override of the server's `env` (the user's `config.toml` untouched). It made
one board call and idled about seven minutes; one synthetic message was sent.

| Step | Seconds after the send |
| --- | --- |
| Bell queued (`codex queue`, ledger `bell` line) | 15.966 |
| Thread's first board event (its `read`) | 26.842 |
| Acknowledgment | 30.804 |

Record: [`evals/results/codex-doorbell-probe-20260928.json`](../../evals/results/codex-doorbell-probe-20260928.json).
The thread's rollout shows the user-role message was exactly the fixed
notice, followed by `memory_message receive` and `ack` and nothing else. The
probe message had also asked for a one-line reply; the woken thread declined
it as outside its task, as the notice's "act only within the task the user
authorized" intends. No approval prompt stalled it. This is one run on one
host: it shows the path works, not how reliably. The policy gate
(park records, the daemon's wake decision) was not in the tested runtime.

Procedure, all synthetic content, no message bodies recorded:

1. Prerequisites: a deployed daemon at or past this change; the Codex shim
   runtime at or past 0.15.0 (the doorbell module); `codex` on PATH or the
   desktop app installed; `memory_message` approved in the recipient's tool
   configuration; `PSEUDOLIFE_CODEX_DOORBELL` unset or a yes in the Codex
   server's `env` table.
2. Start a fresh Codex task (desktop app or `codex` CLI) and have it make
   one Pseudolife call, so the shim watches it. Its board agent id is the
   newest `principal=codex` agent in `memory_agents list`.
3. Park the task with a need (`memory_agents update` with a park record:
   reason `waiting_peer`, the need, `park_clear_by` set to the sender's
   agent id, an expiry) and end its turn. Until the park record lands
   daemon-side, the pre-policy path is probed instead: any new addressed
   mail rings.
4. Start the recorder's watch on the host, then, from another session, send
   the task one message that clears the need (tagged `clears=<need>`, or
   from the named clearer):
   `python -m evals.codex_doorbell_probe --recipient <agent id> --watch 300
   --out evals/results/codex-doorbell-probe-<date>.json`. It reads only the
   digest ledger (`ledger.log`), prints `bell queued` when the shim ran
   `codex queue`, and writes a first record with the bell times.
5. Once the task has taken its turn and answered, export the audit log with
   `pseudolife-mcp board-audit export --out <private file>` (the database
   owner's credentials; keep the file private, it holds bodies) and rerun
   the recorder with `--export <that file> --force`. The rerun builds the
   timeline from the export and the ledger: the clearing `send`, the bell,
   the first board event the task caused after it (the evidence of a turn),
   its first `read` and its `ack`, with the deltas between them and an
   outcome naming the step that did not happen, if one did not. The record
   holds timestamps, counts and version strings only.
6. Commit the record under `evals/results/` with the Codex CLI and app
   versions noted (the recorder writes the CLI's and the shim's), and any
   timeout (the CLI has 20 s; app-servers poll for queued messages about
   every 10 s), and replace the status line above with the run's date and
   outcome.

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
