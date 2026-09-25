# Configuration

Every knob the daemon reads — environment variables, the tuned built-in
defaults, toolset tiers, the stdio shim, LAN sharing, data layout, and
backups. Part of the [user guide](../../README.md#documentation).

## Connection / deployment env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → lite/file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v42). Unset: with the `[lite]` extra installed the daemon auto-starts an embedded PostgreSQL and fills this in itself; otherwise v0.1 file-only mode (announced loudly at startup). |
| `PSEUDOLIFE_MCP_STORAGE` | `auto` | `files` opts the daemon out of the `[lite]` embedded Postgres (file mode even when pg0-embedded is installed). Only consulted when no DSN is set. |
| `PSEUDOLIFE_MCP_DAEMON_URL` | `http://127.0.0.1:8765` | Daemon the shim connects to (and auto-starts). Use an HTTP(S) origin: scheme, host and optional port, without a path, user information, query or fragment. |
| `PSEUDOLIFE_MCP_NO_SPAWN` | _(unset)_ | Set `1` on the **shim** to disable its spawn-a-daemon fallback: when nothing answers at `PSEUDOLIFE_MCP_DAEMON_URL` it waits (up to ~3 min) for an external daemon instead. The Docker-tier installers set this on every shim registration — after a reboot the shim can probe before Docker Desktop has bound the port, and a spawned host fallback then wins the bind race and shadows the real bank with whatever stale local state it finds. Leave unset on pip/lite installs, where the spawn fallback is the intended zero-config path. |
| `PSEUDOLIFE_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Daemon bind address. |
| `PSEUDOLIFE_MCP_TOKEN` | _(unset)_ | Bearer token; **required** to bind a non-loopback host (a `PSEUDOLIFE_MCP_TOKENS` map also satisfies this). Maps to the reserved principal `default`, which keeps the `X-PL-Writer`/`PSEUDOLIFE_WRITER_ID` writer path. |
| `PSEUDOLIFE_MCP_TOKEN_FILE` | _(unset)_ | Client-side private file containing the bearer token. The shim reloads it for each operation, so replacing its contents does not require a client restart. An explicitly configured file takes precedence over a literal token; an unavailable, unsafe or malformed file fails closed. The daemon continues to use its own token configuration. |
| `PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS` | `180` | Total deadline for one upstream shim operation, including connection and initialization. Accepts finite positive seconds; invalid values use the default. Set the client's tool timeout above this value to leave time for the shim's sanitized failure response. An uncertain write is never automatically replayed. |
| `PSEUDOLIFE_MCP_TOKENS` | _(unset)_ | Per-principal bearer tokens: `token:principal,token:principal`. A matched token's principal **is** the writer id and keys the toolset tier (the identity axis that survives the MCP 2026-07-28 stateless core). Malformed entries are logged and skipped — a skipped token does not authenticate, and a map that parses to zero entries with no singular token refuses startup rather than running open. May be set alongside `PSEUDOLIFE_MCP_TOKEN`; the map wins for its tokens. Note the singular-token holder is fully trusted and may still assert any writer via `X-PL-Writer` — mint per-principal tokens when that distinction matters. |
| `PSEUDOLIFE_MCP_TRUST_BIND` | _(unset)_ | Set `1` to allow a non-loopback bind without a token when the boundary is external (containerized, loopback-published). The compose daemon sets this; never set it for a host daemon. |
| `PSEUDOLIFE_MCP_DATA_DIR` | `./data` (cwd-relative) | Weights cache + legacy-migration source + ChromaDB. When the `[lite]` embedded Postgres engages, the default moves to a stable per-user dir instead (`%LOCALAPPDATA%\pseudolife-mcp`, `~/.local/share/pseudolife-mcp`, or `~/Library/Application Support/pseudolife-mcp`) — a per-launch-directory Postgres bank would be a data-scattering footgun. Windows lite note: must be ASCII-only (the daemon refuses otherwise, with the remedy in the message). |
| `PSEUDOLIFE_MCP_CONFIG` | `<data_dir>/config.yaml` if present, else built-ins | Override MIRAS / embedding / memory config. |
| `PSEUDOLIFE_WRITER_ID` | `unknown` | Identifies this writer on every canonical write (schema v11). The shim forwards it as the `X-PL-Writer` header; the compose daemon defaults to `mcp-client`, and the installer pins `claude-code` / `claude-desktop` / `codex` / `gemini` / `mcp-client` in `ops/.env` per the selected `--client`. Existing installs that predate the client selector should set `PSEUDOLIFE_WRITER_ID=claude-code` in `ops/.env` to keep their writer identity (and any `PSEUDOLIFE_MCP_TIER_MAP` keyed on it) stable. |
| `PSEUDOLIFE_MCP_AUTOSAVE_SECONDS` | `30` | Interval of the file-mode autosave loop (weights/state cadence; Postgres-mode entries are transactional regardless). |
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | `60` | Daemon on Linux/glibc only (the Docker tier): how often a background thread calls `malloc_trim(0)` to hand back heap memory glibc keeps after embedder encode bursts. Measured 2026-09-23 with four persistent worker threads, 1,007-1,433 MiB of it was still resident at idle with the fp32 embedder (897-1,476 MiB bf16), and a trim took a median 7 ms with no measurable slowdown of the next encode (`evals/results/allocator-trim-pool-20260923.json`, `allocator-trim-probe-20260923.json`, `allocator-trim-latency-20260923.json`). It lowers what the daemon holds after a burst, not the peak of the burst itself. `0` disables. |
| `PSEUDOLIFE_SESSION_REAP_SECONDS` | `300` | How often the idle-session reaper sweeps. The idle *threshold* it enforces is `PSEUDOLIFE_SESSION_IDLE_SECONDS` — see [Episodes](episodes.md). |
| `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION` | _(unset)_ | Set `1` to restore the retired `mcp-session-id` transport-session fallback for one release (rollback hatch; logs a warning on first use). The header names the HTTP *connection*, not the session — concurrent sessions share it — and the MCP 2026-07-28 revision removes it from the protocol. Session identity rides the hook-registered episode handle and `X-PL-Session` instead — see [Episodes](episodes.md). |
| `PSEUDOLIFE_DAEMON_MEM_LIMIT` | `6g` | Docker tier only (read by compose, not the daemon): hard memory cap on the daemon container, with the memory+swap total pinned to the same value — no swap, so exceeding the cap is a clean container restart rather than a host-wide memory event. Measured 2026-09-23 with the fp32 embedder, the daemon held ~3.1–3.3 GiB anon at rest and up to 4.5 GB hours later, and the old `4g` default OOM-killed it under an ordinary request burst; bf16 takes ~1.4 GB off. `/health`'s `memory` block reports use against the cap. Raise for very large banks. |
| `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` | _(unset)_ | Overrides `embedding.cpu_dtype` (`auto` / `fp32` / `bf16`) — the torch embedder's precision on a CPU. Set `fp32` to roll a daemon back from bf16 without a rebuild; `/health`'s `embedder` block shows the resident dtype. |

For the Docker stack, set these in `ops/.env`
(`cp ops/.env.example ops/.env` — the install/update scripts scaffold it too;
every value is commented, a missing file runs entirely on defaults). The
dream-extractor variables (`PSEUDOLIFE_DREAM_*`) are covered in
[Dreaming](dreaming.md).

When deploying with `ops/update.ps1` or `ops/update.sh`, an explicit assignment
to either authentication variable in `ops/.env` makes that file authoritative
for both. Inherited client token variables cannot add an unintended fallback;
the launching shell's environment is preserved after the deployment command.

## Experimental agent coordination

Coordination adds peer awareness and addressed mail within one bank. It defaults
off and does not reserve files or prevent conflicting edits. Configure it in the
daemon's `config.yaml`, then restart the daemon:

```yaml
coordination:
  enabled: true
  awareness_limit: 5
  allowed_principals: [editor, reviewer]
  audit_retention_days: 90
```

`audit_retention_days` is how long the [audit log](#audit-log) keeps each event:
a whole number of days, default 90, and `0` keeps the log forever.

`awareness_limit` must be an integer from 1 to 20 and caps peer summaries. Existing
episodes have no trustworthy project/task or principal fields, so unregistered
peers are shown with unknown scope and host capability. Titles do not establish
identity. Last reported activity comes from attributed writes; an open episode
does not prove a process is running. Refresh awareness before shared-resource
work and on resume.

The allowed names are principals from the bearer-token configuration above;
the default list is empty. Mailbox operations require PostgreSQL, configured
bearer authentication and a registered adapter's private instance credential.
Two sessions sharing a principal still need distinct adapter identities. A
public agent ID, episode handle or task label never grants mailbox access.
Clients lacking a per-session credential-injecting adapter can use awareness,
but cannot send, receive or acknowledge another instance's mail. Awareness is
gated on the same allowed-principal list: a bearer whose principal is not listed
sees no peers and no awareness section in its briefing, and with no bearer token
configured there is no principal to list, so awareness stays empty on an open
loopback install.

Enable the installed shim adapter with `PSEUDOLIFE_AGENT_COORDINATION=1` and set
`PSEUDOLIFE_MCP_TOKEN` to that principal's bearer token. Optional
`PSEUDOLIFE_AGENT_LABEL`, `PSEUDOLIFE_AGENT_PROJECT` and `PSEUDOLIFE_AGENT_TASK`
provide explicit display and relevance fields. For clients other than Codex,
set `PSEUDOLIFE_AGENT_STATE` to a
private file outside the repository for deliberate mailbox resume. Each concurrent
adapter needs its own state file; sharing one does not create a second identity.
Claude Code sessions can instead set `PSEUDOLIFE_AGENT_STATE_DIR` to a private
directory: the shim keys one state file under it by the
`CLAUDE_CODE_SESSION_ID` Claude Code launches it with, so concurrent sessions
never share one and `claude --resume <id>` returns to the session's address.
That id is fixed for the shim's lifetime: `/clear` or an in-session `/resume`
keeps the running shim and its address. `claude --continue`, or `--resume`
without an id, may launch the shim with the process's startup id instead of
the resumed one, and the session then gets a new address. Without either, each launch
gets a new address, registered as not resumable and retired an hour after it
goes quiet. Never infer recovery from a
title, checkout directory or implicit host resume. Credentials stay in that
private file and adapter headers, not model arguments or memory entries.

The adapter also keeps a per-turn digest: the daemon's `attach` and
`heartbeat` answers preview the five oldest pending messages (sender label,
one-line excerpt) beside the pending count, and the adapter renders them once
behind a watermark that moves only when the text changes. With a host session
id — `CLAUDE_CODE_SESSION_ID` for Claude Code, the thread id for Codex — the
digest is written to `~/.pseudolife-mcp/digests/<sha256(id)>.txt`
(`PSEUDOLIFE_DIGEST_DIR` overrides the directory; set it identically for the
hook's environment, which cannot see the MCP env block; `PSEUDOLIFE_PLUGIN_DIR`
is the daemon-side counterpart, naming the plugin tree whose hook scripts
`/health` digests — the image sets it to its own copy). The plugin's
coordination UserPromptSubmit hook prints the digest only when the watermark passed the
shared `.seen` marker; the tool-result hint uses the same marker, so a change
is delivered once and a quiet turn adds nothing. While mail stays pending and
unchanged, a one-line reminder rides every tenth tool result. The file is
removed when the shim exits; a session id without an adapter (or a host that
exports none, such as the app-level MCP servers Claude Desktop launches from
`claude_desktop_config.json`) gets hints only. Desktop's Code tab runs Claude
Code, whose per-session stdio shim does receive the id.

Claude Code hooks see the current session id, which `/clear` and an
in-session `/resume` change, while the shim keeps the id it was launched with.
The plugin's session hooks therefore keep the shim's digest key once per
Claude Code process, in `claude-<CLAUDE_PID>.host` beside the digests, and
the prompt hook reads through that record while it is confirmed for the
current session. `CLAUDE_PID` is the Claude Code process id, which Claude
Code v2.1.214 and later export to hooks; older versions keep the per-session
key. The record passes to the next session only through a handoff that the
SessionEnd hook binds to the process's creation time. A record left by a
process that exited, even one whose PID was reused, is never followed, and a
host that cannot report a creation time falls back to the per-session key.
After `/clear`, compaction or a resume, the current digest prints once more. A `--continue` launch whose shim got the
startup id cannot be mapped, since no hook ever sees that id; such a session
gets hints only.

### Waking an idle session: `pseudolife-mcp wait-mail`

Mail reaches a recipient on its next Pseudolife tool call or prompt; nothing in
MCP can start a turn in a session that has gone idle, so the host has to.
`pseudolife-mcp wait-mail` gives the host something to wake on: it blocks until
the digest above shows mail nothing has shown yet, prints it and exits.

```sh
pseudolife-mcp wait-mail [--session-id ID | --digest PATH] [--timeout SECONDS] [--interval SECONDS]
```

It keys the coordination digest the way the shim does (`--session-id`, a Codex
thread id for instance, else `CLAUDE_CODE_SESSION_ID`; `PSEUDOLIFE_DIGEST_DIR`
applies); `--digest` names the file outright and accepts only a digest's own
`<64 hex digits>.txt` name. After `/clear` changes the session id, it follows a
`claude-<CLAUDE_PID>.host` record of the shim's spawn-time key for this Claude
process if one exists and its second line confirms it for the current session
(the SHA-256 of its id); without one, a waiter armed after `/clear` finds no
digest. It needs no daemon connection, token or
network. Each check is a file `stat` (every 2 s by default); the file is read
only after the adapter rewrites it. It fires when the watermark is past the
shared `.seen` marker and the digest lists pending mail, so mail that arrived
while the agent was busy fires at once and mail a prompt hook or tool-result
hint already showed does not. On firing it prints the digest body verbatim on
stdout, agent-origin framing included, then advances `.seen` so the hook and
hint do not repeat it, and appends a `wait` line to `ledger.log`. Exit codes:
`0` new mail; `3` timeout (default 4 h, at most 24 h), re-arm; `2` nothing to
wait on — no session id, no digest file (the adapter writes it when it
attaches: at shim start in Claude Code, on a thread's first `memory_*` call in
Codex), a file that disappeared because the shim exited, a file that cannot be
inspected when armed (a later read error is retried), a
stdout that cannot take the mail (left unmarked), or a bad argument.
Diagnostics go to stderr. The adapter refreshes the digest on its 20 s
heartbeat, so a waiter fires up to about 22 s after the send. It also rewrites
an unchanged digest every hour, without moving the watermark, so the day-old
sweep another adapter runs at start never takes a long-idle session's file.

In Claude Code, the agent arms it with the Bash or PowerShell tool and
`run_in_background: true`. Claude Code reports the exit as a task notification,
which starts a turn even in an idle session (observed on Claude Code 2.1.280,
2026-09-23):

1. After registering with `memory_agents`, arm one waiter; keep exactly one
   armed.
2. On exit `0`: `memory_message` receive, act, acknowledge each `message_id`,
   then re-arm. On `3`: re-arm. On `2`: read the stderr line, fix what it
   names (in Codex, make one `memory_*` call first) and re-arm once; if it
   persists, continue pull-only.
3. Before ending a turn that waits on a peer, make sure a waiter is armed.

Arm it from the main conversation: a command started by a foreground subagent
ends with that subagent's final response, and `-p` runs end background commands
shortly after their final result. When `pseudolife-mcp` is not on the shell's
`PATH`, call the shim's own executable or `python -m pseudolife_memory.cli
wait-mail` under the interpreter the shim runs on. In Claude Code's auto mode a
classifier reviews each such command, and in one 2026-09-23 session it refused
a long-running waiter script from the home directory as persistence (it allowed
the same script in another). The recommended setup is a narrow allow rule,
`Bash(pseudolife-mcp wait-mail *)` (and `PowerShell(pseudolife-mcp wait-mail *)`
on Windows), which also matches the bare command: auto mode resolves narrow
shell rules before the classifier runs, while it drops broad ones such as
`Bash(python*)` and every rule naming the Monitor tool, so arm the waiter as a
background Bash or PowerShell command, not a Monitor. Setting
`autoMode.classifyAllShell` suspends even narrow rules. Codex never starts a turn
when a background command exits, so run it there only in the foreground: a
background run would advance `.seen` with nobody reading its output.
Acknowledging some messages while a waiter is armed, and leaving others pending,
rewrites the digest and fires once, the same way the tool-result hint
re-delivers a changed digest.

### Codex CLI and desktop

Use the ordinary stdio shim with `PSEUDOLIFE_WRITER_ID=codex`. Codex supplies
`_meta.threadId` on MCP tool calls; the shim uses this validated task UUID for
attribution and lazily attaches the task's mailbox on its first call. CLI and
desktop runtime probes confirmed that this metadata survives resume and changes
on fork. Process environment variables, checkout names and titles do not select
the mailbox. Missing or malformed metadata leaves ordinary memory available
without attaching a mailbox.

For an existing Codex stdio registration, run the following from a checkout using
the Python environment where Pseudolife is installed:

```sh
python ops/setup-codex-coordination.py --credentials
python ops/setup-codex-coordination.py --check
python ops/setup-codex-coordination.py --enable
```

The check is read-only. Enable requires a configured bearer and an enabled daemon
that allows its principal; it does not change the daemon's authentication or
allowlist. The token must be in the MCP registration's `env`, or explicitly
forwarded through `env_vars`. Setup preserves the command and unrelated settings,
backs up the configuration privately, and uses Codex's versioned configuration
writer. Reconnect the MCP server after changing its environment.

For authenticated connections, the normal installer prepares a private
credential file and connects both the stdio shim and lifecycle hooks to it.
Tokenless installations save their intended endpoint and explicit no-auth state,
so unrelated credentials in the app environment cannot select another bank.
For an existing installation,
`--credentials` performs that setup from the configured bearer. A non-secret
`pseudolife/connection.json` beneath the selected Codex home records the daemon
URL and token-file path for hooks, which do not inherit the MCP server's
environment. The setup refuses conflicting endpoints and follows no redirects.
Upgrading an already running shim requires one reconnect to load the new code
and file setting. Subsequent token-file replacements are read automatically;
the replacement token must resolve to the same bank and principal.

Setup pins `PSEUDOLIFE_AGENT_STATE_DIR` under the selected Codex home's
`pseudolife/agents` directory unless an explicit directory is already configured.
Files are scoped by bank URL and task ID, with credentials stored privately.
Existing state must be a private regular file owned by the current user;
the adapter preserves and rejects unsafe state rather than changing its
permissions and trusting potentially modified contents.
Each file pins the authenticated bank identity and principal, so bearer rotation
preserves the mailbox while a different bank or principal is refused. The first
upgrade can adopt the exact legacy file for the currently configured bearer
after the daemon proves possession of that mailbox's credential hash. The old
file is retained. Files belonging to previously retired bearers are not searched
or merged automatically. Do not set `PSEUDOLIFE_AGENT_STATE`
to one shared file in Codex: automatic attachment refuses that configuration.
`--disable` stops registration on subsequent connections and preserves saved mail.

Transport failures report a sanitized category, operation phase and whether the
operation outcome is known. The shim never automatically replays a tool call:
a write may have committed even when its response was lost or returned a service
error. Retry reads normally; verify uncertain writes before sending another one,
and reuse the original request ID when retrying an addressed message.

An active task should use `memory_agents` before shared-resource work and
`memory_message(action="receive")` on resume and when the coordination digest
(in the prompt hook or a tool result) shows pending mail. Receive does not
acknowledge; use `action="ack"` after reading, with one `message_id` or
several comma-separated (at most 50; a JSON array of strings, the form a
host that stringifies list parameters sends, is read as that list): a batch
returns the receipts in the order given and lists the ids that were not this
mailbox's, instead of failing whole.
These calls work in the CLI and desktop without live wake support.
Setup leaves Codex tool approvals unchanged. A recipient running with approval
policy `never` cannot execute a tool that still requires approval. To authorize
unattended mailbox operations specifically, configure the installed server's
`tools.memory_message.approval_mode = "approve"` in Codex. This permits that
tool's send, receive and acknowledgment actions; it does not approve file writes,
commands or other tools. Without that choice, use the host's normal approval flow.

### Optional Codex live delivery

Codex's documented app-server API can accept tool output into an idle or busy
task. This integration is experimental: OpenAI also labels its WebSocket transport
experimental and unsupported. Install the optional dependency in the shim's exact
runtime with `python -m pip install 'pseudolife-mcp[codex]'`, or install the `codex`
extra from the checkout when testing unreleased changes.

The owner must expose an authenticated loopback WebSocket app-server and keep its
client connected. Configure that server's `--ws-auth capability-token` and
`--ws-token-file` options, then connect its CLI with `codex --remote` using the
same endpoint and credential. Follow the installed Codex CLI's help for client
authentication options. In that server's Pseudolife MCP environment, set:

```toml
PSEUDOLIFE_AGENT_WAKE = "1"
PSEUDOLIFE_CODEX_SERVER_URL = "ws://127.0.0.1:4500"
PSEUDOLIFE_CODEX_SERVER_TOKEN = "<local-server-bearer>"
```

The local-server bearer is separate from `PSEUDOLIFE_MCP_TOKEN`, which authenticates
to the bank. Keep both out of repositories and model prompts. The bridge accepts
only literal loopback endpoints with an explicit port and bearer authentication.
It verifies that the metadata-selected recipient is already loaded on that exact
server, then submits `turn/start` with empty user input and `toolOutput`. Peer
content remains tool output; no approval policy, model, sandbox or user-authority
override is supplied. Only the recipient's explicit acknowledgment marks receipt.

An installed desktop app using a private stdio app-server has no corresponding
external WebSocket endpoint. The bridge cannot attach to that connection and does
not start another server or resume the task elsewhere. Use pull messaging there.
Native app messaging tools and hooks do not establish a generic external wake API.
The desktop app-server binary was exercised separately; this does not establish
live delivery into the installed desktop UI.

See the [Codex validation record](../specs/2026-09-12-codex-coordination.md) and
[OpenAI's app-server contract](https://learn.chatgpt.com/docs/app-server).

### Optional Codex doorbell

Codex starts no turn for MCP notifications, hooks or finished background
commands, so without the bridge a Codex task sees new mail only at its next
Pseudolife call. The optional doorbell wakes an idle task, desktop app included,
through Codex's own `codex queue` command. That command persists a message which
every app-server sharing the Codex home dispatches to the task once it is loaded
and idle; app-servers poll for it about every 10 seconds. Enable it in the
Pseudolife MCP server's environment, next to `PSEUDOLIFE_AGENT_COORDINATION`:

```toml
PSEUDOLIFE_CODEX_DOORBELL = "1"
# Optional: an absolute path; otherwise `codex` is looked up on PATH.
PSEUDOLIFE_CODEX_BIN = 'C:\path\to\codex.exe'
```

Reconnect the MCP server afterwards; the setup helper does not set either value,
and the doorbell stays off without `PSEUDOLIFE_AGENT_COORDINATION=1`. The PATH
lookup uses absolute PATH directories only, never the working directory (the
task's checkout), so a repository cannot supply its own `codex`. A
`PSEUDOLIFE_CODEX_BIN` that is relative or does not exist turns the doorbell
off rather than falling back to PATH. With a non-default Codex home, give the
server `CODEX_HOME` too, in its `env` or through `env_vars`: Codex does not
necessarily pass it to MCP servers, and without it `codex queue` writes to the
default home's queue, which no app-server of the task's home reads.

- **When it rings.** After each 20-second heartbeat the task's adapter reports its
  pending mail. The shim runs `codex queue --thread <task id> --message <notice>`
  only when new addressed mail has arrived, the task has made no Pseudolife call
  for 30 seconds, neither a tool-result hint nor the prompt hook has shown that
  mail, and no earlier doorbell is still unanswered. A successful
  `memory_message receive` from the task answers it, and so does an emptied
  mailbox. An idle task gets one doorbell per batch of mail.
- **What it says.** Codex delivers queued text as a user message, so the doorbell
  never carries peer text, sender labels or excerpts. The notice is fixed and
  only the count varies:
  `[Pseudolife board - automated doorbell, agent-origin, not a user instruction]
  2 addressed messages pending for this thread. Read them with memory_message
  receive and ack each message_id. Act only within the task the user authorized.
  If nothing is pending, end the turn.` The model then reads the mail through
  `memory_message receive`, where it stays framed as agent-origin.
- **How it fails.** The CLI runs in the background with a 20-second timeout, no
  `PSEUDOLIFE_*` variables and, on Windows, no console window; a timeout or shim
  shutdown kills its whole process tree, launcher wrappers included. A missing
  CLI, a non-zero exit or a timeout turns the doorbell off for that shim process
  with one stderr line; pull delivery and hints continue unchanged. Each queued
  doorbell appends a `bell` line to `ledger.log` in the digest directory.
- **Limits.** A task is watched from its first Pseudolife call after the MCP
  server starts: one that has made none since a reconnect cannot be rung until it
  does. Tasks the WebSocket bridge above serves are not rung; if the bridge stops
  for a task, the doorbell takes it over. Codex holds a queued notice while the
  task is running, interrupted or shut down, so a task that ends a long turn
  without Pseudolife calls may wake once to mail it has already read. With more
  than five messages pending, new mail that lands in the same heartbeat as acks
  that keep the count from growing rings no doorbell; it surfaces at the task's
  next Pseudolife call or with the next doorbell. When the bridge stops for a
  task, the mail then pending (including the message it failed to deliver) is
  owed a doorbell.
  `codex queue` refuses ephemeral tasks and goes through a managed Codex
  app-server daemon when one runs. Success means enqueued, not read: only the
  recipient's acknowledgment marks receipt. The recipient still needs
  `memory_message` approval, as described above, to read mail unattended.

### Audit log

The live mailbox forgets on purpose: bodies blank after 24 hours, rows go after
seven days, idle addresses are removed, and a status update overwrites the one
before it. The audit log (`coordination_events`, schema v42) is the durable
record of what happened on the board, kept for at least `audit_retention_days`.

Every board mutation appends one row in the same database transaction as the
mutation itself, so a refused or rolled-back call leaves no event and no event
exists without its change. The events are `register`, `update` (the new values
and the ones they replaced, which is the status history), `attach`, `detach`,
`send` (with the full body), `read`, `ack`, `attempt`, the prune pass's
`expire` (bodies blanked) and `prune` (messages and addresses removed),
`bank_identity`, and the operator's restore `recover` and `rebind`. A `read`
records the first time a receive returned the message: an explicit receive
(`path: pull`), or the recipient's live-delivery adapter fetching it for a wake
attempt (`path: delivery`). The same time is stamped on the message as
`first_read_at`. The per-turn digest's 100-character preview is not a read.
Lease heartbeats are not logged: at the shim's 20-second cadence one session
would add about 4,300 rows a day, and `attach`/`detach` already bracket each
lease.

Each row carries a dense sequence number `seq`, the event, its actor (`agent`,
`daemon` or `operator`), the bearer principal the daemon verified for agent
actions (never a credential), the agent and recipient IDs, project and task,
the message ID, a JSON payload, `created_at`, the message's HLC stamp on `send`
(other events are ordered by `seq`: stamping every mutation would need the full
service initialization that mailbox calls deliberately avoid), and two hashes.
`hash` is sha256 of the previous row's hash followed by the row's canonical
content, so editing, inserting or reordering rows breaks the chain, and so does
removing any but the oldest (see below). Appends take a transaction-scoped
advisory lock after every board-row lock the mutation holds, which orders
writers on separate connections.

Retention is separate from the mailbox. The prune pass that expires bodies also
removes the log's oldest rows once they are older than `audit_retention_days`.
It cuts on UTC day boundaries and removes only a fully expired prefix, so an
event stays at least the window. Normally it stays at most a day longer;
out-of-order timestamps can retain older rows behind a newer row until that
row also expires. The cut is always a prefix, recorded
as an `audit_prune` event naming the last removed row, and the surviving chain
starts from that anchor. `0` never prunes. The pass runs at most once a minute
and only while the board is in use (registration, sending or heartbeats on an
enabled board): a board that goes quiet, or has coordination disabled, keeps its
log, bodies included, until activity resumes.

A synthetic replay at the scale of the 2026-09-23/24 fifteen-session trial (40
agents and 623 messages, plus 15 status updates and 2 attachments per agent,
which the trial's export does not record) left 2,671 events in 1.6 MB including
indexes: 144.5 MB if every one of 90 nights were that busy
([artifact](../../evals/results/coordination-audit-volume-20260924.json)). In the
same run the append added 1.3 to 2.2 ms to the median send, receive and
acknowledgment on a local server, against a control arm with it disabled whose
own two runs differed by up to 0.6 ms. Status-update latency was too noisy there
to read (its two control runs were 1.8 ms apart), and with one writer at a time
the run did not measure waiting on the append lock.

The log is read by an operator, never by an agent: there is no MCP tool and no
REST route for it. `pseudolife-mcp board-audit` reads the bank directly, through
`PSEUDOLIFE_MCP_DATABASE_URL` or the lite tier's embedded instance, in a
read-only snapshot, so it is safe beside a running daemon:

```sh
pseudolife-mcp board-audit export --task fix-week --since 2026-09-23 --out board.jsonl
pseudolife-mcp board-audit verify
```

`export` writes one JSON object per line, oldest first, to stdout or to a new
`--out` file, which it never overwrites. Prefer `--out` for anything you keep: a
PowerShell 5 `>` redirect writes UTF-16. The filters are `--project`, `--task`,
`--agent` (the acting agent or a message's recipient), and `--since` / `--until`
(epoch seconds or ISO 8601; a time without an offset is local). The daemon's
`expire`, `prune` and `audit_prune` rows and the operator's `recover` carry no
project or task and name agents only in their payload, so a filtered export
leaves them out.

`verify` walks the chain and prints one JSON report: `ok`, the number of
`events`, `first_seq`, the head (`head_seq`, `head_hash`, `head_created_at`),
and `start_cut`, the cut the log starts from once retention has removed its
oldest rows. It exits 0 when the chain is intact. It exits 1 with the first
failing `seq` and a `reason`: `sequence_gap`, `broken_link`, `hash_mismatch` or
`unanchored_start` for the chain, or `head_missing`, `head_mismatch` or
`head_pruned` for an expected head. It exits 2 when it could not check.
`verify --input <file>` checks an export file instead of the bank; the export
must be unfiltered, since a filtered one has gaps. In the Docker tier run it
inside the daemon container, which already has the database URL:
`docker exec pseudolife-mcp-daemon pseudolife-mcp board-audit verify`.

What `verify` shows: no row was edited, inserted or reordered, and none was
removed except the oldest, behind a cut record whose own fields add up (written
by the daemon, a window of at least a day, the cutoff that window gives at its
time, and no surviving row older than that cutoff). What it cannot show on its
own, because no secret is involved: that the newest rows were not dropped; that
the table was not rewritten with every hash recomputed; and that the oldest
rows were not removed by someone who also appended a consistent cut record.
Record `head_seq:head_hash` and `head_created_at` from each `verify` somewhere
outside the bank, and later run `verify --expect-head SEQ:HASH`. That catches
the first two. The third needs a series of recorded heads: retention never
removes a row created at or after its cutoff, so a recorded head that comes
back `head_pruned` although its `head_created_at` is at or after
`start_cut.cutoff` means rows went that retention would have kept (unless the
daemon's clock stepped backwards, or the window was raised since). A forged
cut stamped with the current time and your configured window passes
everything else. For history you must be able to prove, keep periodic `--out`
exports (privately) and check them with `verify --input`. The log records mutations made through
the coordination store; a direct SQL edit of the mailbox tables leaves no event.
It proves what was sent and by which verified principal, not that a message was
true.

The log is private data. It holds message bodies verbatim for the whole
retention window, and bodies carry machine paths and usernames. It lives only
in the bank database and its full backups, portable `export`/`import` archives
omit it, and the CLI writes only to stdout or a local file you name. Keep
exports out of repositories and anywhere public.

### Delivery and recovery

Use ordinary `pseudolife-mcp` for authenticated pull messaging. The optional
`pseudolife-mcp channel` mode also requires `PSEUDOLIFE_AGENT_WAKE=1` to emit live
events, plus the host's preview launch opt-in. The cached coordination digest
can appear in tool responses (once per change, then a one-line reminder every
tenth call); attaching it adds no network request to the tool path and never
acknowledges mail. Optional adapter startup requests cancellation after three seconds, then waits
for bounded in-flight request cleanup before falling back to ordinary memory
service. This is not a three-second ceiling on total shim startup time.

Messages have one recipient. Sending confirms durable enqueue; receiving does
not acknowledge. The recipient explicitly acknowledges a message ID, and an
acknowledgment does not mean the requested work is complete. Retries reuse the
same sender request key and content. Coordination traffic is not added to bands,
cortex, graph, retrieval or dream input by the messaging APIs. Store a useful
decision explicitly as ordinary memory if it should become durable knowledge.

Initial limits are 8192 UTF-8 bytes per message, 256 pending messages per recipient,
60 new sends per sender per minute and 50 messages per receive page. Bodies stop
being served after 24 hours; request-key metadata is retained for seven days.
The [audit log](#audit-log) keeps its own copy of every body for
`audit_retention_days`.
Opportunistic pruning runs at most once per minute during registration, sending
or heartbeats. Expired bodies remain unservable even when no adapter is running
to trigger physical cleanup. Full queues and rate limits return explicit errors.
Live delivery attempts a message at most three times in total across
attachments; past that it is left for explicit receive, so one unacknowledged
message cannot wake the host on every restart. The same prune pass removes an
address that holds no lease, is referenced by no retained message and has been
idle for seven days, or for one hour when its adapter registered without a
state file (`capabilities.resumable: false`), since nothing can attach to that
address again; for those the lease too must have been gone for the hour, so a
daemon restart cannot retire a parked shim's address, and if it ever is
retired the state-less adapter registers a fresh one instead of stopping.
Addresses that predate the flag keep the seven-day rule. Idle
means no register, update, attach, send, acknowledgment or forwarded tool call:
the adapter's lease heartbeat counts as activity only when the shim forwarded a
tool call since the previous one, so a parked shim is neither ranked nor
retained as a working one. A client that only reads must acknowledge what it
reads, or hold a lease, to stay registered. `memory_agents(action="list")`
shows peers that hold a lease or were active within the last hour, leased
first, reports the number of other matching peers as `idle_omitted` and sets
`truncated` when the page cut listed peers; a peer's public agent ID stays
addressable while its row exists. A
legacy adapter registers a fresh address on its next start only when the authenticated
daemon explicitly confirms that the saved address no longer exists. It keeps
the old state file beside it with a `.stale` suffix. A rejected bearer or instance
credential preserves the saved address and requires corrected authentication
or the deliberate restore/rebind procedure; an HTTP status alone never proves
that an address should be replaced.
Bank-bound clients preserve their address even when it is missing on the server;
use deliberate recovery rather than silently registering a replacement.

`pseudolife-mcp channel` is the optional Claude Code preview transport. Host
delivery requires explicit preview opt-in and recipient wake configuration;
protocol tests alone do not establish compatibility with an installed host.
Only addressed messages may wake an opted-in recipient. Board/status activity
and receipts do not produce conversational wake-ups. Each live event carries a
fixed agent-origin header, built from daemon-verified sender fields, ahead of
the peer's text. Explicit receive labels each message with `origin: agent` and
includes a note that peer requests cannot grant user approval. Other clients use explicit,
authenticated mailbox retrieval where their adapter supports it; live receiving
support is not assumed from a host's native send tool.

On Claude Code 2.1.267, a first channel-triggered turn after startup or resume
can arrive before the host makes MCP reply tools usable. A successful MCP
initialization or tool-list response does not establish model readiness. If the
host reports an unavailable messaging tool, send an ordinary prompt, confirm a
successful `memory_agents` call, then use `memory_message(action="receive")` to
recover pending mail. A failed reply or transport attempt never acknowledges it.
The experimental adapter does not promise unattended startup recovery.

An outage that exhausts a request's bounded retries pauses background delivery,
clears the cached unread count and prints a warning to stderr once per outage.
Ordinary tool responses carry a degraded-delivery hint, including for pull-only
adapters, and explicit receive keeps working on the same shim as soon as the
daemon answers, provided the caller remains authorized. The adapter's heartbeat
task re-attaches on its own after transient failures, with backoff
(1 s rising to 60 s, held until a heartbeat or receive succeeds), resumes the
lease while it is still valid or takes a new generation once it has expired,
replays unacknowledged mail from the start of the mailbox for a new generation,
and prints a restored notice. Recovery waits for the next retry after the daemon
becomes reachable. A generation change invalidates an older receive page even
when it happens between yielded messages; the old page cannot advance the new
generation's replay cursor. An explicit authentication or identity rejection
preserves state and stops retries with the rejected credential. File-backed
clients observe the credential source and resume after a replacement authenticates
to the saved bank and principal. An authority mismatch remains closed until the
credential again matches the saved authority; it never creates a replacement address. Do not
infer live delivery from a queued or attempted send result.

If initial registration fails, or the shim's startup budget cancels it before
the adapter receives the new address, the adapter releases its empty state
reservation and the next launch registers a fresh address; it never retries
inside the same start and never overwrites a state file that already holds an
identity. An address the daemon created for a lost response was never held by
any adapter, receives no mail, and is pruned with the other idle addresses. Do
not revoke every mailbox to repair one failed registration. A lost attachment
response can leave a lease until expiry; failed competing attachment attempts
do not renew it.

A crash-left empty state file becomes eligible for takeover after one minute.
Takeover uses an owner-only sibling `.lock` file and a nonblocking operating-system
lock, so concurrent launches cannot both register against that stale reservation.
The lock file remains on disk; lock ownership is released when the process exits,
including a crash. Do not delete it while an adapter might be using it.

Full database backups contain coordination mail and the audit log. Portable `export`/`import`
archives omit the coordination tables (agents, mail and the audit log) and their clock metadata so moving
knowledge cannot clone live mailboxes or instance credentials. Follow the
[offline mailbox recovery procedure](coordination-recovery.md) after a database
restore. See the [experimental design](../specs/2026-09-11-agent-coordination-design.md)
for delivery-state and host-verification contracts.

### Waking an idle Claude Code session: the Stop hook

Mail otherwise reaches a Claude Code session only at its next memory call or
prompt, so an idle session can sit on a message for hours. The plugin ships an
opt-in `Stop` hook that waits on the session's digest after every turn and
wakes the session when new mail arrives. It is off unless the hook's
environment sets `PSEUDOLIFE_AGENT_WAKE_HOOK=1` (for example in the `env` block
of `~/.claude/settings.json`, which Claude Code passes to the processes it
starts); the hook's command checks the flag before bash reads the script. It
needs the coordination adapter above, since it waits on the digest file the
adapter writes: without a digest directory it exits at once. It also needs a
Claude Code release that honours `asyncRewake` (verified on 2.1.280); one
that ignored `async` would run it in the foreground and hold each turn end.

Opting in lets any peer allowed to mail this session start a model turn in it
while you are away, in whatever permission mode the session runs; peer text
still cannot grant approval. Every wake spends tokens, and two opted-in
sessions can keep waking each other, so wakes are capped (below).

- The hook runs with `"async": true` and `"asyncRewake": true`: in the
  background after each turn, and exit code 2 starts a new turn even when the
  session is idle. Verified in the Desktop Code tab on Claude Code 2.1.280, in
  auto permission mode (under a second from exit to the new turn). Claude Code
  labels the delivery "Stop hook blocking error"; that label is the wake, not
  a failure. The reminder is one line saying so, then the digest, which reads
  as agent-origin, not user authority.
- It fires when the digest's watermark is past the `.seen` marker and the
  digest lists mail. Mail that arrived during the turn fires at once; a digest
  the session already saw (through the prompt hook, the tool-result hint or an
  earlier wake) does not fire again at the next turn end. Firing advances
  `.seen` and appends a `wait` line to `ledger.log`; if the marker cannot be
  written, the hook does not wake at all. SessionStart clears `.seen` on
  resume and compact, and any change to the digest while mail is pending
  (acknowledging some of it, a message expiring) is a new digest, so either
  can wake the session once more.
- At most 20 wakes per session in any hour. Mail over the cap waits for the
  window to free up; it is delayed, not dropped.
- One watcher per session: each turn end takes the lease in `<key>.wake`, and
  the previous watcher exits within one poll (5 s). A digest file absent when
  the watch starts is waited for; one that vanishes during it (the shim
  exited) ends the watch. After `/clear` it reads the
  digest named by the per-process `claude-<pid>.host` record, when
  SessionStart has written one.
- A watcher waits at most 3540 s after the turn that armed it; the hook's
  `timeout` is 3600 s, which Claude Code enforces on `asyncRewake` hooks.
  `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` (seconds) shortens it. A session idle for
  longer is not woken; its mail still appears on its next prompt. On Linux and
  macOS the watcher also stops when Claude Code exits. In `claude -p` runs,
  Claude Code ends a waiting hook at teardown.
- Codex loads the same `hooks.json`. The `Stop` entry is a no-op there: the
  native command (`lifecycle.ps1 -Event Stop`) exits at once; the bash
  command stops at the flag check, and the script exits unless Claude Code
  started it.
  `ops/setup-codex-hooks.py` approves it with the other three definitions
  (see [Codex specifics](providers.md#codex-specifics)).

## Startup memory policy (`memory_policy`)

Which standing memory policy the session-start hooks serve. The default is
the short core the memory hook has served since 2026-09-24; the other
variants exist so their effect on agent behaviour can be measured
(`evals/memory_policy_bench.py`) rather than argued.

```yaml
memory_policy:
  variant: compact          # none | compact | compact_gaps | full_separate_hook
  ab_arms: []               # e.g. [compact, compact_gaps] for an online A/B test
```

| Variant | What session start serves |
|---|---|
| `none` | No policy text. The episode line and the briefing still serve; the cold-bank onboarding block, which names memory tools too, does not. |
| `compact` (default) | The short core, ahead of the briefing, in the memory hook's output. |
| `compact_gaps` | The core plus three rules the tool descriptions do not carry: recall before stating a current version, number or benchmark; route verified external facts to `memory_world_set`; correct memory-vs-code drift on the spot. |
| `full_separate_hook` | The full memory-loop block ([`examples/CLAUDE.memory.md`](../../examples/CLAUDE.memory.md), 7.5 KB), served by a separate SessionStart output (`GET /api/hook/memory-policy`), because the block plus the briefing exceed the 9,500-byte budget of one hook output. |

The separate output is the plugin's third SessionStart handler
(`session-start.sh memory-policy`, or `lifecycle.ps1 -Event MemoryPolicy` in
Codex on Windows); `ops/setup-codex-hooks.py` installs and approves it for
manual Codex hooks too. For every other variant it answers an empty body and
adds nothing. The `install-hook` scripts' settings hooks do not carry it, so
`full_separate_hook` serves no policy to those installs.

`ab_arms` assigns each session the SessionStart hook registers one arm, by a
SHA-256 of its client session id modulo the arm count; a variant may repeat
for an A/A arm. Sessions that reach the hook without a session id keep
`variant`. The daemon logs each assignment. The bank alone cannot
reconstruct them all: a session root that ends with no stored entry is
deleted, so an online comparison read from the bank sees only the sessions
that stored something (see `evals/capture_metrics.py`). A durable
per-session record is a follow-up that needs a schema change. A custom
`hook-instructions.md` is served in every variant.

## Built-in defaults (tuned for Claude's use case)

- **Embedding backbone `Qwen/Qwen3-Embedding-0.6B`** (`EmbeddingConfig.model_name`,
  default since schema v25) — torch on the CPU, no GPU sidecar, in the
  precision `EmbeddingConfig.cpu_dtype` picks: `auto` (the default) loads
  the model straight into bf16 when the CPU has native bf16 (x86
  AVX512_BF16 / AMX_BF16) and uses fp32 otherwise, since bf16 without
  native support is slow; `fp32` / `bf16` force one. Measured 2026-09-23 on
  the production image, bf16 held ~1.4 GB steady vs ~2.85 GB for fp32, and
  on 400 real bank entries bf16 queries against stored fp32 vectors kept
  top-8 overlap 0.994 and rank-0 60/60, with the regression gate scoring
  every arm identically to its fp32 baseline
  (`evals/results/embedder-cpu-bf16-probe-20260923.json`). The default
  applies everywhere the embedder runs, evals included: an eval on a
  native-bf16 CPU embeds in bf16. Vectors are stored as float32
  either way. `PSEUDOLIFE_EMBEDDING_CPU_DTYPE` overrides the config value;
  `/health` reports the resident `embedder.dtype`. It's
  instruction-asymmetric: query-side text (search/recall probes) is encoded
  with `EmbeddingConfig.query_prefix`'s instruction prefix via
  `encode_query()`; everything stored (entries, fact/world/lesson claim
  text, slot and entity-name embeddings) is encoded bare via `encode()` /
  `encode_single()`. `query_prefix` defaults to the Qwen3-Embedding card's
  exact instruction string — set it to `""` to restore symmetric behavior
  for a model (like the previous default, `all-MiniLM-L6-v2`) that doesn't
  distinguish query/document sides. `max_seq_length` caps the tokenizer at
  512 tokens (a min-with-model-default cap, never a raise) regardless of
  the model's native context window. See
  [asymmetric query/document encoding](retrieval.md#asymmetric-query-and-document-encoding)
  for what this changes about retrieval, and the
  [schema version history](#schema-version-history) below for the v25
  cutover itself.
- **ONNX acceleration is load-only** (`EmbeddingConfig.backend = "onnx"`).
  The MCP defaults select it only when the optional ONNX stack is installed
  *and* the loader would load it: the configured model's artifact already
  resolves locally, and, on native Windows, the model's Transformer module
  does not load from a nested subfolder (see the end of this item).
  Otherwise they choose torch up front and log one INFO line saying why. One
  deliberate exception: an `onnx_file_name` that fails validation (for
  example, one that leaves the model directory) keeps ONNX selected, so the
  loader's warning names the bad setting instead of hiding it. The
  default Qwen3-Embedding-0.6B ships no ONNX artifact, so the daemon runs it
  on torch; MiniLM, whose artifact the daemon image bakes, still gets ONNX.
  An explicit `embedding.backend` is never overridden: `backend: onnx`
  without a loadable artifact still warns and falls back to torch at load.
  `EmbeddingConfig.onnx_file_name` defaults to
  `onnx/model.onnx`; that exact artifact must already exist in a local model
  directory or a revision-specific cached Hub snapshot. A missing artifact falls
  back to torch before SentenceTransformers constructs its ONNX backend, in
  online and offline processes alike. The daemon never downloads ONNX artifacts
  at runtime. The daemon image provisions MiniLM's while building
  (`ops/provision_embedding_models.py` is the reference for how); a pip install
  stays on torch unless the operator puts `onnx/model.onnx`, or whatever
  `onnx_file_name` names, into the local model directory or the cached Hub
  snapshot. Each supported Transformer module must have the artifact in its own
  configured subdirectory; a root-level file does not cover a missing module
  artifact.
  Standard Transformer, Pooling, Normalize and Dense module layouts are
  recognized; unknown module classes use torch. Filenames must end in lowercase
  `.onnx` so validation matches the loader on case-sensitive filesystems.
  No validated ONNX artifact path or `modules.json` may traverse a link between
  the model directory and the file, because the loader's discovery glob does not
  descend into linked directories — a link that stays inside the model directory
  still hides the artifact and re-enables export. The one accepted link is a Hub
  snapshot's leaf link, and only after local-only Hub cache resolution and only
  when it targets that cached repository's own `blobs` directory. With the
  pinned Optimum stack, native Windows does not detect an artifact under a
  nested module subfolder such as `0_Transformer/onnx` and enables export, so a
  model whose Transformer module loads from a subfolder falls back to torch
  there before ONNX construction. A flat `onnx` subfolder resolves on both
  platforms, and the load-only ONNX path remains available in Linux and the
  daemon image.
- **Surprise threshold `0.0`** — the v0.5 store gate measures *novelty*
  (`1 − max cos` to existing entries). Claude stores deliberately, so the
  gate stays permissive (store everything; novelty still drives
  eviction scoring at capacity). Raise it above zero to dedup
  near-duplicate stores. Detected potential conflicts bypass this gate so
  low-novelty updates can land; this does not retire an earlier source note.
- **Meta-filter off** (`memory.meta_filter.enabled = false` in the MCP
  build) — the filter exists to drop auto-captured chat noise ("I don't
  have anything saved about that"); every MCP store is a deliberate tool
  call, and the filter's patterns collided with legitimate dev facts
  about memory systems themselves.
- **Recency base half-life 24h** (`memory.recency_base_half_life_s =
  86400`, vs the 1h chat default) — Claude Code sessions are hours-to-
  days apart; with a 1h half-life the recency boost was effectively
  always zero. This knob is **doubly dormant under the flat default**:
  the depth ramp it feeds has been off since 2026-07-25 AND the ramp is
  structurally inert with one band; it only bites on a multi-band preset
  with `recency_boost_enabled = true`.
- **MIRAS preset `flat`** (default since 2026-08-15) — one band named
  `flat` at capacity 5,250 (the previous continuum's summed total), with
  a `balanced` retention policy. Eviction is a retention-scored **true
  drop** that only fires at genuine capacity: it permanently deletes the
  entry's row (superseded entries go first), is counted
  (`memory_stats().true_drops` since start; `true_drops_total` and
  `last_true_drop` all-time, kept in the `meta` table on Postgres) and
  logged as a WARNING naming the entry — a bank under real pressure is
  visible, never silent. This is the arm the preregistered flat-band
  verdict measured as tying the 8-band continuum on every gate (ranking,
  forced-eviction retention quality, real recorded queries — see the
  [benchmarks page](benchmarks.md#band-structure)), so the simpler
  structure ships. The **`continuum` preset is retained** as the one-line
  rollback: the 8-tier `working … forever` layout with promotion
  thresholds, per-tier retention policies, and the 2026-07-25 demotion
  cascade (a full band demotes into the next; only overflow past
  `forever` drops).
  **Changing the preset in either direction is safe**: hydration reseats
  every row across the new band layout in one pass (rows whose old band
  name is gone land in the first band) and **reconciles the stored band
  stamps** to the new layout, idempotently. If the bank holds more rows
  than the new preset seats, the deepest band is left over capacity and
  the count logged rather than truncated at startup — normal eviction
  drains it from there.
  **Before the first delete**: from 80% of the last band's capacity (the
  only band whose evictions are true drops) `memory_stats()` carries a
  `capacity_warning` and `/health` a `capacity_warning: true` flag. **To
  raise the cap**, switch to a custom preset that keeps the band name
  `flat` (so hydration leaves every row's band stamp alone), merged under
  the existing top-level `memory:` key of `config.yaml` — never a second
  `memory:` key — then restart the daemon:

  ```yaml
  memory:
    miras:
      preset: custom
      bands:
        - name: flat
          max_entries: 10000   # size to the daemon's RAM and latency budget
          update_interval: 1000000000
          promotion_access_count: 1000000000
          promotion_surprise: 1.1
          retention_policy: balanced
  ```

  Every resident entry costs daemon RAM (a correction briefly holds a
  second copy of the bank), and search latency grows with the bank, since
  the BM25 pool is rebuilt over every entry per query.
- **No NLI scorer.** The `cross-encoder/nli-deberta-v3-xsmall`
  contradiction model (~278 MB) is an unwired seam, not a switch: the
  `[nli]` extra and `memory.nli.*` exist for library callers who inject a
  scorer themselves, and no daemon path constructs one. The four-path
  detector — slot identity, negation asymmetry, affirmative replacement,
  state transition — is what actually runs.
- **Cross-encoder reranker off** — wired into the pipeline but disabled by
  default; enable globally (`memory.reranker.enabled = true`) or per-call
  (`memory_search(..., rerank=True)`). Details: [Retrieval](retrieval.md#cross-encoder-reranking).
- **BM25 hybrid lexical pool ON** (since 2026-07-25) — a pure-stdlib
  sparse-retrieval channel that rescues exact-keyword queries. It shipped
  disabled, which meant every eval measured dense-only retrieval; turn it
  off with `memory.bm25.enabled = false` or per-call `bm25=False`. The
  cortex-fact analogue exists but ships **opt-in**
  (`memory.bm25.cortex_enabled = false` by default — a pre-registered A/B
  measured no end-to-end benefit on facts).
  Details: [Retrieval](retrieval.md#bm25-hybrid-retrieval).
- **Depth-ramped recency boost off** (`memory.recency_boost_enabled =
  false`, since 2026-07-25) — retrieval used to scale scores by a
  `0.4 → 0.0` ramp over band depth, treating depth as a proxy for age.
  Depth is set by promotion history, which without retrieval to accrue
  access counts tracks *surprise*, not age — so the ramp could rank a
  weaker shallow match above a stronger deep one (measured: up to 18
  points on the LongMemEval naive-RAG arm). Under the flat default the
  ramp is additionally structural dead weight (one band, no depths), so
  the knob has left the Console config surface; it still applies to
  multi-band presets via `config.yaml`.
- **Superseded entries stay visible** (`memory.hide_superseded = false`,
  since v0.7.3) — an explicitly superseded entry, or one carrying a mark
  from an earlier version, is still retrievable, downranked ×0.55 to favor
  current entries over their history. Ordinary stores do not apply this
  mark merely because the detector finds a potential conflict. Keeping
  history lets the agent say "you used to have X, then
  you said Y". Set it to `true` to restore the pre-v0.7.3 hard filter;
  that filter is why a category query once missed the only entry naming
  the category, and it costs knowledge-update recall, so treat it as a
  debug/audit switch. Before 2026-07-30 this knob was mis-registered as
  `memory.show_superseded` and did nothing.
- **Abstention off** (`memory.search_confidence_floor = 0.0`) — set it
  above zero and `memory_search` returns `low_confidence: true` whenever
  the top match scores below the floor. Calibrated as a pair with
  `memory.cortex.guard_min_score`; the recommended abstention-on values
  and the calibration story: [Retrieval](retrieval.md#abstention--confidence-floors).
- **Dream slot resolver off** (`memory.cortex.dream_slot_match_threshold =
  0.0`) — a positive cosine floor lets the dream pass map a paraphrased
  `(entity, attribute)` onto an existing slot before writing, to catch
  small-model supersession forks. ⚠️ Calibration found **no measurable
  benefit** on the benchmark (stale-leak flat; a false-merge at `0.80`):
  the residual fragmentation comes from the deterministic regex
  auto-promote, not paraphrase. Left off; enable only with the
  false-merge risk in mind. See
  [the single-writer cortex design](../specs/2026-06-19-single-writer-cortex-design.md)
  for the structural fix.
- **Constraint pinning on** (`memory.cortex.pin_constraints = true`, schema
  v35) — a fact whose `distortion_tolerance` is `constraint` is served
  AHEAD of the cosine ranking, marked `pinned: true`, when it is in
  scope: in `memory_search`'s cortex block when the query names the
  fact's entity (separator-insensitive, word-bounded), in
  `memory_recall` when the entity is a seed of the walk. A pin must still
  clear the caller's `min_score` floor, pins take at most half of `top_k`
  (best cosine first) so the ranked answer keeps the rest, and the
  payload never grows. An unlabelled bank is served byte-identically
  either way; set `false` for plain ranking.
- **Slot read telemetry on** (`memory.cortex.read_tracking = true`, schema
  v33) — every cortex slot served as an answer (`memory_fact_get` and the
  cortex-first block of `memory_search`) bumps its `slot_reads` counter,
  one small upsert per fact-serving call. Feeds the `read_audit` section
  of `memory_stats` (never-read fractions, slot coverage). Deliberately
  uncounted: internal verification lookups, and the facts attached to
  `memory_recall`/`memory_graph` neighborhoods (context, not a direct
  answer) — treat a slot's never-read status as a lower bound. Set
  `false` to disable the write; the audit section stays available either
  way (it just stops moving). Since v34 the section also carries
  `graduation_candidates`: entries served in ≥60% of the last 30 days'
  distinct sessions (once ≥8 sessions are on record) — static-context
  ("promote to CLAUDE.md") candidates; vet against the cortex before
  promoting, since the log counts serves before the handler's fact-dedup.
- **Engram cross-index on** (`memory.traces.enabled = true`, schema v13) —
  the dream links each consolidated fact-slot to the dense episodes it came
  from. Forwards, that link is where a fact came from: `memory_get`'s
  `consolidated_into` / `source_entries`. Backwards, it powers two
  read-time cautions: `re_verify` on served facts and `derived_flagged` on
  `memory_supersede` (see [Memory model](memory-model.md#how-current-is-this-fact)).
  Set `false` to silence both — the read surfaces stop paying for the
  cross-index query but otherwise serve exactly as before.
  Since schema v39, correction events for existing traces survive source
  deletion and are preserved even while tracing is off; new trace formation
  remains disabled. Re-enabling tracing can therefore surface those warnings.
  `memory.traces.retention_boost` (default `0.0`) is the separate Phase-2
  MTT-retention weight this same cross-index feeds; `0.0` is today's
  eviction behavior unchanged.
- **No HyDE / no reflection** — both rely on an LLM callback. Claude *is*
  the LLM, so the natural way to reflect is for Claude to call
  `memory_store` with a self-composed summary.
- **Auto-outcome inference on** (`memory.lessons.infer_outcomes = true`) —
  a session episode that closes with entries but zero `memory_outcome`
  calls gets up to `memory.lessons.infer_outcomes_max_signals` (default
  `3`) signals inferred from its own record on the end-of-session dream;
  see [Episodes](episodes.md#inferred-outcomes-at-session-close). Set
  either to `false` / `0` to turn it off.
- **Dream edge quarantine on** (`memory.dream.relation_quarantine_below =
  0.5`) — dream-extracted graph edges scoring below the floor are filed as
  review proposals (`source="dream-low-confidence"`) instead of entering
  the live graph. At the default this catches exactly the untyped
  `related-to` co-mention edges (confidence 0.45); typed relations (0.70)
  write live as before. Set `0.0` to disable and restore write-live
  behavior.
- **Literal-faithfulness gate on, enforcing** (`memory.dream.literal_gate
  = "enforce"`, `memory.dream.literal_gate_scope = "batch"`) — digit-bearing
  tokens in a dream claim's value (date-like spans and `~`-marked
  approximations exempt) must appear in the pull's source notes, allowing
  the legitimate re-formattings extractors produce (spelled numbers,
  hyphenated ranges/compounds, `N+` minimums); unbacked literals are
  dropped and counted (`literal_dropped`/`literal_flagged` in dream
  results). Enforcement became the default on 2026-08-02, when the
  extended matcher left the at-scale probes firing almost exclusively on
  genuinely unbacked literals — derived aggregates and imported world
  knowledge — at 1.3–1.7% of gateable claims
  (`evals/results/gate-firing-normfix-verdict.json`). `"log"` counts
  without dropping; `"off"` disables. The batch-union corpus default
  exists because derived sums and cross-note values are measured
  false-drop classes under per-note (`"source"`) gating.
- **Provenance-span gate off** (`memory.dream.span_gate = "off"`) — the
  literal gate's sibling: where the literal gate checks digit-bearing
  values, the span gate checks that a scalar claim's *quoted source span*
  actually appears in the pull's notes — fidelity-to-source, not
  trustworthiness-of-source. `"log"` counts without acting; `"contend"`
  parks unbacked scalar claims as visible contenders with a
  `span:unbacked` marker, resolvable via `memory_fact_resolve`. Ships off
  because flipping it on requires the live extraction prompt to emit
  quotes (the live v12 prompt does not).
- **Lesson-synthesis dedup on**
  (`memory.lessons.synthesis_dedup_min_similarity = 0.88`) — a synthesized
  lesson that near-matches an existing *current* lesson at a different key
  with the same polarity is silently skipped and counted (`lessons_deduped`
  beside `lesson_signals`/`lessons_written` in the dream-run row).
  Opposite-polarity matches and explicit `lesson_write` callers are never
  gated. `0` disables.
- **Lesson-synthesis batch cap** (`memory.lessons.synthesis_max_signals =
  200`) — most outcome signals one dream sweep drains. The batch commits
  its lessons, graph edges and acknowledgements in one transaction under
  the service lock, so this bounds a single daemon pause rather than the
  total work; whatever it leaves behind is picked up by the next sweep.
  A chosen bound (roughly one extractor batch), not a measured one. `0`
  drains everything pending.
- **Outcome signals kept ten years** (`memory.lessons.signal_retention_days
  = 3650`, was `30` until 2026-09-23) — the dream sweep deletes
  `memory_outcome` signals, consumed or pending, once they are older than
  this. Signals are the only evidence behind a lesson: under the 30-day
  window, 760 of the live bank's 1,618 current lessons had already lost
  every signal they came from. The log grows about 800 rows (under 1 MB on
  disk) a month.
- **Pending signals offered for 30 days** (`memory.lessons.signal_retry_days
  = 30`) — a pending signal is offered to lesson synthesis, oldest first
  and up to `synthesis_max_signals` per sweep, only while it is younger
  than this. A signal whose extraction lands no lesson stays pending and
  is offered again on later sweeps. Past this age it is kept as evidence
  but no longer offered, so a full batch of permanently failing signals
  cannot hold newer ones back for the whole retention window. The age
  counts from when the signal was recorded, not from its first attempt:
  signals never offered (synthesis off, an extractor outage or backlog
  longer than this) age out too. They stay in the table, the Console's
  loop-health tile counts them apart from the pending ones, and raising
  the value offers them again. A chosen bound (the retry lifetime the old
  30-day retention implied), not a measured one. `0` offers pending
  signals for the whole retention window.
- **Slot-index shadow verification on** (`memory.slot_index_shadow_rate =
  0.01`) — ~1% of slot-pool queries recompute the index from scratch and
  compare; divergences land in `stats()` as
  `slot_index_shadow_divergences`. `0.0` disables, `1.0` checks every
  query (dev/debug).
- **Quarantine retype on** (`memory.dream.retype_quarantined_max = 3`) —
  per-dream cap on quarantined pairs re-offered to the extractor for
  typing, shown only the notes where both entities co-occur; a typed
  answer becomes a review proposal, never a live edge. Without it the
  quarantine only accumulates. Set `0` to disable.
- **Dream-run journal retention** (`memory.dream.runs_keep = 50`) — the
  newest N dream-run rows and their pre-image journals (schema v27)
  survive; older ones are pruned on the sweep tick beside superseded-row
  compaction. The journal is what `memory_dream(action="rollback")`
  replays, so this bounds how far back a pass stays revertible — see
  [Dream runs — audit and rollback](dreaming.md#dream-runs--audit-and-rollback-schema-v27).
- **Chronicle extraction on** (`memory.dream.chronicle = true`) — the
  dream pass runs a second, dedicated events-extraction call per
  batch and stores dated occurrences into `chronicle_events` (schema
  v28); temporally-cued searches serve them as an `events` block
  (aggregation cues widen the block and add `events_total`). Default-on
  since 2026-08-12: the pipeline passed its preregistered gates and a
  2026-08-05..08-12 production soak reviewed clean. Needs Postgres; an
  events-pass failure never stalls claims. Set `false` to opt out — see
  [Chronicle events](dreaming.md#chronicle-events-schema-v28--dated-occurrences-beside-facts).
- **Session digests off** (`memory.dream.digest_enabled = false`) — when
  on, the idle dream cycle writes one narrative prose digest per closed
  session episode as a retrievable `source="digest"` band entry (never
  re-mined for facts — `digest` is in `exclude_sources`), and the
  session briefing's recap renders the digest body. The zero-start
  cursor backfills history when first enabled,
  `memory.dream.digest_max_per_cycle` (default `4`) episodes per dream
  pass. `memory.dream.digest_target_chars` (default `1200`) is the prose
  length target passed to the extractor — re-targeted from `800` to the
  length the extractor naturally writes (probe, 2026-08-27) — and
  `memory.dream.digest_context_chars` (default `24000`) caps the
  per-call session context, with longer sessions split on line
  boundaries and map-reduce merged. Default-off pending human review of
  the sidecar quality probe
  (`evals/digest_sidecar_probe.py`).
- **Consolidation quarantine off** (`memory.dream.quarantine_low_trust =
  false`) — when on, a scalar dream claim whose backing entry is
  agent-tier (its `source` maps to origin `agent`) and outside
  `memory.dream.trusted_sources` never takes `current` directly: it
  parks via the existing contender machinery (visible in
  `memory_fact_get` as contested), promotable only by an explicit
  `memory_fact_resolve(accept=true)` or by an independent second
  witness — a later matching claim from a different witness token
  (episode, else source) or a non-agent origin. The same witness
  restating confirms but never promotes. Parks and promotions are
  journaled (schema v27) and covered by `memory_dream(rollback)`.
  Honest scope: this does not stop a poisoned entry from being stored
  or retrieved — episodic search still surfaces it; the claim is that
  poison does not silently gain *canonical* authority. Scalar claims
  only in v1; member ops keep their existing guards. See
  [dreaming](dreaming.md) and the threat model in `SECURITY.md`.
- **Aggregation-recall retrieval knobs off**
  (`memory.search.contiguity_neighbors = 0`,
  `memory.search.timeline_channel = false`) — Phase 1 retrieval-side
  experiments (neighbor expansion, a timeline channel) that measurably
  failed their gates and ship dormant; they remain settable for
  replication but there is no measured reason to enable them.
- **Candidate pool at the served width**
  (`memory.search.candidate_pool_multiplier = 1`,
  `memory.search.fusion = "weighted_sum"`) — the
  retrieve-then-rerank shape (a dense pool `top_k x multiplier` wide,
  optionally merged by reciprocal rank fusion instead of raw-sorting
  incommensurate channel scores) exists and is settable, and it **lost**
  its judged run: on the LongMemEval knowledge-update oracle slice
  (2026-09-04, n=78) multiplier 4 cost naive RAG 0.115 accuracy under
  `rrf` and 0.077 under `weighted_sum`, while serving 36-54% more
  context tokens on every arm that serves turns. Table, caveats and artifacts in
  `evals/README.md` ("Judged verdict (2026-09-04)"). CAUTION if you
  enable `rrf` anyway: it changes the SCALE of every served score to
  ~0.016-0.05, so `memory.search_confidence_floor` must stay 0, and
  `rrf` must not be combined with the cross-encoder reranker
  (`memory.reranker.fusion_weight` collapses to cross-encoder-only
  ordering, `memory.reranker.skip_margin` can never be reached) or with
  a populated reference bank (its raw cosines are not rescaled and
  outrank every memory once the reranker fires). Neither combination has
  been measured.
- **Assistant-stated claims parked, not adopted**
  (`memory.dream.assistant_claims = "contender"`) — what a dream claim
  labelled `speaker: "assistant"` becomes: `contender` writes it at the
  floor `assistant` provenance tier (it may fill an empty slot, but
  against a value or member set of any other origin it parks as a
  contender, and it ranks below user-origin facts at equal similarity),
  `supersede` treats it as an ordinary agent-tier dream claim, and `drop`
  discards it. An unrecognised value falls back to `contender` — a typo
  must not open the overwrite path. **Live on the default path since
  2026-09-05**, when the provenance extraction prompt shipped: an
  extraction can now carry a `speaker` label, so the knob decides what
  happens to assistant-stated claims on a stock install. (It was inert
  before that, because the old prompt never asked for the field. The
  label is asked for only where the note makes the speaker knowable, so
  on a bank whose notes carry no `user:` / `assistant:` marker most
  claims still arrive without one — as do claims from an older prompt or
  an extractor shim launched with `--system-prompt-file` — and those
  write exactly as they did before, whatever this is set to.) Kept off
  the Console deliberately: `supersede`
  is the setting that lets model-stated content overwrite a user-stated
  fact, which is a provenance decision rather than an operator dial. The
  measured comparison of the three values is in `evals/README.md`
  ("Assistant-stated facts").
- **Staleness served as annotation** (`memory.search.stale_policy =
  "annotate"`) — stale records (past 2×TTL for their freshness class)
  carry `effective_confidence`/`stale` flags and nothing more, today's
  behavior. `"demote"` additionally sorts stale records after non-stale
  ones on list surfaces and adds a top-level `warning`; `"quarantine"`
  replaces a stale record's `value` with a wrapper string and moves the
  original to `last_known_value` (data moved, never hidden). Applied at
  the shared record serialisers, so every scalar-fact read surface —
  including the compact `memory_search` / `memory_world_search`
  projections — behaves identically. Deliberate exemptions: version
  history (the audit surface and the recovery path), `chain` summaries
  and graph fact projections (machine-consumed), and set-valued slots
  (set members are structurally always evergreen — the set API carries
  no freshness class — so no set payload can be stale). Non-stale
  records are byte-identical under every policy; an unrecognised policy
  value degrades safely to `annotate`. Console note: the web console
  renders the record `value` field, so under `quarantine` a stale fact
  shows the wrapper there — a known P2 cost to weigh before ever
  flipping the default.

- **Compact MCP payloads on** (`memory.mcp.compact_payloads = true`,
  `memory.mcp.entry_text_chars = 600`) — the payload an MCP client reads
  *back* from a tool call, shaped for its context window: a
  `memory_search` hit's `text` is truncated to `entry_text_chars` and
  marked `truncated: true` (`memory_get` returns the full text); a
  superseded hit carries `replaced_by: {id, at, preview, verified,
  current}` — the successor's row id when one entry (or one current
  entry) has the replacement's text, the supersession date, the
  replacement's first 120 chars, whether an explicit correction
  (`memory_supersede` / `memory_consolidate`, successor source
  `correction` / `consolidation`; a custom consolidate `source` reads
  unverified) made the link, and whether that successor is itself still
  live (`current: false` marks a chain link or an unresolved successor)
  — instead of the replacement's full text, which `verbose=true` still serves
  (2026-09-23: about 4 in 10 links the automatic contradiction detector
  left before it stopped superseding point at an unrelated note, so the
  full text must not arrive framed as the answer);
  the cortex block serves `min(5, top_k)` facts,
  so a narrow search stops paying for five;
  and `memory_fact_get` serves the acting subset — value, kind/members,
  confidence, origin, `asserted_at`/`age`, freshness, the currency and
  label flags, `correct_with`, `source_entries`, `entity_ref`,
  `contenders` — moving provenance, support, writer/session id, tx/valid
  time and the supersession chain behind `verbose=True`. Measured on the
  2026-09-04 agent token ledger (`evals/agent_token_ledger.py`, r3): a
  default `top_k=8` search fell from 14,745 to 9,951 chars mean,
  `memory_fact_get` from 2,175 to 1,296. These are PROJECTIONS above the
  service layer — ranking, `min_score` and every benchmark number are
  unaffected. Set `compact_payloads: false` to restore the pre-2026-09-04
  payloads verbatim (superseded hits keep the `replaced_by` pointer, which
  `memory_get` also serves for a superseded entry, and
  `memory_episode_summary` still compacts its `recent_entries` like
  `memory_recent` — none of the three follows the knob); raise
  `entry_text_chars` for long-form corpora where
  the tail of a note carries the answer.

## Toolset tiers

Three visibility tiers — `minimal` (9 tools: the recall/capture loop, the
set-slot pair, the gate), `core` (24: + graph/recall, world facts, lessons,
documents, episodes, stats, `memory_get`, `memory_fact_resolve`, coordination),
`full` (38) — filtered per principal at `tools/list` (the named principal
from a `PSEUDOLIFE_MCP_TOKENS` bearer, else the writer id; sessions sharing
a credential share a tier view). The filter is
visibility, not auth (the bearer token is the security boundary) — but
Claude clients gate calls against their own tool list, so in practice a
session expands its tier before calling a hidden tool. Defaults:
`PSEUDOLIFE_MCP_TOOLSET` (unset → `full`; the Docker compose file ships
`core`, so lite and host-process installs start at `full`) sets the baseline;
`PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal,claude-code:core"` sets
per-client defaults by principal (writer id). Any caller can step its tier
up or down at runtime with `memory_toolset(action="expand"|"collapse"|"status")`
— the daemon emits `tools/list_changed` so the client refreshes its list.
Eager-loading clients (Claude Desktop) start at ~1.5k tokens of manifest on
`minimal`; clients that defer schemas client-side (Claude Code) barely
notice tiers at all.

**Weak-model deployments:** set `PSEUDOLIFE_MCP_TOOLSET=core` — it exposes
the curated core set and hides the power/hygiene tools (`memory_forget`,
`memory_relation_define`, `memory_dream`, `memory_graph_review`, …) that a
small model can misuse.

## Host-process install (Windows, for GPU / dev)

Runs Postgres in Docker but the daemon on host Python. Use this if you
want to hack on the daemon or run the embedder on a local GPU. Requires
Python 3.10+, Docker Desktop, and roughly 2 GB of disk — the
Qwen3-Embedding-0.6B weights (~1.2 GB) download on first run, on top of
CPU torch and the Python environment.

```powershell
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
python -m venv .venv
.venv\Scripts\activate
pip install -e .

# 1. Start Postgres 18 + pgvector (one-time build, then persistent).
docker compose -f ops/docker-compose.yml up -d --build pseudolife-pg

# 2. Register the daemon to auto-start at logon (binds 127.0.0.1:8765).
ops\install-autostart.ps1
Start-ScheduledTask -TaskName "Pseudolife-MCP Daemon"
```

The `pseudolife-mcp` console-script is now on your PATH — run
`pseudolife-mcp --help` for all modes. The main ones: `pseudolife-mcp serve`
(the daemon), `pseudolife-mcp` (the stdio shim — auto-starts the daemon if
absent), `pseudolife-mcp embedded` (the v0.1 in-process stdio server; no
daemon, no Postgres — an escape hatch), and `pseudolife-mcp briefing`
(print the session-start briefing; used by the hook).

## stdio shim (per-session identity)

The installer wires this by default (`ops/install.sh` / `ops/install.ps1`;
pass `--transport http` / `-Transport http` to opt out) because it's the
mechanism that gives **concurrent** Claude Code sessions distinct identity —
a per-process `X-PL-Session` header, the strongest of the five
[session-identity](#session-identity) tiers. The shim works against
**either** daemon deployment, host-process or the containerized stack — it's
just an HTTP client to `PSEUDOLIFE_MCP_DAEMON_URL` and only spawns a new host
daemon when nothing answers there already (a cross-process lock keeps
concurrent shims from each spawning one). On a Docker-tier install set
`PSEUDOLIFE_MCP_NO_SPAWN=1` in the shim's env — the installers do — so the
shim waits for the container instead of spawning a fallback that races its
port bind. Point Claude Code at it directly:

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "C:\\path\\to\\Pseudolife-MCP\\.venv\\Scripts\\pseudolife-mcp.exe",
      "env": {
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765",
        "PSEUDOLIFE_MCP_NO_SPAWN": "1",
        "PSEUDOLIFE_MCP_DATABASE_URL": "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
        "PSEUDOLIFE_MCP_DATA_DIR": "${USERPROFILE}\\.pseudolife-mcp"
      }
    }
  }
}
```

Replace `C:\path\to\Pseudolife-MCP` with wherever you cloned the repo. The
`PSEUDOLIFE_MCP_DATABASE_URL` matches the bundled `ops/docker-compose.yml`
defaults (user/password `pseudolife`, host port `5433`) — change it only if
you edit the compose file or override the password. The default password is
safe for the stock loopback-only stack (nothing off-box can reach Postgres);
to use your own anyway, set `POSTGRES_PASSWORD` in `ops/.env` **before the
first launch** (see the note in `ops/docker-compose.yml` for changing it
later).

The shim is torch-free, so sessions attach near-instantly; the daemon pays
the one-time embedder warmup once for everyone. On first run with a v≤0.1
`cms_state.pt` present in `PSEUDOLIFE_MCP_DATA_DIR`, the daemon
auto-migrates it into Postgres and renames the originals `*.pre-v8.bak`
(never deletes them). The import records its progress in a
`legacy_migration` meta row, so one that fails part-way resumes on the next
start instead of leaving a short bank behind. While it is unfinished the
daemon keeps serving and `/health` stays `status: "ok"` (so healthchecks and
`ops/update.ps1` are not tripped by it) but carries an extra
`migration_partial` field; the matching ERROR lines in the daemon log name
the resume path. A resume merges rather than overwrites — cortex facts
written during that window are kept, and only slots nobody has written land
from the legacy bank. Leave the original `.pt` files in place until it
completes: the resume reads them, and deleting one makes the bank
unfinishable.

The daemon owns its bank alone. It holds a Postgres advisory-lock *writer
lease* for as long as it runs. A second daemon, a stdio-embedded server, or
a maintenance script or eval that opens the same bank through the service
refuses to start, and names the process that holds it. Stop the daemon for
offline maintenance such as `ops/dedup_cortex.py`. If the daemon loses its
database session and another lease-holding writer used the bank
meanwhile, the daemon re-reads the bank before it serves or saves
anything. If loading the bank fails
at startup, the daemon serves nothing rather than a partly loaded bank:
- `/health` reports `status: "degraded"` with the reason in `not_ready`. A
  daemon refused the lease reads the same way.
- Tool calls are refused.
- Retries back off from 5 s, doubling to 60 s. The daemon retries a
  startup failure on its own for the first couple of minutes. After that,
  or for a failure first met by a later call, it retries on the next call
  or on the session reaper's 5-minute tick.

`init_refusal` is different. It marks a bank the daemon will never serve as
configured, such as an embedding-dimension mismatch, and the shim exits on
it.

## Session identity

Every request resolves "which session/episode does this write belong to"
through one chokepoint, evaluated in strict precedence order:

| tier | source | scope | notes |
|---|---|---|---|
| 1 | `X-PL-Session` header | per shim process = per session | the stdio shim sends this on every call; any integrator can |
| 2 | explicit `episode` argument | per call | pass an open episode id (or its unambiguous ≥8-char prefix) on `memory_store` / `memory_outcome` / `memory_fact_set`, and on the lifecycle tools `memory_episode_start` / `memory_episode_end` / `memory_session_title` — where a resolved handle wins outright (they never consult the header tiers); the daemon mints it and advertises it in the SessionStart briefing |
| 3 | hook-registered active session | machine-scoped pointer | the SessionStart hook forwards Claude Code's own `session_id`; a SessionEnd hook closes it. A singleton — concurrent sessions race it, which is why the lifecycle tools take the per-call handle |
| 4 | `mcp-session-id` header | per connection | **retired** — the header names the connection (concurrent sessions share it) and the MCP 2026-07-28 revision (SEP-2567, "Sessionless") removes it from the protocol. `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` restores it for one release as a rollback hatch |
| 5 | none | — | writer id + idle-gap sessionization (the reaper) — the documented floor when nothing above resolved |

**Why the header outranks the handle when both are present.** A shim
header is infrastructure-asserted per OS process; an `episode` handle is
model-supplied and can be confused between two concurrent sessions'
briefings. But identity and target episode are separable — a write still
lands in the handle's named episode even when the header wins identity for
stamping. An unknown, closed, or ambiguous handle never fails the write —
it degrades to the next tier and the result carries
`"episode_warning": "unknown or closed episode handle"`.

**Tier 3's limitation.** The active-session pointer is one machine-scoped
value, last-start-wins: whichever SessionStart hook fired most recently
owns it until its own SessionEnd clears it (or a later SessionStart
overwrites it). Two concurrent sessions that are both *unheaded* (no shim)
and *handle-less* (no `episode` argument) still misattribute to the newer
one — tiers 1 and 2 are the actual concurrency answer, not tier 3. Accepted
as YAGNI until a real multi-writer/LAN deployment needs a per-writer
pointer.

This cuts across clients, not just across Claude Code sessions: because the
pointer is machine-scoped, a **second client that sets no identity of its
own** — e.g. Codex or a ChatGPT connector talking to the daemon over direct
HTTP with no shim, no hook, and no `episode` argument — resolves at tier 3
to whatever session the Claude Code hook last registered, so its writes are
attributed to Claude's session episode. The fix is the same as for
concurrent sessions: give the second client a tier-1 identity (run it
through the stdio shim) or pass explicit tier-2 `episode` handles on its
writes. The installer's shim mode wires **Codex** through the shim by
default (2026-07-19), and **Gemini CLI** the same way (2026-08-29); each
first-class provider's registration also carries its own
`PSEUDOLIFE_WRITER_ID` (`claude-code` / `codex` / `gemini`) so a shared
bank attributes writes per agent (see
[the providers guide](providers.md)). ChatGPT connectors and other
direct-HTTP clients still hit the tier-3 leak.

**Pointer TTL.** A client that crashes or is killed never fires SessionEnd,
so without a bound its pointer would attribute every later tier-3 write to a
dead session until the next SessionStart overwrote it. The pointer therefore
expires: one older than `PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS` (default
`21600` = 6 h, the resume window — past it a return starts a fresh episode
anyway; `0` disables the TTL) is treated as stale and tier 3 falls through to
the transport/idle-gap floor. The timestamp refreshes on-set only, which
Claude Code re-fires on resume/compact, so a genuinely active session stays
live; resolution never refreshes it (a wrong client's traffic can't keep a
dead session's pointer alive).

The resolved identity becomes the episode's `session_key` wherever it's
used; `session_key` is a free-text field, so none of this required a schema
change.

## Sharing memory on the LAN

Run the daemon with `PSEUDOLIFE_MCP_HOST=0.0.0.0` and a
`PSEUDOLIFE_MCP_TOKEN`; remote clients set the same
`PSEUDOLIFE_MCP_DAEMON_URL` + `PSEUDOLIFE_MCP_TOKEN`. The daemon **refuses
to bind a non-loopback host without a token**, and Postgres itself stays
loopback-only — the LAN only ever sees the daemon.

The token is also what relaxes the MCP endpoint's DNS-rebinding guard. With
a token set, `/mcp` accepts any `Host` header — a LAN address, a
reverse-proxy hostname, a Tailscale name, a compose service name — because
`Authorization` already proves intent. Tokenless (loopback use, or a
container published to 127.0.0.1 via `PSEUDOLIFE_MCP_TRUST_BIND`), `/mcp`
serves loopback `Host` values only and answers anything else with
`421 Invalid Host header`; that is the guard against a rebinding browser
reaching an unauthenticated bank. So: fronting the daemon with a reverse
proxy under a real hostname means setting a token.

## Data layout

**Containerized / daemon mode (recommended).** The durable source of truth
is **Postgres**, which lives in an *external* Docker volume —
`pseudolife-mcp-bank` by default (entries + facts + graph). A second
external volume, `pseudolife-mcp-state`, holds the daemon's ChromaDB
reference bank, the counter file `weights.pt`, and the cortex snapshot.
Both are declared `external` in `ops/docker-compose.yml` precisely so a
container teardown can't take them with it. The host `data/` dir then holds
only backups (`data/backups/` from `ops/backup.ps1` — a `pg_dump` of the
bank *plus* a tar of the state volume) and one-time legacy-import staging —
*not* the live bank.

To wipe the bank in this mode you must drop those volumes deliberately —
**never `docker compose down -v` or `docker volume rm` without
`ops/backup.ps1` first**; `stop` / `start` and `up -d --build` keep both
volumes.

**File mode (no daemon / no Postgres — the `embedded` CLI, or unset
`PSEUDOLIFE_MCP_DATABASE_URL`).** Everything lives under
`PSEUDOLIFE_MCP_DATA_DIR`:

```
data/
├── memory_state/
│   └── cms_state.pt        # Associative entries + metadata (file mode)
├── cortex_state.pt         # Slot-keyed canonical facts (cortex, schema v8)
├── chromadb/               # Reference bank (RAG documents)
└── config.yaml             # Optional overrides
```

In **file mode only**, wipe memory by deleting `data/` and restarting; wipe
just documents via `data/chromadb/`; wipe just the associative store via
`data/memory_state/`. (In containerized mode these files are not the source
of truth — see the volume note above.)

## Windows / WSL2 memory (Docker tier)

Docker Desktop's WSL2 VM (`Vmmem`) claims up to **~50% of host RAM** by
default, which is far more than the stack needs. Adding up the parts
measured 2026-09-23 — daemon ~2.5 GB with the bf16 embedder or ~4 GB with
fp32, the extractor sidecar's ~5.3 GB mmapped model plus its context, and
Postgres — the whole stack wants ~9 GB under dream load with the default
sidecar (~10 GB with fp32), or ~3 GB in `sonnet-only` mode (~4.5 GB), where
the Qwen3 embedding backbone is the bulk of it. Encode bursts add up to
~1 GB on top.
Cap the VM by copying `ops/wslconfig.example` to
`%USERPROFILE%\.wslconfig`, tuning `memory=`, then `wsl --shutdown`.

The daemon container is separately hard-capped at 6 GB, with memory+swap
pinned to the same value so exceeding it is a clean container restart rather
than a host-wide memory event. `PSEUDOLIFE_DAEMON_MEM_LIMIT` in `ops/.env`
raises it for very large banks; `/health`'s `memory` block shows how close
the daemon runs to it (`near_limit` at 90%).

After `wsl --shutdown` the host port forward is gone; `docker restart
pseudolife-mcp-daemon` re-establishes it.

## Backups

`ops\backup.ps1` (Windows) / `ops/backup.sh` (Linux/macOS) runs `pg_dump`
inside the container into `data\backups\` with 7-day rotation, and also
tars the daemon **state volume** (ingested `document_ingest` files, cortex
snapshot, graph snapshots — those live only there, not in Postgres) into a
sibling `pseudolife_state-*.tgz`. An optional off-disk mirror via
`PSEUDOLIFE_BACKUP_MIRROR` carries both artifacts;
`PSEUDOLIFE_BACKUP_MIRROR_KEEP=N` (or `-MirrorKeep` / `--mirror-keep`) caps
the mirror at the newest N files per kind — handy for cloud-synced folders.
The matching `restore` script rehearses the newest backup into a scratch
database by default (never touching the live bank) and only replaces the
live bank with an explicit `-Apply` / `--apply`; add
`-StateArchive <pseudolife_state-*.tgz>` / `--state-archive` to also
restore the state volume (opt-in, so a DB-only restore never clobbers
current state).

Each dump also gets a `pseudolife_manifest-<stamp>.json` beside it: the
per-table row counts, read from the dump itself. The manifests drive a
**row-count gate**. If `entries`, `facts` or `lessons` fell by more than
25% (`-MaxRowDropPercent` / `--max-row-drop-percent`) against the newest
manifest that was not itself held, the script keeps the new dump but skips
local rotation and mirror pruning and says so loudly. It looks for that
baseline in both the backup folder and the mirror. A logical wipe therefore
cannot rotate the good copies away. It also holds when history exists but
none of it is usable (unreadable or count-less manifests, or a folder it
cannot list), so a gate that cannot see never waves a wipe through. On the
first run after upgrading there are dumps but no manifests yet; the
newest complete date-stamped dump is then read as the baseline, and only
a truly empty history rotates without one. The warning names the last
good dump and the `restore` command for it. With no file named, `restore`
skips held dumps (the newest one after a wipe is the one that shrank) and
refuses if every dump is held; naming a file overrides that. The hold repeats on
every run until one passes `-AcceptRowDrop` / `--accept-row-drop`, which
rotates and makes that dump the new baseline. The gate compares each dump
with the newest good one, so it is built for sudden loss: a slow decline
of less than the threshold per backup passes. The manifest is also
copied into the daemon, and `/health` reports it as `last_backup` (`at`,
`age_hours`, `rotation`). The key is absent until a backup script has
run; the pip tiers' `pseudolife-mcp backup` does not record one yet.

Deploys back up first, but nothing else backs up on a schedule. On
Windows, register a daily run once:

```powershell
ops\install-backup-task.ps1              # daily 03:00
ops\install-backup-task.ps1 -At 02:15
ops\install-backup-task.ps1 -Uninstall   # remove it
```

The task runs the main checkout's `ops\backup.ps1`, even when installed
from a worktree; the installer warns if that copy predates the row-count
gate. It catches up at the next boot or logon if the machine was off,
waiting up to 10 minutes (`-DockerWaitSeconds`) for Docker to answer
first, and it runs as you, so `PSEUDOLIFE_BACKUP_MIRROR` applies. Each run
is appended to `data\backups\backup-task.log`. On Linux/macOS, a cron entry
that runs `ops/backup.sh` does the same job, but cron starts with a bare
environment: set `PATH` (so it finds `docker`) and any
`PSEUDOLIFE_BACKUP_MIRROR*` variables in the crontab itself.

The pip tiers (lite / host-process) use `pseudolife-mcp backup` instead:
same shape — a `pg_dump | gzip` of the bank (`--no-owner --no-acl`, so
the artifact restores under any role — rehearsed in the test suite
against a role-named PostgreSQL 18; since the Docker tier's 16→18 bump
(2026-08-14) both tiers run PostgreSQL 18, so a lite dump restores
straight into the Docker tier; the lite tier uses the embedded
runtime's own bundled `pg_dump`, attaching to the running instance or
starting it for the duration) plus a
`pseudolife_lite_state-*.tar.gz` of the data dir (ChromaDB, weights,
config; `embedded_pg/` is excluded — the dump covers it), with the same
7-day rotation (`--keep-days`). The artifact names
(`pseudolife_lite_memory-*` / `pseudolife_lite_state-*`) are deliberately
disjoint from `ops/backup.*`'s, so the two tools can share a directory
without either's rotation or restore-picker ever touching the other's
files. A backup never initializes a bank that doesn't exist yet, a run
that produced no dump never rotates dumps, and rotation only ever
deletes files the tool itself wrote.

### Logical export / import

Beside the physical backups, `pseudolife-mcp export` writes the bank as a
portable ZIP — one JSONL file per table plus a manifest (schema version,
embedding dimension, per-table counts) — from a single read-only snapshot,
so it is safe to run against a live daemon (the snapshot stays open for
the duration, which delays autovacuum on busy tables — prefer a quiet
moment for a very large bank). Unlike a `pg_dump`, the
artifact is deployment-tier- and Postgres-version-independent,
human-readable, and loads additively across schema versions: an export
from an older build imports into a newer one, with new columns taking
their DDL defaults. Embeddings travel verbatim (the manifest pins their
dimension), so neither command needs the embedding model.

`pseudolife-mcp import <archive.zip>` loads an export into a **fresh,
empty bank** in one transaction. It refuses a non-empty bank, refuses
while any other connection holds the database — stop the daemon first
(Docker tier: `docker compose -f ops/docker-compose.yml stop
pseudolife-daemon`); `--force` overrides for connections you know are
inert — and refuses an export whose format version or embedding dimension
it cannot honor. Operational telemetry (retrieval/read logs, the dream-run
journal), agent instance credentials, coordination mail and the board's audit
log deliberately stay behind, and the manifest lists exactly which
tables were excluded. Ingested `document_ingest` files live on the state
volume/data dir, not in Postgres — carry those with the physical backup's
state archive.

Both commands resolve the bank the way `backup` does: the explicit
`PSEUDOLIFE_MCP_DATABASE_URL` first (for the Docker tier that is
`postgresql://pseudolife:<POSTGRES_PASSWORD from
ops/.env>@127.0.0.1:5433/pseudolife_memory`), else the lite tier's
embedded instance, attached or started for the duration — never
initialized: importing is how a fresh bank gets *filled*, but creating
one is the daemon's job.

## Schema version history

The current Postgres meta version is **v42**; migrations are additive
`ADD COLUMN IF NOT EXISTS` on daemon start, and legacy file-mode `.pt`
banks auto-migrate into Postgres. The one exception is v25 itself: a
vector *dimension* change on an existing column is not additive, so
`ensure_schema` refuses to start against a bank still dimensioned at
v24 or earlier instead of attempting an in-place ALTER — run the
human-gated `ops/migrate_embeddings.py` first. Full step-by-step operator
procedure (backup, stop, dry-run, apply, deploy, verify, rollback):
[the v25 migration runbook](../runbooks/embedding-v25-migration.md).
Separately from the schema meta version, Docker-tier installs created
before 2026-08-14 also need the PostgreSQL 16 → 18 volume cutover —
[the PostgreSQL 18 migration runbook](../runbooks/postgres-18-migration.md).
The milestones:

| Version | What it added |
|---|---|
| v11 | Temporal/provenance stamp (tx/valid time, HLC ordering, writer/session) |
| v12 | Graph-insight communities |
| v13 | Provenance-trace engram + reinforcements |
| v14 | Episode `session_key` |
| v15 | Episode `parent_id` (nesting) |
| v16 | `entity_sources` (per-entity project attribution) |
| v17 | `edge_proposals` (deep-dream link candidates) |
| v18 | `entity_proposals` (deep-dream merge/junk candidates) |
| v19 | Partial unique indexes enforcing one current row per slot on facts/world_facts/lessons (+ startup heal of pre-existing duplicates; per-slot write-through persistence replaces the full-table snapshot rewrite) |
| v20 | `dismissed_pairs` (reviewed-distinct pairs stop resurfacing as duplicate findings) |
| v21 | `merge_decisions` audit + write-time near-duplicate merge proposals |
| v22 | `edges(dst_id)` index (dst-side graph lookups no longer sequential-scan) |
| v23 | `facts.freshness_class` — read-time currency on personal cortex facts (evergreen default, so existing facts are unchanged; mark transient ones `volatile` and they decay and flag `stale`) |
| v24 | `entity_kinds` (one `artifact`/`system`/`concept` kind per entity) — `freshness_class` now defaults to inferring from the entity's kind instead of a fixed default; only `system` entities can resolve `volatile`, and an empty table resolves everything to `evergreen`, so behaviour is unchanged until it is populated |
| v25 | `entries`/`facts`/`world_facts`/`lessons.embedding` move from `vector(384)` to `vector(1024)` — default embedding backbone swaps to Qwen/Qwen3-Embedding-0.6B (measured R@10 0.809 vs shipped MiniLM's 0.572). Qwen3-Embedding is instruction-asymmetric — see [asymmetric query/document encoding](retrieval.md#asymmetric-query-and-document-encoding) — so similarity-threshold semantics shift too. `ensure_schema` refuses to start against an existing v24-dimensioned bank rather than attempting an in-place ALTER; migrate first with `ops/migrate_embeddings.py` (dry-run by default; `--apply --backup-verified` to commit) |
| v26 | `facts.kind` (`scalar` \| `member`) and `facts.value_norm` — set-valued cortex slots (many concurrently-current members per `(entity, attribute)`, not one NOW value). The per-slot current-uniqueness constraint splits by kind (`facts_slot_current_scalar_uq` keeps one live scalar row per slot; `facts_member_current_uq` allows several current members on the same slot); the daemon-start duplicate-healing pass is scoped to `kind = 'scalar'` so it never demotes member rows. Additive/idempotent; every existing fact defaults to `kind='scalar'` and dedupes exactly as before. See [Set-valued slots](memory-model.md#set-valued-slots-schema-v26) |
| v27 | `dream_runs` + `dream_run_slots` — every dream pass that pulls entries records a run row (cursor movement, tallies, lifecycle status) and a per-claim pre-image journal (what each slot held before the write, `NULL` = slot absent). The journal is what `memory_dream(action="rollback")` replays, and it survives superseded-row compaction by construction (own tables, own newest-N retention via `memory.dream.runs_keep`). `dream_run_slots.src_entry_id` deliberately carries no FK — entries are evictable. Additive/idempotent |
| v28 | `chronicle_events` — dated occurrences as first-class records beside facts (`occurred_at` = event time, nullable and never fabricated; `occurred_phrase` = the source's verbatim wording; `recorded_at` = transaction time). Additive-only: contradiction handling sets `invalidated_at`, never deletes; event writes journal into `dream_run_slots` (new nullable `chronicle_event_id` column) so rollback can delete them by exact id. No FKs — `src_entry_id` references evictable entries. Extraction into the table (`memory.dream.chronicle`) shipped off by default and flipped on 2026-08-12 after its preregistered gates and a production soak both passed. Additive/idempotent |
| v29 | `facts.stance` — epistemic stance as a labelled field: the source's own hedge words ("probably", "per the runbook"), kept verbatim and separate from `value` so consolidation cannot silently turn a hedged claim into a confident canonical fact (the labelled-field-vs-inline retention result is arXiv:2608.06953). `NULL` = asserted plainly, exactly the pre-v29 behaviour, so the migration is a no-op on existing banks. Stance follows the latest asserting write (a plain restatement clears the hedge), surfaces in `memory_fact_get`/recall/history only when set, and is never an input to confidence, ranking, or supersession. Written by the dream path since the v10 update-anchored stance prompt shipped its gates (2026-08-14); not exposed on the `memory_fact_set` tool surface. Additive/idempotent |
| v30 | `entity_proposals.judge_verdict` / `judge_confidence` / `judge_note` / `judge_model` / `judged_at` — the autonomous Step-C judge's shadow verdict on a pending merge proposal, recorded by the sweep (`memory.deep_dream.judge_mode`: `off` \| `shadow` \| `auto-reject`) and surfaced beside the evidence in review payloads. The verdict is an opinion on the pending row; the durable decision record stays `merge_decisions`, written only when a decision path (human, agent, or the confidence-gated auto-reject) ratifies it. `NULL` = not yet judged, exactly the pre-v30 behaviour, so the migration is a no-op on existing banks. Judge-model floor measured by `evals/judge_ladder.py` (`evals/results/judge-ladder-20260816.json`). Additive/idempotent |
| v31 | `retrieval_events` + `retrieval_uses` — the retrieval event log (learned-reranker Phase 0). Every `memory_search` appends one event row (query text, the ranked served list as JSONB with entry ids/scores/ranks, writer session/episode); a later `memory_get`/`memory_reinforce` on a served entry in the same session writes an implicit relevance label (most-recent serving event wins, bounded by `memory.retrieval_log.use_window_seconds`; the asserted `memory_outcome(used_ids=)` label credits every serving event in that window — it names ids, not queries). Together they are the (query, served, used) training tuples for a future learned fusion/reranker stage — purely observational, no retrieval behaviour changes. Served ids carry no FK (entries are evictable; training joins tolerate dangling ids); labels CASCADE from their event; events are pruned on the dream-sweep tick after `memory.retrieval_log.retention_days` (default 365). Kill-switch: `memory.retrieval_log.enabled`. Additive/idempotent |
| v32 | `retrieval_events.params` — the ranking knobs in force for the query (effective `top_k` / keep-threshold, the recency ramp, BM25 weight and scorer params, the reranker's fusion weight + margin gate and whether it actually fired, timeline/contiguity settings, and the call's filters), logged beside a widened `served` list whose per-entry `components` blob carries the fusion INPUTS: bi-encoder score, cross-encoder score (`null` when the margin gate skipped the pass — a distinction a learned head needs), BM25 boost, surprise, recency and the source/supersession multipliers. Phase 0 logged only the fused score, which is the output a Phase-1 learned head is supposed to predict; the inputs are not recoverable afterwards, because config is mutable at runtime and band recency, supersession flags and access counts all mutate on every serve. Nothing new is computed at serve time — these values were already in hand and were being discarded. `NULL` params = a v31-era row. Additive/idempotent |
| v33 | `slot_reads` + `entries.explicit_reinforcements` — read telemetry. `slot_reads` counts how many times each cortex slot was *served as an answer* (`memory_fact_get` and `memory_search`'s cortex-first block), keyed on the stable `(entity_norm, attribute_norm)` slot like `memory_traces` so counters survive cortex snapshot saves; deliberately uncounted are internal verification lookups (e.g. the dream rollback's post-revert check) and the facts attached to `memory_recall`/`memory_graph` neighborhoods (context, not a direct answer), so never-read is a lower bound. `explicit_reinforcements` moves only on `memory_reinforce`, splitting the deliberate "this was useful" signal out of the shared `reinforcements` counter, which also counts dream-trace links (and still feeds the retention formula unchanged). Both feed the new `read_audit` section of `memory_stats` (never-read fractions by age and source, read/write balance, slot coverage) — motivated by the 2026-08-26 bank audit, where entry reads were measurable but the 4.6k fact slots had no read signal at all. Kill-switch: `memory.cortex.read_tracking`. Additive/idempotent |
| v34 | `retrieval_events.served_facts` — the fact half of the reranker training tuple. The v31 event log recorded only served *entries*; the cortex-first block's facts, served above those entries in every `memory_search` response, were invisible to a future learned reranker. The search handler now attaches them (`[{entity_norm, attribute_norm, rank, score, kind, contested}]`) to the exact event row that search wrote, keyed by the event id `search(return_event_id=True)` hands back — no session-window guessing. `NULL` = a pre-v34 row or a search that served no facts. Also (no DDL): `memory_stats` `read_audit` gains `graduation_candidates` — entries served in ≥60% of the last 30 days' distinct sessions (once ≥8 sessions are on record), i.e. static-context ("promote to CLAUDE.md") candidates that retrieval keeps re-paying for per query. Additive/idempotent |
| v35 | `entries.authority` / `entries.distortion_tolerance` and `facts.authority` / `facts.distortion_tolerance` — the write-time label pair (authority collapse, arXiv 2608.01679; the compaction cliff, arXiv 2608.22752). `authority` is the SPEECH ACT of the text (`directive` \| `observation` \| `quoted`), deliberately a separate axis from the `origin` tier (who wrote — which drives supersession arithmetic and which entries never persisted anyway); `distortion_tolerance` is the fidelity class (`constraint` \| `procedural` \| `belief` \| `preference` \| `episodic`). Set at write time — explicit `memory_store` / `memory_fact_set` parameters, or a deterministic heuristic under the `auto` default that asserts only `constraint` (rule-sized deontic/imperative text) and `quoted`/`directive` — and inherited through `memory_supersede` / `memory_consolidate` / fact supersession unless the new write restates one. Consumers: the dream carries a `constraint` source's text verbatim onto a derived fact and a post-dream guard reports any constraint entry left without a verbatim carrier (`constraint_verbatim` / `constraint_misses`); a `quoted` source is low-trust for the two-man rule; `constraint` facts are pinned ahead of cosine in `memory_search`'s cortex block and `memory_recall` (`memory.cortex.pin_constraints`). `NULL` = observation / unlabelled, exactly the pre-v35 reading, so the migration is a no-op on an existing bank — no backfill, by design. Additive/idempotent |
| v36 | Review-queue autonomy (2026-09-02). `edge_proposals.judge_verdict` / `judge_confidence` / `judge_note` / `judge_model` / `judged_at` / `judge_relation` / `decided_by` / `decided_at` — the link judge's opinion on a pending link proposal (the retype verdict's corrected relation in `judge_relation`) and who settled the row; `entity_proposals.judge2_verdict` / `judge2_confidence` / `judge2_model` / `judged2_at` — the merge judge's SECOND opinion beside the v30 first one (two-vote agreement is the apply gate for rows the single-vote 0.8 reject gate leaves pending); and `curation_judgments` (`store`, sorted slot keys, verdict, keep, fold, confidence, note, model, judged_at) — the store-curation judge's memo, because the lesson/world duplicate listings are recomputed per pass and would otherwise be re-sent every sweep. `NULL` judge columns = not yet judged, exactly the pre-v36 behaviour, so the migration is a no-op on existing banks. Gates measured by `evals/queue_judge_ladder.py` against `evals/results/queue-judge-panel-20260902.json`. Additive/idempotent |
| v37 | Retire-not-delete (2026-09-03). `store_decisions` (`id`, `store`, `entity_norm`, `attribute_norm`, `action`, `decided_by`, `reason`, `record` JSONB, `decided_at`) — the FK-free audit of lesson/world forgets and restores. A `memory_forget(scope="lesson"\|"world")` now retires the slot's rows (`status='retired'`, rows kept; `memory.compaction` treats them like any non-live record) instead of deleting them, and the audit row carries the verbatim record so `lesson_restore` / `world_restore` (`memory_graph_review(action="restore_slot")`, `POST /api/lessons/restore`, `POST /api/world/restore`) still work after compaction has purged the retired row. Also (no DDL): merge and junk rejects write text-keyed tombstones to `dismissed_pairs` (canonical pair / `junk:<canonical>` self-pair) so a verdict outlives the CASCADE-deleted proposal row. No column changes; the table starts empty on an existing bank, so the migration is a no-op there. Additive/idempotent |
| v38 | Durable dream acknowledgement. `entries.dream_state` records `pending`, `acknowledged`, or `legacy-covered`; pre-existing rows retain `NULL` for one-time classification against the legacy cursor and configured source eligibility. New writes default to `pending`, regardless of their timestamps. Exact-entry commit tokens use a bank-local secret in `meta`; logical export excludes that secret. The numeric cursor remains display metadata. This preserves the previous migration boundary; it does not repair historical skipped entries. Additive/idempotent |
| v39 | `memory_trace_invalidations` preserves source-supersession events by normalized slot and source entry ID, without entry or fact foreign keys. Explicit correction records entry retirement and existing trace invalidations together. Events survive source deletion, cortex snapshots and compaction; confirmation still clears the served warning. Table creation and older logical imports reconstruct only surviving superseded source/trace pairs. **Upgrade effect:** the first v39 start materialises one event per surviving superseded-source trace pair — 2077 pairs on the reference bank on 2026-09-11, measured with `ops/measure_reverify_population.py`. That reproduces the warnings the bank already served, but from then on they no longer drain when the source is evicted or deleted; each clears only when its slot is confirmed again (`memory_fact_set` at the slot with the same or a new value, or accepting a contender). To clear a population deliberately, re-assert those slots. `re_verify` stays a passive flag and is still excluded from `correct_with`. Additive/idempotent |
| v40 | Agent coordination (2026-09-11). Adds `coordination_agents` for bearer-owned instances, hashed credentials, explicit scope, activity and adapter attachment generations, and `coordination_messages` for one-recipient mail, per-recipient ordering, sender request-key deduplication, expiry and acknowledgment. Agent rows have no episode FK; episode cleanup cannot remove mail. No embeddings or changes to memory tables. Both tables are operational data excluded from portable knowledge exports. Additive/idempotent; existing banks start with empty coordination tables and the feature remains disabled until configured. |
| v41 | Audited continuum entry reinstatement (2026-09-22). Adds `entry_reinstatement_decisions`, an operation-keyed, FK-free append-only audit that survives later entry deletion. A single Postgres transaction binds the reviewed retirement preimage to the decision and clears only the entry's retirement fields; retries use the operation UUID. The first version refuses entries with trace invalidations and leaves all cortex state unchanged. Additive/idempotent; existing banks start with an empty decision table. |
| v42 | Board audit log (2026-09-24). Adds `coordination_events`, an append-only, FK-free, sha256-hash-chained record of every agent-board mutation (register, update with the replaced values, attach, detach, send with its body, first read, ack, attempt, expire, prune, bank identity, restore recover/rebind), written in the mutation's own transaction and pruned only by its own `coordination.audit_retention_days` window (default 90, `0` keeps it forever), which logs its cuts. Adds `coordination_messages.first_read_at`. Operational data, excluded from portable exports like the other coordination tables; read and verified with `pseudolife-mcp board-audit`. The log is cut at most once a day, on UTC day boundaries, and only while the board is in use. Additive/idempotent; existing banks start with an empty log, and history before the upgrade is not reconstructed: a message still unacknowledged at the upgrade has no `send` event, and its first read afterwards is logged as its first read. |

Later additions that write into these tables without new DDL are listed with the feature that added them rather than as schema milestones: `memory_outcome(used_ids=[...])` (2026-09-05; every in-window serving event credited since 2026-09-08) labels served entries under `used_via="outcome"` — see the memory-model guide.

After running the entity-kind backfill (`evals/apply_entity_kinds.py --apply`), the daemon must be restarted for inference to take effect — it caches the entity-kind map for the life of its process.

A kind you set by hand is locked against later classifier runs — `evals/apply_entity_kinds.py --apply` overlays the model's labels onto the existing table rather than replacing it, so a deliberate marking is never reverted by a re-apply. The R@10 figures behind the v25 swap, and the rest of the shootout, are in [Benchmarks — embedding backbone](benchmarks.md#embedding-backbone--chosen-on-our-own-corpus).

### Extension schemas

A fork or downstream customization that adds its own tables should not
consume the next integer `schema_version` — that number belongs to this
repository's migration ladder, and claiming it guarantees a collision with
the next upstream release. The sanctioned pattern instead:

- **A namespaced marker key**: store the extension's lineage under its own
  `meta` key ending in `_schema_version` (for example
  `myext_schema_version = "v34-myext"`), leaving the integer
  `schema_version` untouched. Keys with this suffix are build-owned:
  the logical export/import skips them exactly like `schema_version`
  itself, so a marker never travels into a bank whose build does not
  provide the extension.
- **Additive, idempotent DDL** (`CREATE TABLE IF NOT EXISTS`, `CREATE OR
  REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`) applied
  after upstream's `ensure_schema` tail, so daemon startup converges on
  the same shape regardless of what version it last ran.
- **Explicit enumeration**: add the extension's tables to
  `BENCH_RESET_TABLES` (so bench tooling can reset them) and to the
  transfer CLI's `EXCLUDED_TABLES` (so ordinary bank transfer neither
  moves nor blocks on them); give them their own portable format if their
  data needs to travel.

Upstream migrations stay unaware of extensions by construction — an
extension that follows this pattern rebases cleanly across upstream
schema bumps, and an upstream bank that has never seen the extension
simply carries no marker.
