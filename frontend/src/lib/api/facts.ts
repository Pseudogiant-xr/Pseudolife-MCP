// Typed calls for the canonical stores: cortex facts (/api/facts*), world
// facts (/api/world) and lessons (/api/lessons). Field lists follow the
// service serialisers (service.py _cortex_record_to_dict, _contender_fields,
// _world_record_to_dict, _lesson_record_to_dict); every field the daemon may
// leave out is optional, and a missing one renders as unavailable, never 0.

import { get, post } from "./client";

/** The dumps are capped server side; this is the cap the classic console asked for. */
export const DUMP_LIMIT = 1000;

/** /api/facts, /api/world and /api/lessons all wrap their dump the same way. */
export interface Limited<T> {
  count: number;
  total: number;
  truncated: boolean;
  entries: T[];
}

export interface Fact {
  entity: string;
  attribute: string;
  value: string;
  origin?: string | null;
  confidence?: number | null;
  effective_confidence?: number | null;
  /** "scalar", or "member" for one row of a set-valued slot. */
  kind?: string;
  status?: string;
  freshness_class?: string | null;
  /** Past twice its freshness TTL: re-verify at the source before acting. */
  stale?: boolean;
  /** Present under the "demote" stale policy. */
  warning?: string;
  /** Present under the "quarantine" stale policy: `value` is then a wrapper. */
  last_known_value?: string;
  contested?: boolean;
  contender_value?: string | null;
  contender_origin?: string | null;
  re_verify?: boolean;
  re_verify_reason?: string | null;
  age?: string | null;
  tx_time?: number | null;
  asserted_at?: number | null;
  last_confirmed?: number | null;
  writer_id?: string | null;
  /** Ids of the memories this slot was derived from (Postgres banks only). */
  source_entries?: number[];
}

/** One version of a scalar slot (GET /api/facts/history), oldest first on the wire. */
export interface ScalarVersion {
  value: string;
  status?: string;
  writer_id?: string | null;
  age?: string | null;
  tx_time?: number | null;
  asserted_at?: number | null;
}

/** One membership event of a set slot. */
export interface SetVersion {
  value: string;
  event: "added" | "removed";
  at?: number | null;
}

export type FactHistory =
  | { kind?: undefined; entity: string; attribute: string; count: number; versions: ScalarVersion[] }
  | { kind: "set"; entity: string; attribute: string; count: number; versions: SetVersion[] };

/** POST /api/facts/resolve. A 200 with resolved:false is a refusal. */
export interface ResolveResult {
  resolved: boolean;
  reason?: string;
  accepted?: boolean;
  action?: string;
  error?: string;
}

/** POST /api/facts/set. */
export interface WriteResult {
  action?: "inserted" | "confirmed" | "superseded" | "contested" | string;
  error?: string;
}

/** POST /api/facts/forget. Hard delete: `removed` counts the records gone. */
export interface ForgetResult {
  removed?: number;
  error?: string;
}

export type Origin = "user" | "action" | "agent";

export interface FactInput {
  entity: string;
  attribute: string;
  value: string;
  origin: Origin;
  confidence: number;
}

export interface WorldFact {
  entity: string;
  attribute: string;
  value: string;
  confidence?: number | null;
  effective_confidence?: number | null;
  stale?: boolean;
  warning?: string;
  last_known_value?: string;
  origin?: string | null;
  freshness_class?: string | null;
  source_url?: string | null;
  source_quote?: string | null;
  retrieved_at?: number | null;
  asserted_at?: number | null;
  last_confirmed?: number | null;
  /** The fixture server sends one; the real service does not. */
  age?: string | null;
}

export interface Lesson {
  task: string;
  aspect?: string | null;
  lesson: string;
  about?: string | null;
  polarity?: string | null;
  outcome?: string | null;
  confidence?: number | null;
  origin?: string | null;
  asserted_at?: number | null;
  last_confirmed?: number | null;
  re_verify?: boolean;
  re_verify_reason?: string | null;
}

export const factsApi = {
  list: () => get<Limited<Fact>>("/api/facts", { limit: DUMP_LIMIT }),
  history: (entity: string, attribute: string) => get<FactHistory>("/api/facts/history", { entity, attribute }),
  resolve: (entity: string, attribute: string, accept: boolean) =>
    post<ResolveResult>("/api/facts/resolve", { entity, attribute, accept }),
  set: (f: FactInput) => post<WriteResult>("/api/facts/set", { ...f }),
  // Without an attribute the daemon forgets every slot of the entity, so an
  // empty one is refused here rather than sent.
  forget: (entity: string, attribute: string) => {
    if (!attribute) return Promise.reject(new Error("attribute is required"));
    return post<ForgetResult>("/api/facts/forget", { entity, attribute });
  },
  world: () => get<Limited<WorldFact>>("/api/world", { limit: DUMP_LIMIT }),
  lessons: () => get<Limited<Lesson>>("/api/lessons", { limit: DUMP_LIMIT }),
};
