import { describe, expect, it } from "vitest";
import type { DreamerStatus } from "./api/config";
import { dreamChip } from "./dreamChip";

const status = (s: Partial<DreamerStatus> = {}): DreamerStatus => ({
  extractor_mode: "primary",
  primary_model: "claude-opus-5-5",
  primary_model_served: null,
  model_override: null,
  reasoning_effort: null,
  ...s,
});

describe("dreamChip", () => {
  it("shows the dreamer's model, not the writer id", () => {
    // 2026-10-16: the chip showed the writer id "codex" on a daemon whose
    // dreamer ran Claude.
    const chip = dreamChip(status());
    expect(chip?.label).toBe("dreamer");
    expect(chip?.value).toBe("claude-opus-5-5");
    expect(chip?.title).toMatch(/dreamer model: claude-opus-5-5/i);
  });

  it("prefers the Settings override and names its effort", () => {
    const chip = dreamChip(status({ model_override: "claude-sonnet-5-5", reasoning_effort: "medium" }));
    expect(chip?.value).toBe("claude-sonnet-5-5");
    expect(chip?.title).toMatch(/at medium effort/);
  });

  it("shows the served model behind a launch-default alias", () => {
    const chip = dreamChip(status({ primary_model: "extractor", primary_model_served: "claude-opus-5" }));
    expect(chip?.value).toBe("claude-opus-5");
  });

  it("shows nothing without a status or a model", () => {
    expect(dreamChip(null)).toBeNull();
    expect(dreamChip(undefined)).toBeNull();
    expect(dreamChip(status({ primary_model: null }))).toBeNull();
    expect(dreamChip(status({ primary_model: "  ", model_override: "" }))).toBeNull();
  });
});
