<!-- i18n-source: v10 (2026-09-04) — canonical English text for the translated
     front doors in this directory. Translators: keep every fenced code block
     byte-identical (commands are never translated); keep "Pseudolife-MCP",
     "Claude Code", "Codex", "MCP", "Cortex Console", and tool names like
     memory_search in English; translate everything else in a natural
     technical register. Each translation must carry the matching
     "i18n-sync" marker (guard: tests/test_i18n_readme.py). Volatile claims
     (versions, sizes, counts, defaults) deliberately live only in the
     English docs this file links to. -->

# Pseudolife-MCP

**Persistent long-term memory for Claude Code, Codex, and other MCP clients.**

An MCP server that gives coding agents a long-term memory that persists
across sessions — surviving context compactions and fresh tasks. Your
coding agent is the intelligence; this server is its memory on disk.

What you get:

- **Associative memory with honest forgetting** — a flat similarity store
  with hybrid dense-plus-lexical retrieval, contradiction detection and
  supersession: corrections replace old answers instead of piling up
  beside them.
- **Canonical facts, not vibes** — one *current* value per
  `entity.attribute` slot (or a member set, for slots that hold many
  concurrent values); corrections supersede rather than silently
  overwrite, and the full version history survives.
- **Dreams** — while you're away, an extractor consolidates the memory
  stream into canonical facts and a knowledge graph.
- **Lessons from its own work** — successes, dead-ends, and your
  corrections become do/avoid guidance surfaced at the start of every
  session.
- **A web console to watch it think** — the Cortex Console: memory stream,
  fact history, knowledge-graph atlas, session episodes, and document RAG.

## Quickstart

Two commands. No Docker, no database to set up, no container runtime:

```bash
pip install "pseudolife-mcp[lite]"
claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
```

Codex instead of Claude Code — same shape:

```bash
pip install "pseudolife-mcp[lite]"
codex mcp add pseudolife-memory -- pseudolife-mcp
```

Then, in either coding agent: *"remember that my staging box is
haze-02"* — and in a fresh session days later, *"which box is staging?"*
gets the answer back from memory. Browse everything in the Cortex Console
at `http://127.0.0.1:8765/ui/`.

The first session auto-starts the daemon, which provisions an embedded
PostgreSQL and downloads the embedding model — a one-time step. Lite ships
no dream **extractor**, so canonical facts don't appear on their own: on
this path `memory_fact_set` is the only **cortex** writer, until an
OpenAI-compatible endpoint is configured.

### Durable tier — Docker

For a long-lived bank: everything above, plus the bundled extractor,
external volumes, health-checked services, and backup/rollback tooling.
Requires Docker and at least one MCP-capable coding agent — Claude Code,
Codex, and Gemini CLI are wired end-to-end; anything else gets paste-ready
config. One command from clone to first memory:

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
# Gemini: add --client gemini — or several: --client claude,codex,gemini
# Other MCP agents (Cursor, Windsurf, Zed, ...): --client generic
```

The installer checks prerequisites (printing one exact fix line for anything
missing) and asks which dream extractor to use — a Claude model via your
Max plan (the lightest install), the Claude shim with the bundled local
model as automatic fallback, the same two shapes with a GPT-5.6 model on a
ChatGPT plan (via the Codex CLI), or the bundled local model alone, which
needs no plan at all. It then brings the stack up, wires the selected clients (the
session-start briefing hook, which delivers the memory-loop guidance every
session, and the MCP transport registration), and health-checks the
daemon. It is idempotent: re-run it any time; `--extractor <mode>`
switches extractor setups.

With the daemon running, the Claude Code **plugin** adds the session-start
memory briefing, the standing memory-loop guidance, and the `/dream` +
`/memory-status` commands — the MCP server itself is registered by the
installer, so the plugin never doubles its tools:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex — the installer's default (shim mode) wires the same stdio shim it
uses for Claude, keeping `PSEUDOLIFE_MCP_NO_SPAWN=1` set on the Docker
tier so a Codex session gets its own identity instead of inheriting a
concurrent Claude session's episode. Exact commands, the direct-HTTP
alternative, and non-default ports/tokens:
[README — Wire into your coding agent](../../README.md#wire-into-your-coding-agent).

## How it works

The agent stores one claim at a time as it works (`memory_store`,
`memory_fact_set`). Between
sessions, the **dream** distils the stream into canonical facts, graph
relations, and procedural lessons. At every session start, a briefing
injects what the memory is unsure about, lessons from past work, and where
you left off. Retrieval blends semantic search over the associative store
with the canonical fact store, so corrected answers win over stale ones.

## Documentation (English)

The canonical, always-current documentation is in English:

- [README](../../README.md) — full install, wiring, tools, troubleshooting
- [Configuration](../guide/configuration.md) · [Retrieval](../guide/retrieval.md)
  · [Dreaming](../guide/dreaming.md) · [Episodes](../guide/episodes.md)
  · [Memory model](../guide/memory-model.md) · [Benchmarks](../guide/benchmarks.md)

This page is a translated introduction, synced to the English README at the
version noted below; where they disagree, the English documentation is
authoritative.
