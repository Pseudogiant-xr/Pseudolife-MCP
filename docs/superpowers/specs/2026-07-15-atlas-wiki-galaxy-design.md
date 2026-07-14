# Cortex Console "Atlas" — wiki view + first-class 3D galaxy — design

**Status:** approved design (2026-07-15)
**Author:** agent + user (brainstorm, 2026-07-15)
**Builds on:** Atlas graph review (`2026-06-27-cortex-console-atlas-design.md`, shipped),
Cortex Console v2 (`2026-06-25-cortex-console-v2-design.md`, shipped).
**Inspiration:** the "second brain / LLM wiki" pattern (natural20.com) — human-browsable
entity pages — minus its weaknesses (LLM-authored prose, no supersession). Our pages are
**live-rendered from structured data**, never written by a model.

## Context

The Graph tab currently has three view modes (2D canvas `ForceGraph`, 3D galaxy via
vendored `3d-force-graph`, table) plus the Atlas review panel, split across an
Overview/Explore mode toggle. The galaxy is a thin default-config render: hover-only
tooltip labels, no search, no community structure, no navigation to the entity's data.
Entity data (facts, world facts, provenance, history) lives in other tabs; the only
click-through is a small node panel with two link-out buttons. There is no
human-readable "page" for an entity anywhere — the one dimension where a file-based
wiki beats the bank (2026-07-14 comparison analysis).

## Goals

- A **wiki page per entity** — article-style, human-readable, assembled live from data
  the bank already has. Zero LLM in the loop, zero staleness, no new write path.
- A **first-class 3D galaxy** as *the* map: community nebulae + constellation labels,
  proximity-faded node labels, search + fly-to, visual encodings of memory state,
  and a time scrubber replaying the bank's growth.
- **One integrated surface**: browsing the wiki is flying the galaxy. Clicking a star
  opens its page; wikilinks fly the camera.
- **Review woven in**: findings surface as pulsing stars and in-page action banners,
  plus the queue for batch work. Same endpoints, same confirm gates.

## Non-goals

- No LLM-authored page content (explicit decision; revisit as a separate layer later).
- No changes to retrieval, dream, graph mutation semantics, or the review findings API.
- No schema change — `entities.created_at` and `edges.asserted_at`/`superseded_at`
  already carry everything the timeline features need. No `SCHEMA_META_VERSION` bump.
- The Insight tab is untouched (out of scope).

## Decisions (from the brainstorm)

1. **Page content = live render only.** Deterministic assembly from facts + edges +
   provenance + world facts + mentions. (Rejected: dream-written summaries, full
   LLM pages — contradicts the drift critique that motivated this work.)
2. **Galaxy is the map.** The 2D canvas `ForceGraph` (455 lines) is retired. Table
   mode survives as the accessibility/no-WebGL/data fallback.
3. **Review = contextual + queue.** In-galaxy pulses, in-page banners, queue drawer.
4. **All four galaxy upgrades**: nebulae+labels, search+fly-to, state encodings,
   time scrubber.
5. **Layout A — galaxy-first split.** Full-bleed galaxy; right wiki panel (~42%,
   maximizable); left review drawer. Explore mode is subsumed by an **isolate
   toggle** (client-side 1–2-hop BFS dim, no second fetch).

## Design

### A. Surface & routing

- `#/graph` (and legacy `#/atlas`) render the new surface. Hash params:
  `?entity=X` opens X's wiki page and flies to it; `?scope=S` scopes the galaxy
  (existing project facet); `?view=table` for the fallback mode.
- Toolbar: project scope facet · search box · `galaxy | table` toggle · Review toggle.
- The Overview/Explore mode toggle, `mode=` and `depth=` params are retired.
  Old `mode=explore&entity=X` deep links degrade gracefully: `entity` still opens
  the page (extra params ignored).

### B. Wiki page — one endpoint, article render

**New read-only endpoint** `GET /api/wiki?entity=X` → service `wiki_page(entity)`,
assembling in one call (avoids 5 client round-trips):

