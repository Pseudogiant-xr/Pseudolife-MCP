// The Dreamer card's model and effort pickers. These two lists are the only
// knob values the console hard-codes; tests/test_extractor_model_lists.py
// parses DREAMER_MODELS below to keep it in step with the installers, the
// shims and the dreaming guide, so keep its one-object-per-line shape.

import type { DreamerStatus } from "./api/config";

// GPT-5.6 and GPT-6 families: served per request by the Codex CLI shim
// (evals/codex_shim.py) or any OpenAI-compatible endpoint that knows these
// ids. Extraction quality is unmeasured: the ladder has only measured the
// Claude models and the local sidecars.
export const DREAMER_MODELS = [
  { id: "claude-opus-5-5", label: "Opus 5.5", note: "recommended — clears the extraction-ladder gate with no regression against Opus 5" },
  { id: "claude-opus-5", label: "Opus 5", note: "the earlier default — chosen over Sonnet on 2026-08-02 (best measured)" },
  { id: "claude-sonnet-5-5", label: "Sonnet 5.5", note: "balanced" },
  { id: "claude-sonnet-5", label: "Sonnet 5", note: "the earlier Sonnet" },
  { id: "claude-haiku-5-5", label: "Haiku 5.5", note: "fastest / lightest on plan usage" },
  { id: "claude-haiku-4-5", label: "Haiku 4.5", note: "the earlier Haiku" },
  { id: "claude-fable-5-1", label: "Fable 5.1", note: "most capable tier" },
  { id: "claude-fable-5", label: "Fable 5", note: "the earlier Fable" },
  { id: "gpt-5.6-sol", label: "Sol", note: "OpenAI flagship — unmeasured here" },
  { id: "gpt-5.6-terra", label: "Terra", note: "OpenAI balanced — unmeasured here" },
  { id: "gpt-5.6-luna", label: "Luna", note: "OpenAI fastest — unmeasured here" },
  { id: "gpt-6-sol", label: "GPT-6 Sol", note: "OpenAI GPT-6 flagship — unmeasured here" },
  { id: "gpt-6-luna", label: "GPT-6 Luna", note: "OpenAI GPT-6 fastest — unmeasured here" },
];

// Common levels every provider understands; the Extractor group's knob takes
// the provider-specific extras (codex "minimal", claude "max") as free text.
export const DREAMER_EFFORTS = ["low", "medium", "high", "xhigh"];

export const OVERRIDE_PATH = "memory.dream.extractor_model_override";
export const EFFORT_PATH = "memory.dream.extractor_reasoning_effort";

/** A picker value: null clears the knob so the endpoint's default serves. */
export type Pick = string | null;

function norm(v: string | null | undefined): Pick {
  const s = (v ?? "").trim();
  return s === "" ? null : s;
}

export function currentModel(s: DreamerStatus | null): Pick {
  return norm(s?.model_override);
}

export function currentEffort(s: DreamerStatus | null): Pick {
  return norm(s?.reasoning_effort);
}

/** An override that is not one of the listed models (typed by hand, or set in the Extractor group). */
export function customModel(s: DreamerStatus | null): string {
  const m = currentModel(s);
  return m && !DREAMER_MODELS.some((x) => x.id === m) ? m : "";
}

/** An effort outside DREAMER_EFFORTS ("minimal", "max"), shown as a disabled active choice. */
export function customEffort(s: DreamerStatus | null): string {
  const e = currentEffort(s);
  return e && !DREAMER_EFFORTS.includes(e) ? e : "";
}

/**
 * The one-knob patch a pick sends, or null when the pick changes nothing
 * (the classic console made a repeat click a no-op).
 */
export function pickPatch(kind: "model" | "effort", value: string | null, s: DreamerStatus | null): Record<string, Pick> | null {
  const v = norm(value);
  const current = kind === "model" ? currentModel(s) : currentEffort(s);
  if (v === current) return null;
  return { [kind === "model" ? OVERRIDE_PATH : EFFORT_PATH]: v };
}

/** The toast after a pick is saved. */
export function pickMessage(kind: "model" | "effort", value: string | null): string {
  const v = norm(value);
  if (kind === "model") {
    return v ? `Dreamer model saved: the next dream extracts with ${v}.` : "Model override cleared: the endpoint's default model serves the next dream.";
  }
  return v ? `Dreamer effort saved: the next dream extracts at ${v} effort.` : "Effort cleared: the endpoint's default effort serves the next dream.";
}

/**
 * The primary model as served. A launch-default alias ("extractor") hides
 * the real model; the daemon resolves it from the endpoint's /v1/models and
 * sends it as primary_model_served.
 */
export function primaryModel(s: DreamerStatus): { model: string; alias: string } {
  const served = s.primary_model_served;
  const configured = s.primary_model ?? "";
  if (served && served !== configured) return { model: served, alias: configured };
  return { model: configured, alias: "" };
}

export function primaryHealth(s: DreamerStatus): { text: string; tone: "ok" | "danger" | "" } {
  if (s.primary_healthy === true) return { text: "Primary answering", tone: "ok" };
  if (s.primary_healthy === false) return { text: "Primary down", tone: "danger" };
  return { text: "Not probed (no fallback configured)", tone: "" };
}

export function modelNote(id: string | null): string {
  return DREAMER_MODELS.find((m) => m.id === id)?.note ?? "";
}
