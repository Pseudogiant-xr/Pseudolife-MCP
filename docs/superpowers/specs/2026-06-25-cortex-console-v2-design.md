# Cortex Console v2 — design

**Status:** approved design (2026-06-25)
**Supersedes the aesthetic/scope of:** `docs/specs/2026-06-23-web-frontend-design.md` (v1, now shipped on `master`)
**Author:** agent + user (brainstorm, 2026-06-25)

## Context

The Cortex Console (v1) is a daemon-served, no-build, vanilla-ESM operator
console for PseudoLife-MCP. It shipped on `master` with eight tabs and a
"precision observatory instrument" aesthetic. A full review (architecture,
docs, code, comparable tools, and a live visual pass) found that the UI is
high-craft, but three things are now true:

1. **A latent bug silently disables the headline design idea.** The `el()`
   hyperscript helper sets inline styles with `Object.assign(node.style, …)`,
   which silently drops CSS custom properties. So `--tone` (stat cards),
   `--dot` (nav/panel/drawer accents), and `--bh` (MIRAS continuum) are never
   applied — every element falls back to the single route `--accent`. The
   "spectral accent per memory layer" language does not render; the continuum
   band-fills show no colour (invisible in the light theme). Confirmed live:
   `getComputedStyle(...).getPropertyValue('--tone'|'--dot'|'--bh')` is empty
   everywhere.

2. **The backend has outgrown the UI.** Capabilities added after v1 are not
   surfaced: the graph-insight digest (god-nodes, suggested questions,
   surprises), communities, `graph_path`, multi-hop `recall`, consolidation
   review, and MTT retention / engram traces (`reinforce`, `source_entries`).
   Several write actions are wired in `routes.py` with no UI.

3. **The user wants a richer visual identity.** v1 deliberately stayed
   restrained ("an instrument, not a toy"). v2 pushes toward expressive,
   product-grade visualisation while keeping the offline/no-build ethos.

## Decisions (from the brainstorm)

- **Scope:** comprehensive v2 — fix + polish, close the backend gap, and a
  design-language elevation.
- **Visual philosophy:** push toward rich viz (distribution charts, an
  animated 3D graph galaxy, dashboard-style panels), staying data-first.
- **Viz approach:** *hybrid* — hand-roll the cheap things (charts, sparklines,
  denser panels) with zero dependencies; vendor exactly **one** offline library
  for the genuinely hard 3D graph galaxy.
- **Information architecture:** grouped navigation (Approach C) with two new
  surfaces.
- **Architecture is unchanged:** extend the existing daemon-served vanilla-ESM
  SPA + thin, enumerated REST over `service.*`. No rewrite, no framework, no
  CDN, fully offline. Every endpoint maps 1:1 to a `service.*` method;
  mutations are individually enumerated (no generic tool proxy); `/api` stays
  token-gated.

## Information architecture

The flat `ROUTES` list in `app.js` gains a `group` field; `buildNav()` renders
group labels. Hash routing and number-key shortcuts are unchanged (keyed by
route id).

```
OVERVIEW     · Observatory
MEMORY       · Cortex · World · Lessons · Stream · Recall   ◀ new
STRUCTURE    · Graph · Insight                              ◀ new
OPERATIONS   · Episodes · Console
```

Consolidation review and engram traces are **drawers**, not tabs. Community
colouring lives **inside** the Graph view.

## Phase 0 — Restore & polish

Pure correctness/polish: no new endpoints, no new data. Safest to land and
verify first; it makes everything after it look right.

1. **`el()` custom-property fix** (`static/js/util.js`): when a `style` key
   starts with `--`, use `node.style.setProperty(k, v)` instead of
   `Object.assign`. One change restores `--tone`, `--dot`, and `--bh`
   everywhere — the whole spectral language comes alive.
2. **Continuum legibility:** verify band-fill contrast in both themes after the
   fix; give each band a distinct, accessible hue and a small min-visible floor
   so a near-empty band still reads.
3. **Graph legend + hint:** complete the legend to explain all etype colours
   (service / database / host / model / person / concept) and explicit-vs-
   derived edges; reposition the hint (top-centre, auto-fade) so it no longer
   collides with bottom node labels.
4. **Schema single-source:** `devserver._health` imports and reports
   `SCHEMA_META_VERSION`, so the topbar and System panel never disagree (today
   the devserver hardcodes 11 while `routes._health` reports 13).
5. **Docs:** this spec supersedes the v1 design doc's aesthetic/scope; fix the
   v1 doc's stale `Status` / `Branch` header.

## Phase 1 — Close the backend gap

New endpoints stay true to the contract: enumerated handlers, 1:1 over
`service.*`, GET = read / POST = mutate, token-gated.

### New REST endpoints (`routes.py`)

| Endpoint | Service call | Surfaces |
|---|---|---|
| `GET /api/graph/digest` | `graph_digest()` → `{available, digest}` | god-nodes, suggested questions, surprises, community count |
| `GET /api/graph/communities[?id=]` | `communities(id?)` | community list / members |
| `GET /api/graph/path?source=&target=&max_hops=` | `graph_path()` | shortest path A→C |
| `GET /api/entry?id=` | `get_entry(entry_id)` (backs `memory_get`) | dense episode + `consolidated_into` + `source_entries` (engram trace); reading gently reinforces |
| `POST /api/reinforce` | `reinforce(entry_id)` | manual reinforce |

Already wired in `routes.py` — Phase 1 only adds UI: `recall`,
`consolidation_candidates`, `consolidate`, `facts/set`, `facts/forget`,
`delete`, `supersede`.

### New & updated UI surfaces

