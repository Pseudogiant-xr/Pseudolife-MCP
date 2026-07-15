# Atlas Stage 2 — Galaxy Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The 3D galaxy becomes THE map — rebuilt vendored bundle (ForceGraph3D + THREE, license-audited), memory-state encodings, community nebulae + constellation labels, proximity node labels, search + fly-to, wiki-panel integration — and the 2D canvas map + Overview/Explore split are retired.

**Architecture:** One new vendored ES module (`galaxy.bundle.js`) built with esbuild from pinned npm packages, exporting the default `ForceGraph3D` constructor *and* its own `THREE` (single three.js instance — mixing two copies is the classic landmine). A new `galaxy.js` renderer module wraps it; `views/graph.js` is rewritten as the layout-A shell (galaxy + wiki panel + review drawer + table fallback). `/api/graph` whole-graph payload gains `created_at`/`asserted_at` (additive; feeds brightness now, the Stage 3 scrubber later).

**Tech Stack:** node 24 / npm 11 (build-time only), esbuild, `3d-force-graph@1.73.6` (pin — the version already proven in prod), vanilla-JS ES modules at runtime, pytest for the API change.

**Spec:** `docs/superpowers/specs/2026-07-15-atlas-wiki-galaxy-design.md` (§A, §C, §E).

## Global Constraints

- Full suite before final commit: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/` with bench Postgres at `127.0.0.1:5433` (skips are not passes).
- TDD with watched RED for API changes; frontend verified against `devserver.py` fixtures in the browser.
- **License gate:** the bundle ships only if every production dependency is under MIT / ISC / BSD-2 / BSD-3 / Apache-2.0 / 0BSD / CC0-1.0 / Unlicense. Anything else (GPL/LGPL/MPL/SSPL/unknown) → STOP, report, do not vendor. Keep `--legal-comments=eof` so upstream @license banners ride inside the bundle; inventory goes in `vendor/README.md`.
- The console stays fully offline at runtime: no CDN imports, the bundle is self-contained (verify: no `from "http`/`from "/` refs, `WebGLRenderer` present).
- No schema bump (API payload additions only).
- `prefers-reduced-motion` honored (no fly animation, cooldown 0, no pulse).
- Stage 3 items are OUT of scope here: time scrubber UI, review pulses/banners, isolate toggle.

---

### Task 1: Rebuild the vendored bundle (+ license audit)

**Files:**
- Create: `pseudolife_memory/web/static/vendor/galaxy.bundle.js` (generated artifact, committed)
- Modify: `pseudolife_memory/web/static/vendor/README.md`
- Delete: `pseudolife_memory/web/static/vendor/3d-force-graph.bundle.js` (in Task 4, once nothing imports it)
- Build scratch: `<scratchpad>/galaxy-bundle/` (never committed)

**Interfaces:**
- Produces: `import("/ui/vendor/galaxy.bundle.js")` → `{ default: ForceGraph3D, THREE }`, single three.js instance. Task 3 consumes exactly this.

- [ ] **Step 1: Scratch project + pinned deps**

```bash
mkdir -p "$SCRATCH/galaxy-bundle" && cd "$SCRATCH/galaxy-bundle"
npm init -y >/dev/null
npm install --save-exact 3d-force-graph@1.73.6
npm install --save-dev --save-exact esbuild@0.24.2 license-checker-rseidelsohn@4.4.2
```

- [ ] **Step 2: License audit — the gate**

```bash
npx license-checker-rseidelsohn --production --summary
npx license-checker-rseidelsohn --production --csv > licenses.csv
npm ls three
```

Verify: every license in the summary is in the allowlist above; `npm ls three`
shows exactly ONE resolved copy of three. If either fails → STOP and report to
the user (do not vendor). Save the summary text for the README.

- [ ] **Step 3: Entry + build**

`entry.mjs`:
```javascript
import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
export default ForceGraph3D;
export { THREE };
```

```bash
npx esbuild entry.mjs --bundle --format=esm --target=es2020 --minify \
  --legal-comments=eof --outfile=galaxy.bundle.js
```

- [ ] **Step 4: Verify the artifact**

```bash
node --input-type=module -e "import('./galaxy.bundle.js').then(m => { \
  console.log('default:', typeof m.default); \
  console.log('THREE.WebGLRenderer:', typeof m.THREE.WebGLRenderer); \
  console.log('same-instance sanity:', typeof m.THREE.Sprite); })"
