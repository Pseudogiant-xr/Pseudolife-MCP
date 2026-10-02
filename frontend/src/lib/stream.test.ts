import { describe, expect, it } from "vitest";
import { ApiError } from "./api/client";
import { searchParams, type Post } from "./api/stream";
import { runConsolidate } from "./consolidation";
import {
  bandLabel,
  type Ask,
  digestTrace,
  facetOptions,
  readStreamQuery,
  runDelete,
  runReinforce,
  runSupersede,
  scoreWidth,
  streamQueryOf,
  supersedeBody,
  topScore,
} from "./stream";

/** A POST fake: answers in order, records every (path, body). */
function fakePost(...answers: unknown[]): { post: Post; calls: { path: string; body: Record<string, unknown> }[] } {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const post = (async (path: string, body: Record<string, unknown>) => {
    calls.push({ path, body });
    const next = answers.shift();
    if (next instanceof Error) throw next;
    return next;
  }) as Post;
  return { post, calls };
}

describe("supersede", () => {
  it("sends entry_id when the entry has an id, never the text", () => {
    expect(supersedeBody({ id: 42, text: "old" }, "new")).toEqual({ entry_id: 42, new_text: "new" });
    expect(supersedeBody({ id: 0, text: "old" }, "new")).toEqual({ entry_id: 0, new_text: "new" });
  });

  it("falls back to old_text only when the entry has no id", () => {
    expect(supersedeBody({ id: null, text: "old" }, "new")).toEqual({ old_text: "old", new_text: "new" });
  });

  it("reports success with the trimmed replacement sent by id", async () => {
    const { post, calls } = fakePost({ superseded_count: 1, new_memory_stored: true });
    const out = await runSupersede({ id: 7, text: "old" }, "  new text ", post);
    expect(calls).toEqual([{ path: "/api/supersede", body: { entry_id: 7, new_text: "new text" } }]);
    expect(out).toEqual({ ok: true, changed: true, tone: "ok", message: "Memory superseded" });
  });

  it("treats an error answer as a failure, with the daemon's reason", async () => {
    const { post } = fakePost({ superseded_count: 0, error: "Target entry is already superseded." });
    const out = await runSupersede({ id: 7, text: "old" }, "new", post);
    expect(out.ok).toBe(false);
    expect(out.message).toContain("already superseded");
  });

  it("treats a missing or zero superseded_count as a failure", async () => {
    for (const answer of [{}, { superseded_count: 0 }]) {
      const { post } = fakePost(answer);
      const out = await runSupersede({ id: 7, text: "old" }, "new", post);
      expect(out.ok).toBe(false);
    }
  });

  it("warns when the replacement was filtered and not stored", async () => {
    const { post } = fakePost({ superseded_count: 1, new_memory_stored: false });
    const out = await runSupersede({ id: 7, text: "old" }, "new", post);
    expect(out).toEqual({
      ok: true,
      changed: true,
      tone: "warn",
      message: "Superseded, but the replacement text was filtered and not stored",
    });
  });

  it("refuses an empty replacement before sending", async () => {
    const { post, calls } = fakePost();
    const out = await runSupersede({ id: 7, text: "old" }, "   ", post);
    expect(out.ok).toBe(false);
    expect(calls).toHaveLength(0);
  });

  it("reports a thrown request as a failure", async () => {
    const { post } = fakePost(new ApiError(400, "new_text required", { error: "new_text required" }));
    const out = await runSupersede({ id: 7, text: "old" }, "new", post);
    expect(out.ok).toBe(false);
    expect(out.message).toContain("Supersede failed");
  });
});

