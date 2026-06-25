// views/observatory.js — the dashboard: health, spectral counts, MIRAS
// continuum, dream state, system facts.
import { el, mount, fmtNum, fmtPct, fmtDuration, fmtAge, loadingBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { toast, confirmDialog } from "../ui.js";

const CARDS = [
  { key: "entries", label: "Associative entries", tone: "var(--c-assoc)", route: "stream", sub: (c) => "across 8 bands" },
  { key: "facts", label: "Canonical facts", tone: "var(--c-cortex)", route: "cortex",
    sub: (c) => c.facts_contested ? `${c.facts_contested} contested` : "cortex" },
  { key: "world", label: "World facts", tone: "var(--c-world)", route: "world",
    sub: (c) => c.world_stale ? `${c.world_stale} stale` : "cited & fresh" },
  { key: "lessons", label: "Lessons", tone: "var(--c-lessons)", route: "lessons", sub: () => "procedural" },
  { key: "episodes", label: "Episodes", tone: "var(--c-episode)", route: "episodes", sub: () => "sessions" },
  { key: "sources", label: "Sources", tone: "var(--c-graph)", route: "stream", sub: (c) => `${fmtNum(c.tags || 0)} tags` },
];

function bandHue(i, n) {
  const start = 186, end = 276; // cyan → violet across the continuum
  return start + ((end - start) * i) / Math.max(1, n - 1);
}

export async function renderObservatory(root, ctx) {
  mount(root, loadingBlock("Reading instruments…"));
  let ov, cfg;
  try {
    [ov, cfg] = await Promise.all([
      api.get("/api/overview"),
      api.get("/api/config").catch(() => null),
    ]);
  } catch (err) {
    mount(root, errorBlock(err));
    return;
  }
  ctx.setCounts?.(ov.counts || {});

  const counts = ov.counts || {};
  const stats = ov.stats || {};
  const dream = ov.dream || {};
  const health = ov.health || {};

  mount(root,
    statCards(counts),
    el("div", { class: "reveal", style: { display: "grid", gap: "16px", gridTemplateColumns: "1fr", marginTop: "26px" } },
      continuumPanel(stats),
      el("div", { style: { display: "grid", gap: "16px", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))" } },
        dreamPanel(dream, cfg, ctx),
        systemPanel(health, stats))),
  );
}

function statCards(counts) {
  return el("div", { class: "stat-grid reveal" },
    CARDS.map((c) =>
      el("div", { class: "stat", style: { "--tone": c.tone }, role: "button", tabindex: "0",
        onclick: () => { if (c.route) location.hash = "#/" + c.route; },
        onkeydown: (e) => { if (e.key === "Enter" && c.route) location.hash = "#/" + c.route; } },
        el("div", { class: "label" }, el("span", { class: "d" }), c.label),
        el("div", { class: "num" }, fmtNum(counts[c.key] ?? 0)),
        el("div", { class: "meta", html: c.sub(counts) }))));
}

function continuumPanel(stats) {
  const bands = Array.isArray(stats.bands) ? stats.bands : [];
  const body = bands.length
    ? el("div", { class: "bands" },
        bands.map((b, i) => {
          const cap = b.capacity || 1;
          const raw = (b.size / cap) * 100;
          const pct = b.size > 0 ? Math.max(2.5, Math.min(100, raw)) : 0;
          const hue = bandHue(i, bands.length);
          return el("div", { class: "band-row", style: { "--bh": `hsl(${hue} 72% 62%)` } },
            el("div", { class: "band-name" }, b.name),
            el("div", { class: "band-track", title: `${fmtNum(b.size)} / ${fmtNum(cap)}` },
              el("div", { class: "band-fill", style: { width: pct.toFixed(1) + "%" } })),
            el("div", { class: "band-meta" },
              el("span", { class: "band-cap" }, `${fmtNum(b.size)}/${fmtNum(cap)}`),
              el("span", { class: "hit", title: "retrieval hit rate" }, fmtPct(b.hit_rate, 0))));
        }))
    : el("div", { class: "empty" }, el("div", { class: "big" }, "No band stats"));

  return el("div", { class: "panel" },
    el("div", { class: "panel-head" },
      el("h2", {}, "Memory continuum"),
      el("span", { class: "sub" }, stats.preset ? `${stats.preset} · ${fmtNum(stats.total_memories || 0)} entries` : ""),
      el("span", { class: "spacer" }),
      el("span", { class: "eyebrow" }, "working → forever")),
    el("div", { class: "panel-body" }, body));
}

function dreamPanel(dream, cfg, ctx) {
  const minBatch = knob(cfg, "memory.dream.min_batch") ?? 8;
  const idleThresh = knob(cfg, "memory.dream.idle_seconds") ?? 1800;
  const backlog = dream.backlog ?? 0;
  const idle = dream.idle_seconds ?? 0;
  const fire = !!dream.would_fire;

  const runBtn = el("button", { class: "btn sm", onclick: async () => {
    if (!(await confirmDialog({ title: "Run a dream now?",
      message: "Consolidates the unconsolidated backlog into canonical facts via the configured extractor. May take a while on CPU.",
      confirmLabel: "Run dream" }))) return;
    runBtn.disabled = true; runBtn.textContent = "Dreaming…";
    try {
      const r = await api.post("/api/dream/run", {});
      toast(`Dream: ${r.inserted || 0} inserted, ${r.superseded || 0} superseded, ${r.relations || 0} edges`, "ok", 6000);
      ctx.refresh();
    } catch (e) { toast("Dream failed: " + e.message, "bad"); runBtn.disabled = false; runBtn.textContent = "Run dream"; }
  } }, "Run dream");

  return el("div", { class: "panel" },
    el("div", { class: "panel-head" },
      el("span", { class: "nav-dot", style: { "--dot": "var(--c-lessons)" } }),
      el("h2", {}, "Dream consolidation"),
      el("span", { class: "spacer" }),
      fire ? el("span", { class: "chip warn" }, el("span", { class: "pulse-dot" }), " would fire")
           : el("span", { class: "chip" }, "idle")),
    el("div", { class: "panel-body" },
      el("div", { class: "gauge" },
        gaugeRow("Backlog", backlog, minBatch, `${fmtNum(backlog)} / ${minBatch}`, false),
        gaugeRow("Quiescence", idle, idleThresh, fmtDuration(idle) + " / " + fmtDuration(idleThresh), true)),
      el("div", { style: { display: "flex", gap: "10px", alignItems: "center", marginTop: "18px" } },
        el("span", { class: "dim", style: { fontSize: ".82rem" } },
          dream.dream_cursor ? "cursor " + fmtAge(dream.dream_cursor) : "no cursor yet"),
        el("span", { class: "spacer", style: { marginLeft: "auto" } }),
        runBtn)));
}

function gaugeRow(label, val, max, text, amber) {
  const pct = Math.min(100, (val / (max || 1)) * 100);
  return el("div", { class: "gauge-row" },
    el("div", { class: "gl" }, label),
    el("div", { class: "gauge-track" }, el("div", { class: "gauge-fill" + (amber ? " amber" : ""), style: { width: pct.toFixed(0) + "%" } })),
    el("div", { class: "gauge-val" }, text));
}

function systemPanel(health, stats) {
  const rows = [
    ["schema", health.schema != null ? "v" + health.schema : "—"],
    ["storage", health.storage || "—"],
    ["writer", health.writer_id || "—"],
    ["persist errors", String(health.persist_errors ?? 0)],
    ["preset", stats.preset || "—"],
    ["interactions", fmtNum(stats.interaction_count || 0)],
    ["retrieval queries", fmtNum(stats.retrieval_queries || 0)],
    ["reference docs", fmtNum(stats.reference?.count || 0)],
  ];
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", {}, "System")),
    el("div", { class: "panel-body" },
      el("dl", { class: "kv" }, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]))));
}

function knob(cfg, path) {
  if (!cfg || !cfg.groups) return null;
  for (const g of cfg.groups) for (const k of g.knobs) if (k.path === path) return k.value;
  return null;
}
