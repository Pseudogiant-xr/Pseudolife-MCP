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
import { confirmDialog, openModal, closeModal, toast } from "../ui.js";

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

  // The review panel calls this with a normalized action descriptor {kind, …}.
  // Per-item proposal actions hit the id-keyed accept/reject endpoints; the
  // group analyzer findings keep their bulk modals.
  async function actOnFinding(d) {
    if (d.kind === "merge-named") {                       // duplicate (recomputed, by name)
      openModal({
        title: "Merge duplicate entities",
        body: el("div", {}, el("p", { class: "dim", style: { marginTop: 0 } },
          "One entity absorbs the other's edges, aliases and project tags. Which name should survive?")),
        actions: [
          { label: "Cancel", onClick: closeModal },
          { label: `Keep “${d.from}”`, kind: "primary", onClick: async () => { closeModal();
            await postAll([{ path: "/api/graph/merge", body: { from: d.into, into: d.from } }], "Merged"); } },
          { label: `Keep “${d.into}”`, onClick: async () => { closeModal();
            await postAll([{ path: "/api/graph/merge", body: { from: d.from, into: d.into } }], "Merged"); } },
        ],
      });
      return;
    }
    if (d.kind === "merge-entity") {                      // merge_candidate proposal
      if (!(await confirmDialog({ title: "Merge entities",
        message: `Fold “${d.from}” into “${d.into}”? Its edges, aliases and project tags move to “${d.into}”.` }))) return;
      await postAll([{ path: "/api/graph/accept-entity-merge", body: { id: d.id } }], "Merged");
      return;
    }
    if (d.kind === "junk-entity") {                       // junk_candidate proposal
      if (!(await confirmDialog({ title: "Delete entity", danger: true,
        message: `Permanently delete “${d.entity}” and its edges? This cannot be undone.` }))) return;
      await postAll([{ path: "/api/graph/accept-entity-junk", body: { id: d.id } }], "Deleted");
      return;
    }
    if (d.kind === "reject-entity") {                     // dismiss a merge/junk proposal
      await postAll([{ path: "/api/graph/reject-entity-proposal", body: { id: d.id } }], "Dismissed");
      return;
    }
    if (d.kind === "accept-link") {                       // proposed_link → real edge
      await postAll([{ path: "/api/graph/accept-proposal", body: { id: d.id } }], "Linked");
      return;
    }
    if (d.kind === "reject-link") {
      await postAll([{ path: "/api/graph/reject-proposal", body: { id: d.id } }], "Dismissed");
      return;
    }
    if (d.kind === "prune") {                             // dubious_edge (bulk)
      const edges = d.edges || [];
      if (!(await confirmDialog({ title: "Prune edges", danger: true,
        message: `Remove ${edges.length} low-confidence inferred edge${edges.length === 1 ? "" : "s"}?` }))) return;
      await postAll(edges.map((e) => ({ path: "/api/graph/unrelate",
        body: { src: e.src, relation: e.relation, dst: e.dst } })), "Pruned");
      return;
    }
    if (d.kind === "delete-names") {                      // test_artifact (bulk, by name)
      const ents = d.entities || [];
      if (!(await confirmDialog({ title: "Delete entities", danger: true,
        message: `Permanently delete ${ents.length} entit${ents.length === 1 ? "y" : "ies"} and their edges? This cannot be undone.` }))) return;
      await postAll(ents.map((e) => ({ path: "/api/graph/delete-entity", body: { entity: e } })), "Deleted");
      return;
    }
    if (d.kind === "assign") {                            // unattributed (bulk)
      const ents = d.entities || [];
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
