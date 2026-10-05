import { describe, expect, it } from "vitest";
import type { Finding, ReviewResponse } from "./api/review";
import {
  automationCounts,
  automationText,
  bulkActions,
  bulkRows,
  countByFilter,
  decisionSubject,
  duplicateActions,
  filterFindings,
  itemCount,
  judgeChip,
  junkActions,
  linkActions,
  mergeActions,
  needsPersonCount,
  parseTypeFilter,
  pruneSelection,
  scopeOptions,
  selectedRows,
  selectVisible,
  slotPairActions,
  toggleKey,
  visibleRows,
} from "./review";
import { planCalls } from "./reviewActions";

// A queue shaped like service.graph_review's answer (memory/graph_review.py).
const findings: Finding[] = [
  { type: "duplicate", action: "merge", entities: ["claude shim", "Claude CLI shim"], score: 0.8, automation: { state: "unfiled", reason: "awaiting bounded analyzer scan" } },
  { type: "duplicate", action: "relate", suggested_relation: "implements", entities: ["band.py", "band"], automation: { state: "gated", reason: "analyzer filing disabled" } },
  { type: "test_artifact", entities: ["payments-db", "pl-healthcheck-target"], automation: { state: "manual", reason: "no automated filing path" } },
  {
    type: "dubious_edge",
    edges: [
      { src: "a", relation: "r", dst: "b", confidence: 0.4 },
      { src: "c", relation: "s", dst: "d", confidence: 0.3 },
      { src: "e", relation: "r", dst: "f", confidence: 0.2 },
    ],
    automation: { state: "manual", reason: "no automated filing path" },
  },
  { type: "orphan", entities: ["lonely-1", "lonely-2"], automation: { state: "manual" } },
  { type: "unattributed", entities: ["alpha", "beta", "gamma"], automation: { state: "manual" } },
  { type: "proposed_link", links: [{ id: 1, src: "A", relation: "related-to", dst: "B" }], automation: { state: "pending", reason: "filed for review" } },
  {
    type: "merge_candidate",
    merges: [
      { id: 1, from: "live daemon", into: "daemon" },
      { id: 3, from: "events v2", into: "events_pass_v2", group: "events v2" },
    ],
    automation: { state: "pending", reason: "filed for review" },
  },
  { type: "junk_candidate", entities: [{ id: 2, entity: "2", reason: "bare-number" }], automation: { state: "pending" } },
];

describe("grouping and counting findings", () => {
  it("counts one decision per proposal, name or edge", () => {
    expect(findings.map(itemCount)).toEqual([1, 1, 2, 3, 2, 3, 1, 2, 1]);
  });

  it("counts items per type filter, duplicates under merges", () => {
    expect(countByFilter(findings)).toEqual({
      all: 16,
      merges: 4,
      links: 1,
      junk: 1,
      edges: 3,
      unattributed: 3,
      test_artifact: 2,
      orphan: 2,
    });
  });

  it("filters findings by type and keeps unknown types under All only", () => {
    const withUnknown = [...findings, { type: "something_new", label: "new" }];
    expect(filterFindings(withUnknown, "merges").map((f) => f.type)).toEqual(["duplicate", "duplicate", "merge_candidate"]);
    expect(filterFindings(withUnknown, "all")).toHaveLength(10);
    expect(countByFilter(withUnknown).all).toBe(17);
  });

  it("falls back to All for an unknown filter in the address bar", () => {
    expect(parseTypeFilter("links")).toBe("links");
    expect(parseTypeFilter("bogus")).toBe("all");
    expect(parseTypeFilter(undefined)).toBe("all");
  });
});

