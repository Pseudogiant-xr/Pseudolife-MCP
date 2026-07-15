// galaxy.js — the first-class 3D galaxy (Atlas stage 2, spec 2026-07-15 §C).
// Wraps the vendored bundle (ForceGraph3D + THREE — ONE three.js instance).
// Encodings: size = degree + facts, hue = project/community, lightness =
// recency of last activity. Community nebulae + constellation labels,
// proximity-faded node labels, search highlight, camera fly-to.
import { el } from "./util.js";
import { projectHue, communityHue, colorFor } from "./graphview.js";

let live = null;             // one galaxy instance, like the old fg3d singleton

export function destroyGalaxy() {
  if (!live) return;
  try { clearInterval(live.guard); } catch {}
  try { cancelAnimationFrame(live.raf); } catch {}
  try { live.ro && live.ro.disconnect(); } catch {}
  try { live.fg && live.fg._destructor && live.fg._destructor(); } catch {}
  live = null;
}

const LABEL_NEAREST = 40;    // nearest N nodes carry a visible name sprite
const NEBULA_MAX = 12;       // largest N communities get a cloud + constellation

// ── sprite factories (CanvasTexture — no extra deps) ────────────────────────
function textSprite(THREE, text, { color = "#c7d2e2", px = 28, shadow = true } = {}) {
  const font = `500 ${px}px 'Hanken Grotesk', sans-serif`;
  const pad = 10, meas = document.createElement("canvas").getContext("2d");
  meas.font = font;
  const w = Math.ceil(meas.measureText(text).width) + pad * 2, h = px + 18;
  const cv = document.createElement("canvas");
  cv.width = w; cv.height = h;
  const c = cv.getContext("2d");
  c.font = font; c.textAlign = "center"; c.textBaseline = "middle";
  if (shadow) { c.shadowColor = "rgba(0,0,0,.6)"; c.shadowBlur = 7; }
  c.fillStyle = color;
  c.fillText(text, w / 2, h / 2);
  const tex = new THREE.CanvasTexture(cv);
  const mat = new THREE.SpriteMaterial({ map: tex, depthWrite: false, transparent: true });
  const sp = new THREE.Sprite(mat);
  const scale = px * 0.32;
  sp.scale.set(scale * (w / h), scale, 1);
  return sp;
}

function nebulaSprite(THREE, hue) {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 256;
  const c = cv.getContext("2d");
  const g = c.createRadialGradient(128, 128, 8, 128, 128, 128);
  g.addColorStop(0, `hsla(${hue} 70% 62% / .16)`);
  g.addColorStop(0.55, `hsla(${hue} 70% 55% / .07)`);
  g.addColorStop(1, `hsla(${hue} 70% 50% / 0)`);
  c.fillStyle = g; c.fillRect(0, 0, 256, 256);
  const mat = new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(cv),
    depthWrite: false, transparent: true });
  return new THREE.Sprite(mat);
}

// ── encodings ────────────────────────────────────────────────────────────────
function hueFor(n, colorBy) {
  return colorBy === "project" ? projectHue(n) : communityHue(n);
}

// lightness 38%..62% by recency of the node's latest activity
function buildColors(nodes, edges, colorBy) {
  const act = {};
  for (const n of nodes) act[n.entity] = n.created_at || 0;
  for (const e of edges) {
    act[e.src] = Math.max(act[e.src] || 0, e.asserted_at || 0);
    act[e.dst] = Math.max(act[e.dst] || 0, e.asserted_at || 0);
  }
  const ts = Object.values(act).filter(Boolean);
  const lo = ts.length ? Math.min(...ts) : 0, hi = ts.length ? Math.max(...ts) : 1;
  const span = hi > lo ? hi - lo : 1;
  const colors = {};
  for (const n of nodes) {
    const t = ((act[n.entity] || lo) - lo) / span;
    const l = Math.round(38 + t * 24);
    const h = hueFor(n, colorBy);
    colors[n.entity] = h == null
      ? (colorBy === "project" ? "#6b7280" : colorFor(n.etype))
      : `hsl(${h} 64% ${l}%)`;
  }
  return colors;
}

