# Cortex Console v2 — Phase 2 (Rich-Viz Elevation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Elevate the console to rich, product-grade visualisation — a vendored 3D graph galaxy (community-coloured, fullscreen), hand-rolled SVG charts, a golden-signals dashboard, and an accessibility pass — while keeping the offline / no-build ethos.

**Architecture:** One vendored offline ESM bundle (3d-force-graph, three.js inlined) lazy-imported only by the galaxy view; everything else hand-rolled SVG/CSS. No build step, no CDN at runtime.

**Tech Stack:** Vanilla ES modules + SVG/CSS. Python ASGI. Browser QA via chrome-devtools MCP against the fixture devserver (`.venv`, port 8770).

## Global Constraints

- **No build step, no CDN at runtime.** The galaxy bundle is vendored (already downloaded to `pseudolife_memory/web/static/vendor/3d-force-graph.bundle.js`, esm.sh `3d-force-graph@1.73.6?bundle`, 1.35 MB, self-contained — three.js inlined, no external imports). Lazy-load via `import('/ui/vendor/3d-force-graph.bundle.js')`.
- **Run Python under `.venv`.** Restart the devserver after editing routes/fixtures; full browser reload for JS/CSS.
- **`prefers-reduced-motion`:** the galaxy must not auto-spin; honour reduced motion.
- **Accessibility:** the table view is the galaxy's keyboard/screen-reader fallback.
- **Commit style:** conventional commits ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Branch:** `feat/cortex-console-v2-p2` (already created off master).

## Verified 3d-force-graph API (from the README + a live spike)

```js
const mod = await import('/ui/vendor/3d-force-graph.bundle.js');
const ForceGraph3D = mod.default;                 // constructor
const g = new ForceGraph3D(domElement, {})        // NB: `new`, element first
  .graphData({ nodes: [{id}], links: [{source, target}] })
  .nodeId('id').nodeLabel(fn).nodeColor(fn).nodeVal(fn)
  .linkColor(fn).linkDirectionalArrowLength(n)
  .backgroundColor('rgba(0,0,0,0)').width(px).height(px)
  .onNodeClick(node => …)
  .cooldownTime(ms).warmupTicks(n)
  .zoomToFit(ms, padding);
g.scene(); g.camera(); g._destructor();           // _destructor() cleans up (verified)
```
Spike confirmed: import works, canvas renders, `scene().children` populated, `_destructor()` exists.

---

### Task 1: Vendor the galaxy bundle + provenance

**Files:**
- Keep: `pseudolife_memory/web/static/vendor/3d-force-graph.bundle.js` (already downloaded)
- Create: `pseudolife_memory/web/static/vendor/README.md` (provenance + license)

- [ ] **Step 1:** Confirm the bundle is present and self-contained:
  `ls -la pseudolife_memory/web/static/vendor/` (≈1.35 MB) and `grep -c 'WebGLRenderer' …bundle.js` (>0).
- [ ] **Step 2:** Write `vendor/README.md` recording: source `https://esm.sh/3d-force-graph@1.73.6?bundle&target=es2020` (resolved to the `es2020/3d-force-graph.bundle.mjs` artifact), version 1.73.6, that three.js is inlined, both MIT-licensed (3d-force-graph © Vasco Asturiano; three.js © three.js authors), date 2026-06-26, and that it is served at `/ui/vendor/` and lazy-imported by the galaxy view. No CDN at runtime.
- [ ] **Step 3:** Commit.

