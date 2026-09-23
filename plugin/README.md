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
to approve the three PseudoLife hook definitions and verify their lifecycle.
Automatic detection reuses a recognized, enabled plugin bundle. If the
runtime cannot support automatic trust, setup gives `/hooks` review guidance
and uses standing instructions when approved. A plugin installation or a
passing script fixture alone does not establish readiness.

Use one hook source so the same event does not run twice; the setup helper
checks for known duplicates. Keep a single MCP transport registration, and follow the
[Codex setup and verification guide](../docs/guide/providers.md#codex-specifics)
for startup budgets, standing instructions and runtime diagnostics.

## Per-turn coordination digest

When the shim's coordination adapter is enabled (`PSEUDOLIFE_AGENT_COORDINATION=1`
in the MCP server's env block), it keeps a small digest file per session under
`~/.pseudolife-mcp/digests/` — the pending addressed messages, rendered once,
behind a watermark that moves only when they change. The UserPromptSubmit hook
reads that file by the `session_id` it receives and prints the digest only when
the watermark passed the `.seen` marker, so a quiet turn adds nothing to the
context and a change appears once. The same marker gates the hint the shim
appends to tool results, so the two paths never repeat each other. SessionStart
on `resume` or `compact` clears the marker so the current digest prints afresh;
the bash hooks do the same on `clear`. Under Claude Code, `/clear` and an
in-session `/resume` give the hooks a new `session_id` while the shim keeps
the one it was launched with, so SessionStart records the shim's key once per
Claude Code process (`claude-<CLAUDE_PID>.host` in the same directory) and the
prompt hook reads through it.
Override the directory with `PSEUDOLIFE_DIGEST_DIR` in *both* the MCP env block
and the hook's environment; they must agree. `ledger.log` in that directory
records one line per hook firing (time, session prefix, watermark, bytes added)
for measuring the cost.

## Why no bundled MCP server?

Earlier versions shipped an HTTP server entry in the plugin. Claude Code
loads a plugin server *alongside* any user-registered server for the same
daemon — no deduplication, doubling every session's tool namespace — and the
only per-server off-switch is disabling the whole plugin, which would also
kill the hooks. Since the stdio shim (per-session identity) can only be
registered outside the plugin, the transport lives with the installer and
the plugin stays hooks-only.

## What it replaces

The plugin supersedes two of the wiring steps of `ops/install.sh` /
`ops/install.ps1`:

| Installer step | Plugin equivalent |
|---|---|
| Session hooks in `settings.json` | bundled hooks (curl, no pip package needed) |
| Memory-loop block appended to `~/.claude/CLAUDE.md` | served as session context by the same hook |

The third step — `claude mcp add` — is **not** replaced: the installer (or
the one-liner above) still owns the MCP transport.

**Migrating from installer hook wiring?** Remove the old pieces so they
don't double up:

1. Delete the `pseudolife-mcp briefing` SessionStart entry from
   `~/.claude/settings.json`
2. Delete the `mid-session discipline` UserPromptSubmit entry from
   `~/.claude/settings.json` (the plugin echoes the same line — keeping
   both injects it twice per turn)
3. Remove the "Memory — use it every session" block from `~/.claude/CLAUDE.md`

(Keep your `claude mcp` registration — the plugin doesn't provide one.)

## Contents

- **SessionStart hook** — curls the daemon's `/api/hook/session-start` for
  the memory-loop instructions + briefing (lessons, unsure-abouts, world
  facts), and registers the session's episode identity. Needs `bash` on PATH
  (Git Bash on Windows) and `curl` — both ship with git / the OS.
- **UserPromptSubmit hook** — echoes a one-line mid-session memory
  discipline on every turn (recall before reviewing code/docs/PRs, then
  compare memory against the files; status questions are memory questions;
  log outcomes). Static — no daemon call, works offline.
- **SessionEnd hook** — closes the session's episode and clears the
  active-session pointer when the session ends.
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
