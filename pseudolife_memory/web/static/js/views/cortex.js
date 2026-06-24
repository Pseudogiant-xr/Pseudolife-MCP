// views/cortex.js — canonical fact review, grouped by entity, with provenance,
// contested-fact resolution, and a per-slot history timeline drawer.
import { el, mount, clear, fmtAge, fmtTime, titleCase, loadingBlock, emptyBlock, errorBlock, debounce } from "../util.js";
import { api } from "../api.js";
import { openDrawer, setDrawerBody, toast, confirmDialog } from "../ui.js";
import { originBadge, confMeter, searchBox, facetBar, badge } from "../components.js";

const TONE = "var(--c-cortex)";
let state = { q: "", origin: "all", data: null };

export async function renderCortex(root, ctx) {
  mount(root, loadingBlock("Reading the cortex…"));
  try {
    state.data = await api.get("/api/facts", { limit: 1000 });
  } catch (err) { mount(root, errorBlock(err)); return; }

  const list = el("div", { class: "fact-list" });
  const note = el("span", { class: "count-note" });
  const toolbar = el("div", { class: "toolbar" },
    searchBox("Filter facts by entity, attribute or value…", debounce((v) => { state.q = v; paint(); }, 150), state.q),
    facetBar([{ value: "all", label: "all origins" }, { value: "user", label: "user" },
              { value: "action", label: "action" }, { value: "agent", label: "agent" }],
             state.origin, (v) => { state.origin = v; paintFacets(); paint(); }),
    note);

  mount(root, toolbar, list);

  function paintFacets() {
    toolbar.querySelectorAll(".facet").forEach((f) => {
      f.classList.toggle("on", f.textContent === (state.origin === "all" ? "all origins" : state.origin));
    });
  }
  function paint() {
    const entries = (state.data.entries || []).filter(matches);
    note.textContent = `${entries.length} fact${entries.length === 1 ? "" : "s"}` +
      (entries.length !== (state.data.entries || []).length ? ` of ${state.data.entries.length}` : "");
    const groups = groupByEntity(entries);
    clear(list);
    if (!groups.length) { list.appendChild(emptyBlock("No matching facts", "Try a different filter.")); return; }
    for (const g of groups) list.appendChild(entityCard(g, ctx));
  }
  paint();
}

function matches(f) {
  if (state.origin !== "all" && String(f.origin || "agent").toLowerCase() !== state.origin) return false;
  if (!state.q) return true;
  const hay = `${f.entity} ${f.attribute} ${f.value}`.toLowerCase();
  return hay.includes(state.q.toLowerCase());
}

function groupByEntity(entries) {
  const m = new Map();
  for (const f of entries) {
    if (!m.has(f.entity)) m.set(f.entity, []);
    m.get(f.entity).push(f);
  }
  return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([entity, facts]) => ({ entity, facts: facts.sort((x, y) => String(x.attribute).localeCompare(String(y.attribute))) }));
}

function entityCard(g, ctx) {
  const contested = g.facts.filter((f) => f.contested).length;
  return el("div", { class: "panel entity-card reveal" },
    el("div", { class: "entity-head" },
      el("span", { class: "nav-dot", style: { "--dot": TONE } }),
      el("span", { class: "name" }, g.entity),
      contested ? badge(`${contested} contested`, "contested") : null,
      el("span", { class: "spacer" }),
      el("button", { class: "btn sm", title: "Open in graph",
        onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(g.entity); } }, "graph ↗"),
      el("span", { class: "n", style: { marginLeft: "10px" } }, `${g.facts.length} fact${g.facts.length === 1 ? "" : "s"}`)),
    g.facts.map((f) => factRow(f, ctx)));
}

function factRow(f, ctx) {
  const row = el("div", { class: "fact-row" + (f.contested ? " is-contested" : ""),
    onclick: () => openHistory(f) },
    el("div", { class: "fact-attr" }, f.attribute),
    el("div", { class: "fact-val" }, f.value),
    el("div", { class: "fact-side" },
      originBadge(f.origin),
      confMeter(f.confidence, TONE),
      el("span", { class: "fact-age", title: f.tx_time ? fmtTime(f.tx_time) : "" }, f.age || (f.tx_time ? fmtAge(f.tx_time) : "")),
      el("span", { class: "hist-ico" }, "history ↗")));
  if (f.contested) {
    row.appendChild(el("div", { class: "contender", onclick: (e) => e.stopPropagation() },
      el("span", { class: "dim" }, "contender:"),
      el("span", { class: "cv" }, f.contender_value ?? "(value)"),
      originBadge(f.contender_origin || "agent"),
      el("span", { class: "spacer", style: { marginLeft: "auto" } }),
      el("button", { class: "btn sm primary", onclick: () => resolve(f, true, ctx) }, "Accept"),
      el("button", { class: "btn sm", onclick: () => resolve(f, false, ctx) }, "Discard")));
  }
  return row;
}

async function resolve(f, accept, ctx) {
  if (!(await confirmDialog({
    title: accept ? "Adopt the contender?" : "Discard the contender?",
    message: accept
      ? `Set ${f.entity}.${f.attribute} = "${f.contender_value}" (the current value is kept as history).`
      : `Keep ${f.entity}.${f.attribute} = "${f.value}" and retire the parked contender.`,
    confirmLabel: accept ? "Adopt" : "Discard", danger: !accept }))) return;
  try {
    await api.post("/api/facts/resolve", { entity: f.entity, attribute: f.attribute, accept });
    toast(accept ? "Contender adopted" : "Contender discarded", "ok");
    ctx.refresh();
  } catch (e) { toast("Resolve failed: " + e.message, "bad"); }
}

async function openHistory(f) {
  openDrawer({ title: `${f.entity} · ${f.attribute}`, accent: TONE, body: loadingBlock("Tracing history…") });
  try {
    const h = await api.get("/api/facts/history", { entity: f.entity, attribute: f.attribute });
    const versions = (h.versions || []).slice().reverse(); // newest first
    setDrawerBody(el("div", {},
      el("p", { class: "dim", style: { marginTop: 0 } },
        `${h.count || versions.length} version${(h.count || versions.length) === 1 ? "" : "s"} at this slot.`),
      versions.length
        ? el("div", { class: "timeline" }, versions.map((v) => el("div", { class: "tl-item" + (v.status === "current" ? " current" : "") },
            el("div", { class: "tl-val" + (v.status === "current" ? "" : " super") }, v.value),
            el("div", { class: "tl-meta" },
              badge(v.status || "—", v.status === "current" ? "" : "agent"),
              v.writer_id ? el("span", {}, "by " + v.writer_id) : null,
              v.age ? el("span", {}, v.age) : (v.tx_time ? el("span", {}, fmtAge(v.tx_time)) : null)))))
        : emptyBlock("No version history")));
  } catch (e) { setDrawerBody(errorBlock(e)); }
}
