import { describe, expect, it } from "vitest";
import type { Post } from "./api/stream";
import { consolidateBody, extractorChip, gaugePct, knobNumber, runConsolidate } from "./consolidation";

function fakePost(...answers: unknown[]): { post: Post; calls: { path: string; body: Record<string, unknown> }[] } {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const post = (async (path: string, body: Record<string, unknown>) => {
    calls.push({ path, body });
    return answers.shift();
  }) as Post;
  return { post, calls };
}

describe("consolidate body", () => {
  it("uses entry_ids when every member has an id", () => {
    const r = consolidateBody([{ id: 1, text: "a" }, { id: 2, text: "b" }], " merged ");
    expect(r).toEqual({ ok: true, body: { entry_ids: [1, 2], new_text: "merged" } });
  });

  it("refuses when only some members have an id", () => {
    const r = consolidateBody([{ id: 1, text: "a" }, { id: null, text: "b" }], "merged");
    expect(r).toEqual({ ok: false, message: "Some selected memories changed; reload the candidates." });
  });

  it("falls back to replaces by text when no member has an id", () => {
    const r = consolidateBody([{ id: null, text: "a" }, { id: null, text: "b" }], "merged");
    expect(r).toEqual({ ok: true, body: { replaces: ["a", "b"], new_text: "merged" } });
  });

  it("refuses empty merged text before sending", async () => {
    expect(consolidateBody([{ id: 1, text: "a" }], "  ").ok).toBe(false);
    const { post, calls } = fakePost();
    const out = await runConsolidate([{ id: 1, text: "a" }], "", post);
    expect(out.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it("does not send a mixed selection", async () => {
    const { post, calls } = fakePost();
    const out = await runConsolidate([{ id: 1, text: "a" }, { id: null, text: "b" }], "m", post);
    expect(out.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });
});

describe("run consolidate", () => {
  const members = [
    { id: 1, text: "a" },
    { id: 2, text: "b" },
  ];

  it("reports success with the retired count", async () => {
    const { post, calls } = fakePost({ superseded_count: 2, new_memory_stored: true });
    const out = await runConsolidate(members, "m", post);
    expect(calls).toEqual([{ path: "/api/consolidate", body: { entry_ids: [1, 2], new_text: "m" } }]);
    expect(out).toEqual({ ok: true, changed: true, tone: "ok", message: "Consolidated 2 memories into one" });
  });

  it("treats an error answer or a missing count as a failure", async () => {
    for (const answer of [{ error: "Ambiguous target." }, {}, { superseded_count: 0 }]) {
      const { post } = fakePost(answer);
      expect((await runConsolidate(members, "m", post)).ok).toBe(false);
    }
  });

  it("warns when the merged text was filtered and not stored", async () => {
    const { post } = fakePost({ superseded_count: 2, new_memory_stored: false });
    const out = await runConsolidate(members, "m", post);
    expect(out).toEqual({
      ok: true,
      changed: true,
      tone: "warn",
      message: "Superseded 2 memories, but the merged text was filtered and not stored",
    });
  });
});

describe("dream thresholds", () => {
  const cfg = {
    groups: [
      { name: "Dream", knobs: [{ path: "memory.dream.min_batch", value: 8 }, { path: "memory.dream.idle_seconds", value: 600 }] },
      { name: "Other", knobs: [{ path: "x.y", value: "text" }] },
    ],
  };

  it("reads numeric knobs and nothing else", () => {
    expect(knobNumber(cfg, "memory.dream.min_batch")).toBe(8);
    expect(knobNumber(cfg, "memory.dream.idle_seconds")).toBe(600);
    expect(knobNumber(cfg, "x.y")).toBeNull();
    expect(knobNumber(cfg, "missing")).toBeNull();
    expect(knobNumber(null, "memory.dream.min_batch")).toBeNull();
  });

  it("fills a gauge against its threshold, capped at full", () => {
    expect(gaugePct(4, 8)).toBe(50);
    expect(gaugePct(20, 8)).toBe(100);
    expect(gaugePct(4, null)).toBeNull();
    expect(gaugePct(undefined, 8)).toBeNull();
    expect(gaugePct(4, 0)).toBeNull();
  });
});

describe("extractor chip", () => {
  const fb = { fallback_url: "http://fallback", primary_url: "http://primary" };

  it("names the mode when no fallback is configured", () => {
    expect(extractorChip({ extractor_mode: "auto" })).toEqual({ text: "extractor auto", tone: "" });
    expect(extractorChip({})).toBeNull();
  });

  it("warns on a forced fallback", () => {
    expect(extractorChip({ ...fb, extractor_mode: "fallback" })?.tone).toBe("warn");
  });

  it("is danger when the primary is down, holding in primary mode", () => {
    expect(extractorChip({ ...fb, extractor_mode: "primary", primary_healthy: false })?.text).toContain("holding");
    expect(extractorChip({ ...fb, extractor_mode: "auto", primary_healthy: false })).toMatchObject({ tone: "danger", text: "extractor primary down, on the fallback" });
  });

  it("warns when the last dream ran on the fallback despite a healthy primary", () => {
    expect(extractorChip({ ...fb, primary_healthy: true, last_dream_extractor: { which: "fallback" } })?.tone).toBe("warn");
    expect(extractorChip({ ...fb, primary_healthy: true, last_dream_extractor: { which: "primary" } })?.tone).toBe("ok");
  });
});
