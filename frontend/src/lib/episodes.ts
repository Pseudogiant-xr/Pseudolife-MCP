// Episodes timeline helpers: ordering and the row's facts. A field the API
// leaves out is unavailable, never zero.

import type { Episode, EpisodeList } from "./api/episodes";
import { fmtDuration, plural } from "./format";

/** The list from either answer shape, newest start first; an episode with no start sorts last. */
export function sortEpisodes(r: EpisodeList | null | undefined): Episode[] {
  const eps = [...(r?.episodes ?? r?.entries ?? [])];
  return eps.sort((a, b) => (b.started_at ?? -Infinity) - (a.started_at ?? -Infinity));
}

export function isOpen(e: Pick<Episode, "ended_at">): boolean {
  return e.ended_at === null || e.ended_at === undefined;
}

export function episodeName(e: Pick<Episode, "title" | "id">): string {
  return (e.title ?? "").trim() || e.id;
}

/** "3 h 5 m" for a closed episode, "open" for one still running, "" when a bound is missing. */
export function episodeSpan(e: Pick<Episode, "started_at" | "ended_at">): string {
  if (isOpen(e)) return "open";
  if (!e.started_at || !e.ended_at) return "";
  return fmtDuration(e.ended_at - e.started_at);
}

/** "12 entries"; "entry count unavailable" when the daemon sent none. */
export function entryCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "entry count unavailable";
  return plural(n, "entry", "entries");
}

export function openCount(eps: Episode[]): number {
  return eps.filter(isOpen).length;
}
