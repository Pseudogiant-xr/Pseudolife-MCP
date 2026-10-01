// Pure logic behind the Cortex view: filtering, grouping rows into entities
// and slots, the facet counts, and reading the daemon's answers to writes
// before anything reports success. No DOM, no state; unit tested in
// cortex.test.ts.

import { softError } from "./api/client";
import type { Fact, FactHistory, ForgetResult, ResolveResult, WriteResult } from "./api/facts";
import { fmtNum, plural, slotKey } from "./format";

export type OriginFilter = "all" | "user" | "action" | "agent";
export type Facet = "all" | "contested" | "stale" | "reverify";

export interface FactFilter {
  q: string;
  origin: OriginFilter;
  facet: Facet;
}

/** The provenance tier a row filters under; anything unset reads as agent. */
export function originOf(f: Pick<Fact, "origin">): string {
  return String(f.origin || "agent").toLowerCase();
}

/** Case-insensitive match over "entity attribute value", as the classic console did. */
export function matchesText(f: Pick<Fact, "entity" | "attribute" | "value">, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return `${f.entity} ${f.attribute} ${f.value}`.toLowerCase().includes(needle);
}

export function matchesFacet(f: Fact, facet: Facet): boolean {
  switch (facet) {
    case "contested":
      return !!f.contested;
    case "stale":
      return f.stale === true;
    case "reverify":
      return !!f.re_verify;
    default:
      return true;
  }
}

export function filterFacts(entries: Fact[], filter: FactFilter): Fact[] {
  return entries.filter(
    (f) =>
      (filter.origin === "all" || originOf(f) === filter.origin) &&
      matchesFacet(f, filter.facet) &&
      matchesText(f, filter.q),
  );
}

/** Number of distinct slots among the rows that pass `pred`. A set slot
 *  arrives as one row per member, each carrying the slot's flags, so rows
 *  over-count. */
export function countSlots(entries: Fact[], pred: (f: Fact) => boolean = () => true): number {
  const keys = new Set<string>();
  for (const f of entries) if (pred(f)) keys.add(slotKey(f.entity, f.attribute));
  return keys.size;
}

export interface FacetCounts {
  slots: number;
  contested: number;
  /** null when no row carries a `stale` field (the daemon did not serve it). */
  stale: number | null;
  reverify: number;
}

export function facetCounts(entries: Fact[]): FacetCounts {
  const staleServed = entries.some((f) => typeof f.stale === "boolean");
  return {
    slots: countSlots(entries),
    contested: countSlots(entries, (f) => !!f.contested),
    stale: staleServed ? countSlots(entries, (f) => f.stale === true) : null,
    reverify: countSlots(entries, (f) => !!f.re_verify),
  };
}

/** One (entity, attribute) slot: a scalar holds one row, a set one row per member. */
export interface Slot {
  key: string;
  entity: string;
  /** The first row's spelling; member rows may spell the attribute differently. */
  attribute: string;
  rows: Fact[];
  isSet: boolean;
  contested: boolean;
  /** The parked rival, read once per slot from its first contested row. */
  contender: { value: string; origin: string } | null;
  stale: boolean;
  reVerify: boolean;
}

/**
 * Collapse rows into slots keyed by slotKey, in order of first appearance.
 * The contender is parked against the slot, not a member, so it is taken
 * once per slot: a view renders one contender block per Slot.
 */
export function groupSlots(rows: Fact[]): Slot[] {
  const byKey = new Map<string, Slot>();
  for (const f of rows) {
    const key = slotKey(f.entity, f.attribute);
    let s = byKey.get(key);
    if (!s) {
      s = {
        key,
        entity: f.entity,
        attribute: f.attribute,
        rows: [],
        isSet: false,
        contested: false,
        contender: null,
        stale: false,
        reVerify: false,
      };
      byKey.set(key, s);
    }
    s.rows.push(f);
    if (f.kind === "member" || s.rows.length > 1) s.isSet = true;
    if (f.contested) {
      s.contested = true;
      if (!s.contender && f.contender_value !== undefined && f.contender_value !== null) {
        s.contender = { value: String(f.contender_value), origin: f.contender_origin || "agent" };
      }
    }
    if (f.stale === true) s.stale = true;
    if (f.re_verify) s.reVerify = true;
  }
  return [...byKey.values()];
}

