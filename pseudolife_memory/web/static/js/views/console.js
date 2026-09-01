// views/console.js — the knobs & dials editor over /api/config. Type-aware
// controls, live-vs-restart badges, dirty tracking, diff-preview, atomic save.
// Headlined by the Dreamer card: effective extractor resolution (from
// /api/dream/status) + a one-click model picker over the model-only
// override knob (memory.dream.extractor_model_override).
import { el, mount, clear, loadingBlock, errorBlock, fmtAge } from "../util.js";
import { api } from "../api.js";
import { openModal, closeModal, toast } from "../ui.js";
import { badge } from "../components.js";

let cfg = null;
let dream = null;        // /api/dream/status snapshot for the Dreamer card
let edits = new Map();   // path -> new value
let originals = new Map();
let viewCtx = null;      // the render ctx (for refresh) — module-scoped so the
                         // save bar can be rebuilt on edit without losing it.

const OVERRIDE_PATH = "memory.dream.extractor_model_override";
const EFFORT_PATH = "memory.dream.extractor_reasoning_effort";
// Common levels every provider understands; the Extractor panel's knob takes
// the provider-specific extras (codex "minimal", claude "max") as free text.
const DREAMER_EFFORTS = ["low", "medium", "high", "xhigh"];
const DREAMER_MODELS = [
  { id: "claude-opus-5", label: "Opus 5",
    note: "recommended — best measured extraction quality" },
  { id: "claude-sonnet-5", label: "Sonnet 5", note: "balanced" },
  { id: "claude-haiku-4-5", label: "Haiku 4.5",
    note: "fastest / lightest on plan usage" },
  { id: "claude-fable-5", label: "Fable 5", note: "most capable tier" },
  // GPT-5.6 family: served per request by the Codex CLI shim
  // (evals/codex_shim.py, :8086) or any OpenAI-compatible endpoint that
  // knows these ids. Extraction quality unmeasured here — the ladder has
  // only measured the Claude models and the local sidecars.
  { id: "gpt-5.6-sol", label: "Sol", note: "OpenAI flagship — unmeasured here" },
  { id: "gpt-5.6-terra", label: "Terra", note: "OpenAI balanced — unmeasured here" },
  { id: "gpt-5.6-luna", label: "Luna", note: "OpenAI fastest — unmeasured here" },
];

export async function renderConsole(root, ctx) {
  viewCtx = ctx;
  mount(root, loadingBlock("Loading configuration…"));
  try {
    // The card degrades gracefully: a dream-status failure hides it rather
    // than blocking the whole config editor.
    [cfg, dream] = await Promise.all([
      api.get("/api/config"),
      api.get("/api/dream/status").catch(() => null),
    ]);
  }
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
    dreamerCard(),
    groups, savebar);

  refreshSaveBar(savebar);
}

// ── Dreamer hero card ──────────────────────────────────────────────────────

