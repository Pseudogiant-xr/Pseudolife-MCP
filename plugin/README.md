# pseudolife-memory — Claude Code plugin

Wires a running [Pseudolife-MCP](https://github.com/Pseudogiant-xr/Pseudolife-MCP)
memory daemon into Claude Code. The plugin is the **wiring layer only** — the
daemon stack (Docker: Postgres + extractor + daemon) is installed separately;
see the [Quickstart](https://github.com/Pseudogiant-xr/Pseudolife-MCP#quickstart).

## Install

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Restart Claude Code (or `/reload-plugins`). If the daemon is running you'll
see the memory briefing at the top of each session; if not, the session tells
you how to start it.

## What it replaces

The plugin supersedes the three wiring steps of `ops/install.sh` /
`ops/install.ps1` — you need **neither** if you use the plugin:

| Installer step | Plugin equivalent |
|---|---|
| `claude mcp add … http://127.0.0.1:8765/mcp` | bundled `.mcp.json` |
| SessionStart briefing hook in `settings.json` | bundled hook (curl, no pip package needed) |
| Memory-loop block appended to `~/.claude/CLAUDE.md` | served as session context by the same hook |

**Migrating from installer wiring?** Remove the old pieces so they don't
double up:

1. `claude mcp remove pseudolife-memory`
2. Delete the `pseudolife-mcp briefing` SessionStart entry from
   `~/.claude/settings.json`
3. Remove the "Memory — use it every session" block from `~/.claude/CLAUDE.md`

## Contents

- **MCP server** — `pseudolife-memory` over HTTP at `127.0.0.1:8765/mcp`
- **SessionStart hook** — curls the daemon's `/api/hook/session-start` for
  the memory-loop instructions + briefing (lessons, unsure-abouts, world
  facts). Needs `bash` on PATH (Git Bash on Windows) and `curl` — both ship
  with git / the OS.
- **`/dream`** — agent-led consolidation pass (facts + graph review)
- **`/memory-status`** — daemon health + bank stats readout

## Non-default setups

Both the MCP server entry and the hook read the same two environment
variables — no file editing (a marketplace-installed plugin lives in a
managed cache; local edits are clobbered on update):

- **Different daemon port/host**: export `PSEUDOLIFE_MCP_DAEMON_URL`
  (default `http://127.0.0.1:8765`).
- **`PSEUDOLIFE_MCP_TOKEN` set on the daemon**: export it too — the MCP
  connection and the hook both send it as a bearer. Without it the hook
  still injects the memory-loop instructions, just not the briefing
  (memory content stays token-gated).