grep -c "WebGLRenderer" galaxy.bundle.js         # > 0
grep -cE 'from ?"(https?:|/)' galaxy.bundle.js   # must be 0
ls -la galaxy.bundle.js                          # expect ~1.3–1.6 MB
```

Expected: `default: function`, `THREE.WebGLRenderer: function`, no external refs.

- [ ] **Step 5: Vendor + document**

Copy to `pseudolife_memory/web/static/vendor/galaxy.bundle.js`. Rewrite the
`## 3d-force-graph.bundle.js` section of `vendor/README.md` as
`## galaxy.bundle.js`: what it is, the exact pinned versions, the full build
recipe (Steps 1–4 verbatim, so the next update is reproducible), the
license-checker summary output, and the attribution lines (`3d-force-graph`
© Vasco Asturiano, MIT; `three.js` © three.js authors, MIT; plus every other
package in the summary). Note that legal comments are embedded at EOF of the
bundle itself.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/web/static/vendor/galaxy.bundle.js pseudolife_memory/web/static/vendor/README.md
git commit -m "feat(vendor): galaxy.bundle.js — ForceGraph3D + THREE, one instance, license-audited"
```

---

### Task 2: `/api/graph` whole-graph payload gains timestamps

**Files:**
- Modify: `pseudolife_memory/service.py` (`_whole_graph`, ~line 3736-3755)
- Modify: `pseudolife_memory/web/fixtures.py` (`graph_neighborhood` seedless path)
- Test: `tests/test_wiki_page.py`, `tests/test_web.py`

**Interfaces:**
- Produces: whole-graph nodes gain `"created_at": float`; whole-graph edges gain `"asserted_at": float`. Seeded-neighborhood path unchanged. Task 3 consumes both fields for the brightness encoding.

- [ ] **Step 1: Failing tests**

Append to `tests/test_wiki_page.py`:
```python
def test_whole_graph_payload_carries_timestamps(svc):
    _seed(svc)
    out = svc.graph_neighborhood(entity=None)
    assert out["found"] is True
    assert all(isinstance(n["created_at"], float) for n in out["nodes"])
    assert all(isinstance(e["asserted_at"], float) for e in out["edges"])
```

Append to `tests/test_web.py`:
```python
def test_graph_route_nodes_carry_timestamps(svc):
    out = ConsoleRoutes(svc).dispatch("GET", "/api/graph", {"scope": "all"}, {})
    assert all("created_at" in n for n in out["nodes"])
    assert all("asserted_at" in e for e in out["edges"])
```

- [ ] **Step 2: Watch them fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py::test_whole_graph_payload_carries_timestamps tests/test_web.py::test_graph_route_nodes_carry_timestamps -v`
Expected: both FAIL with `KeyError`/assert on the missing keys.

- [ ] **Step 3: Implement**

In `_whole_graph` (service.py): the node dict gains
`"created_at": e["created_at"]` (rows already carry it since Stage 1); the edge
dict gains `"asserted_at": e["asserted_at"]` (already selected by
`load_graph`). In `fixtures.py`'s seedless `graph_neighborhood` branch, add a
deterministic `created_at` per node (e.g. `1779600000.0 + i * 86400` in
enumeration order) and `asserted_at` per edge (e.g. `1780600000.0 + i * 43200`)
so the devserver exercises the encodings and, later, the scrubber.

- [ ] **Step 4: Green + regression**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wiki_page.py tests/test_web.py tests/test_fixture_contract.py tests/test_graph.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py pseudolife_memory/web/fixtures.py tests/test_wiki_page.py tests/test_web.py
git commit -m "feat(api): whole-graph payload carries created_at/asserted_at"
```

---

### Task 3: `galaxy.js` — the renderer

**Files:**
- Create: `pseudolife_memory/web/static/js/galaxy.js`
- Modify: `pseudolife_memory/web/static/js/graphview.js` (export hue helpers)

**Interfaces:**
- Consumes: `import("/ui/vendor/galaxy.bundle.js")` (Task 1); graph payload with timestamps (Task 2); `projectHue/communityHue/colorFor` from graphview.
- Produces: `createGalaxy(host, data, { colorBy, onNodeClick }) -> Promise<handle|null>` where `handle = { flyTo(name), setQuery(q), flyToBest(q), destroy() }`; `destroyGalaxy()` module-level cleanup. Task 4 consumes exactly these.

- [ ] **Step 1: Hue helpers in graphview.js**

Replace the bodies of `communityColor`/`projectColor` so hue derivation is
exported and reusable with a caller-chosen lightness:

```javascript
export function communityHue(n) {
  return (n.community != null && n.community !== "")
    ? (Math.abs(Number(n.community)) * 47) % 360 : null;
}

