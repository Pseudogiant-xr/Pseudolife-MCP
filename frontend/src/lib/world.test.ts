import { describe, expect, it } from "vitest";
import type { WorldFact } from "./api/facts";
import { filterWorld, staleCount, worldCheckedAt, worldConfidence } from "./world";

const w = (over: Partial<WorldFact>): WorldFact => ({ entity: "e", attribute: "a", value: "v", ...over });

describe("world facts", () => {
  it("filter on entity, attribute and value, sorted by entity.attribute", () => {
    const rows = [
      w({ entity: "zep", attribute: "headline", value: "graph" }),
      w({ entity: "anthropic", attribute: "model", value: "Opus" }),
      w({ entity: "anthropic", attribute: "hq", value: "SF" }),
    ];
    expect(filterWorld(rows, "").map((x) => `${x.entity}.${x.attribute}`)).toEqual(["anthropic.hq", "anthropic.model", "zep.headline"]);
    expect(filterWorld(rows, "OPUS")).toHaveLength(1);
    expect(rows[0].entity).toBe("zep"); // input untouched
  });

  it("prefer the age-decayed confidence", () => {
    expect(worldConfidence({ effective_confidence: 0.4, confidence: 0.9 })).toBe(0.4);
    expect(worldConfidence({ confidence: 0.9 })).toBe(0.9);
    expect(worldConfidence({})).toBeNull();
  });

  it("date a fact by its last confirmation, else retrieval, else assertion", () => {
    expect(worldCheckedAt({ last_confirmed: 3, retrieved_at: 2, asserted_at: 1 })).toBe(3);
    expect(worldCheckedAt({ retrieved_at: 2, asserted_at: 1 })).toBe(2);
    expect(worldCheckedAt({})).toBeNull();
  });

  it("count stale facts, or report the flag unavailable", () => {
    expect(staleCount([w({ stale: true }), w({ stale: false })])).toBe(1);
    expect(staleCount([w({})])).toBeNull();
  });
});
