// views/graph.js — the Atlas surface (stage 2): a full-bleed 3D galaxy with a
// wiki panel and the review drawer. The galaxy IS the map; `table` is the
// data/accessibility fallback (and the automatic one when WebGL/bundle fails).
//   #/graph                → galaxy, whole scoped graph
//   #/graph?entity=X       → galaxy + X's wiki page open, camera on X
//   #/graph?view=table     → table mode
//   legacy: #/atlas, mode=, depth= — routed here, extra params ignored.
import { el, mount, fmtNum, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { facetBar } from "../components.js";
import { tableView, fullscreenBtn } from "../graphview.js";
import { createGalaxy, destroyGalaxy } from "../galaxy.js";
import { openWikiPanel } from "./wiki_page.js";
import { reviewPanel } from "../atlas_review.js";
import { confirmDialog, openModal, closeModal, toast } from "../ui.js";

// Middle-ellipsis a long name so a modal button label can't overflow/clip.
const ellipsisMid = (s, max = 22) =>
  s.length <= max ? s : `${s.slice(0, max - 9)}…${s.slice(-8)}`;

let state = { scope: "all", view: "galaxy", entity: "", review: false, q: "" };
let galaxy = null;

function parseHash() {
  const h = location.hash || "";
  const qi = h.indexOf("?");
  const p = new URLSearchParams(qi >= 0 ? h.slice(qi + 1) : "");
  const entity = p.get("entity") || "";
  return { entity,
           scope: p.get("scope") || state.scope || "all",
           // an explicit entity deep link overrides a sticky table view — the
           // link's intent is "show me this entity", which needs the galaxy
           view: p.get("view") === "table" ? "table"
               : entity ? "galaxy" : state.view };
}

// Update the address bar without re-triggering the router (panel swaps and
// camera moves are in-surface navigation, not route changes).
function reflectHash() {
  const p = new URLSearchParams();
  if (state.entity) p.set("entity", state.entity);
  if (state.scope && state.scope !== "all") p.set("scope", state.scope);
  if (state.view === "table") p.set("view", "table");
  const qs = p.toString();
  history.replaceState(null, "", location.pathname + "#/graph" + (qs ? "?" + qs : ""));
}

export async function renderGraph(root, ctx) {
  const h = parseHash();
  state.entity = h.entity; state.scope = h.scope; state.view = h.view;

  const toolbar = el("div", { class: "toolbar" });
  const reviewHost = el("div", { class: "review-drawer", style: { display: "none" } });
  const host = el("div", {});
  mount(root, toolbar, reviewHost, host);

  // Every entity name a finding touches — lights up the matching stars.
  function flaggedNames(findings) {
    const out = new Set();
    const add = (v) => { if (typeof v === "string" && v) out.add(v); };
    for (const f of findings || []) {
      for (const e of f.entities || []) { add(e); add(e && e.entity); }
      for (const m of f.merges || []) { add(m.from); add(m.into); }
      for (const l of f.links || []) { add(l.src); add(l.dst); }
      for (const e of f.edges || []) { add(e.src); add(e.dst); }
    }
    return out;
  }

  async function lightFlags() {
    if (!galaxy) return;
    try {
      const rd = await api.get("/api/graph/review", { scope: state.scope });
      galaxy && galaxy.setFlagged(flaggedNames(rd.findings));
    } catch { /* non-fatal — stars just don't pulse */ }
  }

  // ── data + paint ──────────────────────────────────────────────────────────
  async function load() {
    destroyGalaxy(); galaxy = null;
    mount(host, loadingBlock("Charting the galaxy…"));
    try {
      const data = await api.get("/api/graph", { scope: state.scope });
      host._data = data;
      await paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  async function paint(data) {
    destroyGalaxy(); galaxy = null;
    const nodes = (data && data.nodes) || [];
    if (!data || data.found === false || !nodes.length) {
      mount(host, emptyBlock("No graph in scope",
        state.scope === "all" ? "The graph is empty." : `No attributed entities in “${state.scope}”.`));
      return;
    }
    const colorBy = state.scope === "all" ? "project" : "community";
    const banner = data.truncated
      ? el("div", { class: "chip warn", style: { display: "inline-flex", marginBottom: "10px" } },
          `showing the ${fmtNum(nodes.length)} most-connected of ${fmtNum(data.total_nodes ?? nodes.length)} entities`
          + (state.scope === "all" ? " — pick a project to map it in full" : ""))
      : null;
    const viewHost = el("div", {});
    mount(host, banner, viewHost);

    if (state.view === "table") { mount(viewHost, tableView(data)); reflectHash(); return; }

    const wrap = el("div", { class: "graph-wrap galaxy" });
    mount(viewHost, wrap);
    galaxy = await createGalaxy(wrap, data, { colorBy, onNodeClick: (id) => openPage(wrap, id) });
    if (!galaxy) {                       // bundle/WebGL failure → live fallback
      state.view = "table";
      toast("3D engine unavailable — showing the table view", "bad");
      mount(viewHost, tableView(data));
      reflectHash();
      return;
    }
    wrap.appendChild(fullscreenBtn(wrap));
    lightFlags();                        // fire-and-forget: pulse flagged stars
    if (state.entity) {                  // deep link: open page + fly once laid out
      openPage(wrap, state.entity, { fly: "late" });
    }
    reflectHash();
  }

  function openPage(wrap, id, { fly = "now" } = {}) {
    state.entity = id;
    reflectHash();
    if (galaxy) galaxy.clearIsolate();   // a stale isolate dim shouldn't follow navigation
    openWikiPanel(wrap, id, {
      onExplore: (name) => { galaxy && galaxy.flyTo(name); },
      onNavigate: (name) => { galaxy && galaxy.flyTo(name); openPage(wrap, name); },
      onIsolate: (name, on) => (galaxy
        ? (on ? galaxy.isolate(name, 2) : (galaxy.clearIsolate(), true)) : false),
      onFlagAction: (d) => actOnFinding(d),
    });
    if (!galaxy) return;
    if (fly === "late") setTimeout(() => { if (wrap.isConnected && galaxy) galaxy.flyTo(id); }, 1900);
    else galaxy.flyTo(id);
  }

  // ── review drawer (same findings API + actions as before) ────────────────
  async function loadReview() {
    mount(reviewHost, loadingBlock("Scanning the graph…"));
    try {
      const rd = await api.get("/api/graph/review", { scope: state.scope });
      mount(reviewHost, reviewPanel(rd, (f) => actOnFinding(f)));
    } catch (err) { mount(reviewHost, errorBlock(err)); }
  }

  async function refreshAfterMutation() {
    if (state.review) await loadReview();
    await load();
    const wrap = host.querySelector(".graph-wrap");
    if (state.entity && wrap) openPage(wrap, state.entity, { fly: "late" });
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

  // ── toolbar ───────────────────────────────────────────────────────────────
  let projects = [];
  try { projects = (await api.get("/api/graph/projects")).projects || []; } catch { /* non-fatal */ }
  const scopeOpts = [{ value: "all", label: "all projects" }]
    .concat(projects.map((p) => ({ value: p.source, label: `${p.source} (${p.entities})` })));
  const switcher = facetBar(scopeOpts, state.scope,
    (v) => { state.scope = v; state.entity = ""; load(); if (state.review) loadReview(); });

  const search = el("input", { type: "search", class: "galaxy-search",
    placeholder: "find a star…", name: "q", "aria-label": "search entities",
    oninput: (e) => { state.q = e.target.value; galaxy && galaxy.setQuery(state.q); },
    onkeydown: (e) => {
      if (e.key === "Enter" && galaxy) {
        const hitWrap = host.querySelector(".graph-wrap");
        const hit = galaxy.flyToBest(state.q);
        if (hit && hitWrap) openPage(hitWrap, hit);
      }
      if (e.key === "Escape") { e.target.value = ""; state.q = ""; galaxy && galaxy.setQuery(""); }
    } });

  const reviewBtn = el("button", { class: "facet" + (state.review ? " on" : ""),
    onclick: () => { state.review = !state.review; reviewBtn.classList.toggle("on", state.review);
      reviewHost.style.display = state.review ? "" : "none"; if (state.review) loadReview(); } },
    "Review");
  const viewToggle = facetBar(
    [{ value: "galaxy", label: "galaxy" }, { value: "table", label: "table" }],
    state.view, (v) => { state.view = v; paint(host._data); });

  mount(toolbar, el("span", { class: "eyebrow" }, "scope"), switcher, search,
    el("span", { class: "grow" }), reviewBtn, viewToggle);
  reviewHost.style.display = state.review ? "" : "none";
  if (state.review) loadReview();
  await load();
}
