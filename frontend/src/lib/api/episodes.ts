// Episodes: read-only. The write routes (start, end, prune, rename, merge)
// stay agent and CLI only, as in the classic console.

import { get } from "./client";
import type { Epoch, RecentEntry } from "./types";

export interface Episode {
  id: string;
  title?: string | null;
  started_at?: Epoch | null;
  ended_at?: Epoch | null;
  hint?: string | null;
  entry_count?: number | null;
  parent_id?: string | null;
}

/** GET /api/episodes. Older daemons answered with `entries`. */
export interface EpisodeList {
  count?: number;
  episodes?: Episode[];
  entries?: Episode[];
}

export interface EpisodeSummary {
  found: boolean;
  id?: string;
  title?: string | null;
  started_at?: Epoch | null;
  ended_at?: Epoch | null;
  hint?: string | null;
  entry_count?: number | null;
  tag_distribution?: { tag: string; count: number }[];
  source_distribution?: { source: string; count: number }[];
  /** The newest 20 entries of the episode. */
  recent_entries?: RecentEntry[];
}

export const episodesApi = {
  list: (limit = 200) => get<EpisodeList>("/api/episodes", { limit }),
  summary: (id: string) => get<EpisodeSummary>("/api/episodes/summary", { id }),
};
