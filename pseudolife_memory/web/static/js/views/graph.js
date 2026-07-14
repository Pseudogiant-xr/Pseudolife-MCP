// views/graph.js — the unified knowledge-graph surface. Two modes over the one
// shared renderer (graphview.js):
//   • Overview — the WHOLE graph, no seed, coloured by project (all scopes) or
//     community (one project), with a Review queue of graph-hygiene findings.
//   • Explore — a seed entity + depth neighbourhood, coloured by entity type.
// The mode is a pure function of the hash so deep links and the mode toggle
// share one code path:
//   #/graph                     → Overview (whole map)
//   #/graph?entity=X            → Explore X's neighbourhood
//   #/graph?mode=explore        → Explore (empty seed prompt)
//   #/atlas , #/atlas?entity=X  → Overview (X highlighted) — legacy aliases
import { el, mount, fmtNum, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { ForceGraph, renderGalaxy, cleanupGalaxy, tableView, legend,
         zoomControls, fullscreenBtn } from "../graphview.js";
import { reviewPanel } from "../atlas_review.js";
import { openWikiPanel } from "./wiki_page.js";
import { confirmDialog, openModal, closeModal, toast } from "../ui.js";

// Middle-ellipsis a long name so a modal button label can't overflow/clip.
const ellipsisMid = (s, max = 22) =>
  s.length <= max ? s : `${s.slice(0, max - 9)}…${s.slice(-8)}`;

// `view` (map/galaxy/table) and `review` persist across mode switches; the rest
// is re-derived from the hash on every render.
let state = { mode: "overview", scope: "all", entity: "", depth: 1, view: "map", review: false };
let reviewData = null;
let fg = null;

function parseHash() {
  const h = location.hash || "";
  const base = h.replace(/^#\/?/, "").split("?")[0].split("/")[0] || "graph";
  const qi = h.indexOf("?");
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  const entity = p.get("entity") || "";
  const m = p.get("mode");
  const mode = (m === "explore" || m === "overview") ? m
             : (base === "graph" && entity) ? "explore" : "overview";
  return { mode, entity, scope: p.get("scope") || "all",
           depth: parseInt(p.get("depth") || "1", 10) || 1 };
}

// Build a hash for the current state in a given mode (drives re-render).
function hashFor(mode) {
  const p = new URLSearchParams();
  p.set("mode", mode);
  if (state.entity) p.set("entity", state.entity);
  if (mode === "overview" && state.scope && state.scope !== "all") p.set("scope", state.scope);
  if (mode === "explore" && state.depth && state.depth !== 1) p.set("depth", String(state.depth));
  return "#/graph?" + p.toString();
}

export async function renderGraph(root, ctx) {
  const h = parseHash();
  state.mode = h.mode;
  state.entity = h.entity;
  state.scope = h.scope;
  state.depth = h.depth;

  const host = el("div", {});
  const reviewHost = el("div", { style: { marginBottom: "12px", display: "none" } });
  const toolbar = el("div", { class: "toolbar" });
  mount(root, toolbar, reviewHost, host);

  const modeToggle = facetBar(
    [{ value: "overview", label: "Overview" }, { value: "explore", label: "Explore" }],
    state.mode, (m) => { if (m !== state.mode) location.hash = hashFor(m); });
  const viewToggle = facetBar(
    [{ value: "map", label: "map" }, { value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });

  // ── shared painting ───────────────────────────────────────────────────────
  function goExplore(id) { state.entity = id; state.mode = "explore"; location.hash = hashFor("explore"); }

  function paint(data) {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    const nodes = (data && data.nodes) || [];
    if (!data || data.found === false || !nodes.length) {
      mount(host, emptyState(data));
      return;
    }
    const colorBy = state.mode === "explore" ? "etype"
                  : (state.scope === "all" ? "project" : "community");
    const shown = nodes.length;
    // Only the whole-graph overview is capped; flag it so missing nodes read as
    // a readability cap, not data loss.
    const banner = (state.mode === "overview" && data.truncated)
      ? el("div", { class: "chip warn", style: { display: "inline-flex", marginBottom: "10px" } },
          `showing the ${fmtNum(shown)} most-connected of ${fmtNum(data.total_nodes ?? shown)} entities`
          + (state.scope === "all" ? " — pick a project to map it in full" : " — refine the scope to see more"))
      : null;
    const viewHost = el("div", {});
    mount(host, banner, viewHost);

    if (state.view === "table") { mount(viewHost, tableView(data)); return; }
    if (state.view === "galaxy") {
      renderGalaxy(viewHost, data, { colorBy, onNodeClick: (id) => goExplore(id) });
      return;
    }
    const wrap = el("div", { class: "graph-wrap" });
    const canvas = el("canvas", {});
    wrap.appendChild(canvas);
    mount(viewHost, wrap);
    fg = new ForceGraph(canvas, wrap, data, { seed: state.entity, colorBy,
      onSelect: (node) => showNode(wrap, node) });
    wrap.appendChild(zoomControls(fg));
    const etypes = state.mode === "explore"
      ? [...new Set(nodes.map((n) => n.etype).filter(Boolean))] : [];
    wrap.appendChild(legend(etypes, state.mode === "explore" ? "etype" : colorBy));
    wrap.appendChild(el("div", { class: "graph-hint" },
      state.mode === "explore"
        ? "scroll to zoom · drag background to pan · click a node"
        : `${fmtNum(shown)} entities · scroll to zoom · drag to pan · click a node`));
    wrap.appendChild(fullscreenBtn(wrap));
  }

  function emptyState(data) {
    if (state.mode === "explore") {
      return state.entity
        ? emptyBlock("No graph here", `No relations around “${state.entity}”.`)
        : emptyBlock("Explore the graph", "Enter a seed entity above, or switch to Overview for the whole map.");
    }
    return emptyBlock("No graph in scope",
      state.scope === "all" ? "The graph is empty." : `No attributed entities in “${state.scope}”.`);
  }

  // The wiki page replaces the old inline node-panel (Atlas stage 1).
  function showNode(wrap, node) {
    openWikiPanel(wrap, node.entity, { onExplore: (id) => goExplore(id) });
  }

  // ── Overview: whole graph + scope + Review ────────────────────────────────
  async function loadOverview() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    mount(host, loadingBlock("Mapping the graph…"));
    try {
      const data = await api.get("/api/graph", { scope: state.scope });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  async function loadReview() {
    mount(reviewHost, loadingBlock("Scanning the graph…"));
    try {
      reviewData = await api.get("/api/graph/review", { scope: state.scope });
      mount(reviewHost, reviewPanel(reviewData, (f) => actOnFinding(f)));
    } catch (err) { mount(reviewHost, errorBlock(err)); }
  }

  async function refreshAfterMutation() {
    if (state.review) await loadReview();
    await loadOverview();
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
  async function actOnFinding(d) {
    if (d.kind === "merge-named") {                       // duplicate (recomputed, by name)
      const nameRow = (lead, name) => el("div", { style: { display: "flex", gap: "8px", margin: "3px 0" } },
        el("span", { class: "dim", style: { flex: "0 0 auto" } }, lead),
        el("span", { class: "mono", style: { wordBreak: "break-all" } }, name));
      openModal({
        title: "Merge duplicate entities",
        body: el("div", {},
          el("p", { class: "dim", style: { marginTop: 0 } },
            "One entity absorbs the other's edges, aliases and project tags. Which name should survive?"),
          nameRow("A", d.from), nameRow("B", d.into)),
        actions: [
          { label: "Cancel", onClick: closeModal },
          { label: `Keep A — “${ellipsisMid(d.from)}”`, kind: "primary", onClick: async () => { closeModal();
            await postAll([{ path: "/api/graph/merge", body: { from: d.into, into: d.from } }], "Merged"); } },
          { label: `Keep B — “${ellipsisMid(d.into)}”`, onClick: async () => { closeModal();
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
    if (d.kind === "dismiss-duplicate") {                 // duplicate: genuinely-distinct verdict
      // Permanent: the pair never resurfaces as a duplicate finding or
      // deep-dream candidate — worth one confirm, unlike ordinary rejects.
      if (!(await confirmDialog({ title: "Mark as distinct",
        message: `Record that “${d.a}” and “${d.b}” are genuinely different things? The pair stops resurfacing as a duplicate finding — permanently.`,
        confirmLabel: "Mark distinct" }))) return;
      await postAll([{ path: "/api/graph/dismiss-duplicate", body: { a: d.a, b: d.b } }], "Dismissed");
      return;
    }
    if (d.kind === "bless") {                             // dubious_edge (bulk): human "Keep"
      const edges = d.edges || [];
      await postAll(edges.map((e) => ({ path: "/api/graph/bless-edge",
        body: { src: e.src, relation: e.relation, dst: e.dst } })), "Kept");
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
    if (d.kind === "delete-names") {                      // test_artifact / orphan (bulk, by name)
      const ents = d.entities || [];
      if (!(await confirmDialog({ title: "Delete entities", danger: true,
        message: `Permanently delete ${ents.length} entit${ents.length === 1 ? "y" : "ies"} and their edges? This cannot be undone.` }))) return;
      await postAll(ents.map((e) => ({ path: "/api/graph/delete-entity", body: { entity: e } })), "Deleted");
      return;
    }
    if (d.kind === "assign") {                            // unattributed / orphan (bulk)
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

  // ── Explore: seed + depth neighbourhood ───────────────────────────────────
  async function loadExplore() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    if (!state.entity) { paint(null); return; }   // empty seed → prompt, no fetch
    mount(host, loadingBlock("Walking the graph…"));
    try {
      const data = await api.get("/api/graph", { entity: state.entity, depth: state.depth });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  // ── toolbar wiring per mode ───────────────────────────────────────────────
  if (state.mode === "overview") {
    let projects = [];
    try { projects = (await api.get("/api/graph/projects")).projects || []; } catch { /* non-fatal */ }
    const scopeOpts = [{ value: "all", label: "all projects" }]
      .concat(projects.map((p) => ({ value: p.source, label: `${p.source} (${p.entities})` })));
    const switcher = facetBar(scopeOpts, state.scope,
      (v) => { state.scope = v; loadOverview(); if (state.review) loadReview(); });
    const reviewBtn = el("button", { class: "facet" + (state.review ? " on" : ""),
      onclick: () => { state.review = !state.review; reviewBtn.classList.toggle("on", state.review);
        reviewHost.style.display = state.review ? "" : "none"; if (state.review) loadReview(); } },
      "Review");
    mount(toolbar, modeToggle, el("span", { class: "eyebrow" }, "scope"), switcher,
      el("span", { class: "grow" }), reviewBtn, viewToggle);
    reviewHost.style.display = state.review ? "" : "none";
    if (state.review) loadReview();
    loadOverview();
  } else {
    const entityInput = el("input", { type: "text", value: state.entity, placeholder: "seed entity…",
      name: "entity", "aria-label": "seed entity", style: { maxWidth: "260px" },
      onkeydown: (e) => { if (e.key === "Enter") { state.entity = e.target.value.trim(); location.hash = hashFor("explore"); } } });
    const depthSel = el("select", { name: "depth", "aria-label": "depth", style: { width: "auto" },
      onchange: (e) => { state.depth = parseInt(e.target.value, 10); location.hash = hashFor("explore"); } },
      [1, 2, 3].map((d) => el("option", { value: d, selected: d === state.depth }, `depth ${d}`)));
    const goBtn = el("button", { class: "btn",
      onclick: () => { state.entity = entityInput.value.trim(); location.hash = hashFor("explore"); } }, "Explore");
    mount(toolbar, modeToggle, entityInput, depthSel, goBtn, el("span", { class: "grow" }), viewToggle);
    loadExplore();
  }
}
