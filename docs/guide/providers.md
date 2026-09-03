# Providers — one memory bank across every coding agent

The daemon speaks MCP, so any MCP-capable coding agent can use the same
bank. What differs per agent is how much of the **memory loop enforcement**
its platform can carry: Claude Code has a full hook system, Codex has an
experimental one, Gemini CLI has none, and generic MCP clients only get
what the protocol itself delivers. This page is the honest map — what each
provider gets, what its platform cannot support, and what to do about the
gaps. The installer (`ops/install.sh` / `ops\install.ps1`) wires all of
this and prints the same matrix and a per-agent ladder at the end of every
run.

## Capability matrix

| Agent | MCP transport | Session briefing | Per-turn discipline | Standing file |
|---|---|---|---|---|
| Claude Code | stdio shim / HTTP | SessionStart hook or plugin | UserPromptSubmit hook | `~/.claude/CLAUDE.md` |
| OpenAI Codex | stdio shim / HTTP | SessionStart hook (opt-in\*, not on Windows) | — | `~/.codex/AGENTS.md` |
| Gemini CLI | stdio shim / HTTP | — | — | `~/.gemini/GEMINI.md` |
| Other MCP agent | stdio / HTTP (pasted config) | — | — | `AGENTS.md` (your path) |

\* Codex hooks are experimental and off by default — see
[Codex specifics](#codex-specifics) below.

Every agent also gets, with no files touched: the **memory tools**, and the
MCP server **`instructions` field** — a compact statement of the memory
loop that any conforming client shows its model at connect time. That field
is deliberately client-neutral and capped at 512 characters (guard-tested),
so it works identically everywhere.

## The hook-equivalent ladder

Enforcement layers, strongest first. The installer wires the highest rung
each platform supports:

1. **MCP registration** — the tools themselves. Universal.
2. **Server `instructions`** — the protocol-level memory loop. Universal,
   automatic.
3. **Standing instructions file** — the full memory-loop block
   (`examples/CLAUDE.memory.md`) appended to the agent's global context
   file. For hook-less providers this *is* the session briefing, which is
   why the installer recommends the append there — but it never writes a
   standing file without consent: an interactive prompt, or an explicit
   `--instructions append` / `--agents-file`. It never appends silently in
   a non-interactive run.
4. **SessionStart briefing hook** — the daemon-served briefing (memory-loop
   block + what your memory is unsure about + lessons + where you left
   off) injected at session start. Claude Code (hook or plugin), Codex
   (opt-in, not on Windows).
5. **Per-turn discipline line** — a one-line reminder injected on every
   prompt (recall before review, status questions are memory questions,
   log outcomes). Claude Code only.

## One more axis: who dreams

The provider that *talks* to the bank and the model that *consolidates* it
are separate choices, and the installer wires both. `--extractor` /
`-Extractor` takes `sidecar` (the bundled CPU model, the default),
`sonnet-fallback` / `sonnet-only` (a Claude Max plan via the CLI shim), or
`codex-fallback` / `codex-only` (a ChatGPT plan via the Codex CLI shim) —
independently of `--client`. Any OpenAI-compatible endpoint works without a
shim at all. See [Dreaming](dreaming.md) for the wiring and the measured
extraction quality per model.

## Claude Code

Full parity. The [plugin](../../plugin/README.md) is the recommended
hooks/commands layer — it is the only path that registers the session
identity with the daemon (SessionStart forwards Claude Code's own
`session_id`) and closes the episode on SessionEnd. `ops/install-hook.*`
is the non-plugin fallback: it installs the SessionStart briefing
(`pseudolife-mcp briefing --hook-json`) and the per-turn discipline line,
but no SessionEnd hook and no identity registration — those sessions fall
back to the shim header or idle-gap sessionization (see
[Episodes](episodes.md#session-lifecycle--daemon-owned-episodes)). The MCP
transport comes from the installer either way (stdio shim by default),
registered with `PSEUDOLIFE_WRITER_ID=claude-code` so writes are
attributed per provider.

Claude Code reads `CLAUDE.md`, not `AGENTS.md` — see
[the AGENTS.md standard](#the-agentsmd-standard) for the one-line bridge.

## Codex specifics

MCP wiring is first-class (`codex mcp add`, shim or `--url` HTTP;
`PSEUDOLIFE_WRITER_ID=codex`). The session briefing needs Codex's hook
engine, which is **experimental and off by default**:

```toml
# ~/.codex/config.toml
[features]
codex_hooks = true
```

Then `ops/install-hook.sh --client codex` writes the SessionStart hook into
`~/.codex/hooks.json`, and Codex still skips it until you review and trust
its exact definition (start Codex, open `/hooks`, approve). Two further
limits: Codex hooks are **not available on Windows** — there the standing
`~/.codex/AGENTS.md` block is the briefing, and the installer offers to
append it — and Codex has no per-prompt hook, so the per-turn discipline
line has no Codex equivalent anywhere.

Codex can also filter tools **client-side, per project**: a project-scoped
`.codex/config.toml` (loaded for trusted projects only) may register the
server with an `enabled_tools` allow-list, exposing just a subset of the
memory tools to that one project (`disabled_tools` is the matching
deny-list, applied after it):

```toml
# <project>/.codex/config.toml — trusted projects only
[mcp_servers.pseudolife-memory]
url = "http://127.0.0.1:8765/mcp"
enabled_tools = ["memory_search", "memory_store", "memory_outcome"]
startup_timeout_sec = 20
tool_timeout_sec = 60
```

This complements the daemon's server-side
[toolset tiers](configuration.md#toolset-tiers): tiers key the roster to
the caller's identity for every session, while `enabled_tools` narrows it
further for a single project without touching the daemon. One operational
note (verified live 2026-08-31): Codex does not hot-reload newly added MCP
servers — restart the session after registering one.

## Gemini CLI

MCP wiring is first-class and scriptable (flags verified against Gemini CLI
0.57.0):

```bash
gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory pseudolife-mcp
# or HTTP:
gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp
```

`-s user` matters — Gemini defaults to *project* scope.

> **Auth caveat:** since 2026-06-18 Google no longer serves individual-tier
> accounts (free, Google AI Pro, AI Ultra) through Gemini CLI — OAuth
> sign-in fails with `IneligibleTierError`, pointing at Antigravity as the
> migration path. The wiring above is auth-independent and stays correct,
> but to actually run sessions an individual account needs API-key auth
> (set `GEMINI_API_KEY`); enterprise Gemini Code Assist licenses keep
> working unchanged.

If you migrated to **Google Antigravity** (where that error points), it can
use the same bank. Its global MCP config is
`~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "pseudolife-memory": {
      "command": "pseudolife-mcp",
      "args": [],
      "env": {
        "PSEUDOLIFE_WRITER_ID": "antigravity",
        "PSEUDOLIFE_MCP_NO_SPAWN": "1"
      }
    }
  }
}
```

A running Antigravity picks the file up from the refresh button in
Settings → Customizations → Installed MCP Servers, and asks per-tool
approval on first use. Verified live 2026-08-31: tools discovered, search
and fact writes round-tripped, writes attributed as writer `antigravity`.

`PSEUDOLIFE_MCP_NO_SPAWN=1` belongs on Docker-tier shim registrations
(every provider): it makes the shim wait for the compose container instead
of spawning a host fallback that can shadow the real bank after a reboot —
drop it only on the `[lite]` pip tier, where the spawn fallback is the
zero-config path. Gemini CLI has no
hook system that can inject session context, so the standing file is the
briefing: the installer offers to append the block to `~/.gemini/GEMINI.md`
(Gemini's default context file on a stock install; it also reads
`AGENTS.md` where that has been configured as the context file name).

## Other MCP agents (Cursor, Windsurf, Zed, Copilot CLI, …)

`--client generic` prints two paste-ready `mcpServers` shapes — stdio shim
(per-session identity, needs `pip install pseudolife-mcp`) and plain HTTP —
plus the usual config homes per tool. These agents get the tools and the
server `instructions` field; there is no hook layer to wire, so pair the
config with a standing `AGENTS.md` block (the installer offers a
consent-gated append to a path you choose, or `--agents-file <path>`
non-interactively). Writes arrive as the neutral `mcp-client` writer unless
you set `PSEUDOLIFE_WRITER_ID` in the server's `env`.

## The AGENTS.md standard

`AGENTS.md` is the cross-vendor standard for standing agent instructions
(launched by OpenAI in 2025, since transferred to the Linux Foundation's
Agentic AI Foundation; read by 30+ agents including Codex, GitHub Copilot,
Cursor, Gemini CLI, Zed, and Windsurf). A per-project `AGENTS.md` carrying
the memory block reaches almost every agent at once. Claude Code is the
holdout — it reads `CLAUDE.md` — but a `CLAUDE.md` whose **first line is
`@AGENTS.md`** imports the shared file, so one copy serves every tool:

```
@AGENTS.md
```

## Writer ids

Each first-class provider's shim registration carries its own
`PSEUDOLIFE_WRITER_ID` (`claude-code` / `codex` / `gemini`), which the shim
forwards as the `X-PL-Writer` header — so a shared bank can tell which
agent wrote what, and toolset tiers can be keyed per client. HTTP
registrations cannot carry env; there the daemon-side default in `ops/.env`
applies (the installer sets it to the single selected provider's id, or the
neutral `mcp-client` for multi-provider and generic installs). Details:
[session identity](configuration.md#session-identity) and
[toolset tiers](configuration.md#toolset-tiers).
