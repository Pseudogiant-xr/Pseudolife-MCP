import { describe, expect, it } from "vitest";
import type { DreamerStatus } from "./api/config";
import {
  customEffort,
  customModel,
  DREAMER_EFFORTS,
  DREAMER_MODELS,
  EFFORT_PATH,
  OVERRIDE_PATH,
  pickMessage,
  pickPatch,
  primaryHealth,
  primaryModel,
} from "./dreamer";

const status = (s: Partial<DreamerStatus> = {}): DreamerStatus => ({
  primary_url: "http://host.docker.internal:8082/v1",
  primary_model: "extractor",
  primary_model_served: "claude-opus-5",
  model_override: null,
  reasoning_effort: null,
  ...s,
});

describe("dreamer lists", () => {
  it("keep the classic console's ids and order", () => {
    expect(DREAMER_MODELS.map((m) => m.id)).toEqual([
      "claude-opus-5-5",
      "claude-opus-5",
      "claude-sonnet-5",
      "claude-haiku-4-5",
      "claude-fable-5",
      "gpt-5.6-sol",
      "gpt-5.6-terra",
      "gpt-5.6-luna",
      "gpt-6-sol",
      "gpt-6-luna",
    ]);
    expect(DREAMER_EFFORTS).toEqual(["low", "medium", "high", "xhigh"]);
  });
});

describe("pickPatch: the one-knob patch a pick sends", () => {
  it("sets the model override, or clears it with null", () => {
    expect(pickPatch("model", "claude-sonnet-5", status())).toEqual({ [OVERRIDE_PATH]: "claude-sonnet-5" });
    expect(pickPatch("model", null, status({ model_override: "claude-opus-5" }))).toEqual({ [OVERRIDE_PATH]: null });
  });

  it("sets or clears the effort", () => {
    expect(pickPatch("effort", "high", status())).toEqual({ [EFFORT_PATH]: "high" });
    expect(pickPatch("effort", null, status({ reasoning_effort: "low" }))).toEqual({ [EFFORT_PATH]: null });
  });

  it("is a no-op when the pick equals the current value", () => {
    expect(pickPatch("model", null, status())).toBeNull();
    expect(pickPatch("model", "claude-opus-5", status({ model_override: "claude-opus-5" }))).toBeNull();
    expect(pickPatch("effort", "high", status({ reasoning_effort: "high" }))).toBeNull();
    expect(pickPatch("effort", "", status())).toBeNull();
  });

  it("trims a hand-typed model id", () => {
    expect(pickPatch("model", "  my-model  ", status())).toEqual({ [OVERRIDE_PATH]: "my-model" });
  });
});

describe("dreamer card facts", () => {
  it("shows the served model behind a launch-default alias", () => {
    expect(primaryModel(status())).toEqual({ model: "claude-opus-5", alias: "extractor" });
    expect(primaryModel(status({ primary_model: "claude-opus-5-5", primary_model_served: null }))).toEqual({
      model: "claude-opus-5-5",
      alias: "",
    });
  });

  it("reports health as answering, down, or not probed", () => {
    expect(primaryHealth(status({ primary_healthy: true })).tone).toBe("ok");
    expect(primaryHealth(status({ primary_healthy: false })).tone).toBe("danger");
    expect(primaryHealth(status({ primary_healthy: null })).text).toMatch(/Not probed/);
    expect(primaryHealth(status()).tone).toBe("");
  });

  it("surfaces a model or effort outside the lists", () => {
    expect(customModel(status({ model_override: "qwen3-30b" }))).toBe("qwen3-30b");
    expect(customModel(status({ model_override: "claude-opus-5" }))).toBe("");
    expect(customEffort(status({ reasoning_effort: "max" }))).toBe("max");
    expect(customEffort(status({ reasoning_effort: "high" }))).toBe("");
  });

  it("words the toast after a pick", () => {
    expect(pickMessage("model", "claude-opus-5")).toMatch(/next dream extracts with claude-opus-5/);
    expect(pickMessage("model", null)).toMatch(/default model/);
    expect(pickMessage("effort", "low")).toMatch(/at low effort/);
    expect(pickMessage("effort", null)).toMatch(/default effort/);
  });
});
