// Typed calls for the associative store: search, recent, the ranking trace,
// one entry's engram detail, the source facets, the corrections (supersede,
// delete, reinforce) and the consolidation review. Shapes follow
// pseudolife_memory/service.py (_entry_to_dict, trace, get_entry, ...), not
// the devserver fixtures. Every field the daemon may omit is optional.

import { get, post } from "./client";
import type { Epoch } from "./types";

/** One associative entry as /api/search, /api/recent and /api/consolidation serve it. */
export interface StreamEntry {
  /** Storage row id; null in file mode or before the row persisted. */
  id: number | null;
  text: string;
  source?: string;
  /** The band that holds the entry ("flat" under the single-band default). */
  bank?: string;
  timestamp?: Epoch;
  /** Server-rendered age; the store does not send one today, so it is derived. */
  age?: string;
  access_count?: number;
  surprise_score?: number;
  superseded?: boolean;
  superseded_at?: Epoch | null;
  superseded_by_text?: string | null;
  superseded_by_id?: number | null;
  superseded_by_current?: boolean;
  episode_id?: string | null;
  episode_title?: string | null;
  tags?: string[];
  /** Search only. */
  score?: number;
}

/** A canonical fact served ahead of the entries (cortex search-first). */
export interface CortexHit {
  entity: string;
  attribute: string;
  value: string;
  origin?: string;
  score?: number;
  confidence?: number;
  stale?: boolean;
  contested?: boolean;
  re_verify?: boolean;
  re_verify_reason?: string | null;
}

export interface SearchResponse {
  query?: string;
  count?: number;
  entries?: StreamEntry[];
  low_confidence?: boolean;
  cortex?: CortexHit[];
}

export interface RecentEntriesResponse {
  count?: number;
  entries?: StreamEntry[];
}

export interface TraceCandidate {
  text_preview?: string;
  source?: string;
  raw_score?: number;
  superseded?: boolean;
  kept?: boolean;
  drop_reason?: string | null;
}

export interface TraceTier {
  name: string;
  depth?: number;
  filtered_out?: boolean;
  entry_count?: number;
  eligible_count?: number;
  candidates?: TraceCandidate[];
}

export interface Trace {
  config?: Record<string, unknown>;
  filters?: Record<string, unknown>;
  tiers?: TraceTier[];
  reranker?: { fired?: boolean };
  bm25?: { fired?: boolean };
  final_topk?: { text_preview?: string; score?: number; source?: string; bank?: string }[];
}

export interface TraceResponse {
  query?: string;
  count?: number;
  trace?: Trace | null;
}

export interface ConsolidatedFact {
  entity: string;
  attribute: string;
  value: string;
}

/** GET /api/entry?id= (bumps access_count: call only on an explicit click). */
export interface EntryDetail {
  found: boolean;
  faded?: boolean;
  entry_id?: number;
  text?: string;
  source?: string | null;
  reinforcements?: number;
  explicit_reinforcements?: number;
  access_count?: number;
  consolidated_into?: ConsolidatedFact[];
  superseded?: boolean;
  superseded_at?: Epoch | null;
  superseded_by_text?: string | null;
  superseded_by_id?: number | null;
}

export interface SourceCount {
  source: string;
  count: number;
}

export interface SourcesResponse {
  sources?: SourceCount[];
  total?: number;
}

/** POST /api/supersede and /api/consolidate answer the same correction shape. */
export interface CorrectionResult {
  superseded_count?: number;
  superseded_texts?: string[];
  superseded_ids?: number[];
  new_memory_stored?: boolean;
  reason?: string;
  error?: string;
}

export interface DeleteResult {
  deleted_count?: number;
  deleted_texts?: string[];
  error?: string;
  would_delete?: number;
  threshold?: number;
  sample_texts?: string[];
}

export interface ReinforceResult {
  reinforced?: boolean;
  faded?: boolean;
  entry_id?: number;
  /** Not sent by the daemon today; read when present. */
  reinforcements?: number;
  error?: string;
}

export interface ConsolidationCluster {
  cohesion?: number;
  seed_score?: number;
  size?: number;
  members?: StreamEntry[];
}

export interface ConsolidationResponse {
  query?: string | null;
  episode?: string | null;
  count?: number;
  clusters?: ConsolidationCluster[];
}

/** GET /api/config, read only for the dream gauge thresholds. */
export interface ConfigKnob {
  path: string;
  value?: unknown;
}

export interface ConfigResponse {
  groups?: { name: string; knobs: ConfigKnob[] }[];
}

export type Post = <T>(path: string, body: Record<string, unknown>) => Promise<T>;

export interface SearchArgs {
  q: string;
  source: string | null;
  rerank: boolean;
  bm25: boolean;
}

/**
 * Query params for /api/search and /api/trace. rerank and bm25 are sent only
 * when switched on: an absent param tells the daemon to follow its config.
 */
export function searchParams(a: SearchArgs, topK: number): Record<string, string | number | boolean | undefined> {
  return {
    q: a.q,
    top_k: topK,
    rerank: a.rerank ? true : undefined,
    bm25: a.bm25 ? true : undefined,
    source: a.source || undefined,
  };
}

export const streamApi = {
  search: (a: SearchArgs) => get<SearchResponse>("/api/search", searchParams(a, 25)),
  recent: (source: string | null) => get<RecentEntriesResponse>("/api/recent", { n: 50, source: source || undefined }),
  trace: (a: SearchArgs) => get<TraceResponse>("/api/trace", searchParams(a, 12)),
  /** Bumps the entry's access_count on the daemon. */
  entry: (id: number) => get<EntryDetail>("/api/entry", { id }),
  sources: () => get<SourcesResponse>("/api/sources"),
  consolidation: (q: string) => get<ConsolidationResponse>("/api/consolidation", { q: q || undefined }),
  config: () => get<ConfigResponse>("/api/config"),
  post: post as Post,
};
