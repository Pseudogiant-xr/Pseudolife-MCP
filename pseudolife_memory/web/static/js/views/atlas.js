// views/atlas.js — the Atlas overview: the WHOLE graph, no seed needed, with a
// project/topic switcher. scope=all colours nodes by project; a single project
// colours by community. Reuses the shared renderer in graphview.js. (The review
// queue + cleanup actions are Stage 3 — not here.)
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn } from "../graphview.js";

let state = { scope: "all", view: "map" };
let fg = null;

function parseHash() {
  const qi = location.hash.indexOf("?");
  const p = new URLSearchParams(qi >= 0 ? location.hash.slice(qi + 1) : "");
  return { scope: p.get("scope") || "all", entity: p.get("entity") || "" };
}

export async function renderAtlas(root, ctx) {
  const fromHash = parseHash();
  if (fromHash.scope) state.scope = fromHash.scope;
  const seed = fromHash.entity || "";

  mount(root, loadingBlock("Mapping the graph…"));
  let projects = [];
  try { projects = (await api.get("/api/graph/projects")).projects || []; }
  catch (err) { mount(root, errorBlock(err)); return; }

  const host = el("div", {});
  const scopeOpts = [{ value: "all", label: "all projects" }]
    .concat(projects.map((p) => ({ value: p.source, label: `${p.source} (${p.entities})` })));
  const switcher = facetBar(scopeOpts, state.scope, (v) => { state.scope = v; load(); });
  const viewToggle = facetBar(
    [{ value: "map", label: "map" }, { value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });

  mount(root,
    el("div", { class: "toolbar" },
      el("span", { class: "eyebrow" }, "scope"), switcher,
      el("span", { class: "grow" }), viewToggle),
    host);

  async function load() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    mount(host, loadingBlock("Mapping the graph…"));
    try {
      const data = await api.get("/api/graph", { scope: state.scope });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  function paint(data) {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    if (!data || !(data.nodes || []).length) {
      mount(host, emptyBlock("No graph in scope",
        state.scope === "all" ? "The graph is empty." : `No attributed entities in “${state.scope}”.`));
      return;
    }
    const colorBy = state.scope === "all" ? "project" : "community";
    if (state.view === "table") { mount(host, tableView(data)); return; }
    if (state.view === "galaxy") {
      renderGalaxy(host, data, { colorBy,
        onNodeClick: (id) => { location.hash = "#/graph?entity=" + encodeURIComponent(id); } });
      return;
    }
    const wrap = el("div", { class: "graph-wrap" });
    const canvas = el("canvas", {});
    wrap.appendChild(canvas);
    mount(host, wrap);
    fg = new ForceGraph(canvas, wrap, data, { seed, colorBy,
      onSelect: (node) => { location.hash = "#/graph?entity=" + encodeURIComponent(node.entity); } });
    wrap.appendChild(zoomControls(fg));
    wrap.appendChild(legend([], colorBy));
    wrap.appendChild(el("div", { class: "graph-hint" },
      `${(data.nodes || []).length} entities · scroll to zoom · drag to pan · click a node to explore`));
    wrap.appendChild(fullscreenBtn(wrap));
  }

  load();
}
