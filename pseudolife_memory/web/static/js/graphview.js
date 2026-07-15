// graphview.js — shared graph helpers: entity-type / project / community
// colors, the table view, and the fullscreen control. The renderers live in
// galaxy.js (3D) — the old 2D canvas force sim was retired in Atlas stage 2.
import { el } from "./util.js";
import { badge, tagBadge } from "./components.js";

const ETYPE_COLOR = {
  service: "#5b9dff", database: "#3fd0c9", host: "#b083f0", model: "#e8b341",
  person: "#5fcf80", concept: "#e8b341", default: "#5b9dff",
};

export function colorFor(etype) { return ETYPE_COLOR[etype] || ETYPE_COLOR.default; }

// Hue derivation is exported separately so the galaxy can pair the same hues
// with its own recency-driven lightness. null hue ⇒ caller falls back.
export function communityHue(n) {
  return (n.community != null && n.community !== "")
    ? (Math.abs(Number(n.community)) * 47) % 360 : null;
}

// Deterministic hue from the entity's first source; unattributed → null.
export function projectHue(n) {
  const s = (n.sources && n.sources[0]) || "";
  if (!s) return null;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

export function communityColor(n) {
  const h = communityHue(n);
  return h == null ? colorFor(n.etype) : `hsl(${h} 65% 60%)`;
}

export function projectColor(n) {
  const h = projectHue(n);
  return h == null ? "#6b7280" : `hsl(${h} 62% 58%)`;
}

// ── Fullscreen (Fullscreen API + CSS-maximize fallback) ─────────────────────
export function fullscreenBtn(wrap) {
  return el("button", { class: "graph-fs", title: "Fullscreen", "aria-label": "Toggle fullscreen",
    onclick: () => toggleFullscreen(wrap) }, "⛶");
}

function enterMaximized(wrap) {
  wrap.classList.add("maximized");
  const onKey = (e) => { if (e.key === "Escape") { wrap.classList.remove("maximized"); document.removeEventListener("keydown", onKey); } };
  document.addEventListener("keydown", onKey);
}

function toggleFullscreen(wrap) {
  if (document.fullscreenElement) { document.exitFullscreen && document.exitFullscreen(); return; }
  if (wrap.classList.contains("maximized")) { wrap.classList.remove("maximized"); return; }
  if (wrap.requestFullscreen) wrap.requestFullscreen().catch(() => enterMaximized(wrap));
  else enterMaximized(wrap);
}

// ── table view (the data / accessibility / no-WebGL fallback) ───────────────
export function tableView(data) {
  const nodes = data.nodes || [], edges = data.edges || [];
  return el("div", {},
    el("div", { class: "panel", style: { marginBottom: "16px" } },
      el("div", { class: "panel-head" }, el("h2", {}, "Entities"), el("span", { class: "spacer" }), el("span", { class: "sub" }, `${nodes.length}`)),
      el("div", { style: { padding: "4px 8px" } },
        el("table", { class: "tbl" },
          el("thead", {}, el("tr", {}, el("th", {}, "entity"), el("th", {}, "type"), el("th", {}, "facts"))),
          el("tbody", {}, nodes.map((n) => el("tr", {},
            el("td", { class: "mono" }, n.entity),
            el("td", {}, n.etype ? badge(n.etype) : el("span", { class: "dim" }, "—")),
            el("td", { class: "dim" }, String((n.facts || []).length)))))))),
    el("div", { class: "panel" },
      el("div", { class: "panel-head" }, el("h2", {}, "Relations"), el("span", { class: "spacer" }), el("span", { class: "sub" }, `${edges.length}`)),
      el("div", { style: { padding: "4px 8px" } },
        el("table", { class: "tbl" },
          el("thead", {}, el("tr", {}, el("th", {}, "source"), el("th", {}, "relation"), el("th", {}, "target"), el("th", {}, "kind"))),
          el("tbody", {}, edges.map((e) => el("tr", {},
            el("td", { class: "mono" }, e.src), el("td", { class: "mono dim" }, e.relation),
            el("td", { class: "mono" }, e.dst),
            el("td", {}, e.derived ? badge("derived", "agent")
              : (e.tag ? tagBadge(e.tag) : badge("explicit", "action"))))))))));
}
