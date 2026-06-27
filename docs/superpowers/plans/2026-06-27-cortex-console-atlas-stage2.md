# Atlas Stage 2 — the Atlas view (seedless project-scoped map) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new "Atlas" console view that opens to the whole graph (no seed needed), colours it by project, and lets the user switch project scope — closing the original "type a seed to see anything" pain on screen.

**Architecture:** Extract the existing canvas/galaxy graph renderer out of `views/graph.js` into a shared `js/graphview.js` so both the Graph explorer and the new Atlas overview reuse one engine (with a `colorBy` option for project tinting). `views/atlas.js` fetches the Stage-1 endpoints (`/api/graph?scope=`, `/api/graph/projects`), renders the seedless scoped map, and adds a project switcher. Cortex/Insight rows gain a "Show in Atlas" link. The review queue + mutation actions are Stage 3 — NOT in this plan.

**Tech Stack:** Vanilla ESM (no build, no framework), the `el()` hyperscript + `components.js` helpers, the vendored 3d-force-graph for galaxy; verified in-browser via the fixture devserver + chrome-devtools.

## Global Constraints

- **No JS test runner exists.** Frontend verification is the established Cortex Console method: run the fixture devserver and drive it with chrome-devtools — assert the view renders, the interaction works, and **console has zero errors/warnings**. Devserver launch (from repo root, background): `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770` → `http://127.0.0.1:8770/ui/`. It serves the real routes against `FixtureService` (which already returns `graph_neighborhood(scope=)` with `sources` on nodes and `graph_projects()` — added in Stage 1 Task 6).
- **Match existing patterns.** Use `el`/`mount`/`clear` from `util.js`; `facetBar`/`searchBox`/`panel` from `components.js`; CSS classes already in `static/css/styles.css` (`toolbar`, `facets`/`facet`, `graph-wrap`, `graph-legend`, `graph-hint`, `graph-zoom`, `graph-fs`). Do NOT invent a new aesthetic or add a build step. Two font weights, sentence case, CSS variables for colour.
- **No backend changes.** All endpoints exist from Stage 1. This is frontend-only (plus possibly extending the Python web dispatch tests is unnecessary — no routes change).
- **Reuse, don't duplicate.** The Atlas map must use the same renderer as Graph (via `graphview.js`), not a copy.
- **Stage boundary:** Stage 2 ships the map + project switcher + "Show in Atlas" links only. No review queue, no action panel, no mutations.

## File Structure

- `static/js/graphview.js` — NEW. The shared renderer extracted from `graph.js`: `ETYPE_COLOR`, `colorFor`, `communityColor`, `projectColor` (new), the `ForceGraph` canvas class, `renderGalaxy`/`galaxyLegend`/`cleanupGalaxy`, `tableView`, `legend`, `zoomControls`, `fullscreenBtn` (+ `toggleFullscreen`/`enterMaximized`), `clamp`. Renderer gains a `colorBy` option: `"etype"` (default) | `"community"` | `"project"`.
- `static/js/views/graph.js` — MODIFY. Import the renderer from `graphview.js`; delete the moved code; behaviour unchanged.
- `static/js/views/atlas.js` — NEW. The Atlas view (project switcher + seedless scoped map + view toggle).
- `static/js/app.js` — MODIFY. Import `renderAtlas`; add a ROUTES entry under the "Structure" group.
- `static/js/views/cortex.js` — MODIFY. Add a "Show in Atlas" link to each entity card header.
- `static/js/views/insight.js` — MODIFY. Add a "Show in Atlas" affordance to god-node rows.

---

### Task 1: Extract the shared graph renderer into `graphview.js`

**Files:**
- Create: `static/js/graphview.js`
- Modify: `static/js/views/graph.js`

**Interfaces:**
- Produces (exports from `graphview.js`):
  - `class ForceGraph(canvas, wrap, data, opts)` where `opts = { seed?: string, colorBy?: "etype"|"community"|"project", onSelect?: (node)=>void }`
  - `renderGalaxy(host, data, opts)` (opts `{ colorBy, onNodeClick }`), `cleanupGalaxy()`
  - `tableView(data)`, `legend(etypes, mode)`, `zoomControls(fg)`, `fullscreenBtn(wrap)`
  - `colorFor(etype)`, `communityColor(node)`, `projectColor(node)`

