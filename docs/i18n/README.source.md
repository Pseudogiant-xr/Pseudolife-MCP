<!-- i18n-source: v4 (2026-07-19) — canonical English text for the translated
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

- **Associative memory that ages like memory should** — a recency continuum
  of memory bands ranked by similarity, with contradiction detection and
  supersession: corrections replace old answers instead of piling up
  beside them.
- **Canonical facts, not vibes** — one *current* value per
  `entity.attribute` slot; corrections supersede rather than silently
  overwrite, and the full version history survives.
- **Dreams** — while you're away, an extractor consolidates the memory
  stream into canonical facts and a knowledge graph.
- **Lessons from its own work** — successes, dead-ends, and your
  corrections become do/avoid guidance surfaced at the start of every
  session.
- **A web console to watch it think** — the Cortex Console: memory stream,
  fact history, knowledge-graph atlas, session episodes, and document RAG.

## Quickstart

Requires Docker and Claude Code, Codex, or both. One command from clone to
first memory (Claude is the default client):

```bash
git clone https://github.com/Pseudogiant-xr/Pseudolife-MCP.git
cd Pseudolife-MCP
ops/install.sh          # Linux / macOS
ops\install.ps1         # Windows (pwsh 7+)
# Codex: add --client codex / -Client codex
# Both:  add --client both  / -Client both
```

The installer checks prerequisites (printing one exact fix line for anything
missing) and asks which dream extractor to use — Claude Sonnet via your Max
plan (the lightest install), Sonnet with the bundled local model as
automatic fallback, or the bundled local model alone, which needs no plan
at all. It then brings the stack up, wires the selected clients (the
session-start briefing hook and the MCP transport registration), offers to
append the standing memory-loop instruction to `~/.claude/CLAUDE.md` or
`~/.codex/AGENTS.md`, and health-checks the daemon. It is idempotent:
re-run it any time; `--extractor <mode>` switches extractor setups.

With the daemon running, the Claude Code **plugin** adds the session-start
memory briefing, the standing memory-loop guidance, and the `/dream` +
`/memory-status` commands — the MCP server itself is registered by the
installer, so the plugin never doubles its tools:

```
/plugin marketplace add Pseudogiant-xr/Pseudolife-MCP
/plugin install pseudolife-memory@pseudolife-mcp
```

Codex registers the server directly:

```bash
codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
```

Then, in either coding agent: *"remember that my staging box is
haze-02"* — and in a fresh session days later, *"which box is staging?"*
gets the answer back from memory. Browse everything in the Cortex Console
at `http://127.0.0.1:8765/ui/`.

## How it works

The agent stores one claim at a time as it works (`memory_store`,
`memory_fact_set`); a novelty-gated store drops near-duplicates. Between
sessions, the **dream** distils the stream into canonical facts, graph
relations, and procedural lessons. At every session start, a briefing
injects what the memory is unsure about, lessons from past work, and where
you left off. Retrieval blends semantic search over the memory bands with
the canonical fact store, so corrected answers win over stale ones.

## Documentation (English)

The canonical, always-current documentation is in English:

- [README](../../README.md) — full install, wiring, tools, troubleshooting
- [Configuration](../guide/configuration.md) · [Retrieval](../guide/retrieval.md)
  · [Dreaming](../guide/dreaming.md) · [Episodes](../guide/episodes.md)
  · [Memory model](../guide/memory-model.md) · [Benchmarks](../guide/benchmarks.md)

This page is a translated introduction, synced to the English README at the
version noted below; where they disagree, the English documentation is
authoritative.
