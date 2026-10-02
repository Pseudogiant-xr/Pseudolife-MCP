import { describe, expect, it } from "vitest";
import type { ReviewFinding } from "./api/graph";
import {
  bestMatch,
  degreeMap,
  edgeKind,
  extraFlagsFor,
  flaggedNames,
  flagView,
  graphQuery,
  mergeFlags,
  neighborhood,
  nodeSize,
  orphanCount,
  parseGraphQuery,
  pendingDecisions,
  scopeOptions,
  sliderCut,
  tableRows,
  timeSpan,
  truncationNotice,
} from "./graph";

const edges = [
  { src: "daemon", relation: "stores-data-in", dst: "postgres" },
  { src: "daemon", relation: "uses", dst: "extractor" },
  { src: "postgres", relation: "runs-on", dst: "docker" },
];

describe("degree and size", () => {
  it("counts each edge once at each end", () => {
    const d = degreeMap(edges);
    expect(d.get("daemon")).toBe(2);
    expect(d.get("postgres")).toBe(2);
    expect(d.get("docker")).toBe(1);
    expect(d.get("lonely")).toBeUndefined();
  });

  it("counts a self-loop at both ends", () => {
    expect(degreeMap([{ src: "a", dst: "a" }]).get("a")).toBe(2);
  });

  it("sizes a star by connections plus 0.6 per fact", () => {
    expect(nodeSize(0, 0)).toBe(1);
    expect(nodeSize(3, 0)).toBe(4);
    expect(nodeSize(2, 5)).toBeCloseTo(6);
  });

  it("counts the orphans (no connection at all)", () => {
    const nodes = [{ entity: "daemon" }, { entity: "lonely" }, { entity: "docker" }, { entity: "alone" }];
    expect(orphanCount(nodes, degreeMap(edges))).toBe(2);
  });
});

describe("bestMatch", () => {
  const d = degreeMap(edges);
  const names = ["docker", "daemon", "postgres", "extractor"];

  it("picks the most connected name containing the query, case-insensitively", () => {
    expect(bestMatch(names, d, "D")).toBe("daemon");
    expect(bestMatch(names, d, "  post ")).toBe("postgres");
  });

  it("keeps input order on a tie", () => {
    expect(bestMatch(["b-two", "a-two"], new Map([["a-two", 1], ["b-two", 1]]), "two")).toBe("b-two");
  });

  it("returns null for no match or an empty query", () => {
    expect(bestMatch(names, d, "zzz")).toBeNull();
    expect(bestMatch(names, d, "   ")).toBeNull();
  });
});

describe("neighborhood (isolate)", () => {
  const names = ["daemon", "postgres", "extractor", "docker", "far"];
  const chain = [...edges, { src: "docker", relation: "hosts", dst: "far" }];

  it("keeps everything within two hops, either direction", () => {
    expect(neighborhood(names, chain, "daemon", 2)).toEqual(new Set(["daemon", "postgres", "extractor", "docker"]));
    expect(neighborhood(names, chain, "far", 1)).toEqual(new Set(["far", "docker"]));
  });

  it("isolates an orphan to itself and refuses an unknown name", () => {
    expect(neighborhood([...names, "alone"], chain, "alone")).toEqual(new Set(["alone"]));
    expect(neighborhood(names, chain, "ghost")).toBeNull();
  });
});

describe("scopeOptions", () => {
  it("puts umbrellas first, each followed by its children, then orphan children", () => {
    const opts = scopeOptions([
      { source: "pseudolife", entities: 900 },
      { source: "homelab", entities: 300 },
      { source: "pseudolife-mcp", entities: 120, parent: "pseudolife" },
      { source: "nas", entities: 40, parent: "homelab" },
      { source: "stray", entities: 7, parent: "gone-umbrella" },
      { source: "console", entities: 30, parent: "pseudolife" },
    ]);
    expect(opts.map((o) => o.value)).toEqual(["all", "pseudolife", "pseudolife-mcp", "console", "homelab", "nas", "stray"]);
    expect(opts[0]).toEqual({ value: "all", label: "All projects", child: false });
    expect(opts[1]).toEqual({ value: "pseudolife", label: "pseudolife (900)", child: false });
    expect(opts[2]).toEqual({ value: "pseudolife-mcp", label: "↳ pseudolife-mcp (120)", child: true });
    expect(opts[6]).toEqual({ value: "stray", label: "↳ stray (7)", child: true });
  });

  it("offers only All projects when the list is empty", () => {
    expect(scopeOptions([])).toEqual([{ value: "all", label: "All projects", child: false }]);
  });
});