```bash
git add pseudolife_memory/web/static/vendor/
git commit -m "$(printf 'chore(web): vendor 3d-force-graph@1.73.6 bundle (offline, three inlined)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: `charts.js` — hand-rolled SVG chart primitives

**Files:**
- Create: `pseudolife_memory/web/static/js/charts.js`
- Modify: `pseudolife_memory/web/static/css/styles.css` (chart styling)

**Interfaces:**
- Produces: `donut(segments, {size})`, `barRows(rows, {max, fmt})`, `sparkline(values, {w,h})`. Each returns an SVG/DOM node. `segments`/`rows` = `[{label, value, color}]`. Colors come from CSS vars passed by callers.

- [ ] **Step 1:** Write `charts.js` using the `el` helper (SVG via `document.createElementNS` — add a tiny `svgEl(tag, attrs, ...kids)` local helper since `el` makes HTML elements). Implement:
  - `donut(segments, {size=120, thickness=14})` → an SVG ring; each segment a `<circle>` with `stroke-dasharray`/`stroke-dashoffset`; center shows the total. `title` per segment.
  - `barRows(rows, {max, valueFmt})` → a list of label + horizontal bar (`<div>`-based, reusing `.gn-bar`-style) + value. (DOM, not SVG — simpler and matches god-node bars.)
  - `sparkline(values, {w=120, h=28, color})` → an SVG `<polyline>`.
- [ ] **Step 2:** Add CSS: `.chart-legend` (dot + label rows), `.donut-wrap` (flex donut + legend), reuse `.gn-bar` for bars.
- [ ] **Step 3: Verify in the browser.** Restart not needed (JS/CSS). On any page, `evaluate_script` importing `charts.js` and asserting `donut([{label:'a',value:3,color:'#3fd0c9'}], {}).querySelector('circle')` is non-null and `sparkline([1,2,3]).tagName==='svg'`.
- [ ] **Step 4: Commit** (`feat(web): hand-rolled SVG chart primitives (donut/bars/sparkline)`).

---

### Task 3: Observatory — golden-signals reorder + distribution charts

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (`_overview`: add `facts_by_origin`)
- Modify: `pseudolife_memory/web/fixtures.py` (only if `_overview` reads a field fixtures lack — it reuses `cortex_dump`, so no change)
- Modify: `pseudolife_memory/web/static/js/views/observatory.js`
- Test: `tests/test_web.py` (assert `facts_by_origin` in overview counts)

**Interfaces:**
- Consumes: `charts.js` (`donut`, `sparkline`), `/api/overview` (now with `counts.facts_by_origin`), `/api/sources`, `stats.bands`.

- [ ] **Step 1 (TDD backend):** add a test in `tests/test_web.py`:

```python
def test_overview_has_facts_by_origin(svc):
    ov = ConsoleRoutes(svc).dispatch("GET", "/api/overview", {}, {})
    assert "facts_by_origin" in ov["counts"]
    assert isinstance(ov["counts"]["facts_by_origin"], dict)
```

- [ ] **Step 2:** Run it — FAIL (key missing).
- [ ] **Step 3:** In `routes.py` `_overview`, after `contested = …`, add:

```python
        from collections import Counter
        by_origin = dict(Counter((f.get("origin") or "agent") for f in facts))
```

and add `"facts_by_origin": by_origin,` to the returned `counts` dict.

- [ ] **Step 4:** Run the web suite — PASS.
- [ ] **Step 5 (frontend reorder + charts):** In `observatory.js`:
  - **Golden-signals header:** add a top status strip (above the stat cards) that foregrounds the critical signals left→right: storage/schema health, `persist_errors` (red chip when >0), dream `would_fire`, contested facts, stale world. (Reuse `chip`/`pulse-dot`.)
  - Keep the spectral stat cards below.
  - **Add a distributions panel** below the continuum: a `donut` of `counts.facts_by_origin` (user=green, action=azure, agent=slate to match origin badges) + a `barRows` of top sources from `/api/sources` (slice 6).
  - **Continuum sparkline:** add a small hit-rate `sparkline` to the continuum panel header from `stats.bands.map(b => b.hit_rate)`.
- [ ] **Step 6: Verify in the browser.** Full reload `#/observatory`; `evaluate_script` asserts the donut (`.donut-wrap svg circle`) and source bars render; the golden-signals strip shows health/dream chips. Screenshot dark + light. Zero console errors.
- [ ] **Step 7: Commit** (`feat(web): Observatory golden-signals strip + origin/source distributions + hit-rate sparkline`).

---

### Task 4: Insight — community-size distribution chart

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/insight.js`

- [ ] **Step 1:** In `insight.js`, in the Communities panel, prepend a `barRows` (from `charts.js`) of the top communities by size (`d.communities` sorted desc, slice 8, color `var(--c-graph)`), above the existing table. Keep the table.
- [ ] **Step 2: Verify in the browser.** Full reload `#/insight`; assert the community bars render alongside the table. Screenshot.
- [ ] **Step 3: Commit** (`feat(web): Insight community-size distribution bars`).

---

