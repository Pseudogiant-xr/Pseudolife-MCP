// charts.js — tiny hand-rolled SVG/DOM chart primitives. Zero dependencies.
// Colors are passed in by callers (usually CSS vars) so charts stay theme-aware.
import { el } from "./util.js";

const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, ...kids) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, String(v));
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}

/** donut(segments, {size, thickness}) — segments: [{label, value, color}] */
export function donut(segments, { size = 128, thickness = 16 } = {}) {
  const segs = segments || [];
  const total = segs.reduce((s, x) => s + (Number(x.value) || 0), 0) || 1;
  const r = (size - thickness) / 2, cx = size / 2, cy = size / 2;
  const circ = 2 * Math.PI * r;
  const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, class: "donut" });
  svg.appendChild(svgEl("circle", { cx, cy, r, fill: "none", stroke: "var(--bg-inset)", "stroke-width": thickness }));
  let offset = 0;
  for (const seg of segs) {
    const len = ((Number(seg.value) || 0) / total) * circ;
    const c = svgEl("circle", { cx, cy, r, fill: "none", stroke: seg.color || "var(--accent)",
      "stroke-width": thickness, "stroke-dasharray": `${len.toFixed(2)} ${(circ - len).toFixed(2)}`,
      "stroke-dashoffset": (-offset).toFixed(2), transform: `rotate(-90 ${cx} ${cy})` });
    c.appendChild(svgEl("title", {}, `${seg.label}: ${seg.value}`));
    svg.appendChild(c);
    offset += len;
  }
  svg.appendChild(svgEl("text", { x: cx, y: cy, "text-anchor": "middle",
    "dominant-baseline": "central", class: "donut-total" }, String(total)));
  return svg;
}

/** donutWithLegend(segments, opts) — donut + a labelled legend list. */
export function donutWithLegend(segments, opts = {}) {
  const segs = segments || [];
  return el("div", { class: "donut-wrap" },
    donut(segs, opts),
    el("div", { class: "chart-legend" },
      segs.map((s) => el("div", { class: "cl-row" },
        el("span", { class: "cl-dot", style: { background: s.color || "var(--accent)" } }),
        el("span", { class: "cl-lbl" }, s.label),
        el("span", { class: "cl-val" }, String(s.value))))));
}

/** barRows(rows, {max, valueFmt}) — rows: [{label, value, color}] */
export function barRows(rows, { max, valueFmt } = {}) {
  const rs = rows || [];
  const m = max || Math.max(1, ...rs.map((r) => Number(r.value) || 0));
  const fmt = valueFmt || ((v) => String(v));
  return el("div", { class: "bar-rows" },
    rs.map((r) => el("div", { class: "bar-row" },
      el("span", { class: "br-label", title: r.label }, r.label),
      el("span", { class: "br-bar" },
        el("i", { style: { width: Math.round(((Number(r.value) || 0) / m) * 100) + "%",
          background: r.color || "var(--accent)" } })),
      el("span", { class: "br-val" }, fmt(r.value)))));
}

/** sparkline(values, {w, h, color}) */
export function sparkline(values, { w = 120, h = 28, color = "var(--accent)" } = {}) {
  const vals = (values || []).map((v) => Number(v) || 0);
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "sparkline", preserveAspectRatio: "none" });
  if (!vals.length) return svg;
  const max = Math.max(...vals), min = Math.min(...vals), span = max - min || 1;
  const step = vals.length > 1 ? w / (vals.length - 1) : w;
  const pts = vals.map((v, i) =>
    `${(i * step).toFixed(1)},${(h - ((v - min) / span) * (h - 4) - 2).toFixed(1)}`).join(" ");
  svg.appendChild(svgEl("polyline", { points: pts, fill: "none", stroke: color,
    "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  return svg;
}
