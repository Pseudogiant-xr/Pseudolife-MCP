// Read-only projection of the independently versioned RE proof store.
import { get } from "./client";

export interface EvidenceScope {
  project: string;
  binary_id: string;
  artifacts: number;
  claims: Record<string, number>;
}

export interface EvidenceArtifact {
  id: number;
  kind: string;
  locator: string;
  source_path: string;
  content_hash: string;
  summary?: string | null;
  addresses: string[];
  ingested_at: number;
  payload_keys: string[];
}

export interface EvidenceClaim {
  id: number;
  subject: string;
  claim: string;
  status: string;
  confidence?: number | null;
  created_at: number;
  updated_at: number;
  evidence_ids: number[];
}

export interface EvidenceDashboard {
  read_only: true;
  scopes: EvidenceScope[];
  selection: { project: string; binary_id: string } | null;
  totals: { artifacts: number; claims: Record<string, number> };
  artifacts: EvidenceArtifact[];
  claims: EvidenceClaim[];
}

export function loadEvidence(project: string, binaryId: string, query: string, status: string) {
  return get<EvidenceDashboard>("/api/re-evidence", {
    project, binary_id: binaryId, q: query.trim(), status, limit: 250,
  });
}
