// views/recall.js — multi-hop associative recall (search → graph-expand →
// re-query) and a path-between-two-entities mode. Renders the bridging chains a
// single-shot search can't produce.
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";

const TONE = "var(--c-assoc)";
let state = { mode: "hops", q: "", hops: 3, source: "", target: "" };
let resultsEl = null;

export async function renderRecall(root, ctx) {
  draw(root);
}

function draw(root) {
  resultsEl = el("div", {});
  const modeBar = el("div", { class: "facets" },
    modeBtn("hops", "multi-hop", root), modeBtn("path", "path between two", root));
  const controls = state.mode === "hops" ? hopsControls() : pathControls();
  mount(root,
    el("div", { class: "toolbar" }, modeBar, el("span", { class: "grow" })),
    controls, resultsEl);
}

function modeBtn(v, label, root) {
  return el("button", { class: "facet" + (state.mode === v ? " on" : ""),
    onclick: () => { if (state.mode !== v) { state.mode = v; draw(root); } } }, label);
}

// ── multi-hop ────────────────────────────────────────────────────────────
function hopsControls() {
  const input = el("input", { type: "search", placeholder: "Recall across the memory graph…",
    value: state.q, name: "q", "aria-label": "recall query",
    onkeydown: (e) => { if (e.key === "Enter") runHops(e.target.value.trim()); } });
  const hopsSel = el("select", { "aria-label": "hops", style: { width: "auto" },
    onchange: (e) => { state.hops = parseInt(e.target.value, 10); } },
    [1, 2, 3, 4, 5].map((h) => el("option", { value: h, selected: h === state.hops }, `${h} hop${h === 1 ? "" : "s"}`)));
  const go = el("button", { class: "btn", onclick: () => runHops(input.value.trim()) }, "Recall");
  return el("div", { class: "toolbar" },
    el("div", { class: "search-box", style: { flex: 1, minWidth: "240px" } },
      el("span", { class: "ico ico-search" }), input),
    hopsSel, go);
}

async function runHops(q) {
  state.q = q;
  if (!q) { mount(resultsEl, emptyBlock("Enter a query", "Recall walks the graph from entities named in your query.")); return; }
  mount(resultsEl, loadingBlock("Walking the graph…"));
  let r;
  try { r = await api.get("/api/recall", { q, hops: state.hops }); }
  catch (err) { mount(resultsEl, errorBlock(err)); return; }
  clear(resultsEl);

  if (r.low_confidence)
    resultsEl.appendChild(el("div", { class: "chip warn", style: { marginBottom: "12px" } },
      "low confidence — no seed entity resolved; the agent would fall back to plain search"));

  resultsEl.appendChild(el("div", { class: "section-h" },
    el("h2", {}, "Seeds"),
    el("span", { class: "sub" }, `${(r.seeds || []).length} · ${r.iterations || 0} iteration${r.iterations === 1 ? "" : "s"}`)));
  resultsEl.appendChild(el("div", { class: "facets" },
    (r.seeds || []).length ? r.seeds.map((s) => el("span", { class: "chip" }, s)) : el("span", { class: "dim" }, "none")));

  if ((r.paths || []).length) {
    resultsEl.appendChild(el("div", { class: "section-h" }, el("h2", {}, "Bridging paths")));
    for (const p of r.paths) resultsEl.appendChild(chainFromNodes(p));
  }
  if ((r.entities || []).length) {
    resultsEl.appendChild(el("div", { class: "section-h" }, el("h2", {}, "Entities"), el("span", { class: "sub" }, `${r.entities.length}`)));
    for (const ent of r.entities) resultsEl.appendChild(entityCard(ent));
  }
  if ((r.texts || []).length) {
    resultsEl.appendChild(el("div", { class: "section-h" }, el("h2", {}, "Surfaced text")));
    for (const t of r.texts) resultsEl.appendChild(el("div", { class: "entry" }, el("div", { class: "entry-text" }, t)));
  }
  if (!(r.seeds || []).length && !(r.entities || []).length && !(r.texts || []).length)
    resultsEl.appendChild(emptyBlock("No recall", "Nothing connected to that query."));
}

// ── path between two ───────────────────────────────────────────────────────
function pathControls() {
  const src = el("input", { type: "text", placeholder: "source entity…", value: state.source,
    "aria-label": "source entity", style: { maxWidth: "240px" },
    onkeydown: (e) => { if (e.key === "Enter") run(); } });
  const dst = el("input", { type: "text", placeholder: "target entity…", value: state.target,
    "aria-label": "target entity", style: { maxWidth: "240px" },
    onkeydown: (e) => { if (e.key === "Enter") run(); } });
  const run = () => { state.source = src.value.trim(); state.target = dst.value.trim(); doPath(); };
  return el("div", { class: "toolbar" }, src, el("span", { class: "dim" }, "→"), dst,
    el("button", { class: "btn", onclick: run }, "Find path"));
}

async function doPath() {
  if (!state.source || !state.target) {
    mount(resultsEl, emptyBlock("Enter two entities", "Find the shortest path between a source and a target entity.")); return;
  }
  mount(resultsEl, loadingBlock("Tracing the path…"));
  let r;
  try { r = await api.get("/api/graph/path", { source: state.source, target: state.target }); }
  catch (err) { mount(resultsEl, errorBlock(err)); return; }
  clear(resultsEl);
  if (r.found === false) { resultsEl.appendChild(emptyBlock("Entity not found", `No entity named “${r.missing}”.`)); return; }
  if (!(r.edges || []).length || r.hops == null) {
    resultsEl.appendChild(emptyBlock("No path", `No path within range between “${state.source}” and “${state.target}”.`)); return;
  }
  resultsEl.appendChild(el("div", { class: "section-h" },
    el("h2", {}, "Path"), el("span", { class: "sub" }, `${r.hops} hop${r.hops === 1 ? "" : "s"}`)));
  resultsEl.appendChild(chainFromEdges(r.edges));
}

// ── shared renderers ───────────────────────────────────────────────────────
function chainFromNodes(nodes) {
  const row = el("div", { class: "recall-chain" });
  nodes.forEach((n, i) => {
    if (i) row.appendChild(el("span", { class: "rc-arrow" }, "→"));
    row.appendChild(el("span", { class: "rc-node" }, n));
  });
  return row;
}

function chainFromEdges(edges) {
  const row = el("div", { class: "recall-chain" });
  edges.forEach((e, i) => {
    if (i === 0) row.appendChild(el("span", { class: "rc-node" }, e.src));
    row.appendChild(el("span", { class: "rc-arrow", title: e.relation }, "—" + (e.relation || "") + "→"));
    row.appendChild(el("span", { class: "rc-node" }, e.dst));
  });
  return row;
}

function entityCard(ent) {
  const facts = ent.facts || [];
  return el("div", { class: "panel", style: { marginBottom: "12px" } },
    el("div", { class: "entity-head" },
      el("span", { class: "nav-dot", style: { "--dot": TONE } }),
      el("span", { class: "name" }, ent.entity),
      el("span", { class: "spacer" }),
      el("button", { class: "btn sm", onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(ent.entity); } }, "graph ↗")),
    facts.length
      ? el("div", { style: { padding: "6px 18px 12px" } },
          facts.map((f) => el("div", { class: "np-fact" }, el("div", { class: "a" }, f.attribute), el("div", {}, f.value))))
      : el("div", { class: "dim", style: { padding: "6px 18px 14px", fontSize: ".84rem" } }, "no canonical facts"));
}
