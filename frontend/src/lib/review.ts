// Pure logic for the Review view: grouping and counting findings by type,
// the wording of each automation state, bulk selection over a filtered list,
// and the mapping from a finding row to the ReviewAction descriptors that
// lib/reviewRunner.svelte.ts runs. No DOM, no fetches: unit tested.

import type {
  Automation,
  AutomationState,
  Chain,
  DubiousEdge,
  Finding,
  JunkItem,
  Judge,
  LinkItem,
  MergeItem,
  Project,
  Provenance,
  ReviewResponse,
  SlotPair,
} from "./api/review";
import { fmtDecimal, fmtNum, words } from "./format";
import type { ReviewAction } from "./reviewActions";

// ---- types and filters ---------------------------------------------------------

export type TypeFilter = "all" | "merges" | "links" | "junk" | "edges" | "unattributed" | "test_artifact" | "orphan";

export const TYPE_FILTERS: { value: TypeFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "merges", label: "Merges" },
  { value: "links", label: "Links" },
  { value: "junk", label: "Junk" },
  { value: "edges", label: "Edges" },
  { value: "unattributed", label: "Unattributed" },
  { value: "test_artifact", label: "Test artifacts" },
  { value: "orphan", label: "Orphans" },
];

/** Which filter a finding type belongs to. Unknown types show under All only. */
const GROUP_OF: Record<string, TypeFilter> = {
  merge_candidate: "merges",
  duplicate: "merges",
  proposed_link: "links",
  junk_candidate: "junk",
  dubious_edge: "edges",
  unattributed: "unattributed",
  test_artifact: "test_artifact",
  orphan: "orphan",
};

export function parseTypeFilter(v: string | undefined): TypeFilter {
  return TYPE_FILTERS.some((f) => f.value === v) ? (v as TypeFilter) : "all";
}

/** A short, human name for a finding type. */
export const TYPE_LABEL: Record<string, string> = {
  merge_candidate: "Merge proposals",
  duplicate: "Possible duplicate",
  proposed_link: "Proposed links",
  junk_candidate: "Junk proposals",
  dubious_edge: "Dubious edges",
  unattributed: "No project",
  test_artifact: "Test artifacts",
  orphan: "Weakly connected",
};

export function typeLabel(type: string): string {
  return TYPE_LABEL[type] ?? words(type);
}

/** The finding types whose rows are picked with checkboxes and acted on in bulk. */
export const BULK_TYPES = new Set(["dubious_edge", "unattributed", "test_artifact", "orphan"]);

export function isBulk(f: Finding): boolean {
  return BULK_TYPES.has(f.type);
}

/** Entity names of a finding's `entities` list (junk items carry objects). */
export function entityNames(f: Finding): string[] {
  return (f.entities ?? [])
    .map((e) => (typeof e === "string" ? e : e?.entity))
    .filter((n): n is string => typeof n === "string" && n !== "");
}

export function junkItems(f: Finding): JunkItem[] {
  return (f.entities ?? []).filter((e): e is JunkItem => typeof e === "object" && e !== null);
}

/** How many decisions a finding holds: one per proposal, name or edge. */
export function itemCount(f: Finding): number {
  switch (f.type) {
    case "merge_candidate":
      return f.merges?.length ?? 0;
    case "proposed_link":
      return f.links?.length ?? 0;
    case "junk_candidate":
      return junkItems(f).length;
    case "dubious_edge":
      return f.edges?.length ?? 0;
    case "duplicate":
      return entityNames(f).length >= 2 ? 1 : 0;
    case "unattributed":
    case "test_artifact":
    case "orphan":
      return entityNames(f).length;
    default:
      return 1;
  }
}

export function matchesFilter(f: Finding, filter: TypeFilter): boolean {
  return filter === "all" || GROUP_OF[f.type] === filter;
}

export function filterFindings(findings: Finding[], filter: TypeFilter): Finding[] {
  return findings.filter((f) => matchesFilter(f, filter));
}

