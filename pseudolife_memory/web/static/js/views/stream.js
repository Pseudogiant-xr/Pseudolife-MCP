// views/stream.js — associative continuum: live search + recent stream, with
// rerank/BM25 toggles and a ranking-trace debugger drawer.
import { el, mount, clear, fmtAge, fmtTime, truncate, loadingBlock, emptyBlock, errorBlock, debounce } from "../util.js";
import { api } from "../api.js";
import { openDrawer, setDrawerBody, toast } from "../ui.js";
import { badge, searchBox, facetBar } from "../components.js";

const TONE = "var(--c-assoc)";
let state = { q: "", source: "all", rerank: false, bm25: false };

export async function renderStream(root, ctx) {
  let sources = [];
  try { sources = (await api.get("/api/sources")).sources || []; } catch { /* non-fatal */ }

  const results = el("div", {});
  const note = el("span", { class: "count-note" });
  const traceBtn = el("button", { class: "btn sm", onclick: openTrace }, "Explain ranking");

  const facetOpts = [{ value: "all", label: "all sources" },
    ...sources.slice(0, 8).map((s) => ({ value: s.source, label: `${s.source} · ${s.count}` }))];

  mount(root,
    el("div", { class: "toolbar" },
      searchBox("Search the associative memory…", debounce((v) => { state.q = v; load(); }, 250), state.q),
      toggle("rerank", state.rerank, (v) => { state.rerank = v; load(); }),
      toggle("BM25", state.bm25, (v) => { state.bm25 = v; load(); }),
      traceBtn,
      note),
    facetBar(facetOpts, state.source, (v) => { state.source = v; repaintFacets(); load(); }),
    el("div", { style: { height: "14px" } }),
    results);

  function repaintFacets() {
    root.querySelectorAll(".facet").forEach((f) => {
      const on = (state.source === "all" && f.textContent === "all sources") ||
                 f.textContent.startsWith(state.source + " ·");
      f.classList.toggle("on", on);
    });
  }

  async function load() {
    const searching = !!state.q.trim();
    traceBtn.style.display = searching ? "" : "none";
    mount(results, loadingBlock(searching ? "Searching…" : "Loading recent…"));
    try {
      let data, entries;
      if (searching) {
        data = await api.get("/api/search", {
          q: state.q, top_k: 25, rerank: state.rerank || undefined,
          bm25: state.bm25 || undefined, source: state.source === "all" ? undefined : state.source });
        entries = data.entries || [];
      } else {
        data = await api.get("/api/recent", { n: 50, source: state.source === "all" ? undefined : state.source });
        entries = data.entries || [];
      }
      note.textContent = `${entries.length} ${searching ? "result" : "recent"}${entries.length === 1 ? "" : "s"}`;
      clear(results);
      if (searching && data.low_confidence)
        results.appendChild(el("div", { class: "chip warn", style: { marginBottom: "12px" } }, "low confidence — the agent would abstain"));
      if (searching && (data.cortex || []).length) {
        results.appendChild(el("div", { class: "eyebrow", style: { margin: "4px 2px 8px" } }, "cortex (canonical)"));
        for (const f of data.cortex) results.appendChild(cortexHit(f));
        results.appendChild(el("div", { class: "eyebrow", style: { margin: "20px 2px 8px" } }, "associative"));
      }
      if (!entries.length) { results.appendChild(emptyBlock(searching ? "No matches" : "Nothing recent")); return; }
      for (const e of entries) results.appendChild(entryCard(e, searching));
    } catch (err) { mount(results, errorBlock(err)); }
  }

  async function openTrace() {
    if (!state.q.trim()) return;
    openDrawer({ title: "Ranking trace", accent: TONE, body: loadingBlock("Tracing retrieval…") });
    try {
      const t = await api.get("/api/trace", { q: state.q, top_k: 12, rerank: state.rerank || undefined, bm25: state.bm25 || undefined });
      const tr = t.trace || {};
      setDrawerBody(el("div", {},
        el("p", { class: "dim", style: { marginTop: 0 } }, `Query: “${state.q}”`),
        tr.config ? el("div", { class: "facets", style: { marginBottom: "16px" } },
          Object.entries(tr.config).map(([k, v]) => badge(`${k}: ${v}`))) : null,
        el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, "per-tier candidates"),
        el("table", { class: "tbl", style: { marginBottom: "18px" } },
          el("thead", {}, el("tr", {}, el("th", {}, "band"), el("th", {}, "candidates"), el("th", {}, "kept"))),
          el("tbody", {}, (tr.tiers || []).map((ti) => el("tr", {},
            el("td", { class: "mono" }, ti.band), el("td", { class: "mono" }, String(ti.candidates)),
            el("td", { class: "mono" }, String(ti.kept)))))),
        el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, "final top-k"),
        el("ol", { style: { paddingLeft: "20px", margin: 0 } },
          (tr.final_topk || []).map((r) => el("li", { style: { marginBottom: "6px" } },
            el("span", {}, truncate(r.text, 80)), " ",
            el("span", { class: "score-pill" }, (r.score ?? 0).toFixed(3)))))));
    } catch (err) { setDrawerBody(errorBlock(err)); }
  }

  load();
}

