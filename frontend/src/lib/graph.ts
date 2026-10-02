// Pure logic for the Graph view: degree and size, orphans, search matching,
// isolate neighbourhoods, scope facets, review flags (which stars pulse, which
// banners an entity page shows and what each banner's buttons do), the time
// scrubber's range, and the view's address-bar state. No DOM, no engine.

import type {
  GraphEdge,
  GraphNode,
  GraphResponse,
  JunkRow,
  Project,
  ReviewFinding,
  WikiFlag,
} from "./api/graph";
import type { ReviewAction } from "./reviewActions";

// ---- degree, size, orphans -------------------------------------------------

/** Connections per entity name: each edge counts once at each end. */
export function degreeMap(edges: readonly Pick<GraphEdge, "src" | "dst">[]): Map<string, number> {
  const deg = new Map<string, number>();
  for (const e of edges) {
    deg.set(e.src, (deg.get(e.src) ?? 0) + 1);
    deg.set(e.dst, (deg.get(e.dst) ?? 0) + 1);
  }
  return deg;
}

/** Star size (the engine's nodeVal): connections dominate, facts add weight. */
export function nodeSize(degree: number, facts: number): number {
  return 1 + degree + facts * 0.6;
}

/** Entities with no connection at all: the sparse halo around the galaxy. */
export function orphanCount(nodes: readonly Pick<GraphNode, "entity">[], deg: Map<string, number>): number {
  let n = 0;
  for (const node of nodes) if (!deg.get(node.entity)) n += 1;
  return n;
}

// ---- search ----------------------------------------------------------------

/**
 * The star Enter flies to: the most connected entity whose name contains the
 * query (case-insensitive). Ties keep the input order. Null when none match.
 */
export function bestMatch(names: readonly string[], deg: Map<string, number>, query: string): string | null {
  const q = query.trim().toLowerCase();
  if (!q) return null;
  let best: string | null = null;
  let bestDeg = -1;
  for (const name of names) {
    if (!name.toLowerCase().includes(q)) continue;
    const d = deg.get(name) ?? 0;
    if (d > bestDeg) {
      best = name;
      bestDeg = d;
    }
  }
  return best;
}

/** Whether a name matches the live search highlight. */
export function matchesQuery(name: string, query: string): boolean {
  const q = query.trim().toLowerCase();
  return !q || name.toLowerCase().includes(q);
}

// ---- isolate ---------------------------------------------------------------

/**
 * The names within `depth` hops of `name`, edges taken as undirected. Null
 * when the name is neither a node nor an edge endpoint.
 */
export function neighborhood(
  names: readonly string[],
  edges: readonly { src: string; dst: string }[],
  name: string,
  depth = 2,
): Set<string> | null {
  const adj = new Map<string, string[]>();
  const add = (a: string, b: string) => {
    const list = adj.get(a);
    if (list) list.push(b);
    else adj.set(a, [b]);
  };
  for (const e of edges) {
    add(e.src, e.dst);
    add(e.dst, e.src);
  }
  if (!adj.has(name) && !names.includes(name)) return null;
  const keep = new Set([name]);
  let frontier = [name];
  for (let d = 0; d < depth; d++) {
    const next: string[] = [];
    for (const cur of frontier) {
      for (const nb of adj.get(cur) ?? []) {
        if (!keep.has(nb)) {
          keep.add(nb);
          next.push(nb);
        }
      }
    }
    frontier = next;
  }
  return keep;
}

// ---- scope facets ------------------------------------------------------------

export interface ScopeOption {
  value: string;
  label: string;
  /** A child of an umbrella scope (rendered indented). */
  child: boolean;
}

/**
 * Scope choices: "All projects" first, then each umbrella scope (API order,
 * entities descending) followed by its children, then children whose
 * umbrella has no row of its own.
 */
