# Cortex Console v2 — Phase 0 (Restore & Polish) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Revive the console's "spectral accent per memory layer" design language (a silent one-line bug disables it), then land the surrounding polish — grouped nav, continuum legibility, a complete graph legend, a single schema source, and a freshened v1 doc.

**Architecture:** The console is a daemon-served, no-build, vanilla-ESM SPA. Phase 0 touches only the frontend (`pseudolife_memory/web/static/`) plus one devserver health closure and one doc. No new endpoints, no new data, no dependencies.

**Tech Stack:** Vanilla ES modules + hand-rolled CSS (no framework, no build). Python 3.11 ASGI daemon. Tests: pytest (`tests/test_web.py`) for the one server-side change; **browser-driven QA via the chrome-devtools MCP against the fixture devserver** for all frontend changes (the repo has no JS test runner — browser QA is the established convention).

## Global Constraints

- **No build step, no CDN, fully offline.** Vanilla ES modules only; do not add npm/Vite/bundlers or remote `<script>`/`<link>` tags.
- **Run Python under `.venv`.** The fixture devserver and `tests/test_web.py` now transitively import torch (via `AppConfig` → `preset_bands` → `cms`); use `.venv/Scripts/python.exe`. System python has no torch.
- **Frontend QA is browser-driven.** Verify JS/CSS changes by reloading the devserver page in the chrome-devtools MCP and asserting via `evaluate_script` + screenshot. There is no JS unit-test harness; do not invent one.
- **Devserver:** `.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770` → `http://127.0.0.1:8770/ui/`. Serve assets no-store (already configured), so a reload picks up edits.
- **Commit style:** conventional commits; end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Branch:** `feat/cortex-console-v2` (already created).

---

### Task 1: Fix `el()` custom-property handling (revive the spectral system)

`el()` sets inline styles with `Object.assign(node.style, v)`, which silently drops CSS custom properties (`--tone`, `--dot`, `--bh`). Every spectral accent in the app falls back to the single route `--accent`. This is the highest-value change in the whole project.

**Files:**
- Modify: `pseudolife_memory/web/static/js/util.js:9`

**Interfaces:**
- Consumes: nothing.
- Produces: `el(tag, props, ...children)` — unchanged signature; `props.style` objects now apply `--*` keys correctly via `setProperty`.

- [ ] **Step 1: Confirm the failing behavior in the browser**

Ensure the devserver is running, then in the chrome-devtools MCP navigate to `http://127.0.0.1:8770/ui/#/observatory` and run via `evaluate_script`:

```js
() => ({
  stat_tone: getComputedStyle(document.querySelector('.stat')).getPropertyValue('--tone'),
  band_bh: getComputedStyle(document.querySelector('.band-row')).getPropertyValue('--bh'),
  nav_dot: getComputedStyle(document.querySelector('.nav-item')).getPropertyValue('--dot'),
})
```

Expected (bug present): all three values are `""` (empty).

- [ ] **Step 2: Apply the fix**

In `pseudolife_memory/web/static/js/util.js`, replace the style branch (line 9):

```js
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
```

with:

```js
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv == null) continue;
        if (sk.startsWith("--")) node.style.setProperty(sk, String(sv));
        else node.style[sk] = sv;
      }
    }
```

- [ ] **Step 3: Verify the fix in the browser**

Reload `http://127.0.0.1:8770/ui/#/observatory` (the page serves no-store) and re-run the Step 1 `evaluate_script`.
Expected: `stat_tone`, `band_bh`, and `nav_dot` are all non-empty (e.g. an `hsl(...)` or `rgb(...)`/`var(...)` value), and nav dots/stat-card edges/continuum fills now show distinct per-layer colors.

- [ ] **Step 4: Visual confirmation**

`take_screenshot` of `#/observatory` in both themes (toggle with `localStorage.setItem('pl_theme','light'); location.reload()`). Expected: stat cards have colored left edges; MIRAS continuum band-fills are visibly colored in **both** dark and light.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/static/js/util.js
git commit -m "$(printf 'fix(web): apply CSS custom properties in el() via setProperty\n\nObject.assign(node.style, {"--x":v}) silently drops custom properties, so\n--tone/--dot/--bh were unset everywhere and the spectral-accent system never\nrendered. Set --* keys via style.setProperty.\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: Grouped navigation (IA-C, existing tabs)

