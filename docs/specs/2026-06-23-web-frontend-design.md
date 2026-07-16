# Pseudolife Cortex Console — web frontend design

**Status:** shipped v1 (merged to master). Superseded by `docs/superpowers/specs/2026-06-25-cortex-console-v2-design.md`.
**Branch:** merged (was `feat/web-ui`)
**Author:** agent (overnight `/loop` build)

## Problem

Pseudolife-MCP is a powerful multi-layer memory engine, but the only way to
see or steer it is through MCP tool calls issued by an LLM. A human operator
has no way to:

1. **Configure the knobs and dials** (the `config.py` dataclasses) without
   hand-editing a YAML file and restarting.
2. **See memory + health stats** at a glance (band fill, hit rates, fact
   counts, dream backlog, persistence errors).
3. **Visualise the memory** — the knowledge graph and the band continuum.
4. **Review the memories** the cortex holds — canonical facts, world facts,
   lessons, episodes, the associative stream — arranged so they are easy to
   follow.

The README already lists "human-facing outcome-coloured graph visualisation"
as a deferred follow-on, so this is a roadmapped feature, not scope creep.

## Goals

- A **read-mostly operator console** that is beautiful, fast, and intuitive.
- **Zero new runtime infrastructure**: served by the existing daemon, no Node
  service, no second process, fully offline (matches the baked-image ethos).
- **No new business logic**: every endpoint maps 1:1 to an existing
  `service.*` method. The UI is a view over the daemon, not a fork of it.
- **Safe by construction**: read endpoints are free; writes (config, fact
  resolve/forget, delete, supersede, dream) are explicit, validated,
  confirm-gated in the UI, and honour the same bearer-token auth as `/mcp`.

## Non-goals

- Not a chat UI. Claude is the LLM; this is an *instrument panel* for the
  memory it uses.
- Not a replacement for the MCP surface. The daemon keeps serving `/mcp`
  unchanged; the console is additive.
- No multi-user accounts / RBAC. Single operator, loopback-first, optional
  bearer token (same model as the daemon today).

## Research (what's out there, June 2026)

- **doobidoo/mcp-memory-service Dashboard** — the closest comparable. Ships
  8 tabs (Dashboard · Search · Browse · Documents · Manage · Analytics ·
  Quality · API Docs) and explicitly notes direct-HTTP access beats MCP
  overhead (50–150 ms vs 200–500 ms). → validates the thin-REST-over-the-daemon
  approach and the tabbed IA.
- **Mem0 / Zep / Letta / Cognee (2026 comparisons)** — Zep's headline
  differentiator is a *temporal* knowledge graph that tracks how facts change
  over time (fact-validity windows). Pseudolife **already has this**
  (`memory_history`, HLC ordering, `valid_time`/`tx_time`, supersession) — so
  a fact-evolution timeline is essentially free and is a standout feature.
- **KG-visualisation UX research** (arXiv 2304.01311; FalkorDB) — users want
  **both** a force-directed graph *and* a plain table/list view; "Wikipedia-style"
  click-to-navigate exploration wins for open-ended browsing; reduce clutter,
  support filter/zoom/expand-on-demand.

### Differentiators we can uniquely surface (existing strengths)

1. **Temporal fact history** — per-slot version timeline (who/when/why), the
   thing Zep charges for.
2. **Provenance + contention** — origin tiers `user > action > agent`, contested
   facts with a human **resolve** action. No competitor surfaces this.
3. **Ranking-trace debugger** — `memory_trace` rendered visually: why did/didn't
   an entry surface (per-tier candidates, recency/source/supersession
   multipliers, rerank/BM25 fusion). A retrieval-debugging superpower.
4. **Dream / consolidation curation** — show the backlog, run a dream, review
   consolidation clusters, merge them — human-in-the-loop memory hygiene.
5. **MIRAS band continuum** — the 8-tier working→forever continuum as a living
   visual (capacity fill, hit rate, promotion pressure).

## Aesthetic direction

**"Cortex Console" — a precision observatory instrument for a synthetic mind.**

- **Tone:** refined-utilitarian / deep-observatory. Dark, calm, luminous.
  Restraint and precision over maximalist chaos — this is an instrument, not a
  toy. Faint engineering grid + subtle grain for atmosphere; glow only on
  focus/active states.