export interface EntityGroup {
  entity: string;
  /** Rows sorted by attribute. */
  facts: Fact[];
  slots: Slot[];
  contested: number;
  sets: number;
  /** Newest epoch any row was confirmed or written, or null when none is served. */
  latest: number | null;
}

/** When a row was last confirmed, else written, else asserted. */
export function rowTime(f: Pick<Fact, "last_confirmed" | "tx_time" | "asserted_at">): number | null {
  return f.last_confirmed ?? f.tx_time ?? f.asserted_at ?? null;
}

/** Rows grouped by entity, entities sorted by name, rows by attribute (localeCompare). */
export function groupByEntity(entries: Fact[]): EntityGroup[] {
  const m = new Map<string, Fact[]>();
  for (const f of entries) {
    const list = m.get(f.entity);
    if (list) list.push(f);
    else m.set(f.entity, [f]);
  }
  return [...m.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([entity, rows]) => {
      const facts = rows.slice().sort((x, y) => String(x.attribute).localeCompare(String(y.attribute)));
      const slots = groupSlots(facts);
      let latest: number | null = null;
      for (const f of facts) {
        const t = rowTime(f);
        if (t !== null && (latest === null || t > latest)) latest = t;
      }
      return {
        entity,
        facts,
        slots,
        contested: slots.filter((s) => s.contested).length,
        sets: slots.filter((s) => s.isSet).length,
        latest,
      };
    });
}

/**
 * The toolbar's count line: "12 facts of 40, first 1 000 of 1 412 loaded".
 * `shown` passed the filters, `fetched` arrived, `total` exist on the daemon.
 */
export function countNote(
  shown: number,
  fetched: number,
  total: number | null | undefined,
  truncated: boolean,
  noun: [string, string],
): string {
  let s = plural(shown, noun[0], noun[1]);
  if (shown !== fetched) s += ` of ${fmtNum(fetched)}`;
  if (truncated && typeof total === "number") s += `, first ${fmtNum(fetched)} of ${fmtNum(total)} loaded`;
  return s;
}

// ---- reading write answers ---------------------------------------------------

export interface Outcome {
  ok: boolean;
  message: string;
  tone: "ok" | "warn" | "danger";
  /** The view on screen is out of date: reload it. */
  reload: boolean;
}

/**
 * POST /api/facts/resolve answers HTTP 200 either way; `{resolved:false,
 * reason}` is a refusal and must never toast success.
 */
export function interpretResolve(r: ResolveResult | null | undefined, accept: boolean): Outcome {
  const err = softError(r);
  if (err) return { ok: false, message: `Not resolved: ${err}`, tone: "danger", reload: false };
  if (r && r.resolved === true) {
    return { ok: true, message: accept ? "Contender adopted" : "Contender discarded", tone: "ok", reload: true };
  }
  const reason = r?.reason;
  if (reason === "slot_holds_set") {
    // A scalar contender cannot replace a member set.
    return {
      ok: false,
      message: accept
        ? "Not adopted: the slot now holds a set. Add the value as a member instead, or discard the contender."
        : "Not discarded: the slot holds a set (slot_holds_set).",
      tone: "danger",
      reload: false,
    };
  }
  if (reason === "no_contender") {
    // Another client already settled the slot; the block on screen is stale.
    return {
      ok: false,
      message: "Nothing to resolve: the contender was already settled elsewhere. The facts were reloaded.",
      tone: "warn",
      reload: true,
    };
  }
  return { ok: false, message: `Not resolved: ${reason || "unknown reason"}`, tone: "danger", reload: false };
}

