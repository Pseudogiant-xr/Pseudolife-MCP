# Providers — one memory bank across every coding agent

The daemon speaks MCP, so any MCP-capable coding agent can use the same
bank. What differs per agent is how much of the **memory loop guidance**
its platform can carry: Claude Code and current Codex runtimes have
lifecycle hooks, while generic MCP clients only get
what the protocol itself delivers. This page is the honest map — what each
provider gets, what its platform cannot support, and what to do about the
gaps. The installer (`ops/install.sh` / `ops\install.ps1`) wires all of
this and prints the same matrix and a per-agent ladder at the end of every
run.

## Capability matrix

| Agent | MCP transport | Session briefing | Per-turn discipline | Standing file |
|---|---|---|---|---|
| Claude Code | stdio shim / HTTP | SessionStart hook or plugin | UserPromptSubmit hook | `~/.claude/CLAUDE.md` |
| OpenAI Codex | stdio shim / HTTP | SessionStart hook (trust required\*) | UserPromptSubmit hook | `~/.codex/AGENTS.md` |
| Gemini CLI | stdio shim / HTTP | — | — | `~/.gemini/GEMINI.md` |
| Other MCP agent | stdio / HTTP (pasted config) | — | — | `AGENTS.md` (your path) |

\* Hook availability depends on the runtime and policy — see
[Codex specifics](#codex-specifics) below.

Every agent also gets, with no files touched: the **memory tools**, and the
MCP server **`instructions` field** — a compact statement of the memory
loop supplied at connect time; the client controls how it uses that field. It
is deliberately client-neutral and capped at 512 characters (guard-tested),
and the stdio shim forwards the running daemon's value unchanged.

## The hook-equivalent ladder

The installer combines the available delivery layers. These guide the model;
they do not enforce semantic compliance with every memory instruction:

1. **MCP registration** — the tools themselves. Universal.
2. **Server `instructions`** — the protocol-level memory loop. Universal,
   automatic.
3. **Standing instructions file** — the full memory-loop block
   (`examples/CLAUDE.memory.md`) appended to the agent's global context
   file. For hook-less providers this supplies the full policy, without a
   live briefing, so the installer recommends the append — but never writes a
   standing file without consent: an interactive prompt, or an explicit
   `--instructions append` / `--agents-file`. Codex's automatic-memory
   approval also covers this fallback if hook verification fails. Unattended
   Codex setup requires explicit `--codex-hook-trust yes` for that combined
   approval; `--instructions skip` always prevents a standing-file edit.
4. **SessionStart briefing hook** — the daemon-served briefing (memory-loop
   block + what your memory is unsure about + lessons + where you left
   off) injected at session start. Claude Code (hook or plugin), Codex
   (approve setup or review the definitions in `/hooks` first).
5. **Per-turn discipline line** — a one-line reminder injected on every
   prompt (recall before review, status questions are memory questions,
   log outcomes). Claude Code and current Codex runtimes.

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
`PSEUDOLIFE_WRITER_ID=codex`). Current Codex runtimes support SessionStart,
UserPromptSubmit, and SessionEnd on Windows as well as Unix. Hooks are enabled by
default; the canonical feature key is `hooks` (`codex_hooks` is a deprecated
alias). A managed policy or `[features] hooks = false` can disable them.
This is runtime support, not a model capability or a promise about ordinary
ChatGPT conversations. See the [official hook protocol](https://learn.chatgpt.com/docs/hooks).

The Docker installer defaults to automatic hook-source detection. One setup
choice enables automatic memory briefings, reminders, and session cleanup,
uses standing instructions only, or skips this integration. The automatic
choice approves just PseudoLife's exact current hook definitions and permits
the standing memory block as a fallback if verification fails. It does not
approve unrelated hooks or turn off Codex's trust checks.

For an existing installation with the daemon running, use the same helper:

```bash
python ops/setup-codex-hooks.py
```

Auto detection reuses an enabled PseudoLife plugin when its complete hook
bundle matches this installation. Otherwise it installs a private copy of
the three lifecycle scripts and writes manual definitions to the Codex home
(`~/.codex/hooks.json` by default). Known old manual definitions are handled
to avoid duplicate PseudoLife events; unrelated hooks are preserved. An
incomplete or unrecognized plugin bundle requires review rather than
automatically granting trust. Disabled hooks and intentional feature or
policy restrictions remain in place.

| Setting | Docker installer | Standalone helper |
|---|---|---|
| Hook source; default `auto` | `--codex-hooks auto\|manual\|plugin\|skip` | `--source auto\|manual\|plugin\|skip` |
| Scoped approval; default `ask` | `--codex-hook-trust ask\|yes\|no` | `--trust ask\|yes\|no` |
| Standing block; default `auto` | `--instructions auto\|append\|skip` | `--instructions auto\|append\|skip` |

PowerShell uses `-CodexHooks`, `-CodexHookTrust`, and `-Instructions` with
the same values. For unattended setup, explicit `yes` authorizes scoped
trust and the fallback; `ask` without an interactive terminal does not grant
approval. The helper also accepts `--non-interactive` to disable prompting.

```bash
# Full unattended setup with automatic memory approved:
ops/install.sh --extractor sidecar --client codex --codex-hook-trust yes
# Existing installation, same scoped approval:
python ops/setup-codex-hooks.py --trust yes --non-interactive
# Standing instructions only:
python ops/setup-codex-hooks.py --source skip --instructions append --non-interactive
```

```powershell
ops\install.ps1 -Extractor sidecar -Client codex -CodexHookTrust yes
# Standing instructions only:
ops\install.ps1 -Extractor sidecar -Client codex -CodexHooks skip -Instructions append
```

`--instructions append` can keep a standing copy even with working hooks.
Explicit `--instructions skip` prevents that edit, including fallback;
`--source skip` leaves existing hooks alone. The older
`ops/install-hook.ps1 -Client codex` and `ops/install-hook.sh --client codex`
still write briefing and reminder definitions, but do not perform the new
trust and readiness workflow.

The Windows installer and plugin supply
`commandWindows` overrides; Claude
keeps its Bash plugin commands. Plugin SessionEnd on Windows uses a bounded
request inside Codex's three-second maximum, with idle reaping as fallback.
SessionStart retries one transient failure within its 15-second budget. The
native command escapes non-ASCII context so redirected JSON stays valid under
Windows OEM code pages as well as UTF-8.

**Readiness requires execution.** The helper obtains hook identities and
hashes from the installed Codex runtime, backs up configuration, and persists
approved trust through Codex's configuration interface. It then checks the
startup briefing, per-prompt reminder, and episode open/close effects through
an actual local Codex lifecycle, without sending requests to an external
model provider. It reports
memory ready only when those checks pass. The check uses the configured Codex
home in a temporary workspace; project-specific overrides or a different app
runtime can affect another task. Start a fresh task in your Codex application
to receive its startup briefing.

If the runtime's hook or trust interface is unavailable or unsupported, or a
hook fails verification, setup reports what remains unresolved and provides
`/hooks` repair guidance. It installs the standing block only when approved;
installed files or saved hashes alone never count as working hooks. New or
changed definitions require approval again. Manual script bundles use
content-specific paths so updating their code also changes the definitions.

### Hooks versus AGENTS.md

The default SessionStart policy and `examples/CLAUDE.memory.md` contain the
same standing memory instructions. This equivalence covers the **memory
block**, not the rest of a project's `AGENTS.md`: personality, coding rules,
project conventions, and other instructions still belong there. A custom
daemon `hook-instructions.md` can override the default hook policy.

| Mechanism | What it supplies |
|---|---|
| `AGENTS.md` memory block | Standing guidance to recall, capture, and reflect when the client loads instructions |
| `SessionStart` | The memory policy, a live briefing, and session episode identity |
| `UserPromptSubmit` | A short memory reminder on each prompt |
| `SessionEnd` | Automatic session episode cleanup |

Hooks provide timed execution and lifecycle bookkeeping. Their briefings
and reminders still rely on the model to act on instructions: they do not
block work when recall is skipped or guarantee a memory write. Use verified
hooks as the primary integration and standing instructions when hooks cannot
run, or keep both when a standing copy is useful for subagents.

### Verify the registered runtime

1. Inspect `codex mcp list` and `codex mcp get pseudolife-memory` (or the
   plugin's MCP configuration). Keep one registration. Identify the exact
   executable; a repo venv can differ from a global executable or plugin cache.
2. In that environment run `pseudolife-mcp doctor`. It reports interpreter,
   source path, installed package/SDK versions, health, instructions and tool
   annotations. It neither starts a daemon nor calls a bank tool. A healthy
   endpoint alone does not establish a working stdio handshake.
   An unreachable daemon report tells you to start it; a handshake timeout
   suggests checking MCP access and increasing `doctor --timeout` if needed.
   If the shim cannot fetch startup instructions within five seconds, it logs
   a sanitized stderr message and still initializes. Tool requests retain their own fresh upstream
   connections, but persistent authentication failures still need correction;
   reconnect after recovery to receive the startup guidance.
3. Run that interpreter with `-m pip check` and `-m pip show pseudolife-mcp mcp`.
   For a stale published installation, use that interpreter's
   `-m pip install --upgrade "pseudolife-mcp[lite]"`; for a source checkout,
   reinstall the intended checkout with `-m pip install -e .` to refresh
   dependencies and editable metadata. Docker shim-only hosts omit `[lite]`.
   Re-running the Docker installer preserves existing registrations; it does
   not repair a different interpreter already registered with Codex.
4. Reconnect and ask for a real memory search and lesson search. Confirm the
   tools are callable in the new task; `doctor` only proves protocol inventory.
   Store a truthful decision and verify it from another task when testing writes.

For a cold lite daemon, add `startup_timeout_sec = 240`,
`tool_timeout_sec = 180`, and `required = true` to the existing MCP server
table. This allows the shim's 180-second startup wait plus handshake margin;
the tool budget allows first-call model loading. Prewarm the daemon if the
initial model download takes longer. `required` waits for memory's initial
catalog and makes startup failure explicit. Codex otherwise has a 10-second
startup timeout and may assemble an optional catalog earlier. See
[official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Discovery and approvals

Prefer the needed catalog at connection time. An unconfigured daemon defaults
to `full`; deployments can override that with a principal-specific tier map.
For clients that retain their initial catalog, the operator can explicitly
choose `codex:full` in `PSEUDOLIFE_MCP_TIER_MAP` for the intended identity and
then reconnect. Do not change the shared default or another principal simply
to discover one tool. A bearer principal takes precedence over writer identity;
check which identity the registration actually uses.

A September 2026 Codex check expanded core to full: the server listed 35 tools
and sent `list_changed`, but the running turn retained its initial 22 callable
tools. That verifies a current-turn limit only. After expansion, check a fresh
task/reconnection's actual callable catalog; do not infer success from the
server inventory or notification. Client `enabled_tools`/`disabled_tools`
filters can narrow it further.

All tools carry approval hints. Searches are read operations over claims;
access telemetry can still update. Explicit retention reinforcement and tools
mixing status with mutation (toolset, dream, graph review) are marked writes.
Destructive hints also cover replacing document chunks during reingestion and
replacing canonical facts through `memory_store` when auto-promotion is enabled.
Hints inform a client's `default_tools_approval_mode = "writes"`; they are not
authorization and do not override per-tool approval settings or managed policy.
An allow-once prompt does not guarantee durable approval. Inspect any existing
`tools.<tool>.approval_mode` override if prompts differ from the server default;
the installer and doctor do not change approval policy.

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
note: reconnect after registering a server and verify a real tool call in
the new task rather than relying on a config write.

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
hook system that can inject session context, so the standing file supplies
the policy: the installer offers to append the block to `~/.gemini/GEMINI.md`
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
