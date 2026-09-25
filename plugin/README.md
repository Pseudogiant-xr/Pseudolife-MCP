# pseudolife-memory — Claude Code plugin

Wires a running [Pseudolife-MCP](https://github.com/Pseudogiant-xr/Pseudolife-MCP)
memory daemon into Claude Code. The plugin is the **hooks + commands layer** —
the daemon stack (Docker: Postgres + extractor + daemon) is installed
separately, and so is the MCP transport; see the
[Quickstart](https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart).

## Install

The repo installer (`ops/install.sh` / `ops\install.ps1`) installs the
plugin whenever Claude Code is a selected client, adding the marketplace
first if needed, and reports the result on its wiring ladder
(`--claude-plugin skip` / `-ClaudePlugin skip` opts out). By hand, inside
Claude Code:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Then register the MCP transport (the plugin deliberately doesn't bundle one —
see below). Either run the installer, which wires the stdio shim
(recommended: per-session identity for concurrent sessions):

```
./ops/install.sh        # or .\ops\install.ps1 on Windows
```

or add the HTTP transport directly, no pip package needed:

```
claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
```

Restart Claude Code (or `/reload-plugins`). If the daemon is running you'll
see the memory briefing at the top of each session; if not, the session tells
you how to start it.

## Codex compatibility

When loaded by a current Codex runtime, the plugin's lifecycle hooks use the
same events. On Windows, `commandWindows` runs native PowerShell 7 helpers;
Claude keeps the Bash commands. With the daemon running, use
`python ops/setup-codex-hooks.py` from the repository, or the Docker installer,
to approve the PseudoLife hook definitions and verify their lifecycle (the
`Stop` entry is Claude Code's opt-in wake hook and a no-op in Codex).
Automatic detection reuses a recognized, enabled plugin bundle. If the
runtime cannot support automatic trust, setup gives `/hooks` review guidance
and uses standing instructions when approved. A plugin installation or a
passing script fixture alone does not establish readiness.

Use one hook source so the same event does not run twice; the setup helper
checks for known duplicates. Keep a single MCP transport registration, and follow the
[Codex setup and verification guide](../docs/guide/providers.md#codex-specifics)
for startup budgets, standing instructions and runtime diagnostics.

## Startup check-in and per-turn coordination

Memory and coordination have separate SessionStart and UserPromptSubmit
handlers. The coordination startup handler asks the agent to update its
project, task and status with `memory_agents`, list relevant peers, and read
pending `memory_message` mail. It runs independently of the daemon briefing;
neither handler depends on the other running first. Coordination identity
and credentials remain owned by the existing shim adapter.

Coordination is on by default, behind bearer authentication. The startup
handler makes one bounded request (`GET /api/hook/coordination-start`, two
seconds, no retry) with the same connection and credential settings as the
memory handler, and prints the check-in only when the daemon serves it, which
it does where the board is on for that bearer (the daemon cannot see whether
the client has an adapter). A disabled board
(`coordination.enabled: false`), an open install, an unlisted principal, or
a daemon that does not answer adds nothing, and neither does a client that
sets `PSEUDOLIFE_AGENT_COORDINATION` to anything but `1`/`true`/`yes`/`on` in
the hook's environment.

When the shim's coordination adapter is up (the default for a shim holding a
bearer token the daemon serves the board to; `PSEUDOLIFE_AGENT_COORDINATION=0`
in the MCP server's env block turns it off), it keeps a small digest file per
session under
`~/.pseudolife-mcp/digests/` — the pending addressed messages, rendered once,
behind a watermark that moves only when they change. The coordination UserPromptSubmit hook
reads that file by the `session_id` it receives and prints the digest only when
the watermark passed the `.seen` marker, so a quiet turn adds nothing to the
context and a change appears once. The same marker gates the hint the shim
appends to tool results, so the two paths never repeat each other. SessionStart
on `resume`, `compact` or `clear` clears the marker so the current digest
prints afresh. Under Claude Code, `/clear` and an
in-session `/resume` give the hooks a new `session_id` while the shim keeps
the one it was launched with, so the session hooks keep the shim's key once
per Claude Code process (`claude-<CLAUDE_PID>.host` in the same directory).
The prompt hook reads through it while it is confirmed for the current
session. It passes to the next session only through a SessionEnd handoff bound
to the process's creation time, so a record a dead process left is never
followed.
Override the directory with `PSEUDOLIFE_DIGEST_DIR` in *both* the MCP env block
and the hook's environment; they must agree. `ledger.log` in that directory
records one line per hook firing (time, session prefix, watermark, bytes added)
for measuring the cost.

The hook carries bounded previews, not complete handoffs. Use
`memory_message(action="receive")` for full messages and acknowledge only
after reading. A message can contain up to 8,192 UTF-8 bytes; larger handoffs
should reference an artifact the recipient can access. Delivery hints and
acknowledgments do not establish that requested work is complete.

## Why no bundled MCP server?

Earlier versions shipped an HTTP server entry in the plugin. Claude Code
loads a plugin server *alongside* any user-registered server for the same
daemon — no deduplication, doubling every session's tool namespace — and the
only per-server off-switch is disabling the whole plugin, which would also
kill the hooks. Since the stdio shim (per-session identity) can only be
registered outside the plugin, the transport lives with the installer and
the plugin stays hooks-only.

## What it replaces

The plugin replaces installer hook wiring and provides concise startup
guidance. Keep the full standing memory policy when detailed guidance is needed:

| Installer step | Plugin equivalent |
|---|---|
| Session hooks in `settings.json` | bundled hooks (curl, no pip package needed) |
| Memory-loop block appended to `~/.claude/CLAUDE.md` | concise core served at startup; full standing policy remains a separate reference |

The transport step — `claude mcp add` — is **not** replaced: the installer (or
the one-liner above) still owns the MCP transport.

**Migrating from installer hook wiring?** Remove the old pieces so they
don't double up. Rerunning the installer with Claude Code selected offers to
do this for you once the plugin is installed and enabled for all projects:
it lists the entries, backs up `~/.claude/settings.json`, and removes only
the exact commands the installers wrote (`--claude-legacy-hooks remove` /
`-ClaudeLegacyHooks remove` for an unattended run;
`ops/install-hook.sh --remove-legacy` or `ops\install-hook.ps1 -RemoveLegacy`
on their own, `--dry-run` / `-DryRun` to list first). An entry you edited is
listed for review and left alone. By hand:

1. Delete the `pseudolife-mcp briefing` SessionStart entries from
   `~/.claude/settings.json`: the briefing, and the `--coordination`
   check-in that installers write since 2026-09-25
2. Delete the `mid-session discipline` UserPromptSubmit entry from
   `~/.claude/settings.json` (the plugin echoes the same line — keeping
   both injects it twice per turn)
3. Remove any installer-added `Pseudolife coordination:` SessionStart echo
   (2026-09-24 installs) from `~/.claude/settings.json`; the plugin supplies
   its own check-in hook.
   Keep the full standing memory policy if you rely on its detailed guidance.
4. Delete any `pseudolife-mcp episode-start` SessionStart or
   `pseudolife-mcp episode-end` SessionEnd entry (installs from before
   2026-07-14); the daemon owns episodes now.

(Keep your `claude mcp` registration — the plugin doesn't provide one.)

## Contents

- **Memory SessionStart hook** — curls the daemon's `/api/hook/session-start`
  for concise memory guidance and a bounded briefing, and registers the
  session's episode identity. Needs `bash` on PATH
  (Git Bash on Windows) and `curl` — both ship with git / the OS.
- **Memory-policy SessionStart hook** — curls `/api/hook/memory-policy`,
  which returns the full memory-loop block only when the daemon's
  `memory_policy.variant` is `full_separate_hook` (an output of its own, so
  it never shares the briefing's budget); otherwise it adds nothing. See
  [Configuration](../docs/guide/configuration.md#startup-memory-policy-memory_policy).
- **Coordination SessionStart hook** — preserves the local digest mapping
  across supported session changes, then prints the agent check-in the
  daemon serves where the board works (one bounded request).
- **Memory UserPromptSubmit hook** — echoes a one-line mid-session memory
  discipline on every turn (recall before reviewing code/docs/PRs, then
  compare memory against the files; status questions are memory questions;
  log outcomes). Static — no daemon call, works offline.
- **Coordination UserPromptSubmit hook** — reads changed local mailbox previews
  independently of the memory reminder, without a daemon call.
- **SessionEnd hook** — closes the session's episode and clears the
  active-session pointer when the session ends.
- **Stop hook** (opt-in, `PSEUDOLIFE_AGENT_WAKE_HOOK=1`) — waits in the
  background after each turn and wakes the idle session when new addressed
  board mail arrives; off by default. See
  [Configuration](../docs/guide/configuration.md#waking-an-idle-claude-code-session-the-stop-hook).
- **`/dream`** — judgment session over the review queues (graph triage; manual fact extraction only where no extractor is configured)
- **`/memory-status`** — daemon health + bank stats readout

## Updating

The plugin lives in a cache Claude Code refreshes only on request, so it
does not move when the daemon is redeployed. The repo's updater moves it
with the daemon: `ops\update.ps1 -All` / `ops/update.sh --all` (or
`python ops/update_clients.py` on its own) refreshes the marketplace
clone, compares the plugin tree byte for byte against the cache, and
reinstalls the plugin only when they differ — the plugin's version string
is pinned to the package version, so `/plugin update` alone says "already
latest" after a plugin-only change. By hand, when the version did change:

```
/plugin marketplace update pseudolife-mcp
/plugin update pseudolife-memory@pseudolife-mcp
```

Either way, start a new session afterwards. The SessionStart hook sends
the plugin's version and a digest of its hook scripts to the daemon; when
the version differs, or the version matches but the hooks do not, the
session briefing opens with a one-line notice naming the command that
moves it, so a stale cache no longer runs silently.

## Non-default setups

The hooks read the same two environment variables — no file editing (a
marketplace-installed plugin lives in a managed cache; local edits are
clobbered on update):

- **Different daemon port/host**: export `PSEUDOLIFE_MCP_DAEMON_URL`
  (default `http://127.0.0.1:8765`).
- **`PSEUDOLIFE_MCP_TOKEN` set on the daemon**: export it too — the hooks
  send it as a bearer. Without it the session-start hook still injects the
  memory-loop instructions, just not the briefing (memory content stays
  token-gated).
