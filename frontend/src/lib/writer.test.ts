import { describe, expect, it } from "vitest";
import { writerChip } from "./writer";

describe("writerChip", () => {
  it("labels the daemon's writer id as a writer, not as a model", () => {
    // 2026-10-16: a bare "codex" chip on a daemon whose dreamer ran Claude
    // read as "this daemon runs on Codex".
    const chip = writerChip("codex");
    expect(chip?.label).toBe("writer");
    expect(chip?.value).toBe("codex");
    expect(chip?.title).toMatch(/default writer id/i);
    expect(chip?.title).toMatch(/no writer id of their own/i);
    expect(chip?.title).toMatch(/not the dream/i);
  });

  it("shows nothing when the daemon reports no writer id", () => {
    expect(writerChip(undefined)).toBeNull();
    expect(writerChip(null)).toBeNull();
    expect(writerChip("")).toBeNull();
  });
});
