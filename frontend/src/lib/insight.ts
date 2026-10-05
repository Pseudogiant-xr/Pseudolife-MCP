// Pure helpers for the Insight view: inline-code segments in a suggested
// question, and the community ranking for the bar chart.

import type { Community, GodNode } from "./api/insight";

export interface Segment {
  text: string;
  code: boolean;
}

/**
 * Split a digest question on backticks: odd segments are inline code. The
 * view renders each segment as text (never as HTML), so an entity name with
 * markup in it stays inert. Empty segments are dropped.
 */
export function splitBackticks(s: string | null | undefined): Segment[] {
  return String(s ?? "")
    .split("`")
    .map((text, i) => ({ text, code: i % 2 === 1 }))
    .filter((seg) => seg.text !== "");
}

/** The n largest communities, largest first; equal sizes keep the daemon's order. */
export function topCommunities(comms: Community[], n = 8): Community[] {
  return comms
    .map((c, i) => ({ c, i }))
    .sort((a, b) => (b.c.size ?? 0) - (a.c.size ?? 0) || a.i - b.i)
    .slice(0, n)
    .map((x) => x.c);
}

/** The largest value for a bar scale, never below 1 so bars never divide by zero. */
export function maxOf(values: (number | null | undefined)[]): number {
  return Math.max(1, ...values.map((v) => (typeof v === "number" && Number.isFinite(v) ? v : 0)));
}

export function maxDegree(nodes: GodNode[]): number {
  return maxOf(nodes.map((n) => n.degree));
}

/** Percentage width of a bar, clamped to 0-100. */
export function barPct(v: number | null | undefined, max: number): number {
  if (typeof v !== "number" || !Number.isFinite(v) || max <= 0) return 0;
  return Math.max(0, Math.min(100, (v / max) * 100));
}
