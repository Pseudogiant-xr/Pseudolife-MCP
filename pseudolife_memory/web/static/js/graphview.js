// graphview.js — shared knowledge-graph renderer (canvas force sim + 3D galaxy +
// table), reused by the Graph explorer and the Atlas overview. Honours a
// `colorBy` option: "etype" (default) | "community" | "project".
import { el, mount, errorBlock } from "./util.js";
import { badge, tagBadge } from "./components.js";

const ETYPE_COLOR = {
  service: "#5b9dff", database: "#3fd0c9", host: "#b083f0", model: "#e8b341",
  person: "#5fcf80", concept: "#e8b341", default: "#5b9dff",
};

export function colorFor(etype) { return ETYPE_COLOR[etype] || ETYPE_COLOR.default; }

export function communityColor(n) {
  if (n.community != null && n.community !== "") {
    const h = (Math.abs(Number(n.community)) * 47) % 360;
    return `hsl(${h} 65% 60%)`;
  }
  return colorFor(n.etype);
}

// Deterministic hue from the entity's first source; unattributed → neutral grey.
export function projectColor(n) {
  const s = (n.sources && n.sources[0]) || "";
  if (!s) return "#6b7280";
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 62% 58%)`;
}

function nodeFill(n, colorBy) {
  return colorBy === "project" ? projectColor(n)
       : colorBy === "community" ? communityColor(n)
       : colorFor(n.etype);
}

// ── 3D galaxy (vendored 3d-force-graph, lazy-loaded) ────────────────────────
let fg3d = null;

export function cleanupGalaxy() {
  if (!fg3d) return;
  try { clearInterval(fg3d.__guard); } catch {}
  try { fg3d.__ro && fg3d.__ro.disconnect(); } catch {}
  try { fg3d._destructor && fg3d._destructor(); } catch {}
  fg3d = null;
}

export async function renderGalaxy(host, data, opts = {}) {
  const colorBy = opts.colorBy || "community";
  const onNodeClick = opts.onNodeClick || (() => {});
  const wrap = el("div", { class: "graph-wrap" });
  const mountPt = el("div", { style: { width: "100%", height: "100%" } });
  const loading = el("div", { class: "graph-hint galaxy-loading", style: { top: "50%", animation: "none" } }, "loading 3D engine…");
  wrap.appendChild(mountPt);
  wrap.appendChild(loading);
  mount(host, wrap);

  let FG;
  try {
    const mod = await import("/ui/vendor/3d-force-graph.bundle.js");
    FG = mod.default || mod.ForceGraph3D;
    if (typeof FG !== "function") throw new Error("ForceGraph3D constructor not found");
  } catch (err) {
    mount(host, errorBlock(new Error("Could not load the 3D engine — " + (err?.message || err))));
    return;
  }
  if (!mountPt.isConnected) return;   // user switched view/route during the import
  loading.remove();

  const deg = {};
  for (const e of data.edges || []) { deg[e.src] = (deg[e.src] || 0) + 1; deg[e.dst] = (deg[e.dst] || 0) + 1; }
  const nodes = (data.nodes || []).map((n) => ({ id: n.entity, etype: n.etype, community: n.community, sources: n.sources }));
  const links = (data.edges || []).map((e) => ({ source: e.src, target: e.dst, derived: !!e.derived }));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const r0 = wrap.getBoundingClientRect();

  fg3d = new FG(mountPt, {})
    .graphData({ nodes, links })
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeLabel((n) => n.id)
    .nodeColor((n) => nodeFill(n, colorBy))
    .nodeVal((n) => 1 + (deg[n.id] || 0))
    .linkColor((l) => (l.derived ? "rgba(150,170,200,0.22)" : "rgba(150,170,200,0.45)"))
    .linkDirectionalArrowLength(3)
    .width(r0.width).height(r0.height)
    .cooldownTime(reduce ? 0 : 12000)
    .onNodeClick((n) => onNodeClick(n.id));

  // The 3D camera starts aimed at the origin; the layout's centroid drifts, so
  // fit it to the graph once nodes have spread (and again when the engine
  // settles) instead of leaving it parked off to one side.
  const fitCam = () => { try { fg3d && fg3d.zoomToFit(400, 40); } catch {} };
  setTimeout(() => { if (mountPt.isConnected) fitCam(); }, 700);
  fg3d.onEngineStop(fitCam);

  const ro = new ResizeObserver(() => {
    const b = wrap.getBoundingClientRect();
    if (fg3d && b.width) { fg3d.width(b.width); fg3d.height(b.height); }
  });
  ro.observe(wrap);
  fg3d.__ro = ro;
  // The 3D render loop won't stop itself on DOM removal — self-destruct on leave.
  fg3d.__guard = setInterval(() => { if (!mountPt.isConnected) cleanupGalaxy(); }, 1500);

  wrap.appendChild(galaxyLegend(colorBy));
  wrap.appendChild(el("div", { class: "graph-hint" }, "drag to orbit · scroll to zoom · click a node"));
  wrap.appendChild(fullscreenBtn(wrap));
}

function galaxyLegend(mode) {
  const label = mode === "project" ? "by project" : "by community";
  return el("div", { class: "graph-legend" },
    el("span", { class: "lg" }, el("span", { class: "sw",
      style: { background: "conic-gradient(from 0deg,#5b9dff,#3fd0c9,#b083f0,#e8b341,#5b9dff)" } }), label),
    el("span", { class: "lg" }, el("span", { class: "ln" }), "relation"),
    el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
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

// ── zoom controls + legend + table ──────────────────────────────────────────
export function zoomControls(graph) {
  const mk = (label, title, fn) => el("button", { title, onclick: fn }, label);
  return el("div", { class: "graph-zoom" },
    mk("+", "zoom in", () => graph.zoomBy(1.25)),
    mk("−", "zoom out", () => graph.zoomBy(0.8)),
    mk("⤢", "fit to view", () => graph.fitView(true)));
}

export function legend(etypes, mode) {
  if (mode === "project" || mode === "community") {
    const label = mode === "project" ? "by project" : "by community";
    return el("div", { class: "graph-legend" },
      el("span", { class: "lg" }, el("span", { class: "sw",
        style: { background: "conic-gradient(from 0deg,#5b9dff,#3fd0c9,#b083f0,#e8b341,#5b9dff)" } }), label),
      el("span", { class: "lg" }, el("span", { class: "ln" }), "explicit"),
      el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
  }
  const list = etypes && etypes.length ? etypes : ["entity"];
  return el("div", { class: "graph-legend" },
    ...list.map((et) => el("span", { class: "lg" },
      el("span", { class: "sw", style: { background: colorFor(et) } }), et)),
    el("span", { class: "lg" }, el("span", { class: "ln" }), "explicit"),
    el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
}

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

// ── Force-directed simulation ───────────────────────────────────────────────
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export class ForceGraph {
  constructor(canvas, wrap, data, opts = {}) {
    this.canvas = canvas; this.wrap = wrap;
    this.onSelect = opts.onSelect || (() => {});
    this.colorBy = opts.colorBy || "etype";
    canvas.__fg = this;   // debug/test handle
    this.ctx = canvas.getContext("2d");
    this.seed = opts.seed;
    this.build(data);
    this.alpha = 1;
    this.scale = 1; this.panX = 0; this.panY = 0;
    this.dragging = null; this.panning = null; this.hover = null; this.downAt = null;
    this.userInteracted = false;     // auto-fit until the user grabs the camera
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(wrap);
    this.resize();
    this.bind();
    this.running = true;
    this.loop = this.loop.bind(this);
    requestAnimationFrame(this.loop);
  }

  build(data) {
    const map = new Map();
    const node = (id, extra = {}) => {
      if (!map.has(id)) map.set(id, { id, entity: id, x: 0, y: 0, vx: 0, vy: 0, deg: 0, ...extra });
      else Object.assign(map.get(id), extra);
      return map.get(id);
    };
    for (const n of data.nodes || []) node(n.entity, { etype: n.etype, facts: n.facts, canonical: n.canonical, community: n.community, sources: n.sources });
    this.links = [];
    for (const e of data.edges || []) {
      const s = node(e.src), t = node(e.dst);
      s.deg++; t.deg++;
      this.links.push({ s, t, relation: e.relation, derived: !!e.derived });
    }
    this.nodes = [...map.values()];
    const n = this.nodes.length;
    // Spread scales with node count so dense graphs aren't cramped. Absolute
    // size doesn't matter on load — fitView() frames it — but separation does.
    this.rest = 120 + Math.min(n, 50) * 6;
    const cx = 500, cy = 360, R = this.rest * 1.4;
    this.nodes.forEach((nd, i) => {
      const a = (i / Math.max(1, n)) * Math.PI * 2;
      nd.x = cx + Math.cos(a) * R + (Math.random() - 0.5) * 50;
      nd.y = cy + Math.sin(a) * R + (Math.random() - 0.5) * 50;
      if (nd.entity === this.seed) { nd.x = cx; nd.y = cy; }
    });
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const r = this.wrap.getBoundingClientRect();
    this.W = r.width; this.H = r.height;
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.alpha = Math.max(this.alpha, 0.3);
    this.userInteracted = false;   // re-frame on container resize
  }

  tick() {
    const nodes = this.nodes, links = this.links;
    const cx = 500, cy = 360;
    const kRep = 14000, kSpring = 0.04, rest = this.rest;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const f = kRep / d2;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    for (const l of links) {
      let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = kSpring * (d - rest);
      const fx = (dx / d) * f, fy = (dy / d) * f;
      l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
    }
    for (const n of nodes) {
      n.vx += (cx - n.x) * 0.004; n.vy += (cy - n.y) * 0.004; // gentle centering
      if (n === this.dragging) { n.x = this.dragWorld.x; n.y = this.dragWorld.y; n.vx = n.vy = 0; continue; }
      n.vx *= 0.85; n.vy *= 0.85;
      n.x += n.vx * this.alpha; n.y += n.vy * this.alpha;
    }
    this.alpha *= 0.993;
    if (!this.userInteracted) this.fitView(false);  // keep framed until grabbed
  }

  radius(n) { return Math.min(28, 13 + n.deg * 1.8) + (n.entity === this.seed ? 4 : 0); }

  // ── camera ──────────────────────────────────────────────────────────────
  screenToWorld(sx, sy) { return { x: (sx - this.panX) / this.scale, y: (sy - this.panY) / this.scale }; }

  zoomAt(sx, sy, factor) {
    const ns = clamp(this.scale * factor, 0.15, 4);
    const w = this.screenToWorld(sx, sy);
    this.panX = sx - w.x * ns; this.panY = sy - w.y * ns; this.scale = ns;
  }
  zoomBy(factor) { this.userInteracted = true; this.zoomAt(this.W / 2, this.H / 2, factor); }

  fitView(userTriggered) {
    if (!this.nodes.length) return;
    if (userTriggered) this.userInteracted = true;
    // Camera CENTRE is the median position on each axis, not the midpoint of
    // the extremes: at real scale (hundreds of nodes) the layout isn't "one
    // tight cluster plus a couple of outliers" — repulsion gives it a broad,
    // often-skewed continuous spread, so a long thin tail on one side (a
    // handful of stray/weakly-connected nodes — real orphans, or a capped
    // hub whose neighbours got cut) drags a min/max-midpoint far from where
    // most nodes actually sit, which is what "off to the side" turned out to
    // be even after excluding literal extremes. The median tracks the sim's
    // own centering force regardless of any such tail. SCALE still comes
    // from a trimmed half-extent so a few strays don't force the view out
    // to fit them, while everyone still renders (nothing is cropped, they
    // just may sit outside the initial framing).
    const xs = this.nodes.map((nd) => nd.x).sort((a, b) => a - b);
    const ys = this.nodes.map((nd) => nd.y).sort((a, b) => a - b);
    const n = xs.length;
    const median = (arr) => (arr.length % 2
      ? arr[(arr.length - 1) / 2]
      : (arr[arr.length / 2 - 1] + arr[arr.length / 2]) / 2);
    const cx = median(xs), cy = median(ys);
    const trim = n > 12 ? Math.floor(n * 0.04) : 0;
    const pad = Math.max(...this.nodes.map((nd) => this.radius(nd))) + 46;
    const halfW = Math.max(cx - xs[trim], xs[n - 1 - trim] - cx, 1) + pad;
    const halfH = Math.max(cy - ys[trim], ys[n - 1 - trim] - cy, 1) + pad;
    const fit = Math.min(this.W / (halfW * 2), this.H / (halfH * 2));
    // auto-fit only shrinks (never blows a tiny graph up past 1:1); the manual
    // fit button is allowed to enlarge up to 1.6×.
    this.scale = clamp(fit, 0.15, userTriggered ? 1.6 : 1);
    this.panX = this.W / 2 - cx * this.scale;
    this.panY = this.H / 2 - cy * this.scale;
  }

  themeColors() {
    const t = document.documentElement.getAttribute("data-theme");
    if (t === this._theme && this._col) return this._col;
    this._theme = t;
    const light = t === "light";
    this._col = {
      label: light ? "rgba(35,32,27,.95)" : "rgba(230,238,245,.95)",
      edge: light ? "rgba(60,55,45,.28)" : "rgba(150,170,200,.28)",
      edgeHot: "#5b9dff",
      arrow: light ? "rgba(60,55,45,.45)" : "rgba(150,170,200,.4)",
      ring: light ? "rgba(20,18,15,.7)" : "rgba(255,255,255,.85)",
      ringSeed: light ? "rgba(20,18,15,.4)" : "rgba(255,255,255,.4)",
    };
    return this._col;
  }

  draw() {
    const c = this.ctx, col = this.themeColors();
    c.clearRect(0, 0, this.W, this.H);
    c.save();
    c.translate(this.panX, this.panY);
    c.scale(this.scale, this.scale);
    const lw = 1 / this.scale;   // keep strokes ~constant on screen
    // edges
    for (const l of this.links) {
      const hot = this.hover && (l.s === this.hover || l.t === this.hover);
      c.strokeStyle = hot ? col.edgeHot : col.edge;
      c.lineWidth = (hot ? 2 : 1.2) * lw;
      c.setLineDash(l.derived ? [4 * lw, 4 * lw] : []);
      c.beginPath(); c.moveTo(l.s.x, l.s.y); c.lineTo(l.t.x, l.t.y); c.stroke();
      c.setLineDash([]);
      const ang = Math.atan2(l.t.y - l.s.y, l.t.x - l.s.x);
      const tr = this.radius(l.t) + 3;
      const ax = l.t.x - Math.cos(ang) * tr, ay = l.t.y - Math.sin(ang) * tr;
      const h = 7;
      c.fillStyle = hot ? col.edgeHot : col.arrow;
      c.beginPath();
      c.moveTo(ax, ay);
      c.lineTo(ax - Math.cos(ang - 0.4) * h, ay - Math.sin(ang - 0.4) * h);
      c.lineTo(ax - Math.cos(ang + 0.4) * h, ay - Math.sin(ang + 0.4) * h);
      c.closePath(); c.fill();
      if (hot && l.relation) {
        c.fillStyle = col.label; c.font = "11px 'JetBrains Mono', monospace";
        c.textAlign = "center";
        c.fillText(l.relation, (l.s.x + l.t.x) / 2, (l.s.y + l.t.y) / 2 - 5);
      }
    }
    // nodes
    for (const n of this.nodes) {
      const r = this.radius(n), nc = nodeFill(n, this.colorBy);
      const active = n === this.hover || n === this.selected;
      c.beginPath(); c.arc(n.x, n.y, r, 0, Math.PI * 2);
      c.globalAlpha = active ? 1 : 0.9;
      c.fillStyle = nc; c.fill();
      c.globalAlpha = 1;
      if (active) { c.lineWidth = 3 * lw; c.strokeStyle = col.ring; c.stroke(); }
      if (n.entity === this.seed) { c.lineWidth = 2 * lw; c.strokeStyle = col.ringSeed; c.stroke(); }
      c.fillStyle = col.label; c.font = "12px 'Hanken Grotesk', sans-serif";
      c.textAlign = "center"; c.textBaseline = "top";
      c.fillText(n.entity, n.x, n.y + r + 4);
    }
    c.restore();
  }

  loop() {
    if (!this.running || !this.canvas.isConnected) { this.stop(); return; }
    if (this.alpha > 0.012 || this.dragging) { this.tick(); }
    this.draw();
    requestAnimationFrame(this.loop);
  }

  nodeAt(sx, sy) {
    const w = this.screenToWorld(sx, sy);
    for (let i = this.nodes.length - 1; i >= 0; i--) {
      const n = this.nodes[i], r = this.radius(n) + 4;
      if ((w.x - n.x) ** 2 + (w.y - n.y) ** 2 <= r * r) return n;
    }
    return null;
  }

  evtXY(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  bind() {
    const cv = this.canvas;
    cv.addEventListener("pointerdown", (e) => {
      const p = this.evtXY(e); const n = this.nodeAt(p.x, p.y);
      this.downAt = { ...p, t: Date.now(), n };
      if (n) {
        this.dragging = n; this.dragWorld = this.screenToWorld(p.x, p.y);
        this.userInteracted = true; cv.classList.add("grabbing");
        cv.setPointerCapture(e.pointerId); this.alpha = Math.max(this.alpha, 0.5);
      } else {
        this.panning = { x: p.x, y: p.y, panX: this.panX, panY: this.panY };
        this.userInteracted = true; cv.classList.add("grabbing"); cv.setPointerCapture(e.pointerId);
      }
    });
    cv.addEventListener("pointermove", (e) => {
      const p = this.evtXY(e);
      if (this.dragging) { this.dragWorld = this.screenToWorld(p.x, p.y); }
      else if (this.panning) { this.panX = this.panning.panX + (p.x - this.panning.x); this.panY = this.panning.panY + (p.y - this.panning.y); }
      else { const n = this.nodeAt(p.x, p.y); this.hover = n; cv.style.cursor = n ? "pointer" : "grab"; }
    });
    const end = (e) => {
      const p = this.evtXY(e);
      cv.classList.remove("grabbing");
      const moved = this.downAt && ((p.x - this.downAt.x) ** 2 + (p.y - this.downAt.y) ** 2) > 25;
      if (!moved && this.downAt && this.downAt.n) { this.selected = this.downAt.n; this.onSelect(this.downAt.n); }
      this.dragging = null; this.panning = null; this.downAt = null;
    };
    cv.addEventListener("pointerup", end);
    cv.addEventListener("pointercancel", end);
    cv.addEventListener("pointerleave", () => { if (!this.panning && !this.dragging) this.hover = null; });
    cv.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = this.evtXY(e);
      this.userInteracted = true;
      this.zoomAt(p.x, p.y, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });
  }

  stop() { this.running = false; try { this.ro.disconnect(); } catch {} }
}