const findings: ReviewFinding[] = [
  { type: "duplicate", entities: ["Cortex Console", "cortex console web"] },
  { type: "merge_candidate", merges: [{ id: 1, from: "live daemon", into: "daemon" }, { id: 3, from: "events v2", into: "events_pass_v2" }] },
  { type: "junk_candidate", entities: [{ id: 2, entity: "2", reason: "bare-number" }] },
  { type: "proposed_link", links: [{ id: 7, src: "Track A", relation: "related-to", dst: "Track B" }] },
  // Bulk hygiene: never pulses.
  { type: "orphan", entities: ["lonely-1", "lonely-2"] },
  { type: "unattributed", entities: ["alpha"] },
  { type: "dubious_edge" },
  { type: "test_artifact", entities: ["payments-db"] },
];

describe("which findings flag a star", () => {
  it("flags only item-level decisions: duplicates, merges, junk and proposed links", () => {
    expect(flaggedNames(findings)).toEqual(
      new Set(["Cortex Console", "cortex console web", "live daemon", "daemon", "events v2", "events_pass_v2", "2", "Track A", "Track B"]),
    );
  });

  it("flags nothing without a scan", () => {
    expect(flaggedNames(null).size).toBe(0);
  });

  it("counts each pending item-level decision once", () => {
    expect(pendingDecisions(findings)).toBe(1 + 2 + 1 + 1);
    expect(pendingDecisions(null)).toBeNull();
    expect(pendingDecisions([])).toBe(0);
  });
});

describe("entity page flags", () => {
  it("shapes the findings that touch an entity like wiki flags", () => {
    expect(extraFlagsFor(findings, "daemon")).toEqual([{ kind: "merge_candidate", id: 1, entity: "live daemon", into: "daemon" }]);
    expect(extraFlagsFor(findings, "cortex console web")).toEqual([
      { kind: "duplicate", a: "Cortex Console", b: "cortex console web", other: "Cortex Console" },
    ]);
    expect(extraFlagsFor(findings, "Track B")).toEqual([
      { kind: "proposed_link", id: 7, src: "Track A", relation: "related-to", dst: "Track B" },
    ]);
    expect(extraFlagsFor(findings, "2")).toEqual([{ kind: "junk_candidate", id: 2, entity: "2" }]);
    expect(extraFlagsFor(findings, "lonely-1")).toEqual([]);
  });

  it("maps the live wiki's table kinds and dedupes, preferring the scan's copy", () => {
    const server = [
      { kind: "merge", id: 1, entity: "daemon", into: "live daemon" },
      { kind: "unattributed" },
    ];
    const extra = [{ kind: "merge_candidate", id: 1, entity: "live daemon", into: "daemon" }];
    expect(mergeFlags(server, extra)).toEqual([
      { kind: "merge_candidate", id: 1, entity: "live daemon", into: "daemon" },
      { kind: "unattributed" },
    ]);
    expect(mergeFlags([{ kind: "junk", id: 4, entity: "x" }], []).map((f) => f.kind)).toEqual(["junk_candidate"]);
  });

  it("gives each flag kind its review decisions", () => {
    expect(flagView({ kind: "duplicate", a: "a", b: "b", other: "b" }, "a").buttons.map((b) => b.action)).toEqual([
      { kind: "merge-named", a: "a", b: "b" },
      { kind: "dismiss-duplicate", a: "a", b: "b" },
    ]);
    expect(flagView({ kind: "unattributed" }, "nas").buttons.map((b) => b.action)).toEqual([{ kind: "assign", entities: ["nas"] }]);
    expect(flagView({ kind: "proposed_link", id: 7, src: "A", relation: "r", dst: "B" }, "A").buttons.map((b) => b.action)).toEqual([
      { kind: "accept-link", id: 7 },
      { kind: "reject-link", id: 7 },
    ]);
    expect(flagView({ kind: "merge_candidate", id: 1, entity: "x", into: "y" }, "x").buttons.map((b) => b.action)).toEqual([
      { kind: "merge-entity", id: 1, from: "x", into: "y" },
      { kind: "reject-entity", id: 1 },
    ]);
    const junk = flagView({ kind: "junk_candidate", id: 2, entity: "2" }, "2");
    expect(junk.buttons.map((b) => b.action)).toEqual([
      { kind: "junk-entity", id: 2, entity: "2" },
      { kind: "reject-entity", id: 2 },
    ]);
    expect(junk.buttons[0].danger).toBe(true);
  });

  it("renders an unknown kind as plain text with no buttons", () => {
    expect(flagView({ kind: "mystery", entity: "a", into: "b" }, "a")).toEqual({ text: "mystery: a → b", buttons: [] });
  });
});

