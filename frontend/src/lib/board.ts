// Board helpers shared by the Observatory and the Board view.

import type { BoardAgent, BoardChild, BoardEvent, BoardSnapshot, Lease } from "./api/types";
import { fmtDuration, fmtNum, shortId, words } from "./format";

export type Tone = "ok" | "warn" | "danger" | "canon" | "";

/** A park stands while it has a reason and `park_expires` is not past
 *  `now` (the snapshot time), matching the server's `_live_park`. The row
 *  keeps the raw reason after expiry, so an expired park is history. */
export function isParked(a: BoardAgent, now?: number | null): boolean {
  if (a.park_reason === null || a.park_reason === undefined || a.park_reason === "") return false;
  if (a.park_expires && now && a.park_expires <= now) return false;
  return true;
}

export function parkExpired(a: BoardAgent, now?: number | null): boolean {
  return !!a.park_reason && !!a.park_expires && !!now && a.park_expires <= now;
}

/** Dot color: overdue red, parked orange, otherwise green. */
export function agentTone(a: BoardAgent, now?: number | null): Tone {
  if (a.status_overdue) return "danger";
  if (isParked(a, now)) return "warn";
  return "ok";
}

/** The session's name (the harness's title, the agent's own, or its session
 *  title), else the registration label, else a short id. */
export function agentName(a: BoardAgent): string {
  return a.name || a.label || shortId(a.agent_id);
}

/** Whether every word of `query` appears, ignoring case, in one of the
 *  fields a person finds a session by: its name and label, its full id (so
 *  the 8-character short id matches too), project, task and status. */
export function matchesQuery(a: BoardAgent, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return true;
  const hay = [agentName(a), a.label, a.agent_id, a.project, a.task, a.status].join("\n").toLowerCase();
  return terms.every((t) => hay.includes(t));
}

export type Pin = "delegate" | "coordinator" | "mail";
const PIN_ORDER: Pin[] = ["delegate", "coordinator", "mail"];

/** The roster with the sessions the maintainer looks for first pinned to the
 *  top: the delegate, the coordinator, then any with unread mail. Everything
 *  else keeps the server's order (reachable first, most recently active). */
export function orderRoster(
  agents: BoardAgent[],
  role: (a: BoardAgent) => "delegate" | "coordinator" | null,
): { agent: BoardAgent; pin: Pin | null }[] {
  const rows = agents.map((agent) => ({
    agent,
    pin: (role(agent) ?? (agent.pending_count ? "mail" : null)) as Pin | null,
  }));
  const rank = (p: Pin | null) => (p ? PIN_ORDER.indexOf(p) : PIN_ORDER.length);
  // Array.prototype.sort is stable, so ties keep the server's order.
  return rows.sort((x, y) => rank(x.pin) - rank(y.pin));
}

export function stateChip(a: BoardAgent, now?: number | null): { text: string; tone: Tone } {
  if (a.status_overdue) return { text: "overdue", tone: "danger" };
  if (isParked(a, now)) return { text: `parked, ${words(a.park_reason)}`, tone: "warn" };
  if (parkExpired(a, now)) return { text: "park expired", tone: "" };
  return { text: words(a.lifecycle) || "unknown", tone: a.lifecycle === "attached" ? "ok" : "" };
}

export function scopeLine(a: BoardAgent): string {
  return [a.project, a.task].filter(Boolean).join(", ");
}

export interface BoardSummary {
  peers: number;
  parked: number;
  overdue: number;
  attached: number;
  /** Unread mail across the caller's own peers; null when none is countable. */
  unread: number | null;
  idle: number;
}

export function summarize(s: BoardSnapshot | null): BoardSummary | null {
  if (!s || !s.available || !s.agents) return null;
  const agents = s.agents;
  const counted = agents.filter((a) => a.pending_count !== null);
  return {
    peers: agents.length,
    parked: agents.filter((a) => isParked(a, s.snapshot_at)).length,
    overdue: agents.filter((a) => a.status_overdue).length,
    attached: agents.filter((a) => a.lifecycle === "attached").length,
    unread: counted.length ? counted.reduce((n, a) => n + (a.pending_count ?? 0), 0) : null,
    idle: s.idle_omitted ?? 0,
  };
}

export function leaseTone(l: Lease): Tone {
  if (l.expired) return "danger";
  if (!l.holder) return "";
  if (l.stale) return "warn";
  return l.name.startsWith("claim:") ? "canon" : "warn";
}

