// views/re_evidence.js — read-only visibility into the isolated RE proof store.
import { el, mount, fmtAge, fmtNum, fmtTime, loadingBlock, emptyBlock, errorBlock, debounce, pressable, truncate } from "../util.js";
import { api } from "../api.js";
import { badge, searchBox } from "../components.js";
import { openDrawer } from "../ui.js";

const TONE = "var(--c-proof)";
const STATUSES = ["", "verified", "observed", "hypothesis", "todo", "rejected"];
let state = { project: "", binaryId: "", q: "", status: "" };
let requestSerial = 0;

export async function renderReEvidence(root) {
  await load(root, true);
}

async function load(root, showLoading = false) {
  const serial = ++requestSerial;
  if (showLoading) mount(root, loadingBlock("Reading the proof index…"));
  let data;
  try {
    data = await api.get("/api/re-evidence", {
      project: state.project,
      binary_id: state.binaryId,
      q: state.q,
      status: state.status,
      limit: 250,
    });
  } catch (err) {
    if (serial === requestSerial) mount(root, errorBlock(err));
    return;
  }
  if (serial !== requestSerial) return;
  if (data.selection) {
    state.project = data.selection.project;
    state.binaryId = data.selection.binary_id;
  }
  paint(root, data);
}

function paint(root, data) {
  const scopes = data.scopes || [];
  if (!scopes.length || !data.selection) {
    mount(root,
      proofBanner(),
      el("div", { class: "panel reveal" },
        emptyBlock("No RE evidence yet", "Ingest an authoritative artifact with the re_evidence MCP tool.")));
    return;
  }

  const projects = [...new Set(scopes.map((scope) => scope.project))].sort();
  const builds = scopes.filter((scope) => scope.project === state.project);
  const projectSelect = el("select", {
    "aria-label": "Evidence project", value: state.project,
    onchange: (event) => {
      state.project = event.target.value;
      state.binaryId = (scopes.find((scope) => scope.project === state.project) || {}).binary_id || "";
      load(root, true);
    },
  }, projects.map((project) => el("option", {
    value: project, selected: project === state.project,
  }, project)));
  const buildSelect = el("select", {
    "aria-label": "Evidence build", value: state.binaryId,
    onchange: (event) => { state.binaryId = event.target.value; load(root, true); },
  }, builds.map((scope) => el("option", {
    value: scope.binary_id, title: scope.binary_id,
    selected: scope.binary_id === state.binaryId,
  }, shortBuild(scope.binary_id))));
  const statusSelect = el("select", {
    "aria-label": "Claim status", value: state.status,
    onchange: (event) => { state.status = event.target.value; load(root); },
  }, STATUSES.map((status) => el("option", {
    value: status, selected: status === state.status,
  }, status || "all claim statuses")));
  const search = searchBox("Address, locator, summary, path, or claim…", debounce((value) => {
    state.q = value.trim();
    load(root);
  }, 220), state.q);

  const claimsByStatus = data.totals?.claims || {};
  const claimTotal = Object.values(claimsByStatus).reduce((sum, count) => sum + Number(count || 0), 0);
  const artifacts = data.artifacts || [];
  const claims = data.claims || [];

  mount(root,
    proofBanner(),
    el("div", { class: "proof-controls" },
      control("Project", projectSelect),
      control("Binary build", buildSelect),
      control("Search", search),
      control("Claim status", statusSelect)),
    el("div", { class: "stat-grid", style: { marginBottom: "22px" } },
      stat("Artifacts", data.totals?.artifacts || 0, `${artifacts.length} shown`),
      stat("Claims", claimTotal, `${claims.length} shown`),
      stat("Verified", claimsByStatus.verified || 0, "evidence-linked"),
      stat("Observed", claimsByStatus.observed || 0, "evidence-linked"),
      stat("Open ideas", Number(claimsByStatus.hypothesis || 0) + Number(claimsByStatus.todo || 0), "hypothesis + todo")),
    el("div", { class: "proof-layout" },
      proofPanel("Artifacts", artifacts.length, artifacts.length
        ? el("div", { class: "proof-stack" }, artifacts.map(artifactCard))
        : emptyBlock("No matching artifacts", "Clear the search or choose another build.")),
      proofPanel("Claims", claims.length, claims.length
        ? el("div", { class: "proof-stack" }, claims.map((claim) => claimCard(claim, artifacts)))
        : emptyBlock("No matching claims", "Clear the status or search filter."))));
}

function proofBanner() {
  return el("div", { class: "proof-banner reveal" },
    el("span", { class: "mark", "aria-hidden": "true" }),
    el("div", {},
      el("strong", {}, "Read-only proof index"),
      "Artifacts remain isolated from associative memory, cortex promotion, and dream consolidation. The authoritative source stays the original export, capture, log, screenshot, or asset."));
}

function control(label, child) {
  return el("div", { class: "proof-control" }, el("label", {}, label), child);
}

function stat(label, value, meta) {
  return el("div", { class: "stat reveal", style: { "--tone": TONE } },
    el("div", { class: "label" }, el("span", { class: "d" }), label),
    el("div", { class: "num" }, fmtNum(value)),
    el("div", { class: "meta" }, meta));
}