### Task 5: 3D graph galaxy (Galaxy / Graph / Table) + community colour + fullscreen

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/graph.js`
- Modify: `pseudolife_memory/web/fixtures.py` (add `community` to `graph_neighborhood` nodes for QA)
- Modify: `pseudolife_memory/web/static/css/styles.css` (fullscreen + galaxy host)

**Interfaces:**
- Consumes: the vendored bundle (lazy `import`), `graph_neighborhood` data (nodes may carry `community`/`etype`), Fullscreen API.

- [ ] **Step 1:** Add `community` to fixture `graph_neighborhood` nodes (a few distinct ints) so colouring is visible in QA; fall back to `etype` colour when `community` is absent.
- [ ] **Step 2:** In `graph.js`, extend the view toggle to three modes: `galaxy | graph | table` (default keep `graph`). Add a `galaxy` branch in `paint()`.
- [ ] **Step 3:** Implement `renderGalaxy(host, data)`:
  - Lazy `const mod = await import('/ui/vendor/3d-force-graph.bundle.js'); const FG = mod.default;` inside a try/catch — on failure, toast + fall back to the 2D graph mode (the bundle is large; show a `loadingBlock` while importing).
  - Build `{nodes: data.nodes.map(n => ({id:n.entity, etype, community, facts})), links: data.edges.map(e => ({source:e.src, target:e.dst, derived}))}`.
  - Colour: a stable hue per `community` (`hsl(hash(community)*…)`); fall back to `colorFor(etype)`. `nodeLabel(n => n.id)`, `nodeVal(n => 1 + degree)`, `linkColor(e => e.derived ? dashed-ish grey : grey)`, `linkDirectionalArrowLength(3)`, `backgroundColor('rgba(0,0,0,0)')`.
  - Size to the `.graph-wrap`; `ResizeObserver` → `g.width()/g.height()`.
  - `onNodeClick(n => location.hash = "#/graph?entity=" + encodeURIComponent(n.id))`.
  - **Reduced motion:** if `matchMedia('(prefers-reduced-motion: reduce)').matches`, set `cooldownTime(0)` (freeze quickly, no lingering motion); never call any rotation API.
  - Keep a module ref `fg3d`; on re-render/leave call `fg3d._destructor()` and null it (mirror the existing 2D `fg.stop()` cleanup in `load()`/`paint()`).
- [ ] **Step 4: Fullscreen control** (applies to galaxy and 2D): add a maximize button to the graph toolbar. On click, if `document.fullscreenElement` then `document.exitFullscreen()`, else `wrap.requestFullscreen()` (with a `.catch` fallback that toggles a `.maximized` CSS class on `.graph-wrap`). Listen for `fullscreenchange` to resize the active renderer (2D `ResizeObserver` already handles it; for galaxy call `g.width/height`). ESC exits natively. Add `.graph-wrap.maximized { position:fixed; inset:0; z-index:80; height:100vh; border-radius:0; }`.
- [ ] **Step 5:** Add the etype/community note to the legend for galaxy mode (community colouring → "coloured by community").
- [ ] **Step 6: Verify in the browser.** Full reload `#/graph?entity=pseudolife-mcp&depth=2`; switch to **galaxy** → assert a `.graph-wrap canvas` appears and `import` resolved (no console error); community colours differ; click a node re-seeds; click fullscreen → `document.fullscreenElement` set (or `.maximized` class) and canvas resizes; ESC/again exits. Switch to **table** → table renders (keyboard fallback). Screenshot galaxy dark + light.
- [ ] **Step 7: Commit** (`feat(web): 3D graph galaxy (community-coloured, lazy-loaded) + fullscreen`).

---

### Task 6: Accessibility pass

**Files:**
- Modify: `graph.js`, `stream.js`, `cortex.js`, `insight.js`, `styles.css` (targeted fixes only)

- [ ] **Step 1: Audit with chrome-devtools.** On the devserver, for each new/changed surface check: every interactive control is keyboard-reachable and has an accessible name (icon-only buttons — `⋯`, `trace ↗`, `+ fact`, `forget`, the fullscreen/zoom buttons — need `aria-label`/`title`); `:focus-visible` rings are present; the galaxy offers the table as a documented fallback. Run `evaluate_script` to list buttons whose `(textContent||ariaLabel||title)` is empty.
- [ ] **Step 2: Fix found gaps.** Add `aria-label` to any icon-only control missing a name; add a global `:focus-visible { outline:2px solid color-mix(in srgb,var(--accent) 70%, transparent); outline-offset:2px; }` rule if focus rings are weak; ensure the galaxy view has a visible "switch to table for keyboard access" affordance (the existing table toggle suffices — verify it is reachable).
- [ ] **Step 3: Contrast spot-check** the warm light theme on `--ink-3` text and chips; bump a token only if a check fails (note any change).
- [ ] **Step 4: Verify** zero console errors across all tabs (dark + light); the audit `evaluate_script` reports no unnamed interactive controls.
- [ ] **Step 5: Commit** (`feat(web): accessibility pass — names, focus rings, galaxy table fallback`).

---

## Phase 2 exit check

- [ ] `.venv/Scripts/python.exe -m pytest tests/test_web.py -q` passes (incl. `facts_by_origin`).
- [ ] Browser pass at `http://127.0.0.1:8770/ui/`: zero console errors; galaxy loads + community-coloured + fullscreen works + reduced-motion respected; charts render on Observatory + Insight; golden-signals strip present; no unnamed interactive controls.
- [ ] Additive only (one new endpoint field, no schema change) — deployable via the established `backup.ps1`-first daemon rebuild.