describe("time scrubber", () => {
  it("spans the oldest to newest creation or assertion", () => {
    expect(timeSpan([{ created_at: 100 }, { created_at: null }], [{ asserted_at: 300 }, { asserted_at: 0 }])).toEqual({ t0: 100, t1: 300 });
  });

  it("needs two timestamps with a spread", () => {
    expect(timeSpan([{ created_at: 100 }], [])).toBeNull();
    expect(timeSpan([{ created_at: 100 }, { created_at: 100 }], [])).toBeNull();
  });

  it("maps the slider to a cut, with the right end meaning now", () => {
    const span = { t0: 1000, t1: 2000 };
    expect(sliderCut(1000, span)).toBeNull();
    expect(sliderCut(0, span)).toBe(1000);
    expect(sliderCut(500, span)).toBe(1500);
  });
});

describe("table rows", () => {
  const data = {
    nodes: [{ entity: "daemon" }, { entity: "lonely" }, { entity: "postgres" }],
    edges: [{ src: "daemon", relation: "stores-data-in", dst: "postgres" }],
  };
  const d = degreeMap(data.edges);

  it("hides orphans and filters by name or relation", () => {
    expect(tableRows(data, d, { hideOrphans: true, query: "" }).nodes.map((n) => n.entity)).toEqual(["daemon", "postgres"]);
    const r = tableRows(data, d, { hideOrphans: false, query: "STORES" });
    expect(r.nodes).toEqual([]);
    expect(r.edges).toHaveLength(1);
  });

  it("names how each relation is known", () => {
    expect(edgeKind({ derived: true, tag: "INFERRED" })).toEqual({ label: "derived", tone: "" });
    expect(edgeKind({ tag: "EXTRACTED" })).toEqual({ label: "extracted", tone: "ok" });
    expect(edgeKind({ tag: "AMBIGUOUS" })).toEqual({ label: "ambiguous", tone: "warn" });
    expect(edgeKind({})).toEqual({ label: "explicit", tone: "ok" });
  });
});

describe("truncation notice", () => {
  it("says how much is shown and, for all projects, how to see it all", () => {
    const data = { truncated: true, nodes: new Array(2000).fill({}), total_nodes: 3412 };
    expect(truncationNotice(data, "all")).toBe("Showing the 2,000 most connected of 3,412 entities; pick a project to map it in full.");
    expect(truncationNotice(data, "pseudolife")).toBe("Showing the 2,000 most connected of 3,412 entities.");
    expect(truncationNotice({ truncated: false, nodes: [] }, "all")).toBeNull();
  });
});

describe("address bar", () => {
  const cur = { scope: "pseudolife", view: "table" as const };

  it("keeps the current scope and view when the link leaves them out", () => {
    expect(parseGraphQuery({}, cur)).toEqual({ entity: "", scope: "pseudolife", view: "table" });
  });

  it("forces the galaxy for an explicit entity unless view=table", () => {
    expect(parseGraphQuery({ entity: "daemon" }, cur)).toEqual({ entity: "daemon", scope: "pseudolife", view: "galaxy" });
    expect(parseGraphQuery({ entity: "daemon", view: "table", scope: "nas" }, cur)).toEqual({ entity: "daemon", scope: "nas", view: "table" });
  });

  it("writes only non-default values", () => {
    expect(graphQuery({ entity: "", scope: "all", view: "galaxy" })).toEqual({});
    expect(graphQuery({ entity: "a b", scope: "nas", view: "table" })).toEqual({ entity: "a b", scope: "nas", view: "table" });
  });
});
