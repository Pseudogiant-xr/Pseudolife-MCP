// Shapes of the daemon's JSON as the console reads it. Every field the API
// may omit (older daemons, a missing layer, fixture data) is optional here,
// so views must treat a missing value as "unavailable", never as zero.

/** Epoch seconds, as the daemon sends every timestamp. */
export type Epoch = number;

/** GET /health (open; 503 with the same shape when degraded). */
export interface Health {
  status: string;
  schema?: number;
  storage?: string;
  persist_errors?: number;
  auth?: boolean;
  mode?: string;
  fixtures?: boolean;
  error?: string;
}

export interface Band {
  name: string;
  size: number;
  capacity: number;
  hit_rate?: number | null;
  hit_count?: number;
  retention_policy?: string;
}

export interface RetrievalLog {
  events?: number | null;
  uses?: number | null;
  lesson_searches?: number | null;
  last_event_at?: Epoch | null;
  unavailable?: boolean;
  enabled?: boolean;
  write_errors?: number;
}

export interface Stats {
  bands?: Band[];
  preset?: string;
  total_memories?: number;
  retrieval_queries?: number;
  true_drops?: number;
  retrieval_log?: RetrievalLog;
}

export interface PendingPair {
  pending: number;
  retry_pending: number;
}

export interface ReviewQueue {
  pending?: { merge: number; junk: number; link: number };
  oldest_merge_age_days?: number | null;
  oldest_unjudged_merge_age_days?: number | null;
  attention?: { needed: boolean; reasons: string[] };
  available?: boolean;
  error?: string;
}

export interface Stall {
  since: Epoch;
  reason?: string | null;
  consecutive_failures?: number;
  last_error?: string | null;
  last_success_at?: Epoch | null;
  ended_at?: Epoch | null;
}

export interface DreamStatus {
  backlog?: number;
  idle_seconds?: number;
  dream_cursor?: Epoch | null;
  would_fire?: boolean;
  infer_outcomes?: PendingPair;
  digests?: PendingPair;
  review_queue?: ReviewQueue;
  stall?: Stall | null;
  last_stall?: Stall | null;
  extractor_mode?: string;
  primary_healthy?: boolean;
  error?: string;
  detail?: string;
}

export interface WindowCount {
  current: number;
  previous: number;
}

export interface LoopHealth {
  available: boolean;
  window_days?: number;
  stores?: WindowCount;
  outcomes?: WindowCount & { by_outcome?: Record<string, number> };
  sessions?: number;
  pending_signals?: number;
  pending_signals_expired?: number;
  last_lesson_at?: Epoch | null;
  lessons_current?: number;
}

export interface Counts {
  entries?: number;
  facts?: number;
  facts_contested?: number;
  facts_by_origin?: Record<string, number>;
  world?: number;
  world_stale?: number;
  lessons?: number;
  episodes?: number;
  sources?: number;
  tags?: number;
}

/** GET /api/overview */
export interface Overview {
  health?: {
    status?: string;
    schema?: number;
    storage?: string;
    writer_id?: string;
    persist_errors?: number;
    fixtures?: boolean;
  };
  counts?: Counts;
  stats?: Stats;
  dream?: DreamStatus;
  loop?: LoopHealth;
}

/** One entry of GET /api/recent?n= */
export interface RecentEntry {
  id: number | string;
  text: string;
  source?: string;
  timestamp?: Epoch;
  tags?: string[];
  episode_id?: string | null;
  superseded?: boolean;
}

export interface RecentResponse {
  count?: number;
  entries: RecentEntry[];
}

/** POST /api/dream/run */
export interface DreamRunResult {
  pulled?: number;
  claims?: number;
  inserted?: number;
  confirmed?: number;
  contested?: number;
  superseded?: number;
  relations?: number;
  lessons?: number;
  extractor?: string;
  skipped?: string;
  error?: string;
  /** The cursor was held: nothing consolidated, the next sweep retries. */
  extractor_failed?: boolean;
  /** "extract" (the extractor) or "write" (the database). */
  hold_phase?: string;
  extractor_error?: { reason?: string; error?: string };
  /** Memories the per-memory retry set aside. */
  quarantined?: number;
}

// ---- coordination board: GET /api/agents?view=coordination&limit=50 ----

export interface BoardChild {
  label: string;
  since: Epoch;
  agent_id?: string;
}

export type Lifecycle = "registered" | "attached" | "detached" | "revoked" | string;

export interface BoardAgent {
  agent_id: string;
  principal: string;
  label: string;
  project: string;
  task: string;
  status: string;
  episode: string;
  lifecycle: Lifecycle;
  last_activity: Epoch;
  created_at: Epoch;
  children: BoardChild[];
  park_reason: string | null;
  park_needs: string;
  park_clear_by: string;
  park_resume: string;
  park_expires: Epoch | null;
  park_set_at: Epoch | null;
  parent_agent_id: string | null;
  subagent: boolean;
  status_expires_at: Epoch | null;
  status_overdue: boolean;
  status_set_at: Epoch | null;
  /** Server-rendered text, e.g. "5 minutes ago" or "unknown". */
  status_age: string;
  status_stale: boolean;
  /** A count for the caller's own principal, null for everyone else's peers. */
  pending_count: number | null;
}

export interface LeaseHolder {
  agent_id: string;
  label: string;
  principal: string;
  purpose: string;
  acquired_at: Epoch | null;
  expires_at: Epoch | null;
  expected_end: Epoch | null;
  fence?: number;
}

export interface LeaseWaiter {
  agent_id: string;
  label: string;
  enqueued_at: Epoch;
  purpose: string;
}

export interface Lease {
  name: string;
  holder: LeaseHolder | null;
  fence: number | null;
  expires_at: Epoch | null;
  expected_end: Epoch | null;
  stale: boolean;
  expired: boolean;
  queued: number;
  queue: LeaseWaiter[];
}

export type BoardEventKind =
  | "send"
  | "read"
  | "ack"
  | "attempt"
  | "served"
  | "woke"
  | "lease_expire"
  | "expire";

export interface BoardEvent {
  seq: number;
  event: BoardEventKind | string;
  created_at: Epoch;
  agent_id: string | null;
  recipient_agent_id: string | null;
  message_id: string | null;
  project: string | null;
  task: string | null;
  /** Wake decision for a send, read path for a read, otherwise null. */
  detail: string | null;
  expired_count: number;
}

export interface BoardSnapshot {
  enabled: boolean;
  available: boolean;
  reason?: "disabled" | "not_initialized" | string;
  snapshot_at?: Epoch;
  agents?: BoardAgent[];
  truncated?: boolean;
  idle_omitted?: number;
  leases?: Lease[];
  leases_truncated?: boolean;
  events?: BoardEvent[];
  events_truncated?: boolean;
}

/** Error body of a refused /api call. */
export interface ApiErrorBody {
  error: string;
  detail?: string;
  hint?: string;
}