export function scopeOptions(projects: readonly Project[]): ScopeOption[] {
  const tops = projects.filter((p) => !p.parent);
  const kids = projects.filter((p) => p.parent);
  const topNames = new Set(tops.map((t) => t.source));
  const ordered = tops
    .flatMap((t) => [t, ...kids.filter((k) => k.parent === t.source)])
    .concat(kids.filter((k) => !topNames.has(k.parent ?? "")));
  return [
    { value: "all", label: "All projects", child: false },
    ...ordered.map((p) => ({
      value: p.source,
      label: `${p.parent ? "↳ " : ""}${p.source} (${p.entities})`,
      child: !!p.parent,
    })),
  ];
}

// ---- review flags ------------------------------------------------------------

/**
 * Only item-level decisions make a star pulse. Bulk hygiene lists (orphan,
 * unattributed, dubious_edge, test_artifact) run to a thousand names on a
 * live bank and would light the whole galaxy; they stay in the Review view.
 */
export const PULSE_TYPES: ReadonlySet<string> = new Set(["duplicate", "merge_candidate", "junk_candidate", "proposed_link"]);

function junkRows(f: ReviewFinding): JunkRow[] {
  return (f.entities ?? []).filter((e): e is JunkRow => typeof e === "object" && e !== null);
}

function nameList(f: ReviewFinding): string[] {
  return (f.entities ?? []).filter((e): e is string => typeof e === "string");
}

/** Every entity name an item-level finding touches. */
export function flaggedNames(findings: readonly ReviewFinding[] | null | undefined): Set<string> {
  const out = new Set<string>();
  const add = (v: unknown) => {
    if (typeof v === "string" && v) out.add(v);
  };
  for (const f of findings ?? []) {
    if (!PULSE_TYPES.has(f.type)) continue;
    for (const e of nameList(f)) add(e);
    for (const e of junkRows(f)) add(e.entity);
    for (const m of f.merges ?? []) {
      add(m.from);
      add(m.into);
    }
    for (const l of f.links ?? []) {
      add(l.src);
      add(l.dst);
    }
  }
  return out;
}

/**
 * Item-level decisions waiting in the review scan (each duplicate pair, merge,
 * junk row and proposed link counts once). Null when there is no scan.
 */
export function pendingDecisions(findings: readonly ReviewFinding[] | null | undefined): number | null {
  if (!findings) return null;
  let n = 0;
  for (const f of findings) {
    if (f.type === "duplicate") n += 1;
    else if (f.type === "merge_candidate") n += (f.merges ?? []).length;
    else if (f.type === "junk_candidate") n += junkRows(f).length;
    else if (f.type === "proposed_link") n += (f.links ?? []).length;
  }
  return n;
}

/**
 * The findings that touch one entity, shaped like wiki flags, so a pulsing
 * star's page always says why it pulses (duplicates never reach the wiki
 * payload; only the review scan knows them).
 */
export function extraFlagsFor(findings: readonly ReviewFinding[] | null | undefined, name: string): WikiFlag[] {
  const out: WikiFlag[] = [];
  for (const f of findings ?? []) {
    if (f.type === "duplicate") {
      const [a, b] = nameList(f);
      if (a && b && (a === name || b === name)) out.push({ kind: "duplicate", a, b, other: a === name ? b : a });
    } else if (f.type === "merge_candidate") {
      for (const m of f.merges ?? []) {
        if (m.from === name || m.into === name) out.push({ kind: "merge_candidate", id: m.id, entity: m.from, into: m.into });
      }
    } else if (f.type === "junk_candidate") {
      for (const e of junkRows(f)) {
        if (e.entity === name) out.push({ kind: "junk_candidate", id: e.id, entity: e.entity });
      }
    } else if (f.type === "proposed_link") {
      for (const l of f.links ?? []) {
        if (l.src === name || l.dst === name) {
          out.push({ kind: "proposed_link", id: l.id, src: l.src, relation: l.relation, dst: l.dst });
        }
      }
    }
  }
  return out;
}

/**
 * The live daemon's /api/wiki names entity proposals by their table kind
 * ("merge", "junk"); the review scan and the fixtures say "merge_candidate"
 * and "junk_candidate". One vocabulary from here on.
 */