Restructure the flat sidebar into four labeled groups. Only the existing 8 tabs; Recall and Insight slot in during Phase 1.

**Files:**
- Modify: `pseudolife_memory/web/static/js/app.js:14-23` (ROUTES) and `:65-77` (`buildNav`)
- Modify: `pseudolife_memory/web/static/css/styles.css` (add `.nav-group-label`, near the `.nav` block ~line 161)

**Interfaces:**
- Consumes: `el()` from Task 1.
- Produces: each `ROUTES[i]` gains a `group: string` field. Number-key shortcuts (keys 1-8 → `ROUTES[n-1]`) and `routeId()` are unchanged (group labels are not `.nav-item`s and are not in `ROUTES`).

- [ ] **Step 1: Add `group` to each route**

In `app.js`, replace the `ROUTES` array (lines 14-23) with:

```js
const ROUTES = [
  { id: "observatory", label: "Observatory", group: "Overview", accent: "var(--c-assoc)", view: renderObservatory, countKey: null },
  { id: "cortex", label: "Cortex", group: "Memory", accent: "var(--c-cortex)", view: renderCortex, countKey: "facts" },
  { id: "world", label: "World", group: "Memory", accent: "var(--c-world)", view: renderWorld, countKey: "world" },
  { id: "lessons", label: "Lessons", group: "Memory", accent: "var(--c-lessons)", view: renderLessons, countKey: "lessons" },
  { id: "stream", label: "Stream", group: "Memory", accent: "var(--c-assoc)", view: renderStream, countKey: "entries" },
  { id: "graph", label: "Graph", group: "Structure", accent: "var(--c-graph)", view: renderGraph, countKey: null },
  { id: "episodes", label: "Episodes", group: "Operations", accent: "var(--c-episode)", view: renderEpisodes, countKey: "episodes" },
  { id: "console", label: "Console", group: "Operations", accent: "var(--c-assoc)", view: renderConsole, countKey: null },
];
```

- [ ] **Step 2: Emit group labels in `buildNav`**

In `app.js`, replace `buildNav` (lines 65-77) with:

```js
function buildNav() {
  clear(navEl);
  let lastGroup = null;
  for (const r of ROUTES) {
    if (r.group && r.group !== lastGroup) {
      navEl.appendChild(el("div", { class: "nav-group-label" }, r.group));
      lastGroup = r.group;
    }
    const item = el("button", {
      class: "nav-item", dataset: { route: r.id }, style: { "--dot": r.accent },
      onclick: () => { location.hash = "#/" + r.id; closeMobileNav(); },
    },
      el("span", { class: "nav-dot" }),
      el("span", {}, r.label),
      r.countKey ? el("span", { class: "count", dataset: { count: r.countKey } }, "") : null);
    navEl.appendChild(item);
  }
}
```

- [ ] **Step 3: Style the group label**

In `styles.css`, add after the `.nav-item .count` rule (~line 172):

```css
.nav-group-label { font-family:var(--font-mono); font-size:.62rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--ink-3); padding:14px 11px 5px; }
.nav-group-label:first-child { padding-top:4px; }
```

- [ ] **Step 4: Verify in the browser**

Reload `http://127.0.0.1:8770/ui/`. Run `evaluate_script`:

```js
() => [...document.querySelectorAll('.nav-group-label')].map(e => e.textContent)
```

Expected: `["Overview","Memory","Structure","Operations"]`. `take_screenshot` — the sidebar shows four labeled groups; the active item still highlights; pressing `2` still routes to Cortex.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/static/js/app.js pseudolife_memory/web/static/css/styles.css
git commit -m "$(printf 'feat(web): grouped sidebar navigation (Overview/Memory/Structure/Operations)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: MIRAS continuum legibility (min-visible floor)

