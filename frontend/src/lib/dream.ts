// What "Run a dream now" tells the person. A dream can come back without an
// error and still have consolidated nothing: the service holds the cursor when
// the extractor (or the write) fails, and the per-memory retry can set aside
// every memory it pulled. Neither may read as "finished".

import type { DreamRunResult } from "./api/types";
import { fmtNum, plural, words } from "./format";

export interface DreamOutcome {
  tone: "ok" | "warn" | "danger";
  message: string;
}

export function describeDreamRun(r: DreamRunResult): DreamOutcome {
  if (r.error) return { tone: "danger", message: `The dream did not run: ${r.error}` };
  if (r.skipped) return { tone: "warn", message: `The dream was skipped: ${words(r.skipped)}.` };
  const why = r.extractor_error?.error || words(r.extractor_error?.reason) || "";
  if (r.extractor_failed) {
    const what = r.hold_phase === "write" ? "writing its results to the database failed" : "the extractor failed";
    return {
      tone: "danger",
      message: `The dream was held because ${what}${why ? ` (${why})` : ""}. Nothing was consolidated; the backlog stays queued and the next sweep retries it.`,
    };
  }
  const pulled = r.pulled ?? 0;
  if (!pulled) return { tone: "ok", message: "Nothing was waiting, so the dream pulled no memories." };
  if ((r.quarantined ?? 0) >= pulled) {
    return {
      tone: "warn",
      message: `The extractor failed on every memory the dream pulled${why ? ` (${why})` : ""}, so they were set aside to retry.`,
    };
  }
  const parts = [
    plural(pulled, "memory", "memories") + " pulled",
    r.claims !== undefined ? plural(r.claims, "claim") : "",
    r.inserted !== undefined ? `${fmtNum(r.inserted)} new` : "",
    r.confirmed !== undefined ? `${fmtNum(r.confirmed)} confirmed` : "",
    r.contested !== undefined ? `${fmtNum(r.contested)} contested` : "",
    r.superseded !== undefined ? `${fmtNum(r.superseded)} superseded` : "",
    r.relations !== undefined ? plural(r.relations, "edge") : "",
    r.lessons !== undefined ? plural(r.lessons, "lesson") : "",
    r.quarantined ? `${fmtNum(r.quarantined)} set aside to retry` : "",
  ].filter(Boolean);
  const by = r.extractor ? ` with the ${r.extractor} extractor` : "";
  return { tone: "ok", message: `The dream finished${by}: ${parts.join(", ")}.` };
}
