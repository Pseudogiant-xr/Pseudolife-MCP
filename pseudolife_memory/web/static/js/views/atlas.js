// views/atlas.js — the Atlas overview: the WHOLE graph, no seed needed, with a
// project/topic switcher (scope=all colours by project; one project colours by
// community) and a Review queue of graph-hygiene findings with confirm-gated
// cleanup actions. Reuses the shared renderer in graphview.js.
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn } from "../graphview.js";
import { reviewPanel } from "../atlas_review.js";

let state = { scope: "all", view: "map", review: false };
let reviewData = null;
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
  const switcher = facetBar(scopeOpts, state.scope, (v) => { state.scope = v; load(); if (state.review) loadReview(); });
  const viewToggle = facetBar(
    [{ value: "map", label: "map" }, { value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });
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

  // Stub for Task 1; the confirm-gated dispatcher is added in Task 2.
  function actOnFinding(_f) {}

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
