// views/episodes.js — session timeline; click an episode for its summary.
import { el, mount, clear, fmtAge, fmtTime, fmtDuration, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";
import { openDrawer, setDrawerBody } from "../ui.js";
import { badge, groupHead } from "../components.js";

const TONE = "var(--c-episode)";

export async function renderEpisodes(root) {
  mount(root, loadingBlock("Loading episodes…"));
  let data;
  try { data = await api.get("/api/episodes", { limit: 200 }); }
  catch (err) { mount(root, errorBlock(err)); return; }

  const eps = data.episodes || data.entries || [];
  if (!eps.length) { mount(root, emptyBlock("No episodes", "Bracket a working session with memory_episode_start.")); return; }
  eps.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));

  mount(root, el("div", { class: "panel reveal" },
    el("div", { class: "panel-head" },
      el("span", { class: "nav-dot", style: { "--dot": TONE } }),
      el("h2", {}, "Session timeline"),
      el("span", { class: "spacer" }),
      el("span", { class: "sub" }, `${eps.length} episode${eps.length === 1 ? "" : "s"}`)),
    el("div", { class: "panel-body" },
      el("div", { class: "timeline" }, eps.map(episodeItem)))));
}

function episodeItem(e) {
  const open = !e.ended_at;
  const span = open ? "open" : (e.started_at && e.ended_at ? fmtDuration(e.ended_at - e.started_at) : "");
  return el("div", { class: "tl-item" + (open ? " current" : ""), style: { cursor: "pointer" },
    onclick: () => openSummary(e) },
    el("div", { class: "tl-val", style: { fontFamily: "var(--font-display)", fontSize: "1rem" } }, e.title || e.id),
    el("div", { class: "tl-meta" },
      open ? badge("open", "pos") : null,
      el("span", {}, `${e.entry_count ?? 0} entries`),
      span ? el("span", {}, span) : null,
      e.started_at ? el("span", { title: fmtTime(e.started_at) }, "started " + fmtAge(e.started_at)) : null,
      e.hint ? el("span", { class: "dim" }, "· " + e.hint) : null));
}

async function openSummary(e) {
  openDrawer({ title: e.title || e.id, accent: TONE, body: loadingBlock("Summarising…") });
  try {
    const s = await api.get("/api/episodes/summary", { id: e.id });
    if (!s.found) { setDrawerBody(emptyBlock("Episode not found")); return; }
    const dist = (arr, key) => (arr || []).map((d) => badge(`${d[key]} · ${d.count}`));
    setDrawerBody(el("div", {},
      el("dl", { class: "kv", style: { marginBottom: "18px" } },
        el("dt", {}, "entries"), el("dd", {}, String(s.entry_count ?? 0)),
        el("dt", {}, "started"), el("dd", {}, s.started_at ? fmtTime(s.started_at) : "—"),
        el("dt", {}, "ended"), el("dd", {}, s.ended_at ? fmtTime(s.ended_at) : "open")),
      (s.tag_distribution || []).length ? el("div", { style: { marginBottom: "16px" } },
        el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, "tags"),
        el("div", { class: "facets" }, dist(s.tag_distribution, "tag"))) : null,
      (s.source_distribution || []).length ? el("div", { style: { marginBottom: "16px" } },
        el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, "sources"),
        el("div", { class: "facets" }, dist(s.source_distribution, "source"))) : null,
      (s.recent_entries || []).length ? el("div", {},
        el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, "recent entries"),
        (s.recent_entries || []).map((en) => el("div", { class: "entry" },
          el("div", { class: "entry-text" }, en.text)))) : null));
  } catch (err) { setDrawerBody(errorBlock(err)); }
}
