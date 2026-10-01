// Fetch client for the daemon's REST surface. Same conventions as the classic
// console (static/js/api.js): bearer token from localStorage "pl_token",
// JSON in and out, the daemon's {"error": code} bodies surfaced as ApiError.

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

type Params = Record<string, string | number | boolean | null | undefined>;

function buildUrl(path: string, params?: Params): string {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v === null || v === undefined || v === "") continue;
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
  } catch {
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

export const api = {
  /** /health answers 503 with the same JSON when degraded; that is data, not an error. */
  health: () => request<Health>("GET", "/health", { acceptStatus: [503] }),
  overview: () => request<Overview>("GET", "/api/overview"),
  recent: (n: number) => request<RecentResponse>("GET", "/api/recent", { params: { n } }),
  /** Read-only board metadata. Never sends, receives or acknowledges mail. */
  board: (limit = 50) =>
    request<BoardSnapshot>("GET", "/api/agents", { params: { view: "coordination", limit } }),
  // An empty body lets the daemon use its configured memory.dream.max_batch.
  dreamRun: () => request<DreamRunResult>("POST", "/api/dream/run", { body: {} }),
};