- [ ] **Step 1: Create `graphview.js` by relocating the renderer**

Move these symbols verbatim from `static/js/views/graph.js` into a new `static/js/graphview.js`, exporting each: `ETYPE_COLOR`, `colorFor`, `communityColor`, `cleanupGalaxy`, `renderGalaxy`, `galaxyLegend`, `fullscreenBtn`, `enterMaximized`, `toggleFullscreen`, `clamp`, the entire `ForceGraph` class, `zoomControls`, `legend`, `tableView`. Keep the module-level galaxy handle local to `graphview.js` (the `fg3d` variable + `cleanupGalaxy` move together). Add at the top:

```javascript
// graphview.js — shared knowledge-graph renderer (canvas force sim + 3D galaxy +
// table), reused by the Graph explorer and the Atlas overview.
import { el, mount, errorBlock } from "./util.js";
import { badge } from "./components.js";
```

Add a project-colour helper (deterministic hue from the first source string):

```javascript
export function projectColor(n) {
  const s = (n.sources && n.sources[0]) || "";
  if (!s) return "#6b7280";                 // unattributed → neutral grey
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 62% 58%)`;
}
```

Make `ForceGraph` honour `opts.colorBy` when filling nodes. In the constructor, accept `opts` and store `this.colorBy = opts.colorBy || "etype"; this.seed = opts.seed; this.onSelect = opts.onSelect;`. Replace the node fill colour lookup in `draw()` (currently `const nc = colorFor(n.etype)`) with:

```javascript
      const nc = this.colorBy === "project" ? projectColor(n)
               : this.colorBy === "community" ? communityColor(n)
               : colorFor(n.etype);
```

(`build()` already copies node fields; ensure it also copies `community` and `sources`: in the `node(n.entity, {...})` call inside `build()`, add `community: n.community, sources: n.sources`.)

In `renderGalaxy`, accept `opts` and pick the node colour by `opts.colorBy` the same way (the galaxy already maps `community`; extend it: `.nodeColor((n) => opts.colorBy === "project" ? projectColor(n) : communityColor(n))`, and include `sources` in the mapped node objects).

Update `legend(etypes, mode)` to render a project/community swatch when `mode` is `"project"` or `"community"` (a single "by project"/"by community" entry like `galaxyLegend` does) instead of the etype list.

- [ ] **Step 2: Rewrite `graph.js` to import the renderer**

In `static/js/views/graph.js`, delete the relocated symbols and import them:

```javascript
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { badge } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn, colorFor } from "../graphview.js";
```

Update the two construction call-sites to the new options object:
- `fg = new ForceGraph(canvas, wrap, data, { seed: state.entity, onSelect: (node) => showNode(wrap, node) });`
- `renderGalaxy(host, data, { colorBy: "community", onNodeClick: (id) => { location.hash = "#/graph?entity=" + encodeURIComponent(id) + "&depth=" + state.depth; } });`
  (Move the galaxy's `onNodeClick` body into the opts callback; the galaxy currently hardcodes it.)

The Graph view's `paint()` still passes `state.entity` as the seed and keeps its current `colorBy` default (`etype`).

- [ ] **Step 3: Verify the Graph tab is unchanged (devserver + chrome-devtools)**

Start the devserver in the background:
`HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770`

With chrome-devtools: `navigate_page` to `http://127.0.0.1:8770/ui/#/graph`, then enter a seed (the fixture defaults to `pseudolife-mcp` so an empty Explore works), `take_snapshot`, and `list_console_messages`. Verify: the force graph renders (nodes + edges visible), the galaxy and table toggles work, and **console shows zero errors**. Click a node → node panel appears. This is a pure refactor, so behaviour must match pre-change.

- [ ] **Step 4: Commit**

```bash
git add static/js/graphview.js static/js/views/graph.js
git commit -m "refactor(web): extract shared graph renderer into graphview.js"
```

---

### Task 2: The Atlas view — project switcher + seedless scoped map

**Files:**
- Create: `static/js/views/atlas.js`
- Modify: `static/js/app.js`

**Interfaces:**
- Consumes: `ForceGraph`, `renderGalaxy`, `cleanupGalaxy`, `tableView`, `legend`, `zoomControls`, `fullscreenBtn`, `projectColor` from `graphview.js`; `GET /api/graph?scope=`, `GET /api/graph/projects`.
- Produces: `export async function renderAtlas(root, ctx)`; route id `atlas`.

