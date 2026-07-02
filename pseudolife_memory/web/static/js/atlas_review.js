// atlas_review.js — the graph-review queue: findings from /api/graph/review,
// each with a confirm-gated cleanup action. The deep-dream findings
// (merge_candidate / junk_candidate / proposed_link) carry per-item proposal
// ids; analyzer findings (duplicate / dubious_edge / orphan / unattributed /
// test_artifact) carry names/edges. Every entity name is clickable to lazy-load
// its MIRAS provenance (sources + source entries) so a human can judge from
// evidence, not names alone. Mutations are owned by atlas.js via onAct(desc);
// provenance is a read this panel hydrates itself.
import { el } from "./util.js";
import { panel, badge } from "./components.js";
import { api } from "./api.js";

const SEV = { warn: "var(--c-lessons)", info: "var(--c-assoc)", danger: "var(--c-world)" };
const CHIP = { background: "var(--surface-1, rgba(127,127,127,.12))", padding: "2px 7px",
  borderRadius: "6px", fontSize: "12px" };

export function reviewPanel(data, onAct) {
  const findings = (data && data.findings) || [];
  if (!findings.length) {
    return panel("Review queue",
      el("div", { class: "empty" },
        el("div", { class: "big" }, "Graph looks clean"),
        el("div", {}, "No duplicate, orphan, dubious-edge, test-artifact, unattributed, "
          + "merge, junk, or proposed-link findings in scope.")),
      { accent: "var(--c-graph)" });
  }
  return panel("Review queue",
    el("div", {}, findings.map((f) => findingRow(f, onAct))),
    { accent: "var(--c-graph)", sub: String(findings.length) });
}

// ── small bits ──────────────────────────────────────────────────────────────

function dim(text) {
  return text == null || text === ""
    ? null
    : el("span", { class: "dim", style: { fontSize: "12px" } }, text);
}

function sim(v) { return v == null ? null : dim(`sim ${(+v).toFixed(2)}`); }
function conf(v) { return v == null ? null : dim(`conf ${(+v).toFixed(2)}`); }

function btn(label, { kind, onClick }) {
  return el("button", { class: "btn sm" + (kind === "danger" ? " danger" : ""),
    style: kind === "ghost" ? { opacity: ".75" } : null, onclick: onClick }, label);
}

// A clickable entity name that lazy-loads its provenance into a drawer.
// Returns { chip, drawer } so the caller can place the drawer under the row.
function entityRef(name) {
  const drawer = el("div", { style: { display: "none", margin: "2px 0 6px 14px",
    paddingLeft: "8px", borderLeft: "2px solid var(--surface-2, rgba(127,127,127,.25))" } });
  let loaded = false;
  async function toggle() {
    const opening = drawer.style.display === "none";
    drawer.style.display = opening ? "" : "none";
    if (!opening || loaded) return;
    loaded = true;
    drawer.appendChild(dim("loading provenance…"));
    try {
      const p = await api.get("/api/graph/entity-provenance", { entity: name });
      drawer.textContent = "";
      drawer.appendChild(provBody(p));
    } catch (err) {
      drawer.textContent = "";
      drawer.appendChild(dim("provenance unavailable"));
    }
  }
  const chip = el("span", { class: "mono", title: `Show provenance for ${name}`,
    style: { ...CHIP, cursor: "pointer", borderBottom: "1px dotted currentColor" },
    onclick: toggle }, name);
  return { chip, drawer };
}

function provBody(p) {
  if (!p || p.found === false) return dim("no provenance — graph-only node, no source entries");
  const sources = (p.sources || []).map((s) =>
    el("span", { class: "mono", style: { ...CHIP, marginRight: "4px" } },
      `${s.source} ·${s.count}· ${s.origin}`));
  const entries = (p.entries || []).map((e) =>
    el("div", { style: { margin: "3px 0", fontSize: "12px" } },
      el("span", { class: "mono", style: { ...CHIP, marginRight: "6px" } },
        `${e.band} · ${e.source || "—"}`),
      el("span", { class: "dim" }, snippet(e.text))));
  return el("div", {},
    sources.length ? el("div", { style: { marginBottom: "4px" } }, dim("sources: "), ...sources) : null,
    entries.length ? el("div", {}, ...entries) : dim("no source entries"));
}

function snippet(t) {
  t = String(t || "");
  return t.length > 160 ? t.slice(0, 157) + "…" : t;
}

