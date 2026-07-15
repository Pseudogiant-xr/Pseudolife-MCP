# Atlas Stage 3 — Time Scrubber, Contextual Review, Isolate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The galaxy gains a time scrubber replaying the bank's growth, review findings surface contextually (pulsing flagged stars + actionable banners on wiki pages), and the wiki panel gets the isolate toggle that replaces the old explore mode.

**Architecture:** Frontend-only — Stage 2 already ships `created_at`/`asserted_at` in the graph payload and `flags` in the wiki payload. `galaxy.js` gains `setFlagged`, `isolate/clearIsolate`, and a self-contained scrubber (visibility-only over the fixed layout — no re-simulation). The shell fetches review findings lazily to light up flagged stars; wiki flag banners reuse the exact `actOnFinding` descriptors, so every mutation keeps its confirm gate.

**Tech Stack:** vanilla-JS ES modules; no backend or schema changes; pytest suite runs unchanged as regression.

**Spec:** `docs/superpowers/specs/2026-07-15-atlas-wiki-galaxy-design.md` (§C scrubber/isolate, §D review integration).

## Global Constraints

- No new endpoints; mutations go through the existing confirm-gated `actOnFinding` flow only.
- Scrubber changes **visibility only** — never re-heats the simulation (stable positions is the design's core decision).
- `prefers-reduced-motion`: no pulse animation (static warn-tint instead), no scrubber auto-play.
- Full suite (`HF_HUB_OFFLINE=1`, bench PG up) green before final commit; CHANGELOG entry required.
- Frontend QA against `devserver.py` fixtures (which carry deterministic timestamps since Stage 2).

---

### Task 1: `galaxy.js` — scrubber + flagged pulse + isolate

**Files:**
- Modify: `pseudolife_memory/web/static/js/galaxy.js`
- Modify: `pseudolife_memory/web/static/css/styles.css` (scrubber bar)

**Interfaces:**
- Produces (handle additions): `setFlagged(names: Set<string>)`, `isolate(name, depth=2) -> boolean`, `clearIsolate()`. Scrubber is internal to the galaxy (no shell API).
- Node/link visibility, color, and label logic all honor three state layers with priority: search query > isolate dim > flagged tint (reduced-motion only) > base recency color.

- [ ] **Step 1: state + accessors**

Extend `const state = { query: "" };` to
`const state = { query: "", dim: null, flagged: new Set(), tCut: null };`
and replace the `nodeColor` accessor with:

```javascript
  const WARN = "#e8b341";
  const nodeColor = (n) => {
    if (state.query) {
      return n.id.toLowerCase().includes(state.query) ? "#ffffff" : "rgba(90,100,115,0.25)";
    }
    if (state.dim && !state.dim.has(n.id)) return "rgba(90,100,115,0.16)";
    if (reduce && state.flagged.has(n.id)) return WARN;   // static tint instead of pulse
    return colors[n.id] || "#6b7280";
  };
```

Add visibility accessors right after `.nodeColor(nodeColor)` in the builder chain:

```javascript
    .nodeVisibility((n) => state.tCut == null || (n.created_at || 0) <= state.tCut)
    .linkVisibility((l) => {
      if (state.tCut == null) return true;
      const sc = (l.source && l.source.created_at) || 0;
      const tc = (l.target && l.target.created_at) || 0;
      return (l.asserted_at || 0) <= state.tCut && sc <= state.tCut && tc <= state.tCut;
    })
```

and change `linkColor` to dim outside an isolate set:

```javascript
    .linkColor((l) => {
      const dimmed = state.dim &&
        !(state.dim.has(l.source.id ?? l.source) && state.dim.has(l.target.id ?? l.target));
      if (dimmed) return "rgba(90,100,115,0.06)";
      return l.derived ? "rgba(150,170,200,0.20)" : "rgba(150,170,200,0.42)";
    })
```

(`l.source` is a string before layout binds objects — the `?? l.source` fallback covers the first frames.)

- [ ] **Step 2: pulse + label interaction in the existing rAF loop**

In `labelLoop`, after the ranked visibility assignment, add the pulse and honor
scrubber/isolate on labels:

```javascript
    // labels: hide when the star is hidden by the scrubber or dimmed by isolate
    for (const r of ranked) {
      const hidden = (state.tCut != null && (r.n.created_at || 0) > state.tCut)
                  || (state.dim && !state.dim.has(r.n.id));
      if (hidden) r.n.__label.visible = false;
    }
    // flagged stars pulse (skip under reduced motion — they get a tint instead)
    if (!reduce && state.flagged.size) {
      const s = 1 + 0.22 * Math.sin(performance.now() / 300);
      for (const n of ns) {
        if (!n.__threeObj) continue;
        if (state.flagged.has(n.id)) n.__threeObj.scale.setScalar(s);
        else if (n.__threeObj.scale.x !== 1) n.__threeObj.scale.setScalar(1);
      }
    }
```

- [ ] **Step 3: scrubber UI (internal)**

Append after the legend/hint block, before the public handle:

```javascript
  // ── time scrubber: replay the bank's growth (visibility only — the layout
  // is computed once on the full graph and never re-simulated) ──────────────
  const times = [
    ...nodes.map((n) => n.created_at || 0),
    ...links.map((l) => l.asserted_at || 0),
  ].filter(Boolean);
  if (times.length >= 2) {
    const t0 = Math.min(...times), t1 = Math.max(...times);
    if (t1 > t0) {
      const fmtD = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
      const label = el("span", { class: "scrub-date mono" }, "now");
      const slider = el("input", { type: "range", min: "0", max: "1000", value: "1000",
        name: "scrub", "aria-label": "time scrubber" });
      let playing = null;
      const apply = (v) => {
        state.tCut = v >= 1000 ? null : t0 + (t1 - t0) * (v / 1000);
        label.textContent = state.tCut == null ? "now" : fmtD(state.tCut);
        fg.nodeVisibility(fg.nodeVisibility());    // re-evaluate accessors
        fg.linkVisibility(fg.linkVisibility());
      };
      slider.oninput = () => { stopPlay(); apply(+slider.value); };
      function stopPlay() {
        if (playing) { clearInterval(playing); playing = null; playBtn.textContent = "▶"; }
      }
      const playBtn = el("button", { class: "scrub-play", title: "replay growth",
        "aria-label": "replay growth", onclick: () => {
          if (reduce) return;                      // no auto-animation
          if (playing) { stopPlay(); return; }
          let v = 0;
          playBtn.textContent = "❚❚";
          playing = setInterval(() => {
            v += 12;
            if (v >= 1000) { v = 1000; stopPlay(); }
            slider.value = String(v);
            apply(v);
          }, 100);
        } }, "▶");
      wrap.appendChild(el("div", { class: "scrub-bar" }, playBtn, slider, label));
    }
  }
```

- [ ] **Step 4: handle additions** (replace the handle block)

```javascript
  function setFlagged(names) {
    state.flagged = names instanceof Set ? names : new Set(names || []);
    fg.nodeColor(nodeColor);                 // reduced-motion tint path
  }
  function isolate(name, depth = 2) {
    const adj = new Map();
    for (const l of links) {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      (adj.get(s) || adj.set(s, []).get(s)).push(t);
      (adj.get(t) || adj.set(t, []).get(t)).push(s);
    }
    if (!adj.has(name) && !nodes.some((n) => n.id === name)) return false;
    const keep = new Set([name]);
    let frontier = [name];
    for (let d = 0; d < depth; d++) {
      const next = [];
      for (const cur of frontier) for (const nb of adj.get(cur) || []) {
        if (!keep.has(nb)) { keep.add(nb); next.push(nb); }
      }
      frontier = next;
    }
    state.dim = keep;
    fg.nodeColor(nodeColor); fg.linkColor(fg.linkColor());
    return true;
  }
  function clearIsolate() {
    state.dim = null;
    fg.nodeColor(nodeColor); fg.linkColor(fg.linkColor());
  }
  const handle = { flyTo, setQuery, flyToBest, setFlagged, isolate, clearIsolate,
                   destroy: destroyGalaxy };
  wrap.__galaxy = handle;   // debug/QA handle (parity with the old canvas.__fg)
  return handle;
```

`setQuery` also re-evaluates link/label state implicitly through the accessors —
no change needed there.

- [ ] **Step 5: CSS** (append to styles.css)

```css
/* ── Atlas stage 3: time scrubber ───────────────────────────────────────── */
.scrub-bar { position: absolute; left: 50%; transform: translateX(-50%); bottom: 12px;
  display: flex; align-items: center; gap: 10px; padding: 6px 12px; z-index: 6;
  background: var(--bg-2); border: 1px solid var(--line-2); border-radius: 999px;
  box-shadow: var(--shadow-pop); }
.scrub-bar input[type="range"] { width: min(320px, 40vw); }
.scrub-play { background: none; border: none; cursor: pointer; color: var(--ink-3);
  font-size: .9rem; }
.scrub-date { font-size: .74rem; color: var(--ink-3); min-width: 74px; text-align: right; }
```

Move the existing `.graph-hint` out of the way if they collide (QA will show it;
the hint sits bottom-center too — relocate the hint to bottom-left via
`.graph-wrap.galaxy .graph-hint { left: 18px; transform: none; }` if needed).

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/web/static/js/galaxy.js pseudolife_memory/web/static/css/styles.css
git commit -m "feat(console): galaxy time scrubber, flagged-star pulse, isolate"
```

---

### Task 2: shell + wiki page — pulse wiring, actionable banners, isolate toggle

**Files:**
- Modify: `pseudolife_memory/web/static/js/views/graph.js`
- Modify: `pseudolife_memory/web/static/js/views/wiki_page.js`

**Interfaces:**
- Consumes: Task 1 handle (`setFlagged/isolate/clearIsolate`), existing `actOnFinding` descriptors, `/api/graph/review` finding shapes (duplicate: `entities:[a,b]`; merge_candidate: `merges:[{id,from,into}]`; junk_candidate: `entities:[{id,entity}]`; proposed_link: `links:[{id,src,dst}]`; dubious_edge: `edges:[{src,dst}]`; unattributed/test_artifact/orphan: `entities:[str|{entity}]`).
- Produces: `openWikiPanel(wrap, name, { onExplore, onNavigate, onIsolate, onFlagAction })`.

- [ ] **Step 1: graph.js — flagged names from findings + lazy review fetch**

Add near the top of `renderGraph`'s helpers:

```javascript
  // Every entity name a finding touches — lights up the matching stars.
  function flaggedNames(findings) {
    const out = new Set();
    const add = (v) => { if (typeof v === "string" && v) out.add(v); };
    for (const f of findings || []) {
      for (const e of f.entities || []) { add(e); add(e && e.entity); }
      for (const m of f.merges || []) { add(m.from); add(m.into); }
      for (const l of f.links || []) { add(l.src); add(l.dst); }
      for (const e of f.edges || []) { add(e.src); add(e.dst); }
    }
    return out;
  }

  async function lightFlags() {
    if (!galaxy) return;
    try {
      const rd = await api.get("/api/graph/review", { scope: state.scope });
      galaxy && galaxy.setFlagged(flaggedNames(rd.findings));
    } catch { /* non-fatal — stars just don't pulse */ }
  }
```

Call `lightFlags()` (no await) right after the successful `createGalaxy` in
`paint()`, and keep `loadReview()` unchanged (the drawer refetches on open —
same endpoint, cheap).

- [ ] **Step 2: graph.js — panel hooks + post-mutation reopen**

In `openPage`, pass the two new hooks:

```javascript
    openWikiPanel(wrap, id, {
      onExplore: (name) => { galaxy && galaxy.flyTo(name); },
      onNavigate: (name) => { galaxy && galaxy.flyTo(name); openPage(wrap, name); },
      onIsolate: (name, on) => (galaxy ? (on ? galaxy.isolate(name, 2) : (galaxy.clearIsolate(), true)) : false),
      onFlagAction: (d) => actOnFinding(d),
    });
```

And extend `refreshAfterMutation` so an acted-on page comes back (and the
pulses re-light):

```javascript
  async function refreshAfterMutation() {
    if (state.review) await loadReview();
    await load();
    lightFlags();
    const wrap = host.querySelector(".graph-wrap");
    if (state.entity && wrap) openPage(wrap, state.entity, { fly: "late" });
  }
```

- [ ] **Step 3: wiki_page.js — actionable flag banner + isolate button**

Replace `flagBanner(d)` with a version that emits the same action descriptors
`actOnFinding` already handles (confirm gates included, for free):

```javascript
function flagBanner(d, onFlagAction) {
  if (!d.flags.length) return null;
  const act = (label, desc, kind) => onFlagAction
    ? el("button", { class: "btn sm" + (kind === "danger" ? " danger" : ""),
        onclick: () => onFlagAction(desc) }, label)
    : null;
  return el("div", { class: "wp-flags" }, d.flags.map((f) => {
    if (f.kind === "unattributed")
      return el("div", { class: "chip warn wp-flag" },
        "unattributed — no project owns this entity ",
        act("Assign", { kind: "assign", entities: [d.entity] }));
    if (f.kind === "proposed_link")
      return el("div", { class: "chip warn wp-flag" },
        `proposed link: ${f.src} ${f.relation} ${f.dst} `,
        act("Accept", { kind: "accept-link", id: f.id }),
        act("Reject", { kind: "reject-link", id: f.id }));
    if (f.kind === "merge_candidate")
      return el("div", { class: "chip warn wp-flag" },
        `suggested merge: ${f.entity} → ${f.into} `,
        act("Merge", { kind: "merge-entity", id: f.id, from: f.entity, into: f.into }),
        act("Reject", { kind: "reject-entity", id: f.id }));
    if (f.kind === "junk_candidate")
      return el("div", { class: "chip warn wp-flag" },
        `suspected junk: ${f.entity ?? d.entity} `,
        act("Delete", { kind: "junk-entity", id: f.id, entity: f.entity ?? d.entity }, "danger"),
        act("Reject", { kind: "reject-entity", id: f.id }));
    return el("div", { class: "chip warn wp-flag" },
      `${f.kind}: ${f.entity ?? ""}${f.into ? " → " + f.into : ""}`);
  }));
}
```

Thread the hooks through `render(host, d, nav, onExplore)` →
`render(host, d, nav, opts)` (read `onExplore`, `onIsolate`, `onFlagAction`
from opts; update the one call site) and swap the actions row to:

```javascript
    el("div", { class: "wp-actions" },
      el("button", { class: "btn sm primary", onclick: () => onExplore && onExplore(d.entity) },
        "Focus in galaxy"),
      (() => {
        if (!onIsolate) return null;
        let on = false;
        const b = el("button", { class: "btn sm", onclick: () => {
          on = !on ? !!onIsolate(d.entity, true) : (onIsolate(d.entity, false), false);
          b.classList.toggle("on", on);
          b.textContent = on ? "Show all" : "Isolate";
        } }, "Isolate");
        return b;
      })(),
      el("button", { class: "btn sm", title: `Cortex facts filtered to ${d.entity}`,
        onclick: () => { location.hash = "#/cortex?q=" + encodeURIComponent(d.entity); } },
        "Facts ↗"));
```

Call `flagBanner(d, onFlagAction)` at the call site. Small CSS:
`.wp-flag { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }`
(append to styles.css).

- [ ] **Step 4: Commit**

```bash
git add pseudolife_memory/web/static/js/views/graph.js pseudolife_memory/web/static/js/views/wiki_page.js pseudolife_memory/web/static/css/styles.css
git commit -m "feat(console): contextual review — pulsing flags, banner actions, isolate toggle"
```

---

### Task 3: QA, suite, CHANGELOG, review pass

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Devserver QA (fixtures carry deterministic timestamps + 9 findings)**

Scrubber: drag left → stars/edges vanish oldest-last (fixture nodes are spaced
a day apart), date label tracks, drag to end → "now", play button sweeps and
stops; layout never re-heats (positions identical before/after). Pulse:
fixture review has findings (duplicate, dubious edges, unattributed…) — the
named stars pulse; drawer actions still work; wiki page of a flagged entity
shows the banner with buttons; an action round-trips (confirm → toast →
galaxy reloads → panel reopens). Isolate: on a hub page, Isolate dims the far
graph, labels hide on dimmed stars, Show all restores. Search still overrides
visually. Reduced-motion (devtools emulation): no pulse (tint instead), play
button inert. Both themes. Route-switch leak check. No console errors.

- [ ] **Step 2: Full suite** — `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/ -q` (bench PG up). Expected: all pass (no backend changes).

- [ ] **Step 3: CHANGELOG** (under `[Unreleased]`)

```markdown
### Added (2026-07-15 — Atlas stage 3: time, review, focus)
- Galaxy time scrubber: replay the bank's growth — stars/edges appear by
  `created_at`/`asserted_at` over a fixed layout (visibility only, no
  re-simulation), with play/pause; date readout; disabled auto-play under
  reduced motion.
- Contextual review: entities named in open findings pulse in the galaxy
  (static tint under reduced motion); wiki-page flag banners carry the same
  confirm-gated actions as the review drawer (merge/reject/accept/assign);
  acting refreshes the galaxy and reopens the page.
- Isolate toggle on wiki pages: dims everything beyond the entity's 2-hop
  neighborhood (client-side, no fetch) — the explore-mode replacement.
```

- [ ] **Step 4: Review pass** (`/code-review` medium — renderer/perf class), fix findings, commit CHANGELOG, then finishing-a-development-branch (merge → push → deploy per session pattern).

## Self-review notes

- Spec §C/§D coverage: scrubber ✔ (visibility-only, honors reduced motion),
  pulses ✔, banners-with-actions ✔ (descriptor reuse keeps confirm gates),
  isolate ✔ (1–2 hop BFS client-side; depth 2 chosen), post-mutation refresh ✔.
- Priority order query > isolate > flag-tint defined once in `nodeColor`.
- Types: handle methods named identically in Tasks 1/2; `onFlagAction`
  descriptors match `actOnFinding`'s existing kinds exactly.
