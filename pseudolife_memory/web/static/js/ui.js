// ui.js — shared overlays: toast, modal, confirm, drawer.
import { el, clear } from "./util.js";

// ── Toasts ───────────────────────────────────────────────────────────────
function toastHost() {
  let h = document.querySelector(".toasts");
  if (!h) { h = el("div", { class: "toasts" }); document.body.appendChild(h); }
  return h;
}
export function toast(message, kind = "info", ms = 3800) {
  // role=status so screen readers announce mutation results.
  const node = el("div", { class: `toast ${kind}`, role: "status" }, message);
  toastHost().appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s, transform .3s";
    node.style.opacity = "0"; node.style.transform = "translateX(12px)";
    setTimeout(() => node.remove(), 300);
  }, ms);
}

// ── Modal ────────────────────────────────────────────────────────────────
export function closeModal() {
  const bg = document.querySelector(".modal-bg");
  if (!bg) return;
  bg.remove();
  bg._onClose?.();       // fires on EVERY close path (button, backdrop, ESC)
  bg._restoreFocus?.();
}
export function openModal({ title, body, actions = [], onClose = null }) {
  closeModal();
  const opener = document.activeElement;
  const bg = el("div", { class: "modal-bg",
    onclick: (e) => { if (e.target === bg) closeModal(); } });
  bg._onClose = onClose;
  bg._restoreFocus = () => { if (opener && document.contains(opener)) opener.focus?.(); };
  const foot = el("div", { class: "modal-foot" },
    actions.map((a) =>
      el("button", { class: `btn ${a.kind || ""}`, onclick: () => a.onClick?.() }, a.label)));
  const box = el("div", { class: "modal", role: "dialog", "aria-modal": "true",
    "aria-label": typeof title === "string" ? title : "Dialog" },
    el("div", { class: "modal-head" }, title),
    el("div", { class: "modal-body" }, body),
    actions.length ? foot : null);
  // Minimal focus trap: Tab cycles within the dialog.
  bg.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = [...box.querySelectorAll(
      "button, input, select, textarea, a[href], [tabindex]:not([tabindex='-1'])",
    )].filter((n) => !n.disabled);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  });
  bg.appendChild(box);
  document.body.appendChild(bg);
  bg.querySelector("input,select,textarea,button")?.focus();
  return bg;
}

export function confirmDialog({ title = "Are you sure?", message = "", danger = false,
  confirmLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    // Resolve BEFORE closeModal so the onClose(false) fallback is a no-op for
    // button paths; backdrop/ESC closes reach onClose and settle as false
    // (previously those paths left the caller's await suspended forever).
    openModal({
      title,
      body: el("div", { class: "dim" }, message),
      onClose: () => resolve(false),
      actions: [
        { label: "Cancel", onClick: () => { resolve(false); closeModal(); } },
        { label: confirmLabel, kind: danger ? "danger" : "primary",
          onClick: () => { resolve(true); closeModal(); } },
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
  const d = el("div", { class: "drawer", role: "dialog",
    "aria-label": typeof title === "string" ? title : "Details" },
    el("div", { class: "drawer-head" },
      el("span", { class: "nav-dot", style: { "--dot": accent } }),
      el("h3", {}, title),
      el("button", { class: "icon-btn", style: { marginLeft: "auto" }, onclick: closeDrawer,
        "aria-label": "Close", title: "Close" }, "✕")),
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