/** Items per filter, for the type filter's counts. */
export function countByFilter(findings: Finding[]): Record<TypeFilter, number> {
  const out = Object.fromEntries(TYPE_FILTERS.map((f) => [f.value, 0])) as Record<TypeFilter, number>;
  for (const f of findings) {
    const n = itemCount(f);
    out.all += n;
    const g = GROUP_OF[f.type];
    if (g) out[g] += n;
  }
  return out;
}

// ---- automation -------------------------------------------------------------------

export const AUTOMATION_STATES: AutomationState[] = ["unfiled", "pending", "gated", "manual", "terminal"];

interface StateWords {
  label: string;
  /** One line under the count in the summary strip. */
  sub: string;
  tone: "warn" | "";
  /** Nothing automated will settle it: a person decides. */
  needsPerson: boolean;
}

const STATE_WORDS: Record<AutomationState, StateWords> = {
  unfiled: { label: "Waiting to be queued", sub: "Found by the analyzer, not filed yet", tone: "", needsPerson: false },
  pending: { label: "Queued", sub: "Filed for review", tone: "", needsPerson: false },
  gated: { label: "Automation paused", sub: "Needs a person", tone: "warn", needsPerson: true },
  manual: { label: "Manual review", sub: "No automated path", tone: "", needsPerson: true },
  terminal: { label: "Already decided", sub: "A past decision stops refiling", tone: "", needsPerson: false },
};

export interface AutomationText {
  state: string;
  label: string;
  reason: string;
  tone: "warn" | "";
  needsPerson: boolean;
}

/** The automation badge for a finding, or null when the daemon sent none. */
export function automationText(a: Automation | null | undefined): AutomationText | null {
  if (!a || !a.state) return null;
  const w = STATE_WORDS[a.state as AutomationState];
  return {
    state: a.state,
    label: w?.label ?? words(a.state),
    reason: (a.reason ?? "").trim(),
    tone: w?.tone ?? "",
    needsPerson: w?.needsPerson ?? false,
  };
}

export function stateWords(s: AutomationState): StateWords {
  return STATE_WORDS[s];
}

/**
 * Findings per automation state: the daemon's own tally when it sent one,
 * else counted from the findings that carry a state. Null when neither is
 * available (an older daemon, or the fixtures), so the strip says
 * "unavailable" instead of showing zeros.
 */
export function automationCounts(r: ReviewResponse | null): Record<AutomationState, number> | null {
  if (!r) return null;
  const empty = () => Object.fromEntries(AUTOMATION_STATES.map((s) => [s, 0])) as Record<AutomationState, number>;
  if (r.automation && typeof r.automation === "object") {
    const out = empty();
    for (const s of AUTOMATION_STATES) out[s] = Number(r.automation[s] ?? 0) || 0;
    return out;
  }
  const findings = r.findings ?? [];
  if (!findings.some((f) => f.automation?.state)) return null;
  const out = empty();
  for (const f of findings) {
    const s = f.automation?.state as AutomationState | undefined;
    if (s && s in out) out[s] += 1;
  }
  return out;
}

/** Findings that a person has to settle (automation paused or no automated path). */
export function needsPersonCount(counts: Record<AutomationState, number> | null): number | null {
  return counts ? counts.gated + counts.manual : null;
}

// ---- bulk selection over a filtered list --------------------------------------------

export interface BulkRow {
  key: string;
  /** What the filter box matches against. */
  text: string;
  /** Entity names the row is about (the evidence panel loads these). */
  entities: string[];
  edge?: DubiousEdge;
  name?: string;
}

export function edgeKey(e: DubiousEdge): string {
  return `${e.src}\u0000${e.relation}\u0000${e.dst}`;
}

export function bulkRows(f: Finding): BulkRow[] {
  if (f.type === "dubious_edge") {
    return (f.edges ?? []).map((e) => ({
      key: edgeKey(e),
      text: `${e.src} ${e.relation} ${e.dst}`,
      entities: [e.src, e.dst],
      edge: e,
    }));
  }
  // Names are unique within one finding; keep the first if a daemon repeats one.
  const seen = new Set<string>();
  const rows: BulkRow[] = [];
  for (const n of entityNames(f)) {
    if (seen.has(n)) continue;
    seen.add(n);
    rows.push({ key: n, text: n, entities: [n], name: n });
  }
  return rows;
}

