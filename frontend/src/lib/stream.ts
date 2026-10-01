// Pure logic for the Stream view: address-bar state, source facets, the
// ranking-trace digest, and the three corrections (supersede, delete,
// reinforce). Each correction takes the POST call as a parameter so the
// tests drive it with fakes; each returns an Outcome the view turns into a
// toast or an inline message. None of them reports a refusal as success.

import { softError } from "./api/client";
import type {
  CorrectionResult,
  DeleteResult,
  Post,
  ReinforceResult,
  SourceCount,
  StreamEntry,
  Trace,
} from "./api/stream";
import { actionError } from "./errors";
import { fmtNum, plural, truncate, words } from "./format";

export type Outcome =
  | { ok: true; changed: boolean; tone: "ok" | "warn"; message: string }
  | { ok: false; message: string };

// ---- address bar -------------------------------------------------------------

export const ALL_SOURCES = "all";

/** #/stream?q=&source= -> the view's query and source ("all" when absent). */
export function readStreamQuery(query: Record<string, string>): { q: string; source: string } {
  return { q: (query.q ?? "").trim(), source: query.source?.trim() || ALL_SOURCES };
}

/** The view's state as address-bar params; defaults are left out. */
export function streamQueryOf(q: string, source: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (q.trim()) out.q = q.trim();
  if (source && source !== ALL_SOURCES) out.source = source;
  return out;
}

// ---- facets ------------------------------------------------------------------

export interface FacetOption {
  value: string;
  label: string;
  count?: number;
}

/**
 * "All sources" plus the eight largest sources. A source named by the address
 * bar but outside the top eight is appended, so a deep link still shows which
 * filter is on.
 */
export function facetOptions(sources: SourceCount[] | null | undefined, current: string, cap = 8): FacetOption[] {
  const opts: FacetOption[] = [{ value: ALL_SOURCES, label: "All sources" }];
  for (const s of (sources ?? []).slice(0, cap)) opts.push({ value: s.source, label: s.source, count: s.count });
  if (current !== ALL_SOURCES && !opts.some((o) => o.value === current)) {
    const known = (sources ?? []).find((s) => s.source === current);
    opts.push({ value: current, label: current, count: known?.count });
  }
  return opts;
}

// ---- results -------------------------------------------------------------------

/** The top score of a result list, the scale for the per-row score bars. */
export function topScore(entries: StreamEntry[]): number | null {
  let max: number | null = null;
  for (const e of entries) {
    if (typeof e.score === "number" && Number.isFinite(e.score) && (max === null || e.score > max)) max = e.score;
  }
  return max;
}

/** Bar width (0..100) of a score relative to the top result; null when unscored. */
export function scoreWidth(score: number | undefined, top: number | null): number | null {
  if (typeof score !== "number" || !Number.isFinite(score) || top === null || top <= 0) return null;
  return Math.max(0, Math.min(100, (score / top) * 100));
}

/** The band chip is noise under the flat default: every entry would carry it. */
export function bandLabel(bank: string | undefined): string | null {
  return bank && bank !== "flat" ? bank : null;
}

// ---- ranking trace -------------------------------------------------------------

export interface TierRow {
  name: string;
  filteredOut: boolean;
  candidates: number;
  kept: number;
}

export interface TraceDigest {
  config: { key: string; value: string }[];
  tiers: TierRow[];
  drops: { text: string; reason: string }[];
  rerankerFired: boolean | null;
  bm25Fired: boolean | null;
  final: { text: string; score: number | null }[];
}

export function configValue(v: unknown): string {
  if (v === null || v === undefined) return "none";
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "number" || typeof v === "string") return String(v);
  return JSON.stringify(v);
}

/** What the trace panel shows: per-tier candidates and kept, why the rest dropped, the final top-k. */
export function digestTrace(t: Trace | null | undefined, maxDrops = 6): TraceDigest | null {
  if (!t) return null;
  const tiers = (t.tiers ?? []).map((ti) => {
    const c = ti.candidates ?? [];
    return { name: ti.name, filteredOut: !!ti.filtered_out, candidates: c.length, kept: c.filter((x) => x.kept).length };
  });
  const drops: { text: string; reason: string }[] = [];
  for (const ti of t.tiers ?? []) {
    for (const c of ti.candidates ?? []) {
      if (!c.kept && c.drop_reason && drops.length < maxDrops) {
        drops.push({ text: c.text_preview ?? "", reason: c.drop_reason });
      }
    }
  }
  return {
    config: Object.entries(t.config ?? {}).map(([key, value]) => ({ key, value: configValue(value) })),
    tiers,
    drops,
    rerankerFired: t.reranker ? !!t.reranker.fired : null,
    bm25Fired: t.bm25 ? !!t.bm25.fired : null,
    final: (t.final_topk ?? []).map((r) => ({
      text: r.text_preview ?? "",
      score: typeof r.score === "number" && Number.isFinite(r.score) ? r.score : null,
    })),
  };
}