/** POST /api/facts/forget: `removed: 0` means the slot was already gone. */
export function interpretForget(r: ForgetResult | null | undefined): Outcome {
  const err = softError(r);
  if (err) return { ok: false, message: `Not forgotten: ${err}`, tone: "danger", reload: false };
  if (typeof r?.removed === "number" && r.removed > 0) {
    return { ok: true, message: "Fact forgotten", tone: "ok", reload: true };
  }
  if (r?.removed === 0) {
    return {
      ok: false,
      message: "Nothing was forgotten: the fact was already gone. The facts were reloaded.",
      tone: "warn",
      reload: true,
    };
  }
  return { ok: false, message: "Not forgotten: the daemon gave no count of removed records.", tone: "danger", reload: true };
}

/** POST /api/facts/set: a weaker-tier value against a stronger one is parked, not written. */
export function interpretSet(r: WriteResult | null | undefined): Outcome {
  const err = softError(r);
  if (err) return { ok: false, message: `Not asserted: ${err}`, tone: "danger", reload: false };
  switch (r?.action) {
    case "contested":
      return {
        ok: true,
        message: "Parked as a contender: a stronger value holds the slot",
        tone: "warn",
        reload: true,
      };
    case "confirmed":
      return { ok: true, message: "Fact confirmed", tone: "ok", reload: true };
    case "superseded":
      return { ok: true, message: "Fact asserted; the old value is kept as history", tone: "ok", reload: true };
    default:
      return { ok: true, message: "Fact asserted", tone: "ok", reload: true };
  }
}

// ---- history ladder ------------------------------------------------------------

export interface Rung {
  value: string;
  /** "current", "superseded", "contested", "added", "removed", ... */
  label: string;
  current: boolean;
  by: string | null;
  /** Server-rendered age when served, else null (the view derives one from `ts`). */
  age: string | null;
  ts: number | null;
}

/**
 * The versions newest first. The daemon sends them oldest first with its own
 * tie-break, so the list is reversed rather than re-sorted. A set slot sends
 * membership events instead; a member is current when its newest event is
 * an add.
 */
export function historyLadder(h: FactHistory | null | undefined): Rung[] {
  if (!h || !Array.isArray(h.versions)) return [];
  if (h.kind === "set") {
    const lastIndex = new Map<string, number>();
    h.versions.forEach((v, i) => lastIndex.set(v.value, i));
    return h.versions
      .map((v, i) => ({
        value: v.value,
        label: v.event,
        current: v.event === "added" && lastIndex.get(v.value) === i,
        by: null,
        age: null,
        ts: v.at ?? null,
      }))
      .reverse();
  }
  return h.versions
    .map((v) => ({
      value: v.value,
      label: v.status || "unknown",
      current: v.status === "current",
      by: v.writer_id || null,
      age: v.age || null,
      ts: v.tx_time ?? v.asserted_at ?? null,
    }))
    .reverse();
}

/** Distinct origins across a slot's rows, in first-seen order. */
export function slotOrigins(s: Slot): string[] {
  return [...new Set(s.rows.map((f) => f.origin || "agent"))];
}

/** The confidence span across a slot's rows, or null when none is served. */
export function confidenceSpan(s: Slot): { min: number; max: number } | null {
  const c = s.rows.map((f) => f.confidence).filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  if (!c.length) return null;
  return { min: Math.min(...c), max: Math.max(...c) };
}

/** The row whose time is newest, for a slot's age column. */
export function newestRow(s: Slot): Fact {
  let best = s.rows[0];
  let bestT = rowTime(best) ?? -Infinity;
  for (const f of s.rows) {
    const t = rowTime(f) ?? -Infinity;
    if (t > bestT) {
      best = f;
      bestT = t;
    }
  }
  return best;
}
