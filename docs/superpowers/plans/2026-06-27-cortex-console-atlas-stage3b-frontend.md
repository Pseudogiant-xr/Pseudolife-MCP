# Atlas Stage 3b — review queue + confirm-gated cleanup UI (frontend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Atlas review workbench — a "Review (N)" queue of graph-hygiene findings (from the Stage 3a `/api/graph/review`) with confirm-gated, one-click cleanup actions (merge, delete, prune, assign) that call the Stage 3a mutation endpoints and refresh the view.

**Architecture:** A new `atlas_review.js` renders the findings list (pure rendering; action handlers injected). `atlas.js` (the Stage 2 view) gains a "Review" toggle that fetches `/api/graph/review?scope=` and shows the queue above the map, plus a confirm-gated action dispatcher that posts to the mutation endpoints (via `confirmDialog`/`openModal`/`toast`) and re-loads the review + map. No backend changes — all endpoints + fixtures exist from Stage 3a.

**Tech Stack:** Vanilla ESM, `el()` hyperscript, `components.js`/`ui.js` helpers, `api.get`/`api.post`; verified in-browser via the fixture devserver + chrome-devtools.

## Global Constraints

- **No JS test runner.** Verification is the fixture devserver + chrome-devtools: render, interact, **zero console errors**. Devserver (repo root, background): `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770` → `http://127.0.0.1:8770/ui/`. The `FixtureService` already serves `graph_review` (returns demo findings) and `graph_merge`/`graph_delete_entity`/`graph_unrelate`/`graph_assign_scope` (return success) — added in Stage 3a — so the full review→action→refresh loop is exercisable offline.
- **Match existing patterns.** `el`/`mount`/`clear` (`util.js`); `panel`/`badge` (`components.js`); `confirmDialog`/`openModal`/`closeModal`/`toast` (`ui.js`); `api.get`/`api.post` (`api.js`). CSS classes already present (`panel`, `panel-head`, `panel-body`, `toolbar`, `facets`/`facet`, `btn sm`, `btn sm danger`, `chip`, `dim`, `mono`). Two font weights, sentence case, CSS variables for colour.
- **Confirm-gate every mutation.** No destructive call fires without `confirmDialog` (or `openModal` choice). Destructive confirms pass `danger: true`. On success: `toast(..., "ok")` and re-fetch the review + the graph.
- **No backend changes.** Frontend-only (`atlas.js`, new `atlas_review.js`).
- **Stage boundary:** this completes the Atlas workbench. No new analyzer detectors or endpoints.

## File Structure

- `static/js/atlas_review.js` — NEW. `reviewPanel(data, onAct)` renders the findings list with one action button per finding; pure rendering, `onAct(finding)` injected.
- `static/js/views/atlas.js` — MODIFY. Add a "Review (N)" toolbar toggle, a review host above the map, `loadReview()`, and the confirm-gated `actOnFinding(finding)` dispatcher.

---

### Task 1: Review queue panel (read-only render + toggle)

**Files:**
- Create: `static/js/atlas_review.js`
- Modify: `static/js/views/atlas.js`

**Interfaces:**
- Produces: `reviewPanel(data, onAct) -> HTMLElement` where `data` is the `/api/graph/review` response (`{findings:[...], counts:{total}}`) and `onAct(finding)` is called when a finding's action button is clicked.
- Consumes (Task 2 supplies the real `onAct`): in this task `onAct` is a stub.

- [ ] **Step 1: Create `static/js/atlas_review.js`**

```javascript
// atlas_review.js — the graph-review queue: findings from /api/graph/review,
// each with a confirm-gated cleanup action. Pure rendering; the action handler
// is supplied by atlas.js (which owns the post + re-fetch).
import { el } from "./util.js";
import { panel, badge } from "./components.js";

const SEV = { warn: "var(--c-lessons)", info: "var(--c-assoc)", danger: "var(--c-world)" };
const ACTION_LABEL = { merge: "Merge", delete: "Delete", prune: "Prune", assign: "Assign project" };

export function reviewPanel(data, onAct) {
  const findings = (data && data.findings) || [];
  if (!findings.length) {
    return panel("Review queue",
      el("div", { class: "empty" },
        el("div", { class: "big" }, "Graph looks clean"),
        el("div", {}, "No duplicate, orphan, dubious-edge, test-artifact, or unattributed findings in scope.")),
      { accent: "var(--c-graph)" });
  }
  return panel("Review queue",
    el("div", {}, findings.map((f) => findingRow(f, onAct))),
    { accent: "var(--c-graph)", sub: String(findings.length) });
}

function chips(items, fmt) {
  return el("div", { style: { marginTop: "6px", display: "flex", flexWrap: "wrap", gap: "4px" } },
    items.slice(0, 6).map((m) => el("span", { class: "mono",
      style: { fontSize: "12px", background: "var(--surface-1, rgba(127,127,127,.12))", padding: "2px 7px", borderRadius: "6px" } }, fmt(m))),
    items.length > 6 ? el("span", { class: "dim", style: { fontSize: "12px" } }, `+${items.length - 6}`) : null);
}

function findingRow(f, onAct) {
  const acc = SEV[f.severity] || SEV.info;
  return el("div", { style: { borderLeft: `3px solid ${acc}`, padding: "8px 10px", marginBottom: "8px",
      background: "var(--surface-1, rgba(127,127,127,.06))", borderRadius: "0 8px 8px 0" } },
    el("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
      badge(String(f.type || "").replace(/_/g, " ")),
      el("span", { style: { fontWeight: "500" } }, f.label),
      el("span", { style: { marginLeft: "auto" } },
        f.action && f.action !== "review"
          ? el("button", { class: "btn sm" + (f.action === "delete" ? " danger" : ""),
              title: ACTION_LABEL[f.action] || f.action, onclick: () => onAct(f) }, ACTION_LABEL[f.action] || f.action)
          : null)),
    (f.entities || []).length ? chips(f.entities, (m) => m) : null,
    (f.edges || []).length ? chips(f.edges, (e) => `${e.src} —${e.relation}→ ${e.dst}`) : null);
}
```

