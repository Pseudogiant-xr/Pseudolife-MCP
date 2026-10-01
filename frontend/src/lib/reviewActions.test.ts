import { describe, expect, it } from "vitest";
import { planCalls } from "./reviewActions";

// Pins each review decision to the exact POSTs the classic console sent
// (static/js/views/graph.js actOnFinding), so the port changes nothing the
// daemon sees.
describe("planCalls", () => {
  it("merges a named duplicate into the survivor the person picks", () => {
    const p = planCalls({ kind: "merge-named", a: "claude shim", b: "Claude CLI shim" });
    expect(p.ask).toEqual({ type: "pick-survivor", a: "claude shim", b: "Claude CLI shim" });
    expect(p.calls("claude shim")).toEqual([{ path: "/api/graph/merge", body: { from: "Claude CLI shim", into: "claude shim" } }]);
    expect(p.calls("Claude CLI shim")).toEqual([{ path: "/api/graph/merge", body: { from: "claude shim", into: "Claude CLI shim" } }]);
  });

  it("sends nothing for a survivor that is neither name", () => {
    expect(planCalls({ kind: "merge-named", a: "x", b: "y" }).calls("z")).toEqual([]);
    expect(planCalls({ kind: "merge-named", a: "x", b: "y" }).calls(undefined)).toEqual([]);
  });

  it("confirms filed merges and junk deletions, junk as danger", () => {
    const m = planCalls({ kind: "merge-entity", id: 7, from: "a", into: "b" });
    expect(m.ask.type).toBe("confirm");
    expect(m.ask.type === "confirm" && m.ask.danger).toBe(false);
    expect(m.calls()).toEqual([{ path: "/api/graph/accept-entity-merge", body: { id: 7 } }]);
    const j = planCalls({ kind: "junk-entity", id: 9, entity: "tmp" });
    expect(j.ask.type === "confirm" && j.ask.danger).toBe(true);
    expect(j.calls()).toEqual([{ path: "/api/graph/accept-entity-junk", body: { id: 9 } }]);
  });

  it("rejects and link decisions go out without a confirm", () => {
    expect(planCalls({ kind: "reject-entity", id: 1 }).ask.type).toBe("none");
    expect(planCalls({ kind: "reject-entity", id: 1 }).calls()).toEqual([{ path: "/api/graph/reject-entity-proposal", body: { id: 1 } }]);
    expect(planCalls({ kind: "accept-link", id: 2 }).calls()).toEqual([{ path: "/api/graph/accept-proposal", body: { id: 2 } }]);
    expect(planCalls({ kind: "reject-link", id: 3 }).calls()).toEqual([{ path: "/api/graph/reject-proposal", body: { id: 3 } }]);
  });

  it("relates a duplicate pair, then marks it distinct", () => {
    const p = planCalls({ kind: "relate-named", src: "file", relation: "implements", dst: "concept" });
    expect(p.calls()).toEqual([
      { path: "/api/graph/relate", body: { src: "file", relation: "implements", dst: "concept" } },
      { path: "/api/graph/dismiss-duplicate", body: { a: "file", b: "concept" } },
    ]);
  });

  it("confirms the permanent distinct verdicts", () => {
    const d = planCalls({ kind: "dismiss-duplicate", a: "a", b: "b" });
    expect(d.ask.type).toBe("confirm");
    expect(d.calls()).toEqual([{ path: "/api/graph/dismiss-duplicate", body: { a: "a", b: "b" } }]);
    const s = planCalls({
      kind: "dismiss-slot-pair",
      store: "lesson",
      a: { entity: "e1", attribute: "x" },
      b: { entity: "e2", attribute: "y" },
    });
    expect(s.ask.type).toBe("confirm");
    expect(s.calls()).toEqual([
      { path: "/api/curation/dismiss-duplicate", body: { store: "lesson", a_entity: "e1", a_attribute: "x", b_entity: "e2", b_attribute: "y" } },
    ]);
  });

  it("blesses without a confirm and prunes with a danger confirm, one call per edge", () => {
    const edges = [
      { src: "a", relation: "r", dst: "b" },
      { src: "c", relation: "s", dst: "d" },
    ];
    const b = planCalls({ kind: "bless", edges });
    expect(b.ask.type).toBe("none");
    expect(b.calls().map((c) => c.path)).toEqual(["/api/graph/bless-edge", "/api/graph/bless-edge"]);
    const p = planCalls({ kind: "prune", edges });
    expect(p.ask.type === "confirm" && p.ask.danger).toBe(true);
    expect(p.calls()).toEqual([
      { path: "/api/graph/unrelate", body: { src: "a", relation: "r", dst: "b" } },
      { path: "/api/graph/unrelate", body: { src: "c", relation: "s", dst: "d" } },
    ]);
  });

  it("deletes names with a danger confirm, one call per entity", () => {
    const p = planCalls({ kind: "delete-names", entities: ["x", "y"] });
    expect(p.ask.type === "confirm" && p.ask.danger).toBe(true);
    expect(p.calls()).toEqual([
      { path: "/api/graph/delete-entity", body: { entity: "x" } },
      { path: "/api/graph/delete-entity", body: { entity: "y" } },
    ]);
  });

  it("assigns a trimmed project and sends nothing for a blank one", () => {
    const p = planCalls({ kind: "assign", entities: ["x", "y"] });
    expect(p.ask).toEqual({ type: "pick-project", count: 2 });
    expect(p.calls("  Pseudolife-MCP ")).toEqual([
      { path: "/api/graph/assign-scope", body: { entity: "x", source: "Pseudolife-MCP" } },
      { path: "/api/graph/assign-scope", body: { entity: "y", source: "Pseudolife-MCP" } },
    ]);
    expect(p.calls("   ")).toEqual([]);
  });
});
