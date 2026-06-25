// consolidation.js — human-in-the-loop dream curation: review near-duplicate
// candidate clusters and merge each into a single canonical memory.
import { el, loadingBlock, emptyBlock, errorBlock } from "./util.js";
import { api } from "./api.js";
import { openDrawer, setDrawerBody, openModal, closeModal, toast } from "./ui.js";

export async function openConsolidationDrawer(ctx) {
  openDrawer({ title: "Consolidation review", accent: "var(--c-lessons)",
    body: loadingBlock("Finding consolidation candidates…") });
  await load(ctx);
}

async function load(ctx) {
  let res;
  try { res = await api.get("/api/consolidation"); }
  catch (err) { setDrawerBody(errorBlock(err)); return; }
  const clusters = res.clusters || [];
  if (!clusters.length) {
    setDrawerBody(emptyBlock("No clusters", "No consolidation candidates right now."));
    return;
  }
  setDrawerBody(el("div", {},
    el("p", { class: "dim", style: { marginTop: 0 } },
      `${clusters.length} cluster${clusters.length === 1 ? "" : "s"} of near-duplicate memories that could merge into one.`),
    clusters.map((c, i) => clusterCard(c, i, ctx))));
}

function clusterCard(c, i, ctx) {
  const members = c.members || [];
  return el("div", { class: "panel", style: { marginBottom: "14px" } },
    el("div", { class: "panel-head" },
      el("h2", {}, `Cluster ${i + 1}`),
      el("span", { class: "sub" }, `cohesion ${Number(c.cohesion ?? 0).toFixed(2)} · ${members.length} entries`),
      el("span", { class: "spacer" }),
      el("button", { class: "btn sm primary", onclick: () => preview(c, ctx) }, "Consolidate")),
    el("div", { class: "panel-body" },
      members.map((m) => el("div", { class: "entry" }, el("div", { class: "entry-text" }, m.text)))));
}

function preview(c, ctx) {
  const members = c.members || [];
  const ta = el("textarea", { rows: 4, "aria-label": "merged memory text",
    value: members[0] ? members[0].text : "" });
  openModal({
    title: `Merge ${members.length} memories into one?`,
    body: el("div", {},
      el("p", { class: "dim", style: { marginTop: 0 } },
        "The members below are superseded and replaced by a single new memory. Edit the merged text:"),
      el("div", { style: { maxHeight: "180px", overflowY: "auto", marginBottom: "12px" } },
        members.map((m) => el("div", { class: "entry", style: { opacity: ".7" } },
          el("div", { class: "entry-text" }, m.text)))),
      ta),
    actions: [
      { label: "Cancel", onClick: closeModal },
      { label: "Consolidate", kind: "primary", onClick: () => doConsolidate(members, ta.value.trim(), ctx) },
    ],
  });
}

async function doConsolidate(members, newText, ctx) {
  if (!newText) { toast("Merged text can't be empty", "bad"); return; }
  try {
    const r = await api.post("/api/consolidate",
      { replaces: members.map((m) => m.text), new_text: newText });
    closeModal();
    toast(`Consolidated ${r.superseded_count ?? members.length} → 1`, "ok");
    ctx?.refresh?.();
    load(ctx);
  } catch (e) { toast("Consolidate failed: " + e.message, "bad"); }
}