// ---- corrections ---------------------------------------------------------------

/**
 * Supersede by id preserves identity: the entry's row id when it has one,
 * its exact text only when it has none (file mode, not yet persisted).
 */
export function supersedeBody(entry: Pick<StreamEntry, "id" | "text">, newText: string): Record<string, unknown> {
  return entry.id !== null && entry.id !== undefined
    ? { entry_id: entry.id, new_text: newText }
    : { old_text: entry.text, new_text: newText };
}

export async function runSupersede(
  entry: Pick<StreamEntry, "id" | "text">,
  newText: string,
  post: Post,
): Promise<Outcome> {
  const text = newText.trim();
  if (!text) return { ok: false, message: "Write the replacement text first." };
  let r: CorrectionResult;
  try {
    r = await post<CorrectionResult>("/api/supersede", supersedeBody(entry, text));
  } catch (e) {
    return { ok: false, message: actionError(e, "Supersede") };
  }
  // The daemon refuses a whole selection with `error` (missing, ambiguous or
  // already retired target): nothing changed, so the form stays open.
  const err = softError(r);
  if (err) return { ok: false, message: `Supersede failed: ${err}` };
  if (!r.superseded_count) {
    return { ok: false, message: "Supersede failed: nothing was retired. Reload the stream and try again." };
  }
  // A retirement whose replacement was filtered out is not a rejection: the
  // original is history now, so retrying would only fail.
  if (r.new_memory_stored === false) {
    return { ok: true, changed: true, tone: "warn", message: "Superseded, but the replacement text was filtered and not stored" };
  }
  return { ok: true, changed: true, tone: "ok", message: "Memory superseded" };
}

/** A confirmation request, the shape lib/overlay.svelte.ts confirm() takes. */
export interface Ask {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
}

export function deleteConfirmCopy(text: string): Ask {
  return {
    title: "Delete this memory?",
    message: `Permanently remove: “${truncate(text, 120)}”.`,
    confirmLabel: "Delete the memory",
    danger: true,
  };
}

export function bulkConfirmCopy(n: number, threshold: number | undefined): Ask {
  const limit = threshold !== undefined ? `, more than the bulk-delete threshold of ${fmtNum(threshold)}` : "";
  return {
    title: `Delete all ${fmtNum(n)} copies?`,
    message: `${plural(n, "memory has", "memories have")} exactly this text${limit}. Delete every one?`,
    confirmLabel: `Delete ${fmtNum(n)}`,
    danger: true,
  };
}

/**
 * Delete by exact text (the body is {text} even when the entry has an id:
 * /api/delete filters by text). A danger confirm first. Identical texts are
 * not deduplicated, so the match can exceed memory.delete_confirm_threshold;
 * the daemon then refuses with bulk_confirm_required and the count, and a
 * second confirm decides whether to resend with confirm_bulk. Resolves null
 * when the first confirm is declined (nothing to report).
 */
export async function runDelete(
  text: string,
  post: Post,
  ask: (req: Ask) => Promise<boolean>,
): Promise<Outcome | null> {
  if (!(await ask(deleteConfirmCopy(text)))) return null;
  let r: DeleteResult;
  try {
    r = await post<DeleteResult>("/api/delete", { text });
    if (r.error === "bulk_confirm_required") {
      const n = r.would_delete ?? 0;
      if (!(await ask(bulkConfirmCopy(n, r.threshold)))) {
        return { ok: true, changed: false, tone: "warn", message: `Not deleted: ${plural(n, "memory matches", "memories match")} this text` };
      }
      r = await post<DeleteResult>("/api/delete", { text, confirm_bulk: true });
    }
  } catch (e) {
    return { ok: false, message: actionError(e, "Delete") };
  }
  const err = softError(r);
  if (err) return { ok: false, message: `Delete failed: ${words(err)}` };
  const n = r.deleted_count ?? 1;
  if (n === 0) return { ok: true, changed: false, tone: "warn", message: "Nothing matched this text, so nothing was deleted" };
  return { ok: true, changed: true, tone: "ok", message: `Deleted ${plural(n, "memory", "memories")}` };
}

export async function runReinforce(entryId: number, post: Post): Promise<Outcome & { reinforcements?: number }> {
  let r: ReinforceResult;
  try {
    r = await post<ReinforceResult>("/api/reinforce", { entry_id: entryId });
  } catch (e) {
    return { ok: false, message: actionError(e, "Reinforce") };
  }
  const err = softError(r);
  if (err) return { ok: false, message: `Reinforce failed: ${err}` };
  if (r.reinforced === false) {
    return { ok: false, message: "This memory has faded from the bank, so there is nothing to reinforce." };
  }
  return { ok: true, changed: true, tone: "ok", message: "Memory reinforced", reinforcements: r.reinforcements };
}