With Task 1 fixed, band-fills are colored. Add a small floor so a non-empty-but-tiny band still reads, and confirm light-theme contrast.

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/observatory.js:69` (the `pct` computation in `continuumPanel`)

**Interfaces:**
- Consumes: `el()` from Task 1; `continuumPanel(stats)` internal.
- Produces: no API change.

- [ ] **Step 1: Add the min-visible floor**

In `observatory.js`, in `continuumPanel`, replace:

```js
          const pct = Math.min(100, (b.size / cap) * 100);
```

with:

```js
          const raw = (b.size / cap) * 100;
          const pct = b.size > 0 ? Math.max(2.5, Math.min(100, raw)) : 0;
```

- [ ] **Step 2: Verify in the browser**

Reload `#/observatory`. `evaluate_script`:

```js
() => [...document.querySelectorAll('.band-fill')].map(f => ({
  w: f.style.width,
  bg: getComputedStyle(f).backgroundImage.slice(0,40)
}))
```

Expected: every band with `size > 0` has a non-zero `width` and a gradient `background-image` (not `none`).

- [ ] **Step 3: Light-theme contrast check**

`evaluate_script`: `() => { localStorage.setItem('pl_theme','light'); location.reload(); }`, then `take_screenshot` of `#/observatory`. Expected: continuum fills are clearly visible against the cream track in light theme. Reset to dark afterward.

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/web/static/js/views/observatory.js
git commit -m "$(printf 'fix(web): give non-empty MIRAS bands a min-visible fill floor\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: Complete the graph legend + reposition the hint

