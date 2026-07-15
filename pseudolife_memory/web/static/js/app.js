// app.js — shell, hash router, nav, theme, token, topbar.
import { el, clear, mount, fmtNum } from "./util.js";
import { api, getToken, setToken, onUnauthorized } from "./api.js";
import { toast, openModal, closeModal, closeDrawer } from "./ui.js";
import { renderObservatory } from "./views/observatory.js";
import { renderCortex } from "./views/cortex.js";
import { renderWorld } from "./views/world.js";
import { renderLessons } from "./views/lessons.js";
import { renderStream } from "./views/stream.js";
import { renderRecall } from "./views/recall.js";
import { renderGraph } from "./views/graph.js";
import { renderInsight } from "./views/insight.js";
import { renderEpisodes } from "./views/episodes.js";
import { renderConsole } from "./views/console.js";

const ROUTES = [
  { id: "observatory", label: "Observatory", group: "Overview", accent: "var(--c-assoc)", view: renderObservatory, countKey: null },
  { id: "cortex", label: "Cortex", group: "Memory", accent: "var(--c-cortex)", view: renderCortex, countKey: "facts" },
  { id: "world", label: "World", group: "Memory", accent: "var(--c-world)", view: renderWorld, countKey: "world" },
  { id: "lessons", label: "Lessons", group: "Memory", accent: "var(--c-lessons)", view: renderLessons, countKey: "lessons" },
  { id: "stream", label: "Stream", group: "Memory", accent: "var(--c-assoc)", view: renderStream, countKey: "entries" },
  { id: "recall", label: "Recall", group: "Memory", accent: "var(--c-assoc)", view: renderRecall, countKey: null },
  { id: "graph", label: "Graph", group: "Structure", accent: "var(--c-graph)", view: renderGraph, countKey: null },
  // `atlas` is the legacy hash for the graph's Overview mode — kept routable so
  // old #/atlas deep links still resolve, but hidden from the nav (one Graph tab).
  { id: "atlas", label: "Graph", group: "Structure", accent: "var(--c-graph)", view: renderGraph, countKey: null, hidden: true, navAs: "graph" },
  { id: "insight", label: "Insight", group: "Structure", accent: "var(--c-graph)", view: renderInsight, countKey: null },
  { id: "episodes", label: "Episodes", group: "Operations", accent: "var(--c-episode)", view: renderEpisodes, countKey: "episodes" },
  { id: "console", label: "Console", group: "Operations", accent: "var(--c-assoc)", view: renderConsole, countKey: null },
];
const byId = Object.fromEntries(ROUTES.map((r) => [r.id, r]));
const NAV = ROUTES.filter((r) => !r.hidden);   // routes shown in the sidebar / number shortcuts

const appEl = document.getElementById("app");
const navEl = document.getElementById("nav");
const viewEl = document.getElementById("view");
const titleEl = document.getElementById("view-title");
const statusEl = document.getElementById("topbar-status");

let current = null;
let counts = {};

// ── theme ──────────────────────────────────────────────────────────────────
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("pl_theme", t);
}
applyTheme(localStorage.getItem("pl_theme") || "dark");
document.getElementById("theme-toggle").onclick = () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(next);
};

// ── token ───────────────────────────────────────────────────────────────────
function openTokenModal() {
  const input = el("input", { type: "password", value: getToken(), placeholder: "PSEUDOLIFE_MCP_TOKEN (blank if none)" });
  openModal({
    title: "Access token",
    body: el("div", {},
      el("p", { class: "dim", style: { marginTop: 0 } },
        "Sent as a bearer token on /api requests. Leave blank when the daemon runs without a token (loopback default)."),
      input),
    actions: [
      { label: "Cancel", onClick: closeModal },
      { label: "Save", kind: "primary", onClick: () => { setToken(input.value.trim()); closeModal(); boot(); toast("Token saved", "ok"); } },
    ],
  });
}
document.getElementById("token-btn").onclick = openTokenModal;
onUnauthorized(() => { toast("Unauthorized — set an access token", "bad"); openTokenModal(); });

// ── nav ──────────────────────────────────────────────────────────────────────
function buildNav() {
  clear(navEl);
  let lastGroup = null;
  for (const r of NAV) {
    if (r.group && r.group !== lastGroup) {
      navEl.appendChild(el("div", { class: "nav-group-label" }, r.group));
      lastGroup = r.group;
    }
    const item = el("button", {
      class: "nav-item", dataset: { route: r.id }, style: { "--dot": r.accent },
      onclick: () => { location.hash = "#/" + r.id; closeMobileNav(); },
    },
      el("span", { class: "nav-dot" }),
      el("span", {}, r.label),
      r.countKey ? el("span", { class: "count", dataset: { count: r.countKey } }, "") : null);
    navEl.appendChild(item);
  }
}
function paintNav() {
  // A hidden route (e.g. #/atlas) lights up the nav item it aliases (navAs).
  const activeId = current && (current.navAs || current.id);
  navEl.querySelectorAll(".nav-item").forEach((n) => {
    n.classList.toggle("active", n.dataset.route === activeId);
    const ck = n.querySelector(".count[data-count]");
    if (ck) ck.textContent = counts[ck.dataset.count] != null ? fmtNum(counts[ck.dataset.count]) : "";
  });
}