export function normalizeFlag(f: WikiFlag): WikiFlag {
  if (f.kind === "merge") return { ...f, kind: "merge_candidate" };
  if (f.kind === "junk") return { ...f, kind: "junk_candidate" };
  return f;
}

function flagKey(f: WikiFlag): string {
  return [f.kind, f.id ?? "", f.a ?? "", f.b ?? "", f.src ?? "", f.dst ?? ""].join("\u0000");
}

/**
 * Server flags plus the review scan's, deduplicated by kind and identity.
 * When both know a proposal, the scan's copy wins: its merge direction is the
 * one an accept applies (the wiki payload does not re-derive it).
 */
export function mergeFlags(server: readonly WikiFlag[] | null | undefined, extra: readonly WikiFlag[] | null | undefined): WikiFlag[] {
  const seen = new Set<string>();
  const out: WikiFlag[] = [];
  for (const raw of [...(extra ?? []), ...(server ?? [])]) {
    const f = normalizeFlag(raw);
    const k = flagKey(f);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(f);
  }
  return out;
}

export interface FlagButton {
  label: string;
  action: ReviewAction;
  danger?: boolean;
}

export interface FlagView {
  text: string;
  buttons: FlagButton[];
}

/** What a flag banner says and which review decisions its buttons run. */
export function flagView(f: WikiFlag, entity: string): FlagView {
  switch (f.kind) {
    case "duplicate":
      if (!f.a || !f.b) break;
      return {
        text: `Possible duplicate of “${f.other ?? (f.a === entity ? f.b : f.a)}”`,
        buttons: [
          { label: "Merge", action: { kind: "merge-named", a: f.a, b: f.b } },
          { label: "Mark distinct", action: { kind: "dismiss-duplicate", a: f.a, b: f.b } },
        ],
      };
    case "unattributed":
      return {
        text: "No project owns this entity",
        buttons: [{ label: "Assign a project", action: { kind: "assign", entities: [entity] } }],
      };
    case "proposed_link":
      if (f.id === undefined) break;
      return {
        text: `Proposed link: ${f.src ?? "?"} ${f.relation ?? "?"} ${f.dst ?? "?"}`,
        buttons: [
          { label: "Accept the link", action: { kind: "accept-link", id: f.id } },
          { label: "Reject", action: { kind: "reject-link", id: f.id } },
        ],
      };
    case "merge_candidate": {
      if (f.id === undefined) break;
      const from = f.entity ?? entity;
      const into = f.into ?? "?";
      return {
        text: `Suggested merge: ${from} → ${into}`,
        buttons: [
          { label: "Merge", action: { kind: "merge-entity", id: f.id, from, into } },
          { label: "Reject", action: { kind: "reject-entity", id: f.id } },
        ],
      };
    }
    case "junk_candidate": {
      if (f.id === undefined) break;
      const name = f.entity ?? entity;
      return {
        text: `Suspected junk: ${name}`,
        buttons: [
          { label: "Delete the entity", action: { kind: "junk-entity", id: f.id, entity: name }, danger: true },
          { label: "Reject", action: { kind: "reject-entity", id: f.id } },
        ],
      };
    }
  }
  const tail = [f.entity, f.into].filter(Boolean).join(" → ");
  return { text: tail ? `${f.kind}: ${tail}` : f.kind, buttons: [] };
}

// ---- time scrubber -------------------------------------------------------------

export interface TimeSpan {
  t0: number;
  t1: number;
}

/**
 * The bank's growth window: oldest to newest entity creation or edge
 * assertion. Null when there are fewer than two timestamps or no spread,
 * in which case the scrubber is not shown.
 */