function proofPanel(title, count, body) {
  return el("section", { class: "panel reveal" },
    el("div", { class: "panel-head" },
      el("span", { class: "nav-dot", style: { "--dot": TONE } }),
      el("h2", {}, title),
      el("span", { class: "spacer" }),
      el("span", { class: "sub" }, `${count} shown`)),
    el("div", { class: "panel-body" }, body));
}

function artifactCard(artifact) {
  return el("article", {
    class: "proof-card", "aria-label": `Artifact ${artifact.id} at ${artifact.locator}`,
    ...pressable(() => openArtifact(artifact)),
  },
    el("div", { class: "proof-card-head" },
      el("span", { class: "locator" }, artifact.locator || `artifact ${artifact.id}`),
      badge(artifact.kind || "artifact", "action"),
      el("span", { class: "spacer" }),
      el("span", { class: "dim mono" }, `#${artifact.id}`)),
    el("div", { class: "proof-summary" }, artifact.summary || artifact.source_path || "No summary"),
    artifact.addresses?.length ? el("div", { class: "proof-addresses" },
      artifact.addresses.slice(0, 8).map((address) => el("span", { class: "proof-address" }, address)),
      artifact.addresses.length > 8 ? el("span", { class: "proof-address" }, `+${artifact.addresses.length - 8}`) : null) : null,
    el("div", { class: "proof-meta" },
      el("span", { title: fmtTime(artifact.ingested_at) }, fmtAge(artifact.ingested_at)),
      el("span", { class: "hash", title: artifact.content_hash }, artifact.content_hash || "no hash")));
}

function claimCard(claim, artifacts) {
  const linked = new Set(claim.evidence_ids || []);
  const linkedArtifacts = artifacts.filter((artifact) => linked.has(artifact.id));
  return el("article", {
    class: "proof-card proof-claim",
    "aria-label": `${claim.status} claim ${claim.id} for ${claim.subject}`,
    ...pressable(() => openClaim(claim, linkedArtifacts)),
  },
    el("div", { class: "proof-card-head" },
      el("span", { class: "locator" }, claim.subject || `claim ${claim.id}`),
      statusBadge(claim.status),
      el("span", { class: "spacer" }),
      claim.confidence != null ? el("span", { class: "dim mono" }, `${Math.round(Number(claim.confidence) * 100)}%`) : null),
    el("div", { class: "proof-summary" }, claim.claim),
    el("div", { class: "proof-meta" },
      el("span", { class: "proof-linked" }, `${(claim.evidence_ids || []).length} linked artifact${(claim.evidence_ids || []).length === 1 ? "" : "s"}`),
      el("span", { title: fmtTime(claim.updated_at) }, `updated ${fmtAge(claim.updated_at)}`)));
}

function statusBadge(status) {
  const tone = { verified: "pos", observed: "action", rejected: "neg", hypothesis: "contested", todo: "agent" }[status] || "agent";
  return badge(status || "unknown", tone);
}

function openArtifact(artifact) {
  openDrawer({ title: artifact.summary || artifact.locator || `Artifact ${artifact.id}`, accent: TONE,
    body: artifactDetails(artifact) });
}

function artifactDetails(artifact) {
  return el("div", {},
    detailList([
      ["id", artifact.id], ["kind", artifact.kind], ["locator", artifact.locator],
      ["source", artifact.source_path], ["ingested", fmtTime(artifact.ingested_at)],
      ["sha-256", artifact.content_hash],
    ]),
    artifact.addresses?.length ? detailSection("Structured addresses",
      el("div", { class: "proof-addresses" }, artifact.addresses.map((address) => el("span", { class: "proof-address" }, address)))) : null,
    artifact.payload_keys?.length ? detailSection("Payload keys",
      el("div", { class: "mono dim" }, artifact.payload_keys.join(", "))) : null);
}

function openClaim(claim, linkedArtifacts) {
  openDrawer({ title: `${claim.status || "claim"} · ${claim.subject}`, accent: TONE,
    body: el("div", {},
      el("p", { style: { marginTop: 0, lineHeight: "1.6" } }, claim.claim),
      detailList([
        ["id", claim.id], ["status", claim.status], ["confidence", claim.confidence ?? "—"],
        ["created", fmtTime(claim.created_at)], ["updated", fmtTime(claim.updated_at)],
        ["evidence ids", (claim.evidence_ids || []).join(", ") || "none"],
      ]),
      linkedArtifacts.length ? detailSection("Loaded linked artifacts",
        el("div", { class: "proof-stack" }, linkedArtifacts.map(artifactCard))) : null) });
}

function detailList(rows) {
  return el("dl", { class: "kv" }, rows.flatMap(([key, value]) => [
    el("dt", {}, key), el("dd", { title: String(value ?? "") }, truncate(value ?? "—", 180)),
  ]));
}

function detailSection(title, body) {
  return el("div", { style: { marginTop: "20px" } },
    el("div", { class: "eyebrow", style: { marginBottom: "8px" } }, title), body);
}

function shortBuild(binaryId) {
  const value = String(binaryId || "");
  if (value.length <= 54) return value;
  return value.slice(0, 28) + "…" + value.slice(-20);
}