// One actionable item line: [chips/score] … [buttons], with the provenance
// drawers for its entities stacked underneath.
function itemRow(inline, refs) {
  return el("div", { style: { padding: "3px 0" } },
    el("div", { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
      ...inline),
    ...refs.map((r) => r.drawer));
}

// ── per-type renderers ───────────────────────────────────────────────────────

const SELECTABLE = new Set(["dubious_edge", "unattributed", "test_artifact", "orphan"]);

// A filterable, capped-scroll checkbox list. Opt-in: nothing selected initially.
// row(item) -> {cell, extra?}; filterText(item) -> string; onChange(count).
function selectableList(items, { row, filterText, onChange }) {
  const selected = new Set();
  const emit = () => onChange && onChange(selected.size);
  const rows = [];
  const listEl = el("div", { style: { maxHeight: "280px", overflowY: "auto", marginTop: "6px",
    border: "1px solid var(--surface-2, rgba(127,127,127,.2))", borderRadius: "8px", padding: "4px 6px" } });
  for (const item of items) {
    const ftext = String(filterText(item) || "");
    const cb = el("input", { type: "checkbox", name: "review-select",
      "aria-label": `select ${ftext || "row"}`, style: { marginRight: "8px", flex: "0 0 auto" },
      onchange: () => { cb.checked ? selected.add(item) : selected.delete(item); emit(); } });
    const { cell, extra } = row(item);
    const line = el("div", { style: { display: "flex", alignItems: "center", gap: "4px", padding: "2px 0" } }, cb, cell);
    const rowEl = el("div", {}, line, extra || null);
    rows.push({ row: rowEl, cb, item, text: ftext.toLowerCase() });
    listEl.appendChild(rowEl);
  }
  const filter = el("input", { type: "text", placeholder: "filter…", name: "review-filter",
    "aria-label": "filter selection", style: { fontSize: "12px", padding: "3px 7px" },
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

// Build the selectable list for a finding (edge rows vs entity-name rows).
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
      dubious_edge:  [["Keep", null, (s) => ({ kind: "bless", edges: s })],
                      ["Prune", "danger", (s) => ({ kind: "prune", edges: s })]],
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

function body(f, onAct) {
  switch (f.type) {
    case "merge_candidate": return (f.merges || []).map((m) => mergeItem(m, onAct));
    case "junk_candidate":  return (f.entities || []).map((j) => junkItem(j, onAct));
    case "proposed_link":   return (f.links || []).map((l) => linkItem(l, onAct));
    case "duplicate":       return dupItem(f, onAct);
    default:                return null;   // selectable findings render via findingRow
  }
}

function mergeItem(m, onAct) {
  const from = entityRef(m.from), into = entityRef(m.into);
  return itemRow([
    from.chip, dim("→"), into.chip, sim(m.similarity), dim(m.reason),
    btn("Merge", { onClick: () => onAct({ kind: "merge-entity", id: m.id, from: m.from, into: m.into }) }),
    btn("Reject", { kind: "ghost", onClick: () => onAct({ kind: "reject-entity", id: m.id }) }),
  ], [from, into]);
}

function junkItem(j, onAct) {
  const ref = entityRef(j.entity);
  return itemRow([
    ref.chip, dim(j.reason),
    btn("Delete", { kind: "danger", onClick: () => onAct({ kind: "junk-entity", id: j.id, entity: j.entity }) }),
    btn("Reject", { kind: "ghost", onClick: () => onAct({ kind: "reject-entity", id: j.id }) }),
  ], [ref]);
}

function linkItem(l, onAct) {
  const s = entityRef(l.src), d = entityRef(l.dst);
  return itemRow([
    s.chip, dim(`—${l.relation}→`), d.chip, sim(l.similarity), conf(l.confidence), dim(l.rationale),
    btn("Accept", { onClick: () => onAct({ kind: "accept-link", id: l.id }) }),
    btn("Reject", { kind: "ghost", onClick: () => onAct({ kind: "reject-link", id: l.id }) }),
  ], [s, d]);
}

function dupItem(f, onAct) {
  const [a, b] = f.entities || [];
  if (!a || !b) return null;
  const ra = entityRef(a), rb = entityRef(b);
  return itemRow([
    ra.chip, dim("↔"), rb.chip, f.score != null ? dim(`jaccard ${(+f.score).toFixed(2)}`) : null,
    btn("Merge", { onClick: () => onAct({ kind: "merge-named", from: a, into: b }) }),
    btn("Dismiss", { kind: "ghost", onClick: () => onAct({ kind: "dismiss-duplicate", a, b }) }),
  ], [ra, rb]);
}
