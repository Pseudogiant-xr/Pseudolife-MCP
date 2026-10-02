// Pure logic for the consolidation review (merge a cluster of near-duplicate
// memories into one) and the dream gauges' config-derived thresholds.

import { softError } from "./api/client";
import type { ConfigResponse, CorrectionResult, Post, StreamEntry } from "./api/stream";
import { actionError } from "./errors";
import { plural } from "./format";
import type { Outcome } from "./stream";

type Member = Pick<StreamEntry, "id" | "text">;

/**
 * The /api/consolidate body for a cluster. Row ids when every member has one;
 * exact texts when none do; refused when only some do, because that mix means
 * the candidates changed under the reviewer.
 */
export function consolidateBody(
  members: Member[],
  newText: string,
): { ok: true; body: Record<string, unknown> } | { ok: false; message: string } {
  const text = newText.trim();
  if (!text) return { ok: false, message: "Merged text can't be empty." };
  if (members.length === 0) return { ok: false, message: "This cluster has no members; reload the candidates." };
  const withIds = members.filter((m) => m.id !== null && m.id !== undefined);
  if (withIds.length && withIds.length !== members.length) {
    return { ok: false, message: "Some selected memories changed; reload the candidates." };
  }
  return {
    ok: true,
    body: withIds.length
      ? { entry_ids: members.map((m) => m.id), new_text: text }
      : { replaces: members.map((m) => m.text), new_text: text },
  };
}

export async function runConsolidate(members: Member[], newText: string, post: Post): Promise<Outcome> {
  const built = consolidateBody(members, newText);
  if (!built.ok) return built;
  let r: CorrectionResult;
  try {
    r = await post<CorrectionResult>("/api/consolidate", built.body);
  } catch (e) {
    return { ok: false, message: actionError(e, "Consolidate") };
  }
  const err = softError(r);
  if (err) return { ok: false, message: `Consolidate failed: ${err}` };
  const n = r.superseded_count ?? 0;
  if (!n) {
    return { ok: false, message: "Consolidate failed: nothing was retired. Reload the candidates and try again." };
  }
  // Only `error` means nothing changed; a filtered merged text still left
  // the members retired, so that is reported, not retried.
  if (r.new_memory_stored === false) {
    return { ok: true, changed: true, tone: "warn", message: `Superseded ${plural(n, "memory", "memories")}, but the merged text was filtered and not stored` };
  }
  return { ok: true, changed: true, tone: "ok", message: `Consolidated ${plural(n, "memory", "memories")} into one` };
}

// ---- dream gauges ----------------------------------------------------------------

export const MIN_BATCH_KNOB = "memory.dream.min_batch";
export const IDLE_SECONDS_KNOB = "memory.dream.idle_seconds";

/** A numeric knob's live value from GET /api/config, or null when absent. */
export function knobNumber(cfg: ConfigResponse | null | undefined, path: string): number | null {
  for (const g of cfg?.groups ?? []) {
    for (const k of g.knobs ?? []) {
      if (k.path === path) return typeof k.value === "number" && Number.isFinite(k.value) ? k.value : null;
    }
  }
  return null;
}

/** Gauge fill (0..100) of a value against its threshold; null when either is unknown. */
export function gaugePct(value: number | null | undefined, threshold: number | null | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (typeof threshold !== "number" || !Number.isFinite(threshold) || threshold <= 0) return null;
  return Math.max(0, Math.min(100, (value / threshold) * 100));
}

// ---- extractor health --------------------------------------------------------------

/** Dream-status fields about the extractor that the shared DreamStatus type omits. */
export interface ExtractorStatus {
  extractor_mode?: string;
  primary_healthy?: boolean;
  primary_url?: string | null;
  fallback_url?: string | null;
  last_dream_extractor?: { which?: string; base_url?: string | null } | null;
}

export interface Chip {
  text: string;
  tone: "" | "ok" | "warn" | "danger";
  title?: string;
}

/**
 * Which extractor serves the dream. With no fallback configured there is
 * only one, so the chip just names the mode. With a fallback: a forced
 * fallback warns; a down primary is danger (holding when the mode is
 * primary, else dreaming on the fallback); a healthy primary whose last
 * dream still ran on the fallback warns until a dream lands on the primary.
 */
export function extractorChip(d: ExtractorStatus | null | undefined): Chip | null {
  if (!d) return null;
  const mode = d.extractor_mode || "auto";
  if (!d.fallback_url) return d.extractor_mode ? { text: `extractor ${d.extractor_mode}`, tone: "" } : null;
  const primary = d.primary_url ? ` (${d.primary_url})` : "";
  if (mode === "fallback") {
    return { text: "extractor on the fallback, forced", tone: "warn", title: "Forced by the extractor mode." };
  }
  if (d.primary_healthy === false) {
    return mode === "primary"
      ? { text: "extractor primary down, dreams holding", tone: "danger", title: `The primary${primary} is unreachable and the mode is primary, so dreams wait until it returns.` }
      : { text: "extractor primary down, on the fallback", tone: "danger", title: `The primary${primary} is unreachable, so dreams use the fallback.` };
  }
  if (d.last_dream_extractor?.which === "fallback") {
    return {
      text: "last dream ran on the fallback",
      tone: "warn",
      title: `The primary${primary} is healthy now, but the most recent dream ran on the fallback; the next dream probes the primary again.`,
    };
  }
  return { text: "extractor primary healthy", tone: "ok", title: d.primary_url ?? undefined };
}
