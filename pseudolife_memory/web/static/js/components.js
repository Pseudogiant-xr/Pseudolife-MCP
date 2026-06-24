// components.js — small reusable building blocks shared across views.
import { el } from "./util.js";

export function panel(title, body, { sub, accent, actions } = {}) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" },
      accent ? el("span", { class: "nav-dot", style: { "--dot": accent } }) : null,
      el("h2", {}, title),
      sub ? el("span", { class: "sub" }, sub) : null,
      el("span", { class: "spacer" }),
      actions || null),
    el("div", { class: "panel-body" }, body));
}

export function badge(text, cls = "") {
  return el("span", { class: `badge ${cls}` }, text);
}

export function originBadge(origin) {
  const o = String(origin || "agent").toLowerCase();
  const cls = ["user", "action", "agent"].includes(o) ? o : "agent";
  return el("span", { class: `badge ${cls}`, title: `provenance tier: ${o}` }, o);
}

export function confMeter(c, tone = "var(--accent)") {
  const pct = Math.round((Number(c) || 0) * 100);
  return el("span", { class: "conf", title: `confidence ${pct}%` },
    el("span", { class: "bar" }, el("i", { style: { width: pct + "%", background: tone } })),
    el("span", { class: "v" }, (Number(c) || 0).toFixed(2)));
}

export function searchBox(placeholder, oninput, value = "") {
  const input = el("input", { type: "search", placeholder, value,
    name: "q", "aria-label": placeholder || "Search",
    oninput: (e) => oninput(e.target.value) });
  return el("div", { class: "search-box" }, el("span", { class: "ico ico-search" }), input);
}

export function facetBar(options, active, onPick) {
  return el("div", { class: "facets" },
    options.map((o) => {
      const val = typeof o === "string" ? o : o.value;
      const label = typeof o === "string" ? o : o.label;
      return el("button", { class: "facet" + (val === active ? " on" : ""),
        onclick: () => onPick(val) }, label);
    }));
}

export function groupHead(title, count) {
  return el("div", { class: "group-h" },
    el("span", { class: "t" }, title),
    count != null ? el("span", { class: "c" }, count) : null);
}
