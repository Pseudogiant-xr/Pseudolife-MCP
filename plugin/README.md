# pseudolife-memory — Claude Code plugin

Wires a running [Pseudolife-MCP](https://github.com/Pseudogiant-xr/Pseudolife-MCP)
memory daemon into Claude Code. The plugin is the **hooks + commands layer** —
the daemon stack (Docker: Postgres + extractor + daemon) is installed
separately, and so is the MCP transport; see the
[Quickstart](https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart).

## Install

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Then register the MCP transport (the plugin deliberately doesn't bundle one —
see below). Either re-run the installer, which wires the stdio shim
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
2. Remove the "Memory — use it every session" block from `~/.claude/CLAUDE.md`

(Keep your `claude mcp` registration — the plugin doesn't provide one.)

## Contents

- **SessionStart hook** — curls the daemon's `/api/hook/session-start` for
  the memory-loop instructions + briefing (lessons, unsure-abouts, world
  facts), and registers the session's episode identity. Needs `bash` on PATH
  (Git Bash on Windows) and `curl` — both ship with git / the OS.
- **SessionEnd hook** — closes the session's episode and clears the
  active-session pointer when the session ends.
- **`/dream`** — agent-led consolidation pass (facts + graph review)
- **`/memory-status`** — daemon health + bank stats readout

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