- **Spectral accent per memory layer** (the one memorable idea — each layer is
  a spectral line):
  - Cortex (canonical facts) → **amber/gold** (established truth)
  - Associative continuum → **cyan** (fluid recall)
  - World cortex → **green** (external, cited)
  - Lessons (procedural) → **violet** (learned behaviour)
  - Graph / entities → **azure** (structure)
  - Episodes → **slate** (sessions)
- **Typography:** vendored OFL fonts with strong system fallbacks (offline):
  a characterful display serif (Fraunces) for headings, a humanist sans for
  body, JetBrains Mono for values/data/ids. Never Inter/Roboto/system-default
  as the primary face.
- **Motion:** one orchestrated load with staggered reveals; calm transitions;
  the graph simulation is the kinetic centrepiece. No gratuitous micro-bounce.
- **Light theme** offered as a toggle (persisted), but dark is the default and
  the design's home.

## Architecture

```
browser (SPA, vanilla ES modules, no build)
   │  fetch JSON
   ▼
daemon ASGI app  (pseudolife_memory/daemon.py)
   ├── /health            (open — unchanged)
   ├── /mcp               (token-gated — unchanged)
   ├── /                  → redirect to /ui/
   ├── /ui/*              (static SPA shell — open; it's just code)
   └── /api/*             (token-gated like /mcp; JSON over service.*)
```

- **New module** `pseudolife_memory/web/`:
  - `api.py` — builds a pure-ASGI router for `/api/*` + a static file server
    for `/ui/*`, composed *under* the existing `AuthHealthASGI` gate. No
    Starlette dependency beyond what the MCP SDK already pulls.
  - `routes.py` — the endpoint table; each handler calls one `service.*` method
    and returns its dict. Read handlers GET; mutations POST and are explicitly
    enumerated (never a generic passthrough).
  - `config_io.py` — read the effective config as JSON; validate+write a knob
    patch to `<data_dir>/config.yaml` atomically (utils/atomic_io) with a
    timestamped backup, and live-mutate `service.config` for knobs whose read
    path is live; flag restart-required knobs.
  - `static/` — `index.html`, `app.js` + ES modules, `styles.css`, vendored
    fonts. Served verbatim.
  - `devserver.py` — a **fixture-backed** dev server (canned JSON, no
    MemoryService, no Postgres) so the frontend can be QA'd in a browser
    without touching the live bank. This is the visual-iteration harness.
- **Auth:** extend `AuthHealthASGI` so `/`, `/ui/*`, `/health` are open and
  `/api/*` joins `/mcp` behind the bearer gate. The static shell is just code;
  data is gated. The UI collects the token (localStorage) and sends
  `Authorization: Bearer …` on `/api` calls; a 401 shows a token prompt.

### Why served-from-daemon + vanilla, no-build

- The daemon already runs uvicorn/ASGI; adding routes is a few hundred lines.
- A Node/Vite build stage would bloat the offline Docker image and add a JS
  toolchain to a Python repo. Vanilla ES modules + hand-rolled SVG charts +
  a small canvas force-graph keep the image a `COPY` away and fully offline.
- Karpathy discipline: minimal new code, no speculative abstraction.

## API surface (read = GET, mutation = POST)

