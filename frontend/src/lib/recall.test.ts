import { describe, expect, it } from "vitest";
import {
  clampHops,
  pathSteps,
  readPath,
  readRecallQuery,
  recallIsEmpty,
  recallQueryOf,
  seedView,
  splitEntities,
} from "./recall";

describe("address bar", () => {
  it("defaults to multi-hop with three hops", () => {
    expect(readRecallQuery({})).toEqual({ mode: "hops", q: "", hops: 3, source: "", target: "" });
  });

  it("reads path mode and clamps hops", () => {
    expect(readRecallQuery({ mode: "path", source: "a", target: "b" })).toMatchObject({ mode: "path", source: "a", target: "b" });
    expect(readRecallQuery({ q: "x", hops: "9" }).hops).toBe(5);
    expect(readRecallQuery({ q: "x", hops: "0" }).hops).toBe(1);
    expect(clampHops("junk")).toBe(3);
  });

  it("writes only the active mode's inputs", () => {
    expect(recallQueryOf({ mode: "hops", q: "daemon", hops: 3, source: "a", target: "b" })).toEqual({ q: "daemon" });
    expect(recallQueryOf({ mode: "hops", q: "daemon", hops: 2, source: "", target: "" })).toEqual({ q: "daemon", hops: "2" });
    expect(recallQueryOf({ mode: "path", q: "daemon", hops: 2, source: "a", target: "" })).toEqual({ mode: "path", source: "a" });
  });
});

describe("seeds and entities", () => {
  const seeds = Array.from({ length: 18 }, (_, i) => `s${i}`);

  it("caps seeds at fifteen with a remainder until expanded", () => {
    expect(seedView(seeds, false)).toEqual({ shown: seeds.slice(0, 15), more: 3 });
    expect(seedView(seeds, true)).toEqual({ shown: seeds, more: 0 });
    expect(seedView(["a"], false)).toEqual({ shown: ["a"], more: 0 });
  });

  it("splits entities by whether they carry facts", () => {
    const { withFacts, bare } = splitEntities([
      { entity: "a", facts: [{ attribute: "x", value: "1" }] },
      { entity: "b", facts: [] },
      { entity: "c" },
    ]);
    expect(withFacts.map((e) => e.entity)).toEqual(["a"]);
    expect(bare.map((e) => e.entity)).toEqual(["b", "c"]);
  });

  it("calls an answer with no seeds, entities or texts empty", () => {
    expect(recallIsEmpty({ seeds: [], entities: [], texts: [] })).toBe(true);
    expect(recallIsEmpty({ texts: ["t"] })).toBe(false);
  });
});

describe("path", () => {
  it("chains the path forward", () => {
    const steps = pathSteps(["a", "b", "c"], [
      { src: "a", relation: "uses", dst: "b" },
      { src: "b", relation: "runs-on", dst: "c" },
    ]);
    expect(steps).toEqual([
      { kind: "node", name: "a" },
      { kind: "edge", relation: "uses", forward: true },
      { kind: "node", name: "b" },
      { kind: "edge", relation: "runs-on", forward: true },
      { kind: "node", name: "c" },
    ]);
  });

  it("marks an edge stored against the walk as backward instead of breaking the chain", () => {
    const steps = pathSteps(["a", "b", "c"], [
      { src: "b", relation: "depends-on", dst: "a" },
      { src: "b", relation: "runs-on", dst: "c" },
    ]);
    expect(steps.map((s) => (s.kind === "node" ? s.name : `${s.relation}:${s.forward}`))).toEqual([
      "a",
      "depends-on:false",
      "b",
      "runs-on:true",
      "c",
    ]);
  });

  it("falls back to the served edges when the path does not line up", () => {
    const steps = pathSteps(undefined, [{ src: "a", relation: "uses", dst: "b" }]);
    expect(steps).toEqual([
      { kind: "node", name: "a" },
      { kind: "edge", relation: "uses", forward: true },
      { kind: "node", name: "b" },
    ]);
  });

  it("reads found, missing, no-path and refused answers", () => {
    expect(readPath({ found: false, missing: "ghost" })).toEqual({ kind: "missing", name: "ghost" });
    expect(readPath({ found: true, path: [], edges: [], hops: null })).toEqual({ kind: "none" });
    expect(readPath({ error: "graph_requires_postgres", hint: "The graph lives in Postgres" })).toEqual({
      kind: "refused",
      message: "The graph lives in Postgres",
    });
    const found = readPath({ found: true, path: ["a", "b"], edges: [{ src: "a", relation: "r", dst: "b" }], hops: 1 });
    expect(found.kind === "found" && found.hops).toBe(1);
  });
});
