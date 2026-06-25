// views/graph.js — knowledge-graph visualizer. Canvas force-directed sim with a
// camera (scroll-to-zoom, drag-to-pan, fit), wide default spread, drag nodes,
// click-to-inspect, expand-on-click, plus a table view (UX research: some users
// prefer tables to node-link graphs).
import { el, mount, clear, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { badge } from "../components.js";

const ETYPE_COLOR = {
  service: "#5b9dff", database: "#3fd0c9", host: "#b083f0", model: "#e8b341",
  person: "#5fcf80", concept: "#e8b341", default: "#5b9dff",
};
let state = { entity: "", depth: 1, view: "graph" };
let fg = null;
let fg3d = null;

function parseHash() {
  const qi = location.hash.indexOf("?");
  const params = new URLSearchParams(qi >= 0 ? location.hash.slice(qi + 1) : "");
  return { entity: params.get("entity") || "", depth: parseInt(params.get("depth") || "1", 10) };
}

export async function renderGraph(root, ctx) {
  const fromHash = parseHash();
  if (fromHash.entity) state.entity = fromHash.entity;
  if (fromHash.depth) state.depth = fromHash.depth;

  const entityInput = el("input", { type: "text", value: state.entity, placeholder: "seed entity…",
    name: "entity", "aria-label": "seed entity", style: { maxWidth: "260px" },
    onkeydown: (e) => { if (e.key === "Enter") { state.entity = e.target.value.trim(); load(); } } });
  const depthSel = el("select", { name: "depth", "aria-label": "depth", style: { width: "auto" },
    onchange: (e) => { state.depth = parseInt(e.target.value, 10); load(); } },
    [1, 2, 3].map((d) => el("option", { value: d, selected: d === state.depth }, `depth ${d}`)));
  const goBtn = el("button", { class: "btn", onclick: () => { state.entity = entityInput.value.trim(); load(); } }, "Explore");
  const viewToggle = el("div", { class: "facets" },
    el("button", { class: "facet" + (state.view === "galaxy" ? " on" : ""), onclick: () => setView("galaxy") }, "galaxy"),
    el("button", { class: "facet" + (state.view === "graph" ? " on" : ""), onclick: () => setView("graph") }, "graph"),
    el("button", { class: "facet" + (state.view === "table" ? " on" : ""), onclick: () => setView("table") }, "table"));

  const host = el("div", {});
  mount(root,
    el("div", { class: "toolbar" }, entityInput, depthSel, goBtn, el("span", { class: "grow" }), viewToggle),
    host);

  function setView(v) {
    state.view = v;
    root.querySelectorAll(".facets .facet").forEach((f) => f.classList.toggle("on", f.textContent === v));
    paint(host._data);
  }

  async function load() {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    mount(host, loadingBlock("Walking the graph…"));
    try {
      const data = await api.get("/api/graph", { entity: state.entity || undefined, depth: state.depth });
      host._data = data;
      paint(data);
    } catch (err) { mount(host, errorBlock(err)); }
  }

  function paint(data) {
    if (fg) { fg.stop(); fg = null; }
    cleanupGalaxy();
    if (!data || data.found === false || !(data.nodes || []).length) {
      mount(host, emptyBlock("No graph here", state.entity ? `No relations around “${state.entity}”.` : "Enter a seed entity to explore."));
      return;
    }
    if (state.view === "table") { mount(host, tableView(data)); return; }
    if (state.view === "galaxy") { renderGalaxy(host, data); return; }
    const wrap = el("div", { class: "graph-wrap" });
    const canvas = el("canvas", {});
    wrap.appendChild(canvas);
    mount(host, wrap);
    fg = new ForceGraph(canvas, wrap, data, state.entity, (node) => showNode(wrap, node));
    wrap.appendChild(zoomControls(fg));
    const etypes = [...new Set((data.nodes || []).map((n) => n.etype).filter(Boolean))];
    wrap.appendChild(legend(etypes));
    wrap.appendChild(el("div", { class: "graph-hint" }, "scroll to zoom · drag background to pan · click a node"));
    wrap.appendChild(fullscreenBtn(wrap));
  }

  function showNode(wrap, node) {
    wrap.querySelector(".node-panel")?.remove();
    const panel = el("div", { class: "node-panel" },
      el("div", { class: "np-head" },
        el("span", { class: "sw", style: { width: "10px", height: "10px", borderRadius: "50%", background: colorFor(node.etype) } }),
        el("span", { class: "name" }, node.entity),
        el("button", { class: "x", title: "close", onclick: () => panel.remove() }, "✕")),
      el("div", { class: "np-body" },
        node.etype ? el("div", { style: { marginBottom: "8px" } }, badge(node.etype)) : null,
        (node.facts || []).length
          ? (node.facts || []).map((f) => el("div", { class: "np-fact" },
              el("div", { class: "a" }, f.attribute), el("div", {}, f.value)))
          : el("div", { class: "dim", style: { fontSize: ".84rem" } }, "no canonical facts"),
        el("div", { style: { display: "flex", gap: "8px", marginTop: "12px" } },
          el("button", { class: "btn sm primary", onclick: () => { location.hash = "#/graph?entity=" + encodeURIComponent(node.entity) + "&depth=" + state.depth; } }, "Re-center here"),
          el("button", { class: "btn sm", onclick: () => { location.hash = "#/cortex"; } }, "Facts ↗"))));
    wrap.appendChild(panel);
  }

  load();
}

function colorFor(etype) { return ETYPE_COLOR[etype] || ETYPE_COLOR.default; }

function zoomControls(graph) {
  const mk = (label, title, fn) => el("button", { title, onclick: fn }, label);
  return el("div", { class: "graph-zoom" },
    mk("+", "zoom in", () => graph.zoomBy(1.25)),
    mk("−", "zoom out", () => graph.zoomBy(0.8)),
    mk("⤢", "fit to view", () => graph.fitView(true)));
}

function legend(etypes) {
  const list = etypes && etypes.length ? etypes : ["entity"];
  return el("div", { class: "graph-legend" },
    ...list.map((et) => el("span", { class: "lg" },
      el("span", { class: "sw", style: { background: colorFor(et) } }), et)),
    el("span", { class: "lg" }, el("span", { class: "ln" }), "explicit"),
    el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
}

function tableView(data) {
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
            el("td", {}, e.derived ? badge("derived", "agent") : badge("explicit", "action")))))))));
}

