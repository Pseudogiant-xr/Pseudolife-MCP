// Pure logic behind the World view: filter, sort, and the time a cited fact
// was last checked. Unit tested in world.test.ts.

import type { WorldFact } from "./api/facts";

/** Case-insensitive match over "entity attribute value", as the classic console did. */
export function worldMatches(w: Pick<WorldFact, "entity" | "attribute" | "value">, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return `${w.entity} ${w.attribute} ${w.value}`.toLowerCase().includes(needle);
}

/** Filtered and sorted by "entity.attribute" (localeCompare); the input is not mutated. */
export function filterWorld(entries: WorldFact[], q: string): WorldFact[] {
  return entries
    .filter((w) => worldMatches(w, q))
    .sort((a, b) => `${a.entity}.${a.attribute}`.localeCompare(`${b.entity}.${b.attribute}`));
}

/** The trust shown: the age-decayed confidence when served, else the stored one. */
export function worldConfidence(w: Pick<WorldFact, "effective_confidence" | "confidence">): number | null {
  const v = w.effective_confidence ?? w.confidence;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** When the fact was last confirmed, else retrieved, else asserted; null when none is served. */
export function worldCheckedAt(w: Pick<WorldFact, "last_confirmed" | "retrieved_at" | "asserted_at">): number | null {
  return w.last_confirmed ?? w.retrieved_at ?? w.asserted_at ?? null;
}

/** Stale world facts in the given rows; null when no row carries the flag. */
export function staleCount(entries: WorldFact[]): number | null {
  if (!entries.some((w) => typeof w.stale === "boolean")) return null;
  return entries.filter((w) => w.stale === true).length;
}
