# Configuration

Every knob the daemon reads — environment variables, the tuned built-in
defaults, toolset tiers, the stdio shim, LAN sharing, data layout, and
backups. Part of the [user guide](../../README.md#documentation).

## Connection / deployment env vars

| Variable | Default | Effect |
|----------|---------|--------|
| `PSEUDOLIFE_MCP_DATABASE_URL` | _(unset → lite/file mode)_ | Postgres DSN; when set, PG is the source of truth (schema v52). Unset: with the `[lite]` extra installed the daemon auto-starts an embedded PostgreSQL and fills this in itself; otherwise v0.1 file-only mode (announced loudly at startup). |
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
| `PSEUDOLIFE_MCP_SHARED_HOST` | _(unset)_ | Set `1` on a stdio shim launcher serving multiple cloud/ChatGPT/Dot conversations without a supported per-conversation binding. It registers no board address and refuses mailbox operations, while memory and awareness calls remain available. Writer IDs `claude-desktop` and `tunnel` always use this guard. The operator setting takes precedence over Codex metadata and channel mode; it does not authenticate a caller. See [Delivery and recovery](#delivery-and-recovery). |
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

On the Docker tier `config.yaml` is `/data/config.yaml` in the daemon's
state volume (compose sets `PSEUDOLIFE_MCP_DATA_DIR=/data`; `docker exec
pseudolife-mcp-daemon cat /data/config.yaml` shows it), read at startup, so
a hand-edited value applies after the daemon restarts (`docker restart
pseudolife-mcp-daemon`), while a `PSEUDOLIFE_MCP_TOKENS` change is an
`ops/.env` change and needs the container recreated (`ops/update.*`, or
`docker compose -f ops/docker-compose.yml up -d --no-deps pseudolife-daemon`).

An `ops/.env` copied from a Windows host carries CRLF line endings. Compose
reads such a file fine, but a value the shell installer read from it ended
in a CR, and `docker volume create` refused `pseudolife-mcp-bank-pg18\r` as
an invalid name (Debian 13, 2026-09-29). `ops/install.sh` and
`ops/update.sh` therefore rewrite a CRLF `ops/.env` with LF endings before
reading it — in place, keeping the file's owner-only mode, with the
original kept beside it as `ops/.env.crlf-<stamp>` (gitignored like every
`ops/.env*`) — and print one line saying so; a file already on LF is not
touched. `ops/install.ps1` reads the file line by line and needs no
rewrite.

When deploying with `ops/update.ps1` or `ops/update.sh`, an explicit assignment
to either authentication variable in `ops/.env` makes that file authoritative
for both. Inherited client token variables cannot add an unintended fallback;
the launching shell's environment is preserved after the deployment command.

## Experimental agent coordination

Coordination adds peer awareness and addressed mail within one bank. It is on
by default, behind bearer authentication, and does not reserve files or prevent
conflicting edits. Configure it in the daemon's `config.yaml`, then restart the
daemon; `enabled: false` turns it off:

```yaml
coordination:
  enabled: true
  awareness_limit: 5
  allowed_principals: [editor, reviewer]
  daemon_notice_principals: []
  audit_retention_days: 90
  wake:
    per_recipient_per_hour: 20
    urgent_per_sender_per_hour: 6
    nightly_total: 200
    fan_out_stagger_seconds: 30
    active_seconds: 60
    nudge_interval_seconds: 3600
```

`audit_retention_days` is how long the [audit log](#audit-log) keeps each event:
a whole number of days, default 90, and `0` keeps the log forever.

`wake` caps the rings the daemon decides at send (schema v49; see
[park records and the wake decision](#park-records-and-the-wake-decision)).
Every ring is an unattended model turn in the recipient, so each is bounded:
`per_recipient_per_hour` counts every ring to one address (20, the figure the
Claude Code Stop hook already used); `urgent_per_sender_per_hour` (6) bounds
the `urgent` flag; `nightly_total` (200) is a rolling day of rings across the
bank; `fan_out_stagger_seconds` (30) spaces the rings from one sender's burst;
`active_seconds` (60) is the window in which a recipient's own last board
action makes new mail `hinted` rather than rung; `nudge_interval_seconds`
(3600) bounds the ring that asks an idle, unparked session to park. The
values are whole numbers; a cap of 0 rings nobody (or never honours
`urgent`), and the two windows are at least 1. Over a cap the send answers
`capped` with the cap's name.

Wake is on by default and policy-gated (maintainer decision, 2026-09-28), so
the caps are the guarantee, not the usual rate. `/health` reports the caps in
force, and `pseudolife-mcp doctor` prints them beside each registered
client's wake path (`stop_hook` for Claude Code, `doorbell` for Codex: `on`,
or `off` with the setting that turned it off, `off (no codex CLI)`; for
Codex also `off (PSEUDOLIFE_WRITER_ID is not codex)` and `off (no bearer
token)`, since the shim arms the doorbell only as the codex writer with a
bearer; for Claude Code `off (plugin not installed)` / `off (plugin
disabled)`, since only the plugin carries the Stop hook). The hooks and the
shim read these switches trimmed and case-insensitively, and a blank value
is unset. The client-side switches are
`PSEUDOLIFE_AGENT_COORDINATION=0` (the master off switch for that client),
`PSEUDOLIFE_AGENT_WAKE_HOOK=0` (the
[Stop hook](#waking-an-idle-claude-code-session-the-stop-hook)) and
`PSEUDOLIFE_CODEX_DOORBELL=0` (the [Codex doorbell](#codex-doorbell)).

`awareness_limit` must be an integer from 1 to 20 and caps peer summaries. Existing
episodes have no trustworthy project/task or principal fields, so unregistered
peers are shown with unknown scope and host capability. Titles do not establish
identity. Last reported activity comes from attributed writes; an open episode
does not prove a process is running. Refresh awareness before shared-resource
work and on resume.

The allowed names are principals from the bearer-token configuration above.
Without the key only `default`, the singular `PSEUDOLIFE_MCP_TOKEN` principal,
is admitted: principals of a `PSEUDOLIFE_MCP_TOKENS` map are separately
trusted identities and join the board only when listed (an explicit list
replaces the default; include `default` to keep it). An upgrade that already
authenticates clients through a map therefore finds them off the board until
they are listed. On the default setting, a shim whose bearer is unlisted
leaves coordination off at startup without an error, and memory keeps
working; with `PSEUDOLIFE_AGENT_COORDINATION=1` it shows a
coordination-unavailable hint instead. For Codex,
`ops/setup-codex-coordination.py --check` names this cause
([Codex CLI and desktop](#codex-cli-and-desktop)). Mailbox operations
require PostgreSQL, configured
bearer authentication and a registered adapter's private instance credential.
Two sessions sharing a principal still need distinct adapter identities. A
public agent ID, episode handle or task label never grants mailbox access.
Clients lacking a per-session credential-injecting adapter can use awareness,
but cannot send, receive or acknowledge another instance's mail. Awareness is
gated on the same allowed-principal list: a bearer whose principal is not listed
sees no peers and no awareness section in its briefing, and with no bearer token
configured there is no principal to list, so the board stays dormant on an open
loopback install: no awareness, no mail and no startup check-in. The
installers mint a token by default; see [Turning the board on](#turning-the-board-on).

`daemon_notice_principals` (default empty) names the principals that may
post a board notice as the daemon itself through `POST /api/daemon-notice`:
the unattended updater's "updated" or "held off" notice (see
[unattended daemon updates](#unattended-daemon-updates-on-headless-hosts-updatesunattended_daemon)).
It is a separate list because `allowed_principals` admits `default`, the
principal every ordinary session uses, and a notice that reads as the
daemon's must not be something any session, or an agent steered by text it
has read, can send. While it is empty every such notice is refused, and
the refusal names this key. To turn the notice on, give the scheduled run
its own principal: a `PSEUDOLIFE_MCP_TOKENS` entry `<token>:updater` in the
daemon's environment (the map works beside the singular token), `updater`
in `daemon_notice_principals` and in `allowed_principals` (the run reads
the board as well; an explicit list replaces the default, so write
`[default, updater]` to keep your sessions on it), and that token as the
scheduled run's bearer. The reserved `daemon` principal is never admitted
as a caller, whatever either list says, and each notice carries a line
naming the principal that posted it.

The installed shim starts its adapter (for Codex, its per-thread registry) by
default when it holds a bearer token (`PSEUDOLIFE_MCP_TOKEN` or
`PSEUDOLIFE_MCP_TOKEN_FILE`) and the daemon serves that bearer the board; it
asks at startup and otherwise stays quiet. A question the daemon does not
answer (no connection, a timeout, a 5xx, 408 or 429) is not a no: a Claude Code session's
shim asks again in the background on the registration retry schedule below,
registers once the answer is yes, and stays quiet if it is no. A Codex shim
still reads no answer as no for that process. `PSEUDOLIFE_AGENT_COORDINATION=0`
(any value but `1`, `true`, `yes` or `on`) turns it off for that client, and
`=1` skips the question and reports any refusal on stderr.

The startup check-in follows the board. The daemon serves the hook text
(`GET /api/hook/coordination-start`) only where the board is on for that
bearer: enabled, authenticated, a listed principal, PostgreSQL. The plugin
hook and the installers' `pseudolife-mcp briefing --coordination` hook print
what it serves, and the shim appends a compact check-in to the MCP
instructions only when its adapter is up. The daemon cannot see whether a
client has an adapter, so a check-in can still reach one that cannot complete
it: a client connected over HTTP without the shim whose hooks hold a token; a
client that opted out only in its MCP env block (set the opt-out where the
hooks see it too, since they cannot read that block); and a Docker install
wired by `ops/install-hook.*`, whose `docker exec` check-in asks with the
daemon container's own token. The check-in tells the agent to say so and
continue when the tools are unavailable.

Since 2026-09-28 the check-in also says when a message is due, not only
how to send one (a review of six sessions had found 15 status updates, 9
peer lists and 7 receives against no sends until a human asked for one). It
carries two field-neutral rules, the two of five candidates that changed
decisions in `evals/coordination_checkin_bench.py` (see `evals/README.md`):
before using something shared, look for whoever holds it or has it
booked, and if someone does, message them that you are next, even when
their status says when they expect to finish, because a status line is not
a queue (if the board shows it free, use it and say so in your status); and
keep your status true (what you hold, what you wait on, when you expect to
finish). The Codex form, inside its
512-character MCP-instructions budget, carries the first, bounded the same
way. An install's own
words for its shared things (a suite lock, a GPU, a style guide, an API
quota) belong in the daemon's `<data_dir>/hook-instructions.md`, which the
memory hook serves after its core (3.5 KB cap, see
[Startup memory policy](#startup-memory-policy-memory_policy) below);
[`examples/hook-instructions.md`](../../examples/hook-instructions.md) is
one host's copy (suite and GPU status words, the lease holder, a
pre-flight before a full run). For the Docker install the file goes into
the daemon's data volume (`/data` in the container). The hook reads it at
every session start, so no restart is needed:

```bash
docker cp examples/hook-instructions.md pseudolife-mcp-daemon:/data/hook-instructions.md
```

`evals/coordination_checkin_bench.py` measures whether the text moves the
send/no-send decision (see `evals/README.md`). Optional
`PSEUDOLIFE_AGENT_LABEL`, `PSEUDOLIFE_AGENT_PROJECT` and `PSEUDOLIFE_AGENT_TASK`
provide explicit display and relevance fields. For clients other than Codex,
set `PSEUDOLIFE_AGENT_STATE` to a
private file outside the repository for deliberate mailbox resume. Each concurrent
adapter needs its own state file; sharing one does not create a second identity.
Claude Code sessions use `PSEUDOLIFE_AGENT_STATE_DIR` instead, a private
directory. When the installers register Claude Code with a token file, they
set it beside the token to `~/.pseudolife-mcp/claude-code-agents`, keeping a
value the registration already has (see
[Turning the board on](#turning-the-board-on)); a registration made by hand
sets it itself. The shim keys one state file under it by the
`CLAUDE_CODE_SESSION_ID` Claude Code launches it with, so concurrent sessions
never share one and `claude --resume <id>` returns to the session's address.
That id is fixed for the shim's lifetime: `/clear`, compaction or an
in-session `/resume` keeps the running shim and its address. `claude
--continue`, or `--resume` without an id, may launch the shim with the
process's startup id instead of the resumed one, and the session then gets a
new address. A state-backed address registers as resumable and is kept for
seven days after its last activity or lease (longer while a retained message
names it), so the board holds about a week of sessions; a session resumed
after its address was removed registers a new one and keeps the old state
file with a `.stale` suffix. Without a state file or directory,
each launch gets a new address, registered as not resumable and retired an
hour after it goes quiet. Never infer recovery from a
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
Code, whose per-session stdio shim does receive the id. That shim serves the
session's calls only while Desktop's app-level entry has a different name, so
the installer names that entry `pseudolife-desktop` (see
[Claude Desktop](providers.md#claude-desktop)).

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

### Turning the board on

A default `ops/install.sh` / `ops\install.ps1` run turns the board on:

1. When neither `PSEUDOLIFE_MCP_TOKEN` nor `PSEUDOLIFE_MCP_TOKENS` is set in
   `ops/.env` or the installer's environment, it writes a random
   `PSEUDOLIFE_MCP_TOKEN` to `ops/.env`, makes that file owner-only, and never
   prints the value. The daemon's `default` principal is on the default
   `allowed_principals` list.
2. It writes an owner-only token file per shim client it wires,
   `~/.pseudolife-mcp/claude-code.token` and `~/.pseudolife-mcp/gemini.token`.
   A `PSEUDOLIFE_MCP_TOKENS` map entry for that client's principal
   (`claude-code`, `gemini`) wins over the singular token. List a map's
   principals in `allowed_principals`.
3. It registers Claude Code with `PSEUDOLIFE_MCP_TOKEN_FILE` (re-read on every
   call, so a rotation needs no restart), `PSEUDOLIFE_MCP_DAEMON_URL` and
   `PSEUDOLIFE_AGENT_STATE_DIR` (`~/.pseudolife-mcp/claude-code-agents`, which
   keeps a resumed session's board address), and Gemini CLI with the first two.
   Codex and Claude Desktop get their own credential files, as before.
4. With the Claude Code plugin, it sets `PSEUDOLIFE_MCP_TOKEN_FILE` and
   `PSEUDOLIFE_MCP_DAEMON_URL` in the `env` block of `~/.claude/settings.json`.
   The plugin's hooks read the Claude Code process environment, not the MCP
   registration, so without it they are refused; beside a Codex connection
   file they also refuse a token file without the matching URL. It leaves that
   block alone when it, or the installer's own environment, already sets
   `PSEUDOLIFE_MCP_TOKEN` or `PSEUDOLIFE_MCP_TOKEN_FILE`, and never replaces a
   URL already there.

An existing install gets the same by re-running the installer. After it
upgrades the shim behind an existing Claude Code registration, it adds the
three settings to that registration in place (a backup of `~/.claude.json`
is taken first) and keeps any credential the registration already has; restart
Claude Code sessions to load them. A custom or HTTP registration is left alone
with a warning: a token-gated daemon refuses an HTTP registration, which cannot
carry a token file, so register the stdio shim instead. An existing Gemini CLI
registration is always left alone with a warning naming the fix, since
`gemini mcp list` shows no environment and the installer cannot tell whether it
already carries a token.

The installer's final ladder and `pseudolife-mcp doctor` (its `board` field,
run from the registered command's environment) print one line: `on - token
present, principal allowed`, or `off - ` and the daemon's reason. The reason
comes from the `X-PL-Board` header on `GET /api/hook/coordination-start`
(`disabled`, `authentication_required`, `unauthorized`,
`principal_not_allowed` or `coordination_requires_postgres`).

To keep an open-loopback install with the board dormant, pass `--no-token`
(`-NoToken`). `--transport http` mints no token either, nor does a host that
cannot install the shim (no Python >= 3.10 that can make a virtualenv, and no
pipx), since its registrations fall back to HTTP. None of these removes a
token that is already configured.

### Coordination diagnostic quickstart

Run `pseudolife-mcp doctor --host codex` (or `--host claude-code`) from the
registered command's environment. The `coordination` snapshot separates
daemon reachability, bearer admission, saved-instance registration, advertised
tools and configured wake. `ok` remains the runtime/MCP check; selecting
`--agent-state` additionally requires successful registration verification.
`generic` reports unsupported idle wake and next-turn pull; `claude-desktop`
reports no per-conversation mailbox. A healthy endpoint or configured doorbell
does not establish enqueue, host wake, receive or ack.

To verify an existing mailbox without attaching or modifying it, pass
`--agent-state <private-saved-instance.json>`. Doctor uses a nonce proof through
the context endpoint's `read_only` mode, checks a version-2 file's bank/principal
binding and never transmits its instance credential. A missing or invalid file,
rejected bearer, changed binding, unavailable bank or older daemon without this
capability remains a separate diagnostic. Cold storage is not initialized by
this check. Saved state and client settings are preserved; default doctor never
registers, receives, acknowledges or starts a daemon.

For a complete **fixture** proof, explicitly select a disposable PostgreSQL
server with pgvector and CREATE/DROP DATABASE permission, then run:

```sh
PSEUDOLIFE_TEST_DATABASE_URL='postgresql://fixture@127.0.0.1:6543/postgres' pseudolife-mcp doctor --disposable-proof
```

In PowerShell set `$env:PSEUDOLIFE_TEST_DATABASE_URL` to that disposable fixture
DSN before running the same command. There is no configured-bank or bench-server
fallback. The command creates a fresh tagged bank, uses the real authenticated
ASGI API and adapters to enqueue, observe a hint or synthetic listener ring,
receive and acknowledge, then drops only the bank it created. It never reads
saved client settings or uses production agents. The JSON reports individual
stages and successful fixture removal. `harness_ack` is acknowledgment by the
test harness; `host_delivery` and `model_ack` remain `unverified`. Console fixture state lives in the proof's temporary directory. `--timeout` bounds the async proof, excluding synchronous bank/schema setup and cleanup; shutdown can extend that budget. Cleanup uses fresh admin connections to the explicitly selected fixture server, with a separate 30-second DROP statement budget per attempt (two attempts); if both attempts fail, the report includes `fixture_removed: false`, the exact generated `fixture_bank` and a `DROP DATABASE` instruction for that owned bank only. Actual host
acceptance still needs a separately isolated supported-client session.

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

Each waiter owns a separate `<key>.<uuid32hex>.wait-armed` marker containing
its owner token and a listener epoch renewed on each poll. The adapter
aggregates unexpired listener records and reports that evidence to the daemon
at attach and heartbeat. Exit clears only the waiter's own marker, so one
waiter's exit does not disarm another; adapter teardown leaves these records
alone. An interrupted process's evidence expires within 60 seconds.

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

The Stop hook alone watches for at most 59 minutes. A park record can outlive
that watcher, so for a longer wait arm one main-session background
`pseudolife-mcp wait-mail --timeout 14400` and re-arm after mail or the
four-hour timeout. Where Codex supports its doorbell, that is its durable host
path. Where the host provides neither path, record `next-turn-only` in the
park or status: a durable park is not evidence of a durable listener.

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

### Leases: `pseudolife-mcp lease`

Awareness and mail say who is working on what; they do not stop two agents
starting the same GPU job or full test suite at once. A lease does: a named,
expiring hold on a shared resource, taken around any command.

```sh
pseudolife-mcp lease run NAME [--expect DURATION] [--ttl SECONDS] [--purpose TEXT] [--no-board] [--timeout DURATION] -- COMMAND [ARGS...]
pseudolife-mcp lease hold NAME --while-pid PID [--expect DURATION] [--ttl SECONDS] [--purpose TEXT] [--worktree PATH] [--no-board] [--timeout DURATION]
pseudolife-mcp lease check NAME [--json]
pseudolife-mcp lease list [NAME] [--json]
```

What excludes is an OS file lock, `~/.pseudolife-mcp/locks/lease-<NAME>.lock`
(`PSEUDOLIFE_LEASE_LOCK_DIR` overrides the directory; characters outside
`A-Za-z0-9._-` become `_`, plus a short hash of the name). The OS releases it
the moment the holding process exits or dies, so a crash leaves nothing stale.
With a bearer token (`PSEUDOLIFE_MCP_TOKEN` or `PSEUDOLIFE_MCP_TOKEN_FILE`) and
a daemon whose board is on, the board mirrors the lock: the run registers a
short-lived address (retired an hour after its last activity), queues for
`NAME` in arrival order, reports its position and the holder on stderr about
once a minute, then takes the OS lock and runs the command, renewing the board
lease every third of `--ttl` (default 120 s, from 30 s to a day). When a lease
frees, the head of the queue has 300 s to take it before the board passes it
on. `--expect` sets the expected end the board shows, counted from the grant
and marked stale once past; every call repeats the same value, which leaves it
alone. `--purpose` says what the lease is for.

Without a token, with the daemon unreachable, the board off or refused for this
bearer, or with `--no-board`, the run says once why and waits on the OS lock
alone: polled every 2 s, not in arrival order. The board never stops a command
from running: a board that keeps failing for two minutes is dropped the same
way, and a renewal that finds the lease lost warns once while the command
continues under the OS lock. A lock held by something the board does not show
(a `--no-board` run) delays a board holder until it is freed.

The command inherits the terminal and the environment, plus
`PSEUDOLIFE_LEASES_HELD` (comma-separated names, appended to any inherited
value). Exit codes: the command's own; `75` when `--timeout` (`90`, `90s`,
`20m`, `2h`) expired before the lease was held, and the command did not run;
`128+N` when stopped by signal N (`130` Ctrl-C, `143` SIGTERM, `129` SIGHUP);
`64` for a run nested inside a run of the same lease (its name is in
`PSEUDOLIFE_LEASES_HELD` while that lease's lock is held), which would
otherwise wait for itself forever; `2` a usage error; `71` an unusable lock
file; `126` or `127` a command that cannot start or is not found.

A stop never releases the lease under a running command. Ctrl-C reaches the
command too, so it first gets 10 s to clean up on its own; SIGTERM or SIGHUP
sent to the `lease` process is forwarded to it. After that it is interrupted
(POSIX only), terminated and killed, 10 s apart. The `lease` process holds
the lock, not the command: kill it outright (SIGKILL, Task Manager) and the
lock goes while the command may run on. On a case-insensitive filesystem,
names that differ only in case share one lock file, which can only make one
wait for the other.

`lease hold NAME --while-pid PID` is the lease for a process the command did
not start and cannot wrap: a launcher starts a detached server, then starts a
hold that lasts as long as the server's pid does. The order is the reverse of
`run`, because the process already owns the resource: the OS lock first (a
lock another process holds is waited for, or given up on at once with
`--timeout 0`, exit `75`), the board second, and both are released when PID
exits, or when the hold is stopped (`128+N`), which leaves PID running.
`--worktree` names the checkout in the notices below, by its name, never its path (a path names the OS user); `--expect` is the
expected end the board shows. All board traffic (the lease, its renewals, the
notices below, the release) runs on a thread of its own after the OS lock has
moved, so a slow or failing daemon never delays the lock; the release side gets
20 seconds, after which the board record is left to lapse at its ttl.
`evals/qwen_server.ps1` uses it around the bench server: `Start-Qwen` refuses to
launch while `lease check gpu` says the lease is held (beside its VRAM
busy-check, which stays the guard against anything that takes no lease), and
right after it launches a server it holds `gpu` for that server's pid, so the
lease covers the model load. It checks the hold a moment later and says so if
the hold could not take the lock (another process got it since the check) or
could not run. It runs the CLI from the checkout (`$env:PSEUDOLIFE_LEASE_PYTHON`,
else the checkout's `.venv`, else `python` on PATH, each as
`-m pseudolife_memory.cli`), and only then a `pseudolife-mcp` on PATH.

`lease check NAME` is the launch gate for an orchestrator, in place of watching
process CPU: it prints the local lock's state and the board's holder with the
expected end, and exits `0` when the lease is free, `1` when it is held, and
`70` when the check itself failed, which a gate must not read as held
(`--json` for one report). It is held when the local lock is held, or when the
board shows a holder. The one exception: a `lease hold` or suite mirror whose
board label carries this lock directory's instance id (`lease-hold@<id>`; the
id is 12 random hex digits in `instance.id` beside the locks, so no host or
user name reaches the board) beside a free local lock outlived its process
(killed outright), is shown as stale, and lapses at its ttl. Any other board
holder, such as a session that claimed the lease with `memory_agents`, a
`lease run`, or a hold from WSL or another machine or account, counts as held
until it is released or lapses, since no local lock can speak for it.
For `full-suite` it probes the test suite's own lock (`full-suite.lock` and its
slots, with the holder record's pid, worktree and start time), since that file,
not `lease-full-suite.lock`, is the truth for a full run. A run with
`PSEUDOLIFE_SUITE_LOCK=off` takes no lock, so no check sees it.

The test suite's lock is mirrored the same way. A full `pytest` run
(`tests/conftest.py`, `tests/suite_lock.py`) holds the board lease `full-suite`
behind its OS lock: while it queues it is a board waiter, once it holds the lock
it holds the lease, with the run's pid and worktree as its purpose and an
expected end from the median of the last five timed runs
(`~/.pseudolife-mcp/locks/full-suite.durations.jsonl`; 25 minutes until five
are on record). Only a run that ran its tests, passed or failed, is timed: an
interrupted run or a collection error would drag the median down. The lock is
freed first and the board told after, and a run that leaves the queue without
the lock (refused, a changed tree, Ctrl-C) gives its board place back. When the
lock's holder is a run the board does not show (older code, no bearer), the
board grants the lease to the first waiter; that waiter hands it back and asks
no more until it holds the lock, so the board never names a queued run as the
holder. The OS lock stays the truth: a board that is unreachable, refuses, or shows another
holder costs one line and never delays or stops the run, and
`PSEUDOLIFE_SUITE_LOCK=off` (CI) takes neither. The run's bearer and daemon URL
are read when conftest is imported, before the suite's own client isolation
strips them, and the mirror is built only for a run that takes the lock.

Acquiring and releasing `hold` and the suite's mirror send one notice each
(`LEASE NAME acquired: pid, worktree, expected end` / `LEASE NAME released:
pid, worktree, held for`) by board mail to the peers the lease concerns, in
the same project (compared without case; every project when the sender has
none set): live agents (attached, or registered without an adapter) whose
status says `suite=running`, `suite=queued` or `gpu=`, and any agent parked
with `park_clear_by` naming the lease while the park stands (a reason set, and
`park_expires` not yet passed), attached or not, since mail waits for a parked
session. The release notice rings such a session, subject to the wake path
and caps: for 60 seconds after a hold ends, the daemon counts its last holder
as the clearer the lease name stands for (taking a lease clears nothing, so
the acquire notice waits in the queue). Both leases go to both status groups
on purpose: a GPU server beside a full suite is the contention. At most 20
peers are told per event; one refused send does not stop the rest. The
notices are automatic and need no reply; they replace hand-written
SUITE-START/SUITE-END notes.

`lease list` shows each board lease (holder, purpose, age, expected end,
queue) beside the local lock files, each probed held or free, and whether the
test suite's own lock (`full-suite.lock`, which leases never take) is held.
Without the board it shows the local state with a one-line note. The instance
credential stays inside the `lease` process: it is never printed or passed to
the command.

`pseudolife-mcp lease break NAME` is the operator's way to free a lease whose
holder will never release it, such as a dead session's day-long claim. It opens
the bank directly, as `board-audit` and `export` do
(`PSEUDOLIFE_MCP_DATABASE_URL`, else the lite tier's data dir), grants the
lease to the next waiter, and logs a `lease_break` with the operator as its
actor. It frees the board's record only: a process still holding the local lock
keeps it until it exits.

Sessions hold leases too, from the model's side, with no process and no OS lock
behind them. `memory_agents(action="claim", lease=NAME, status=PURPOSE,
expect=SECONDS)` takes or queues for a session-held lease, such as
`coordinator:<project>` or `claim:<path>` for a work area; claiming again renews
it, and `action="release"` frees it or leaves its queue. A `claim:` lease lasts
a day between renewals, any other an hour. A claim is advisory: it tells peers,
it blocks no edit. A queued session is not told when its turn comes: it sees
the grant the next time it lists or claims, and must renew within the same
300 s window. `memory_agents(action="list")` carries the held and queued leases
(resource leases before claims, and `leases_truncated` when the page cut some
off), and `memory_agents(action="update", status=..., expect=SECONDS)` gives a
status an expected duration: past it the peer list marks the row
`status_overdue`. Over REST these are the coordination actions `lease`
(acquire, renew, or queue once), `release`, and `leases`, a listing that needs
only the bearer.

#### Exact repository file claims

Through the stdio shim, use `memory_agents(action="claim",
worktree="/absolute/checkout", path="src/file.py", status="editing")`.
On Windows the worktree can be `C:/workspace/project`. Release with the same
`worktree` and `path` and `action="release"`. These inputs replace `lease`.
The shim resolves the Git common directory locally and hashes its canonical
directory path and filesystem device/file IDs, so linked worktrees use one repository
identity while separate clones, even of the same remote, do not. Filesystems
without a nonzero file ID are unsupported. IDs are local to the filesystem
view: cross-host or Windows/WSL identity equivalence is not guaranteed.
The daemon receives a hashed `repository_id` and a relative path; it
never opens the checkout. Direct HTTP tool clients must call
`pseudolife_memory.repository_claims.prepare_file_claim(worktree, path)` on
their own host and pass its `repository_id`/`path` fields. Raw `worktree`
inputs sent directly to the daemon return `file_claim_requires_local_client`.

Paths accept `/` or `\`, repeated separators and `.` components, and use
Unicode NFC. They reject absolute/drive paths, any `..` component, Git
metadata, control characters, globs, Windows devices/streams and trailing
dots/spaces. Windows uses conservative case folding, including case-sensitive
NTFS directories; POSIX preserves case unless the common Git config sets
`core.ignorecase=true`. Worktree-local overrides do not change that shared
policy. Keep the common case policy unchanged while claims are held.
Windows DOS short-name syntax (`~` followed by a digit) is unsupported.
Existing directories, special files, symlinks and Windows reparse points (including
junctions) are refused; aliases are refused even when their target is inside
the checkout. Missing paths are accepted, so a deletion keeps its claim.
For a rename, claim both the old and new names separately; moving or deleting
a file does not release either claim. Hard links are separate path names.
If a path becomes unsupported after acquisition, release using the returned
`repository_id`/`path` or lease key without repeating local file validation.

The result includes `file_claim` with the prepared identity/path. Its opaque
`claim:file:<hash>` lease key uses the same FIFO queue, day-long renewal TTL
and five-minute acceptance window as other claims. A queued response reports
the conflicting `holder`'s `agent_id`, `principal`, `fence` and `expires_at`;
the caller's top-level `fence` remains null until it holds the lease. Listings
show the opaque key, holder and queue, not a persisted path mapping. Exact
claims cover only that normalized path, not a directory's descendants.
Filesystem validation is a snapshot, and later edits can change the path's
type or target. Claims remain advisory: no edit, commit or push guard consumes
the fence. Existing literal `claim:<text>` and generic resource leases keep
their semantics and do not overlap repository file claims.

### Codex CLI and desktop

Use the ordinary stdio shim with `PSEUDOLIFE_WRITER_ID=codex`. Codex supplies
`_meta.threadId` on MCP tool calls; the shim uses this validated task UUID for
attribution and lazily attaches the task's mailbox on its first call. CLI and
desktop runtime probes confirmed that this metadata survives resume and changes
on fork. Process environment variables, checkout names and titles do not select
the mailbox. Missing or malformed metadata leaves ordinary memory available
without attaching a mailbox.

A native subagent (`collaboration.spawn_agent`) is a thread of its own, so it
gets its own address too, and since schema v50 that address is linked to its
parent's. Codex 0.158.0 (CLI and desktop, measured 2026-09-29) sends
`x-codex-turn-metadata` beside `threadId`; for a subagent it carries
`thread_source: "subagent"`, the child's `thread_id` and the spawning
thread's `parent_thread_id`. The shim accepts that parent only when the
metadata's thread is the call's validated `threadId` and the parent is a
different canonical UUID, and passes it when the thread registers; anything
malformed is ignored and the call goes on. A user's fork of a conversation
carries no subagent source and stays a peer. The daemon links the child to
the parent's row under the same principal (only among Codex thread rows),
at once or when the parent makes its first call later; peers see
`parent_agent_id` on the child. A subagent does not send board mail: its
parent does ([Delivery and recovery](#delivery-and-recovery)).

For an existing Codex stdio registration, run the following from a checkout using
the Python environment where Pseudolife is installed:

```sh
python ops/setup-codex-coordination.py --credentials
python ops/setup-codex-coordination.py --check
```

The check is read-only. Coordination is on by default, so a registration
without `PSEUDOLIFE_AGENT_COORDINATION` reports `ready (default-on)` when the
daemon serves the board to its bearer, the same question the shim asks at
startup. Nothing more is needed then. When it reports `needs-configuration`
because the daemon refuses the bearer's principal, its `reason` says so
(below). The report also names the wake path the shim would take with this
registration, read the way the shim reads the switches and `pseudolife-mcp
doctor` reports them: `wake` is `live` (the [app-server
bridge](#optional-codex-live-delivery)), `doorbell` (the [Codex
doorbell](#codex-doorbell), on by default since 2026-09-28, so a ready
registration with a `codex` CLI reports it) or `pull-only`, and
`wake_reason` says why: the switch that turned it off
(`PSEUDOLIFE_AGENT_COORDINATION=0`, `PSEUDOLIFE_CODEX_DOORBELL=0`), `no codex
CLI`, `no bearer token`, `PSEUDOLIFE_WRITER_ID is not codex`, a fixed
`PSEUDOLIFE_AGENT_STATE`, a server disabled in Codex, or the board question
the daemon answered no to.

`python ops/setup-codex-coordination.py --enable` is only for pinning explicit
mode (`PSEUDOLIFE_AGENT_COORDINATION=1`), after which the check reports
`ready (explicit)`. In explicit mode the shim skips the startup question and
keeps the adapter's own diagnostics. Enable also sets `PSEUDOLIFE_WRITER_ID=codex`
and `PSEUDOLIFE_MCP_NO_SPAWN=1`. It leaves `PSEUDOLIFE_AGENT_WAKE` as it is:
unset means no live delivery (the doorbell still rings by default), and a
live-delivery opt-in survives a re-run. Enable
requires a configured bearer and an enabled daemon that allows its principal;
it does not change the daemon's authentication or allowlist. The token must be
in the MCP registration's `env`, or explicitly forwarded through `env_vars`.
Setup preserves the command and unrelated settings, backs up the configuration
privately, and uses Codex's versioned configuration writer. Reconnect the MCP
server after changing its environment.

A Codex bearer from a `PSEUDOLIFE_MCP_TOKENS` map (principal `codex`, say) is
off the board until an operator lists it: without `allowed_principals` only
`default` is admitted. On the default setting the shim's startup question
then leaves coordination off with no error, and memory keeps working. In
explicit mode each task instead gets an "identity attachment unavailable"
hint. In either mode the check reports
`principal not allowed on the board (add 'codex' to coordination.allowed_principals in config.yaml)`.
Add the principal the map gives that bearer, keeping `default` if the
singular token should stay on the board, and restart the daemon:

```yaml
coordination:
  allowed_principals: [default, codex]
```

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
mailbox's, instead of failing whole. An ambiguous prefix in a batch refuses
the whole call, since acknowledging the wrong message cannot be undone.

A send names its recipient by agent id, or by a unique prefix of it of at
least 8 hex characters, the length every surface shows: a prefix that matches
several ids is refused with `ambiguous_recipient` and the candidates cut to
the shortest prefixes that tell them apart, and one that matches none fails
as the full id would (`recipient_not_found`). `reply_to` and `ack` take
prefixes the same way, resolved among the caller's own mail
(`ambiguous_reply`, `ambiguous_message_id`), so another mailbox's ids
neither resolve nor make a prefix ambiguous. A direct send's receipt names
the `recipient_agent_id` it resolved to. `to: "project:<name>"` sends to
every attached, non-idle agent in that project except the sender, and
`to: "all"` to every attached, non-idle agent on the board except the
sender: the peers the list shows with `adapter_available`. One request id
covers the burst, so a retry with it returns the same result: `recipients`
and one receipt per recipient with its `message_id`, `recipient_agent_id`
and a `wake` decision (`live` for an attached peer that opted into wake,
which the daemon rings; `pull` for one that reads at its next receive or
digest). The burst is atomic and refused whole, writing nothing, above 50
recipients (`fanout_too_large`), when nobody is reachable
(`no_recipients`) or when one mailbox is full (`queue_full`, naming that
mailbox's prefix); it counts once against the sender's rate, and a reply
cannot ride it. Each recipient gets its own message and its own audit event.
A refusal that carries such a detail surfaces it after the code in the MCP
tool's error (`ambiguous_recipient: 518a3e67aa, 518a3e67ab`) and as a
separate `detail` field beside `error` on REST.
These calls work in the CLI and desktop without live wake support.
Codex hook setup (`ops/setup-codex-hooks.py`, or the installer's Codex step)
asks for the `memory_message` approval together with the hooks. Its prompt
names it, and a yes, or `--trust yes`, sets
`tools.memory_message.approval_mode = "approve"` on the `pseudolife-memory`
server in Codex's user configuration, through Codex's own config writer and
after a backup. A no, or `--trust no`, leaves it as it is. A value you chose
yourself (`"prompt"`, say) is kept, and an existing `"approve"` is left
alone. The JSON report's `mailbox_approval` says which happened: `set`,
`already`, `kept-explicit`, `declined`, or `unavailable` (no
`pseudolife-memory` server in the user configuration, or Codex refused the
write; `mailbox_approval_detail` gives the reason, and hook setup still
completes). When the approval is not set, the end-of-setup notice says how to
choose it. The reason is the doorbell: a board ring wakes a thread only when it
has mail it needs, and a woken thread that must ask before calling
`memory_message` stalls on that prompt. A recipient running with approval
policy `never` cannot execute a tool that still requires approval.

The trade-off is plain: Codex approves per tool, not per action, so the same
approval lets the thread send board mail without asking. Board mail is
rate-limited, audited and expires, cannot grant permissions, and reaches only
allowed principals. The approval covers that one tool; it does not approve file
writes, commands or other tools. To set it by hand, configure the installed
server's `tools.memory_message.approval_mode = "approve"` in Codex; without
it, use the host's normal approval flow.

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

### Codex doorbell

Codex starts no turn for MCP notifications, hooks or finished background
commands, so without the bridge a Codex task sees new mail only at its next
Pseudolife call. The doorbell wakes an idle task, desktop app included,
through Codex's own `codex queue` command. That command persists a message which
every app-server sharing the Codex home dispatches to the task once it is loaded
and idle; app-servers poll for it about every 10 seconds.

It is on by default since 2026-09-28 (before that, opt-in with
`PSEUDOLIFE_CODEX_DOORBELL=1`) whenever the shim finds a `codex` CLI and the
coordination adapter is up (on by default with a bearer token, off with
`PSEUDOLIFE_AGENT_COORDINATION=0`, which stays the master off switch). It
rings only when the daemon decides a message should wake the task: the task
is parked with a declared need and the message plausibly clears it (see the
`wake` caps under [Experimental agent coordination](#experimental-agent-coordination)).
Set these in the Pseudolife MCP server's environment, next to
`PSEUDOLIFE_AGENT_COORDINATION`, to turn it off or to name the CLI:

```toml
# Opt out (any value but 1/true/yes/on):
PSEUDOLIFE_CODEX_DOORBELL = "0"
# Optional: an absolute path; otherwise `codex` is looked up on PATH, then
# in the desktop app's own bin directory.
PSEUDOLIFE_CODEX_BIN = 'C:\path\to\codex.exe'
```

Reconnect the MCP server afterwards; the setup helper does not set either
value. The lookup uses absolute PATH directories only, never the working
directory (the task's checkout), so a repository cannot supply its own
`codex`; on Windows it then looks in the desktop app's
`%LOCALAPPDATA%\OpenAI\Codex\bin\<build>\codex.exe` (newest build), so a
desktop-only install rings too. A `PSEUDOLIFE_CODEX_BIN` that is relative or
does not exist turns the doorbell off rather than falling back to PATH. With
no CLI found the default stays quiet and falls back to pull delivery;
`pseudolife-mcp doctor` reports `doorbell: off (no codex CLI)`, and an
explicit `=1` says so on stderr. With a non-default Codex home, give the
server `CODEX_HOME` too, in its `env` or through `env_vars`: Codex does not
necessarily pass it to MCP servers, and without it `codex queue` writes to the
default home's queue, which no app-server of the task's home reads.

A woken task reads its mail with `memory_message receive`, so the task needs
`memory_message` approved in Codex's tool configuration; without that
approval a woken task stalls on an approval prompt until someone answers it
(the 2026-09-12 validation record's complete-path test approved it explicitly).
Hook setup sets that approval when you approve the hooks
([Codex CLI and desktop](#codex-cli-and-desktop)).

- **When it rings.** After each 20-second heartbeat the task's adapter reports its
  pending mail. The shim runs `codex queue --thread <task id> --message <notice>`
  only when new addressed mail has arrived, the task has made no Pseudolife call
  for 30 seconds, neither a tool-result hint nor the prompt hook has shown that
  mail, no earlier doorbell is still unanswered, and (v49) the daemon decided
  a ring for it ([the wake decision](#park-records-and-the-wake-decision)):
  the adapter offers the decision once its `ring_at` has come, and the
  doorbell takes it only at the moment it would ring, so an active or
  informed task never spends it. Without a decision (chatter to a parked
  task, or a daemon older than v49) the arrival stays owed and nothing
  rings; a later decision for the task covers it. A successful
  `memory_message receive` from the task answers it, and so does an emptied
  mailbox. An idle task gets one doorbell per batch of mail. A `nudged`
  ring (an idle task that never parked) adds one fixed sentence to the
  notice asking for a park record.
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
  shutdown kills its whole process tree, launcher wrappers included. On Windows
  the CLI runs in a job object that every process it starts joins, so the kill
  also reaches a worker whose parent has already exited; where the shim cannot
  give it a job (a parent job that forbids nesting), `taskkill /T` does the
  kill, as before. A missing
  CLI, a non-zero exit or a timeout turns the doorbell off for that shim process
  with one stderr line; pull delivery and hints continue unchanged. Each queued
  doorbell appends a `bell` line to `ledger.log` in the digest directory, with
  the ring's decision and reason as its sixth column.
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
`send` (with the full body; a burst to a project or the whole board writes one
per recipient, each with its own body and salt and `fanout: {to, recipients}`
in its payload), `read`, `ack`, `attempt`, the prune pass's
`expire` (bodies blanked) and `prune` (messages and addresses removed),
`bank_identity`, the lease events, the operator's restore `recover` and
`rebind`, and the operator's `redact` ([below](#redacting-a-body)). A `read`
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

A `send` row written from schema v46 on keeps the message text in a separate
`body` column and a random 16-byte salt in `body_salt`, both outside the hash.
Its hashed payload holds sha256(salt || body) (`text_commitment`), and
neither the text nor its length, so the chain vouches for
the body without containing it, and an operator can remove one body without
breaking the chain. Redaction removes the salt with the body, so what stays in
the chain cannot be used to confirm a guess of what was removed. A `send` row written before v46 has the
text (`text`) inside its hashed payload and no `body`; that body cannot be
removed, and stays until audit retention removes the row.

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
REST route for it. `pseudolife-mcp board-audit` reaches the bank directly,
through `PSEUDOLIFE_MCP_DATABASE_URL` or the lite tier's embedded instance;
`export` and `verify` read one read-only snapshot, so they are safe beside a
running daemon:

```sh
pseudolife-mcp board-audit export --task fix-week --since 2026-09-23 --out board.jsonl
pseudolife-mcp board-audit verify
```

`export` writes one JSON object per line, oldest first, to stdout or to a new
`--out` file, which it never overwrites. Each line carries the row's columns,
the payload parsed, and `body` and `body_salt` (`null` except on a v46
`send` that has not been redacted). Prefer `--out` for anything you keep: a
PowerShell 5 `>` redirect writes UTF-16. The filters are `--project`, `--task`,
`--agent` (the acting agent or a message's recipient: a full id, or a unique
prefix of 8 or more characters, resolved against registered addresses and
the log's own ids, so an address the prune pass removed still resolves; an
ambiguous prefix is refused naming the candidates), and `--since` / `--until`
(epoch seconds or ISO 8601; a time without an offset is local). The daemon's
`expire`, `prune` and `audit_prune` rows and the operator's `recover` carry no
project or task and name agents only in their payload, so a filtered export
leaves them out.

`verify` walks the chain and prints one JSON report: `ok`, the number of
`events`, `first_seq`, the head (`head_seq`, `head_hash`, `head_created_at`),
and `start_cut`, the cut the log starts from once retention has removed its
oldest rows. It exits 0 when the chain is intact. It exits 1 with the first
failing `seq` and a `reason`: `sequence_gap`, `broken_link`, `hash_mismatch` or
`unanchored_start` for the chain; `body_mismatch` for a body that, with its
salt, does not open the commitment its `send` row's payload holds, a body on
any other row, a salt left without its body, or a body or salt written back
after a `redact` row named it; `body_missing` for
a v46 `send` whose body is gone without a later operator `redact` row that
names it and says it removed the body (reported after the whole walk, since
that row comes later); `body_not_exported` for a v46 `send` in an export that
has no `body` fields at all; or `head_missing`, `head_mismatch` or
`head_pruned` for an expected head. It exits 2 when it could not check.
`verify --input <file>` checks an export file instead of the bank; the export
must be unfiltered, since a filtered one has gaps, and a line that repeats a
key is refused. Export and verify with a v46 or later CLI: an export written
by an older one has no `body` fields, so its v46 sends fail as
`body_not_exported`, and an older `verify` checks no bodies at all. An export
of a pre-v46 log still verifies. In the Docker tier run it
inside the daemon container, which already has the database URL:
`docker exec pseudolife-mcp-daemon pseudolife-mcp board-audit verify`.

What `verify` shows: no row was edited, inserted or reordered, and none was
removed except the oldest, behind a cut record whose own fields add up (written
by the daemon, a window of at least a day, the cutoff that window gives at its
time, and a first surviving row no older than that cutoff; retention removes
only an expired prefix, so a later row stamped before the cutoff can remain).
No v46 body was edited, and none was removed except behind a `redact` row.
What it cannot show on its own, because no secret is involved: that the
newest rows were not dropped; that the table was not rewritten with every
hash recomputed; that the oldest rows were not removed by someone who
also appended a consistent cut record; and that a body was not removed by
someone who also appended a consistent `redact` row (which then stays in the
log, reason and all, like the operator's own).
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
retention window unless an operator redacts one, and bodies carry machine
paths and usernames. It lives only in the bank database and its full backups,
portable `export`/`import` archives omit it, and the CLI writes only to stdout
or a local file you name. Keep exports out of repositories and anywhere public.

#### Redacting a body

A body that must not stay in the log (a credential pasted into a message by
mistake) can be removed by the operator, never by an agent: there is no MCP
tool or REST route for it.

```sh
pseudolife-mcp board-audit redact --message-id <id> --reason "pasted a credential"
```

`--message-id` takes the full id or a unique prefix of 8 or more characters
(an ambiguous one is refused naming the candidates). A message sent to a
project or to `all` is one copy per recipient: redacting one leaves the
others, so the result lists them as `other_copies` (and says so on stderr);
redact each. In one transaction it blanks the `send` row's `body` and `body_salt`, blanks
the live copy in the mailbox if prune has not already and ends its delivery,
and appends a
chained `redact` row (actor `operator`, the message's agents, project and task,
and a payload naming the message, the `send` row's `seq`, the reason and
`audit_copy: removed`). After the commit it runs `VACUUM (ANALYZE)
coordination_events, coordination_messages` and `VACUUM pg_statistic`: the old
row versions that still hold the body are freed for reuse, and the planner
statistics are rebuilt, since `ANALYZE` copies sampled column values under
1 kB (bodies, salts, live texts, request fingerprints) word for word into
`pg_statistic`, where they would otherwise stay until the next automatic
analyze. It prints one JSON result, `{"ok": true, "message_id", "seq",
"redact_seq", "redact_hash", "expect_head", "live_body_cleared", "audit_copy",
"vacuumed"}`, and exits 0. Record `expect_head` outside the bank, as for
`verify`: a later `verify --expect-head` with it shows the redaction's own
record is still there. A failed vacuum does not undo the redaction; the result
says `"vacuumed": false`, and you can run both `VACUUM`s later. It says the
same, and prints Postgres's warning, when Postgres skipped a step instead of
failing it: a role that may not vacuum or analyze a table gets a warning and a
skip, and a skipped `ANALYZE` leaves the body in the statistics, so run the
two `VACUUM`s as the tables' owner or a superuser.

A message sent before v46 has its body inside the hashed payload, which cannot
change: that audit copy stays until retention removes the `send` row. While
its live copy is still in the mailbox (up to 24 hours), `redact` blanks it and
takes it out of delivery all the same, logging `audit_copy: kept`, and says so
on stderr. With audit retention under seven days, a message's `send` row can
go before its mailbox row's request fingerprint does; `redact` still blanks
that fingerprint (and any live text), logs `audit_copy: gone` with `"seq":
null` in the payload and the result, and says so on stderr.

It refuses, printing `{"ok": false, "message_id", "reason"}` and exiting 1,
when neither the log nor the mailbox has the message (`message_not_found`: an
unknown id, or one whose `send` row retention removed and whose mailbox row is
gone too), when the message was sent before
v46 and its live copy is gone (`body_in_hashed_payload`), when the body is
already redacted (`already_redacted`), or when `--reason` is blank, longer
than 240 characters, holds a control, format or line or paragraph separator
character (`invalid_reason`), or looks like a credential (`secret_like_body`).
It exits 2 when it could not run: the board busy (a row or the audit chain
locked for more than 5 seconds; nothing changed, retry), or a bank whose log
predates v46. It takes the same locks as the daemon's own board writes, so it
is safe beside a running daemon; in the Docker tier run it inside the daemon
container as for `verify`.

What redaction does not reach: audit copies of bodies sent before v46; full
backups, WAL archives and `--out` exports taken earlier, which still hold the
body (restoring such a backup brings it back, so redact again after a
restore); the database's write-ahead log until the server recycles it; row
versions a still-open snapshot (a long `export`) or a replication slot keeps
the vacuum from freeing; the bytes of freed row versions, which a vacuum
marks for reuse but does not overwrite (`VACUUM FULL` rewrites a table); the
superseded statistics row, when the role running `redact` may not vacuum
`pg_statistic` (reported as above; autovacuum frees it later); and whatever
the recipient already read. What stays in the bank
cannot confirm a guess of the body: the `send` row keeps only its salted
commitment, and the salt goes with the body; the mailbox row's request
fingerprint (a sha256 over the recipient, body, reply and expiry, kept seven
days for retries) is blanked too, so a retry of the redacted request is
refused as `request_conflict`. The reason is hashed into the chain for good;
describe the mistake, never repeat the secret.

#### Coordination telemetry

`pseudolife-mcp board-audit stats` turns the log, the v49
`coordination_wakes` table and the suite lock's durations file into one
JSON object of coordination telemetry for a window (default the last 24
hours), so a maintainer can see whether mail moves, rings land and parks
clear without reading an export:

```sh
pseudolife-mcp board-audit stats
pseudolife-mcp board-audit stats --since 2026-09-27 --until 2026-09-28 --out stats.json
pseudolife-mcp board-audit stats --append ~/board-stats.jsonl
```

It reads one read-only snapshot like `export`, so it is safe beside a running
daemon (in the Docker tier, inside the daemon container). `--since` /
`--until` take epoch seconds or ISO 8601 (a time without an offset is local);
the window is `[since, until)` and every figure is as of `until`: a send
still unread then counts as unread, a park still standing as open, a ring not
yet served as never served. Events from up to 7 days before `since` (the
longest a park can stand) are read only to know which parks stood or had
lapsed when the window opened; a park set before the window is not counted.
`--out` writes a new file (never
replaced), `--append` adds one line to a JSON lines log (created if missing);
with neither the object goes to stdout. `--input <export>` reads an export
file instead of the bank, clipped to the same span (wake rows are not
exported, so wake precision is empty there). `--durations` names the suite lock's
`full-suite.durations.jsonl` (default: the lock directory,
`PSEUDOLIFE_SUITE_LOCK_DIR` or `~/.pseudolife-mcp/locks`).

The object (`"shape": 1`) carries `report`, `shape`, `version` (the package),
`generated_at`, `window` (`since`, `until`, `hours`), `sources` (where
events, wakes and durations came from: `bank`, `export`, `none`, `file`,
`absent`), `events` (`total` and `by_kind`), and five sections. Percentiles
are nearest-rank, in seconds, with the sample size beside them (`{"n", "p50",
"p95"}`, `null` where there is no sample).

- `mail_latency`: send to first read (the `send` event to the `read` event the
  daemon logs once per message): `sends`, `read`, `unread`, `seconds`,
  then the same `by_decision` (the send's wake decision: `rung`, `nudged`,
  `hinted`, `withheld`, `no_path`, `capped`, `not_needed`, or `unknown` for a
  send from before v49) and `by_principal` (the recipient's principal, as
  the log saw it act in the window, else `unknown`). For a session with a
  live channel the first read is usually the adapter pushing the mail in
  (`path: delivery`), so there it measures delivery, not the model reading.
- `wake_precision`: for each `rung` or `nudged` row decided in the window,
  whether it was `served` to the recipient's adapter before `until` (`never_served`,
  `share_never_served`, `seconds_to_served`), whether the recipient acted on
  the board (an update, read, ack, send or register; not the adapter's own
  delivery reads) within 120 s of being
  served (`acted_after_served`, `share_acted_after_served`), and, where the
  Stop hook posted its [woke marker](#waking-an-idle-claude-code-session-the-stop-hook)
  within an hour of the serve, the same from the turn actually starting
  (`woke`, `share_woke`, `seconds_served_to_woke`, `acted_after_woke`,
  `share_acted_after_woke`); overall and `by_decision`.
- `park_outcomes`: parks set in the window, read as the daemon applies
  updates (the live-park rule): an `update` setting `park_reason` while no
  park stands starts a park; one setting it, or any other park field, while
  a park stands refines that park, which then counts under its latest
  reason; a lapsed park's fields stay on the row, but the next park is a new
  one and the lapsed one ended at its expiry (logs written before that rule,
  when a park field could revive a lapsed park, are read as that daemon
  applied them). `parks`, how each ended in `cleared` (`send`: a `rung`
  message reached the agent between the park and its clearing update;
  `owner_update`: the agent cleared it with no ring in between, by a plain
  status or a null reason; `expiry`: `park_expires` passed before any
  clearing and by `until`; `open`: still standing at `until`),
  `seconds_to_clear` over the first two, then the same `by_reason`. A park
  set before the 7-day lookback is never counted.
- `sends_per_session_hour`: `sends` in the window over attached
  `session_hours` (from an `attach` that is not a renewal to the same
  address's `detach`, a `prune` removing it, or `until`, clipped to the
  window; heartbeats are not logged, so an address attached before the window
  with no fresh attach in it counts no hours, though its sends still count)
  and their `rate`, overall and `by_principal` (the sender's).
- `suite_lock`: the full-suite `runs` whose `ended` stamp falls in the
  window, their `hold` and, where the record carries `queued_at` and
  `started_at` (runs since this release), their `queue_wait`.

Only counts, seconds, decision and reason names, principal names and version
strings leave the tool: no message body, no agent id (not even a prefix),
no path, no worktree and no user name is copied from any input, so the
object can be kept beside a PR or a report where an export never could.

#### Secret-shaped text

The board refuses text shaped like a credential wherever it would keep it: a
message body and its request id (`send`); a status, label, project, task,
episode or capability name (`register` and `update`, including
`memory_agents update`; project and task are copied into every later audit
row by that agent); a lease name
or purpose (`lease`, `release` and `leases`, including `memory_agents claim`,
whose status is the purpose); and a redaction reason. The call fails with
`secret_like_body` (HTTP 400 on the REST API) before the text is stored
anywhere, and the error never repeats the text. (The daemon's own
once-a-minute prune pass may run first on `register` and `send`; it involves
no caller text.)

The shapes are GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` with 36 or
more characters, `github_pat_`), GitLab `glpat-`, Hugging Face `hf_`,
Anthropic `sk-ant-`, OpenAI-style `sk-` and Stripe `sk_live_`/`rk_live_` keys,
Google `AIza` API keys, AWS access key ids (`AKIA`/`ASIA`) and secret access
keys, Slack `xox` tokens, JWTs, a `Bearer` token, the password in a DSN
(`postgresql://user:<password>@host`), PEM private-key headers, and a key
whose name contains `secret`, `token`, `password`, `passwd`, `credential`,
`key`, `auth` or `bearer` (such as `PSEUDOLIFE_MCP_TOKENS`,
`X-PL-Agent-Key` or `private_key`) given a generated-looking value after `:`,
`=` or, for a `--flag`, a space. A value is split at `,`, `:` and `=`, so each
token in a principal map (`name:token,name:token`) counts on its own, and a
piece counts when it is 20 or more characters of letters and digits with
lower case, upper case and digits all present, or 32 or more in one unbroken
run (no `_` or `-`, which word-joined identifiers have).
Where a prefix also starts ordinary identifiers (`ghs_`, `github_pat_`,
`sk-`), the rest must look generated too. Not refused: ordinary prose about
tokens and secrets; git SHAs, digests (whole or truncated), UUIDs, message and
agent ids; anything with a `/` after a key (paths, branches, `owner/repo`);
names, words, word-joined identifiers and counts; placeholders (`<token>`,
`$VAR`, `${VAR}`). Single-case passwords shorter than 32 characters get
through; this is a net for common
shapes, not a guarantee, and a v46 body it misses can still be redacted. Over
the 2026-09-23/24 fifteen-session trial's board export it refused none of the
816 message bodies and request ids, the 25 statuses, or the labels, projects,
tasks and episodes of its 26 agents.

### Coordination report

`evals/coordination_report.py` turns a board record into an aggregate-only
report, the measure a coordination change is judged by: a change should beat
the committed baseline of the 2026-09-23/24 trial
([artifact](../../evals/results/coordination-baseline-20260924.json)) by more
than night-to-night noise. One night is a reference point, not an interval:
that noise stays unmeasured until a second comparable night is reported. The
report reads an audit export, or the older whole-board export the trial was
recorded in, and writes a new JSON file plus a Markdown rendering beside it. It
never replaces either without `--force`, and never its own input:

```sh
pseudolife-mcp board-audit export --out board.jsonl
python -m evals.coordination_report board.jsonl --out night.json \
  --since 2026-09-26T16:00+10:00 --until 2026-09-27T08:00+10:00 \
  --window "evening=2026-09-26T16:00+10:00/2026-09-26T21:30+10:00"
```

Export the whole log and scope the report with `--since`/`--until` (messages
by send time; staleness is measured at `--until`). The report still reads the
registrations and statuses set before the scope, which an export filtered at
the source drops: it flags such an export (a gap in its sequence, or a start no
retention cut anchors). In a filtered export, a recipient that registered
before the cut and did not act after it appears under the principal `unknown`,
and staleness counts the agents with no status event.

It reports acknowledgement latency (median, p90, acknowledged and never
acknowledged) per recipient principal, overall and in each `--window`
(`LABEL=START/END` by send time, epoch seconds or ISO 8601 with an offset,
either side may be left open); the share of acknowledgements that covered three
or more messages at once; each directed pair's busiest 60 minutes; near-identical
fan-out bursts; the kind mix; SUITE-START/SUITE-END traffic and baton passes;
status staleness from an audit export (the older export has no status history),
leaving out detached and revoked sessions, whose statuses no peer is shown; and
resources agents coordinated by hand repeatedly, listed as candidate lease
declarations with counts only. Every metric carries its definition in the
output. Wakes per session-hour, requests past their reply-by time and the time
to answer a NEEDS-HUMAN message are `null` until the schema records what they
need.

The kind mix uses a message's declared `kind` when the event carries one and
otherwise a tag-first heuristic over the body, hand-calibrated on the trial and
marked `heuristic` in the output. Some of its rules depend on which agent
coordinates: `--coordinator auto` (the default) picks the agent with the most
distinct counterparties, `none` turns those rules off, and an agent id names
one.

The report is aggregate-only by construction. Bodies, labels, statuses, tasks,
projects and paths are matched against fixed keyword lists in memory and only
counts are written; agents appear as `<principal>-<n>` in first-seen order, and
no raw id or input path reaches either file. A principal is written by name
only when it is one of the installer's role names (`default`, `claude-code`,
`claude-desktop`, `codex`, `gemini`, `mcp-client`); any other is written as
`principal-<n>`, because an operator-chosen principal can be a username or a
host. `--keep-principal NAME` keeps one you know to be a role. Window labels are
limited to 40 letters, digits, spaces and `:._+-`. The input itself stays
private.

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
60 new sends per sender per minute (a send to a project or to `all` counts as
one, so a sender can reach at most 60 × 50 mailboxes a minute) and 50 messages
per receive page. Live receive stops serving bodies after 24 hours;
request-key metadata is retained for seven days.
The [audit log](#audit-log) keeps its own copy of every body for
`audit_retention_days`, unless the operator [redacts](#redacting-a-body) it.
Receive adds `continuity`: `status` is `messages`, `empty`,
`expired_unacknowledged`, or `retention_gap`; `expired_unacknowledged` counts
unacknowledged expired messages after the supplied cursor that remain inside
the metadata window. `metadata_gap` means sequence metadata after that cursor
has left the window, so the API cannot establish whether those messages were
acknowledged. `high_water` is the mailbox's latest allocated sequence, and
`metadata_retention_seconds` states the bound. Expired bodies are never included.
These fields do not advance the receive cursor or acknowledge mail. A cursor
ahead of the mailbox still returns `invalid_cursor`, with `cursor_ahead` detail;
malformed and foreign-mailbox cursors remain `invalid_cursor`.

`memory_message(action="history")` and `POST /api/coordination/history` read a
bounded slice of this authenticated instance's sent and received audit mail.
They require the same bearer, bank binding and instance credentials as receive;
another instance, even under the same principal, cannot inspect this mailbox.
An addressed recipient can read mail sent by another principal. Optional
`peer` is an exact agent ID, including an address already pruned from the board;
prefixes are not resolved. `limit` is 1–50 (default 50). Results contain
`messages`, `has_more`, a separate `after` cursor, `retention_days`, and the
UTC-day `retained_since` bound (`null` when configured retention is unlimited).
Pass the history cursor only to history, and omit it when changing the peer
filter. Audit retention applies even before the next prune pass; an already
pruned send event cannot be reconstructed, and pre-audit mail has no history.

Each historical message carries its sender, recipient, sorted `participants`
pair, `reply_to`, original recipient sequence, timestamps and acknowledgment
state. A reply ID may reference mail outside the retained slice; no thread root
is inferred. `body_state` is `retained`, `redacted`, or `unavailable`; a redacted
body is `null`, including legacy bodies that remain in an operator-only hashed
audit payload. `state` is `pending`, `expired`, or `acknowledged`, reflecting
recorded delivery state, with acknowledgment taking precedence over expiry.
Operator redaction also expires the live message; history reflects that expiry.
History is context: reading it does not stamp first-read telemetry, acknowledge,
retry, requeue, wake an agent, or add anything to dream input. It never returns
audit salts, commitments or request fingerprints.

Text shaped like a credential (a body, request id, status, scope field, or
lease name or purpose) is refused with `secret_like_body`
([secret-shaped text](#secret-shaped-text)).
Opportunistic pruning runs at most once per minute during registration, sending
or heartbeats. Expired bodies remain unservable even when no adapter is running
to trigger physical cleanup. Full queues and rate limits return explicit errors.
Live delivery attempts a message at most three times in total across
attachments; past that it is left for explicit receive, so one unacknowledged
message cannot wake the host on every restart. The same prune pass removes an
address that is referenced by no retained message and has had neither its own
activity nor a lease for seven days, or for one hour when its adapter
registered without a state file (`capabilities.resumable: false`), since
nothing can attach to that address again. A held lease keeps a live shim's
address even while it is idle, so a daemon restart or a host sleep shorter than
that window cannot retire it; if a state-less address is ever retired, its
adapter registers a fresh one instead of stopping.
Addresses that predate the flag keep the seven-day rule. Idle
means no register, update, new attachment, send, acknowledgment or forwarded
tool call: the adapter's lease heartbeat counts as activity only when the shim
forwarded a tool call since the previous one, and an adapter re-attaching under
its own attachment ID (after a daemon outage or a host sleep) is recovering its
lease, not acting, so a parked shim is neither ranked nor retained as a working
one. A client that only reads must acknowledge what it
reads, or hold a lease, to stay registered. `memory_agents(action="list")`
shows peers active within the last hour, or within the last three hours while
they hold a lease, leased first; it reports the number of other matching peers
as `idle_omitted` and sets `truncated` when the page cut listed peers; a peer's
public agent ID stays addressable while its row exists. Each listed peer
carries `status_set_at`, `status_age` and `status_stale`. The time comes from
the [audit log](#audit-log): the newest registration or status update. A status
the log no longer covers is reported as older than the log's oldest event, for
example `more than 21 hours ago`. A non-empty status older than two hours is
marked stale. The two-hour and three-hour windows come from the first day of
the live audit log (2026-09-25). Working agents refreshed their status within
34 minutes at p95 and never went more than 51 minutes between board actions
inside a work block. Idle stretches ran 5.4 hours or longer.

Claude Desktop's app-level entry (writer ID `claude-desktop`) is one process
serving every conversation in the app, so it registers no coordination
address: whichever conversation called it would post, set status and read
mail as all of them. It refuses `memory_agents` `update`, `claim` and
`release`, and `memory_message`, with an error saying why, prepends the same advice to its MCP
instructions, and its `memory_agents(action="list")` shows open sessions only,
not the board. A Claude Code session makes those calls on its own per-session
server. In the Desktop app's Code tab that works only while the two entries have
different names: the installer registers both as `pseudolife-memory`, and
where the names match, Desktop serves the Code tab from its app-level entry.
The writer ID is operator configuration, not authentication: the guard keeps
honestly configured clients apart, while the daemon itself refuses any board
write that carries no instance credential.

For a custom cloud/ChatGPT/Dot launcher that shares one stdio shim among
conversations, set `PSEUDOLIFE_MCP_SHARED_HOST=1` in that launcher's environment
and restart its shim. The known `tunnel` writer uses the same guard automatically;
setting the marker to `0` does not disable it for `tunnel` or `claude-desktop`.
Any value except empty, `0`, `false`, `no` or `off` (trimmed and
case-insensitive) turns the guard on, so a mistyped marker fails closed.
The shared shim refuses all `memory_message` actions and `memory_agents`
`update`, `claim` and `release` before sending them upstream, even when
coordination or channel delivery is explicitly enabled. It serves no board
check-in and binds no adapter or Codex thread registry. Memory calls and
`memory_agents(action="list")` continue with the configured bearer principal.

The [OpenAI Plugin reference](https://developers.openai.com/plugins/reference)
documents `_meta["openai/session"]` for correlating calls within a ChatGPT
conversation. This shared shim does not establish a trusted conversation
binding from that field: metadata, tool arguments, project/task labels and
the bearer principal's name cannot select another mailbox. A principal named
`codex` does not make a cloud launcher a session-aware Codex host. The supported
Codex transport still binds threads through its own host path. Distinct cloud
conversation mailboxes remain unsupported through this shared shim; ordinary
memory access is available. Existing custom cloud deployments need the marker
and a restart separately from installing this code. No launch registration or
live cloud configuration is changed by this guard.

An adapter with saved state registers a fresh address on its next start only when the authenticated
daemon explicitly confirms that the saved address no longer exists (pruned
after seven idle days with no retained mail, or absent from a restored
database, where `rebind` cannot restore it either). A bank-bound client first
verifies that the daemon is still its saved bank and principal. It keeps
the old state file beside it with a `.stale` suffix. A rejected bearer or instance
credential, or a different bank or principal, preserves the saved address and
requires corrected authentication or the deliberate restore/rebind procedure;
an HTTP status alone never proves that an address should be replaced.

A subagent that a Claude Code session spawns with its Agent tool runs in the
same shim process, so its board calls carry the parent's identity (probed
2026-09-27: the subagent's `memory_agents(action="list")` left out the parent's
own row, as the list does for the caller, and its receive returned the
parent's cursor). Nothing in the shim can tell the two apart: a subagent's
status update overwrites the parent's, its `ack` marks the parent's mail read
before the parent sees it, and its `send` goes out under the parent's name. So
a subagent only reads the board (`memory_agents(action="list")`,
`memory_message(action="receive")` without `ack`, `memory_search`), and the
orchestrating session owns the address. The served check-in says so, and
since 2026-09-30 the Claude Code plugin enforces it: a PreToolUse hook
(`plugin/hooks/subagent-board-guard.sh`) sees the `agent_id` Claude Code
puts in a subagent's hook input, and never in the parent's, and denies that
subagent's `memory_agents` update, claim and release and `memory_message`
send and ack, whatever the server's name. Its list and receive pass, and so
does every call the parent makes. The subagent reads the refusal as
`PreToolUse:<tool> hook error: Pseudolife board: refused ...`, which tells
it to ask the parent instead (probed on Claude Code 2.1.283: the child's
update never reached the server, its list and the parent's update did). A
payload the hook cannot read, and `PSEUDOLIFE_AGENT_COORDINATION` set to
anything but a yes, let the call through. Installs without the plugin keep
the instruction only. In Codex the same entry allows everything: a Codex
child has a board address of its own. Claude Code
subagents get no addresses of their own: the parent names them on its own row
with `memory_agents(action="update", children=["review storage", "tests"])`, at
most 8 labels of at most 40 characters. Peers see them as `children`, a list of
`{label, since}` in which the daemon stamps `since` and keeps it for a label
the next update carries over. Omitting `children` leaves it unchanged and `[]`
clears it; a children-only update does not move `status_set_at`.

Since schema v50 (maintainer decision 2026-09-30: subagents are liveness
information on their parent, not peers) the plugin keeps that list current
on its own. Its SubagentStart and SubagentStop hooks
(`plugin/hooks/subagent-board.sh`) read the payload's `agent_id` and
`agent_type`, find the session's board address in the `<key>.agent` file the
shim writes beside its digest (as the Stop hook does), and make one bounded
`POST /api/hook/subagent?agent=<id>&event=start|stop&child=<agent_id>&type=<agent_type>`
each: a start adds `{label: "<agent_type>#<first 8 of the id>", since,
agent_id}`, a stop removes it. SubagentStart carries no task description,
hence the label. The hooks are `async` (their answer is ignored) and fail
open: a down daemon, a refused bearer or a session without an address adds
nothing and never holds a subagent back. A parent's `children=[...]` update
replaces only its own labels and keeps the live hook entries (`[]` clears
the parent's labels); a hook never removes a parent label; a parent label
spelled like a live hook entry names that same child. Hook entries have a
cap of their own, eight, and never count against the parent's eight labels,
so a parent's update is never refused because of them; past it a new start
replaces the oldest hook entry. A hook entry whose stop never came (a killed
session, a hook that failed open, or an async stop that overtook its own
start) does not stay: past three hours (`HOOK_CHILD_TTL`, the attached-idle
window) it is no longer listed and goes at the next write to the list, and a
detach (the shim ending with its session) clears every hook entry, logged as
an `update` by the daemon; the parent's own labels stay. An update is one
transaction: a refused `children` (more than eight labels, a duplicate, one
over 40 characters) applies nothing else sent with it, status and park
included, and the `invalid_children` detail says nothing was updated. In
Codex these hooks do nothing: a Codex native subagent has an address of its
own (next paragraph).

A Codex native subagent (spawned with `collaboration.spawn_agent`) runs on its
own thread, so it gets its own board address like any Codex thread (see
[Codex CLI and desktop](#codex-cli-and-desktop)), linked to its parent: the
row carries `parent_agent_id` and `subagent: true`. Its parent sends for it:
`memory_message(action="send")` from a subagent's address is refused with
`child_send_refused` ("a subagent does not send board mail; ask your parent
session"; HTTP 403 on REST), whether or not the parent has registered yet.
It still receives and acknowledges its own mail and sets its own status and
park record. A peer that sees a subagent working on something it is
touching messages the parent.

### Park records and the wake decision

A session that stops records why, so a peer's mail can wake it only when the
mail clears what it is waiting for (schema v49, maintainer decision
2026-09-28: on 2026-09-27 a session sat all night on a blocker that had
cleared, while any message could wake a session with nothing to wait for).
The **park record** lives on the agent row and is set through
`memory_agents(action="update", ...)`:

| Field | Meaning |
| --- | --- |
| `park_reason` | `done`, `blocked`, `needs_approval`, `needs_info`, `needs_resource` or `waiting_peer`; `""` (REST: `null`) clears the whole record |
| `park_needs` | What would clear it, one line (120 characters) |
| `park_clear_by` | Who can: an agent id, `maintainer`, a lease name, or `anyone` (120) |
| `park_resume` | What to do once cleared (240) |
| `park_expires` | An epoch after which the park no longer stands; a park set without one expires after 12 hours, and none may be more than 7 days ahead (`invalid_park`) |

Use `done` only when no follow-up is expected: nothing will ring you.
Waiting on a merge click or a review that may still bring fixes? Park
`needs_approval` with `park_clear_by` set to the reviewer's agent id or
`maintainer`, or `waiting_peer`. A park records intent; automatic wake requires
a live listener. Check the sender's wake receipt; `no_path` means mail is
queued for receive on a later turn. For waits over 59 minutes, especially
`needs_approval` waiting on maintainer, arm `wait-mail` in the background or
keep the Codex doorbell active; otherwise record that you are reachable
on your next turn.

Before parking on a dependency that may take longer than 59 minutes, keep a
host path armed as described under [wait-mail](#waking-an-idle-session-pseudolife-mcp-wait-mail)
or [Codex doorbell](#codex-doorbell). Otherwise make the `next-turn-only`
limitation explicit. A sender checks the receipt instead of assuming that
an installed hook wakes an idle recipient. On `no_path`, use the recipient
host's messaging tool when available: Claude Desktop's session `send_message`
starts a user turn. If no host path is available, expect delivery on the next
turn and tell the maintainer when the dependency is urgent. Peer text still
does not grant approval.

An omitted field stays; a refinement or a new reason keeps the standing
expiry. A park past its `park_expires` no longer stands: a new reason over
it is a new park, with the 12-hour default counted from then and none of
the lapsed park's `park_needs`, `park_clear_by` or `park_resume` carried
over. A plain status
update while parked clears the record,
since a session that is working is not parked; a task or `children` update
leaves it. A park field on its own refines a standing park and is refused
(`invalid_park`) on an unparked row or a lapsed park, as are an unknown
reason and a bad expiry; credential-shaped text is `secret_like_body`. Every peer row in
`memory_agents(action="list")`, and the caller's own row in the update result,
carries the six `park_*` fields, `park_set_at` being the daemon's stamp.
`memory_agents`' description asks sessions to park when they stop, and the
[Stop hook park gate](#waking-an-idle-claude-code-session-the-stop-hook) asks
once when a turn ends without one (in Codex, a child thread's stop is asked
under the child's own address). (The served check-in does not say it yet:
it is the text the check-in bench measured, and changes only with a new run.)

**The daemon decides, the shim rings.** `memory_message(action="send")` takes
two optional fields, `clears` (which parked need the message answers, 120
characters) and `urgent`, and returns `wake` beside the receipt:

| `wake.decision` | When | Extra fields |
| --- | --- | --- |
| `hinted` | the recipient is not parked and acted on the board within `active_seconds`; its next tool result carries the mail (a parked session has stopped, so it is decided on its park however recently it parked) | |
| `not_needed` | the recipient is parked `done` | |
| `no_path` | the recipient has neither a live channel nor a currently armed ring listener; an installed or declared ring capability alone is insufficient | the parked need, if any; `reason: listener_unknown` or `listener_expired` for an unarmed or expired ring path |
| `rung` | a live path is armed and the recipient is parked with a need the mail plausibly clears: the sender is `park_clear_by` (or, when that names a lease, released it or let it expire within the last 60 seconds, by the daemon's audit log; never for `maintainer`, an agent id or an id prefix), `park_clear_by` is `anyone`, `clears` names the need (the same words, or one's words as a run of whole words inside the other's, holding a word of four letters or more), or `urgent` within the sender's cap | `ring_at` |
| `withheld` | parked with a need the mail does not clear | `park_needs`, `park_clear_by` |
| `nudged` | idle with no park record (or an expired one), rung at most once per `nudge_interval_seconds` with a request to park | `ring_at` |
| `capped` | over a cap: `reason` names it (`recipient_hour`, `nightly`, `urgent_sender_hour`, `nudge_hour`) | the parked need, if any |

`reason` says which branch decided (`active`, `parked_done`, `wake_disabled`,
`listener_unknown`, `listener_expired`,
`clearer`, `anyone`, `clears`, `urgent`, `need_not_cleared`, `no_park`, or a
cap). `rung` is evidence of a known armed path at send time; it does not mean
that a turn started, that the recipient read the message, or that it acted.
Chatter never rings. A retry of the same `request_id` repeats the first
decision, and the audit log's `send` event names it. Rings from one sender's
burst are staggered by `fan_out_stagger_seconds` through `ring_at`. Each ring
is a `coordination_wakes` row; the recipient's attach and heartbeat answers
carry the newest for one heartbeat interval after it is first served
(`wake`, with the latest `ring_at`), so a retried heartbeat still gets it,
and the adapter takes each ring once. A wake path is a live channel or an
armed **ring path**. `ring` in `capabilities` describes what an adapter can
support; it does not prove a listener is still running. The adapter reports
`ring_armed_until` at attach and heartbeat, a finite epoch capped by the
daemon to 60 seconds and valid only while that attachment is live. It renews
that evidence only while a recipient listener is armed. Missing evidence is
`listener_unknown`; expired evidence is `listener_expired`. Eligible mail
without a listener remains queued for a later armed listener; the send's
`no_path` receipt remains the honest result at send time. A pending queued
ring survives an outage or a new attachment until its mail is acknowledged
or expires; the adapter suppresses repeat delivery within one attachment
generation. A daemon older than
v49 refuses the attach parameter once, and the adapter stops sending it.
The shim rings through its client's
path: the Claude adapter writes
`<key>.ring` beside the digest (line 1 the digest watermark, line 2 the
decision and reason) at `ring_at` for the Stop hook, and the Codex doorbell
asks the adapter for a due ring at the moment it would otherwise run
`codex queue` (an offer whose mail the session has already seen, or that
has nothing pending, is dropped). Every ring's ledger line (`ring` from the adapter, `wait` from
the Stop hook, `bell` from the doorbell) carries the decision and reason as a
sixth column, and a Stop hook that fires posts a `woke` marker the daemon
logs for the address, so [`board-audit stats`](#coordination-telemetry)
can tell a ring that was served from one whose turn started. Against a daemon older than v49 nothing rings; pull delivery,
tool-result hints and the prompt-hook digest are unchanged.

`pseudolife-mcp channel` is the optional Claude Code preview transport. Host
delivery requires explicit preview opt-in and recipient wake configuration;
protocol tests alone do not establish compatibility with an installed host.
Only addressed messages may wake an opted-in recipient, and since v49 the
live channel carries only mail the daemon decided to ring (`rung`,
`nudged`) or hinted to an active session, plus mail sent before v49; mail
it withheld, or found not needed, waits for an explicit `receive`, which
still returns everything. Board/status activity
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
credential again matches the saved authority; it never creates a replacement address. A
saved address the daemon reports missing while the shim runs (the host slept or
the daemon was unreachable past the seven-day retention) also stops background
delivery and keeps the state file, but the notice says to restart the session:
`rebind` cannot restore a missing address, and the next start retires the state
and registers a new one. Do not
infer live delivery from a queued or attempted send result.

If initial registration fails, or the shim's startup budget cancels it before
the adapter receives the new address, the adapter releases its empty state
reservation and never overwrites a state file that already holds an identity.
When the daemon simply did not answer in time (a timeout, a refused or dropped
connection, a 5xx or 429, or `attachment_busy` while the lease of an attach the
startup budget cancelled runs out), the shim keeps the session's memory tools and
retries the registration in the background, after 1, 2, 5, 10 and 30 seconds
and then every minute, until it lands or the session ends; each attempt starts
from a fresh adapter. Until it lands, `memory_agents` update, claim and release
and `memory_message` are refused with a message saying registration is being
retried, and the first tool result after it lands says the board now works. A
refusal retrying cannot change (unauthorized, principal not allowed, bank
mismatch, invalid saved state, or a state file another process is registering)
stops there, as before, and the next tool result says so once. An address the
daemon created for a lost response was never held by any adapter, receives no
mail, and is pruned with the other idle addresses; a link slow enough to lose
register responses can leave one per background attempt. Do
not revoke every mailbox to repair one failed registration. A lost attachment
response can leave a lease until expiry; failed competing attachment attempts
do not renew it.

A crash-left empty state file becomes eligible for takeover after one minute.
Takeover uses an owner-only sibling `.lock` file and a nonblocking operating-system
lock, so concurrent launches cannot both register against that stale reservation.
The lock file remains on disk; lock ownership is released when the process exits,
including a crash. Do not delete it while an adapter might be using it.

Full database backups contain coordination mail and the audit log. Portable `export`/`import`
archives omit the coordination tables (agents, mail, leases, rings and the audit log) and their clock metadata so moving
knowledge cannot clone live mailboxes or instance credentials. Follow the
[offline mailbox recovery procedure](coordination-recovery.md) after a database
restore. See the [experimental design](../specs/2026-09-11-agent-coordination-design.md)
for delivery-state and host-verification contracts.

### Waking an idle Claude Code session: the Stop hook

Mail otherwise reaches a Claude Code session only at its next memory call or
prompt, so an idle session can sit on a message for hours. The plugin ships a
`Stop` hook that waits on the session's digest after every turn and wakes the
session when mail that should wake it arrives. It is on by default since
2026-09-28 (before that, opt-in with `PSEUDOLIFE_AGENT_WAKE_HOOK=1`); set
`PSEUDOLIFE_AGENT_WAKE_HOOK=0` in the hook's environment to turn it off (for
example in the `env` block of `~/.claude/settings.json`, which Claude Code
passes to the processes it starts), and `PSEUDOLIFE_AGENT_COORDINATION=0`
there turns off the whole board for the client, hook included. The hook's
command checks both flags before bash reads the script, and refuses a script
that does not parse, so a broken copy cannot wake every session at every turn
end. It needs the coordination adapter above, since it waits on the digest
file the adapter writes: without a digest directory it exits at once. It also
needs a Claude Code release that honours `asyncRewake` (verified on 2.1.280);
one that ignored `async` would run it in the foreground and hold each turn end.

A wake lets a peer allowed to mail this session start a model turn in it
while you are away, in whatever permission mode the session runs; peer text
still cannot grant approval. That is why wake is policy-gated: the daemon
rings a session only while it is parked with a declared need that the message
plausibly clears (the sender is the one it named, the message is tagged
`clears=<need>`, or the sender set `urgent`), and never for chatter. Every
wake spends tokens, and two sessions can keep waking each other, so wakes are
also capped (below, and by the daemon's `wake` caps under
[Experimental agent coordination](#experimental-agent-coordination)).
`pseudolife-mcp doctor` reports the hook's state under `wake.claude_code`.

- The hook runs with `"async": true` and `"asyncRewake": true`: in the
  background after each turn, and exit code 2 starts a new turn even when the
  session is idle. Verified in the Desktop Code tab on Claude Code 2.1.280, in
  auto permission mode (under a second from exit to the new turn). Claude Code
  labels the delivery "Stop hook blocking error"; that label is the wake, not
  a failure. The reminder is one line saying so, then the digest, which reads
  as agent-origin, not user authority.
- It fires on a ring the daemon decided (v49): when the shim's `<key>.ring`
  marker is past the `.seen` marker, the digest's watermark is past it too
  and the digest lists mail. The shim writes the marker for `rung` and
  `nudged` mail only ([the wake decision](#park-records-and-the-wake-decision)),
  so chatter to a parked session, mail the daemon withheld, and a digest
  that merely changed (an acknowledgement, an expiry) do not wake the
  session. A ring for mail that arrived during the turn fires at once; a
  digest the session already saw (through the prompt hook, the tool-result
  hint or an earlier wake) does not fire again at the next turn end. A
  nudge adds one sentence to the wake text asking for a park record. Firing
  advances `.seen` and appends a `wait` line to `ledger.log` whose sixth
  column is the ring's decision and reason; if the marker cannot be
  written, the hook does not wake at all. SessionStart clears `.seen` on
  resume and compact, so a pending ring can wake the session once more.
- At most 20 wakes per session in any hour, the same figure the daemon now
  applies per recipient before it decides a ring. A ring over the hook's
  cap waits for the window to free up; it is delayed, not dropped.
- **The park gate.** Before arming the wait, when the turn that ended is not
  itself a Stop-hook continuation (`stop_hook_active` is false) and the shim
  has named this session's board address in `<key>.agent`, the hook asks the
  daemon once, `GET /api/hook/park-gate?agent=<id>&since=<turn start>` (2 s,
  the start from the `<key>.turn` stamp the prompt hook leaves), whether
  the session parked. The daemon answers `block` when the row has no live
  park record and its status is not done-shaped (it does not start with
  done, complete, finished or merged), or when an unparked session set no
  status or park during the turn. A live standing park allows the stop
  without another update unless the session received a `rung` delivery
  after the turn started: then the park must have been set strictly after
  the newest such delivery, or the gate answers `block` with
  `not_updated_this_turn`, even if an earlier update occurred this turn.
  Delivery means the wake decision's `created_at`, not its staggered
  `ring_at` or the adapter's `served_at`; mail to another session and
  other wake decisions do not invalidate the park. The hook then ends
  a blocked turn at once with "Before
  ending: update your board status with why you stopped and what you need
  (memory_agents update park_reason=... park_needs=... park_clear_by=...
  park_resume=...). Use done only when no follow-up is expected: nothing will
  ring you. Waiting on a merge click or a review that may still bring fixes?
  Park needs_approval with park_clear_by set to the reviewer's agent id or
  maintainer, or waiting_peer. A park records intent; automatic wake requires
  a live listener. Check the sender's wake receipt; no_path means mail is
  queued for receive on a later turn. For waits over 59 minutes, especially
  needs_approval waiting on maintainer, arm wait-mail in the background or
  keep the Codex doorbell active; otherwise record that you are reachable
  on your next turn." as the wake text and a `gate` ledger line (its
  fifth column is the message's length in UTF-8 bytes plus one, on every
  client). Once: the continuation's Stop carries `stop_hook_active: true` and
  is not asked (Claude Code also caps stop-hook continuations at eight in a
  row). An async Stop hook cannot use the `decision: "block"` JSON, so the block
  rides the same exit-2 rewake as the mail wake. No answer (a daemon that
  is down, a bearer it refuses, a redirect, an answer cut off at the time
  limit, no address) is allow: the gate never holds a turn on an error.
  The bearer comes from `PSEUDOLIFE_MCP_TOKEN` or a private
  `PSEUDOLIFE_MCP_TOKEN_FILE` (owner-only, one link, the same check as the
  other hooks), the URL from `PSEUDOLIFE_MCP_DAEMON_URL`, as for the other
  hooks. On Windows, Git Bash uses the native ACL rules: current-user
  ownership, a protected DACL, and allow rules only for the owner or OWNER
  RIGHTS; reparse points in the file or its parents are rejected. A token
  file rejected by the Stop gate or coordination-start hook leaves a `token`
  line with value `rejected` in the digest directory's `ledger.log`, without
  a path or token. OneDrive-redirected profiles using reparse points are
  rejected: move the token file outside the redirected folder and update
  `PSEUDOLIFE_MCP_TOKEN_FILE`.
- **The woke marker.** After a wake fires, the hook posts once to the daemon
  that this address's turn is starting, `POST /api/hook/woke?agent=<id>` (in
  the background, 2 s, the same bearer and URL as the gate, so a daemon that
  hangs never delays the wake); the daemon logs a `woke` audit event for the
  address whose payload counts the rings served to it in the last hour, and
  nothing else. It is telemetry: [`board-audit
  stats`](#coordination-telemetry) measures wake precision from the turn
  actually starting rather than from the ring being served. The answer is
  ignored, and a daemon that does not answer, or an address the bearer's
  principal does not own, records nothing; without a `<key>.agent` address
  there is no marker. The wake fires regardless.
- One watcher per session: each turn end takes the lease in `<key>.wake`, and
  the previous watcher exits within one poll (5 s). A digest file absent when
  the watch starts is waited for; one that vanishes during it (the shim
  exited) ends the watch. After `/clear` it reads the
  digest named by the per-process `claude-<pid>.host` record, when
  SessionStart has written one.
  The lease keeps a stable owner token; each watcher owns a separate
  `<key>.<Stop-owner-token>.wake-armed` marker carrying that token and a
  listener epoch renewed at each poll, capped at the watcher's deadline. The
  adapter accepts the marker only while its owner matches the lease, so a
  superseded watcher cannot reclaim it. Exit removes only that watcher's own
  marker and leaves the shared `.wake` ownership stamp alone; the stamp
  without a matching live marker proves no liveness. Stale evidence expires
  within 60 seconds even after an interrupted process.
- A watcher waits at most 3540 s after the turn that armed it; the hook's
  `timeout` is 3600 s, which Claude Code enforces on `asyncRewake` hooks.
  `PSEUDOLIFE_AGENT_WAKE_HOOK_WAIT` (seconds) shortens it. A session idle for
  longer needs the [background waiter](#waking-an-idle-session-pseudolife-mcp-wait-mail)
  re-armed after mail or its four-hour timeout; without that, its mail appears
  on its next prompt and the ring listener expires. The watcher
  also stops when Claude Code exits: at once on Linux and macOS, and within
  a minute on Windows, where it lists the process through `ps -W` at arm
  time and then once a minute (a Windows PID is invisible to `kill -0`). In
  `claude -p` runs, Claude Code ends a waiting hook at teardown.
- Codex loads the same `hooks.json` and gets only the park gate, on every
  platform: on Windows through the entry's native command
  (`lifecycle.ps1 -Event Stop`), on macOS and Linux through the same bash
  script, which recognises Codex context the way the other bash hooks do
  (`PSEUDOLIFE_CODEX_HOOK=1`, or `PLUGIN_ROOT` equal to
  `CLAUDE_PLUGIN_ROOT`), unless Claude Code started the hook for its own
  session (`CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID` equal to the
  payload's id), which always stays Claude's. This is the plugin install:
  a manual Codex install (`ops/setup-codex-hooks.py` without the plugin)
  has no `Stop` hook, so no gate. Either path makes the same one request,
  through the managed connection file under the Codex home or the explicit
  daemon
  settings (with the other hooks' checks: an explicit URL may not disagree
  with the managed one, the bearer file must be private), and returns a
  block the way Codex documents for `Stop`, `{"decision": "block",
  "reason": <the message>}` on stdout with exit 0, which Codex turns into a
  continuation prompt, plus the `gate` ledger line; allow, no address, a
  continuation's Stop, or no answer prints nothing. The wake itself stays
  Claude Code's: in Codex context the script exits after the gate and never
  arms the wait (the [doorbell](#codex-doorbell) is Codex's wake path). An
  explicit `PSEUDOLIFE_AGENT_WAKE_HOOK` or `PSEUDOLIFE_AGENT_COORDINATION`
  of `0`, `false`, `no` or `off` turns it off. Whether Codex honours the
  decision of a hook declared `async` has not been probed on a live install.
  `ops/setup-codex-hooks.py` approves it with the other three definitions
  (see [Codex specifics](providers.md#codex-specifics)).
- The plugin's subagent liveness hook (schema v50, `subagent-board.sh`),
  which keeps a Claude Code session's `children` current (see [Delivery and
  recovery](#delivery-and-recovery)), runs on SubagentStart and from a
  second SubagentStop group, separate from the child park gate's group
  below. Both entries are no-ops in Codex: `lifecycle.ps1 -Event
  SubagentBoardStart|SubagentBoardStop` exits at once, and the bash script
  exits in Codex context. Codex's native subagents are linked to their
  parent by the shim instead. `ops/setup-codex-hooks.py` approves the
  entries Codex lists and treats them as optional, like Stop, accepting
  either or both of SubagentStop's two handlers; one disabled in `/hooks`
  stays disabled.
- **A Codex child thread's stop (SubagentStop).** A native Codex child
  (`collaboration.spawn_agent`) or fork has a board address of its own: the
  shim keys it by the child's MCP `threadId` and writes the child's
  `<key>.agent` record under that id. The root's `Stop` never sees it, since
  Codex's hook payloads carry the root thread's `session_id` into a child.
  The plugin's `SubagentStop` entry runs the same park gate at the child's
  stop, keyed by the payload's `agent_id`, which names the child (it equalled
  the child's MCP `threadId` in the 2026-09-29 probe on Codex CLI 0.158.0
  and desktop 0.158.0-alpha.2.1): `lifecycle.ps1 -Event SubagentStop` on
  Windows, `stop-wake.sh subagent-stop` in Codex context elsewhere. The id
  must be the canonical lower-case UUID the shim accepts from `_meta.threadId`;
  anything else, or no `<key>.agent` for the child, asks nothing, and the
  root's `session_id` is never used in its place. A child gets no prompt
  hook, so there is no turn stamp and the daemon judges the child's standing
  record (a block when it has no live park and its status is not
  done-shaped). A block continues the child once, with the same
  `{"decision": "block", "reason": ...}` on stdout; its next stop carries
  `stop_hook_active` and is not asked. The same opt-outs turn it off.
  Claude Code fires `SubagentStop` too and runs the bash command there,
  which does nothing. The entry is synchronous (10 s), plugin installs only,
  like `Stop`; setup approves it with the others and treats it as optional.
  The served message asks the child to park; it never suggests sending mail.

## Startup memory policy (`memory_policy`)

Which standing memory policy the session-start hooks serve. The default is
the short core the memory hook has served since 2026-09-24; the other
variants exist so their effect on agent behaviour can be measured
(`evals/memory_policy_bench.py`) rather than argued.

```yaml
memory_policy:
  variant: compact          # none | compact | full_separate_hook
  ab_arms: []               # e.g. [compact, full_separate_hook] for an online A/B test
```

| Variant | What session start serves |
|---|---|
| `none` | No policy text. The episode line and the briefing still serve; the cold-bank onboarding block, which names memory tools too, does not. |
| `compact` (default) | The short core, ahead of the briefing, in the memory hook's output. Since 2026-09-25 it restates three of the full block's rules: search before stating a "current" version, number or benchmark; correct memory-vs-code drift on the spot; route verified external facts to `memory_world_set`. |
| `full_separate_hook` | The full memory-loop block ([`examples/CLAUDE.memory.md`](../../examples/CLAUDE.memory.md), 7.5 KB), served by a separate SessionStart output (`GET /api/hook/memory-policy`), because the block plus the briefing exceed the 9,500-byte budget of one hook output. |

The separate output is the plugin's third SessionStart handler
(`session-start.sh memory-policy`, or `lifecycle.ps1 -Event MemoryPolicy` in
Codex on Windows); `ops/setup-codex-hooks.py` installs and approves it for
manual Codex hooks too. For every other variant it answers an empty body and
adds nothing. The `install-hook` scripts' settings hooks do not carry it, so
`full_separate_hook` serves no policy to those installs.

Every variant's policy text is served once per conversation. On a resume or a
compaction (SessionStart `source=resume|compact`, forwarded by the plugin's
hooks) neither output re-sends it: the main output keeps the drift notices,
the episode-handle line and a pointer to the full briefing (after a
compaction, also a custom `hook-instructions.md`, which nothing else
carries), and the separate output adds nothing.

`ab_arms` assigns each session the SessionStart hook registers one arm, by a
SHA-256 of its client session id modulo the arm count; a variant may repeat
for an A/A arm. Sessions that reach the hook without a session id keep
`variant`. Each registration records the variant it served in the
session's `client_sessions` row (schema v43, see
[Episodes](episodes.md#session-record)), which outlives the session's root,
so an online comparison can be read from the bank for every hook-registered
session, including those that stored nothing (`evals/capture_metrics.py`
reports sessions per variant). A custom `hook-instructions.md` is served in
every variant.

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
  above zero and `memory_search` also returns `low_confidence: true` when
  the top match scores below the floor and no cortex fact clears
  `memory.cortex.guard_min_score`. No value is calibrated for the current
  embedder, and the pair this guide used to recommend would flag a fifth
  of real searches whose hits agents used:
  [Retrieval](retrieval.md#abstention--confidence-floors). The dense
  relevance floor under it, `memory.search.min_score` (`0.25`), is a
  separate knob and not an abstention signal either.
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
- **Bulk delete confirmation at 20** (`memory.delete_confirm_threshold =
  20`, since 2026-09-29) — `memory_forget(scope="memory")` and
  `POST /api/delete` refuse a match larger than this unless the call
  carries `confirm_bulk: true`; the refusal removes nothing and reports
  `would_delete`, the threshold and up to 20 of the matched texts. The
  guard counts matches whatever filters produced them (a broad substring
  is as dangerous as a bare source). 20 is also the `deleted_texts`
  sample cap, so an unconfirmed delete always lists everything it removed.
  `0` disables. Added after a `text` + `source` delete, under the
  OR-combination of filters then in force, removed every entry in the
  source (1,399; restored from backup); filters now narrow the match, AND
  across kinds like `memory_search`.
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
  `memory_recent`, and every compact entry keeps its write `date`
  (2026-09-25) — none of the four follows the knob); raise
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

### Where the shim lives: side-by-side runtimes behind one launcher

The installers put the shim into its own **runtime** and register one
**launcher** path with every client (`pseudolife_memory/runtimes.py`;
`python ops/shim_runtime.py` from a checkout):

| | Windows | Linux / macOS |
|---|---|---|
| runtimes | `%LOCALAPPDATA%\pseudolife-mcp\runtimes\NNNNNN\` | `$XDG_DATA_HOME/pseudolife-mcp/runtimes/NNNNNN/` (`~/.local/share/...`) |
| launcher | `%LOCALAPPDATA%\pseudolife-mcp\bin\pseudolife-mcp.exe` (its directory goes on the user `PATH`) | `$XDG_DATA_HOME/pseudolife-mcp/bin/pseudolife-mcp`, linked from `~/.local/bin/pseudolife-mcp` |

A runtime is a plain virtualenv holding the package (`pip install
--no-deps`) and the shim's own dependencies — not torch, chromadb or the
embedder, which a stdio shim never loads (`serve` and `embedded` need a
full install). `runtime.json` inside it (version, source, commit) is
written last, so a directory without it is an install that did not
finish; the launcher starts the highest-numbered complete runtime (on
Windows a console-script executable of the kind pip writes, on POSIX a
`/bin/sh` script that `exec`s it). Installing a new version — the
installer, `ops/update.ps1 -All` / `update.sh --all`, or `python
ops/shim_runtime.py install --source <checkout or requirement>` — builds a
new runtime beside the old ones and never touches a file a running session
has open: sessions keep the runtime they started with, the next session
start takes the new one, and an old runtime is removed (by the same step,
or `shim_runtime.py prune`) only once no process runs from it and no
registration names it. A registration that still names a runtime, pipx or
virtualenv path directly is moved to the launcher in place, the file
backed up first and its permission bits kept (`shim_runtime.py migrate`); a
mode argument such as `channel` stays. A registration that would spawn its
own daemon (no `PSEUDOLIFE_MCP_NO_SPAWN=1` and a loopback daemon URL — the
pip and lite tiers) is never moved onto a shim runtime, which cannot serve.
The same steps make `pseudolife-mcp` typed in a terminal reach the
launcher (`shim_runtime.py expose` runs that step alone). On POSIX
`~/.local/bin/pseudolife-mcp` becomes a symlink to the launcher: a free
name is linked; an older pipx link into `venvs/pseudolife-mcp`, or a
pip --user console script of this package, is moved aside as
`pseudolife-mcp.<kind>-<stamp>` (never deleted; `pipx uninstall` removes a
moved pipx link with its venv and leaves the new link, which resolves
outside every pipx venv); anything else is left as it is and named. When
`~/.local/bin` is not on `PATH` the step prints the one-line fix
(`export PATH="$HOME/.local/bin:$PATH"` in your shell profile) and edits no
profile; when an earlier `PATH` entry still wins, it names that entry. On
Windows the launcher directory is prepended to the user `PATH`
(`HKCU\Environment`, never the machine `PATH`) and a settings-change
broadcast reaches consoles opened afterwards. A terminal that was already
open keeps its old `PATH`, so the step says to open a new one when this
one still runs another copy. An older pipx `pseudolife-mcp.exe` further
down `PATH` is left in place; `pipx uninstall pseudolife-mcp` removes it
once no session runs it. `pseudolife-mcp doctor` reports
`path_resolution`: what the name resolves to, the launcher, and a warning
when they differ. Set `PSEUDOLIFE_SHIM_PYTHON` to choose the interpreter
the runtimes are created from; `PSEUDOLIFE_SHIM_RUNTIMES` and
`PSEUDOLIFE_SHIM_LAUNCHER` (together) move both paths, and then no `PATH`
is changed unless `PSEUDOLIFE_SHIM_USER_BIN` names the directory to link
from (on POSIX it also moves the default `~/.local/bin`). A host whose Python cannot make a virtualenv falls back to the
earlier pipx / `pip install --user` install, which does need every session
closed to upgrade.

The installer wires this by default (`ops/install.sh` / `ops/install.ps1`;
pass `--transport http` / `-Transport http` to opt out) because it's the
mechanism that gives **concurrent** Claude Code sessions distinct identity —
an `X-PL-Session` header, the strongest of the five
[session-identity](#session-identity) tiers. Under Claude Code (writer id
unset or `claude-code`) the header is the session id Claude Code launched the
shim with, the same id its SessionStart hook registers, so the shim and the
hook share one session episode. Other hosts get one id per shim process. The
shim opens no episode itself; see [Episodes](episodes.md). The shim works against
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

## Claude Code plugin

### Where the plugin lives: one cache folder per commit

The plugin (`plugin/`, served by the `pseudolife-mcp` marketplace straight
from this repository's master) is an install of its own, like the shim.
Claude Code copies it into its plugin cache,
`~/.claude/plugins/cache/pseudolife-mcp/pseudolife-memory/<version>/`
(`CLAUDE_CODE_PLUGIN_CACHE_DIR` moves the whole `plugins` directory), and
each session runs its hooks from the folder it started with.

Claude Code names that folder by the `version` in the plugin's manifest,
and `plugin/.claude-plugin/plugin.json` deliberately carries none. The
version is then the marketplace commit (12 hex characters, as for
Anthropic's own plugins), so every change to the plugin gets a folder of
its own and an update never replaces a folder a session is using:

- `pseudolife-mcp update`, `ops/update.ps1 -All` / `ops/update.sh --all`
  and `python ops/update_clients.py` refresh the marketplace clone and
  compare its plugin tree with the installed cache, byte for byte (CRLF
  read as LF; the top-level dot entries Claude Code keeps there, such as
  `.in_use/` and `.orphaned_at`, are skipped). When the two differ they run
  `claude plugin update pseudolife-memory@pseudolife-mcp`, which installs
  the new copy in a new folder beside the old one and switches the record
  in `installed_plugins.json`, and they compare again. Nothing is
  uninstalled at any point: an update that fails leaves the installed copy
  installed.
- Sessions already running keep the copy they loaded. A session started
  afterwards runs the new one. `/plugin marketplace update pseudolife-mcp`
  then `/plugin update pseudolife-memory@pseudolife-mcp` inside Claude Code
  do the same.
- Claude Code stamps the replaced folder `.orphaned_at` and deletes it
  itself 14 days later, once no running session marks it `.in_use`
  (Claude Code 2.1.283; the sweep runs at most once a day).
- The plugin's release, which the SessionStart hook reports to the daemon
  for the version handshake, is in `plugin/release.json`, pinned to
  `pyproject.toml` by `tests/test_plugin_packaging.py`.

Until 2026-09-30 the manifest carried the release version. Every plugin
change on master then landed on the same folder name: `/plugin update`
answered "already at the latest version", and the updater's uninstall and
reinstall could not replace a folder running sessions held (EPERM on
Windows), which left the plugin uninstalled until the apps were restarted.
The first update past that change installs a commit-named copy beside the
release-named one. A marketplace clone that still offers the installed
version (an older clone, or a fork that pins one) cannot install beside
it; the updater reports that as failed and leaves the plugin installed.

Codex keeps a copy of its own (a plugin without a version sits under
`local/` in `~/.codex/plugins/cache/`) and runs plugin hooks only once
they are approved; see `ops/setup-codex-hooks.py`.

## Session identity

Every request resolves "which session/episode does this write belong to"
through one chokepoint, evaluated in strict precedence order:

| tier | source | scope | notes |
|---|---|---|---|
| 1 | `X-PL-Session` header | per session | the stdio shim sends this on every call: Claude Code's own session id under Claude Code (the id tier 3 registers), one id per shim process elsewhere, the thread id on each Codex call; any integrator can |
| 2 | explicit `episode` argument | per call | pass an open episode id (or its unambiguous ≥8-char prefix) on `memory_store` / `memory_outcome` / `memory_fact_set`, and on the lifecycle tools `memory_episode_start` / `memory_episode_end` / `memory_session_title` — where a resolved handle wins outright (they never consult the header tiers); the daemon mints it and advertises it in the SessionStart briefing |
| 3 | hook-registered active session | machine-scoped pointer | the SessionStart hook forwards Claude Code's own `session_id`; a SessionEnd hook closes it. A singleton — concurrent sessions race it, which is why the lifecycle tools take the per-call handle |
| 4 | `mcp-session-id` header | per connection | **retired** — the header names the connection (concurrent sessions share it) and the MCP 2026-07-28 revision (SEP-2567, "Sessionless") removes it from the protocol. `PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1` restores it for one release as a rollback hatch |
| 5 | none | — | writer id + idle-gap sessionization (the reaper) — the documented floor when nothing above resolved |

**Why the header outranks the handle when both are present.** A shim
header is infrastructure-asserted, by the host or per OS process; an `episode` handle is
model-supplied and can be confused between two concurrent sessions'
briefings. But identity and target episode are separable — a write still
lands in the handle's named episode even when the header wins identity for
stamping, and it opens no episode for a different header session. An unknown,
closed, or ambiguous handle never fails the write —
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

Moved to its own page: [Sharing one bank across machines](remote-bank.md).
In short, the daemon **refuses to bind a non-loopback host without a token**
(`PSEUDOLIFE_MCP_TOKEN` or a `PSEUDOLIFE_MCP_TOKENS` map), a configured token
lets `/mcp` accept a LAN, tailnet or reverse-proxy `Host` header, and
Postgres stays loopback-only. That page covers the exposure recipes
(Tailscale Serve, LAN publish, reverse proxy), per-machine principals, board
admission, and client-only installs.

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
sidecar (~10 GB with fp32), or ~3 GB in `claude-only` mode (~4.5 GB), where
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
<dir>\ops\install-backup-task.ps1 -ScriptCheckout <dir>   # see below
ops\install-backup-task.ps1 -Uninstall   # remove it
```

The task runs the main checkout's `ops\backup.ps1`, even when installed
from a worktree; the installer warns if that copy predates the row-count
gate. It catches up at the next boot or logon if the machine was off,
waiting up to 10 minutes (`-DockerWaitSeconds`) for Docker to answer
first, and it runs as you, so `PSEUDOLIFE_BACKUP_MIRROR` applies. Each run
is appended to `data\backups\backup-task.log`, headed by the HEAD commit of
the checkout whose `backup.ps1` it ran.

If the main checkout cannot follow master (for example, it holds
uncommitted work), run the backup from a dedicated worktree of master
instead. Create it with `git worktree add --detach <dir> origin/master`
from the main checkout, lock it with `git worktree lock <dir>`, then install
with that worktree's own copy: `<dir>\ops\install-backup-task.ps1
-ScriptCheckout <dir>`. The installer refuses an unlocked worktree, because
worktree cleanup would otherwise delete the script the task runs. It also
refuses a script checkout that would receive the dumps itself, such as a
separate clone running its own installer. Dumps and the log still go to
the main checkout's `data\backups`, where a replica push looks for them;
`restore` from `<dir>` reads its own `data\backups`, so name the files
there with `-BackupFile` (and `-StateArchive`). Move the worktree forward
when you deploy (`git -C <dir> fetch origin master`, then
`git -C <dir> checkout --detach origin/master`); the log shows which commit
each night ran.

On Linux/macOS, a cron entry
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
it cannot honor. Operational telemetry (retrieval/read logs, the client-session
record, the dream-run journal), agent instance credentials, coordination mail and
the board's audit log deliberately stay behind, and the manifest lists exactly which
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

## Updating: `pseudolife-mcp update`

The installed shim carries the update (`pseudolife_memory/update_cli.py`;
`ops/update.ps1` and `ops/update.sh` are thin wrappers over the same code
for a checkout). It needs no checkout.

**The first update from 0.15.0 or earlier** has no such command (that
shim answers "unknown mode", and that daemon never checks for a release,
so nothing announces it): from a checkout, `git pull`, then
`ops/update.sh --all` or `ops/update.ps1 -All`; for a pipx or pip install,
`pipx install --force "pseudolife-mcp[lite]==<version>"` or
`pip install --upgrade "pseudolife-mcp[lite]==<version>"` (the `[lite]`
extra only for a lite install). After that, `pseudolife-mcp update`
exists. The installers do not put the shim launcher's directory
(`~/.local/share/pseudolife-mcp/bin`, `%LOCALAPPDATA%\pseudolife-mcp\bin`)
on `PATH`; until you add it, run the launcher by the full path the
installer and the session notices print.

| command | what it does |
|---|---|
| `pseudolife-mcp update` | Docker tier: pull `ghcr.io/pseudogiant-xr/pseudolife-daemon:<newest release on PyPI>`, back the bank up, tag the running image `…-pre-update-<stamp>`, recreate only the daemon container, wait for `/health` at that version, then the client side. Pip / lite: upgrade the package where it is installed; restart nothing. |
| `pseudolife-mcp update --tag 0.15.1` | the same for a pinned release |
| `pseudolife-mcp update --check` | report only: exit 0 when a newer release exists, 3 when current, 2 when PyPI or the daemon did not answer |
| `--clients-only` / `--daemon-only` | one half: the shim runtime + plugin cache + Codex step, or the daemon. On a client-only machine (run from a shim runtime, no daemon container here) plain `update` and `--clients-only` both move the clients to the daemon's release (the newest release when it does not answer), and `--daemon-only` is refused naming the daemon's host |
| `--reinstall` | recreate the daemon at the version it already runs; with `--clients-only`, install the release even over a checkout-built shim runtime of the same version |
| `--allow-downgrade` | with `--tag`, allow a release older than the one the daemon runs (refused otherwise: the bank's schema may be newer than that release knows) |
| `--env-file <path>` | the compose env file, when the one the container was created with is gone (without it the recreate would reset the Postgres password, the volume names and the bearer, so it stops instead) |
| `--no-backup`, `--rollback-tag`, `--keep-rollbacks`, `--force-rollback-tag`, `--health-retries`, `--health-delay-ms`, `--no-cache-prune`, `--json` | the checkout deploy's knobs, same meaning |

What it refuses: a target it cannot read (PyPI unreachable and no
`--tag`: nothing is guessed), a downgrade without `--allow-downgrade`,
Docker installed but not answering (Docker Desktop stopped is not "no
daemon container": the shim runtime this runs from is never pip-upgraded
in its place), on a pip install an editable checkout, and with
`--clients-only` a release whose version equals a current shim runtime
built from a checkout (a checkout-built daemon still reports the last
release's version, and the launcher starts the newest runtime: the
release build would replace the checkout's code; the refusal names the
checkout command). On Windows a pip install's own upgrade command is printed
rather than run, since pip cannot replace the running console script;
pipx's `install --force` likewise, since it deletes the environment the
command runs from.

Where things come from: the compose files are the ones the running daemon
container was created with (its `com.docker.compose.project.*` labels),
plus `docker-compose.ghcr.yml` beside them; when they are gone the
package's bundled copies (pinned byte-equal to `ops/`) are written under
`~/.pseudolife-mcp/compose/`. The backup is the checkout's `ops/backup.*`
when the compose project's working directory is still a checkout — that
one keeps the row-count gate and the mirror — else the built-in one: a
`pg_dump -Z9` inside the Postgres container, copied out and checked for
the dump's closing marker (a killed dump is a valid gzip of a truncated
file, so a missing marker stops the update with the `.part` artifact
kept), plus a tar of the daemon's `/data`, both under
`~/.pseudolife-mcp/backups` in the checkout scripts' file names (restore
them with `ops/restore.ps1 -BackupFile <path>` / `restore.sh
--backup-file <path>`; the built-in backup keeps no manifest, row-count
gate or mirror, and rotates only its own files older than seven days,
always keeping the newest three of each kind). When the compose files are
gone AND no env file was found beside them, the update stops unless
`--env-file` names one: the bundled compose files without the env would
reset the Postgres password, the volume names and the bearer.
The rollback is the running image tagged by id as
`ghcr.io/pseudogiant-xr/pseudolife-daemon:<version>-pre-update-<stamp>`,
because the GHCR overlay selects the daemon's image through
`PSEUDOLIFE_IMAGE_TAG`: the printed rollback is that variable naming the
tag on the same `docker compose … up -d --no-deps pseudolife-daemon`. A
daemon that comes back healthy at a version other than the one pulled
fails the update with that rollback and moves no client. Release mode
builds nothing, so it prunes no build cache. `PSEUDOLIFE_DOCKER` names
the docker command (the tests use it); `PSEUDOLIFE_MCP_DAEMON_URL` or
`--daemon-url` names the daemon. Release mode moves no Codex hook copy (a
checkout's client step refreshes manual copies; Codex's plugin manager
moves its plugin copy); it prints approval steps only when Codex would ask
again, and the check compares Codex's clone with the daemon just deployed,
never with a checkout at some other commit.

### Being told, and the unattended client half (`updates`)

```yaml
updates:
  check_releases: true          # ask PyPI for the newest release, on a background thread
  check_interval_seconds: 21600 # every six hours; 60 is the floor
  unattended_clients: false     # the shim may take the client half of an update by itself
```

With `check_releases` on (the default) the daemon reads the newest
release from PyPI once per interval on its own thread, never on a request,
keeps the last good answer, and serves it on `/health` as
`updates.latest_release` (with `checked_at`). When that release is newer
than the daemon, the session-start briefing opens with one line naming
the command: `release X is available (daemon Y[, plugin Z]) — run
pseudolife-mcp update`, and no second line about the plugin, since that
command moves both. A daemon with no route to PyPI offers nothing;
`check_releases: false` makes no request at all. The plugin-behind,
daemon-behind and hooks-differ notices, `pseudolife-mcp doctor` and the
shim's own version line all name `pseudolife-mcp update` (or its
`--clients-only` half) first, with the checkout scripts as the
alternative; the hooks-differ notice of a daemon built from a checkout
(`build.source: checkout` on `/health`) names the checkout scripts first.
When `PATH` does not find the shim launcher, the SessionStart hook
reports its path and the notices name the launcher by that path.

`unattended_clients` (default off) lets the safe half run by itself:
when a session's shim finds the daemon running a newer release than the
shim is (the state right after `pseudolife-mcp update --daemon-only`, or
after a release update that moved the daemon but not this client), it
starts `pseudolife-mcp update --clients-only --tag <the daemon's
version>` in the background and says so in its served instructions. That
installs the daemon's release as a new shim runtime beside the running
one and refreshes the plugin cache; the running session keeps its
runtime, the next session starts on the new one, and the Codex step is
still printed for the operator (the log is
`~/.pseudolife-mcp/update-clients.log`). One attempt per release per
hour: the run writes its exit code to a result file beside the log (5
when a client step failed, so a runtime that was never installed is
never reported as finished), and the next session's shim says when the
last attempt failed, and which command to run, instead of trying again.
Only a Docker-tier registration
on this host takes it (the daemon URL is loopback and the registration
carries `PSEUDOLIFE_MCP_NO_SPAWN`, as the Docker-tier installers set it):
on a lite daemon or against a remote one the command could not succeed,
so nothing is started. A plugin cache that is stale while the daemon and
the shim are at the same version is not refreshed unattended; the
briefing's hooks-differ line names the command for that. The daemon
recreate, with its backup and rollback tag, is never taken by this knob:
that stays `pseudolife-mcp update`, run on purpose. (The knob lives in
the daemon's `config.yaml` and reaches the shim through `/health`, so one
setting governs every client of that daemon.) `PSEUDOLIFE_RELEASE_CHECK=0`
in the daemon's environment makes no release request whatever the file
says; the test daemons run with it.

### Unattended daemon updates on headless hosts (`updates.unattended_daemon`)

```bash
pseudolife-mcp update --schedule 03:30   # once: a daily task (Windows) or systemd --user timer (Linux)
pseudolife-mcp update --unattended       # what that run does; exit 0 updated, 3 current, 4 held off
pseudolife-mcp update --unschedule
```

```yaml
updates:
  unattended_daemon: true   # default false: the scheduled run then only reports
```

The scheduled run applies a new release only when BOTH hold: the daemon's
`config.yaml` has `updates.unattended_daemon: true` (the run reads it from
`/health`, so installing the timer alone changes nothing), and the agent
board lists no active session. It then takes the same path as an attended
`pseudolife-mcp update`: backup (the checkout's script or the built-in
`pg_dump` + state tar), the rollback tag, recreate only the daemon, wait
for `/health` at the new version, then the client side; the board is
read once more right before the daemon is recreated, since the backup
can take minutes, and a session that started meanwhile holds the
recreate off before the rollback tag is moved (the backup already taken
is harmless). When a release is out it posts a board notice from the
daemon's reserved principal, stamped with the posting principal, to the
sessions the board lists as active, provided the run's bearer principal is
listed in [`coordination.daemon_notice_principals`](#experimental-agent-coordination)
(empty by default, so out of the box the notice is refused, the run says so
in one line, and the log is the only record): updated (with the rollback tag, the
client states and, when Codex would ask to approve its hooks again, the
approval steps), or held off and why (which sessions are active; the board
unreadable). With the knob off it only logs, so a daily run never
nags. The durable record is `~/.pseudolife-mcp/unattended-update.log`,
which every step line reaches; the notice is best effort. An update succeeds
only when no session is active, so its notice usually reaches nobody
and a session that starts later meets the new daemon through the
version handshake and the log; a failed update posts the failure with
the rollback line when the daemon can still take it, and always logs
it. Idle sessions do not hold it off: a session with no activity in the
board's active window keeps its shim runtime and reconnects to the
recreated daemon on its next call; the held-off notice says how many
were idle (a count of stale addresses, so an upper bound). Exit codes:
0 updated, 3 current, 4 held off, 2 the run could not check, 1 the
update failed. `--unattended` refuses `--no-backup`, `--clients-only`,
`--daemon-only`, `--force-rollback-tag`, `--allow-downgrade`,
`--reinstall` and `--check`: it always backs up and moves the daemon
and the clients together.

One update runs at a time on a host. Every daemon-recreating run
(`pseudolife-mcp update` in release or checkout mode, so also
`ops/update.ps1` and `ops/update.sh`), every `--clients-only` run and the
unattended run hold an exclusive OS lock on `~/.pseudolife-mcp/update.lock`
(under `PSEUDOLIFE_MCP_DATA_DIR` when that is set) until they exit; the OS
drops it if the process dies, and the holder's pid sits beside it in
`update.lock.pid`. An attended run that finds it held exits 2 with
"another pseudolife-mcp update is running (pid N)" and changes nothing; the
unattended run treats it as a hold-off (exit 4, with a notice), never a
failure. The unattended run reads the daemon's version again after taking
the lock and before the backup and the rollback tag, so a release an
attended update applied in the meantime is "nothing to do" (exit 3), never
tagged as the rollback of the version it replaced. `--check` and the pip
tier take no lock; `ops/update_clients.py` does not either.

The "no session is active" check sees only sessions on the agent board:
clients started through the shim with its board adapter, whose bearer
principal is in `coordination.allowed_principals`. It does not see a client
connected over HTTP without the shim, a client that opted out of the board
(`PSEUDOLIFE_AGENT_COORDINATION=0`), or a client whose token-map principal
is not in `allowed_principals`; the daemon can be recreated under any of
them. A board
session counts as active while its own last board action (registering, a
status update, a lease, sending or receiving mail; a heartbeat alone does
not count) is within the last hour (`ACTIVE_WINDOW`, 3600 seconds), or
within the last three hours (`ATTACHED_IDLE_WINDOW`, `STATUS_STALE_AFTER`
of two hours plus `ACTIVE_WINDOW`) while it holds a live attachment lease;
both are in `pseudolife_memory/storage/coordination.py`. Anything quieter
is counted as idle and does not hold the update off.

What the run needs: a Docker-tier daemon on this host and the bearer
(`PSEUDOLIFE_MCP_TOKEN_FILE` or `PSEUDOLIFE_MCP_TOKEN` in the run's
environment) whose principal is admitted to the board, since the idle
check registers a throwaway board address; for its notice, that principal
must also be in `coordination.daemon_notice_principals` (`--schedule`
prints one line saying so). On Linux the unit carries its own token file,
so running `--schedule` with `PSEUDOLIFE_MCP_TOKEN_FILE` naming the
dedicated principal's token (in that shell only) gives the timer its own
bearer. The Windows task runs with the user's User-scope environment
variables, not the shell's, so its bearer is whatever
`PSEUDOLIFE_MCP_TOKEN_FILE` (or `PSEUDOLIFE_MCP_TOKEN`) holds at User
scope. No installer sets that: the installers give each client its own
token file in that client's registration. There the run can post notices
only if that principal is listed, which lets every other process reading
the same User-scope bearer post them too; leaving the list empty keeps the
log as the record.

`--schedule` resolves the bearer the scheduled run will see (Linux: the
token file it writes into the unit; Windows: the User-scope environment)
and refuses with exit 2 when none resolves, naming the command that sets
it: without a bearer the run cannot read the board, so it would hold off
every day with exit 4, which the scheduler counts as success. On Windows
that command is, in PowerShell, once:

```powershell
[Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_TOKEN_FILE", "<path to the token file>", "User")
```

`--allow-no-bearer` installs the schedule anyway, with a warning. Its
output names the bearer the run will use. `--schedule` also refuses on a
pip install, which has no daemon container to update. `--schedule` on Linux writes
`~/.config/systemd/user/pseudolife-update.{service,timer}` with the
daemon URL and the token file as `Environment=` lines (a token given only
as `PSEUDOLIFE_MCP_TOKEN` is written to a private
`~/.pseudolife-mcp/unattended-update.token` first; the token itself never
lands in the unit; `SuccessExitStatus=3 4` keeps "current" and "held
off" from counting as unit failures) and enables the timer. A `--user`
timer runs only while that user has a session unless lingering is on:
`loginctl enable-linger <user>` once, which is the headless case this
is for. On Windows it registers the task `Pseudolife Unattended Update`
through `schtasks`, which on an administrator account needs an elevated
PowerShell opened from the Start menu (the refusal says so); the task
runs while the user is signed in, with the user's User-scope environment
variables, so `PSEUDOLIFE_MCP_TOKEN_FILE` and any
`PSEUDOLIFE_MCP_DAEMON_URL` must be set at User scope (the command
above), not only in a shell. A `--daemon-url` given with `--schedule` rides
along in the task or unit. The task or timer runs the shim launcher
(`pseudolife-mcp update --unattended`), so it follows the side-by-side
runtimes; `--unschedule` removes the task or the unit, the timer and the
private token file. A tokenless install cannot read the board and
therefore never updates unattended; nor does macOS get a scheduler here
(run `--unattended` from your own). The bank backup is taken every time
and is never skipped by this path. The board read registers a throwaway
address labelled "unattended update", pruned by the board after its
hour; a run never counts that label as a session.

### Codex hook re-approval, on every update path

Codex runs only hook handlers it has approved, and it approves a handler by
its definition in `hooks.json` (the command, timeout, `async` and
`statusMessage`), not by the script the command runs: measured on Codex
0.158.0 on 2026-09-30, editing a script left the approval in place and
editing the command asked again. So an update whose scripts changed but
whose `hooks.json` did not needs no approval:

- A plugin copy with older scripts is reported as `behind`: update the
  plugin in Codex's plugin manager and its approvals carry over.
- Manual copies (`setup-codex-hooks.py --source manual`) run through a
  launcher whose commands never change, and the update refreshes them
  itself ([the launcher](providers.md)).

An approval is due only when `hooks.json` changed, when a new handler
position appears, or when a manual copy from before 2026-09-30 still names
a script bundle in its commands (one last approval moves it to the
launcher). When the scripts cannot be compared at all (release mode with no
Claude plugin cache holding the daemon's scripts), a plugin copy whose
scripts differ is treated as needing approval too, to be safe. Then, and
only then, every update path (`pseudolife-mcp
update`, `ops/update.ps1` / `update.sh`, `ops/update_clients.py`, the
unattended run's board notice) prints the complete steps: which files
changed (read from Codex's marketplace clone against the checkout, or
against the Claude plugin cache when that holds the daemon's scripts), the
refresh (update the plugin in Codex's plugin manager so its clone holds the
new scripts; `ops/setup-codex-hooks.py --source plugin` approves what the
clone holds and does not pull one), the approval (`/hooks` in a Codex
session, or `python ops/setup-codex-hooks.py --source plugin --trust yes` /
`--codex-hook-trust yes` for an unattended install), what is off until
then (the handlers Codex has not approved), and the check:
`pseudolife-mcp doctor` reports `codex_hooks = current`, or
`bundle-present` for a manual copy. `tests/test_codex_hook_launcher.py`
pins the fields Codex approves in the plugin's `hooks.json`, so a change
that would ask every Codex user again is a deliberate one. An update that
needs no approval says nothing about it, and neither does one that finds no
marketplace clone at all. A daemon-only update (`ops/update.ps1` without
`-All`, `update --daemon-only`) whose hook scripts changed says in one
line that the client side and these steps are still to do; the shim's
unattended client half leaves the steps beside its result file
(`~/.pseudolife-mcp/update-clients.<version>.codex`) and its next
session's note points at them.

## Schema version history

The current Postgres meta version is **v52**; migrations are additive
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
| v37 | Retire-not-delete (2026-09-03). `store_decisions` (`id`, `store`, `entity_norm`, `attribute_norm`, `action`, `decided_by`, `reason`, `record` JSONB, `decided_at`) — the FK-free audit of lesson/world forgets and restores. A `memory_forget(scope="lesson"\|"world")` now retires the slot's rows (`status='retired'`, rows kept; `memory.compaction` treats them like any non-live record) instead of deleting them, and the audit row carries the verbatim record so `lesson_restore` / `world_restore` (`memory_graph_review(action="restore_slot")`, `POST /api/lessons/restore`, `POST /api/world/restore`) still work after compaction has purged the retired row. Also (no DDL): merge and junk rejects write text-keyed tombstones to `dismissed_pairs` (canonical pair / `junk:<canonical>` self-pair) so a verdict outlives the CASCADE-deleted proposal row. No column changes; the table starts empty on an existing bank, so the migration is a no-op there. Since 2026-10-01 (no DDL) the table also holds lesson lineage rows (`action='lineage'`, `decided_by='lesson_synthesis'`: the entries a synthesised lesson was derived from) and the `retire` rows of lessons retired because a forget removed their last lineage entry (`decided_by='source_cascade'`, `reason='source_forgotten'`). Additive/idempotent |
| v38 | Durable dream acknowledgement. `entries.dream_state` records `pending`, `acknowledged`, or `legacy-covered`; pre-existing rows retain `NULL` for one-time classification against the legacy cursor and configured source eligibility. New writes default to `pending`, regardless of their timestamps. Exact-entry commit tokens use a bank-local secret in `meta`; logical export excludes that secret. The numeric cursor remains display metadata. This preserves the previous migration boundary; it does not repair historical skipped entries. Additive/idempotent |
| v39 | `memory_trace_invalidations` preserves source-supersession events by normalized slot and source entry ID, without entry or fact foreign keys. Explicit correction records entry retirement and existing trace invalidations together. Events survive source deletion, cortex snapshots and compaction; confirmation still clears the served warning. Table creation and older logical imports reconstruct only surviving superseded source/trace pairs. **Upgrade effect:** the first v39 start materialises one event per surviving superseded-source trace pair — 2077 pairs on the reference bank on 2026-09-11, measured with `ops/measure_reverify_population.py`. That reproduces the warnings the bank already served, but from then on they no longer drain when the source is evicted or deleted; each clears only when its slot is confirmed again (`memory_fact_set` at the slot with the same or a new value, or accepting a contender). To clear a population deliberately, re-assert those slots. `re_verify` stays a passive flag and is still excluded from `correct_with`. Additive/idempotent |
| v40 | Agent coordination (2026-09-11). Adds `coordination_agents` for bearer-owned instances, hashed credentials, explicit scope, activity and adapter attachment generations, and `coordination_messages` for one-recipient mail, per-recipient ordering, sender request-key deduplication, expiry and acknowledgment. Agent rows have no episode FK; episode cleanup cannot remove mail. No embeddings or changes to memory tables. Both tables are operational data excluded from portable knowledge exports. Additive/idempotent; existing banks start with empty coordination tables and the feature remains disabled until configured. |
| v41 | Audited continuum entry reinstatement (2026-09-22). Adds `entry_reinstatement_decisions`, an operation-keyed, FK-free append-only audit that survives later entry deletion. A single Postgres transaction binds the reviewed retirement preimage to the decision and clears only the entry's retirement fields; retries use the operation UUID. The first version refuses entries with trace invalidations and leaves all cortex state unchanged. Additive/idempotent; existing banks start with an empty decision table. |
| v42 | Board audit log (2026-09-24). Adds `coordination_events`, an append-only, FK-free, sha256-hash-chained record of every agent-board mutation (register, update with the replaced values, attach, detach, send with its body, first read, ack, attempt, expire, prune, bank identity, restore recover/rebind), written in the mutation's own transaction and pruned only by its own `coordination.audit_retention_days` window (default 90, `0` keeps it forever), which logs its cuts. Adds `coordination_messages.first_read_at`. Operational data, excluded from portable exports like the other coordination tables; read and verified with `pseudolife-mcp board-audit`. The log is cut at most once a day, on UTC day boundaries, and only while the board is in use. Additive/idempotent; existing banks start with an empty log, and history before the upgrade is not reconstructed: a message still unacknowledged at the upgrade has no `send` event, and its first read afterwards is logged as its first read. |
| v43 | Durable client-session record (2026-09-25). Adds `client_sessions`, one FK-free row per session key the daemon registered (the SessionStart hook, or `POST /api/episode/start` from the stdio shim and the CLI episode hooks): `registered_via` (`hook` \| `api`, the first registration's), the bearer's `principal`, `started_at` (first registration, never moves) and `start_times` (every registration, so a resumed client's new shim still pairs with it), `ended_at` + `end_reason` (the most recent close: `end` for SessionEnd or shim exit, `idle` for the reaper; cleared when the session registers again or a store or handle reopens it), the startup memory-policy `policy_variant` the hook assigned, and `episode_ids`, every root episode the session was given. A root that ends holding no entry is still pruned; the row is not, so the searches and outcomes of a session that stored nothing keep a session to count against, and an online `memory_policy.ab_arms` test keeps each session's arm. Written best-effort (a failed write never fails a session start); Postgres only; operational data, excluded from portable exports. Additive/idempotent; existing banks start with an empty table, and sessions before the upgrade are not reconstructed. [Episodes — session record](episodes.md#session-record) |
| v44 | Memory-loop observability (2026-09-25). Adds `lesson_search_events`: one row per `memory_lesson_search` call (query, caller session and episode, the lessons served by `(entity_norm, attribute_norm)` slot key with rank and score; an empty list for a search that found nothing). It is a separate table from `retrieval_events`, whose rows the retrieval replay and telemetry harnesses re-run as `memory_search` calls. FK-free; it shares the retrieval log's switch (`memory.retrieval_log.enabled`) and retention (`retention_days`). Adds `outcome_signals.used_ids` (JSONB): what an outcome's `used_ids` became, as `{"credited", "unmatched", "served_elsewhere"}` id lists, or `{"unchecked", "reason"}` when the label write failed; `NULL` when the outcome named no ids, the log is off, or this best-effort write failed (counted in `retrieval_log.write_errors`). The column is serving telemetry and stays out of portable exports, like the retrieval log. Additive/idempotent; existing rows read `NULL` and the new table starts empty. |
| v45 | Resource leases (2026-09-26). Adds `coordination_leases`, one FK-free row per lease name (holder agent and principal, purpose, the current grant's fence from the `coordination_lease_fence` sequence, so a name's fence never repeats, the acquired, expiry and expected-end times, the estimate the hold was given, and when the lease was last freed, after which a week free and unqueued forgets the row), `coordination_lease_waiters`, each lease's FIFO queue, and `coordination_agents.status_expires_at`, when a status says it stops being true. A process-held lease's truth is an OS file lock that `pseudolife-mcp lease run` takes on the host, and the row mirrors it; a session-held lease (`coordinator:<project>`, `claim:<path>`) lives only here. A freed lease goes to the head of its queue, which must renew within five minutes or lose it to the next. Grants, releases, expiries and operator breaks are audit events; renewals are not. Operational data, excluded from portable exports like the other coordination tables. Additive/idempotent. |
| v46 | Redactable board message bodies (2026-09-26). Adds `coordination_events.body` and `body_salt`. From v46 a `send` event keeps the message body in that column, outside the row hash, and its hashed payload carries sha256(salt || body) (`text_commitment`) instead of the text, and not its length, so `pseudolife-mcp board-audit redact` can remove one body behind a chained operator `redact` event and the chain still verifies. `verify` checks every present body against its salted commitment (`body_mismatch`) and accepts an absent one only behind such an event (`body_missing`). Send events written before v46 keep the body inside the hashed payload, which redaction cannot touch (it still takes their live mailbox copy); they leave the log only through audit retention. The columns are added only when missing, so an open `board-audit export` never blocks the schema pass. The board also refuses credential-shaped message bodies, request ids, statuses, scope fields, capability names, lease names and purposes, and redaction reasons with `secret_like_body` (no DDL). Additive/idempotent; existing rows read `NULL`. [Audit log — redacting a body](#redacting-a-body) |
| v47 | Subagents on the board (2026-09-27). Adds `coordination_agents.children`, a JSON list of `{label, since}` (default `[]`): the subagents a session runs under its own board address. `memory_agents(action="update", children=[...])` sets it (at most 8 labels of at most 40 characters, no duplicates; `[]` clears it, omitting it leaves it), the daemon stamps each label's `since` and keeps it for a label the next update carries over, and `memory_agents(action="list")` returns it on every peer row. The column is added only when missing, like v46's. Additive/idempotent; existing rows read `[]`. [Delivery and recovery](#delivery-and-recovery) |
| v48 | Fan-out mail and id prefixes (2026-09-28). One `memory_message` send may reach every attached, non-idle agent in a project (`to: "project:<name>"`) or on the board (`to: "all"`) under one request id, with one `coordination_messages` row and one `send` audit event per recipient, so the sender's request key becomes the unique index `coordination_messages_request_idx` over `(sender_agent_id, request_id, recipient_agent_id)`. The index is created before the pre-v48 `UNIQUE (sender_agent_id, request_id)` constraint is dropped, and the drop runs only where that constraint exists, so an open `board-audit export` never blocks the schema pass. Agent and message ids may be given by a unique prefix of 8 or more hex characters (no DDL). Additive/idempotent; existing rows are unchanged. [Experimental agent coordination](#experimental-agent-coordination) |
| v49 | Park records and the wake decision (2026-09-28). Adds `coordination_agents.park_reason`, `park_needs`, `park_clear_by`, `park_resume`, `park_expires` and `park_set_at`: a session's standing statement of why it stopped and what clears it, set through `memory_agents(action="update", park_reason=..., ...)`, cleared by a null reason or a plain status update; `coordination_messages.wake`: the wake decision a send returned, repeated on a retry (`NULL` on earlier messages); and `coordination_wakes`: every `rung` or `nudged` ring the daemon decided, with its reason, `ring_at` and `served_at`, read by the caps (`coordination.wake`), the fan-out stagger and the recipient's next attach or heartbeat, and cut after seven days by the prune pass. The columns are added only when missing, like v46's and v47's. Additive/idempotent; existing rows read no park. [Park records and the wake decision](#park-records-and-the-wake-decision) |
| v50 | Subagents as their parent's children (2026-09-30). Adds `coordination_agents.parent_thread`, the parent Codex thread a native subagent registered with (set once at register, `NULL` on every other row), and `parent_agent_id`, the parent's row under the same principal, filled at register or when the parent registers later and cleared when prune removes the parent. A row with a parent thread is refused `memory_message` sends (`child_send_refused`). `children` entries may now carry an `agent_id`: those are the ones the plugin's SubagentStart hook lists, which a parent's update keeps (no DDL). The columns are added only when missing, like v47's and v49's. Additive/idempotent; existing rows read `NULL`, not subagents. [Delivery and recovery](#delivery-and-recovery) |
| v51 | Forget cascade (2026-09-29). Adds `edge_evidence` for newly extracted dream edges. Forgetting an entry retires facts with no remaining current source, retires affected session digests and queues regeneration from surviving entries, and retires dream edges with no remaining current evidence. Older edges without entry provenance are unchanged. Additive/idempotent. |
| v52 | Indexed retained coordination history (2026-10-01). Adds partial send indexes for sender, recipient, exact participant pairs and principal, plus principal timeline, expiry and per-message lifecycle indexes. History seeks each direction independently before merging bounded pages and obtains its cursor high-water mark from two indexed heads. Console reads use principal and message lookups; both read paths cap each SQL statement at five seconds while holding the board lock. Large visible histories or expiry payloads can fail with a sanitized error and be retried. Additive/idempotent; existing audit rows and retention semantics are unchanged. |

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