- [ ] **Step 2: Wire the toggle + review host into `atlas.js`**

In `static/js/views/atlas.js`, add the import:

```javascript
import { reviewPanel } from "../atlas_review.js";
```

Extend the module state and add a review host. Change `let state = { scope: "all", view: "map" };` to:

```javascript
let state = { scope: "all", view: "map", review: false };
let reviewData = null;
```

In `renderAtlas`, after the `viewToggle` is built and before `mount(root, ...)`, add a Review toggle and a review host element, and include them in the mount. Replace the existing `mount(root, el("div",{class:"toolbar"}, ...), host);` with:

```javascript
  const reviewBtn = el("button", { class: "facet" + (state.review ? " on" : ""),
    onclick: () => { state.review = !state.review; reviewBtn.classList.toggle("on", state.review);
      reviewHost.style.display = state.review ? "" : "none"; if (state.review) loadReview(); } },
    "Review");
  const reviewHost = el("div", { style: { display: state.review ? "" : "none", marginBottom: "12px" } });

  mount(root,
    el("div", { class: "toolbar" },
      el("span", { class: "eyebrow" }, "scope"), switcher,
      el("span", { class: "grow" }), reviewBtn, viewToggle),
    reviewHost, host);

  async function loadReview() {
    mount(reviewHost, loadingBlock("Scanning the graph…"));
    try {
      reviewData = await api.get("/api/graph/review", { scope: state.scope });
      mount(reviewHost, reviewPanel(reviewData, (f) => actOnFinding(f)));
    } catch (err) { mount(reviewHost, errorBlock(err)); }
  }

  // Stub for Task 1; Task 2 replaces this with the confirm-gated dispatcher.
  function actOnFinding(_f) {}
```

Also re-load the review when the scope changes: in the `switcher` callback, after `state.scope = v; load();`, add `if (state.review) loadReview();`. (The switcher is built as `facetBar(scopeOpts, state.scope, (v) => { state.scope = v; load(); })` — change the callback body to `{ state.scope = v; load(); if (state.review) loadReview(); }`.)

- [ ] **Step 3: Verify the queue renders (devserver + chrome-devtools)**

Start the devserver in the background: `HF_HUB_OFFLINE=1 ./.venv/Scripts/python.exe -m pseudolife_memory.web.devserver --port 8770`.
With chrome-devtools: `navigate_page` to `http://127.0.0.1:8770/ui/#/atlas`, hard-reload (`navigate_page reload ignoreCache`), `take_snapshot`. Verify the toolbar shows a "Review" toggle. Click it (`click` the Review facet), `take_snapshot` + `list_console_messages`. Verify: the Review queue panel appears with the fixture findings (a "merge" duplicate, a "delete" test-artifact, a "prune" dubious-edge, an "assign" unattributed), each with member/edge chips and an action button; toggling off hides it; **zero console errors**.

- [ ] **Step 4: Commit**

```bash
git add static/js/atlas_review.js static/js/views/atlas.js
git commit -m "feat(web): Atlas review queue panel + toggle"
```

---

### Task 2: Confirm-gated cleanup actions

**Files:**
- Modify: `static/js/views/atlas.js`

**Interfaces:**
- Consumes: `reviewPanel`'s `onAct(finding)`; `api.post`; `confirmDialog`/`openModal`/`closeModal`/`toast`; the Stage 3a endpoints `POST /api/graph/{merge,delete-entity,unrelate,assign-scope}`.
- Produces: `actOnFinding(finding)` — confirm-gated dispatcher that mutates, then re-loads review + map.

- [ ] **Step 1: Add the imports + replace the `actOnFinding` stub**

In `static/js/views/atlas.js`, add to the imports:

```javascript
import { confirmDialog, openModal, closeModal, toast } from "../ui.js";
```

Replace the Task-1 stub `function actOnFinding(_f) {}` with the dispatcher:

