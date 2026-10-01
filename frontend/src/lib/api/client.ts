// Fetch client for the daemon's REST surface: bearer token from localStorage
// "pl_token", JSON in and out, the daemon's {"error": code} bodies surfaced
// as ApiError. Views add their own typed calls on top of get() and post().

import { readKey, TOKEN_KEY } from "../storage";
import type {
  ApiErrorBody,
  BoardSnapshot,
  DreamRunResult,
  Health,
  Overview,
  RecentResponse,
} from "./types";

export class ApiError extends Error {
  /** HTTP status; 0 when the daemon could not be reached at all. */
  readonly status: number;
  /** The daemon's error code ("unauthorized", "authentication_required", ...). */
  readonly code: string;
  readonly body: Partial<ApiErrorBody> | null;

  constructor(status: number, code: string, body: Partial<ApiErrorBody> | null) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

export type Params = Record<string, string | number | boolean | string[] | null | undefined>;

function buildUrl(path: string, params?: Params): string {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v === null || v === undefined || v === "") continue;
    if (Array.isArray(v)) {
      if (v.length) url.searchParams.set(k, v.join(","));
      continue;
    }
    url.searchParams.set(k, String(v));
  }
  return url.pathname + url.search;
}

async function parse(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { error: "invalid_response", detail: text.slice(0, 200) };
  }
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  opts: { params?: Params; body?: unknown; acceptStatus?: number[] } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = readKey(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.params), {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (e) {
    // fetch() rejects a header value outside Latin-1 before sending anything:
    // that is a token pasted with a stray character, not a down daemon.
    if (e instanceof TypeError && token && /[^\u0000-ÿ]/.test(token)) {
      throw new ApiError(0, "token_not_latin1", null);
    }
    throw new ApiError(0, "network", null);
  }
  const data = await parse(res);
  if (!res.ok && !(opts.acceptStatus ?? []).includes(res.status)) {
    const body = (data && typeof data === "object" ? data : null) as Partial<ApiErrorBody> | null;
    const code = typeof body?.error === "string" ? body.error : `http_${res.status}`;
    throw new ApiError(res.status, code, body);
  }
  return (data ?? {}) as T;
}

/** GET a JSON route. Empty and null params are dropped; arrays join with commas. */
export function get<T>(path: string, params?: Params): Promise<T> {
  return request<T>("GET", path, { params });
}

/** POST a JSON object. The daemon requires an object body on every POST. */
export function post<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  return request<T>("POST", path, { body });
}

/**
 * Several write routes answer HTTP 200 with {"error": "..."} (a refusal the
 * daemon considers normal, such as bulk_confirm_required). Returns that code,
 * or null when the answer carries none. Callers must check it before
 * reporting success.
 */
export function softError(r: unknown): string | null {
  if (r && typeof r === "object" && "error" in r) {
    const e = (r as { error: unknown }).error;
    if (typeof e === "string" && e) return e;
  }
  return null;
}

export const api = {
  /** /health answers 503 with the same JSON when degraded; that is data, not an error. */
  health: () => request<Health>("GET", "/health", { acceptStatus: [503] }),
  overview: () => get<Overview>("/api/overview"),
  recent: (n: number, source?: string) => get<RecentResponse>("/api/recent", { n, source }),
  /** Read-only board metadata. Never sends, receives or acknowledges mail. */
  board: (limit = 50) => get<BoardSnapshot>("/api/agents", { view: "coordination", limit }),
  // An empty body lets the daemon use its configured memory.dream.max_batch.
  dreamRun: () => post<DreamRunResult>("/api/dream/run", {}),
};
