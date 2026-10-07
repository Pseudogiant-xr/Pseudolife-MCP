import { describe, expect, it } from "vitest";
import type { Fact } from "./api/facts";
import {
  confidenceSpan,
  countNote,
  countSlots,
  facetCounts,
  filterFacts,
  groupByEntity,
  groupSlots,
  historyLadder,
  interpretForget,
  interpretResolve,
  interpretSet,
  newestRow,
  slotOrigins,
} from "./cortex";

const fact = (over: Partial<Fact>): Fact => ({ entity: "e", attribute: "a", value: "v", ...over });

// A set slot as /api/facts serves it: one row per member, each carrying the
// slot's contested flag and the same contender. The second member spells
// the attribute differently; the store keys both to one slot.
const setRows: Fact[] = [
  fact({ entity: "pseudolife-mcp", attribute: "supported clients", value: "Claude Code", kind: "member", origin: "user", confidence: 1, contested: true, contender_value: "Cursor", contender_origin: "agent", tx_time: 100 }),
  fact({ entity: "pseudolife-mcp", attribute: "Supported_Clients", value: "Claude Desktop", kind: "member", origin: "agent", confidence: 0.9, contested: true, contender_value: "Cursor", contender_origin: "agent", tx_time: 200 }),
];

describe("set-valued slots", () => {
  it("collapse member rows into one slot keyed by slotKey", () => {
    const slots = groupSlots(setRows);
    expect(slots).toHaveLength(1);
    expect(slots[0].isSet).toBe(true);
    expect(slots[0].rows.map((r) => r.value)).toEqual(["Claude Code", "Claude Desktop"]);
    expect(slots[0].attribute).toBe("supported clients");
  });

  it("carry the contender once per slot, not once per row", () => {
    const slots = groupSlots([...setRows, fact({ entity: "pseudolife-mcp", attribute: "status", value: "active" })]);
    const withContender = slots.filter((s) => s.contender !== null);
    expect(withContender).toHaveLength(1);
    expect(withContender[0].contender).toEqual({ value: "Cursor", origin: "agent" });
    expect(slots.find((s) => s.attribute === "status")?.contender).toBeNull();
  });

  it("count contested slots, deduped by slot", () => {
    expect(countSlots(setRows, (f) => !!f.contested)).toBe(1);
    const g = groupByEntity(setRows);
    expect(g[0].contested).toBe(1);
    expect(g[0].sets).toBe(1);
    expect(facetCounts(setRows).contested).toBe(1);
  });

  it("mark a lone member row as a set", () => {
    expect(groupSlots([fact({ kind: "member" })])[0].isSet).toBe(true);
    expect(groupSlots([fact({ kind: "scalar" })])[0].isSet).toBe(false);
  });

  it("report every origin, the confidence span and the newest row", () => {
    const s = groupSlots(setRows)[0];
    expect(slotOrigins(s)).toEqual(["user", "agent"]);
    expect(confidenceSpan(s)).toEqual({ min: 0.9, max: 1 });
    expect(newestRow(s).value).toBe("Claude Desktop");
  });

  it("defaults a contender without an origin to agent", () => {
    const s = groupSlots([fact({ contested: true, contender_value: "x" })])[0];
    expect(s.contender).toEqual({ value: "x", origin: "agent" });
  });
});

describe("filtering", () => {
  const rows: Fact[] = [
    fact({ entity: "postgres", attribute: "host port", value: "5433", origin: "user" }),
    fact({ entity: "postgres", attribute: "role", value: "pseudolife", origin: "action", stale: true }),
    fact({ entity: "daemon", attribute: "status", value: "Active", origin: null, re_verify: true }),
    fact({ entity: "daemon", attribute: "model", value: "x", origin: "assistant", contested: true, contender_value: "y" }),
  ];
  const all = { q: "", origin: "all" as const, facet: "all" as const };

  it("matches entity, attribute and value case-insensitively", () => {
    expect(filterFacts(rows, { ...all, q: "POSTGRES" })).toHaveLength(2);
    expect(filterFacts(rows, { ...all, q: "host port" })).toHaveLength(1);
    expect(filterFacts(rows, { ...all, q: "active" })).toHaveLength(1);
    expect(filterFacts(rows, { ...all, q: "  " })).toHaveLength(4);
  });

  it("filters by origin, reading an unset origin as agent", () => {
    expect(filterFacts(rows, { ...all, origin: "user" }).map((f) => f.value)).toEqual(["5433"]);
    expect(filterFacts(rows, { ...all, origin: "agent" }).map((f) => f.attribute)).toEqual(["status"]);
    // An unknown tier ("assistant") only shows under all origins.
    expect(filterFacts(rows, { ...all, origin: "agent" }).some((f) => f.origin === "assistant")).toBe(false);
  });

  it("filters by facet on the served flags only", () => {
    expect(filterFacts(rows, { ...all, facet: "contested" }).map((f) => f.attribute)).toEqual(["model"]);
    expect(filterFacts(rows, { ...all, facet: "stale" }).map((f) => f.attribute)).toEqual(["role"]);
    expect(filterFacts(rows, { ...all, facet: "reverify" }).map((f) => f.attribute)).toEqual(["status"]);
  });

  it("leaves the stale count unavailable when no row carries the flag", () => {
    expect(facetCounts([fact({})]).stale).toBeNull();
    expect(facetCounts(rows).stale).toBe(1);
    expect(facetCounts(rows).reverify).toBe(1);
  });
});

