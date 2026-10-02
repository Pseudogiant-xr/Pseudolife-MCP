// Typed reads for the Graph view: the scoped whole graph, the project list,
// the review scan (pulses, flag banners, pending count), one entity's page,
// and a fact slot's version history. Writes go through lib/reviewRunner.

import { get } from "./client";

export interface GraphFact {
  attribute: string;
  value: string;
  origin?: string | null;
  confidence?: number | null;
}

export interface GraphNode {
  entity: string;
  canonical?: string;
  etype?: string | null;
  aliases?: string[];
  community?: number | string | null;
  sources?: string[];
  /** Epoch seconds. */
  created_at?: number | null;
  facts?: GraphFact[];
}

export interface GraphEdge {
  src: string;
  relation: string;
  dst: string;
  derived?: boolean;
  confidence?: number | null;
  origin?: string | null;
  /** Epoch seconds. */
  asserted_at?: number | null;
  /** Provenance class from classify_edge (extracted, inferred, ambiguous). */
  tag?: string | null;
}

export interface GraphResponse {
  found?: boolean;
  entity?: string | null;
  scope?: string;
  nodes?: GraphNode[];
  edges?: GraphEdge[];
  truncated?: boolean;
  total_nodes?: number;
  total_edges?: number;
  /** HTTP 200 refusal, e.g. graph_requires_postgres in file mode. */
  error?: string;
  hint?: string;
}

export interface Project {
  source: string;
  entities: number;
  /** The umbrella scope this one rolls up into (memory.scopes.rollup). */
  parent?: string;
}

export interface ProjectsResponse {
  projects?: Project[];
}

export interface MergeRow {
  id: number | string;
  from: string;
  into: string;
  similarity?: number | null;
  reason?: string | null;
  group?: string | null;
}

export interface JunkRow {
  id: number | string;
  entity: string;
  reason?: string | null;
}

export interface LinkRow {
  id: number | string;
  src: string;
  relation: string;
  dst: string;
  confidence?: number | null;
  similarity?: number | null;
}

export interface ReviewFinding {
  type: string;
  severity?: string;
  label?: string;
  action?: string;
  /** Names for duplicate / orphan / unattributed / test_artifact; rows for junk_candidate. */
  entities?: (string | JunkRow)[];
  merges?: MergeRow[];
  links?: LinkRow[];
  score?: number;
  suggested_relation?: string;
}

export interface ReviewResponse {
  findings?: ReviewFinding[];
  counts?: { total?: number };
}

export interface WikiFlag {
  kind: string;
  id?: number | string;
  entity?: string | null;
  into?: string | null;
  src?: string | null;
  relation?: string | null;
  dst?: string | null;
  /** Duplicate pairs (client-side, from the review scan). */
  a?: string;
  b?: string;
  other?: string;
  reason?: string | null;
}

export interface WikiFact {
  attribute: string;
  value: string;
  origin?: string | null;
  confidence?: number | null;
  asserted_at?: number | null;
  history_available?: boolean;
}

export interface WikiWorldFact {
  attribute: string;
  value: string;
  confidence?: number | null;
  source_url?: string | null;
  retrieved_at?: number | null;
}

export interface WikiRelation {
  relation: string;
  target?: string;
  source?: string;
  derived?: boolean;
  confidence?: number | null;
  tag?: string | null;
}

export interface WikiMention {
  id?: number;
  ts?: number | null;
  source?: string | null;
  episode_title?: string | null;
  text?: string | null;
}

export interface WikiTimelineItem {
  ts?: number | null;
  kind: string;
  text: string;
}

export interface WikiPage {
  found: boolean;
  entity: string;
  canonical?: string;
  etype?: string | null;
  aliases?: string[];
  projects?: { source: string; count?: number; origin?: string }[];
  community?: number | string | null;
  first_seen?: number | null;
  facts?: WikiFact[];
  world_facts?: WikiWorldFact[];
  relations?: { out?: WikiRelation[]; in?: WikiRelation[] };
  mentions?: WikiMention[];
  timeline?: WikiTimelineItem[];
  flags?: WikiFlag[];
  error?: string;
  hint?: string;
}

/** One version of a slot. Single-valued slots carry tx_time/status; set slots carry at/event. */
export interface HistoryVersion {
  value: string;
  status?: string;
  tx_time?: number | null;
  asserted_at?: number | null;
  at?: number | null;
  event?: string;
}

export interface HistoryResponse {
  kind?: string;
  versions?: HistoryVersion[];
  error?: string;
}

export const graphApi = {
  graph: (scope: string) => get<GraphResponse>("/api/graph", { scope: scope === "all" ? "" : scope }),
  projects: () => get<ProjectsResponse>("/api/graph/projects"),
  review: (scope: string) => get<ReviewResponse>("/api/graph/review", { scope: scope === "all" ? "" : scope }),
  wiki: (entity: string) => get<WikiPage>("/api/wiki", { entity }),
  history: (entity: string, attribute: string) => get<HistoryResponse>("/api/facts/history", { entity, attribute }),
};
