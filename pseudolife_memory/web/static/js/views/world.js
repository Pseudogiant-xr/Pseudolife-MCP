// views/world.js — world cortex: cited external facts with age-decayed trust.
import { el, mount, clear, fmtAge, loadingBlock, emptyBlock, errorBlock, debounce, safeHttpUrl } from "../util.js";
import { api } from "../api.js";
import { badge, confMeter, searchBox } from "../components.js";

const TONE = "var(--c-world)";
let state = { q: "", data: null };

export async function renderWorld(root) {
  mount(root, loadingBlock("Reading the world cortex…"));
  try { state.data = await api.get("/api/world", { limit: 1000 }); }
  catch (err) { mount(root, errorBlock(err)); return; }

  const list = el("div", {});
  const note = el("span", { class: "count-note" });
  mount(root,
    el("div", { class: "toolbar" },
      searchBox("Filter world facts…", debounce((v) => { state.q = v; paint(); }, 150), state.q),
      note),
    list);

  function paint() {
    const entries = (state.data.entries || []).filter((w) =>
      !state.q || `${w.entity} ${w.attribute} ${w.value}`.toLowerCase().includes(state.q.toLowerCase()));
    note.textContent = `${entries.length} world fact${entries.length === 1 ? "" : "s"}` +
      (state.data.truncated ? ` · first ${(state.data.entries || []).length} of ${state.data.total} loaded` : "");
    clear(list);
    if (!entries.length) { list.appendChild(emptyBlock("No world facts", "Add cited external facts via memory_world_set.")); return; }
    entries.sort((a, b) => `${a.entity}.${a.attribute}`.localeCompare(`${b.entity}.${b.attribute}`));
    for (const w of entries) list.appendChild(worldCard(w));
  }
  paint();
}

function freshnessBadge(fc) {
  const m = { evergreen: "world", slow: "action", volatile: "agent" };
  return badge(fc || "volatile", m[fc] || "");
}

function worldCard(w) {
  const href = safeHttpUrl(w.source_url);
  return el("div", { class: "world-card reveal" },
    el("div", { class: "world-head" },
      el("span", { class: "nav-dot", style: { "--dot": TONE } }),
      el("span", { class: "world-claim" },
        el("span", { class: "ent" }, `${w.entity} · ${w.attribute} → `),
        el("span", { class: "val" }, w.value)),
      el("span", { class: "spacer" }),
      w.stale ? badge("stale", "stale") : null,
      freshnessBadge(w.freshness_class),
      confMeter(w.effective_confidence ?? w.confidence, TONE)),
    w.source_quote ? el("blockquote", { class: "world-quote" }, "“" + w.source_quote + "”") : null,
    el("div", { class: "world-src" },
      w.source_url
        ? (href
            ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, w.source_url)
            : el("span", { class: "dim" }, w.source_url))
        : el("span", { class: "dim" }, "no source url"),
      w.age ? el("span", { class: "dim", style: { marginLeft: "10px" } }, w.age) : null));
}