export function projectHue(n) {
  const s = (n.sources && n.sources[0]) || "";
  if (!s) return null;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

export function communityColor(n) {
  const h = communityHue(n);
  return h == null ? colorFor(n.etype) : `hsl(${h} 65% 60%)`;
}

export function projectColor(n) {
  const h = projectHue(n);
  return h == null ? "#6b7280" : `hsl(${h} 62% 58%)`;
}
```

(Behavior identical for existing callers — same hashes, same strings.)

- [ ] **Step 2: Write `galaxy.js`**

```javascript
// galaxy.js — the first-class 3D galaxy (Atlas stage 2, spec 2026-07-15 §C).
// Wraps the vendored bundle (ForceGraph3D + THREE — ONE three.js instance).
// Encodings: size = degree + facts, hue = project/community, lightness =
// recency of last activity. Community nebulae + constellation labels,
// proximity-faded node labels, search highlight, camera fly-to.
import { el } from "./util.js";
import { projectHue, communityHue, colorFor } from "./graphview.js";

let live = null;             // one galaxy instance, like the old fg3d singleton

export function destroyGalaxy() {
  if (!live) return;
  try { clearInterval(live.guard); } catch {}
  try { cancelAnimationFrame(live.raf); } catch {}
  try { live.ro && live.ro.disconnect(); } catch {}
  try { live.fg && live.fg._destructor && live.fg._destructor(); } catch {}
  live = null;
}

const LABEL_NEAREST = 40;    // nearest N nodes carry a visible name sprite
const NEBULA_MAX = 12;       // largest N communities get a cloud + constellation

// ── sprite factories (CanvasTexture — no extra deps) ────────────────────────
function textSprite(THREE, text, { color = "#c7d2e2", px = 28, shadow = true } = {}) {
  const font = `500 ${px}px 'Hanken Grotesk', sans-serif`;
  const pad = 10, meas = document.createElement("canvas").getContext("2d");
  meas.font = font;
  const w = Math.ceil(meas.measureText(text).width) + pad * 2, h = px + 18;
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  const c = cv.getContext("2d");
  c.font = font; c.textAlign = "center"; c.textBaseline = "middle";
  if (shadow) { c.shadowColor = "rgba(0,0,0,.6)"; c.shadowBlur = 7; }
  c.fillStyle = color;
  c.fillText(text, w / 2, h / 2);
  const tex = new THREE.CanvasTexture(cv);
  const mat = new THREE.SpriteMaterial({ map: tex, depthWrite: false, transparent: true });
  const sp = new THREE.Sprite(mat);
  const scale = px * 0.32;
  sp.scale.set(scale * (w / h), scale, 1);
  return sp;
}

function nebulaSprite(THREE, hue) {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 256;
  const c = cv.getContext("2d");
  const g = c.createRadialGradient(128, 128, 8, 128, 128, 128);
  g.addColorStop(0, `hsla(${hue} 70% 62% / .16)`);
  g.addColorStop(0.55, `hsla(${hue} 70% 55% / .07)`);
  g.addColorStop(1, `hsla(${hue} 70% 50% / 0)`);
  c.fillStyle = g; c.fillRect(0, 0, 256, 256);
  const mat = new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(cv),
    depthWrite: false, transparent: true });
  return new THREE.Sprite(mat);
}

// ── encodings ────────────────────────────────────────────────────────────────
function hueFor(n, colorBy) {
  const h = colorBy === "project" ? projectHue(n) : communityHue(n);
  return h;
}

// lightness 38%..62% by recency of the node's latest activity
function buildColors(nodes, edges, colorBy) {
  const act = {};
  for (const n of nodes) act[n.entity] = n.created_at || 0;
  for (const e of edges) {
    act[e.src] = Math.max(act[e.src] || 0, e.asserted_at || 0);
    act[e.dst] = Math.max(act[e.dst] || 0, e.asserted_at || 0);
  }
  const ts = Object.values(act).filter(Boolean);
  const lo = Math.min(...ts, Infinity), hi = Math.max(...ts, -Infinity);
  const span = hi > lo ? hi - lo : 1;
  const colors = {};
  for (const n of nodes) {
    const t = ((act[n.entity] || lo) - lo) / span;
    const l = Math.round(38 + t * 24);
    const h = hueFor(n, colorBy);
    colors[n.entity] = h == null
      ? (colorBy === "project" ? "#6b7280" : colorFor(n.etype))
      : `hsl(${h} 64% ${l}%)`;
  }
  return colors;
}

function legendEl(colorBy) {
  return el("div", { class: "graph-legend" },
    el("span", { class: "lg" }, el("span", { class: "sw",
      style: { background: "conic-gradient(from 0deg,#5b9dff,#3fd0c9,#b083f0,#e8b341,#5b9dff)" } }),
      colorBy === "project" ? "hue: project" : "hue: community"),
    el("span", { class: "lg" }, "bright = recent"),
    el("span", { class: "lg" }, "size = connections"));
}