- **Insight tab** (new, *Structure*) — renders `graph_digest`: a hero panel of
  **god-nodes** (top entities by degree), the dream's **suggested questions**,
  **surprises** (cross-community bridges the agent inferred), and a **community
  list** with sizes. A god-node jumps to Graph seeded there; a community filters
  Graph to it. Graceful empty state when no dream has run
  (`available: False`).
- **Recall surface** (new, *Memory*) — query → multi-hop result rendered as the
  resolved **seeds** and the **bridging chains** (`A —rel→ B —rel→ C`), the
  surfaced entities + facts, and a `low_confidence` banner when no seed
  resolves. Includes a **path-between-two-entities** mode (`graph_path`).
- **Consolidation review** (drawer, from Observatory's dream area) — list
  `consolidation_candidates` clusters by cohesion, preview the merged text,
  `consolidate` (confirm-gated).
- **Engram trace drawer** (on a Stream entry) — `memory_get` detail:
  reinforcement count, `source_entries` (what consolidated into / from this),
  and a **Reinforce** button.
- **Write actions, surfaced safely** — Cortex entity cards get *assert/correct*
  (`facts/set`) + *forget*; Stream entries get *supersede* / *delete*. All
  destructive actions are **confirm-gated and danger-styled**, preserving the
  "read-mostly, safe by construction" posture. `delete` already requires a
  filter server-side.

Each new surface is a self-contained view/drawer module reading one (or one
composed) endpoint — same pattern as the existing views, testable in isolation.

## Phase 2 — Rich-viz elevation

### 1. 3D graph galaxy (the one vendored library)

Vendor **`3d-force-graph` + three.js** offline into `static/vendor/` (pinned
version; license + provenance recorded; **no CDN**), **lazy-loaded** via dynamic
`import()` only when the galaxy view opens, so the base bundle stays light. The
Graph tab becomes a three-way view — **Galaxy / Graph (current 2D canvas) /
Table**:

- **Galaxy:** nodes coloured by **community** (`communities()` assignment),
  sized by degree, seed highlighted; explicit vs. derived edges styled
  distinctly; click → re-center / facts / "path to…".
- The 2D canvas and table views are kept; the table is also the accessibility
  fallback (UX research backs keeping a non-graph view).
- **Fullscreen:** a maximize control in the graph toolbar (beside zoom/fit)
  calls the Fullscreen API on the graph container, with a **CSS-maximize
  fallback** when the Fullscreen API is blocked. The 2D sim's `ResizeObserver`
  and the galaxy lib's own resize reflow the canvas automatically; **ESC**
  exits. Applies to Galaxy and 2D Graph.
- **Reduced motion:** no auto-rotation; honour `prefers-reduced-motion` (static
  framing, opt-in spin).

### 2. Hand-rolled charts (zero new dependencies)

A small reusable SVG chart primitive (`bar` / `donut` / `sparkline`) in a new
`static/js/charts.js`, used for:

- **Observatory:** facts-by-origin donut (user/action/agent), entries-by-source
  bar, entries-by-band distribution; a hit-rate sparkline per continuum band.
- **Insight:** community-size and degree distributions; god-nodes as ranked
  degree-bars; surprises as cards; suggested questions as actionable chips.

### 3. Dashboard IA per research (golden-signals / F-pattern)

Reorder Observatory so the **critical signals sit top-left** —
health / `persist_errors`, dream-ready, contested + stale counts — then counts,
then the continuum. Keep panels-per-view restrained.

### 4. Accessibility audit

Keyboard-operable everything (the galaxy falls back to the table view for
keyboard/screen-reader users), visible focus rings, `aria-label`s on all new
controls, and a contrast pass (especially the warm light theme). Verified with
the chrome-devtools accessibility tooling.

**Footprint honesty:** three.js + 3d-force-graph is the only heavy add
(~hundreds of KB, vendored). Everything else (charts, Insight, IA, fullscreen)
is hand-rolled SVG/CSS, consistent with the no-build ethos.

## Verification

- **Backend (TDD, first):** extend `tests/test_web.py` — each new route
  dispatches to the right `service.*` method, honours the auth gate, returns
  405 on the wrong verb, and handles `available: False` (digest/communities
  before any dream). Tests use a **mock service** (no PG/torch), preserving the
  repo's fast pure-logic convention. (This is separate from the fixture
  devserver, which now requires torch — run it with `.venv`.)
- **Frontend QA:** chrome-devtools MCP (fresh cache) across every tab in
  dark / light / mobile, asserting zero console errors and exercising: spectral
  colours actually render, continuum fills visible, Insight digest, Recall
  chains, galaxy + community colouring + **fullscreen enter/exit/resize/ESC**,
  charts, and confirm-gated write actions.
- **Accessibility:** keyboard + focus + contrast pass; table view as the
  graph's a11y fallback.

## Risks & mitigations

- **Vendored three.js (size/offline):** pin a version, vendor it, lazy-load,
  record the license, and verify with the network disabled.
- **Galaxy perf on large graphs:** neighbourhoods stay depth-capped (≤3) and
  node-capped; WebGL handles the rest.
- **Fullscreen API blocked** (iframe/permissions): CSS-maximize fallback.
- **Digest/communities need a prior dream:** every new surface has a graceful
  empty state.
- **`el()` fix regression:** minimal (one branch); covered by visual QA.
- **Live deploy:** additive only — schema is unchanged (`graph_digest` /
  `communities` already in v12/v13). Ship via the established daemon-rebuild +
  `backup.ps1`-first procedure, user-present, never `down -v`.

## Out of scope

- Chat UI (Claude is the LLM; this is an instrument panel).
- Multi-user accounts / RBAC (single operator, loopback-first, optional bearer
  token — unchanged from v1).
- Any change to the MCP surface or business logic; the console remains a view
  over the daemon.
