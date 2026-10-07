// The sidebar's routes. Each id is a hash route (#/cortex). ALIASES maps the
// classic console's hash ids onto these, so an old bookmark still lands.

import type { Counts } from "./api/types";

export type IconName =
  | "observatory"
  | "cortex"
  | "world"
  | "lessons"
  | "stream"
  | "recall"
  | "graph"
  | "review"
  | "insight"
  | "board"
  | "episodes"
  | "settings";

export type NavGroup = "Overview" | "Memory" | "Structure" | "Operations";

export interface NavItem {
  id: string;
  label: string;
  group: NavGroup;
  icon: IconName;
  countKey?: keyof Counts;
  /** One line under the title in the topbar. */
  subtitle: string;
}

export const NAV: NavItem[] = [
  { id: "observatory", label: "Observatory", group: "Overview", icon: "observatory", subtitle: "How the bank is doing right now" },
  { id: "cortex", label: "Cortex", group: "Memory", icon: "cortex", countKey: "facts", subtitle: "Canonical facts, one current value per slot" },
  { id: "world", label: "World", group: "Memory", icon: "world", countKey: "world", subtitle: "Cited facts about the outside world" },
  { id: "lessons", label: "Lessons", group: "Memory", icon: "lessons", countKey: "lessons", subtitle: "What worked and what to avoid" },
  { id: "stream", label: "Stream", group: "Memory", icon: "stream", countKey: "entries", subtitle: "Search and browse the associative store" },
  { id: "recall", label: "Recall", group: "Memory", icon: "recall", subtitle: "Walk the graph from a question" },
  { id: "graph", label: "Graph", group: "Structure", icon: "graph", subtitle: "Entities and how they relate" },
  { id: "review", label: "Review", group: "Structure", icon: "review", subtitle: "Graph proposals waiting for a decision" },
  { id: "insight", label: "Insight", group: "Structure", icon: "insight", subtitle: "Hubs, communities and open questions" },
  { id: "board", label: "Board", group: "Operations", icon: "board", subtitle: "Who is working on what, and what they hold" },
  { id: "episodes", label: "Episodes", group: "Operations", icon: "episodes", countKey: "episodes", subtitle: "Working sessions, newest first" },
  { id: "settings", label: "Settings", group: "Operations", icon: "settings", subtitle: "The daemon's live configuration" },
  { id: "re-evidence", label: "RE Evidence", group: "Operations", icon: "recall", subtitle: "Immutable artifacts and evidence-linked claims, scoped by build" },
];

/** Classic console hash ids that map onto a route here. */
export const ALIASES: Record<string, string> = {
  atlas: "graph",
  coordination: "board",
  console: "settings",
};

export const GROUPS: readonly NavGroup[] = ["Overview", "Memory", "Structure", "Operations"];

export const DEFAULT_ROUTE = "observatory";

export function resolveRoute(id: string): string {
  return ALIASES[id] ?? id;
}

export function hrefFor(item: NavItem): string {
  return `#/${item.id}`;
}

export function findNav(id: string): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}

/** Digit shortcuts: 1-9, then 0 for the tenth item. */
export function navForDigit(key: string): NavItem | undefined {
  if (!/^[0-9]$/.test(key)) return undefined;
  const n = key === "0" ? 10 : Number.parseInt(key, 10);
  return n <= NAV.length ? NAV[n - 1] : undefined;
}

/** Topbar jump: exact label or id, then prefix, then substring match. */
export function matchNav(query: string): NavItem | undefined {
  const q = query.trim().toLowerCase();
  if (!q) return undefined;
  return (
    NAV.find((n) => n.label.toLowerCase() === q || n.id === q) ??
    NAV.find((n) => n.label.toLowerCase().startsWith(q)) ??
    NAV.find((n) => n.label.toLowerCase().includes(q))
  );
}