- [ ] **Step 1: Create `static/js/views/atlas.js`**

```javascript
// views/atlas.js — the Atlas overview: the WHOLE graph, no seed needed, with a
// project/topic switcher. scope=all colours nodes by project; a single project
// colours by community. Reuses the shared renderer in graphview.js. (The review
// queue + cleanup actions are Stage 3 — not here.)
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn } from "../graphview.js";

let state = { scope: "all", view: "map" };
let fg = null;

function parseHash() {
  const qi = location.hash.indexOf("?");
  const p = new URLSearchParams(qi >= 0 ? location.hash.slice(qi + 1) : "");
  return { scope: p.get("scope") || "all", entity: p.get("entity") || "" };
}

export async function renderAtlas(root, ctx) {
  const fromHash = parseHash();
  if (fromHash.scope) state.scope = fromHash.scope;
  const seed = fromHash.entity || "";

  mount(root, loadingBlock("Mapping the graph…"));
  let projects = [];
  try { projects = (await api.get("/api/graph/projects")).projects || []; }
  catch (err) { mount(root, errorBlock(err)); return; }

  const host = el("div", {});
  const scopeOpts = [{ value: "all", label: "all projects" }]
    .concat(projects.map((p) => ({ value: p.source, label: `${p.source} (${p.entities})` })));
  const switcher = facetBar(scopeOpts, state.scope, (v) => { state.scope = v; load(); });
  const viewToggle = facetBar(
    [{ value: "map", label: "map" }, { value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });

  mount(root,
    el("div", { class: "toolbar" },
      el("span", { class: "eyebrow" }, "scope"), switcher,
      el("span", { class: "grow" }), viewToggle),
    host);

  async function load() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    mount(host, loadingBlock("Mapping the graph…"));
    try {
      const data = await api.get("/api/graph", { scope: state.scope });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  function paint(data) {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    if (!data || !(data.nodes || []).length) {
      mount(host, emptyBlock("No graph in scope",
        state.scope === "all" ? "The graph is empty." : `No attributed entities in "${state.scope}".`));
      return;
    }
    const colorBy = state.scope === "all" ? "project" : "community";
    if (state.view === "table") { mount(host, tableView(data)); return; }
    if (state.view === "galaxy") {
      renderGalaxy(host, data, { colorBy,
        onNodeClick: (id) => { location.hash = "#/graph?entity=" + encodeURIComponent(id); } });
      return;
    }
    const wrap = el("div", { class: "graph-wrap" });
    const canvas = el("canvas", {});
    wrap.appendChild(canvas);
    mount(host, wrap);
    fg = new ForceGraph(canvas, wrap, data, { seed, colorBy,
      onSelect: (node) => { location.hash = "#/graph?entity=" + encodeURIComponent(node.entity); } });
    wrap.appendChild(zoomControls(fg));
    wrap.appendChild(legend([], colorBy));
    wrap.appendChild(el("div", { class: "graph-hint" },
      `${(data.nodes || []).length} entities · scroll to zoom · drag to pan · click a node to explore`));
    wrap.appendChild(fullscreenBtn(wrap));
  }

  load();
}
```

- [ ] **Step 2: Register the Atlas route in `app.js`**

Add the import (with the other view imports near the top of `static/js/app.js`):

```javascript
import { renderAtlas } from "./views/atlas.js";
```

Add a ROUTES entry in the "Structure" group, immediately after the `graph` entry:

```javascript
  { id: "atlas", label: "Atlas", group: "Structure", accent: "var(--c-graph)", view: renderAtlas, countKey: null },
```

- [ ] **Step 3: Verify Atlas on the devserver (chrome-devtools)**

Devserver running (Task 1 Step 3). With chrome-devtools: `navigate_page` to `http://127.0.0.1:8770/ui/#/atlas`. `take_snapshot` + `take_screenshot` + `list_console_messages`. Verify:
- The Atlas nav item appears under Structure; the view loads WITHOUT entering a seed (the seedless win).
- The scope switcher lists "all projects" plus the fixture projects (`pseudolife-mcp`, `gw2-reshade`, `hermes-infra`).
- The map renders nodes coloured by project (multiple hues) at scope=all.
- Clicking a project facet (e.g. `gw2-reshade`) re-fetches and the map shrinks to that project (fixture filters to GW2-prefixed nodes); the legend reads "by community".
- `map`/`galaxy`/`table` toggles all render. Console shows zero errors across these interactions.

