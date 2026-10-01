// The sidebar's routes. Only Observatory and Board are native in this phase;
// every other item opens the classic console at /ui/#/<classic id>.

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
  /** Native views render here; the rest link to the classic console. */
  native: boolean;
  /** Classic console hash id, for non-native items. */
  classic?: string;
  countKey?: keyof Counts;
  /** One line under the title in the topbar. */
  subtitle: string;
}

export const NAV: NavItem[] = [
  { id: "observatory", label: "Observatory", group: "Overview", icon: "observatory", native: true, subtitle: "How the bank is doing right now" },
  { id: "cortex", label: "Cortex", group: "Memory", icon: "cortex", native: false, classic: "cortex", countKey: "facts", subtitle: "" },
  { id: "world", label: "World", group: "Memory", icon: "world", native: false, classic: "world", countKey: "world", subtitle: "" },
  { id: "lessons", label: "Lessons", group: "Memory", icon: "lessons", native: false, classic: "lessons", countKey: "lessons", subtitle: "" },
  { id: "stream", label: "Stream", group: "Memory", icon: "stream", native: false, classic: "stream", countKey: "entries", subtitle: "" },
  { id: "recall", label: "Recall", group: "Memory", icon: "recall", native: false, classic: "recall", subtitle: "" },
  { id: "graph", label: "Graph", group: "Structure", icon: "graph", native: false, classic: "graph", subtitle: "" },
  { id: "review", label: "Review", group: "Structure", icon: "review", native: false, classic: "graph", subtitle: "" },
  { id: "insight", label: "Insight", group: "Structure", icon: "insight", native: false, classic: "insight", subtitle: "" },
  { id: "board", label: "Board", group: "Operations", icon: "board", native: true, subtitle: "Who is working on what, and what they hold" },
  { id: "episodes", label: "Episodes", group: "Operations", icon: "episodes", native: false, classic: "episodes", countKey: "episodes", subtitle: "" },
  { id: "settings", label: "Settings", group: "Operations", icon: "settings", native: false, classic: "console", subtitle: "" },
];

export const GROUPS: readonly NavGroup[] = ["Overview", "Memory", "Structure", "Operations"];

export const DEFAULT_ROUTE = "observatory";

export function classicHref(classicId: string): string {
  return `/ui/#/${classicId}`;
}

export function hrefFor(item: NavItem): string {
  return item.native ? `#/${item.id}` : classicHref(item.classic ?? item.id);
}

export function findNav(id: string): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}

/** Digit shortcuts, as in the classic console: 1-9, then 0 for the tenth. */
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