export function timeSpan(
  nodes: readonly Pick<GraphNode, "created_at">[],
  edges: readonly Pick<GraphEdge, "asserted_at">[],
): TimeSpan | null {
  let t0 = Infinity;
  let t1 = -Infinity;
  let n = 0;
  const see = (t: number | null | undefined) => {
    if (!t) return;
    n += 1;
    if (t < t0) t0 = t;
    if (t > t1) t1 = t;
  };
  for (const x of nodes) see(x.created_at);
  for (const e of edges) see(e.asserted_at);
  return n >= 2 && t1 > t0 ? { t0, t1 } : null;
}

export const SCRUB_MAX = 1000;

/** Slider position (0..1000) to a time cut; the right end means "now" (null). */
export function sliderCut(v: number, span: TimeSpan): number | null {
  if (v >= SCRUB_MAX) return null;
  return span.t0 + (span.t1 - span.t0) * (Math.max(0, v) / SCRUB_MAX);
}

// ---- table ---------------------------------------------------------------------

/** The table's rows: optional orphan hiding, then the search filter. */
export function tableRows(
  data: Pick<GraphResponse, "nodes" | "edges">,
  deg: Map<string, number>,
  opts: { hideOrphans: boolean; query: string },
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  let nodes = data.nodes ?? [];
  let edges = data.edges ?? [];
  if (opts.hideOrphans) nodes = nodes.filter((n) => deg.get(n.entity));
  const q = opts.query.trim().toLowerCase();
  if (q) {
    nodes = nodes.filter((n) => n.entity.toLowerCase().includes(q));
    edges = edges.filter(
      (e) => e.src.toLowerCase().includes(q) || e.dst.toLowerCase().includes(q) || e.relation.toLowerCase().includes(q),
    );
  }
  return { nodes, edges };
}

/** "Showing the N most connected of TOTAL entities", or null when not truncated. */
export function truncationNotice(data: Pick<GraphResponse, "truncated" | "nodes" | "total_nodes">, scope: string): string | null {
  if (!data.truncated) return null;
  const shown = (data.nodes ?? []).length;
  const total = data.total_nodes ?? shown;
  const fmt = (n: number) => n.toLocaleString("en-US");
  const head = `Showing the ${fmt(shown)} most connected of ${fmt(total)} entities`;
  return scope === "all" ? `${head}; pick a project to map it in full.` : `${head}.`;
}

// ---- address bar -----------------------------------------------------------------

export type GraphMode = "galaxy" | "table";

export interface GraphQueryState {
  entity: string;
  scope: string;
  view: GraphMode;
}

/**
 * The classic parameters: `entity`, `scope`, `view=table`. A missing scope or
 * view keeps the current one; an explicit entity without `view=table` forces
 * the galaxy, because the link means "show me this entity".
 */
export function parseGraphQuery(query: Record<string, string>, current: { scope: string; view: GraphMode }): GraphQueryState {
  const entity = query.entity ?? "";
  const scope = query.scope || current.scope || "all";
  const view: GraphMode = query.view === "table" ? "table" : entity ? "galaxy" : current.view;
  return { entity, scope, view };
}

/** The query to reflect: only non-default values are written. */
export function graphQuery(s: GraphQueryState): Record<string, string> {
  const out: Record<string, string> = {};
  if (s.entity) out.entity = s.entity;
  if (s.scope && s.scope !== "all") out.scope = s.scope;
  if (s.view === "table") out.view = "table";
  return out;
}

// ---- relation kind ---------------------------------------------------------------

/**
 * How a relation is known: derived by a rule, or its provenance tag
 * (extracted = a person or a confirming action said so; inferred = an agent's
 * extraction; ambiguous = low confidence or still a proposal).
 */
export function edgeKind(e: Pick<GraphEdge, "derived" | "tag">): { label: string; tone: "" | "ok" | "warn" } {
  if (e.derived) return { label: "derived", tone: "" };
  const t = (e.tag ?? "").toLowerCase();
  if (t === "extracted") return { label: "extracted", tone: "ok" };
  if (t === "ambiguous") return { label: "ambiguous", tone: "warn" };
  if (t === "inferred") return { label: "inferred", tone: "" };
  return { label: t || "explicit", tone: t ? "" : "ok" };
}
