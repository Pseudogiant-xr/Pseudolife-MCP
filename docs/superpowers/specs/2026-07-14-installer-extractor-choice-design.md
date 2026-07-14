# One-shot installer with extractor choice (issues #13 tier 2 + sidecar-optional)

**Date:** 2026-07-14 · **Status:** approved

## Problem

Install is a hand-run sequence of README steps; fresh installs miss steps
(#12: memory loop never fired). Separately, the E4B extractor sidecar is a
9.4 GB image that users on a Max plan (Sonnet-primary dream) may not want at
all — today it is mandatory (daemon `depends_on` + unconditional `up`).

## Decisions (user-approved)

- **Delivery:** full `ops/install.sh` + `ops/install.ps1` orchestrators (the
  #13 tier-2 consolidation), not a standalone configure script.
- **Extractor mode is always an explicit choice** — interactive prompt; no
  default. Non-interactive runs must pass `--extractor`, else the script
  stops with usage.
- **CLAUDE.md:** the installer prompts and, on consent, appends
  `examples/CLAUDE.memory.md` to `~/.claude/CLAUDE.md` (skip when the
  `pseudolife-memory` marker is already present). Flag: `--claude-md
  append|skip`.

## Extractor modes

| Mode | ops/.env managed block | Sidecar | Shim |
|------|------------------------|---------|------|
| `sidecar` | empty (stock defaults) | runs | — |
| `sonnet-fallback` | shim primary + sidecar fallback, `EXTRACTOR_MODE=auto` | runs (fallback) | autostart installed |
| `sonnet-only` | shim primary only, `EXTRACTOR_MODE=primary` | **never built/pulled** | autostart installed |

`sonnet-only` uses `EXTRACTOR_MODE=primary` deliberately: it states the
intent and keeps the `auto`-without-fallback startup warning silent. Dreams
pause (per-sweep retry) while the shim is down/logged out — accepted
trade-off, stated in output.

## Mechanisms

- **Managed `ops/.env` block** between marker comments; replaced wholesale on
  re-run, user lines outside the block untouched.
- **Sidecar disable** = `profiles: ["disabled"]` on the extractor service,
  written to the gitignored `ops/docker-compose.override.yml` (already
  auto-included by update scripts). A profiled service is skipped by `up`
  entirely — no build, no pull. The installer owns the file only when it
  starts with the installer's marker line; an existing user override (e.g. a
  GGUF mount) is never merged into — the exact snippet is printed instead.
  Switching to `sonnet-only` also removes the running extractor container
  (container only; it has no volumes). Choosing a sidecar mode again removes
  the marker-owned override.
- **Compose:** the daemon's `depends_on` on the extractor is dropped
  (extraction is runtime HTTP with per-sweep retry; ordering nicety only).
  `pseudolife-pg: service_healthy` stays. Worst case for stock installs: a
  dream sweep fires before the extractor finishes loading → probe fails,
  retried next sweep.

## Orchestration order

preflight (abort on fail) → volume create (names from existing `ops/.env` if
overridden) → extractor choice → managed env block + override handling →
`compose up -d --build` → shim autostart (Sonnet modes; Windows prints the
elevation caveat) → `install-hook` → CLAUDE.md consent append → `claude mcp
add` (skip if `claude mcp get` finds it) → health poll → per-mode verify
hints (`memory_dream(action="status")` expectations).

Idempotent throughout: every step is already-present-aware; re-running with a
different `--extractor` is the supported way to switch modes.

## Testing

Scripts only (no daemon logic): `bash -n` both, scratch-path runs of the
prompt-less paths, and a live re-run on the dogfood box with `--extractor
sonnet-fallback` (its current state) expecting a clean converge. Full pytest
suite before commit (compose changed). Deploy via `ops/update.ps1`.