// ── the galaxy ───────────────────────────────────────────────────────────────
export async function createGalaxy(host, data, opts = {}) {
  destroyGalaxy();
  const colorBy = opts.colorBy || "community";
  const onNodeClick = opts.onNodeClick || (() => {});
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const wrap = host;                         // caller supplies the .graph-wrap
  const mountPt = el("div", { style: { width: "100%", height: "100%" } });
  const loading = el("div", { class: "graph-hint galaxy-loading",
    style: { top: "50%", animation: "none" } }, "loading 3D engine…");
  wrap.appendChild(mountPt); wrap.appendChild(loading);

  let FG, THREE;
  try {
    const mod = await import("/ui/vendor/galaxy.bundle.js");
    FG = mod.default; THREE = mod.THREE;
    if (typeof FG !== "function" || !THREE) throw new Error("bundle exports missing");
  } catch (err) {
    loading.remove();
    return null;                             // caller falls back to table
  }
  if (!mountPt.isConnected) return null;     // route changed during import
  loading.remove();

  const deg = {}, facts = {};
  for (const e of data.edges || []) { deg[e.src] = (deg[e.src] || 0) + 1; deg[e.dst] = (deg[e.dst] || 0) + 1; }
  for (const n of data.nodes || []) facts[n.entity] = (n.facts || []).length;
  const colors = buildColors(data.nodes || [], data.edges || [], colorBy);
  const nodes = (data.nodes || []).map((n) => ({ id: n.entity, etype: n.etype,
    community: n.community, sources: n.sources, created_at: n.created_at }));
  const links = (data.edges || []).map((e) => ({ source: e.src, target: e.dst,
    derived: !!e.derived, asserted_at: e.asserted_at }));
  const r0 = wrap.getBoundingClientRect();

  const state = { query: "" };
  const nodeColor = (n) => {
    const base = colors[n.id] || "#6b7280";
    if (!state.query) return base;
    return n.id.toLowerCase().includes(state.query) ? "#ffffff" : "rgba(90,100,115,0.25)";
  };

  const fg = new FG(mountPt, {})
    .graphData({ nodes, links })
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeLabel((n) => n.id)
    .nodeColor(nodeColor)
    .nodeVal((n) => 1 + (deg[n.id] || 0) + (facts[n.id] || 0) * 0.6)
    .nodeThreeObjectExtend(true)
    .nodeThreeObject((n) => {
      const sp = textSprite(THREE, n.id, { color: "#c7d2e2", px: 26 });
      sp.center.set(0.5, -0.9);              // float above the star
      sp.visible = false;                    // proximity loop reveals it
      sp.__isLabel = true;
      n.__label = sp;
      return sp;
    })
    .linkColor((l) => (l.derived ? "rgba(150,170,200,0.20)" : "rgba(150,170,200,0.42)"))
    .linkDirectionalArrowLength(3)
    .width(r0.width).height(r0.height)
    .cooldownTime(reduce ? 0 : 12000)
    .onNodeClick((n) => onNodeClick(n.id));

  live = { fg, THREE, wrap, mountPt, state, colors, nodeColor };

  // camera: median-fit once spread, again on settle (stage-1 lesson: the
  // centroid drifts, zoomToFit tracks it)
  const fitCam = () => { try { fg.zoomToFit(400, 40); } catch {} };
  setTimeout(() => { if (mountPt.isConnected) fitCam(); }, 700);

  // ── nebulae + constellations (recomputed when the engine cools) ──────────
  const nebulae = new THREE.Group();
  fg.scene().add(nebulae);
  function paintNebulae() {
    nebulae.clear();
    const byComm = new Map();
    for (const n of fg.graphData().nodes) {
      if (n.community == null || n.x == null) continue;
      if (!byComm.has(n.community)) byComm.set(n.community, []);
      byComm.get(n.community).push(n);
    }
    const top = [...byComm.entries()].sort((a, b) => b[1].length - a[1].length)
      .slice(0, NEBULA_MAX).filter(([, m]) => m.length >= 3);
    for (const [cid, members] of top) {
      const cx = members.reduce((s, n) => s + n.x, 0) / members.length;
      const cy = members.reduce((s, n) => s + n.y, 0) / members.length;
      const cz = members.reduce((s, n) => s + n.z, 0) / members.length;
      const spread = Math.sqrt(members.reduce((s, n) =>
        s + (n.x - cx) ** 2 + (n.y - cy) ** 2 + (n.z - cz) ** 2, 0) / members.length);
      const hue = (Math.abs(Number(cid)) * 47) % 360;
      const cloud = nebulaSprite(THREE, hue);
      cloud.position.set(cx, cy, cz);
      cloud.scale.set(spread * 3.2, spread * 3.2, 1);
      nebulae.add(cloud);
      const anchor = members.slice().sort((a, b) =>
        (deg[b.id] || 0) - (deg[a.id] || 0))[0];
      const label = textSprite(THREE, anchor.id, { color: `hsl(${hue} 70% 72%)`, px: 34 });
      label.position.set(cx, cy + spread * 1.5, cz);
      label.material.opacity = 0.75;
      nebulae.add(label);
    }
  }
  fg.onEngineStop(() => { fitCam(); paintNebulae(); });
  setTimeout(() => { if (mountPt.isConnected) paintNebulae(); }, 1600);

  // ── proximity labels: nearest N visible, re-ranked continuously ──────────
  const camera = fg.camera();
  function labelLoop() {
    if (!live || live.fg !== fg) return;
    const ns = fg.graphData().nodes;
    const ranked = ns.filter((n) => n.x != null && n.__label)
      .map((n) => ({ n, d: camera.position.distanceTo({ x: n.x, y: n.y, z: n.z }) }))
      .sort((a, b) => a.d - b.d);
    ranked.forEach((r, i) => { r.n.__label.visible = i < LABEL_NEAREST; });
    live.raf = requestAnimationFrame(labelLoop);
  }
  live.raf = requestAnimationFrame(labelLoop);

  // ── lifecycle: resize + self-destruct on DOM removal ─────────────────────
  const ro = new ResizeObserver(() => {
    const b = wrap.getBoundingClientRect();
    if (b.width) { fg.width(b.width); fg.height(b.height); }
  });
  ro.observe(wrap);
  live.ro = ro;
  live.guard = setInterval(() => { if (!mountPt.isConnected) destroyGalaxy(); }, 1500);

  wrap.appendChild(legendEl(colorBy));
  wrap.appendChild(el("div", { class: "graph-hint" },
    "drag to orbit · scroll to zoom · click a star"));

  // ── public handle ─────────────────────────────────────────────────────────
  function flyTo(name) {
    const n = fg.graphData().nodes.find((x) => x.id === name);
    if (!n || n.x == null) return false;
    const d = Math.hypot(n.x, n.y, n.z) || 1;
    const dist = 55 + (fg.nodeVal()(n) || 1) * 2;
    const ratio = 1 + dist / d;
    fg.cameraPosition({ x: n.x * ratio, y: n.y * ratio, z: n.z * ratio },
      { x: n.x, y: n.y, z: n.z }, reduce ? 0 : 1100);
    return true;
  }
  function setQuery(q) {
    state.query = (q || "").trim().toLowerCase();
    fg.nodeColor(nodeColor);                 // re-evaluate accessor
  }
  function flyToBest(q) {
    const s = (q || "").trim().toLowerCase();
    if (!s) return null;
    const m = fg.graphData().nodes.filter((n) => n.id.toLowerCase().includes(s))
      .sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0))[0];
    if (m) flyTo(m.id);
    return m ? m.id : null;
  }
  return { flyTo, setQuery, flyToBest, destroy: destroyGalaxy };
}
```

- [ ] **Step 3: Syntax sanity**

Run: `node --check pseudolife_memory/web/static/js/galaxy.js` — expect silence
(node --check parses ESM by extension? if it complains about modules, use
`node --input-type=module --check < file` or skip; the devserver QA in Task 5
is the real gate).

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/web/static/js/galaxy.js pseudolife_memory/web/static/js/graphview.js
git commit -m "feat(console): galaxy.js — encodings, nebulae, proximity labels, search, fly-to"
```

