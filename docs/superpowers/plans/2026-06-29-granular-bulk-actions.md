# Granular per-item bulk actions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human prune/assign/delete a *chosen subset* of a long review finding (and make `orphan` actionable), instead of all-or-nothing.

**Architecture:** Pure frontend. A new `selectableList` component in `atlas_review.js` renders a filterable, capped-scroll checkbox list; `findingRow` routes the four group findings (`dubious_edge`, `unattributed`, `test_artifact`, `orphan`) through it with action button(s) that operate on `getSelected()`. `views/atlas.js` is unchanged (its `prune`/`assign`/`delete-names` handlers already iterate the array they're given). The findings already carry their full lists, so there's no backend change beyond enriching the demo fixture.

**Tech Stack:** Vanilla ES-module JS (no build, no JS test runner). Python/pytest only for the backend dispatch test.

## Global Constraints

- **No JS unit runner.** Frontend verification is on the fixture devserver (`python -m pseudolife_memory.web.devserver`, port 8770) driven by the chrome-devtools MCP (NOT Playwright — it serves stale cached assets). Reload with `ignoreCache` after edits.
- Start the devserver with the Bash tool `run_in_background: true` (it dies with a one-shot shell otherwise).
- **Opt-in default:** nothing selected initially; action buttons read `Label (N)` and are `disabled` at 0.
- No backend logic change; `views/atlas.js` `actOnFinding` already handles `prune` / `assign` / `delete-names`.
- Run the backend test with `./.venv/Scripts/python.exe -m pytest tests/test_web.py -q`.
- Work on branch `feat/granular-bulk-actions` (already holds the design-spec commit).
- Reuse existing helpers in `atlas_review.js`: `el`, `CHIP`, `btn`, `entityRef` (returns `{chip, drawer}`), `badge`, `SEV`, `dim`, `panel`.

---

### Task 1: `selectableList` component + fixture, wired to `dubious_edge`

**Files:**
- Modify: `pseudolife_memory/web/static/js/atlas_review.js` (add `selectableList` + `SELECTABLE` + `selectableBody`; rewrite `findingRow`; drop the now-dead `bulkAction`)
- Modify: `pseudolife_memory/web/fixtures.py` (`graph_review`: give `dubious_edge` ~12 edges, add an `orphan` finding)
- Verify: fixture devserver + chrome-devtools

**Interfaces:**
- Produces: `selectableList(items, { row, filterText, onChange }) -> { node, getSelected }` where
  `row(item) -> { cell: Element, extra?: Element }`, `filterText(item) -> string`,
  `onChange(selectedCount)`. `getSelected()` returns the selected items in list order.
- Produces: `SELECTABLE = new Set(["dubious_edge","unattributed","test_artifact","orphan"])` and
  `selectableBody(f, setCount) -> { node, getSelected }`.

- [ ] **Step 1: Enrich the fixture so the UI is exercised**

In `pseudolife_memory/web/fixtures.py`, inside `graph_review`'s `findings` list, replace the
single-edge `dubious_edge` entry and the `unattributed` entry, and add an `orphan` entry:

```python
            {"type": "dubious_edge", "severity": "warn", "action": "prune",
             "label": "12 low-confidence inferred edges",
             "edges": [{"src": f"node{i}", "relation": "related-to", "dst": f"node{i+1}",
                        "confidence": 0.55} for i in range(12)]},
            {"type": "unattributed", "severity": "info", "action": "assign",
             "label": "5 entities with no project",
             "entities": ["alpha", "beta", "gamma", "delta", "epsilon"]},
            {"type": "orphan", "severity": "info", "action": "review",
             "label": "4 weakly-connected entities",
             "entities": ["lonely-1", "lonely-2", "lonely-3", "lonely-4"]},
```

- [ ] **Step 2: Add `selectableList` to `atlas_review.js`**

Add near the bottom of `atlas_review.js` (after `nameChips`):

```javascript
const SELECTABLE = new Set(["dubious_edge", "unattributed", "test_artifact", "orphan"]);

// A filterable, capped-scroll checkbox list. Opt-in: nothing selected initially.
function selectableList(items, { row, filterText, onChange }) {
  const selected = new Set();
  const emit = () => onChange && onChange(selected.size);
  const rows = [];
  const listEl = el("div", { style: { maxHeight: "280px", overflowY: "auto", marginTop: "6px",
    border: "1px solid var(--surface-2, rgba(127,127,127,.2))", borderRadius: "8px", padding: "4px 6px" } });
  for (const item of items) {
    const cb = el("input", { type: "checkbox", style: { marginRight: "8px", flex: "0 0 auto" },
      onchange: () => { cb.checked ? selected.add(item) : selected.delete(item); emit(); } });
    const { cell, extra } = row(item);
    const line = el("div", { style: { display: "flex", alignItems: "center", gap: "4px", padding: "2px 0" } }, cb, cell);
    const rowEl = el("div", {}, line, extra || null);
    rows.push({ row: rowEl, cb, item, text: String(filterText(item) || "").toLowerCase() });
    listEl.appendChild(rowEl);
  }
  const filter = el("input", { type: "text", placeholder: "filter…", "aria-label": "filter selection",
    style: { fontSize: "12px", padding: "3px 7px" },
    oninput: () => { const q = filter.value.toLowerCase();
      for (const r of rows) r.row.style.display = (!q || r.text.includes(q)) ? "" : "none"; } });
  const selAll = el("button", { class: "btn sm", onclick: () => {
    for (const r of rows) if (r.row.style.display !== "none") { r.cb.checked = true; selected.add(r.item); } emit(); } },
    "select all");
  const clear = el("button", { class: "btn sm", style: { opacity: ".75" }, onclick: () => {
    for (const r of rows) r.cb.checked = false; selected.clear(); emit(); } }, "clear");
  const controls = el("div", { style: { display: "flex", gap: "6px", alignItems: "center", flexWrap: "wrap" } },
    filter, selAll, clear);
  return { node: el("div", {}, controls, listEl), getSelected: () => [...selected] };
}

// Build the selectable list for a finding (edges vs entity names).
function selectableBody(f, onChange) {
  if (f.type === "dubious_edge") {
    return selectableList(f.edges || [], {
      row: (e) => ({ cell: el("span", { class: "mono", style: CHIP },
        `${e.src} —${e.relation}→ ${e.dst}`, e.confidence != null ? ` (${(+e.confidence).toFixed(2)})` : "") }),
      filterText: (e) => `${e.src} ${e.relation} ${e.dst}`, onChange });
  }
  const names = (f.entities || []).map((e) => (typeof e === "string" ? e : e.entity));
  return selectableList(names, {
    row: (n) => { const r = entityRef(n); return { cell: r.chip, extra: r.drawer }; },
    filterText: (n) => n, onChange });
}
```

- [ ] **Step 3: Rewrite `findingRow` to route SELECTABLE findings; drop `bulkAction`**

Replace the existing `findingRow` and `bulkAction` functions with:

```javascript
function findingRow(f, onAct) {
  const acc = SEV[f.severity] || SEV.info;
  const frame = (buttons, inner) => el("div", { style: { borderLeft: `3px solid ${acc}`,
      padding: "8px 10px", marginBottom: "8px", background: "var(--surface-1, rgba(127,127,127,.06))",
      borderRadius: "0 8px 8px 0" } },
    el("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
      badge(String(f.type || "").replace(/_/g, " ")),
      el("span", { style: { fontWeight: "500" } }, f.label),
      el("span", { style: { marginLeft: "auto", display: "flex", gap: "6px" } }, buttons || null)),
    el("div", { style: { marginTop: "6px" } }, inner));

  if (SELECTABLE.has(f.type)) {
    let getSel = () => [];
    const specs = {
      dubious_edge:  [["Prune", "danger", (s) => ({ kind: "prune", edges: s })]],
      unattributed:  [["Assign", null, (s) => ({ kind: "assign", entities: s })]],
      test_artifact: [["Delete", "danger", (s) => ({ kind: "delete-names", entities: s })]],
      orphan:        [["Delete", "danger", (s) => ({ kind: "delete-names", entities: s })],
                      ["Assign", null, (s) => ({ kind: "assign", entities: s })]],
    }[f.type];
    const made = specs.map(([label, kind, make]) =>
      ({ label, b: btn(`${label} (0)`, { kind, onClick: () => onAct(make(getSel())) }) }));
    const setCount = (n) => { for (const { label, b } of made) { b.textContent = `${label} (${n})`; b.disabled = n === 0; } };
    const list = selectableBody(f, setCount);
    getSel = list.getSelected;
    setCount(0);
    return frame(made.map((m) => m.b), list.node);
  }

  return frame(null, body(f, onAct));
}
```

Then remove the now-unused `bulkAction`, `edgeList`, and `nameChips` functions (the `body`
switch no longer routes any type to them; `body` keeps only `merge_candidate` / `junk_candidate`
/ `proposed_link` / `duplicate`). Update `body`'s default case to return `null`:

```javascript
function body(f, onAct) {
  switch (f.type) {
    case "merge_candidate": return (f.merges || []).map((m) => mergeItem(m, onAct));
    case "junk_candidate":  return (f.entities || []).map((j) => junkItem(j, onAct));
    case "proposed_link":   return (f.links || []).map((l) => linkItem(l, onAct));
    case "duplicate":       return dupItem(f, onAct);
    default:                return null;
  }
}
```

- [ ] **Step 4: Start the devserver and verify `dubious_edge` selection**

Start (Bash, `run_in_background: true`): `./.venv/Scripts/python.exe -m pseudolife_memory.web.devserver`
Then in chrome-devtools: navigate `http://127.0.0.1:8770/ui/#/atlas`, reload `ignoreCache`, click **Review**, and run this `evaluate_script`:

```javascript
async () => {
  // open the dubious_edge finding's list
  const badge = [...document.querySelectorAll('*')].find(e => e.children.length===0 && e.textContent.trim()==='dubious edge');
  const card = badge.closest('div').parentElement.parentElement;
  const boxes = card.querySelectorAll('input[type=checkbox]');
  const pruneBtn = [...card.querySelectorAll('button')].find(b => b.textContent.startsWith('Prune'));
  const disabledAt0 = pruneBtn.disabled;
  boxes[0].click(); boxes[2].click();                 // select 2
  const label2 = pruneBtn.textContent;                 // "Prune (2)"
  // filter narrows
  const filter = card.querySelector('input[type=text]');
  filter.value = 'node5'; filter.dispatchEvent(new Event('input'));
  const visible = [...card.querySelectorAll('input[type=checkbox]')].filter(c => c.closest('div').parentElement.style.display !== 'none').length;
  return { totalBoxes: boxes.length, disabledAt0, label2, visibleAfterFilter: visible };
}
```
Expected: `totalBoxes: 12`, `disabledAt0: true`, `label2: "Prune (2)"`, `visibleAfterFilter: 1`.

Then verify Prune posts ONLY the selected subset: clear filter, select 2 edges, click Prune, accept the confirm, and check `list_network_requests` shows exactly **2** `POST /api/graph/unrelate` calls (not 12). Confirm zero console errors via `list_console_messages`.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/web/static/js/atlas_review.js pseudolife_memory/web/fixtures.py
git commit -m "feat(atlas-review): selectableList + per-item Prune for dubious edges"
```

---

### Task 2: Wire `unattributed` (Assign), `test_artifact` (Delete), `orphan` (Delete + Assign)

**Files:**
- Modify: `pseudolife_memory/web/static/js/atlas_review.js` (no new code — Task 1's `findingRow`
  switch + `selectableBody` already handle these types; this task verifies the entity-row path)
- Verify: fixture devserver + chrome-devtools

**Interfaces:**
- Consumes: `selectableList`, `selectableBody`, `findingRow` (Task 1); `entityRef` (existing).

> Note: Task 1's code already routes all four SELECTABLE types. This task exists to verify the
> entity-row path (provenance drawer + Assign modal + orphan's two buttons) independently — if any
> assertion fails, fix `selectableBody`/`findingRow` here.

- [ ] **Step 1: Verify entity findings on the devserver**

Reload `http://127.0.0.1:8770/ui/#/atlas` (`ignoreCache`), open **Review**, run:

```javascript
async () => {
  const card = (name) => { const b = [...document.querySelectorAll('*')].find(e => e.children.length===0 && e.textContent.trim()===name);
    return b.closest('div').parentElement.parentElement; };
  // unattributed: Assign button + provenance chip still clickable
  const ua = card('unattributed');
  const assign = [...ua.querySelectorAll('button')].find(b => b.textContent.startsWith('Assign'));
  ua.querySelectorAll('input[type=checkbox]')[0].click();
  const assignLabel = assign.textContent;                       // "Assign (1)"
  const hasProvChip = !!ua.querySelector('span[title^="Show provenance"]');
  // orphan: two buttons
  const orph = card('orphan');
  const orphBtns = [...orph.querySelectorAll('button')].map(b => b.textContent.replace(/\s*\(\d+\)/,'')).filter(t => ['Delete','Assign'].includes(t));
  return { assignLabel, hasProvChip, orphButtons: orphBtns };
}
```
Expected: `assignLabel: "Assign (1)"`, `hasProvChip: true`, `orphButtons: ["Delete","Assign"]`.

- [ ] **Step 2: Verify Assign posts only the selected subset**

Select 2 of the 5 `unattributed` entities, click **Assign**, type a project in the modal, confirm.
`list_network_requests`: exactly **2** `POST /api/graph/assign-scope`. Then on `orphan`, select 1,
click **Delete**, confirm: exactly **1** `POST /api/graph/delete-entity`. Console clean.

- [ ] **Step 3: Commit (only if Task 1 code needed fixes here)**

```bash
git add pseudolife_memory/web/static/js/atlas_review.js
git commit -m "fix(atlas-review): verify entity-finding selection (assign/delete/orphan)"
```
If no fix was needed, skip — Task 1's commit already covers it; note that in the report.

---

### Task 3: Backend regression, then ship + deploy (user-present)

**Files:** No code. Regression + operational runbook.

- [ ] **Step 1: Backend test green**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_web.py -q`
Expected: PASS (the enriched fixture keeps `counts.total == len(findings)` and a `merge` action present).

- [ ] **Step 2: Stop the devserver, merge to local master**

Stop the background devserver (kill the port-8770 process), then:
```bash
git checkout master && git merge --ff-only feat/granular-bulk-actions && git branch -d feat/granular-bulk-actions
```

- [ ] **Step 3: CHANGELOG + commit**

Add an `### Added` bullet: per-item selection (filter + select-all-filtered + live count) for the
`dubious_edge` / `unattributed` / `test_artifact` findings and a new actionable `orphan`
(Delete + Assign). Commit.

- [ ] **Step 4: Deploy (explicit user authorization required)**

```bash
pwsh -NoProfile -File ops/backup.ps1
docker tag pseudolife-daemon:0.2.0 pseudolife-daemon:0.2.0-pre-granularbulk
docker compose -f ops/docker-compose.yml up -d --no-deps --build pseudolife-daemon
```
Verify `/health` (schema 18) and pg/extractor untouched.

- [ ] **Step 5: Live verify**

Reload `http://127.0.0.1:8765/ui/#/atlas` (`ignoreCache`), open **Review**, and confirm on the real
queue: `dubious_edge` (~192) and `unattributed`/`orphan` render selectable lists; the filter narrows;
a small selection prunes/assigns/deletes only the selected items (network count matches selection);
zero console errors. (Do not bulk-prune the real 192 — verify with a 1–2 item selection.)

---

## Self-Review

**Spec coverage:**
- Component 1 `selectableList` → Task 1 Step 2. ✓
- Component 2 `findingRow` wiring (4 findings, opt-in count buttons, provenance rows) → Task 1 Step 3 + Task 2. ✓
- Component 3 `atlas.js` unchanged (handlers take the array) → confirmed in Task 1/2 verifications (network shows N posts for N selected). ✓
- Component 4 fixture enrichment → Task 1 Step 1. ✓
- Test plan (filter / select-all / count / subset prune / orphan / provenance / console / test_web) → Task 1 Step 4, Task 2 Steps 1–2, Task 3 Step 1. ✓

**Placeholder scan:** none — full code for `selectableList`, `selectableBody`, `findingRow`, `body`, and the fixture; concrete `evaluate_script` assertions with expected values.

**Type consistency:** `selectableList(items, { row, filterText, onChange })` and `row(item)->{cell,extra}`
are used identically in `selectableBody`. The onAct descriptors (`{kind:"prune",edges}`,
`{kind:"assign",entities}`, `{kind:"delete-names",entities}`) match the kinds already handled by
`views/atlas.js actOnFinding` from the prior feature. `entityRef` is used as `{chip, drawer}` (its
existing shape). `getSelected()` returns items (edge objects for dubious_edge; name strings for the
entity findings), matching what the handlers post.

**Note — spec→plan refinement:** the spec listed `cell` + `rowExtra` as separate options; the plan
consolidates them into one `row(item) -> {cell, extra}` callback so an entity's `entityRef` is built
once (chip + its drawer) — same behaviour, avoids a double `entityRef` call.