function legendEl(colorBy) {
  return el("div", { class: "graph-legend" },
    el("span", { class: "lg" }, el("span", { class: "sw",
      style: { background: "conic-gradient(from 0deg,#5b9dff,#3fd0c9,#b083f0,#e8b341,#5b9dff)" } }),
      colorBy === "project" ? "hue: project" : "hue: community"),
    el("span", { class: "lg" }, "bright = recent"),
    el("span", { class: "lg" }, "size = connections"));
}

// ── the galaxy ───────────────────────────────────────────────────────────────
export async function createGalaxy(host, data, opts = {}) {
  destroyGalaxy();
  const colorBy = opts.colorBy || "community";
  const onNodeClick = opts.onNodeClick || (() => {});
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const wrap = host;                         // caller supplies the .graph-wrap
  const mountPt = el("div", { style: { width: "100%", height: "100%" } });
  const loading = el("div", { class: "graph-hint galaxy-loading",
    style: { top: "50%", animation: "none" } }, "loading 3D engine…");
  wrap.appendChild(mountPt); wrap.appendChild(loading);

  let FG, THREE;
  try {
    const mod = await import("/ui/vendor/galaxy.bundle.js");
    FG = mod.default; THREE = mod.THREE;
    if (typeof FG !== "function" || !THREE) throw new Error("bundle exports missing");
  } catch (err) {
    console.error("galaxy bundle load failed", err);
    loading.remove();
    return null;                             // caller falls back to table
  }
  if (!mountPt.isConnected) return null;     // route changed during the import
  loading.remove();

  const deg = {}, facts = {};
  for (const e of data.edges || []) { deg[e.src] = (deg[e.src] || 0) + 1; deg[e.dst] = (deg[e.dst] || 0) + 1; }
  for (const n of data.nodes || []) facts[n.entity] = (n.facts || []).length;
  const colors = buildColors(data.nodes || [], data.edges || [], colorBy);
  const nodes = (data.nodes || []).map((n) => ({ id: n.entity, etype: n.etype,
    community: n.community, sources: n.sources, created_at: n.created_at }));
  const links = (data.edges || []).map((e) => ({ source: e.src, target: e.dst,
    derived: !!e.derived, asserted_at: e.asserted_at }));
  const r0 = wrap.getBoundingClientRect();

  // State layers, priority: search query > isolate dim > flagged tint
  // (reduced-motion only; motion users get the pulse) > base recency color.
  const state = { query: "", dim: null, flagged: new Set(), tCut: null,
                  hideOrphans: false };
  const WARN = "#e8b341";
  const nodeColor = (n) => {
    if (state.query) {
      return n.id.toLowerCase().includes(state.query) ? "#ffffff" : "rgba(90,100,115,0.25)";
    }
    if (state.dim && !state.dim.has(n.id)) return "rgba(90,100,115,0.16)";
    if (reduce && state.flagged.has(n.id)) return WARN;
    return colors[n.id] || "#6b7280";
  };
  const nodeVal = (n) => 1 + (deg[n.id] || 0) + (facts[n.id] || 0) * 0.6;

  // ONE definition per state-dependent accessor. poke() re-installs fresh
  // thin wrappers around these — never restated bodies. (The 2026-07-15
  // hide-orphans bug: poke() carried a stale COPY of nodeVisible that
  // predated the orphan check, so every poke silently erased the feature.)
  const nodeVisible = (n) => {
    if (state.hideOrphans && !deg[n.id]) return false;     // no connections
    return state.tCut == null || (n.created_at || 0) <= state.tCut;
  };
  const linkVisible = (l) => {
    if (state.tCut == null) return true;
    const sc = (l.source && l.source.created_at) || 0;
    const tc = (l.target && l.target.created_at) || 0;
    return (l.asserted_at || 0) <= state.tCut && sc <= state.tCut && tc <= state.tCut;
  };
  const linkColorOf = (l) => {
    const dimmed = state.dim &&
      !(state.dim.has(l.source.id ?? l.source) && state.dim.has(l.target.id ?? l.target));
    if (dimmed) return "rgba(90,100,115,0.06)";
    return l.derived ? "rgba(150,170,200,0.20)" : "rgba(150,170,200,0.42)";
  };

  const fg = new FG(mountPt, {})
    .graphData({ nodes, links })
    .backgroundColor("rgba(0,0,0,0)")
    .nodeId("id")
    .nodeLabel((n) => n.id)
    .nodeColor((n) => nodeColor(n))
    .nodeVal(nodeVal)
    .nodeThreeObjectExtend(true)
    .nodeThreeObject((n) => {
      const sp = textSprite(THREE, n.id, { color: "#c7d2e2", px: 26 });
      sp.center.set(0.5, -0.9);              // float above the star
      sp.visible = false;                    // proximity loop reveals it
      n.__label = sp;
      return sp;
    })
    .nodeVisibility((n) => nodeVisible(n))
    .linkVisibility((l) => linkVisible(l))
    .linkColor((l) => linkColorOf(l))
    .linkDirectionalArrowLength(3)
    .width(r0.width).height(r0.height)
    .showNavInfo(false)                      // we render our own hint line
    // Pre-simulate before first paint so an early fly-to (which freezes the
    // layout) still lands on a mostly-settled map, not a mid-explosion one.
    .warmupTicks(reduce ? 120 : 60)
    .cooldownTime(reduce ? 0 : 12000)
    .onNodeClick((n) => onNodeClick(n.id));

  live = { fg, THREE, wrap, mountPt, state };

  // Re-set every state-dependent accessor with a FRESH closure. Passing the
  // same function reference back can be treated as "unchanged" by the prop
  // system and silently skip the scene update — fresh identities always take.
  // Wrappers only: the logic lives in the named accessors above.
  function poke() {
    fg.nodeColor((n) => nodeColor(n));
    fg.linkColor((l) => linkColorOf(l));
    fg.nodeVisibility((n) => nodeVisible(n));
    fg.linkVisibility((l) => linkVisible(l));
  }

  // ── camera policy ─────────────────────────────────────────────────────────
  // The camera NEVER moves on its own once the user has expressed intent
  // (orbit, wheel, or fly-to). Auto-fit runs only on an untouched view: once
  // early (700ms, layout has spread) and once at settle. A fly-to also
  // FREEZES the simulation — the star being inspected must stay where the
  // camera put it; the old "re-fly at settle to correct drift" produced an
  // uncommanded zoom-out-zoom-in (reported 2026-07-15).
  let interacted = false;
  wrap.addEventListener("pointerdown", () => { interacted = true; }, { capture: true });
  wrap.addEventListener("wheel", () => { interacted = true; }, { capture: true, passive: true });
  const fitCam = () => { try { fg.zoomToFit(400, 40); } catch {} };
  setTimeout(() => { if (mountPt.isConnected && !interacted) fitCam(); }, 700);

  // ── nebulae + constellations (recomputed when the engine cools) ──────────
  const nebulae = new THREE.Group();
  fg.scene().add(nebulae);
  function paintNebulae() {
    nebulae.clear();
    const byComm = new Map();
    for (const n of fg.graphData().nodes) {
      if (n.community == null || n.x == null) continue;
      if (!byComm.has(n.community)) byComm.set(n.community, []);
      byComm.get(n.community).push(n);
    }
    const top = [...byComm.entries()].sort((a, b) => b[1].length - a[1].length)
      .slice(0, NEBULA_MAX).filter(([, m]) => m.length >= 3);
    for (const [cid, members] of top) {
      const cx = members.reduce((s, n) => s + n.x, 0) / members.length;
      const cy = members.reduce((s, n) => s + n.y, 0) / members.length;
      const cz = members.reduce((s, n) => s + n.z, 0) / members.length;
      const spread = Math.sqrt(members.reduce((s, n) =>
        s + (n.x - cx) ** 2 + (n.y - cy) ** 2 + (n.z - cz) ** 2, 0) / members.length) || 20;
      const hue = (Math.abs(Number(cid)) * 47) % 360;
      const cloud = nebulaSprite(THREE, hue);
      cloud.position.set(cx, cy, cz);
      cloud.scale.set(spread * 3.2, spread * 3.2, 1);
      nebulae.add(cloud);
      const anchor = members.slice().sort((a, b) =>
        (deg[b.id] || 0) - (deg[a.id] || 0))[0];
      const label = textSprite(THREE, anchor.id, { color: `hsl(${hue} 70% 72%)`, px: 34 });
      label.position.set(cx, cy + spread * 1.5, cz);
      label.material.opacity = 0.75;
      nebulae.add(label);
    }
  }
  // Engine-stop: fit exactly once, and only if the user never touched the
  // camera. Nebulae recompute here regardless (positions are final).
  let fitted = false;
  fg.onEngineStop(() => {
    if (!fitted) { fitted = true; if (!interacted) fitCam(); }
    paintNebulae();
  });
  setTimeout(() => { if (mountPt.isConnected) paintNebulae(); }, 1600);

  // ── proximity labels: nearest N visible, re-ranked continuously ──────────
  const camera = fg.camera();
  const camPos = new THREE.Vector3();
  function labelLoop() {
    if (!live || live.fg !== fg) return;
    camPos.copy(camera.position);
    const ns = fg.graphData().nodes;
    const ranked = [];
    for (const n of ns) {
      if (n.x == null || !n.__label) continue;
      const dx = n.x - camPos.x, dy = n.y - camPos.y, dz = n.z - camPos.z;
      ranked.push({ n, d: dx * dx + dy * dy + dz * dz });
    }
    ranked.sort((a, b) => a.d - b.d);
    for (let i = 0; i < ranked.length; i++) ranked[i].n.__label.visible = i < LABEL_NEAREST;
    // labels: hide when the star is hidden by the scrubber or dimmed by isolate
    for (const r of ranked) {
      const hidden = (state.tCut != null && (r.n.created_at || 0) > state.tCut)
                  || (state.dim && !state.dim.has(r.n.id));
      if (hidden) r.n.__label.visible = false;
    }
    // flagged stars pulse (skip under reduced motion — they get a tint instead)
    if (!reduce && state.flagged.size) {
      const s = 1 + 0.22 * Math.sin(performance.now() / 300);
      for (const n of ns) {
        if (!n.__threeObj) continue;
        if (state.flagged.has(n.id)) n.__threeObj.scale.setScalar(s);
        else if (n.__threeObj.scale.x !== 1) n.__threeObj.scale.setScalar(1);
      }
    }
    live.raf = requestAnimationFrame(labelLoop);
  }
  live.raf = requestAnimationFrame(labelLoop);

  // ── lifecycle: resize + self-destruct on DOM removal ─────────────────────
  const ro = new ResizeObserver(() => {
    const b = wrap.getBoundingClientRect();
    if (b.width) { fg.width(b.width); fg.height(b.height); }
  });
  ro.observe(wrap);
  live.ro = ro;
  live.guard = setInterval(() => { if (!mountPt.isConnected) destroyGalaxy(); }, 1500);

  wrap.appendChild(legendEl(colorBy));
  wrap.appendChild(el("div", { class: "graph-hint" },
    "drag to orbit · scroll to zoom · click a star"));

  // ── time scrubber: replay the bank's growth (visibility only — the layout
  // is computed once on the full graph and never re-simulated) ──────────────
  const times = [
    ...nodes.map((n) => n.created_at || 0),
    ...links.map((l) => l.asserted_at || 0),
  ].filter(Boolean);
  if (times.length >= 2) {
    const t0 = Math.min(...times), t1 = Math.max(...times);
    if (t1 > t0) {
      const fmtD = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
      const label = el("span", { class: "scrub-date mono" }, "now");
      const slider = el("input", { type: "range", min: "0", max: "1000", value: "1000",
        name: "scrub", "aria-label": "time scrubber" });
      let playing = null;
      const apply = (v) => {
        state.tCut = v >= 1000 ? null : t0 + (t1 - t0) * (v / 1000);
        label.textContent = state.tCut == null ? "now" : fmtD(state.tCut);
        poke();
      };
      function stopPlay() {
        if (playing) { clearInterval(playing); playing = null; playBtn.textContent = "▶"; }
      }
      slider.oninput = () => { stopPlay(); apply(+slider.value); };
      const playBtn = el("button", { class: "scrub-play", title: "replay growth",
        "aria-label": "replay growth", onclick: () => {
          if (reduce) return;                      // no auto-animation
          if (playing) { stopPlay(); return; }
          let v = 0;
          playBtn.textContent = "❚❚";
          playing = setInterval(() => {
            v += 12;
            if (v >= 1000) { v = 1000; stopPlay(); }
            slider.value = String(v);
            apply(v);
          }, 100);
        } }, "▶");
      wrap.appendChild(el("div", { class: "scrub-bar" }, playBtn, slider, label));
    }
  }

  // ── public handle ─────────────────────────────────────────────────────────
  function flyTo(name) {
    const n = fg.graphData().nodes.find((x) => x.id === name);
    if (!n || n.x == null) return false;
    interacted = true;
    // Freeze the layout mid-warmup: the inspected star must not drift out of
    // frame, and freezing beats correcting with a second uncommanded fly.
    try { fg.cooldownTicks(0); } catch {}
    const d = Math.hypot(n.x, n.y, n.z) || 1;
    const dist = 55 + nodeVal(n) * 2;
    const ratio = 1 + dist / d;
    fg.cameraPosition({ x: n.x * ratio, y: n.y * ratio, z: n.z * ratio },
      { x: n.x, y: n.y, z: n.z }, reduce ? 0 : 1100);
    return true;
  }
  function setQuery(q) {
    state.query = (q || "").trim().toLowerCase();
    poke();
  }
  function flyToBest(q) {
    const s = (q || "").trim().toLowerCase();
    if (!s) return null;
    const m = fg.graphData().nodes.filter((n) => n.id.toLowerCase().includes(s))
      .sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0))[0];
    if (m) flyTo(m.id);
    return m ? m.id : null;
  }
  function setHideOrphans(on) {
    state.hideOrphans = !!on;
    poke();
  }
  function orphanCount() {
    return nodes.reduce((c, n) => c + (deg[n.id] ? 0 : 1), 0);
  }
  function setFlagged(names) {
    state.flagged = names instanceof Set ? names : new Set(names || []);
    poke();                                  // reduced-motion tint path
  }
  function isolate(name, depth = 2) {
    const adj = new Map();
    const addAdj = (a, b) => {
      if (!adj.has(a)) adj.set(a, []);
      adj.get(a).push(b);
    };
    for (const l of links) {
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      addAdj(s, t); addAdj(t, s);
    }
    if (!adj.has(name) && !nodes.some((n) => n.id === name)) return false;
    const keep = new Set([name]);
    let frontier = [name];
    for (let d = 0; d < depth; d++) {
      const next = [];
      for (const cur of frontier) for (const nb of adj.get(cur) || []) {
        if (!keep.has(nb)) { keep.add(nb); next.push(nb); }
      }
      frontier = next;
    }
    state.dim = keep;
    poke();
    return true;
  }
  function clearIsolate() {
    state.dim = null;
    poke();
  }
  const handle = { flyTo, setQuery, flyToBest, setFlagged, isolate, clearIsolate,
                   setHideOrphans, orphanCount, fg, destroy: destroyGalaxy };
  wrap.__galaxy = handle;   // debug/QA handle (parity with the old canvas.__fg)
  return handle;
}
