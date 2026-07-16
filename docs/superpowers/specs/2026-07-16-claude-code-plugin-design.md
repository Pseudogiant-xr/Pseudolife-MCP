# Claude Code plugin + in-repo marketplace — design

2026-07-16. Motivated by the claude-mem comparison: the plugin marketplace is
a discovery channel we're absent from, and a plugin can *replace* the two
ugliest wiring steps (settings.json hook surgery, the CLAUDE.md append) rather
than adding a parallel path.

## Verified platform facts (code.claude.com docs, 2026-07-16)

- Marketplace manifest: `.claude-plugin/marketplace.json` at repo root;
  plugin entries may `source: "./plugin"` inside the same repo.
- Plugin `.mcp.json` supports `{"type": "http", "url": ...}` — identical to
  `claude mcp add --transport http`.
- Hook commands run in **PowerShell by default on Windows**, `sh -c` on Unix;
  a per-hook `"shell": "bash"` selects Git Bash on Windows (our users have
  git). One bash-syntax command therefore works on all platforms.
- **SessionStart hook stdout is injected into context as-is** (10,000-char
  cap). No JSON wrapping needed.
- Plugins **cannot** ship a CLAUDE.md — a SessionStart hook is the only
  standing-instruction mechanism a plugin has.
- Third-party marketplace auto-update is off by default; authors bump
  `plugin.json` `version`, users pull via `/plugin marketplace update`.

## Shape

One plugin, `pseudolife-memory`, living in `plugin/` in this repo; the repo
itself is the marketplace:

```
.claude-plugin/marketplace.json      # marketplace "pseudolife-mcp"
plugin/
├── .claude-plugin/plugin.json       # name pseudolife-memory, version == pyproject
├── .mcp.json                        # {"type":"http","url":"http://127.0.0.1:8765/mcp"}
├── hooks/hooks.json                 # SessionStart curl hook (shell: bash)
├── commands/
│   ├── dream.md                     # examples/commands/dream.md minus its header comment
│   └── memory-status.md             # /health + memory_stats readout
└── README.md                        # install, migration from installer wiring, token note
```

Install story becomes:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

The Docker stack still comes from `git clone` + `ops/install.sh` — the plugin
is the wiring layer only. Its SessionStart hook tells Claude when the daemon
is down and how to point the user at the stack install.

## SessionStart hook — one curl, zero host deps

The pip CLI (`pseudolife-mcp briefing`) is just a urllib wrapper over the
daemon's REST `/api/briefing`; the plugin cuts out the pip dependency by
hitting a new **plain-text** endpoint directly:

```json
{"SessionStart": [{"hooks": [{
  "type": "command", "shell": "bash", "timeout": 10,
  "command": "curl -sf --max-time 5 http://127.0.0.1:8765/api/hook/session-start || echo '<daemon-down guidance>'"
}]}]}
```

`GET /api/hook/session-start` (new, special-cased in `web/api.py` like
`/health`, *before* the JSON dispatcher):

- returns `200 text/plain`, always — a briefing must never break a session;
- body = the **memory-loop instruction block** (the standing instructions
  that today require the CLAUDE.md append), plus the briefing markdown
  (`svc.session_briefing`) when available;
- **auth**: browser gate applies (tokenless stays loopback-only); the bearer
  check does not 401 — an unauthorized request (token set, no header) gets
  the *instructions only*, never memory content. Default local installs
  (no token) get instructions + briefing. Token users can add the header via
  their own settings hook, or live with instructions-only;
- briefing failures are swallowed server-side (instructions still served);
  total body capped ~9,500 chars under the 10k hook cap.

The instruction text lives as `MEMORY_LOOP_BLOCK` in a new
`pseudolife_memory/web/session_hook.py` (module constant — no package-data
changes), and must equal `examples/CLAUDE.memory.md` minus its leading HTML
comment (guard test).

This makes the CLAUDE.md append **optional** for plugin users: the same block
arrives as hook context every session, and uninstalling the plugin removes it
cleanly.

## Coexistence with the installer

- `ops/install.sh` / `install.ps1`: before steps 8–10 (session hooks,
  CLAUDE.md block, `claude mcp add`), detect
  `~/.claude/plugins/marketplaces/pseudolife-mcp` and skip those steps with a
  message — the plugin owns the wiring.
- Existing installer-wired users who add the plugin double up (two briefing
  injections, duplicate server name). `plugin/README.md` documents the
  migration: `claude mcp remove pseudolife-memory`, remove the settings.json
  briefing hook (re-run installer prints it), drop the CLAUDE.md block.

## Guards (all RED-first)

1. `MEMORY_LOOP_BLOCK` == `examples/CLAUDE.memory.md` body (comment stripped).
2. `plugin/commands/dream.md` == `examples/commands/dream.md` (comment stripped).
3. `plugin/.claude-plugin/plugin.json` version == `pyproject.toml` version —
   **the release version-cut now touches five files** (CLAUDE.md checklist
   updated).
4. Manifest sanity: marketplace source `./plugin`, plugin name, http URL in
   `.mcp.json`, hook command hits `/api/hook/session-start` and carries a
   daemon-down fallback.
5. Endpoint tests: 200 text/plain with instructions; briefing appended when
   available; token-set + no bearer → instructions only; briefing exception →
   instructions only; 10k cap respected.
6. PII sweep covers the new files automatically (tracked-tree guard).

## Out of scope

Auto-starting the Docker stack from the plugin; serving the hook cross-host;
plugin-side token configuration; registry/PyPI packaging changes. The
`/api/hook/session-start` endpoint is additive — no schema bump.
