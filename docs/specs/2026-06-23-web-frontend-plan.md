# Cortex Console — implementation plan & progress tracker

Companion to `2026-06-23-web-frontend-design.md`. Phased so each `/loop`
iteration lands a coherent slice. Checkboxes are the live progress tracker.

## Phase 0 — design & harness
- [x] Research (mem0/zep/letta/cognee, doobidoo dashboard, KG-viz UX)
- [x] Design doc + this plan
- [x] Branch `feat/web-ui`
- [x] Backend skeleton: `web/__init__.py`, `web/api.py` (ASGI router + static),
      `web/routes.py` (all read + enumerated write endpoints), wired into
      `daemon.py` (build_console_app replaces AuthHealthASGI; /ui+/api gated)
- [x] `web/config_io.py` (read + write: 28 knobs / 7 groups, atomic yaml +
      backup, live-mutate + restart classification) — verified
- [x] `web/devserver.py` + `web/fixtures.py` (fixture-backed, no PG/torch) —
      QA harness; backend smoke-verified (routes, config r/w, ASGI, 404/auth)

## Phase 1 — shell + Observatory  ✅
- [x] `static/index.html`, `static/css/styles.css` (design system; vendored OFL
      fonts Fraunces/Hanken Grotesk/JetBrains Mono; dark+light themes)
- [x] `static/js/{app,api,util,ui}.js` (hash router, fetch client w/ token,
      hyperscript helper, toast/modal/drawer); per-section spectral tinting
- [x] Observatory: 6 spectral count cards, 8-band MIRAS continuum, dream gauges
      + run-dream action, system panel; nav counts + topbar status chips
- [x] 7 view stubs so module graph resolves (cortex/world/lessons/stream/
      graph/episodes/console — filled in Phases 2-5)
- [x] QA via chrome-devtools MCP (fresh-cache browser): dark + light + mobile
      (390px) all clean, **zero console errors**

### QA note (cache gotcha)
Serve css/js/html with `Cache-Control: no-store` (fonts/images cached).
Playwright MCP keeps a persistent browser cache that `page.close()` does NOT
flush — use the **chrome-devtools MCP** browser for UI QA (fresh cache; honours
no-store). Dev server: `python -m pseudolife_memory.web.devserver --port 8770`,
run via the tool's `run_in_background` (NOT PowerShell Start-Job — it dies with
the one-shot shell). Screenshots → `.qa/` (gitignored).

## Phase 2 — review surfaces (read)
- [x] `components.js` (panel/badge/originBadge/confMeter/searchBox/facetBar/groupHead)
- [x] Cortex tab — facts grouped by entity, provenance badges + confidence
      meters, contested-fact Accept/Discard, **history-timeline drawer**
      (current vs superseded, attributed). QA ✓
- [x] World tab — cited cards, freshness badge, decayed confidence, stale flag,
      source quote+link. QA ✓
- [x] Lessons tab — task-grouped, do/avoid polarity borders, outcome, about,
      polarity filter. QA ✓
- [x] Episodes tab — session timeline + summary drawer (tags/sources/recent). QA ✓
- [x] Stream tab — live search + recent stream, rerank/BM25 toggles, source
      facets, superseded indicator, **ranking-trace debugger drawer**. QA ✓
- [x] Sources/Tags facets (folded into Stream)
- [x] Bug fixed: overlays (drawer/modal) now close on route change
- [x] a11y: search inputs + selects get name + aria-label

## Phase 3 — graph visualizer  ✅
- [x] `static/js/views/graph.js` — canvas force-directed sim (repulsion/spring/
      centering, alpha cooling, DPR-aware, pointer drag), legend, hint
- [x] Seed input + depth select + Explore; click node → facts panel +
      re-center; expand via re-seed; derived (dashed) vs explicit edges + arrows
- [x] Table-view toggle (entities + relations tables); theme-aware canvas colors
- [x] QA: graph render, node-click panel, table view, dark + light. ✓

