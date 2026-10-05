import { describe, expect, it } from "vitest";
import { barPct, maxDegree, splitBackticks, topCommunities } from "./insight";

describe("splitBackticks", () => {
  it("marks backtick-quoted segments as code", () => {
    expect(splitBackticks("Which value of `model` for `dream extractor` is correct?")).toEqual([
      { text: "Which value of ", code: false },
      { text: "model", code: true },
      { text: " for ", code: false },
      { text: "dream extractor", code: true },
      { text: " is correct?", code: false },
    ]);
  });

  it("handles leading code, plain text, empty and missing input", () => {
    expect(splitBackticks("`x` connects")).toEqual([
      { text: "x", code: true },
      { text: " connects", code: false },
    ]);
    expect(splitBackticks("no code here")).toEqual([{ text: "no code here", code: false }]);
    expect(splitBackticks("")).toEqual([]);
    expect(splitBackticks(null)).toEqual([]);
  });

  it("keeps markup as plain text", () => {
    expect(splitBackticks("`<img src=x onerror=alert(1)>`")).toEqual([{ text: "<img src=x onerror=alert(1)>", code: true }]);
  });

  it("treats an unclosed backtick's tail as code, like the classic console", () => {
    expect(splitBackticks("a `b")).toEqual([
      { text: "a ", code: false },
      { text: "b", code: true },
    ]);
  });
});

describe("community and hub scales", () => {
  it("ranks the largest communities first and keeps ties in order", () => {
    const comms = [
      { label: "a", size: 3 },
      { label: "b", size: 9 },
      { label: "c", size: 3 },
      { label: "d" },
      ...Array.from({ length: 8 }, (_, i) => ({ label: `x${i}`, size: 1 })),
    ];
    expect(topCommunities(comms, 4).map((c) => c.label)).toEqual(["b", "a", "c", "x0"]);
    expect(topCommunities(comms)).toHaveLength(8);
  });

  it("scales bars against the largest value, never dividing by zero", () => {
    expect(maxDegree([{ display: "a", degree: 12 }, { display: "b", degree: 6 }])).toBe(12);
    expect(maxDegree([])).toBe(1);
    expect(barPct(6, 12)).toBe(50);
    expect(barPct(null, 12)).toBe(0);
    expect(barPct(20, 12)).toBe(100);
  });
});
