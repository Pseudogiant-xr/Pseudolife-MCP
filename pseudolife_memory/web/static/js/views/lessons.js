// views/lessons.js — procedural memory: do/avoid lessons grouped by task-type.
import { el, mount, clear, titleCase, loadingBlock, emptyBlock, errorBlock, debounce } from "../util.js";
import { api } from "../api.js";
import { badge, confMeter, searchBox, groupHead, facetBar } from "../components.js";

const TONE = "var(--c-lessons)";
let state = { q: "", pol: "all", data: null };

export async function renderLessons(root) {
  mount(root, loadingBlock("Recalling lessons…"));
  try { state.data = await api.get("/api/lessons", { limit: 1000 }); }
  catch (err) { mount(root, errorBlock(err)); return; }

  const list = el("div", {});
  const note = el("span", { class: "count-note" });
  mount(root,
    el("div", { class: "toolbar" },
      searchBox("Filter lessons…", debounce((v) => { state.q = v; paint(); }, 150), state.q),
      facetBar([{ value: "all", label: "all" }, { value: "+", label: "do (+)" }, { value: "-", label: "avoid (−)" }],
               state.pol, (v) => { state.pol = v; repaintFacets(); paint(); }),
      note),
    list);

  function repaintFacets() {
    const labels = { all: "all", "+": "do (+)", "-": "avoid (−)" };
    root.querySelectorAll(".facet").forEach((f) => f.classList.toggle("on", f.textContent === labels[state.pol]));
  }
  function paint() {
    const entries = (state.data.entries || []).filter(matches);
    note.textContent = `${entries.length} lesson${entries.length === 1 ? "" : "s"}` +
      (state.data.truncated ? ` · first ${(state.data.entries || []).length} of ${state.data.total} loaded` : "");
    clear(list);
    if (!entries.length) { list.appendChild(emptyBlock("No lessons yet", "Outcome signals are distilled into lessons by the dream pass.")); return; }
    const groups = new Map();
    for (const l of entries) { if (!groups.has(l.task)) groups.set(l.task, []); groups.get(l.task).push(l); }
    for (const [task, ls] of [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      list.appendChild(groupHead(task, `${ls.length} lesson${ls.length === 1 ? "" : "s"}`));
      for (const l of ls) list.appendChild(lessonCard(l));
    }
  }
  paint();
}

function matches(l) {
  const pol = l.polarity || (l.outcome === "failure" ? "-" : "+");
  if (state.pol !== "all" && pol !== state.pol) return false;
  if (!state.q) return true;
  return `${l.task} ${l.aspect} ${l.lesson} ${l.about || ""}`.toLowerCase().includes(state.q.toLowerCase());
}

function lessonCard(l) {
  const pol = l.polarity || (l.outcome === "failure" ? "-" : "+");
  const cls = pol === "-" ? "neg" : "pos";
  return el("div", { class: `lesson ${cls} reveal` },
    el("div", { class: "lesson-head" },
      badge(pol === "-" ? "avoid" : "do", pol === "-" ? "neg" : "pos"),
      el("span", { class: "aspect" }, l.aspect || "general"),
      l.outcome ? el("span", { class: "aspect" }, "· " + l.outcome) : null,
      el("span", { class: "spacer" }),
      l.confidence != null ? confMeter(l.confidence, TONE) : null),
    el("div", { class: "lesson-text" }, l.lesson),
    l.about ? el("div", { class: "lesson-about" }, el("span", { class: "k" }, "about: "), l.about) : null);
}
