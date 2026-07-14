// views/wiki_page.js — the live-rendered entity wiki page (spec 2026-07-15 §B).
// Pure render over GET /api/wiki: no LLM, no staleness, read-only.
import { el, mount, loadingBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { badge } from "../components.js";
import { colorFor } from "../graphview.js";

const fmtDate = (ts) => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "—";

// A clickable entity name: swaps the panel to that entity's page.
function wikilink(name, nav) {
  return el("a", { class: "wikilink", href: "#/graph?entity=" + encodeURIComponent(name),
    onclick: (e) => { e.preventDefault(); nav(name); } }, name);
}

function section(title, ...children) {
  const kids = children.flat().filter(Boolean);
  if (!kids.length) return null;
  return el("div", { class: "wp-section" },
    el("div", { class: "wp-section-title" }, title), ...kids);
}

function factsSection(d) {
  if (!d.facts.length) return null;
  return section("Facts", el("table", { class: "tbl" }, el("tbody", {},
    d.facts.map((f) => el("tr", {},
      el("td", { class: "mono dim" }, f.attribute),
      el("td", {}, f.value,
        f.history_available
          ? el("button", { class: "wp-hist", title: "supersession history",
              onclick: (e) => toggleHistory(e.target, d.canonical, f.attribute) }, "⟲")
          : null),
      el("td", { class: "dim" }, `${f.origin || "—"} · ${f.confidence}`))))));
}

async function toggleHistory(btn, entity, attribute) {
  const row = btn.closest("tr");
  const next = row.nextElementSibling;
  if (next && next.classList.contains("wp-hist-row")) { next.remove(); return; }
  const cell = el("td", { colspan: "3" }, loadingBlock("History…"));
  const tr = el("tr", { class: "wp-hist-row" }, cell);
  row.after(tr);
  try {
    const h = await api.get("/api/facts/history", { entity, attribute });
    const items = (h.versions || []).slice().reverse().map((r) =>
      el("div", { class: "wp-hist-item" },
        el("span", { class: "dim mono" }, fmtDate(r.tx_time)), " ", r.value, " ",
        el("span", { class: "dim" }, `(${r.status})`)));
    mount(cell, items.length ? items : el("span", { class: "dim" }, "no history"));
  } catch (err) { mount(cell, errorBlock(err)); }
}

function relationsSection(d, nav) {
  const line = (r, other, arrow) => el("div", { class: "wp-rel" },
    el("span", { class: "mono dim" }, arrow + " "),
    el("span", { class: "mono" }, r.relation), " ",
    wikilink(other, nav), " ",
    r.derived ? badge("derived", "agent")
              : el("span", { class: "dim" }, String(r.confidence ?? "")));
  return section("Relations",
    d.relations.out.map((r) => line(r, r.target, "→")),
    d.relations.in.map((r) => line(r, r.source, "←")));
}

function flagBanner(d) {
  if (!d.flags.length) return null;
  return el("div", { class: "wp-flags" }, d.flags.map((f) => {
    if (f.kind === "unattributed")
      return el("div", { class: "chip warn" }, "unattributed — no project owns this entity");
    if (f.kind === "proposed_link")
      return el("div", { class: "chip warn" }, `proposed link: ${f.src} ${f.relation} ${f.dst}`);
    return el("div", { class: "chip warn" },
      `${f.kind}: ${f.entity ?? ""}${f.into ? " → " + f.into : ""}`);
  }));
}

function render(host, d, nav, onExplore) {
  mount(host,
    el("div", { class: "wp-head" },
      el("span", { class: "sw", style: { background: colorFor(d.etype) } }),
      el("h2", {}, d.entity),
      d.etype ? badge(d.etype) : null,
      el("span", { class: "grow" }),
      el("button", { class: "x", title: "close",
        onclick: () => host.closest(".wiki-panel")?.remove() }, "✕")),
    el("div", { class: "wp-meta dim" },
      d.aliases.length ? `aka ${d.aliases.join(", ")} · ` : "",
      `first seen ${fmtDate(d.first_seen)}`,
      d.community != null ? ` · community ${d.community}` : "",
      d.projects.length ? ` · ${d.projects.map((p) => p.source).join(", ")}` : ""),
    flagBanner(d),
    factsSection(d),
    section("World", d.world_facts.map((w) =>
      el("div", { class: "wp-world" },
        el("span", { class: "mono dim" }, w.attribute), " ", w.value, " ",
        w.source_url
          ? el("a", { href: w.source_url, target: "_blank",
                      rel: "noopener noreferrer" }, "source")
          : null))),
    relationsSection(d, nav),
    section("Mentions", d.mentions.map((m) =>
      el("div", { class: "wp-mention" },
        el("div", { class: "dim" }, `${fmtDate(m.ts)} · ${m.source}`
          + (m.episode_title ? ` · ${m.episode_title}` : "")),
        el("div", {}, m.text)))),
    section("Timeline", d.timeline.map((t) =>
      el("div", { class: "wp-tl" },
        el("span", { class: "dim mono" }, fmtDate(t.ts)),
        el("span", { class: "wp-tl-kind" }, t.kind), " ", t.text))),
    el("div", { class: "wp-actions" },
      el("button", { class: "btn sm primary", onclick: () => onExplore(d.entity) },
        "Explore from here"),
      el("button", { class: "btn sm", title: `Cortex facts filtered to ${d.entity}`,
        onclick: () => { location.hash = "#/cortex?q=" + encodeURIComponent(d.entity); } },
        "Facts ↗")));
}

// Open (or refresh) the wiki panel inside `wrap` for `entityName`.
export function openWikiPanel(wrap, entityName, { onExplore } = {}) {
  let panel = wrap.querySelector(".wiki-panel");
  if (!panel) { panel = el("div", { class: "wiki-panel" }); wrap.appendChild(panel); }
  const host = el("div", { class: "wp-body" });
  mount(panel, host);
  mount(host, loadingBlock("Opening page…"));
  const nav = (name) => openWikiPanel(wrap, name, { onExplore });
  api.get("/api/wiki", { entity: entityName }).then((d) => {
    if (!host.isConnected) return;
    if (!d.found) {
      mount(host, el("div", { class: "wp-head" },
        el("h2", {}, entityName),
        el("span", { class: "grow" }),
        el("button", { class: "x", title: "close",
          onclick: () => host.closest(".wiki-panel")?.remove() }, "✕")),
        el("div", { class: "dim", style: { padding: "8px 0" } },
          "No page — this entity isn't in the graph."));
      return;
    }
    render(host, d, nav, onExplore || (() => {}));
  }).catch((err) => { if (host.isConnected) mount(host, errorBlock(err)); });
}