The legend only explains "entity (blue)" while nodes use 5+ etype colors; the bottom hint pill collides with node labels.

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/graph.js` — `legend()` (lines 108-113) and `paint()` (the `wrap.appendChild(legend())` call ~line 72)
- Modify: `pseudolife_memory/web/static/css/styles.css` — `.graph-hint` (lines 507-509)

**Interfaces:**
- Consumes: `colorFor(etype)` (exists in graph.js); `el()` from Task 1.
- Produces: `legend(etypes: string[])` — now takes the list of etypes present.

- [ ] **Step 1: Make the legend list present etypes**

In `graph.js`, replace `legend()` (lines 108-113) with:

```js
function legend(etypes) {
  const list = etypes && etypes.length ? etypes : ["entity"];
  return el("div", { class: "graph-legend" },
    ...list.map((et) => el("span", { class: "lg" },
      el("span", { class: "sw", style: { background: colorFor(et) } }), et)),
    el("span", { class: "lg" }, el("span", { class: "ln" }), "explicit"),
    el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
}
```

- [ ] **Step 2: Pass the present etypes from `paint()`**

In `graph.js` `paint()`, replace:

```js
    wrap.appendChild(legend());
```

with:

```js
    const etypes = [...new Set((data.nodes || []).map((n) => n.etype).filter(Boolean))];
    wrap.appendChild(legend(etypes));
```

- [ ] **Step 3: Reposition + auto-fade the hint**

In `styles.css`, replace the `.graph-hint` rule (lines 507-509):

```css
.graph-hint { position:absolute; left:50%; transform:translateX(-50%); bottom:12px; font-size:.72rem; color:var(--ink-3);
  font-family:var(--font-mono); background:color-mix(in srgb,var(--bg-1) 70%,transparent);
  padding:4px 10px; border-radius:99px; border:1px solid var(--line); pointer-events:none; white-space:nowrap; }
```

with (move to top-center, fade out after 6s so it never overlaps lower node labels):

```css
.graph-hint { position:absolute; left:50%; transform:translateX(-50%); top:12px; bottom:auto; font-size:.72rem; color:var(--ink-3);
  font-family:var(--font-mono); background:color-mix(in srgb,var(--bg-1) 70%,transparent);
  padding:4px 10px; border-radius:99px; border:1px solid var(--line); pointer-events:none; white-space:nowrap;
  animation:hintfade .4s var(--ease) 6s forwards; }
@keyframes hintfade { to { opacity:0; visibility:hidden; } }
```

- [ ] **Step 4: Verify in the browser**

Reload `http://127.0.0.1:8770/ui/#/graph?entity=pseudolife-mcp&depth=2`. `evaluate_script`:

```js
() => ({
  swatches: [...document.querySelectorAll('.graph-legend .lg .sw')].length,
  hintTop: getComputedStyle(document.querySelector('.graph-hint')).top,
})
```

Expected: `swatches` ≥ 2 (one per present etype), `hintTop` ≈ `12px`. `take_screenshot`: legend lists each node-type color; hint sits top-center and is clear of bottom labels.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/static/js/views/graph.js pseudolife_memory/web/static/css/styles.css
git commit -m "$(printf 'feat(web): complete graph etype legend; move hint top-center with auto-fade\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: Single schema source in the devserver health

`devserver._health` hardcodes `schema: 11`; `routes._health` reports `SCHEMA_META_VERSION` (13). Make the devserver report the real version too.

**Files:**
- Modify: `pseudolife_memory/web/devserver.py:34-37` (the `_health` closure in `build_dev_app`)
- Test: `tests/test_web.py` (add one test using the existing `_call` helper)

**Interfaces:**
- Consumes: `build_dev_app(token=None)` (exists); `_call(app, method, path)` (exists in test_web.py); `SCHEMA_META_VERSION` from `pseudolife_memory.storage.schema`.
- Produces: devserver `/health` reports the real schema version.

- [ ] **Step 1: Write the failing test**

In `tests/test_web.py`, add (after `test_asgi_health_open`):

```python
def test_devserver_health_reports_real_schema():
    import json
    from pseudolife_memory.web.devserver import build_dev_app
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION
    st, body = _call(build_dev_app(), "GET", "/health")
    assert st == 200
    assert json.loads(body)["schema"] == SCHEMA_META_VERSION
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py::test_devserver_health_reports_real_schema -v`
Expected: FAIL — `assert 11 == 13` (hardcoded 11 ≠ real version).

- [ ] **Step 3: Fix the devserver health closure**

In `pseudolife_memory/web/devserver.py`, replace the `_health` closure inside `build_dev_app` (lines 34-37):

```python
    def _health() -> dict:
        return {"status": "ok", "schema": 11, "storage": "postgres (fixture)",
                "auth": token is not None, "persist_errors": 0,
                "mode": "devserver"}
```

with:

```python
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    def _health() -> dict:
        return {"status": "ok", "schema": SCHEMA_META_VERSION,
                "storage": "postgres (fixture)", "auth": token is not None,
                "persist_errors": 0, "mode": "devserver"}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_web.py::test_devserver_health_reports_real_schema -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/devserver.py tests/test_web.py
git commit -m "$(printf 'fix(web): devserver /health reports SCHEMA_META_VERSION (was hardcoded 11)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 6: Freshen the v1 design doc header

The v1 spec still says `Status: in progress` / `Branch: feat/web-ui`.

**Files:**
- Modify: `docs/specs/2026-06-23-web-frontend-design.md:3-4`

**Interfaces:** none.

- [ ] **Step 1: Update the header**

Replace lines 3-4:

```markdown
**Status:** in progress (2026-06-23)
**Branch:** `feat/web-ui`
```

with:

```markdown
**Status:** shipped v1 (merged to master). Superseded by `docs/superpowers/specs/2026-06-25-cortex-console-v2-design.md`.
**Branch:** merged (was `feat/web-ui`)
```

- [ ] **Step 2: Commit**

```bash
git add docs/specs/2026-06-23-web-frontend-design.md
git commit -m "$(printf 'docs(web): mark v1 frontend design shipped/superseded\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

## Phase 0 exit check

After all six tasks:
- [ ] `.venv/Scripts/python.exe -m pytest tests/test_web.py -q` passes (existing 24 + the new schema test).
- [ ] Browser pass at `http://127.0.0.1:8770/ui/`: zero console errors (`list_console_messages`), spectral colors render across Observatory/Cortex/nav, continuum fills visible in dark **and** light, grouped nav present, graph legend complete + hint clear of labels.
- [ ] Phase 0 is independently deployable (frontend + devserver only; no schema/endpoint change) via the established daemon-rebuild + `backup.ps1`-first procedure when the user is present.
