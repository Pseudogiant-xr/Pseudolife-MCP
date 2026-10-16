// The top bar's model chip: the model the next dream extracts with, read from
// GET /api/dream/status (the same answer the Settings Dreamer card shows).
// It replaced a bare writer-id chip, which read "codex" on a daemon whose
// dreamer ran Claude (2026-10-16); the writer id now shows only in the
// Observatory's details, as "Default writer".

import type { DreamerStatus } from "./api/config";
import { primaryModel } from "./dreamer";

export interface DreamChip {
  label: string;
  value: string;
  title: string;
}

export function dreamChip(s: DreamerStatus | null | undefined): DreamChip | null {
  if (!s) return null;
  const override = (s.model_override ?? "").trim();
  const model = override || primaryModel(s).model.trim();
  if (!model) return null;
  const effort = (s.reasoning_effort ?? "").trim();
  return {
    label: "dreamer",
    value: model,
    title:
      `Dreamer model: ${model}${effort ? ` at ${effort} effort` : ""}. ` +
      `The next dream extracts with it; change it in Settings.`,
  };
}