/** Case-insensitive substring match; an empty query matches everything. */
export function rowMatches(row: BulkRow, query: string): boolean {
  const q = query.trim().toLowerCase();
  return !q || row.text.toLowerCase().includes(q);
}

export function visibleRows(rows: BulkRow[], query: string): BulkRow[] {
  return rows.filter((r) => rowMatches(r, query));
}

/** Add every visible row to the selection; rows the filter hides are untouched. */
export function selectVisible(selected: Iterable<string>, rows: BulkRow[], query: string): string[] {
  const out = new Set(selected);
  for (const r of visibleRows(rows, query)) out.add(r.key);
  return [...out];
}

export function toggleKey(selected: Iterable<string>, key: string, on: boolean): string[] {
  const out = new Set(selected);
  if (on) out.add(key);
  else out.delete(key);
  return [...out];
}

/** Drop selected keys that no longer exist (after a reload). */
export function pruneSelection(selected: Iterable<string>, rows: BulkRow[]): string[] {
  const present = new Set(rows.map((r) => r.key));
  return [...selected].filter((k) => present.has(k));
}

export function selectedRows(rows: BulkRow[], selected: Iterable<string>): BulkRow[] {
  const s = new Set(selected);
  return rows.filter((r) => s.has(r.key));
}

// ---- mapping rows to ReviewAction descriptors -----------------------------------------

export type ButtonTone = "primary" | "secondary" | "danger" | "ghost";

export interface ActionButton {
  /** Stable id for in-flight tracking. */
  id: string;
  label: string;
  tone: ButtonTone;
  action: ReviewAction;
  /** Disabled with nothing selected (bulk buttons). */
  disabled?: boolean;
}

const nOf = (n: number, one: string, many: string) => `${fmtNum(n)} ${n === 1 ? one : many}`;

/** The bulk buttons for a hygiene finding, given the rows the person picked. */
export function bulkActions(f: Finding, picked: BulkRow[]): ActionButton[] {
  const n = picked.length;
  const names = picked.map((r) => r.name).filter((x): x is string => !!x);
  const edges = picked.map((r) => r.edge).filter((x): x is DubiousEdge => !!x);
  const ents = nOf(n, "entity", "entities");
  const disabled = n === 0;
  switch (f.type) {
    case "dubious_edge":
      return [
        { id: "bless", label: n ? `Keep ${nOf(n, "edge", "edges")}` : "Keep edges", tone: "secondary", action: { kind: "bless", edges }, disabled },
        { id: "prune", label: n ? `Remove ${nOf(n, "edge", "edges")}` : "Remove edges", tone: "danger", action: { kind: "prune", edges }, disabled },
      ];
    case "unattributed":
      return [{ id: "assign", label: n ? `Assign ${ents} to a project` : "Assign to a project", tone: "secondary", action: { kind: "assign", entities: names }, disabled }];
    case "test_artifact":
      return [{ id: "delete", label: n ? `Delete ${ents}` : "Delete entities", tone: "danger", action: { kind: "delete-names", entities: names }, disabled }];
    case "orphan":
      return [
        { id: "assign", label: n ? `Assign ${ents} to a project` : "Assign to a project", tone: "secondary", action: { kind: "assign", entities: names }, disabled },
        { id: "delete", label: n ? `Delete ${ents}` : "Delete entities", tone: "danger", action: { kind: "delete-names", entities: names }, disabled },
      ];
    default:
      return [];
  }
}

export function mergeActions(m: MergeItem): ActionButton[] {
  return [
    { id: `merge:${m.id}`, label: "Merge", tone: "primary", action: { kind: "merge-entity", id: m.id, from: m.from, into: m.into } },
    { id: `reject-entity:${m.id}`, label: "Reject", tone: "secondary", action: { kind: "reject-entity", id: m.id } },
  ];
}