// ── topbar status ─────────────────────────────────────────────────────────
function paintStatus(overview) {
  clear(statusEl);
  const h = overview?.health || {};
  const dream = overview?.dream || {};
  const chips = [];
  if (h.schema != null) chips.push(el("span", { class: "chip" }, el("span", { class: "k" }, "schema"), " v" + h.schema));
  if (h.storage) chips.push(el("span", { class: "chip" }, el("span", { class: "k" }, "store"), " " + h.storage));
  if (h.persist_errors) chips.push(el("span", { class: "chip bad" }, "persist errors: " + h.persist_errors));
  if (dream.would_fire) chips.push(el("span", { class: "chip warn" }, el("span", { class: "pulse-dot" }), " dream ready"));
  // No overview means the fetch failed — say so instead of claiming "live".
  const state = overview
    ? el("span", { class: "chip ok" }, el("span", { class: "pulse-dot" }), " live")
    : el("span", { class: "chip bad" }, "offline");
  chips.unshift(state);
  mount(statusEl, chips);
}

// ── routing ──────────────────────────────────────────────────────────────────
function routeId() {
  // Strip any ?query (e.g. #/graph?entity=foo) and path tail before matching.
  const id = (location.hash || "").replace(/^#\/?/, "").split("?")[0].split("/")[0];
  return byId[id] ? id : "observatory";
}

async function renderRoute() {
  closeDrawer(); closeModal();   // never leave an overlay across a route change
  const r = byId[routeId()];
  current = r;
  // Views can opt out of the centered reading column via CSS keyed on this
  // (the galaxy wants the whole viewport, prose views want --maxw).
  viewEl.dataset.route = r.navAs || r.id;
  titleEl.textContent = r.label;
  document.documentElement.style.setProperty("--accent", r.accent);
  paintNav();
  clear(viewEl);
  viewEl.scrollTop = 0;
  const ctx = { refresh: renderRoute, setCounts: (c) => { counts = { ...counts, ...c }; paintNav(); } };
  try {
    if (typeof r.view === "function") await r.view(viewEl, ctx);
    else mount(viewEl, placeholder(r));
  } catch (err) {
    console.error("view error", err);
    mount(viewEl, el("div", { class: "error-box" },
      el("div", { class: "big" }, "View failed to render"),
      el("div", { class: "mono" }, err?.message || String(err))));
  }
}

function placeholder(r) {
  return el("div", { class: "panel reveal" },
    el("div", { class: "panel-body" },
      el("div", { class: "empty" },
        el("div", { class: "big" }, r.label + " — coming online"),
        el("div", {}, "This surface is being wired up."))));
}

// ── refresh / shortcuts ─────────────────────────────────────────────────────
document.getElementById("refresh-btn").onclick = () => { refreshCounts(); renderRoute(); };
document.addEventListener("keydown", (e) => {
  if (e.target instanceof Element && e.target.matches("input,textarea,select")) return;
  // Don't shadow browser shortcuts (Ctrl/Cmd+R reload, Ctrl+1..9 tab switch).
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "r") { refreshCounts(); renderRoute(); }
  const n = e.key === "0" ? 10 : parseInt(e.key, 10);
  if (n >= 1 && n <= NAV.length) location.hash = "#/" + NAV[n - 1].id;
});

// ── mobile nav ──────────────────────────────────────────────────────────────
document.getElementById("nav-toggle").onclick = () => appEl.classList.toggle("nav-open");
function closeMobileNav() { appEl.classList.remove("nav-open"); }

// ── boot ──────────────────────────────────────────────────────────────────
async function refreshCounts() {
  try {
    const ov = await api.get("/api/overview");
    counts = ov.counts || {};
    paintStatus(ov);
    paintNav();
    return ov;
  } catch (err) {
    if (err.code !== 401) paintStatus(null);
    return null;
  }
}

async function boot() {
  buildNav();
  await refreshCounts();
  // Unhide BEFORE the first render: a graph/atlas landing route constructs its
  // canvas from getBoundingClientRect(), which is 0×0 while #app is display:none
  // — leaving the camera off-screen until a later resize. The splash still
  // covers the screen during this frame, so there's no visible flash.
  appEl.hidden = false;
  await renderRoute();
  const splash = document.getElementById("splash");
  splash.classList.add("hide");
  setTimeout(() => splash.remove(), 600);
}

window.addEventListener("hashchange", renderRoute);
boot();
