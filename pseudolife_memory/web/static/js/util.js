// util.js — tiny hyperscript + formatting helpers (no framework).

/** el("div", {class:"x", onclick:fn}, child, [children]) -> HTMLElement */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sv == null) continue;
        if (sk.startsWith("--")) node.style.setProperty(sk, String(sv));
        else node.style[sk] = sv;
      }
    }
    else if (k === "html") node.innerHTML = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "for") node.htmlFor = v;
    else if (k in node && k !== "list" && k !== "type") {
      try { node[k] = v; } catch { node.setAttribute(k, v); }
    } else node.setAttribute(k, v);
  }
  appendKids(node, children);
  return node;
}

function appendKids(node, kids) {
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  appendKids(node, children);
  return node;
}

// ── formatting ─────────────────────────────────────────────────────────────
export function fmtNum(n) {
  if (n == null || isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US");
}

export function fmtPct(x, digits = 0) {
  if (x == null || isNaN(x)) return "—";
  return (x * 100).toFixed(digits) + "%";
}

export function fmtAge(tsSeconds) {
  if (!tsSeconds) return "";
  const s = Date.now() / 1000 - Number(tsSeconds);
  if (s < 60) return "just now";
  const m = s / 60; if (m < 60) return `${Math.floor(m)}m ago`;
  const h = m / 60; if (h < 24) return `${Math.floor(h)}h ago`;
  const d = h / 24; if (d < 30) return `${Math.floor(d)}d ago`;
  const mo = d / 30; if (mo < 12) return `${Math.floor(mo)}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

export function fmtDuration(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Number(seconds));
  if (s < 90) return `${Math.round(s)}s`;
  const m = s / 60; if (m < 90) return `${Math.round(m)}m`;
  const h = m / 60; if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

export function fmtTime(tsSeconds) {
  if (!tsSeconds) return "—";
  return new Date(Number(tsSeconds) * 1000).toLocaleString();
}

export function titleCase(s) {
  return String(s || "").replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function truncate(s, n = 160) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// State helpers used by views
export function loadingBlock(label = "Querying the cortex…") {
  return el("div", { class: "loading" }, el("div", { class: "spinner" }), el("div", {}, label));
}
export function emptyBlock(big = "Nothing here yet", sub = "") {
  return el("div", { class: "empty" }, el("div", { class: "big" }, big), sub && el("div", {}, sub));
}
export function errorBlock(err) {
  const msg = err && err.code === 401 ? "Unauthorized — set an access token." : (err?.message || String(err));
  return el("div", { class: "error-box" }, el("div", { class: "big" }, "Request failed"), el("div", { class: "mono" }, msg));
}
