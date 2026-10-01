// Typed calls for Recall: the multi-hop walk (GET /api/recall) and the
// shortest relation path between two entities (GET /api/graph/path). Shapes
// follow memory/recall.py recall_state_to_dict and service.graph_path.

import { get } from "./client";

export interface RecallFact {
  attribute: string;
  value: string;
  re_verify?: boolean;
  re_verify_reason?: string | null;
  /** A constraint fact moved to the front of a seed entity's list. */
  pinned?: boolean;
}

export interface RecallEntity {
  entity: string;
  facts?: RecallFact[];
}

export interface RecallResponse {
  query?: string;
  seeds?: string[];
  entities?: RecallEntity[];
  paths?: string[][];
  texts?: string[];
  iterations?: number;
  hops?: number;
  low_confidence?: boolean;
  /** Served only when a search ceiling cut the walk short. */
  truncated?: boolean;
  searches_issued?: number;
  error?: string;
}

export interface PathEdge {
  src: string;
  relation: string;
  dst: string;
}

export interface PathResponse {
  found?: boolean;
  /** The name that resolved to no entity, when found is false. */
  missing?: string;
  path?: string[];
  edges?: PathEdge[];
  hops?: number | null;
  source?: string;
  target?: string;
  /** graph_requires_postgres on a file-mode daemon. */
  error?: string;
  hint?: string;
}

export const recallApi = {
  recall: (q: string, hops: number) => get<RecallResponse>("/api/recall", { q, hops }),
  path: (source: string, target: string) => get<PathResponse>("/api/graph/path", { source, target }),
};