---

### Task 4: `views/graph.js` rewrite — layout-A shell; retire the 2D map

**Files:**
- Rewrite: `pseudolife_memory/web/static/js/views/graph.js`
- Modify: `pseudolife_memory/web/static/js/graphview.js` (delete `ForceGraph`, `zoomControls`, `renderGalaxy`, `cleanupGalaxy`, `galaxyLegend`; keep `colorFor`, hue/color fns, `legend`, `tableView`, `fullscreenBtn`)
- Modify: `pseudolife_memory/web/static/js/views/wiki_page.js` (navigate hook + button label)
- Delete: `pseudolife_memory/web/static/vendor/3d-force-graph.bundle.js`
- Modify: `pseudolife_memory/web/static/css/styles.css` (review drawer + search input)

**Interfaces:**
- Consumes: `createGalaxy/destroyGalaxy` (Task 3 signatures), `openWikiPanel` (Stage 1), existing `reviewPanel`/`actOnFinding` action descriptors and all `/api/graph/*` mutation endpoints (unchanged).
- Produces: routes `#/graph`, `#/graph?entity=X`, `?scope=S`, `?view=table`; legacy `mode=`/`depth=` params ignored gracefully.

- [ ] **Step 1: wiki_page.js — navigation hook + label**

In `openWikiPanel`, replace the internal nav with a hookable one and relabel
the explore button (explore mode no longer exists):