export function leaseState(l: Lease): string {
  // The snapshot never settles leases, so an expired hold stays listed until
  // the next lease call or prune pass hands it on.
  if (l.expired) return "expired, awaiting settlement";
  if (!l.holder) return l.queued ? "free, queued" : "free";
  if (l.stale) return "past expected end";
  return "held";
}

/** Resolve an agent id to a label from the roster, else a short id. */
export function nameResolver(s: BoardSnapshot | null): (id: string | null | undefined) => string {
  const byId = new Map((s?.agents ?? []).map((a) => [a.agent_id, agentName(a)]));
  for (const l of s?.leases ?? []) {
    const holder = l.holder?.name || l.holder?.label;
    if (l.holder && holder && !byId.has(l.holder.agent_id)) byId.set(l.holder.agent_id, holder);
    for (const w of l.queue) {
      const waiter = w.name || w.label;
      if (waiter && !byId.has(w.agent_id)) byId.set(w.agent_id, waiter);
    }
  }
  return (id) => (id ? (byId.get(id) ?? shortId(id)) : "someone");
}

/** The event as a sentence, with agent names resolved. */
export function eventSentence(e: BoardEvent, name: (id: string | null) => string): string {
  const who = name(e.agent_id);
  const to = e.recipient_agent_id ? name(e.recipient_agent_id) : "";
  switch (e.event) {
    case "send":
      return to ? `${who} sent mail to ${to}` : `${who} sent mail`;
    case "read":
      return `${who} read mail`;
    case "ack":
      return `${who} acknowledged mail`;
    case "attempt":
      return to ? `The daemon tried to deliver mail to ${to}` : `The daemon tried a delivery for ${who}`;
    case "served":
      return `A wake was served to ${to || who}`;
    case "woke":
      return `${to || who} was woken`;
    case "lease_expire":
      return `A lease held by ${who} expired`;
    case "expire":
      return e.expired_count === 1 ? "One unread message expired" : `${e.expired_count} unread messages expired`;
    default:
      return `${words(e.event)} by ${who}`;
  }
}

/**
 * "Expected by" for a peer's status. Overdue is the server's call
 * (`status_overdue`, decided on the daemon's clock), never the browser's;
 * the distance is measured against the snapshot time for the same reason.
 */
export function expectedBy(a: BoardAgent, snapshotAt?: number | null): { text: string; overdue: boolean } {
  const due = a.status_expires_at;
  if (due === null || due === undefined) return { text: "not set", overdue: false };
  const delta = snapshotAt ? due - snapshotAt : null;
  if (a.status_overdue) {
    return { text: delta !== null && delta < 0 ? `overdue by ${fmtDuration(-delta)}` : "overdue", overdue: true };
  }
  return { text: delta !== null && delta > 0 ? `in ${fmtDuration(delta)}` : "due now", overdue: false };
}

/** Whether the peer has a live adapter that can take mail now (served since v50). */
export function adapterText(a: BoardAgent): string | null {
  const v = (a as BoardAgent & { adapter_available?: boolean }).adapter_available;
  if (v === undefined || v === null) return null;
  return v ? "Live adapter available" : "No live adapter";
}

/** Unread mail: counted only for the caller's own principal's peers. */
export function pendingText(a: BoardAgent): string {
  if (a.pending_count === null || a.pending_count === undefined) return "Not visible to this token";
  return a.pending_count === 0 ? "None unread" : `${fmtNum(a.pending_count)} unread`;
}

/** A child entry with an agent id was listed by the SubagentStart hook; one without, by its parent. */
export function childSource(c: BoardChild): string {
  return c.agent_id ? "hook reported" : "parent reported";
}

/** Waiters counted in `queued` but past the first ten the snapshot lists. */
export function waitersOmitted(l: Lease): number {
  return Math.max(0, (l.queued ?? 0) - (l.queue?.length ?? 0));
}

/** Whether a scroll pane is at (or within `slack` px of) its end: a message
 *  thread follows new messages only then, and stays put while the reader
 *  has scrolled up to read history. */
export function nearBottom(
  m: { scrollTop: number; clientHeight: number; scrollHeight: number },
  slack = 32,
): boolean {
  return m.scrollHeight - m.scrollTop - m.clientHeight <= slack;
}
