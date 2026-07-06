// views/console.js — the knobs & dials editor over /api/config. Type-aware
// controls, live-vs-restart badges, dirty tracking, diff-preview, atomic save.
import { el, mount, clear, loadingBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { openModal, closeModal, toast } from "../ui.js";
import { badge } from "../components.js";

let cfg = null;
let edits = new Map();   // path -> new value
let originals = new Map();
let viewCtx = null;      // the render ctx (for refresh) — module-scoped so the
                         // save bar can be rebuilt on edit without losing it.

export async function renderConsole(root, ctx) {
  viewCtx = ctx;
  mount(root, loadingBlock("Loading configuration…"));
  try { cfg = await api.get("/api/config"); }
  catch (err) { mount(root, errorBlock(err)); return; }

  edits = new Map();
  originals = new Map();
  for (const g of cfg.groups) for (const k of g.knobs) originals.set(k.path, k.value);

  const groups = el("div", { class: "knob-groups" }, cfg.groups.map(groupPanel));
  const savebar = el("div", { class: "savebar", style: { display: "none" } });
  mount(root,
    el("div", { class: "toolbar" },
      el("span", { class: "count-note" }, `${originals.size} knobs · `),
      el("span", { class: "chip", title: "config file on the daemon host" },
        el("span", { class: "k" }, "config"), " " + (cfg.config_path || "config.yaml"))),
    groups, savebar);

  refreshSaveBar(savebar);
}

function groupPanel(g) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, g.name)),
    el("div", { class: "panel-body" }, g.knobs.map((k) => knobRow(k))));
}

function knobRow(k) {
  const row = el("div", { class: "knob", dataset: { path: k.path } });
  row.appendChild(el("div", { class: "info" },
    el("div", { class: "lbl" }, k.label || k.path),
    k.help ? el("div", { class: "help" }, k.help) : null,
    el("div", { class: "kbadges" },
      el("span", { class: "badge mono", style: { opacity: ".7" } }, k.path.split(".").slice(-2).join(".")),
      k.restart ? badge("restart required", "restart") : badge("live", "live"))));
  row.appendChild(el("div", { class: "ctrl" }, control(k, row), defaultHint(k, row)));
  return row;
}

function control(k, row) {
  const onChange = (val) => setEdit(k, val, row);
  if (k.type === "bool") {
    const input = el("input", { type: "checkbox", checked: !!k.value, onchange: (e) => onChange(e.target.checked) });
    return el("label", { class: "switch" }, input, el("span", { class: "track" }));
  }
  if (k.type === "enum") {
    return el("select", { name: k.path, "aria-label": k.label,
      onchange: (e) => onChange(e.target.value) },
      (k.options || []).map((o) => el("option", { value: o, selected: o === k.value }, o)));
  }
  if (k.type === "string") {
    // Empty field = unset (null), so operators can clear a value. Suggestions
    // render as a datalist (freeform + common endpoints).
    const listId = k.suggestions?.length ? "dl-" + k.path.replace(/\./g, "-") : null;
    const input = el("input", { type: "text", value: k.value ?? "", name: k.path,
      "aria-label": k.label, ...(listId ? { list: listId } : {}),
      oninput: (e) => onChange(e.target.value.trim() === "" ? null : e.target.value.trim()) });
    if (!listId) return input;
    return el("span", { class: "with-datalist" }, input,
      el("datalist", { id: listId }, k.suggestions.map((s) => el("option", { value: s }))));
  }
  // int / float
  const step = k.type === "int" ? (k.step || 1) : (k.step || 0.01);
  return el("input", { type: "number", value: k.value, name: k.path, "aria-label": k.label,
    min: k.min, max: k.max, step,
    // An emptied field is "no edit", not a value — sending "" to the server
    // produced a raw float('') conversion error in the save toast.
    oninput: (e) => onChange(e.target.value === "" ? originals.get(k.path) : Number(e.target.value)) });
}