// ── 3D galaxy (vendored 3d-force-graph, lazy-loaded) ────────────────────────
function communityColor(n) {
  if (n.community != null && n.community !== "") {
    const h = (Math.abs(Number(n.community)) * 47) % 360;
    return `hsl(${h} 65% 60%)`;
  }
  return colorFor(n.etype);
}

function cleanupGalaxy() {
  if (!fg3d) return;
  try { clearInterval(fg3d.__guard); } catch {}
  try { fg3d.__ro && fg3d.__ro.disconnect(); } catch {}
  try { fg3d._destructor && fg3d._destructor(); } catch {}
  fg3d = null;
}

async function renderGalaxy(host, data) {
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
  const nodes = (data.nodes || []).map((n) => ({ id: n.entity, etype: n.etype, community: n.community }));
  const links = (data.edges || []).map((e) => ({ source: e.src, target: e.dst, derived: !!e.derived }));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const r0 = wrap.getBoundingClientRect();

  fg3d = new FG(mountPt, {})
    .graphData({ nodes, links })
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeLabel((n) => n.id)
    .nodeColor((n) => communityColor(n))
    .nodeVal((n) => 1 + (deg[n.id] || 0))
    .linkColor((l) => (l.derived ? "rgba(150,170,200,0.22)" : "rgba(150,170,200,0.45)"))
    .linkDirectionalArrowLength(3)
    .width(r0.width).height(r0.height)
    .cooldownTime(reduce ? 0 : 12000)
    .onNodeClick((n) => { location.hash = "#/graph?entity=" + encodeURIComponent(n.id) + "&depth=" + state.depth; });

  const ro = new ResizeObserver(() => {
    const b = wrap.getBoundingClientRect();
    if (fg3d && b.width) { fg3d.width(b.width); fg3d.height(b.height); }
  });
  ro.observe(wrap);
  fg3d.__ro = ro;
  // The 3D render loop won't stop itself on DOM removal — self-destruct on leave.
  fg3d.__guard = setInterval(() => { if (!mountPt.isConnected) cleanupGalaxy(); }, 1500);

  wrap.appendChild(galaxyLegend());
  wrap.appendChild(el("div", { class: "graph-hint" }, "drag to orbit · scroll to zoom · click a node"));
  wrap.appendChild(fullscreenBtn(wrap));
}

function galaxyLegend() {
  return el("div", { class: "graph-legend" },
    el("span", { class: "lg" }, el("span", { class: "sw",
      style: { background: "conic-gradient(from 0deg,#5b9dff,#3fd0c9,#b083f0,#e8b341,#5b9dff)" } }), "by community"),
    el("span", { class: "lg" }, el("span", { class: "ln" }), "relation"),
    el("span", { class: "lg" }, el("span", { class: "ln dash" }), "derived"));
}

// ── Fullscreen (Fullscreen API + CSS-maximize fallback) ─────────────────────
function fullscreenBtn(wrap) {
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

// ── Force-directed simulation ───────────────────────────────────────────────
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

class ForceGraph {
  constructor(canvas, wrap, data, seed, onSelect) {
    this.canvas = canvas; this.wrap = wrap; this.onSelect = onSelect;
    canvas.__fg = this;   // debug/test handle
    this.ctx = canvas.getContext("2d");
    this.seed = seed;
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
    for (const n of data.nodes || []) node(n.entity, { etype: n.etype, facts: n.facts, canonical: n.canonical });
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
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of this.nodes) {
      const r = this.radius(n) + 46;   // pad for the label drawn under the node
      minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r);
      minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r);
    }
    const bw = Math.max(1, maxX - minX), bh = Math.max(1, maxY - minY);
    const fit = Math.min(this.W / bw, this.H / bh);
    // auto-fit only shrinks (never blows a tiny graph up past 1:1); the manual
    // fit button is allowed to enlarge up to 1.6×.
    this.scale = clamp(fit, 0.15, userTriggered ? 1.6 : 1);
    this.panX = this.W / 2 - ((minX + maxX) / 2) * this.scale;
    this.panY = this.H / 2 - ((minY + maxY) / 2) * this.scale;
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
      const r = this.radius(n), nc = colorFor(n.etype);
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
