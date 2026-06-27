// atlas_review.js — the graph-review queue: findings from /api/graph/review,
// each with a confirm-gated cleanup action. Pure rendering; the action handler
// is supplied by atlas.js (which owns the post + re-fetch).
import { el } from "./util.js";
import { panel, badge } from "./components.js";

const SEV = { warn: "var(--c-lessons)", info: "var(--c-assoc)", danger: "var(--c-world)" };
const ACTION_LABEL = { merge: "Merge", delete: "Delete", prune: "Prune", assign: "Assign project" };

export function reviewPanel(data, onAct) {
  const findings = (data && data.findings) || [];
  if (!findings.length) {
    return panel("Review queue",
      el("div", { class: "empty" },
        el("div", { class: "big" }, "Graph looks clean"),
        el("div", {}, "No duplicate, orphan, dubious-edge, test-artifact, or unattributed findings in scope.")),
      { accent: "var(--c-graph)" });
  }
  return panel("Review queue",
    el("div", {}, findings.map((f) => findingRow(f, onAct))),
    { accent: "var(--c-graph)", sub: String(findings.length) });
}

function chips(items, fmt) {
  return el("div", { style: { marginTop: "6px", display: "flex", flexWrap: "wrap", gap: "4px" } },
    items.slice(0, 6).map((m) => el("span", { class: "mono",
      style: { fontSize: "12px", background: "var(--surface-1, rgba(127,127,127,.12))", padding: "2px 7px", borderRadius: "6px" } }, fmt(m))),
    items.length > 6 ? el("span", { class: "dim", style: { fontSize: "12px" } }, `+${items.length - 6}`) : null);
}

function findingRow(f, onAct) {
  const acc = SEV[f.severity] || SEV.info;
  return el("div", { style: { borderLeft: `3px solid ${acc}`, padding: "8px 10px", marginBottom: "8px",
      background: "var(--surface-1, rgba(127,127,127,.06))", borderRadius: "0 8px 8px 0" } },
    el("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
      badge(String(f.type || "").replace(/_/g, " ")),
      el("span", { style: { fontWeight: "500" } }, f.label),
      el("span", { style: { marginLeft: "auto" } },
        f.action && f.action !== "review"
          ? el("button", { class: "btn sm" + (f.action === "delete" ? " danger" : ""),
              title: ACTION_LABEL[f.action] || f.action, onclick: () => onAct(f) }, ACTION_LABEL[f.action] || f.action)
          : null)),
    (f.entities || []).length ? chips(f.entities, (m) => m) : null,
    (f.edges || []).length ? chips(f.edges, (e) => `${e.src} —${e.relation}→ ${e.dst}`) : null);
}