```javascript
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

  async function actOnFinding(f) {
    if (f.action === "merge") {
      const [a, b] = f.entities || [];
      if (!a || !b) return;
      // Let the user pick which name survives.
      openModal({
        title: "Merge duplicate entities",
        body: el("div", {}, el("p", { class: "dim", style: { marginTop: 0 } },
          "One entity absorbs the other's edges, aliases and project tags. Which name should survive?")),
        actions: [
          { label: "Cancel", onClick: closeModal },
          { label: `Keep “${a}”`, kind: "primary", onClick: async () => { closeModal();
            await postAll([{ path: "/api/graph/merge", body: { from: b, into: a } }], "Merged"); } },
          { label: `Keep “${b}”`, onClick: async () => { closeModal();
            await postAll([{ path: "/api/graph/merge", body: { from: a, into: b } }], "Merged"); } },
        ],
      });
      return;
    }
    if (f.action === "delete") {
      const ents = f.entities || [];
      if (!(await confirmDialog({ title: "Delete entities", danger: true,
        message: `Permanently delete ${ents.length} entit${ents.length === 1 ? "y" : "ies"} and their edges? This cannot be undone.` }))) return;
      await postAll(ents.map((e) => ({ path: "/api/graph/delete-entity", body: { entity: e } })), "Deleted");
      return;
    }
    if (f.action === "prune") {
      const edges = f.edges || [];
      if (!(await confirmDialog({ title: "Prune edges", danger: true,
        message: `Remove ${edges.length} low-confidence inferred edge${edges.length === 1 ? "" : "s"}?` }))) return;
      await postAll(edges.map((e) => ({ path: "/api/graph/unrelate",
        body: { src: e.src, relation: e.relation, dst: e.dst } })), "Pruned");
      return;
    }
    if (f.action === "assign") {
      const ents = f.entities || [];
      const input = el("input", { type: "text", placeholder: "project / source name", name: "project" });
      openModal({
        title: "Assign a project",
        body: el("div", {}, el("p", { class: "dim", style: { marginTop: 0 } },
          `Tag ${ents.length} unattributed entit${ents.length === 1 ? "y" : "ies"} with a project. They'll appear under that scope.`), input),
        actions: [
          { label: "Cancel", onClick: closeModal },
          { label: "Assign", kind: "primary", onClick: async () => {
            const src = input.value.trim(); if (!src) return; closeModal();
            await postAll(ents.map((e) => ({ path: "/api/graph/assign-scope", body: { entity: e, source: src } })), "Assigned"); } },
        ],
      });
      return;
    }
  }
```

- [ ] **Step 2: Verify the actions on the devserver (chrome-devtools)**

Devserver running. Reload `http://127.0.0.1:8770/ui/#/atlas`, open Review.
- Click the duplicate finding's **Merge** → a modal with two "Keep …" choices appears; pick one → (fixture returns `{merged:true}`) → a success toast, the modal closes, the review + map re-fetch. Console clean.
- Click the test-artifact **Delete** → a danger confirm dialog → confirm → success toast + refresh.
- Click the dubious-edge **Prune** → danger confirm → confirm → success toast + refresh.
- Click the unattributed **Assign** → a modal with a project input → type a name, Assign → success toast + refresh.
`list_console_messages` after each — **zero errors** throughout.

- [ ] **Step 3: Commit**

```bash
git add static/js/views/atlas.js
git commit -m "feat(web): confirm-gated cleanup actions in the Atlas review queue"
```

---

## Self-Review

**Spec coverage (Stage 3b = spec §D review queue + action panel):**
- Review queue listing findings → Task 1 (`reviewPanel` + toggle). ✓
- Per-finding confirm-gated actions (merge/delete/prune/assign) calling the Stage 3a endpoints → Task 2. ✓
- Confirm-gated + toast + re-fetch after mutation → Task 2 (`confirmDialog`/`openModal`, `toast`, `refreshAfterMutation`). ✓
- Scope-aware review (re-loads on switcher change) → Task 1. ✓

**Out of scope (done in 3a / earlier):** the analyzer + endpoints (3a); the map + switcher + Show-in-Atlas (Stage 2). No "backed up first" server wording (the deploy/ops backup is the safety net; the UI confirm is the gate). Bulk-action partial-failure handling is best-effort (per-call toast on failure).

**Placeholder scan:** none — `atlas_review.js` and the `atlas.js` additions are complete; Task 1 ships a real (stub) `actOnFinding` that Task 2 replaces with the dispatcher.

**Type consistency:** `reviewPanel(data, onAct)`; `onAct(finding)` / `actOnFinding(f)`; finding shape `{type, severity, label, action, entities?, edges?}` (matches the Stage 3a `graph_review` output and the FixtureService stub); endpoint bodies `{from,into}` / `{entity}` / `{src,relation,dst}` / `{entity,source}` match the Stage 3a routes. Consistent across both tasks.

**Verification reality:** browser-verified via the fixture devserver + chrome-devtools (no JS test runner). The FixtureService serves both `graph_review` (findings) and all four mutation stubs (success), so the full review→confirm→mutate→refresh loop runs offline with zero console errors.
