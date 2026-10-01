// App-wide reactive state: route, theme, token modal, refresh tick and the
// shared daemon resources (health, overview, board) that several views read.

import { untrack } from "svelte";
import { api, ApiError } from "./api/client";
import type { BoardSnapshot, Health, Overview } from "./api/types";
import { DEFAULT_ROUTE, findNav } from "./nav";
import { readKey, THEME_KEY, TOKEN_KEY, writeKey } from "./storage";

export type Theme = "dark" | "light";

function routeFromHash(): string {
  const id = location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] ?? "";
  return id || DEFAULT_ROUTE;
}

export const ui = $state({
  route: routeFromHash(),
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
    ui.route = routeFromHash();
    ui.drawerOpen = false;
  };
  window.addEventListener("hashchange", onHash);
  return () => window.removeEventListener("hashchange", onHash);
}

export function navigate(id: string): void {
  const item = findNav(id);
  if (item && !item.native) {
    location.href = `/ui/#/${item.classic ?? item.id}`;
    return;
  }
  location.hash = `#/${id}`;
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

async function run<T>(r: Resource<T>, fetcher: () => Promise<T>): Promise<void> {
  if (r.loading) return;
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
}

export const loadHealth = () => load(store.health, api.health);
export const loadOverview = () => load(store.overview, api.overview);
/** GET only: refreshing the board never sends, receives or acks mail. */
export const loadBoard = () => load(store.board, () => api.board(50));
