// ui.js — shared overlays: toast, modal, confirm, drawer.
import { el, clear } from "./util.js";

// ── Toasts ───────────────────────────────────────────────────────────────
function toastHost() {
  let h = document.querySelector(".toasts");
  if (!h) { h = el("div", { class: "toasts" }); document.body.appendChild(h); }
  return h;
}
export function toast(message, kind = "info", ms = 3800) {
  const node = el("div", { class: `toast ${kind}` }, message);
  toastHost().appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s, transform .3s";
    node.style.opacity = "0"; node.style.transform = "translateX(12px)";
    setTimeout(() => node.remove(), 300);
  }, ms);
}

// ── Modal ────────────────────────────────────────────────────────────────
export function closeModal() {
  document.querySelector(".modal-bg")?.remove();
}
export function openModal({ title, body, actions = [] }) {
  closeModal();
  const bg = el("div", { class: "modal-bg",
    onclick: (e) => { if (e.target === bg) closeModal(); } });
  const foot = el("div", { class: "modal-foot" },
    actions.map((a) =>
      el("button", { class: `btn ${a.kind || ""}`, onclick: () => a.onClick?.() }, a.label)));
  bg.appendChild(el("div", { class: "modal" },
    el("div", { class: "modal-head" }, title),
    el("div", { class: "modal-body" }, body),
    actions.length ? foot : null));
  document.body.appendChild(bg);
  bg.querySelector("input,select,textarea,button")?.focus();
  return bg;
}

export function confirmDialog({ title = "Are you sure?", message = "", danger = false,
  confirmLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    openModal({
      title,
      body: el("div", { class: "dim" }, message),
      actions: [
        { label: "Cancel", onClick: () => { closeModal(); resolve(false); } },
        { label: confirmLabel, kind: danger ? "danger" : "primary",
          onClick: () => { closeModal(); resolve(true); } },
      ],
    });
  });
}

// ── Drawer (right slide-in) ───────────────────────────────────────────────
export function closeDrawer() {
  document.querySelector(".drawer")?.remove();
  document.querySelector(".drawer-bg")?.remove();
}
export function openDrawer({ title, accent = "var(--accent)", body }) {
  closeDrawer();
  const bg = el("div", { class: "drawer-bg", onclick: closeDrawer });
  const d = el("div", { class: "drawer" },
    el("div", { class: "drawer-head" },
      el("span", { class: "nav-dot", style: { "--dot": accent } }),
      el("h3", {}, title),
      el("button", { class: "icon-btn", style: { marginLeft: "auto" }, onclick: closeDrawer }, "✕")),
    el("div", { class: "drawer-body" }, body));
  document.body.appendChild(bg);
  document.body.appendChild(d);
  return d;
}

export function setDrawerBody(node) {
  const b = document.querySelector(".drawer-body");
  if (b) { clear(b); b.appendChild(node); }
}

// Global ESC closes overlays.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeModal(); closeDrawer(); }
});
