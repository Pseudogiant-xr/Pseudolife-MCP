// Typed read of the graph digest (GET /api/graph/digest), built by the dream
// sweep (pseudolife_memory/memory/graph_insight.py build_digest).

import { get } from "./client";

export interface DigestQuestion {
  question: string;
  /** contested_fact, bridge_entity, verify_inferred, isolated_entity, low_cohesion */
  type?: string | null;
  why?: string | null;
}

/** A hub. The daemon ranks these by betweenness, degree breaking ties. */
export interface GodNode {
  entity_id?: number;
  display: string;
  degree?: number | null;
  betweenness?: number | null;
}

export interface Community {
  id?: number;
  /** The community's highest-degree member. */
  label: string;
  size?: number | null;
  cohesion?: number | null;
}

export interface Surprise {
  src: string;
  relation?: string | null;
  dst: string;
  confidence?: number | null;
  origin?: string | null;
  score?: number | null;
  why?: string | null;
}

export interface Digest {
  computed_at?: number | null;
  totals?: { entities?: number; edges?: number; communities?: number };
  questions?: DigestQuestion[];
  god_nodes?: GodNode[];
  communities?: Community[];
  surprises?: Surprise[];
}

export interface DigestResponse {
  available: boolean;
  /** "no_digest" (no dream has built one yet) or "no_storage". */
  reason?: string;
  digest?: Digest;
}

export const insightApi = {
  digest: () => get<DigestResponse>("/api/graph/digest"),
};
