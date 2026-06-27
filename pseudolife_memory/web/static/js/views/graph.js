// views/graph.js — knowledge-graph explorer. A seed entity + depth drive a
// neighbourhood query; rendered via the shared graphview.js engine (canvas force
// sim / 3D galaxy / table). UX research: some users prefer tables to node-link.
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { badge } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn, colorFor } from "../graphview.js";

let state = { entity: "", depth: 1, view: "graph" };
let fg = null;

function parseHash() {
  const qi = location.hash.indexOf("?");
  const params = new URLSearchParams(qi >= 0 ? location.hash.slice(qi + 1) : "");
  return { entity: params.get("entity") || "", depth: parseInt(params.get("depth") || "1", 10) };
}

export async function renderGraph(root, ctx) {
  const fromHash = parseHash();
  if (fromHash.entity) state.entity = fromHash.entity;
  if (fromHash.depth) state.depth = fromHash.depth;

  const entityInput = el("input", { type: "text", value: state.entity, placeholder: "seed entity…",
    name: "entity", "aria-label": "seed entity", style: { maxWidth: "260px" },
    onkeydown: (e) => { if (e.key === "Enter") { state.entity = e.target.value.trim(); load(); } } });
  const depthSel = el("select", { name: "depth", "aria-label": "depth", style: { width: "auto" },
    onchange: (e) => { state.depth = parseInt(e.target.value, 10); load(); } },
    [1, 2, 3].map((d) => el("option", { value: d, selected: d === state.depth }, `depth ${d}`)));
  const goBtn = el("button", { class: "btn", onclick: () => { state.entity = entityInput.value.trim(); load(); } }, "Explore");
  const viewToggle = el("div", { class: "facets" },
    el("button", { class: "facet" + (state.view === "galaxy" ? " on" : ""), onclick: () => setView("galaxy") }, "galaxy"),
    el("button", { class: "facet" + (state.view === "graph" ? " on" : ""), onclick: () => setView("graph") }, "graph"),
    el("button", { class: "facet" + (state.view === "table" ? " on" : ""), onclick: () => setView("table") }, "table"));

  const host = el("div", {});
  mount(root,
    el("div", { class: "toolbar" }, entityInput, depthSel, goBtn, el("span", { class: "grow" }), viewToggle),
    host);

  function setView(v) {
    state.view = v;
    root.querySelectorAll(".facets .facet").forEach((f) => f.classList.toggle("on", f.textContent === v));
    paint(host._data);
  }

  async function load() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    mount(host, loadingBlock("Walking the graph…"));
    try {
      const data = await api.get("/api/graph", { entity: state.entity || undefined, depth: state.depth });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  function paint(data) {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    if (!data || data.found === false || !(data.nodes || []).length) {
      mount(host, emptyBlock("No graph here", state.entity ? `No relations around “${state.entity}”.` : "Enter a seed entity to explore."));
      return;
    }
    if (state.view === "table") { mount(host, tableView(data)); return; }
    if (state.view === "galaxy") {
      renderGalaxy(host, data, { colorBy: "community",
        onNodeClick: (id) => { location.hash = "#/graph?entity=" + encodeURIComponent(id) + "&depth=" + state.depth; } });
      return;
    }
    const wrap = el("div", { class: "graph-wrap" });
    const canvas = el("canvas", {});
    wrap.appendChild(canvas);
    mount(host, wrap);
    fg = new ForceGraph(canvas, wrap, data, { seed: state.entity, onSelect: (node) => showNode(wrap, node) });
    wrap.appendChild(zoomControls(fg));
    const etypes = [...new Set((data.nodes || []).map((n) => n.etype).filter(Boolean))];
    wrap.appendChild(legend(etypes));
    wrap.appendChild(el("div", { class: "graph-hint" }, "scroll to zoom · drag background to pan · click a node"));
    wrap.appendChild(fullscreenBtn(wrap));
  }

  function showNode(wrap, node) {
    wrap.querySelector(".node-panel")?.remove();
    const panel = el("div", { class: "node-panel" },
      el("div", { class: "np-head" },
        el("span", { class: "sw", style: { width: "10px", height: "10px", borderRadius: "50%", background: colorFor(node.etype) } }),
        el("span", { class: "name" }, node.entity),
        el("button", { class: "x", title: "close", onclick: () => panel.remove() }, "✕")),
      el("div", { class: "np-body" },
        node.etype ? el("div", { style: { marginBottom: "8px" } }, badge(node.etype)) : null,
        (node.facts || []).length
          ? (node.facts || []).map((f) => el("div", { class: "np-fact" },
              el("div", { class: "a" }, f.attribute), el("div", {}, f.value)))
          : el("div", { class: "dim", style: { fontSize: ".84rem" } }, "no canonical facts"),
        el("div", { style: { display: "flex", gap: "8px", marginTop: "12px" } },
          el("button", { class: "btn sm primary", onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(node.entity) + "&depth=" + state.depth; } }, "Re-center here"),
          el("button", { class: "btn sm", onclick: () => { location.hash = "#/cortex"; } }, "Facts ↗"))));
    wrap.appendChild(panel);
  }

  load();
}