```javascript
export function openWikiPanel(wrap, entityName, opts = {}) {
  const { onExplore, onNavigate } = opts;
  // …unchanged panel/host setup…
  const nav = (name) => (onNavigate ? onNavigate(name)
                                    : openWikiPanel(wrap, name, opts));
  // …unchanged fetch/render…
}
```

and in `render(...)` change the action button text from
`"Explore from here"` to `"Focus in galaxy"` (same `onExplore` callback slot —
the view now passes a fly-to).

- [ ] **Step 2: Rewrite `views/graph.js`**

```javascript
// views/graph.js — the Atlas surface (stage 2): a full-bleed 3D galaxy with a
// wiki panel and the review drawer. The galaxy IS the map; `table` is the
// data/accessibility fallback (and the automatic one when WebGL/bundle fails).
//   #/graph                → galaxy, whole scoped graph
//   #/graph?entity=X       → galaxy + X's wiki page open, camera on X
//   #/graph?view=table     → table mode
//   legacy: #/atlas, mode=, depth= — routed here, extra params ignored.
import { el, mount, fmtNum, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { tableView, fullscreenBtn } from "../graphview.js";
import { createGalaxy, destroyGalaxy } from "../galaxy.js";
import { openWikiPanel } from "./wiki_page.js";
import { reviewPanel } from "../atlas_review.js";
import { confirmDialog, openModal, closeModal, toast } from "../ui.js";

const ellipsisMid = (s, max = 22) =>
  s.length <= max ? s : `${s.slice(0, max - 9)}…${s.slice(-8)}`;

let state = { scope: "all", view: "galaxy", entity: "", review: false, q: "" };
let galaxy = null;

function parseHash() {
  const h = location.hash || "";
  const qi = h.indexOf("?");
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  return { entity: p.get("entity") || "",
           scope: p.get("scope") || state.scope || "all",
           view: p.get("view") === "table" ? "table" : state.view };
}

// Update the address bar without re-triggering the router (panel swaps and
// camera moves are in-surface navigation, not route changes).
function reflectHash() {
  const p = new URLSearchParams();
  if (state.entity) p.set("entity", state.entity);
  if (state.scope && state.scope !== "all") p.set("scope", state.scope);
  if (state.view === "table") p.set("view", "table");
  const qs = p.toString();
  history.replaceState(null, "", location.pathname + "#/graph" + (qs ? "?" + qs : ""));
}

export async function renderGraph(root, ctx) {
  const h = parseHash();
  state.entity = h.entity; state.scope = h.scope; state.view = h.view;

  const toolbar = el("div", { class: "toolbar" });
  const reviewHost = el("div", { class: "review-drawer", style: { display: "none" } });
  const host = el("div", {});
  mount(root, toolbar, reviewHost, host);

  // ── data + paint ──────────────────────────────────────────────────────────
  async function load() {
    destroyGalaxy(); galaxy = null;
    mount(host, loadingBlock("Charting the galaxy…"));
    try {
      const data = await api.get("/api/graph", { scope: state.scope });
      host._data = data;
      await paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  async function paint(data) {
    destroyGalaxy(); galaxy = null;
    const nodes = (data && data.nodes) || [];
    if (!data || data.found === false || !nodes.length) {
      mount(host, emptyBlock("No graph in scope",
        state.scope === "all" ? "The graph is empty." : `No attributed entities in “${state.scope}”.`));
      return;
    }
    const colorBy = state.scope === "all" ? "project" : "community";
    const banner = data.truncated
      ? el("div", { class: "chip warn", style: { display: "inline-flex", marginBottom: "10px" } },
          `showing the ${fmtNum(nodes.length)} most-connected of ${fmtNum(data.total_nodes ?? nodes.length)} entities`
          + (state.scope === "all" ? " — pick a project to map it in full" : ""))
      : null;
    const viewHost = el("div", {});
    mount(host, banner, viewHost);

    if (state.view === "table") { mount(viewHost, tableView(data)); reflectHash(); return; }

    const wrap = el("div", { class: "graph-wrap galaxy" });
    mount(viewHost, wrap);
    galaxy = await createGalaxy(wrap, data, { colorBy, onNodeClick: (id) => openPage(wrap, id) });
    if (!galaxy) {                       // bundle/WebGL failure → live fallback
      state.view = "table";
      toast("3D engine unavailable — showing the table view", "bad");
      mount(viewHost, tableView(data));
      reflectHash();
      return;
    }
    wrap.appendChild(fullscreenBtn(wrap));
    if (state.entity) {                  // deep link: open page + fly once laid out
      openPage(wrap, state.entity, { fly: "late" });
    }
    reflectHash();
  }

  function openPage(wrap, id, { fly = "now" } = {}) {
    state.entity = id;
    reflectHash();
    openWikiPanel(wrap, id, {
      onExplore: (name) => { galaxy && galaxy.flyTo(name); },
      onNavigate: (name) => { galaxy && galaxy.flyTo(name); openPage(wrap, name); },
    });
    if (!galaxy) return;
    if (fly === "late") setTimeout(() => galaxy && galaxy.flyTo(id), 1900);
    else galaxy.flyTo(id);
  }

  // ── review drawer (same findings API + actions as before) ────────────────
  async function loadReview() {
    mount(reviewHost, loadingBlock("Scanning the graph…"));
    try {
      const rd = await api.get("/api/graph/review", { scope: state.scope });
      mount(reviewHost, reviewPanel(rd, (f) => actOnFinding(f)));
    } catch (err) { mount(reviewHost, errorBlock(err)); }
  }

  async function refreshAfterMutation() {
    if (state.review) await loadReview();
    await load();
  }

  async function postAll(calls, okMsg) {
    let ok = 0;
    for (const c of calls) {
      try { await api.post(c.path, c.body); ok += 1; }
      catch (err) { toast(`${c.path.split("/").pop()} failed — ${err.message}`, "bad"); }
    }
    if (ok) { toast(`${okMsg} (${ok})`, "ok"); await refreshAfterMutation(); }
  }

  async function actOnFinding(d) {
    /* KEEP VERBATIM from the current graph.js — the whole dispatch block
       (merge-named / merge-entity / junk-entity / reject-entity / accept-link /
       reject-link / dismiss-duplicate / bless / prune / delete-names / assign),
       including every confirmDialog/openModal flow. It only depends on
       postAll/refreshAfterMutation/openModal/closeModal/confirmDialog/
       ellipsisMid, all of which exist here unchanged. */
  }

  // ── toolbar ───────────────────────────────────────────────────────────────
  let projects = [];
  try { projects = (await api.get("/api/graph/projects")).projects || []; } catch { /* non-fatal */ }
  const scopeOpts = [{ value: "all", label: "all projects" }]
    .concat(projects.map((p) => ({ value: p.source, label: `${p.source} (${p.entities})` })));
  const switcher = facetBar(scopeOpts, state.scope,
    (v) => { state.scope = v; state.entity = ""; load(); if (state.review) loadReview(); });

  const search = el("input", { type: "search", class: "galaxy-search",
    placeholder: "find a star…", name: "q", "aria-label": "search entities",
    oninput: (e) => { state.q = e.target.value; galaxy && galaxy.setQuery(state.q); },
    onkeydown: (e) => {
      if (e.key === "Enter" && galaxy) {
        const hitWrap = host.querySelector(".graph-wrap");
        const hit = galaxy.flyToBest(state.q);
        if (hit && hitWrap) openPage(hitWrap, hit);
      }
      if (e.key === "Escape") { e.target.value = ""; state.q = ""; galaxy && galaxy.setQuery(""); }
    } });

  const reviewBtn = el("button", { class: "facet" + (state.review ? " on" : ""),
    onclick: () => { state.review = !state.review; reviewBtn.classList.toggle("on", state.review);
      reviewHost.style.display = state.review ? "" : "none"; if (state.review) loadReview(); } },
    "Review");
  const viewToggle = facetBar(
    [{ value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });

  mount(toolbar, el("span", { class: "eyebrow" }, "scope"), switcher, search,
    el("span", { class: "grow" }), reviewBtn, viewToggle);
  reviewHost.style.display = state.review ? "" : "none";
  if (state.review) loadReview();
  await load();
}
```

