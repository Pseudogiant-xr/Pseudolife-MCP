// App-wide reactive state: route, theme, token modal, refresh tick and the
// shared daemon resources (health, overview, board) that several views read.

import { untrack } from "svelte";
import { api, ApiError, setUnauthorizedHandler } from "./api/client";
import { settleConfirm, toast } from "./overlay.svelte";
import { settlePick } from "./reviewRunner.svelte";
import type { BoardSnapshot, Health, Overview } from "./api/types";
import { DEFAULT_ROUTE, resolveRoute } from "./nav";
import { readKey, THEME_KEY, TOKEN_KEY, writeKey } from "./storage";

export type Theme = "dark" | "light";

export type Query = Record<string, string>;

/** "#/graph?entity=daemon&scope=all" -> route "graph", query {entity, scope}. */
function parseHash(hash = location.hash): { route: string; query: Query } {
  const raw = hash.replace(/^#\/?/, "");
  const q = raw.indexOf("?");
  const path = q === -1 ? raw : raw.slice(0, q);
  const id = path.split("/")[0] ?? "";
  const query: Query = {};
  if (q !== -1) {
    for (const [k, v] of new URLSearchParams(raw.slice(q + 1))) query[k] = v;
  }
  return { route: resolveRoute(id || DEFAULT_ROUTE), query };
}

/** The hash for a route and its query; empty values are left out. */
export function hrefTo(route: string, query: Record<string, string | number | boolean | null | undefined> = {}): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined || v === "" || v === false) continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return `#/${route}${qs ? `?${qs}` : ""}`;
}

const initial = parseHash();

export const ui = $state({
  route: initial.route,
  /** The current route's query string, e.g. {entity: "daemon"} on #/graph?entity=daemon. */
  query: initial.query as Query,
  theme: (readKey(THEME_KEY) === "light" ? "light" : "dark") as Theme,
  hasToken: readKey(TOKEN_KEY) !== "",
  tokenOpen: false,
  drawerOpen: false,
  /** Bumped by every manual refresh; views reload what they show. */
  tick: 0,
});

/** A view may put a live line under the topbar title (e.g. the board's peer
 *  count). Keyed by route so a stale line never shows on another view. */
export const page = $state({ route: "", subtitle: "" });

export function setSubtitle(route: string, subtitle: string): void {
  page.route = route;
  page.subtitle = subtitle;
}

export function startRouter(): () => void {
  const onHash = () => {
    const next = parseHash();
    if (next.route !== ui.route) {
      window.scrollTo(0, 0);
      // A question asked by the view being left must not outlive it (Back
      // during a delete confirm would otherwise still run the delete).
      settleConfirm(false);
      settlePick(null);
    }
    ui.route = next.route;
    ui.query = next.query;
    ui.drawerOpen = false;
  };
  window.addEventListener("hashchange", onHash);
  return () => window.removeEventListener("hashchange", onHash);
}

export function navigate(id: string, query: Record<string, string | number | boolean | null | undefined> = {}): void {
  location.hash = hrefTo(id, query);
}

/**
 * Rewrite the current route's query in the address bar without a navigation
 * (no hashchange, no history entry), for view state worth bookmarking such as
 * a filter or a selected entity.
 */
export function setQuery(query: Record<string, string | number | boolean | null | undefined>): void {
  const href = hrefTo(ui.route, query);
  history.replaceState(history.state, "", href);
  ui.query = parseHash(href).query;
}

export function applyTheme(theme: Theme): void {
  ui.theme = theme;
  document.documentElement.setAttribute("data-theme", theme);
  writeKey(THEME_KEY, theme);
}

export function toggleTheme(): void {
  applyTheme(ui.theme === "dark" ? "light" : "dark");
}

export function saveToken(token: string): void {
  writeKey(TOKEN_KEY, token.trim());
  ui.hasToken = token.trim() !== "";
  refresh();
}

export function refresh(): void {
  ui.tick += 1;
}

// A 401 "unauthorized" means the stored token is missing or wrong: open the
// token dialog, once per burst (a view load fires several requests at once).
let unauthorizedAt = 0;
setUnauthorizedHandler(() => {
  const now = Date.now();
  if (now - unauthorizedAt < 15_000 || ui.tokenOpen) return;
  unauthorizedAt = now;
  ui.tokenOpen = true;
  toast("The daemon did not accept this console's token. Store the one it was started with.", "warn", 6000);
});

// ---- shared resources ------------------------------------------------------

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** Local time (ms) the last successful answer arrived. */
  at: number;
}

function resource<T>(): Resource<T> {
  return { data: null, error: null, loading: false, at: 0 };
}

export const store = $state({
  health: resource<Health>(),
  overview: resource<Overview>(),
  board: resource<BoardSnapshot>(),
});

function asApiError(e: unknown): ApiError {
  return e instanceof ApiError ? e : new ApiError(0, "client_error", null);
}

/** Loaders are called from effects: untracked, so the state they write
 *  (loading, data) never re-triggers the effect that started them. */
function load<T>(r: Resource<T>, fetcher: () => Promise<T>): Promise<void> {
  return untrack(() => run(r, fetcher));
}

// A load requested while one is in flight runs once more when it finishes,
// so a refresh after a new token (or after a dream) is never dropped in
// favour of an answer to the old request.
const again = new WeakSet<object>();

async function run<T>(r: Resource<T>, fetcher: () => Promise<T>): Promise<void> {
  if (r.loading) {
    again.add(r);
    return;
  }
  r.loading = true;
  try {
    r.data = await fetcher();
    r.error = null;
    r.at = Date.now();
  } catch (e) {
    r.error = asApiError(e);
    // Keep the last good data only for transient failures; an auth refusal
    // must not leave a previous token's view on screen.
    if (r.error.status === 401 || r.error.status === 403) r.data = null;
  } finally {
    r.loading = false;
  }
  if (again.has(r)) {
    again.delete(r);
    await run(r, fetcher);
  }
}

export const loadHealth = () => load(store.health, api.health);
export const loadOverview = () => load(store.overview, api.overview);
/** GET only: refreshing the board never sends, receives or acks mail. */
export const loadBoard = () => load(store.board, () => api.board(50));