```jsonc
{
  "found": true,
  "entity": "postgres",          // display name
  "canonical": "postgres",
  "etype": "database",
  "aliases": ["pg", "postgresql"],
  "projects": [{"source": "pseudolife-mcp", "count": 12, "origin": "derived"}],
  "community": 3,
  "first_seen": 1750000000.0,     // entities.created_at
  "facts": [                      // canonical cortex facts for this entity
    {"attribute": "...", "value": "...", "confidence": 0.9, "origin": "user",
     "stamp": ..., "history_available": true}
  ],
  "world_facts": [ {"attribute": "...", "value": "...", "source_url": "...", ...} ],
  "relations": {
    "out": [{"relation": "runs-on", "target": "docker-desktop", "derived": false,
             "confidence": 0.9, "asserted_at": ...}],
    "in":  [{"relation": "stores-data-in", "source": "daemon", ...}]
  },
  "mentions": [                   // provenance entries (via entity_provenance)
    {"entry_id": 42, "snippet": "...", "source": "pseudolife-mcp", "ts": ...}
  ],
  "timeline": [                   // merged chronology, newest first, capped
    {"ts": ..., "kind": "entity-created" | "edge-asserted" | "fact-stamped", "text": "..."}
  ],
  "flags": [                      // review findings touching this entity
    {"finding": "duplicate", "other": "Postgres DB", ...}
  ]
}
```

- Fact **history** stays on the existing `GET /api/facts/history` endpoint, fetched
  lazily when the user expands a fact's supersession chain.
- Unknown entity → `{"found": false}` (panel shows a create-nothing empty state —
  wiki pages are never minted from the UI).
- `flags` must not re-run the full review scan per page load: `wiki_page()` computes
  only the cheap per-entity subset (open proposals naming the entity, dubious edges
  incident to it, its unattributed/orphan status) directly from the tables the scan
  reads. The full-scan queue remains the drawer's job.

**Client render** (`js/views/wiki_page.js`): article layout — title + etype badge +
alias line + project chips + first-seen date; sections **Facts** (with expandable
history), **World** (citation links, existing world_url sanitization rules),
**Relations** (grouped by relation; each endpoint a wikilink), **Mentions**
(snippets linking to Stream/entry), **Timeline** (compact), and a **flag banner**
up top when `flags` is non-empty, carrying the same actions as the review queue.
Every entity name anywhere in the page is a wikilink: click → panel swaps to that
page, camera flies to its star, hash updates (panel back/forward via history).

### C. Galaxy renderer (`js/galaxy.js`)

**Vendored bundle rebuild.** Current bundle exports only the default constructor;
nebulae/labels/fly-to need `THREE`. Rebuild ONE self-contained ES module (esbuild)
exporting `{ ForceGraph3D (default), THREE }` from the same dependency graph —
single three.js instance, no CDN at runtime, documented in `vendor/README.md`
per the existing offline rules (same version pin discipline, `WebGLRenderer`
presence check).

**Data.** `GET /api/graph` (whole-graph overview payload) gains per-node
`created_at`, per-edge `asserted_at` (additive fields; existing tests untouched,
new ones pin them). Node scale cap + truncation banner carry over unchanged.

**Features.**
- *Encodings:* star size = degree + fact count; color = project (all-scopes) or
  community (single scope) via the existing `projectColor`/`communityColor`;
  brightness/emissive = recency of the entity's latest activity (max of
  `created_at`, newest incident `asserted_at`); review-flagged stars pulse
  (sinusoidal emissive+scale).
- *Nebulae:* one soft radial-gradient sprite per community, positioned at the
  member centroid, scaled to member spread, recomputed when the engine cools
  (not per frame); constellation label = top-degree member's display name.
  Cap: largest ~12 communities get nebulae/labels.
- *Node labels:* canvas-texture sprites, shown for the nearest ~40 nodes by
  camera distance (throttled recompute), plus hovered/selected always.
- *Search + fly-to:* toolbar box; type → matching stars highlight (emissive) and
  non-matches dim; Enter/click a result → `cameraPosition()` tween to the node +
  open its wiki page. Wikilink navigation uses the same fly-to.