function toggle(label, checked, onchange) {
  const input = el("input", { type: "checkbox", checked, onchange: (e) => onchange(e.target.checked) });
  return el("label", { class: "switch", title: `toggle ${label}` }, input,
    el("span", { class: "track" }),
    el("span", { style: { marginLeft: "8px", fontSize: ".82rem", color: "var(--ink-2)" } }, label));
}

function cortexHit(f) {
  return el("div", { class: "entry", style: { borderLeft: "3px solid var(--c-cortex)" } },
    el("div", { class: "entry-text" },
      el("span", { class: "mono dim" }, `${f.entity} · ${f.attribute} → `), el("b", {}, f.value)),
    el("div", { class: "entry-meta" }, badge(f.origin || "agent", (f.origin || "agent")),
      f.score != null ? el("span", { class: "score-pill" }, Number(f.score).toFixed(3)) : null));
}

function entryCard(e, searching) {
  return el("div", { class: "entry" + (e.superseded ? " super" : "") },
    el("div", { class: "entry-text" }, e.text),
    el("div", { class: "entry-meta" },
      e.source ? badge(e.source) : null,
      e.bank ? el("span", { class: "band-chip" }, e.bank) : null,
      ...(e.tags || []).slice(0, 6).map((t) => el("span", { class: "band-chip" }, "#" + t)),
      e.superseded ? badge("superseded", "stale") : null,
      el("span", { class: "spacer" }),
      e.id != null ? el("button", { class: "btn sm", style: { padding: "3px 9px" },
        title: "engram trace", onclick: () => openEngram(e) }, "trace ↗") : null,
      e.access_count != null ? el("span", { class: "dim", style: { fontSize: ".74rem" } }, `${e.access_count}×`) : null,
      e.timestamp ? el("span", { class: "dim", style: { fontSize: ".74rem" }, title: fmtTime(e.timestamp) }, e.age || fmtAge(e.timestamp)) : null,
      searching && e.score != null ? el("span", { class: "score-pill" }, Number(e.score).toFixed(3)) : null));
}

async function openEngram(entry) {
  openDrawer({ title: "Engram trace", accent: TONE, body: loadingBlock("Dereferencing the episode…") });
  let d;
  try { d = await api.get("/api/entry", { id: entry.id }); }
  catch (err) { setDrawerBody(errorBlock(err)); return; }
  if (!d.found) { setDrawerBody(emptyBlock("Faded", "This episode has been forgotten.")); return; }
  const facts = d.consolidated_into || [];
  const reCount = el("span", { class: "chip" }, el("span", { class: "k" }, "reinforcements"), " " + (d.reinforcements ?? 0));
  const reBtn = el("button", { class: "btn sm primary", onclick: async () => {
    reBtn.disabled = true;
    try {
      const r = await api.post("/api/reinforce", { entry_id: d.entry_id });
      reCount.textContent = "";
      reCount.append(el("span", { class: "k" }, "reinforcements"), " " + (r.reinforcements ?? (d.reinforcements || 0) + 1));
      toast("Reinforced", "ok");
    } catch (e2) { toast("Reinforce failed: " + e2.message, "bad"); reBtn.disabled = false; }
  } }, "Reinforce");
  setDrawerBody(el("div", {},
    el("div", { class: "entry", style: { marginTop: 0 } }, el("div", { class: "entry-text" }, d.text)),
    el("div", { class: "facets", style: { margin: "12px 0" } },
      d.source ? badge(d.source) : null, reCount,
      el("span", { class: "chip" }, el("span", { class: "k" }, "access"), " " + (d.access_count ?? 0))),
    el("div", { class: "eyebrow", style: { margin: "16px 0 8px" } }, "consolidated into"),
    facts.length
      ? el("div", {}, facts.map((f) => el("div", { class: "np-fact" },
          el("div", { class: "a" }, `${f.entity} · ${f.attribute}`), el("div", {}, f.value))))
      : el("div", { class: "dim", style: { fontSize: ".84rem" } }, "no canonical facts formed from this episode"),
    el("div", { style: { marginTop: "18px" } }, reBtn)));
}