| Method & path | service call | Notes |
|---|---|---|
| `GET /api/health` | daemon `_health()` | schema, storage, auth, persist_errors |
| `GET /api/stats` | `stats()` | per-band sizes/caps/hit-rates |
| `GET /api/overview` | composed | counts: facts, world, lessons, episodes, sources, tags, dream backlog |
| `GET /api/facts?limit=` | `cortex_dump()` | grouped by entity in the UI |
| `GET /api/facts/history?entity=&attribute=` | `history()` | version timeline |
| `GET /api/facts/contenders?entity=&attribute=` | `cortex_contenders()` | contested |
| `POST /api/facts/resolve` | `cortex_resolve()` | {entity,attribute,accept} |
| `POST /api/facts/set` | `cortex_write()` | deliberate assert/correct |
| `POST /api/facts/forget` | `cortex_forget()` | confirm-gated |
| `GET /api/world?limit=` | `world_dump()` | citations + freshness + stale |
| `GET /api/lessons?limit=` | `lessons_dump()` | polarity/outcome |
| `GET /api/episodes?limit=` | `episode_list()` | timeline |
| `GET /api/episodes/summary?id=` | `episode_summary()` | tag/source dist |
| `GET /api/recent?n=&source=` | `recent()` | associative stream |
| `GET /api/search?q=&top_k=&rerank=&bm25=&min_score=` | `search()` | live search |
| `GET /api/trace?q=&…` | `trace()` | ranking-trace debugger |
| `GET /api/recall?q=&hops=` | `recall()` | multi-hop |
| `GET /api/sources` / `GET /api/tags` | `list_sources()`/`list_tags()` | facets |
| `GET /api/graph?entity=&depth=&to=` | `graph_neighborhood()` | viz data |
| `GET /api/dream/status` | `dream_status()` | backlog/would_fire |
| `POST /api/dream/run` | `dream_run()` | confirm-gated, may be slow |
| `GET /api/consolidation?q=&episode=` | `consolidation_candidates()` | clusters |
| `POST /api/consolidate` | `consolidate()` | confirm-gated |
| `POST /api/delete` | `delete()` | confirm-gated, requires a filter |
| `POST /api/supersede` | `supersede()` | correction |
| `GET /api/config` | `config_io.read` | effective knobs + metadata |
| `POST /api/config` | `config_io.write` | validated patch → yaml + live |

All mutation endpoints are individually enumerated handlers (no generic tool
proxy) so the console can never trigger something the design didn't intend.

## UI information architecture (tabs)

1. **Observatory (Dashboard)** — health banner (schema/storage/auth/persist
   errors), overview counts per layer (spectral cards), MIRAS band continuum
   (capacity fill + hit rate), dream backlog gauge with a "would fire" state.
2. **Cortex** — canonical facts grouped by entity ("Wikipedia-style"), origin
   tier + confidence + age badges, contested flag with a resolve action, click
   a fact → history timeline drawer. Search/filter by entity/attribute/origin.
3. **World** — cited external facts: value + source quote + link + freshness
   class + decayed confidence + stale flag.
4. **Lessons** — procedural memory grouped by task-type, +/− polarity, outcome,
   the tool/source it concerns.
5. **Stream** — the associative continuum: searchable (rerank/bm25/min_score
   toggles), per-entry band/source/tags/score, superseded indicator, and a
   "trace" affordance opening the ranking-trace debugger.
6. **Graph** — force-directed neighbourhood from a seed entity; derived/inverse
   edges styled distinctly; node click shows facts + expands a hop; **table
   view toggle** (per UX research). Path-between-two-entities mode.
7. **Episodes** — session timeline with per-episode summaries.
8. **Console (Config)** — grouped knob editor (Retrieval / Cortex / Dream /
   Lessons / Recall / Time), each knob with description (from docstrings),
   type-aware control, default, current value, and a restart-required badge.
   Diff-preview before save; writes to config.yaml + live where safe.

## Risks & mitigations

- **Can't run a 2nd MemoryService against live PG** (LockNotAvailable on schema
  DDL). → All visual QA uses `devserver.py` fixtures; live verification is the
  existing daemon-rebuild deploy path, done only with the user present.
- **Config live-mutation safety** — only scalar knobs whose read path is live
  are mutated in-process; structural knobs (band preset, embedder, storage
  write_mode) are write-to-yaml + restart-required, clearly badged. Atomic
  write + timestamped backup; type/range validation server-side.
- **Offline fonts** — vendored woff2 with full system fallback stacks so a
  missing/again-undownloaded font never breaks layout.
- **Graph size** — neighbourhood queries are depth-capped (≤3) server-side; the
  canvas sim handles the resulting node counts (tens, not thousands).

## Verification

- Fixture devserver + a browser driver (Playwright/Chrome-DevTools MCP):
  screenshot every tab at desktop + narrow widths; assert no console errors;
  exercise search, graph expand, config diff-preview, fact-history drawer.
- Backend: unit tests for `config_io` (round-trip, validation, atomicity) and
  the route table (each path dispatches to the right service method; auth gate;
  405 on wrong verb). Pure-logic tests run without Postgres, matching the
  repo's existing skip-when-no-PG convention.