describe("correction answers, supersede and consolidate", () => {
  // Mirrors tests/test_correction_interfaces.py::test_console_correction_preserves_identity_and_reports_rejection.
  const MODES = ["ids", "files", "filtered", "rejected", "unchanged"] as const;
  const METHODS = ["supersede", "consolidate"] as const;

  for (const method of METHODS) {
    for (const mode of MODES) {
      it(`${method}: ${mode}`, async () => {
        const entries =
          mode === "files"
            ? [
                { id: null, text: "first note" },
                { id: null, text: "second note" },
              ]
            : [
                { id: 41, text: "same text" },
                { id: 42, text: "same text" },
              ];
        const retired = method === "supersede" ? 1 : entries.length;
        const answer = {
          ids: { new_memory_stored: true, superseded_count: retired },
          files: { new_memory_stored: true, superseded_count: retired },
          filtered: { new_memory_stored: false, superseded_count: retired },
          rejected: {
            new_memory_stored: false,
            superseded_count: 0,
            reason: "target_superseded",
            error: "This entry changed; search again.",
          },
          unchanged: { new_memory_stored: false, superseded_count: 0 },
        }[mode];
        const { post, calls } = fakePost(answer);
        const out =
          method === "supersede"
            ? await runSupersede(entries[0], "The revised note", post)
            : await runConsolidate(entries, "The revised note", post);

        const expected =
          mode === "files"
            ? method === "supersede"
              ? { old_text: "first note" }
              : { replaces: ["first note", "second note"] }
            : method === "supersede"
              ? { entry_id: 41 }
              : { entry_ids: [41, 42] };
        expect(calls).toEqual([
          { path: `/api/${method}`, body: { ...expected, new_text: "The revised note" } },
        ]);

        if (mode === "rejected" || mode === "unchanged") {
          // The form stays open with a danger message.
          expect(out.ok).toBe(false);
          expect(out.message).toContain(mode === "rejected" ? "search again" : "try again");
        } else if (mode === "filtered") {
          expect(out).toMatchObject({ ok: true, changed: true, tone: "warn" });
          expect(out.message).toContain("not stored");
        } else {
          expect(out).toMatchObject({ ok: true, changed: true, tone: "ok" });
        }
      });
    }
  }
});

