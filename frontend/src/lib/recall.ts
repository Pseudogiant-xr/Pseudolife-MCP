// Pure logic for the Recall view: address-bar state, the seed cap, the
// entity split, and reading a path answer into a chain of steps.

import type { PathEdge, PathResponse, RecallEntity, RecallResponse } from "./api/recall";

export type RecallMode = "hops" | "path";

export interface RecallState {
  mode: RecallMode;
  q: string;
  hops: number;
  source: string;
  target: string;
}

export const DEFAULT_HOPS = 3;
export const SEED_CAP = 15;

export function clampHops(raw: unknown): number {
  const n = typeof raw === "number" ? raw : Number.parseInt(String(raw ?? ""), 10);
  if (!Number.isFinite(n)) return DEFAULT_HOPS;
  return Math.max(1, Math.min(5, Math.round(n)));
}

/** #/recall?q=&hops= or #/recall?mode=path&source=&target= */
export function readRecallQuery(query: Record<string, string>): RecallState {
  return {
    mode: query.mode === "path" ? "path" : "hops",
    q: (query.q ?? "").trim(),
    hops: query.hops ? clampHops(query.hops) : DEFAULT_HOPS,
    source: (query.source ?? "").trim(),
    target: (query.target ?? "").trim(),
  };
}

/** The address-bar params for a state; only the active mode's inputs, defaults left out. */
export function recallQueryOf(s: RecallState): Record<string, string> {
  if (s.mode === "path") {
    const out: Record<string, string> = { mode: "path" };
    if (s.source) out.source = s.source;
    if (s.target) out.target = s.target;
    return out;
  }
  const out: Record<string, string> = {};
  if (s.q) out.q = s.q;
  if (s.hops !== DEFAULT_HOPS) out.hops = String(s.hops);
  return out;
}

/** The first `cap` seeds, and how many more stay behind "+N more" until expanded. */
export function seedView(seeds: string[] | undefined, expanded: boolean, cap = SEED_CAP): { shown: string[]; more: number } {
  const all = seeds ?? [];
  if (expanded || all.length <= cap) return { shown: all, more: 0 };
  return { shown: all.slice(0, cap), more: all.length - cap };
}

/** Entities with canonical facts get a full section; the rest are compact links. */
export function splitEntities(entities: RecallEntity[] | undefined): { withFacts: RecallEntity[]; bare: RecallEntity[] } {
  const withFacts: RecallEntity[] = [];
  const bare: RecallEntity[] = [];
  for (const e of entities ?? []) ((e.facts ?? []).length ? withFacts : bare).push(e);
  return { withFacts, bare };
}

export function recallIsEmpty(r: RecallResponse): boolean {
  return !(r.seeds ?? []).length && !(r.entities ?? []).length && !(r.texts ?? []).length;
}

export type Step = { kind: "node"; name: string } | { kind: "edge"; relation: string; forward: boolean };

/**
 * The path as alternating nodes and relations. graph_path reports each edge
 * in its stored direction, which can run against the walk (B -rel-> A on a
 * walk from A to B), so the edge is aligned to the path and marked backward
 * rather than printed in an order that breaks the chain. Without a usable
 * `path` the edges are chained as served.
 */
export function pathSteps(path: string[] | undefined, edges: PathEdge[]): Step[] {
  if (!edges.length) return [];
  if (path && path.length === edges.length + 1) {
    const steps: Step[] = [{ kind: "node", name: path[0] }];
    edges.forEach((e, i) => {
      const forward = !(e.src === path[i + 1] && e.dst === path[i]);
      steps.push({ kind: "edge", relation: e.relation, forward });
      steps.push({ kind: "node", name: path[i + 1] });
    });
    return steps;
  }
  const steps: Step[] = [{ kind: "node", name: edges[0].src }];
  for (const e of edges) {
    steps.push({ kind: "edge", relation: e.relation, forward: true });
    steps.push({ kind: "node", name: e.dst });
  }
  return steps;
}

export type PathOutcome =
  | { kind: "refused"; message: string }
  | { kind: "missing"; name: string }
  | { kind: "none" }
  | { kind: "found"; hops: number; steps: Step[] };

export function readPath(r: PathResponse): PathOutcome {
  if (r.error) return { kind: "refused", message: r.hint || r.error };
  if (r.found === false) return { kind: "missing", name: r.missing ?? "" };
  const edges = r.edges ?? [];
  if (!edges.length || r.hops === null || r.hops === undefined) return { kind: "none" };
  return { kind: "found", hops: r.hops, steps: pathSteps(r.path, edges) };
}
