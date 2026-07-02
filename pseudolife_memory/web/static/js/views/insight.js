// views/insight.js — the graph-insight digest: suggested questions, god-nodes,
// communities, and surprises (cross-community / low-confidence bridges). Renders
// graph_digest(); empty until a dream sweep has built the digest.
import { el, mount, fmtNum, fmtAge, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { panel, badge } from "../components.js";
import { barRows } from "../charts.js";

export async function renderInsight(root, ctx) {
  mount(root, loadingBlock("Reading the graph digest…"));
  let res;
  try { res = await api.get("/api/graph/digest"); }
  catch (err) { mount(root, errorBlock(err)); return; }

  if (!res || !res.available) {
    mount(root, emptyBlock("No graph digest yet",
      "The digest is built on the dream sweep. Run a dream from the Observatory, then check back."));
    return;
  }

  const d = res.digest || {};
  const t = d.totals || {};
  const maxDeg = Math.max(1, ...(d.god_nodes || []).map((g) => g.degree || 0));

  mount(root,
    el("div", { class: "section-h reveal" },
      el("h2", {}, "Graph insight"),
      el("span", { class: "sub" },
        `${fmtNum(t.entities || 0)} entities · ${fmtNum(t.edges || 0)} edges · ${fmtNum(t.communities || 0)} communities`),
      el("span", { class: "spacer" }),
      el("span", { class: "eyebrow" }, d.computed_at ? "updated " + fmtAge(d.computed_at) : "")),
    questionsPanel(d.questions || []),
    el("div", { class: "reveal", style: { display: "grid", gap: "16px", marginTop: "16px",
      gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))" } },
      godNodesPanel(d.god_nodes || [], maxDeg),
      communitiesPanel(d.communities || [])),
    surprisesPanel(d.surprises || []));
}

// Render `backtick`-quoted segments as styled inline code.
function inlineCode(text) {
  return String(text || "").split("`").map((p, i) => (i % 2 ? el("code", { class: "ic" }, p) : p));
}

function questionsPanel(qs) {
  const body = qs.length
    ? el("div", {}, qs.map((q) => el("div", { class: "insight-q" },
        el("div", { class: "q-text" }, ...inlineCode(q.question)),
        el("div", { class: "q-meta" },
          badge(String(q.type || "").replace(/_/g, " ")),
          el("span", { class: "dim" }, q.why || "")))))
    : emptyBlock("No open questions", "The digest surfaced nothing to verify.");
  return el("div", { class: "reveal" },
    panel("Suggested questions", body, { accent: "var(--c-cortex)", sub: qs.length ? `${qs.length}` : "" }));
}

function godNodesPanel(nodes, maxDeg) {
  const body = nodes.length
    ? el("div", {}, nodes.map((n) => el("button", { class: "gn-row", type: "button",
        title: `open ${n.display} in the graph`,
        onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(n.display); } },
        el("span", { class: "gn-name" }, n.display),
        el("span", { class: "gn-bar" }, el("i", { style: { width: Math.round((n.degree / maxDeg) * 100) + "%" } })),
        el("span", { class: "gn-deg" }, String(n.degree)),
        el("span", { class: "gn-atlas", title: "Show in the graph overview", role: "link",
          style: { marginLeft: "8px", cursor: "pointer" },
          onclick: (e) => { e.stopPropagation();
            location.hash = "#/atlas?entity=" + encodeURIComponent(n.display); } }, "↗"))))
    : emptyBlock("No hubs");
  return panel("God-nodes", body, { accent: "var(--c-graph)", sub: "by degree" });
}

function communitiesPanel(comms) {
  if (!comms.length) return panel("Communities", emptyBlock("No communities"), { accent: "var(--c-graph)" });
  const topBars = comms.slice().sort((a, b) => (b.size || 0) - (a.size || 0)).slice(0, 8)
    .map((c) => ({ label: c.label, value: c.size || 0, color: "var(--c-graph)" }));
  const body = el("div", {},
    barRows(topBars, {}),
    el("div", { style: { height: "16px" } }),
    el("table", { class: "tbl" },
      el("thead", {}, el("tr", {}, el("th", {}, "community"), el("th", {}, "size"), el("th", {}, "cohesion"))),
      el("tbody", {}, comms.map((c) => el("tr", { style: { cursor: "pointer" },
        title: `open ${c.label} in the graph`,
        onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(c.label); } },
        el("td", {}, c.label),
        el("td", { class: "mono" }, String(c.size)),
        el("td", { class: "mono dim" }, Number(c.cohesion ?? 0).toFixed(2)))))));
  return panel("Communities", body, { accent: "var(--c-graph)", sub: `${comms.length}` });
}

function surprisesPanel(surprises) {
  const body = surprises.length
    ? el("div", {}, surprises.map((s) => el("div", { class: "surprise-card" },
        el("div", { class: "sc-edge mono" },
          el("b", {}, s.src),
          el("span", { class: "dim" }, " —" + (s.relation || "rel") + "→ "),
          el("b", {}, s.dst),
          el("span", { class: "score-pill", style: { marginLeft: "auto" } }, "score " + (s.score ?? 0))),
        el("div", { class: "sc-why dim" }, s.why || ""))))
    : emptyBlock("No surprises", "No cross-community or low-confidence bridges flagged.");
  return el("div", { class: "reveal" },
    panel("Surprises", body, { accent: "var(--c-lessons)", sub: surprises.length ? `${surprises.length}` : "" }));
}
