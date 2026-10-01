// Board helpers shared by the Observatory and the Board view.

import type { BoardAgent, BoardEvent, BoardSnapshot, Lease } from "./api/types";
import { shortId, words } from "./format";

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

export function agentName(a: BoardAgent): string {
  return a.label || shortId(a.agent_id);
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
  if (l.expired) return "expired";
  if (!l.holder) return l.queued ? "free, queued" : "free";
  if (l.stale) return "past expected end";
  return "held";
}

/** Resolve an agent id to a label from the roster, else a short id. */
export function nameResolver(s: BoardSnapshot | null): (id: string | null | undefined) => string {
  const byId = new Map((s?.agents ?? []).map((a) => [a.agent_id, agentName(a)]));
  for (const l of s?.leases ?? []) {
    if (l.holder?.label && !byId.has(l.holder.agent_id)) byId.set(l.holder.agent_id, l.holder.label);
    for (const w of l.queue) if (w.label && !byId.has(w.agent_id)) byId.set(w.agent_id, w.label);
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