export function junkActions(j: JunkItem): ActionButton[] {
  return [
    { id: `junk:${j.id}`, label: "Delete the entity", tone: "danger", action: { kind: "junk-entity", id: j.id, entity: j.entity } },
    { id: `reject-entity:${j.id}`, label: "Reject", tone: "secondary", action: { kind: "reject-entity", id: j.id } },
  ];
}

export function linkActions(l: LinkItem): ActionButton[] {
  return [
    { id: `accept-link:${l.id}`, label: "Accept", tone: "primary", action: { kind: "accept-link", id: l.id } },
    { id: `reject-link:${l.id}`, label: "Reject", tone: "secondary", action: { kind: "reject-link", id: l.id } },
  ];
}

/**
 * A duplicate pair by name. A file/concept pair (action "relate") offers the
 * relation first, with the analyzer's suggestion (`implements` by default):
 * the first name is the file, the second the concept.
 */
export function duplicateActions(f: Finding): ActionButton[] {
  const [a, b] = entityNames(f);
  if (!a || !b) return [];
  const key = `${a}\u0000${b}`;
  const out: ActionButton[] = [];
  if (f.action === "relate") {
    const relation = f.suggested_relation || "implements";
    out.push({ id: `relate:${key}`, label: `Relate (${relation})`, tone: "primary", action: { kind: "relate-named", src: a, relation, dst: b } });
  }
  out.push({ id: `merge-named:${key}`, label: "Merge", tone: f.action === "relate" ? "secondary" : "primary", action: { kind: "merge-named", a, b } });
  out.push({ id: `dismiss:${key}`, label: "Mark distinct", tone: "ghost", action: { kind: "dismiss-duplicate", a, b } });
  return out;
}

export function slotPairActions(store: "lesson" | "world", p: SlotPair): ActionButton[] {
  return [
    {
      id: `slot:${store}:${p.a_key ?? `${p.a.entity}|${p.a.attribute}`}:${p.b_key ?? `${p.b.entity}|${p.b.attribute}`}`,
      label: "Mark distinct",
      tone: "ghost",
      action: {
        kind: "dismiss-slot-pair",
        store,
        a: { entity: p.a.entity, attribute: p.a.attribute },
        b: { entity: p.b.entity, attribute: p.b.attribute },
      },
    },
  ];
}

// ---- small presenters ------------------------------------------------------------------

/** The judge's opinion as a chip: verdict, the relation for "relate", and confidence. */
export function judgeChip(j: Judge | null | undefined): { text: string; tone: "ok" | "danger" | "lessons" } | null {
  if (!j || !j.verdict) return null;
  const verdict = j.verdict === "relate" && j.relation ? `relate ${j.relation}` : j.verdict;
  const conf = j.confidence === null || j.confidence === undefined ? "" : ` ${fmtDecimal(Number(j.confidence))}`;
  const tone = j.verdict === "accept" ? "ok" : j.verdict === "reject" || j.verdict === "relate" ? "danger" : "lessons";
  return { text: `${verdict}${conf}`, tone };
}

/** How many merge rows of a finding share this row's group (including it). */
export function groupSize(f: Finding, group: string | null | undefined): number {
  if (!group) return 0;
  return (f.merges ?? []).filter((m) => m.group === group).length;
}

/** Lesson polarity: "+" is a thing to do, "-" a thing to avoid. */
export function polarityWord(p: string | null | undefined): string | null {
  if (p === "+") return "do";
  if (p === "-") return "avoid";
  return null;
}

/** "a / b" for a decision pair, else the proposal id. */
export function decisionSubject(d: { pair?: string[] | null; proposal_id?: number | string | null }): string {
  if (d.pair && d.pair.length) return d.pair.join(" / ");
  if (d.proposal_id !== null && d.proposal_id !== undefined) return `proposal ${d.proposal_id}`;
  return "";
}

// ---- scope picker -----------------------------------------------------------------------

export interface ScopeOption {
  value: string;
  label: string;
  /** A child of an umbrella project (memory.scopes.rollup), shown indented. */
  child: boolean;
}