## Phase 4 — search/trace lab + recall  ✅ (folded into Stream)
- [x] Search panel with rerank/BM25 toggles
- [x] Ranking-trace debugger view (per-tier candidates + final top-k)
- [ ] Dedicated multi-hop recall view (recall is wired in API; surfaced via
      graph re-seed for now — optional dedicated view later)

## Phase 5 — Console (config editor) + write actions  ✅
- [x] `config_io.write` already done in Phase 0 (verified)
- [x] Console tab: grouped knobs, type-aware controls, live/restart badges,
      diff-preview modal, atomic save + backup. QA ✓ (fixed ctx-refresh bug)
- [x] Guarded write actions wired: fact resolve, dream run (Observatory),
      config save — all confirm/preview-gated. (delete/supersede/consolidate
      endpoints exist; UI surfaces are resolve + dream + config for now)
- [ ] Backend unit tests (config_io round-trip/validation; route dispatch) — TODO

## Polish + bug-sweep (Phase 6, in progress)
- [x] Graph canvas theme-aware (light-theme labels were invisible) — fixed
- [x] Mobile: fact-row + knob rows stack (were crushing values to 1ch) — fixed
- [x] Auth/token flow verified (401 → modal → authenticated)
- [x] Backend tests added (tests/test_web.py, 24 passing, no PG/torch)
- [x] Console toggles broken (self-referential `--accent: var(--accent)` on the
      Console route invalidated --accent → transparent switches/buttons) — fixed
- [x] graph-from-cortex "graph ↗" navigated to Observatory (routeId didn't strip
      `?query` before matching) — fixed
- [x] Observatory "Sources" card showed summed totals not distinct counts — fixed
- [x] Graph: wider default spread + camera (scroll-zoom, drag-pan, zoom buttons,
      auto-fit on load, fit button) for dense graphs — done, QA'd on 25-node fixture
- [ ] Final commit + PR (awaiting user sign-off)

## Live deploy log (daemon image rebuilds; ops/backup.ps1 each time; --no-deps)
- 2026-06-24 10:59 — initial console deploy (all 8 tabs)
- 2026-06-24 11:11 — graph-from-cortex routing + distinct source/tag counts
- 2026-06-24 11:27 — graph wider spread + zoom/pan/fit

## Phase 3 — graph visualizer
- [ ] Canvas force-directed sim (`static/js/graph.js`)
- [ ] Seed picker, expand-on-click, derived/inverse edge styling, node facts popover
- [ ] Table-view toggle; path-between-two mode
- [ ] QA

## Phase 4 — search/trace lab + recall
- [ ] Search panel with rerank/bm25/min_score toggles
- [ ] Ranking-trace debugger view (memory_trace)
- [ ] Multi-hop recall view

## Phase 5 — Console (config editor) + write actions
- [ ] `config_io.write` (validate → atomic yaml + live-mutate + restart flags)
- [ ] Config tab: grouped knobs, descriptions, diff-preview, save
- [ ] Guarded write actions: fact resolve/set/forget, delete, supersede,
      dream run, consolidate (each confirm-gated)
- [ ] Backend unit tests (config_io round-trip/validation; route dispatch/auth)

## Phase 6 — polish & hardening
- [ ] Full design-review pass (spacing, hierarchy, motion, empty/error/loading
      states, keyboard nav, a11y contrast, mobile widths)
- [ ] Bug sweep via browser driver across every tab
- [ ] README section + screenshots; CHANGELOG entry
- [ ] Final self-review; leave deploy (daemon image rebuild) for the user

## Deploy note (for the user, NOT done autonomously)
The live console ships by rebuilding the daemon image (`docker compose build
pseudolife-daemon && up -d --no-deps pseudolife-daemon`) — same procedure as
prior deploys, `ops/backup.ps1` first, never `down -v`, volumes preserved.
Until then, all work is verified on the fixture devserver. The daemon serves
the console at `http://127.0.0.1:8765/ui/` after deploy.

## Conventions
- Vanilla ES modules, no build step, no CDN (offline).
- Each REST handler wraps exactly one `service.*` method.
- Mutations are explicit, enumerated, confirm-gated in the UI, token-gated.
- Spectral accent per memory layer (see design doc).