describe("delete", () => {
  // Mirrors tests/test_correction_interfaces.py::test_console_delete_handles_the_bulk_refusal.
  const REFUSED = {
    deleted_count: 0,
    error: "bulk_confirm_required",
    would_delete: 21,
    threshold: 20,
    sample_texts: Array(20).fill("repeated status line"),
  };
  const first = { path: "/api/delete", body: { text: "repeated status line" } };

  function asker(...answers: boolean[]) {
    const asked: Ask[] = [];
    return {
      asked,
      ask: async (req: Ask) => {
        asked.push(req);
        return answers.shift() ?? false;
      },
    };
  }

  it("single: one danger confirm, one POST {text}, reports Deleted 1", async () => {
    const { post, calls } = fakePost({ deleted_count: 1, deleted_texts: ["x"] });
    const { ask, asked } = asker(true);
    const out = await runDelete("repeated status line", post, ask);
    expect(asked).toHaveLength(1);
    expect(asked[0]).toMatchObject({ title: "Delete this memory?", danger: true });
    expect(calls).toEqual([first]);
    expect(out).toMatchObject({ ok: true, changed: true, tone: "ok" });
    expect(out?.message).toContain("Deleted 1");
  });

  it("confirmed: the bulk refusal asks again and re-posts with confirm_bulk", async () => {
    const { post, calls } = fakePost(REFUSED, { deleted_count: 21, deleted_texts: [] });
    const { ask, asked } = asker(true, true);
    const out = await runDelete("repeated status line", post, ask);
    expect(asked).toHaveLength(2);
    expect(asked[1]).toMatchObject({ title: "Delete all 21 copies?", danger: true, confirmLabel: "Delete 21" });
    expect(asked[1].message).toContain("threshold of 20");
    expect(calls).toEqual([first, { path: "/api/delete", body: { text: "repeated status line", confirm_bulk: true } }]);
    expect(out).toMatchObject({ ok: true, changed: true, tone: "ok" });
    expect(out?.message).toContain("Deleted 21");
  });

  it("declined: exactly one POST and a warning naming the count", async () => {
    const { post, calls } = fakePost(REFUSED);
    const { ask, asked } = asker(true, false);
    const out = await runDelete("repeated status line", post, ask);
    expect(asked).toHaveLength(2);
    expect(calls).toEqual([first]);
    expect(out).toMatchObject({ ok: true, changed: false, tone: "warn" });
    expect(out?.message).toContain("21");
  });

  it("sends nothing when the first confirm is declined", async () => {
    const { post, calls } = fakePost();
    const out = await runDelete("x", post, asker(false).ask);
    expect(out).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("reports 1 when the answer carries no deleted_count", async () => {
    const { post } = fakePost({});
    const out = await runDelete("x", post, asker(true).ask);
    expect(out?.ok && out.message).toBe("Deleted 1 memory");
  });

  it("treats any other error answer as a failure", async () => {
    const { post } = fakePost({ error: "storage_unavailable" });
    const out = await runDelete("x", post, asker(true).ask);
    expect(out?.ok).toBe(false);
  });

  it("says nothing was deleted when nothing matched", async () => {
    const { post } = fakePost({ deleted_count: 0, deleted_texts: [] });
    const out = await runDelete("x", post, asker(true).ask);
    expect(out).toMatchObject({ ok: true, changed: false, tone: "warn" });
  });

  it("reports a thrown request as a failure", async () => {
    const { post } = fakePost(new ApiError(0, "network", null));
    const out = await runDelete("x", post, asker(true).ask);
    expect(out?.ok).toBe(false);
  });
});

describe("reinforce", () => {
  it("posts the entry id", async () => {
    const { post, calls } = fakePost({ reinforced: true, entry_id: 9 });
    const out = await runReinforce(9, post);
    expect(calls).toEqual([{ path: "/api/reinforce", body: { entry_id: 9 } }]);
    expect(out.ok).toBe(true);
  });

  it("reports a faded entry as a failure, not as reinforced", async () => {
    const { post } = fakePost({ reinforced: false, faded: true });
    const out = await runReinforce(9, post);
    expect(out.ok).toBe(false);
  });
});

describe("search params", () => {
  it("sends rerank and bm25 only when on", () => {
    const off = searchParams({ q: "x", source: null, rerank: false, bm25: false }, 25);
    expect(off).toEqual({ q: "x", top_k: 25, rerank: undefined, bm25: undefined, source: undefined });
    const on = searchParams({ q: "x", source: "claude", rerank: true, bm25: true }, 12);
    expect(on).toEqual({ q: "x", top_k: 12, rerank: true, bm25: true, source: "claude" });
  });
});

describe("address bar", () => {
  it("reads q and source, defaulting the source to all", () => {
    expect(readStreamQuery({})).toEqual({ q: "", source: "all" });
    expect(readStreamQuery({ q: " shim ", source: "claude" })).toEqual({ q: "shim", source: "claude" });
  });

  it("writes only non-default values", () => {
    expect(streamQueryOf("", "all")).toEqual({});
    expect(streamQueryOf("shim", "claude")).toEqual({ q: "shim", source: "claude" });
  });
});

describe("facets", () => {
  const sources = Array.from({ length: 10 }, (_, i) => ({ source: `s${i}`, count: 100 - i }));

  it("offers all sources plus the top eight", () => {
    const opts = facetOptions(sources, "all");
    expect(opts).toHaveLength(9);
    expect(opts[0]).toEqual({ value: "all", label: "All sources" });
    expect(opts[1]).toEqual({ value: "s0", label: "s0", count: 100 });
  });

  it("keeps a deep-linked source outside the top eight visible", () => {
    const opts = facetOptions(sources, "s9");
    expect(opts.at(-1)).toEqual({ value: "s9", label: "s9", count: 91 });
    expect(facetOptions([], "gone").at(-1)).toEqual({ value: "gone", label: "gone", count: undefined });
  });
});

describe("results", () => {
  it("scales score bars to the top result", () => {
    const top = topScore([{ id: 1, text: "a", score: 0.5 }, { id: 2, text: "b", score: 0.25 }, { id: 3, text: "c" }]);
    expect(top).toBe(0.5);
    expect(scoreWidth(0.25, top)).toBe(50);
    expect(scoreWidth(undefined, top)).toBeNull();
    expect(topScore([{ id: 1, text: "a" }])).toBeNull();
  });

  it("hides the band chip under the flat default", () => {
    expect(bandLabel("flat")).toBeNull();
    expect(bandLabel(undefined)).toBeNull();
    expect(bandLabel("working")).toBe("working");
  });
});

describe("trace digest", () => {
  it("counts candidates and kept per tier and collects drop reasons", () => {
    const d = digestTrace({
      config: { preset: "flat", chain_residual: false, top_k: 12 },
      tiers: [
        {
          name: "flat",
          candidates: [
            { text_preview: "a", kept: true, drop_reason: null },
            { text_preview: "b", kept: false, drop_reason: "superseded" },
            { text_preview: "c", kept: false, drop_reason: null },
          ],
        },
        { name: "deep", filtered_out: true, candidates: [] },
      ],
      reranker: { fired: true },
      final_topk: [{ text_preview: "a", score: 0.91 }],
    });
    expect(d?.tiers).toEqual([
      { name: "flat", filteredOut: false, candidates: 3, kept: 1 },
      { name: "deep", filteredOut: true, candidates: 0, kept: 0 },
    ]);
    expect(d?.drops).toEqual([{ text: "b", reason: "superseded" }]);
    expect(d?.config).toEqual([
      { key: "preset", value: "flat" },
      { key: "chain_residual", value: "off" },
      { key: "top_k", value: "12" },
    ]);
    expect(d?.rerankerFired).toBe(true);
    expect(d?.bm25Fired).toBeNull();
    expect(d?.final).toEqual([{ text: "a", score: 0.91 }]);
  });

  it("is null without a trace", () => {
    expect(digestTrace(null)).toBeNull();
  });
});