function defaultHint(k, row) {
  if (k.default == null) return null;
  return el("button", { class: "def", title: "reset to default",
    onclick: () => resetTo(k, row) }, "default: " + String(k.default));
}

function setEdit(k, val, row) {
  const orig = originals.get(k.path);
  if (val === orig || String(val) === String(orig)) edits.delete(k.path);
  else edits.set(k.path, val);
  row.classList.toggle("dirty", edits.has(k.path));
  refreshSaveBar(document.querySelector(".savebar"));
}

function resetTo(k, row) {
  // set the control back to default, registering an edit if default != current
  const ctrl = row.querySelector(".ctrl");
  if (k.type === "bool") { ctrl.querySelector("input").checked = !!k.default; }
  else if (k.type === "enum") { ctrl.querySelector("select").value = k.default; }
  else { ctrl.querySelector("input").value = k.default ?? ""; }
  setEdit(k, k.default, row);
}

function refreshSaveBar(bar) {
  bar = bar || document.querySelector(".savebar");
  if (!bar) return;
  if (!edits.size) { bar.style.display = "none"; clear(bar); return; }
  bar.style.display = "";
  clear(bar);
  bar.appendChild(el("span", { class: "n" }, `${edits.size} change${edits.size === 1 ? "" : "s"}`));
  const needsRestart = [...edits.keys()].some((p) => knobByPath(p)?.restart);
  if (needsRestart) bar.appendChild(badge("restart required", "restart"));
  bar.appendChild(el("span", { class: "spacer" }));
  bar.appendChild(el("button", { class: "btn", onclick: () => discardAll() }, "Discard"));
  bar.appendChild(el("button", { class: "btn primary", onclick: () => preview() }, "Review & save"));
}

function discardAll() {
  edits.clear();
  document.querySelectorAll(".knob.dirty").forEach((r) => {
    const k = knobByPath(r.dataset.path);
    const ctrl = r.querySelector(".ctrl");
    if (k.type === "bool") ctrl.querySelector("input").checked = !!k.value;
    else if (k.type === "enum") ctrl.querySelector("select").value = k.value;
    else ctrl.querySelector("input").value = k.value ?? "";
    r.classList.remove("dirty");
  });
  refreshSaveBar(document.querySelector(".savebar"));
}

function preview() {
  const rows = [...edits.entries()].map(([path, val]) => {
    const k = knobByPath(path);
    return el("div", { class: "diff-row" },
      el("span", { class: "p" }, path),
      el("span", { class: "old" }, String(originals.get(path))),
      el("span", {}, "→"),
      el("span", { class: "new" }, String(val)),
      k?.restart ? badge("restart", "restart") : null);
  });
  openModal({
    title: `Apply ${edits.size} config change${edits.size === 1 ? "" : "s"}?`,
    body: el("div", {},
      el("p", { class: "dim", style: { marginTop: 0 } },
        "Writes to config.yaml (atomic, with a timestamped backup). Live knobs take effect immediately; restart-flagged knobs apply on the next daemon restart."),
      el("div", {}, rows)),
    actions: [
      { label: "Cancel", onClick: closeModal },
      { label: "Save", kind: "primary", onClick: () => save() },
    ],
  });
}

async function save() {
  const patch = Object.fromEntries(edits);
  try {
    const res = await api.post("/api/config", { patch });
    closeModal();
    const parts = [];
    if (res.applied?.length) parts.push(`${res.applied.length} applied live`);
    if (res.restart_required?.length) parts.push(`${res.restart_required.length} need restart`);
    toast("Saved · " + (parts.join(" · ") || "ok"), "ok", 6000);
    if (res.backup) toast("Backup: " + res.backup.split(/[\\/]/).pop(), "info", 5000);
    viewCtx?.refresh();
  } catch (e) { toast("Save failed: " + e.message, "bad"); }
}

function knobByPath(path) {
  for (const g of cfg.groups) for (const k of g.knobs) if (k.path === path) return k;
  return null;
}
