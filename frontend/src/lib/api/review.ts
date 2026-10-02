// Typed reads for the graph review queue (GET /api/graph/review and friends)
// and the one write this view owns directly (POST /api/graph/rejudge). Every
// decision on a finding goes through lib/reviewRunner.svelte.ts instead.
// Shapes follow pseudolife_memory/service.py graph_review and
// memory/graph_review.py, not the fixtures (which omit `automation`).

import { get, post } from "./client";

export type AutomationState = "unfiled" | "pending" | "gated" | "manual" | "terminal";

export interface Automation {
  state: string;
  reason?: string | null;
}

/** A judge model's opinion on a filed proposal; shown, never applied here. */
export interface Judge {
  verdict: string;
  confidence?: number | null;
  note?: string | null;
  model?: string | null;
  /** For a "relate" verdict: the relation that holds from `from` to `into`. */
  relation?: string | null;
}

export interface MergeItem {
  id: number | string;
  from: string;
  into: string;
  similarity?: number | null;
  reason?: string | null;
  /** Rows sharing a group pivot on this entity: at most one of them can land. */
  group?: string | null;
  judge?: Judge;
  judge2?: Judge;
}

export interface JunkItem {
  id: number | string;
  entity: string;
  reason?: string | null;
}

export interface LinkItem {
  id: number | string;
  src: string;
  relation: string;
  dst: string;
  confidence?: number | null;
  similarity?: number | null;
  rationale?: string | null;
  source?: string | null;
  tag?: string | null;
  judge?: Judge;
}

export interface DubiousEdge {
  src: string;
  relation: string;
  dst: string;
  confidence?: number | null;
  tag?: string | null;
}

export interface Finding {
  type: string;
  severity?: string;
  label?: string;
  action?: string;
  automation?: Automation;
  /** Names (duplicate, orphan, unattributed, test_artifact) or junk items. */
  entities?: (string | JunkItem)[];
  edges?: DubiousEdge[];
  merges?: MergeItem[];
  links?: LinkItem[];
  /** duplicate: name-token Jaccard similarity. */
  score?: number | null;
  suggested_relation?: string | null;
}

export interface RecentMerge {
  id?: number;
  proposal_id?: number | null;
  entity?: string | null;
  into?: string | null;
  status?: string | null;
  score?: number | null;
  reason?: string | null;
  decided_by?: string | null;
  /** Epoch seconds. */
  decided_at?: number | null;
}

export interface MergeStats {
  accepted?: number;
  rejected?: number;
  total?: number;
  accept_rate?: number | null;
}

export interface AutoDecision {
  queue?: string | null;
  action?: string | null;
  state?: string | null;
  proposal_id?: number | string | null;
  pair?: string[] | null;
  recorded_at?: number | null;
  ended_at?: number | null;
  end_reason?: string | null;
}

export interface ReviewResponse {
  findings?: Finding[];
  counts?: { total?: number };
  /** Findings per automation state (live daemons; the fixtures omit it). */
  automation?: Partial<Record<AutomationState, number>>;
  recent_merges?: RecentMerge[];
  merge_decision_stats?: MergeStats;
  automatic_decisions?: AutoDecision[];
}

export interface SlotSide {
  entity: string;
  attribute: string;
  value?: string | null;
  /** Lessons: "+" (do) or "-" (avoid). */
  polarity?: string | null;
  outcome?: string | null;
  about?: string | null;
  /** World facts: the cited source, agent-written (render via SourceLink). */
  source_url?: string | null;
}

export interface SlotPair {
  a_key?: string;
  b_key?: string;
  a: SlotSide;
  b: SlotSide;
  similarity?: number | null;
}

export interface CurationResponse {
  lesson_duplicates?: SlotPair[];
  world_duplicates?: SlotPair[];
  error?: string;
}

export interface Project {
  source: string;
  entities: number;
  /** The umbrella this source rolls up into (memory.scopes.rollup). */
  parent?: string;
}

export interface ProvenanceEntry {
  id?: number;
  band?: string | null;
  source?: string | null;
  ts?: number | null;
  text?: string | null;
  episode_title?: string | null;
}

export interface Provenance {
  found?: boolean;
  entity?: string;
  sources?: { source: string; count: number; origin?: string | null }[];
  entries?: ProvenanceEntry[];
  error?: string;
}

export interface ChainEvent {
  t?: number | null;
  kind: string;
  summary?: string | null;
  refs?: { episode_title?: string | null } & Record<string, unknown>;
}

export interface Chain {
  found?: boolean;
  entity?: string;
  count?: number;
  events?: ChainEvent[];
  error?: string;
}

export interface RejudgeResult {
  requeued?: number;
  queues?: Record<string, number>;
  limit?: number;
  error?: string;
}

export const reviewApi = {
  review: (scope: string) => get<ReviewResponse>("/api/graph/review", { scope: scope === "all" ? undefined : scope }),
  curation: () => get<CurationResponse>("/api/curation/duplicates"),
  projects: () => get<{ projects?: Project[] }>("/api/graph/projects"),
  provenance: (entity: string) => get<Provenance>("/api/graph/entity-provenance", { entity }),
  chain: (entity: string) => get<Chain>("/api/chain", { entity }),
  /** Forget up to 32 pending judge opinions so the next sweep re-judges them. */
  rejudge: () => post<RejudgeResult>("/api/graph/rejudge", { limit: 32 }),
};