describe("grouping", () => {
  it("sorts entities by name and rows by attribute", () => {
    const g = groupByEntity([
      fact({ entity: "zeta", attribute: "b" }),
      fact({ entity: "alpha", attribute: "role" }),
      fact({ entity: "alpha", attribute: "host port" }),
      fact({ entity: "Beta", attribute: "x" }),
    ]);
    expect(g.map((x) => x.entity)).toEqual(["alpha", "Beta", "zeta"]);
    expect(g[0].facts.map((f) => f.attribute)).toEqual(["host port", "role"]);
    expect(g[0].slots.map((s) => s.attribute)).toEqual(["host port", "role"]);
  });

  it("takes the newest confirmed-or-written time per entity, or null", () => {
    const g = groupByEntity([
      fact({ entity: "a", tx_time: 10, last_confirmed: 50 }),
      fact({ entity: "a", attribute: "b", tx_time: 40 }),
      fact({ entity: "b" }),
    ]);
    expect(g[0].latest).toBe(50);
    expect(g[1].latest).toBeNull();
  });
});

describe("countNote", () => {
  it("says how many passed the filter and how many were loaded", () => {
    expect(countNote(40, 40, 40, false, ["fact", "facts"])).toBe("40 facts");
    expect(countNote(1, 40, 40, false, ["fact", "facts"])).toBe("1 fact of 40");
    expect(countNote(12, 1000, 1412, true, ["fact", "facts"])).toBe("12 facts of 1 000, first 1 000 of 1 412 loaded");
  });
});

describe("interpretResolve", () => {
  it("toasts success only for a resolved answer", () => {
    expect(interpretResolve({ resolved: true, accepted: true }, true)).toMatchObject({ ok: true, message: "Contender adopted", reload: true });
    expect(interpretResolve({ resolved: true, accepted: false }, false)).toMatchObject({ ok: true, message: "Contender discarded" });
  });

  it("words slot_holds_set separately for adopt and discard", () => {
    const a = interpretResolve({ resolved: false, reason: "slot_holds_set" }, true);
    const d = interpretResolve({ resolved: false, reason: "slot_holds_set" }, false);
    expect(a.ok).toBe(false);
    expect(d.ok).toBe(false);
    expect(a.message).toMatch(/^Not adopted: the slot now holds a set/);
    expect(d.message).toBe("Not discarded: the slot holds a set (slot_holds_set).");
    expect(a.reload).toBe(false);
  });

  it("reloads on no_contender, since the view is stale", () => {
    const o = interpretResolve({ resolved: false, reason: "no_contender" }, true);
    expect(o).toMatchObject({ ok: false, reload: true, tone: "warn" });
    expect(o.message).not.toMatch(/adopted|discarded/i);
  });

  it("names any other reason", () => {
    expect(interpretResolve({ resolved: false, reason: "locked" }, true).message).toBe("Not resolved: locked");
    expect(interpretResolve({ resolved: false }, false).message).toBe("Not resolved: unknown reason");
    expect(interpretResolve(null, true).ok).toBe(false);
    expect(interpretResolve({ error: "boom" } as never, true)).toMatchObject({ ok: false, message: "Not resolved: boom" });
  });
});

describe("interpretForget and interpretSet", () => {
  it("only a positive removed count is a forget", () => {
    expect(interpretForget({ removed: 2 })).toMatchObject({ ok: true, message: "Fact forgotten" });
    expect(interpretForget({ removed: 0 })).toMatchObject({ ok: false, reload: true });
    expect(interpretForget({})).toMatchObject({ ok: false });
    expect(interpretForget({ error: "nope" })).toMatchObject({ ok: false, message: "Not forgotten: nope" });
  });

  it("says when a write was parked as a contender", () => {
    expect(interpretSet({ action: "inserted" })).toMatchObject({ ok: true, message: "Fact asserted", tone: "ok" });
    expect(interpretSet({ action: "contested" })).toMatchObject({ ok: true, tone: "warn" });
    expect(interpretSet({ action: "confirmed" }).message).toBe("Fact confirmed");
    expect(interpretSet({ error: "bad" })).toMatchObject({ ok: false });
  });
});

describe("historyLadder", () => {
  it("lists scalar versions newest first, current marked", () => {
    const rungs = historyLadder({
      entity: "e",
      attribute: "a",
      count: 3,
      versions: [
        { value: "one", status: "superseded", writer_id: "w1", tx_time: 1 },
        { value: "two", status: "superseded", tx_time: 1 },
        { value: "three", status: "current", writer_id: "w3", age: "2 hours ago", tx_time: 3 },
      ],
    });
    expect(rungs.map((r) => r.value)).toEqual(["three", "two", "one"]);
    expect(rungs.map((r) => r.current)).toEqual([true, false, false]);
    expect(rungs[0]).toMatchObject({ by: "w3", age: "2 hours ago", label: "current" });
    expect(rungs[1].by).toBeNull();
  });

  it("marks a set member current while its newest event is an add", () => {
    const rungs = historyLadder({
      kind: "set",
      entity: "e",
      attribute: "a",
      count: 5,
      versions: [
        { value: "x", event: "added", at: 1 },
        { value: "y", event: "added", at: 2 },
        { value: "x", event: "removed", at: 3 },
        { value: "z", event: "added", at: 4 },
        { value: "x", event: "added", at: 5 },
      ],
    });
    expect(rungs.map((r) => `${r.label} ${r.value} ${r.current}`)).toEqual([
      "added x true",
      "added z true",
      "removed x false",
      "added y true",
      "added x false",
    ]);
  });

  it("is empty for a missing answer", () => {
    expect(historyLadder(null)).toEqual([]);
  });
});