- [ ] **Step 4: Commit**

```bash
git add static/js/views/atlas.js static/js/app.js
git commit -m "feat(web): Atlas view — seedless project-scoped graph + switcher"
```

---

### Task 3: "Show in Atlas" cross-links from Cortex and Insight

**Files:**
- Modify: `static/js/views/cortex.js`
- Modify: `static/js/views/insight.js`

**Interfaces:**
- Consumes: the `atlas` route (Task 2). Navigation target: `#/atlas?entity=<name>` (Atlas reads `entity` from the hash and seeds/centres it on the scope=all map).

- [ ] **Step 1: Add "Show in Atlas" to Cortex entity cards**

In `static/js/views/cortex.js`, locate the `entityCard(g, ctx)` function's header row (the element holding the entity name). Add a small link button after the entity name:

```javascript
      el("button", { class: "btn sm ghost", title: "Show in Atlas",
        onclick: () => { location.hash = "#/atlas?entity=" + encodeURIComponent(g.entity); } },
        "Atlas ↗"),
```

(Match the existing header markup — insert it as a sibling of the entity-name node, inside the card head. Use the `btn sm` classes already used elsewhere in the file/console.)

- [ ] **Step 2: Add "Show in Atlas" to Insight god-node rows**

In `static/js/views/insight.js`, the `godNodesPanel` rows currently navigate to `#/graph?entity=`. Add an Atlas affordance: change the god-node row so the primary click still goes to Graph, but append a small "Atlas ↗" control that navigates to `#/atlas?entity=`. Concretely, inside the `gn-row` button, after the `gn-deg` span, add:

```javascript
        el("span", { class: "gn-atlas", title: "Show in Atlas", role: "link",
          onclick: (e) => { e.stopPropagation();
            location.hash = "#/atlas?entity=" + encodeURIComponent(n.display); } }, "↗"),
```

(`e.stopPropagation()` keeps the row's own Graph navigation from also firing.)

- [ ] **Step 3: Verify the cross-links (chrome-devtools)**

Devserver running. With chrome-devtools: navigate to `#/cortex`, `take_snapshot`, find an entity card, `click` its "Atlas ↗" button, and confirm the hash becomes `#/atlas?entity=…` and the Atlas view renders with that entity present/centred. Repeat from `#/insight` (god-node "↗"). `list_console_messages` — zero errors.

- [ ] **Step 4: Commit**

```bash
git add static/js/views/cortex.js static/js/views/insight.js
git commit -m "feat(web): Show-in-Atlas links from Cortex and Insight"
```

---

## Self-Review

**Spec coverage (Stage 2 = spec §D "Atlas view" + §E stage 2):**
- Seedless scoped map (opens without a seed) → Task 2. ✓
- Project switcher as the primary control → Task 2 (`facetBar` over `/api/graph/projects`). ✓
- Colour by project in "all" scope, by community within a project → Tasks 1 (`projectColor`, `colorBy`) + 2. ✓
- Galaxy/Table inherit the scope → Task 2 (same `data`, same toggle). ✓
- "Show in Atlas" from Cortex + Insight → Task 3. ✓
- Reuse the existing renderer (no duplication) → Task 1 (shared `graphview.js`). ✓

**Out of Stage 2 (Stage 3):** review queue, action panel, `graph_review`, mutation endpoints, node-cap/`truncated` UI.

**Placeholder scan:** none — new logic is given in full; the Task 1 extraction is a precise relocation of named symbols.

**Type consistency:** `ForceGraph(canvas, wrap, data, opts)` with `opts.{seed,colorBy,onSelect}`; `renderGalaxy(host, data, {colorBy,onNodeClick})`; `legend(etypes, mode)`; `projectColor(node)` — used identically in Tasks 1→2. The Graph view (Task 1 Step 2) and Atlas view (Task 2) both construct `ForceGraph`/`renderGalaxy` with the same option shape.

**Verification reality:** no JS unit tests; every task ends with a chrome-devtools pass on the fixture devserver (renders + interaction + zero console errors), which is how the rest of the console was QA'd. The FixtureService already serves `scope` + `sources` + `graph_projects` (Stage 1 Task 6), so the devserver exercises the real fetch/render paths.