/**
 * "All projects", then each top-level project followed by the children that
 * roll up into it, then children whose umbrella has no entities of its own.
 * Labels carry the entity count. A scope from the address bar that the daemon
 * no longer lists is kept, so the picker still shows what is in effect.
 */
export function scopeOptions(projects: Project[], current = "all"): ScopeOption[] {
  const label = (p: Project) => `${p.source} (${fmtNum(p.entities)})`;
  const known = new Set(projects.map((p) => p.source));
  const kids = new Map<string, Project[]>();
  const tops: Project[] = [];
  const strays: Project[] = [];
  for (const p of projects) {
    if (p.parent && p.parent !== p.source) {
      if (known.has(p.parent)) kids.set(p.parent, [...(kids.get(p.parent) ?? []), p]);
      else strays.push(p);
    } else tops.push(p);
  }
  const out: ScopeOption[] = [{ value: "all", label: "All projects", child: false }];
  for (const t of tops) {
    out.push({ value: t.source, label: label(t), child: false });
    for (const k of kids.get(t.source) ?? []) out.push({ value: k.source, label: label(k), child: true });
  }
  for (const s of strays) out.push({ value: s.source, label: label(s), child: true });
  if (current !== "all" && !known.has(current)) out.push({ value: current, label: current, child: false });
  return out;
}

// ---- evidence ---------------------------------------------------------------------------

/** The row the evidence panel describes. Only fields the daemon served. */
export interface EvidenceSubject {
  key: string;
  /** A short sentence naming the row, e.g. "Merge live daemon into daemon". */
  title: string;
  entities: string[];
  finding: Finding;
  merge?: MergeItem;
  junk?: JunkItem;
  link?: LinkItem;
  edge?: DubiousEdge;
}

/** One entity's lazily loaded provenance and causal chain. */
export interface EntityEvidence {
  loading: boolean;
  prov: Provenance | null;
  provError: boolean;
  chain: Chain | null;
  chainError: boolean;
}

export function mergeSubject(f: Finding, m: MergeItem): EvidenceSubject {
  return { key: `merge:${m.id}`, title: `Merge “${m.from}” into “${m.into}”`, entities: [m.from, m.into], finding: f, merge: m };
}

export function junkSubject(f: Finding, j: JunkItem): EvidenceSubject {
  return { key: `junk:${j.id}`, title: `Delete “${j.entity}” as junk`, entities: [j.entity], finding: f, junk: j };
}

export function linkSubject(f: Finding, l: LinkItem): EvidenceSubject {
  return { key: `link:${l.id}`, title: `Link “${l.src}” ${l.relation} “${l.dst}”`, entities: [l.src, l.dst], finding: f, link: l };
}

export function duplicateSubject(f: Finding): EvidenceSubject | null {
  const [a, b] = entityNames(f);
  if (!a || !b) return null;
  return { key: `dup:${a}\u0000${b}`, title: `Is “${a}” the same thing as “${b}”?`, entities: [a, b], finding: f };
}

export function bulkSubject(f: Finding, r: BulkRow): EvidenceSubject {
  if (r.edge) {
    const e = r.edge;
    return { key: `${f.type}:${r.key}`, title: `Edge “${e.src}” ${e.relation} “${e.dst}”`, entities: r.entities, finding: f, edge: e };
  }
  return { key: `${f.type}:${r.key}`, title: `“${r.name}”: ${typeLabel(f.type).toLowerCase()}`, entities: r.entities, finding: f };
}

/**
 * Findings with a unique key each, for keyed lists. The daemon sends at most
 * one finding of each bulk type, so those key by type alone and a bulk list
 * (with its selection) stays mounted across reloads; anything else, or a
 * repeated type, keys by position too.
 */
export function keyFindings(findings: Finding[]): { key: string; finding: Finding }[] {
  const seen = new Set<string>();
  return findings.map((finding, i) => {
    let key = BULK_TYPES.has(finding.type) ? finding.type : `${finding.type}:${entityNames(finding).join("\u0000")}`;
    if (seen.has(key)) key = `${key}#${i}`;
    seen.add(key);
    return { key, finding };
  });
}