function dreamerCard() {
  if (!dream || !dream.primary_url) return null;
  const override = dream.model_override || null;
  // A launch-default alias ("extractor"/"bench") hides the real model; the
  // daemon resolves it from the endpoint's /v1/models. Show the concrete
  // model and keep the alias in the tooltip.
  const served = dream.primary_model_served;
  const aliasResolved = served && served !== dream.primary_model;
  const chips = [
    el("span", { class: "chip", title: aliasResolved
        ? `configured name "${dream.primary_model}" is a launch-default alias — `
          + "resolved from the endpoint's /v1/models"
        : "effective primary endpoint → model" },
      el("span", { class: "k" }, "primary"),
      ` ${dream.primary_url} → `,
      el("span", { class: "mono" }, (aliasResolved ? served : dream.primary_model) || "?")),
    healthChip(),
  ];
  if (dream.fallback_url) {
    chips.push(el("span", { class: "chip", title: "effective fallback endpoint → model" },
      el("span", { class: "k" }, "fallback"),
      ` ${dream.fallback_url} → `,
      el("span", { class: "mono" }, dream.fallback_model || "?")));
  }
  const last = dream.last_dream_extractor;
  if (last) {
    chips.push(el("span", { class: last.which === "fallback" ? "chip warn" : "chip" },
      el("span", { class: "k" }, "last dream"),
      ` ${last.which}${last.at ? " · " + fmtAge(last.at) : ""}`));
  }
  chips.push(el("span", { class: "chip",
    title: "who owns the endpoint settings (Extractor panel below); the model picker wins over both" },
    el("span", { class: "k" }, "settings"), " " + (dream.extractor_source || "env")));

  const segs = DREAMER_MODELS.map((m) =>
    el("button", {
      class: "seg" + (override === m.id ? " active" : ""),
      "aria-pressed": String(override === m.id), title: m.note,
      onclick: () => setDreamerModel(m.id),
    }, m.label));
  segs.push(el("button", {
    class: "seg" + (!override ? " active" : ""),
    "aria-pressed": String(!override),
    title: "clear the override — the endpoint's own default model serves",
    onclick: () => setDreamerModel(null),
  }, "Default"));
  const customInput = el("input", { type: "text", placeholder: "model id…",
    "aria-label": "custom dreamer model",
    value: override && !DREAMER_MODELS.some((m) => m.id === override) ? override : "" });
  customInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && customInput.value.trim()) setDreamerModel(customInput.value.trim());
  });

  const effort = dream.reasoning_effort || null;
  const effortSegs = [el("button", {
    class: "seg" + (!effort ? " active" : ""),
    "aria-pressed": String(!effort),
    title: "clear — the endpoint's own default effort serves "
      + "(for the CLI shims, the host CLI config)",
    onclick: () => setDreamerEffort(null),
  }, "Default")];
  for (const lv of DREAMER_EFFORTS) {
    effortSegs.push(el("button", {
      class: "seg" + (effort === lv ? " active" : ""),
      "aria-pressed": String(effort === lv),
      title: `pin reasoning_effort=${lv} on every primary extractor call`,
      onclick: () => setDreamerEffort(lv),
    }, lv));
  }
  if (effort && !DREAMER_EFFORTS.includes(effort)) {
    // A provider extra ("minimal"/"max") set via the Extractor panel —
    // surface it as the active state so the row never hides its own knob.
    effortSegs.push(el("button", { class: "seg active",
      "aria-pressed": "true", disabled: true,
      title: "custom effort — set via the Extractor panel below",
    }, effort));
  }

  return el("div", { class: "panel dreamer reveal" },
    el("div", { class: "panel-head" },
      el("h2", {}, "Dreamer"),
      el("span", { class: "sub" }, "which model consolidates memories")),
    el("div", { class: "panel-body" },
      el("div", { class: "dreamer-chips" }, chips),
      el("div", { class: "dreamer-pick" },
        el("span", { class: "lbl" }, "Model"),
        el("div", { class: "seg-row", role: "group", "aria-label": "dreamer model" }, segs),
        el("span", { class: "with-custom" }, customInput,
          el("button", { class: "btn", onclick: () => {
            const v = customInput.value.trim();
            if (v) setDreamerModel(v);
          } }, "Apply"))),
      el("div", { class: "dreamer-pick" },
        el("span", { class: "lbl" }, "Effort"),
        el("div", { class: "seg-row", role: "group", "aria-label": "dreamer reasoning effort" },
          effortSegs)),
      el("p", { class: "help", style: { margin: "8px 0 0" } },
        "Applies live to the next dream via the model-only override — endpoint "
        + "wiring keeps its owner. Any model id the wired endpoint serves "
        + "works here (LM Studio / Ollama / vLLM model names included). The "
        + "Claude CLI shim honours claude-* names per request, the Codex CLI "
        + "shim gpt-* names; the local sidecar ignores model names. Effort "
        + "rides each request as reasoning_effort — the CLI shims map it to "
        + "their effort flag, most local runtimes ignore the unknown field, "
        + "and the fallback sidecar is never affected (provider extras like "
        + "\"minimal\"/\"max\" via the Extractor panel below).")));
}

function healthChip() {
  if (dream.primary_healthy === true) return el("span", { class: "chip ok" }, "primary ✓");
  if (dream.primary_healthy === false) return el("span", { class: "chip bad" }, "primary DOWN");
  return el("span", { class: "chip", title: "single-extractor deploy — no probe on status" }, "no probe");
}

async function setDreamerModel(value) {
  if (edits.size) {
    toast("Save or discard the pending edits below first", "warn");
    return;
  }
  const current = dream?.model_override || null;
  if ((value || null) === current) return;
  try {
    await api.post("/api/config", { patch: { [OVERRIDE_PATH]: value } });
    toast(value
      ? `Saved · next dream extracts with ${value}`
      : "Saved · override cleared — endpoint default serves", "ok", 6000);
    viewCtx?.refresh();
  } catch (e) { toast("Save failed: " + e.message, "bad"); }
}

async function setDreamerEffort(value) {
  if (edits.size) {
    toast("Save or discard the pending edits below first", "warn");
    return;
  }
  const current = dream?.reasoning_effort || null;
  if ((value || null) === current) return;
  try {
    await api.post("/api/config", { patch: { [EFFORT_PATH]: value } });
    toast(value
      ? `Saved · next dream extracts at ${value} effort`
      : "Saved · effort cleared — endpoint default serves", "ok", 6000);
    viewCtx?.refresh();
  } catch (e) { toast("Save failed: " + e.message, "bad"); }
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