describe("automation state wording", () => {
  it("names each state and says which ones need a person", () => {
    expect(automationText({ state: "unfiled", reason: "awaiting bounded analyzer scan" })).toEqual({
      state: "unfiled",
      label: "Waiting to be queued",
      reason: "awaiting bounded analyzer scan",
      tone: "",
      needsPerson: false,
    });
    expect(automationText({ state: "pending" })?.label).toBe("Queued");
    expect(automationText({ state: "terminal" })?.label).toBe("Already decided");
    const gated = automationText({ state: "gated", reason: "review-queue judges disabled" });
    expect(gated?.label).toBe("Automation paused");
    expect(gated?.tone).toBe("warn");
    expect(gated?.needsPerson).toBe(true);
    expect(automationText({ state: "manual" })).toMatchObject({ label: "Manual review", needsPerson: true, reason: "" });
  });

  it("shows an unknown state in words and nothing for a missing one", () => {
    expect(automationText({ state: "held_back" })?.label).toBe("held back");
    expect(automationText(undefined)).toBeNull();
    expect(automationText({ state: "" })).toBeNull();
  });

  it("prefers the daemon's tally, else counts the findings, else unavailable", () => {
    const tallied: ReviewResponse = { findings: [], automation: { unfiled: 1, pending: 2, gated: 3, manual: 4, terminal: 5 } };
    expect(automationCounts(tallied)).toEqual({ unfiled: 1, pending: 2, gated: 3, manual: 4, terminal: 5 });
    const counted = automationCounts({ findings });
    expect(counted).toEqual({ unfiled: 1, pending: 3, gated: 1, manual: 4, terminal: 0 });
    expect(needsPersonCount(counted)).toBe(5);
    expect(automationCounts({ findings: [{ type: "orphan", entities: ["x"] }] })).toBeNull();
    expect(needsPersonCount(null)).toBeNull();
    expect(automationCounts(null)).toBeNull();
  });
});

describe("bulk selection over a filtered list", () => {
  const edges = bulkRows(findings[3]);
  const names = bulkRows(findings[5]);

  it("builds one row per edge or name, keyed stably", () => {
    expect(edges.map((r) => r.text)).toEqual(["a r b", "c s d", "e r f"]);
    expect(edges[0].entities).toEqual(["a", "b"]);
    expect(names.map((r) => r.key)).toEqual(["alpha", "beta", "gamma"]);
    expect(bulkRows({ type: "orphan", entities: ["x", "x", "y"] }).map((r) => r.key)).toEqual(["x", "y"]);
  });

  it("filters case-insensitively; an empty filter shows every row", () => {
    expect(visibleRows(names, "  AL ")).toHaveLength(1);
    expect(visibleRows(names, "")).toHaveLength(3);
    expect(visibleRows(edges, " r ").map((r) => r.text)).toEqual(["a r b", "e r f"]);
  });

  it("select all selects only the visible rows and keeps earlier picks", () => {
    expect(selectVisible([], names, "a").sort()).toEqual(["alpha", "beta", "gamma"]);
    expect(selectVisible([], names, "ph")).toEqual(["alpha"]);
    expect(selectVisible(["gamma"], names, "be").sort()).toEqual(["beta", "gamma"]);
    expect(selectVisible([], names, "zzz")).toEqual([]);
  });

  it("toggles, prunes vanished rows and maps keys back to rows in list order", () => {
    let sel = toggleKey([], "gamma", true);
    sel = toggleKey(sel, "alpha", true);
    sel = toggleKey(sel, "gamma", false);
    expect(sel).toEqual(["alpha"]);
    expect(pruneSelection(["alpha", "gone"], names)).toEqual(["alpha"]);
    expect(selectedRows(names, ["gamma", "alpha"]).map((r) => r.key)).toEqual(["alpha", "gamma"]);
  });
});