- *Time scrubber:* bottom slider over `[min created_at, max(created_at, asserted_at)]`.
  Layout is computed ONCE on the full graph; the scrubber only toggles star/edge
  visibility (`created_at`/`asserted_at` ≤ t) — stable positions, no re-simulation.
  Play button animates t. Edges whose endpoints are hidden are hidden.
- *Isolate toggle* (on the wiki panel): client-side BFS from the open entity;
  nodes beyond depth 1–2 fade to ~10% opacity, edges likewise; camera unchanged.
- *Motion:* `prefers-reduced-motion` → no pulse, no play-animation, cooldown 0
  (existing pattern). Auto-fit camera logic (`zoomToFit` on settle) carries over.
- *Lifecycle:* keep the current self-destruct guard (interval isConnected check),
  ResizeObserver, `_destructor()` on route leave; the ONE fg3d instance rule stays.

### D. Review integration

- Findings API (`/api/graph/review`) and all mutation endpoints unchanged.
- Queue drawer (left): the existing findings list restyled to the drawer, same
  `actOnFinding` action descriptors, confirm gates and backup-first semantics
  preserved verbatim.
- Galaxy: entities named in current findings pulse; clicking one opens its page,
  whose flag banner offers the same actions.
- After any mutation: refetch review + graph + open page (same refresh flow as today).

### E. Retirements & compatibility

- Deleted: 2D canvas `ForceGraph` class, `zoomControls`, 2D-specific legend paths,
  Overview/Explore mode wiring, the old node-panel (`showNode`).
- Kept: `tableView` (fallback mode), color functions, fullscreen button,
  `atlas_review.js` action wiring (restyled), all API mutation code.
- `views/graph.js` is rewritten as the surface orchestrator; new modules:
  `galaxy.js` (renderer), `views/wiki_page.js` (article render).
- Bundle-load failure or no WebGL → automatic table mode + error notice (never a
  dead tab). Old `#/atlas` and `#/graph?entity=` deep links keep working.

### F. Error handling & performance

- Wiki fetch failure → in-panel `errorBlock`; galaxy unaffected.
- Scale: bank is O(hundreds) of entities; existing overview cap + banner retained.
  Label sprites capped (~40 + hover/selected), nebulae capped (~12), centroid
  recompute only on engine-cool. No per-frame allocation in the render loop.
- Theme: galaxy background, label/nebula colors read the `data-theme` custom
  properties on both themes (2D themeColors pattern ported).

### G. Testing & QA

- **Pytest (TDD, watched RED):** `tests/test_web_wiki.py` — `wiki_page()` assembly
  (identity/aliases/projects, facts, world, relations in+out, mentions, timeline
  merge order, flags subset, `found:false`); graph payload timestamp fields;
  route registration + auth parity with other GET endpoints.
- **Frontend QA (Browser pane against `devserver.py` + `fixtures.py`):** galaxy
  loads offline; click → fly-to + page; wikilink hop; search; scrubber replay;
  isolate; review pulse → banner action → refresh; table fallback (bundle load
  failure path); both themes; reduced-motion; fullscreen; leak check on repeated
  route switches (the guard interval fires).
- **Review pass:** this is a perf/renderer change in the mandated category —
  independent `/code-review` medium (or reviewer subagent) before commit.
- **Ship:** CHANGELOG under `[Unreleased]`; no schema bump; deploy via
  `ops/update.ps1`; post-deploy live check = load `#/graph` against the live
  daemon and exercise `/api/wiki` for a known entity.

## Implementation stages (independently shippable)

1. **Stage 1 — wiki spine:** `/api/wiki` + wiki panel on the existing surface
   (galaxy untouched). Ships the biggest UX win first.
2. **Stage 2 — galaxy rebuild:** vendored bundle rebuild, `galaxy.js` with
   encodings, labels, nebulae, search/fly-to; 2D map + explore mode retired;
   layout A shell (panel + drawer).
3. **Stage 3 — time scrubber + review contextualization + polish** (pulses,
   banners, isolate, reduced-motion, fallbacks, QA sweep).