**Note on `actOnFinding`:** copy the entire dispatch block verbatim from the
pre-rewrite `views/graph.js` (git show `HEAD:...views/graph.js`, lines 184-275).
Do not retype it.

- [ ] **Step 3: Slim `graphview.js`**

Delete: `ForceGraph` class, `zoomControls`, `renderGalaxy`, `cleanupGalaxy`,
`galaxyLegend`, the `fg3d` singleton, and the now-unused `errorBlock`/`badge`…
imports if nothing else in the file uses them. Keep: `ETYPE_COLOR`, `colorFor`,
`communityHue/projectHue/communityColor/projectColor` (Task 3), `legend`,
`tableView`, `fullscreenBtn`, `enterMaximized`/`toggleFullscreen`, `nodeFill`
only if `tableView`/`legend` still need it (they don't — delete it too).
Then delete the old bundle:

```bash
git rm pseudolife_memory/web/static/vendor/3d-force-graph.bundle.js
```

Grep-check nothing references the old names:
`grep -rn "renderGalaxy\|cleanupGalaxy\|new ForceGraph\|zoomControls\|3d-force-graph.bundle" pseudolife_memory/web/static/js/` → 0 hits.

- [ ] **Step 4: CSS**

Append to `styles.css`:

```css
/* ── Atlas stage 2: galaxy shell ────────────────────────────────────────── */
.galaxy-search { max-width: 220px; }
.review-drawer { position: fixed; left: 0; top: 0; bottom: 0; width: min(440px, 92vw);
  z-index: 30; overflow-y: auto; padding: 16px;
  background: var(--bg-2); border-right: 1px solid var(--line-2);
  box-shadow: var(--shadow-pop); animation: slidein .2s var(--ease); }
```

(If the existing `reviewPanel` styles assume in-flow placement, verify in the
devserver and adjust padding only — do not restyle the findings themselves.)

- [ ] **Step 5: Commit**

```bash
git add -A pseudolife_memory/web/static
git commit -m "feat(console): galaxy-first Atlas shell — 2D map and explore mode retired"
```

---

### Task 5: QA, full suite, CHANGELOG, review pass

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Devserver QA (browser, fixtures)**

`.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8771`, then:
galaxy loads offline (new bundle path); stars sized/colored, brightness varies;
nebulae + constellation labels appear after settle; nearest-N node labels
visible and re-rank while orbiting; search dims non-matches, Enter flies + opens
the page; click a star → wiki panel + fly-to; wikilink hop flies the camera;
"Focus in galaxy" re-centers; scope switch reloads; Review drawer opens with
findings and an action round-trips; table mode renders; `#/graph?entity=X` deep
link opens page + flies after layout; legacy `#/graph?mode=explore&entity=X`
still lands with the page open; both themes; `prefers-reduced-motion` (emulate
via devtools) skips fly animation; route-switch leak check (galaxy guard fires,
`performance.memory` steady over 5 switches); bundle-failure fallback: temporarily
rename `galaxy.bundle.js` on disk → table + toast, rename back.

- [ ] **Step 2: Full suite**

`HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q` with bench PG up.
Expected: all pass (Python surface unchanged except Task 2).

- [ ] **Step 3: CHANGELOG** (under `[Unreleased]`)

```markdown
### Added (2026-07-15 — Atlas stage 2: the galaxy is the map)
- Console Graph tab rebuilt around the 3D galaxy: memory-state encodings
  (size = connections + facts, hue = project/community, brightness = recency),
  community nebulae with constellation labels, proximity-faded star labels,
  search with dim/highlight + fly-to, wiki-page click-through (wikilinks fly
  the camera). Review queue lives in a drawer; table mode is the fallback
  (automatic when WebGL/the 3D bundle is unavailable).
- `/api/graph` whole-graph payload carries `created_at`/`asserted_at`
  (additive; feeds the recency encoding now, the time scrubber in stage 3).
- Vendored `galaxy.bundle.js` (3d-force-graph 1.73.6 + three, single instance,
  esbuild, MIT/permissive-only license audit recorded in vendor/README.md)
  replaces `3d-force-graph.bundle.js`.

### Removed (2026-07-15 — Atlas stage 2)
- The 2D canvas force-graph map and the Overview/Explore mode split (legacy
  `mode=`/`depth=` deep links degrade gracefully).
```

- [ ] **Step 4: Review pass, commit**

`/code-review` medium (mandated: renderer/perf class). Address findings.

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Atlas stage 2 galaxy"
```

---

## Self-review notes

- **Spec §C coverage:** encodings ✔ (Task 3 buildColors/nodeVal), nebulae+labels
  ✔ (paintNebulae, cap 12), proximity labels ✔ (labelLoop, cap 40), search+fly-to
  ✔ (setQuery/flyToBest/flyTo), reduced-motion ✔, lifecycle guard ✔, truncation
  banner ✔, wiki integration ✔ (openPage/onNavigate). Scrubber/isolate/pulses
  deferred to Stage 3 per spec stages.
- **§E retirements:** ForceGraph/zoomControls/mode-split deleted ✔, tableView
  kept ✔, fallback-on-bundle-failure ✔, legacy deep links ✔.
- **License gate** is a STOP condition, not advisory (Task 1 Step 2).
- **Type consistency:** `createGalaxy(host, data, {colorBy, onNodeClick})` and
  handle `{flyTo, setQuery, flyToBest, destroy}` used identically in Tasks 3/4;
  `openWikiPanel(wrap, name, {onExplore, onNavigate})` matches the Task 4 Step 1
  signature change.