describe("mapping finding rows to review actions", () => {
  it("offers bulk buttons that carry exactly the picked rows", () => {
    const rows = bulkRows(findings[3]);
    const [keep, prune] = bulkActions(findings[3], selectedRows(rows, [rows[0].key, rows[2].key]));
    expect(keep.label).toBe("Keep 2 edges");
    expect(keep.action).toEqual({ kind: "bless", edges: [findings[3].edges![0], findings[3].edges![2]] });
    expect(prune.tone).toBe("danger");
    expect(prune.action.kind).toBe("prune");
    expect(planCalls(prune.action).calls()).toEqual([
      { path: "/api/graph/unrelate", body: { src: "a", relation: "r", dst: "b" } },
      { path: "/api/graph/unrelate", body: { src: "e", relation: "r", dst: "f" } },
    ]);
  });

  it("disables bulk buttons with nothing picked", () => {
    for (const b of bulkActions(findings[4], [])) expect(b.disabled).toBe(true);
    expect(bulkActions(findings[4], []).map((b) => b.action.kind)).toEqual(["assign", "delete-names"]);
  });

  it("assigns and deletes by name", () => {
    const rows = bulkRows(findings[5]);
    const [assign] = bulkActions(findings[5], rows.slice(0, 1));
    expect(assign.action).toEqual({ kind: "assign", entities: ["alpha"] });
    expect(assign.label).toBe("Assign 1 entity to a project");
    const [del] = bulkActions(findings[2], bulkRows(findings[2]));
    expect(del.action).toEqual({ kind: "delete-names", entities: ["payments-db", "pl-healthcheck-target"] });
    expect(del.label).toBe("Delete 2 entities");
    expect(bulkActions({ type: "merge_candidate" }, [])).toEqual([]);
  });

  it("maps proposals to accept and reject by id", () => {
    const m = findings[7].merges![0];
    expect(mergeActions(m).map((b) => b.action)).toEqual([
      { kind: "merge-entity", id: 1, from: "live daemon", into: "daemon" },
      { kind: "reject-entity", id: 1 },
    ]);
    expect(junkActions({ id: 2, entity: "2" }).map((b) => b.action)).toEqual([
      { kind: "junk-entity", id: 2, entity: "2" },
      { kind: "reject-entity", id: 2 },
    ]);
    expect(linkActions(findings[6].links![0]).map((b) => b.action)).toEqual([
      { kind: "accept-link", id: 1 },
      { kind: "reject-link", id: 1 },
    ]);
  });

  it("offers merge and mark distinct on a duplicate, relate first on a file/concept pair", () => {
    expect(duplicateActions(findings[0]).map((b) => [b.label, b.tone, b.action])).toEqual([
      ["Merge", "primary", { kind: "merge-named", a: "claude shim", b: "Claude CLI shim" }],
      ["Mark distinct", "ghost", { kind: "dismiss-duplicate", a: "claude shim", b: "Claude CLI shim" }],
    ]);
    const rel = duplicateActions(findings[1]);
    expect(rel.map((b) => b.label)).toEqual(["Relate (implements)", "Merge", "Mark distinct"]);
    expect(rel[0].action).toEqual({ kind: "relate-named", src: "band.py", relation: "implements", dst: "band" });
    expect(rel[1].tone).toBe("secondary");
    expect(duplicateActions({ type: "duplicate", action: "relate", entities: ["f.py", "f"] })[0].label).toBe("Relate (implements)");
    expect(duplicateActions({ type: "duplicate", entities: ["only one"] })).toEqual([]);
  });

  it("marks a curation pair distinct by its two slots", () => {
    const [b] = slotPairActions("world", {
      a: { entity: "MCP spec", attribute: "session identity", source_url: "https://example.com" },
      b: { entity: "MCP specification", attribute: "session-id status" },
      similarity: 0.88,
    });
    expect(b.action).toEqual({
      kind: "dismiss-slot-pair",
      store: "world",
      a: { entity: "MCP spec", attribute: "session identity" },
      b: { entity: "MCP specification", attribute: "session-id status" },
    });
  });
});

describe("presenters", () => {
  it("shows the judge's verdict, relation and confidence", () => {
    expect(judgeChip({ verdict: "accept", confidence: 0.84 })).toEqual({ text: "accept 0.84", tone: "ok" });
    expect(judgeChip({ verdict: "relate", relation: "implements" })).toEqual({ text: "relate implements", tone: "danger" });
    expect(judgeChip({ verdict: "unsure" })?.tone).toBe("lessons");
    expect(judgeChip({ verdict: "" })).toBeNull();
  });

  it("names a decision by its pair, else its proposal", () => {
    expect(decisionSubject({ pair: ["a", "b"] })).toBe("a / b");
    expect(decisionSubject({ proposal_id: 7 })).toBe("proposal 7");
    expect(decisionSubject({})).toBe("");
  });
});

describe("scope picker", () => {
  it("lists umbrellas before their children, then children of absent umbrellas", () => {
    const opts = scopeOptions([
      { source: "homelab", entities: 3, parent: "lab" },
      { source: "pseudolife", entities: 1200 },
      { source: "pseudolife-mcp", entities: 40, parent: "pseudolife" },
    ]);
    expect(opts).toEqual([
      { value: "all", label: "All projects", child: false },
      { value: "pseudolife", label: "pseudolife (1 200)", child: false },
      { value: "pseudolife-mcp", label: "pseudolife-mcp (40)", child: true },
      { value: "homelab", label: "homelab (3)", child: true },
    ]);
  });

  it("keeps a scope from the address bar that the daemon no longer lists", () => {
    expect(scopeOptions([], "gone").map((o) => o.value)).toEqual(["all", "gone"]);
    expect(scopeOptions([], "all")).toHaveLength(1);
  });
});
