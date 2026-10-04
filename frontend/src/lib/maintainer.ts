// Pure helpers for the maintainer's passkey actions: who holds each role,
// whether this browser can sign right now and why not, the daemon's errors
// in plain words, and the message thread with one agent. The Svelte side
// (dialog state, toasts) is lib/maintainerFlow.svelte.ts.

import { ApiError, softError } from "./api/client";
import type {
  ChallengeAnswer,
  ChallengeBody,
  InboxMessage,
  KeyChange,
  MaintainerStatus,
  Passkey,
  Preview,
  SentMessage,
  Signed,
} from "./api/maintainer";
import type { BoardAgent, Epoch, Lease } from "./api/types";
import { fmtDuration, shortId, words } from "./format";
import { readKey, writeKey } from "./storage";
import { CeremonyError, type AssertionJSON, type Support } from "./webauthn";

// ---- durations ------------------------------------------------------------------

export interface Hold {
  label: string;
  seconds: number;
}

/** The delegate's grant and extend choices (spec section 5). */
export const HOLDS: Hold[] = [
  { label: "1 h", seconds: 3600 },
  { label: "8 h", seconds: 8 * 3600 },
  { label: "24 h", seconds: 24 * 3600 },
  { label: "3 days", seconds: 3 * 86400 },
  { label: "7 days", seconds: 7 * 86400 },
];
export const DEFAULT_HOLD = 24 * 3600;

/** A coordinator holds an ordinary session lease, which lasts at most a day
 *  between renewals (LEASE_TTL_MAX in storage/coordination.py); the holder
 *  renews it from there, so the Console asks no duration. */
export const COORDINATOR_HOLD = 86400;

export const HTTPS_COMMAND = "tailscale serve --https=8443 http://127.0.0.1:8765";
export const ENROL_COMMAND = "pseudolife-mcp maintainer enrol-code";

// ---- roles ------------------------------------------------------------------------

export type RoleKind = "delegate" | "coordinator";

export interface RoleHolder {
  agent_id: string;
  expires_at: Epoch | null;
  /** When the hold began, from the board's lease listing, if it is there. */
  acquired_at: Epoch | null;
  /** "maintainer" or "operator" for a delegate; null when only the lease is known. */
  granted_by: string | null;
  /** The lease listing's label for the holder, a fallback name. */
  label: string | null;
}

export interface ProjectRoleHolders {
  delegate: RoleHolder | null;
  coordinator: RoleHolder | null;
}

export const leaseName = (kind: RoleKind, project: string) => `${kind}:${project}`;

function liveLease(leases: Lease[], name: string): Lease | null {
  const l = leases.find((x) => x.name === name);
  return l && l.holder && !l.expired ? l : null;
}

/**
 * Who holds each role in `project`. The status's `roles` map is the
 * authority when the daemon serves it; without it (an unavailable maintainer
 * surface on an older daemon) the board's `delegate:` / `coordinator:`
 * leases say the same. The lease adds the grant time and a label either way.
 */
export function rolesFor(project: string, status: MaintainerStatus | null, leases: Lease[]): ProjectRoleHolders {
  const out: ProjectRoleHolders = { delegate: null, coordinator: null };
  const served = status?.roles && typeof status.roles === "object" ? status.roles : null;
  for (const kind of ["delegate", "coordinator"] as const) {
    const lease = liveLease(leases, leaseName(kind, project));
    if (served) {
      const r = served[project]?.[kind] ?? null;
      if (!r || !r.agent_id) continue;
      const same = lease?.holder?.agent_id === r.agent_id ? lease : null;
      out[kind] = {
        agent_id: r.agent_id,
        expires_at: r.expires_at ?? same?.expires_at ?? null,
        acquired_at: same?.holder?.acquired_at ?? null,
        granted_by: "granted_by" in r ? String(r.granted_by) : null,
        label: same?.holder?.label || null,
      };
    } else if (lease?.holder) {
      out[kind] = {
        agent_id: lease.holder.agent_id,
        expires_at: lease.expires_at ?? lease.holder.expires_at ?? null,
        acquired_at: lease.holder.acquired_at ?? null,
        granted_by: null,
        label: lease.holder.label || null,
      };
    }
  }
  return out;
}

/** The role an agent holds in its own project, if any. */
export function roleOf(a: BoardAgent, status: MaintainerStatus | null, leases: Lease[]): RoleKind | null {
  if (!a.project) return null;
  const r = rolesFor(a.project, status, leases);
  if (r.delegate?.agent_id === a.agent_id) return "delegate";
  if (r.coordinator?.agent_id === a.agent_id) return "coordinator";
  return null;
}

/** Projects the picker offers: every session's project and every project holding a role. */
export function projectsOf(agents: BoardAgent[], status: MaintainerStatus | null, leases: Lease[]): string[] {
  const set = new Set<string>();
  for (const a of agents) if (a.project) set.add(a.project);
  for (const p of Object.keys(status?.roles ?? {})) set.add(p);
  for (const l of leases) {
    const m = /^(delegate|coordinator):(.+)$/.exec(l.name);
    if (m && l.holder && !l.expired) set.add(m[2]);
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}

/** The project with a role first, else the first project. */
export function defaultProject(projects: string[], status: MaintainerStatus | null, leases: Lease[]): string | null {
  const withRole = projects.find((p) => {
    const r = rolesFor(p, status, leases);
    return r.delegate || r.coordinator;
  });
  return withRole ?? projects[0] ?? null;
}

/** Whether a session can be given a role at all (spec: registered, not a subagent, has a project). */
export function roleEligible(a: BoardAgent): { ok: boolean; why: string } {
  if (a.subagent) return { ok: false, why: "A subagent cannot hold a role." };
  if (a.lifecycle === "revoked") return { ok: false, why: "This session is revoked." };
  if (!a.project) return { ok: false, why: "This session names no project, so a role here would never be read." };
  return { ok: true, why: "" };
}

/** "22 h left" for a role's expiry, measured on the browser clock. */
export function timeLeft(expiresAt: Epoch | null, nowMs: number): string {
  if (!expiresAt) return "no end set";
  const s = expiresAt - nowMs / 1000;
  return s <= 0 ? "ending now" : `${fmtDuration(s)} left`;
}

/** Share of the hold still left, 0..100, or null when the grant time is unknown. */
export function leftPercent(h: RoleHolder, nowMs: number): number | null {
  if (!h.acquired_at || !h.expires_at || h.expires_at <= h.acquired_at) return null;
  const pct = ((h.expires_at - nowMs / 1000) / (h.expires_at - h.acquired_at)) * 100;
  return Math.max(0, Math.min(100, pct));
}

/**
 * What a role change displaces, from the daemon's preview (the authority:
 * it read the leases when it issued the challenge). Empty when the preview
 * is not a role preview.
 */
export function displacedLines(preview: Preview | null | undefined, nameOf: (id: string) => string): string[] {
  if (!preview || typeof preview.action !== "string") return [];
  const lines: string[] = [];
  const role = preview.role === "delegate" ? "your delegate" : "the coordinator";
  if (preview.action !== "revoke" && typeof preview.replaces === "string" && preview.replaces) {
    lines.push(`“${nameOf(preview.replaces)}” stops being ${role}.`);
  }
  const also = typeof preview.also_breaks === "string" ? preview.also_breaks : "";
  if (also.startsWith("coordinator:")) lines.push("It also stops being the coordinator: a session holds one role at a time.");
  else if (also.startsWith("delegate:")) lines.push("It also stops being your delegate: a session holds one role at a time.");
  return lines;
}

export function grantedByText(by: string | null): string {
  if (by === "maintainer") return "Granted by you with your passkey";
  if (by === "operator") return "Granted by the operator on the daemon host";
  return "Granted";
}

// ---- readiness --------------------------------------------------------------------

export interface Blocked {
  ok: false;
  title: string;
  body: string;
  /** A command to run on the daemon host, shown as code. */
  command?: string;
}
export type Readiness = { ok: true } | Blocked;

/** A key that can sign now: active and past its quarantine. */
export function usableKey(k: Passkey, nowMs: number): boolean {
  return k.state === "active" && (!k.active_from || k.active_from * 1000 <= nowMs);
}

export function inQuarantine(k: Passkey, nowMs: number): boolean {
  return k.state === "active" && !!k.active_from && k.active_from * 1000 > nowMs;
}

function statusBlocked(status: MaintainerStatus | null, statusError: ApiError | null): Blocked | null {
  if (statusError) {
    if (statusError.status === 404) {
      return {
        ok: false,
        title: "This daemon has no passkey support",
        body: "It predates maintainer messages and Console roles. Update the daemon, then reload the Console.",
      };
    }
    return {
      ok: false,
      title: "Passkey status could not be read",
      body: `The daemon answered ${statusError.status ? `HTTP ${statusError.status}, ` : ""}${words(statusError.code)}. Refresh to try again.`,
    };
  }
  if (!status) return { ok: false, title: "Reading passkey status", body: "One moment." };
  if (!status.available) {
    switch (status.reason) {
      case "maintainer_https_required":
        return {
          ok: false,
          title: "Passkeys need HTTPS at a fixed name",
          body: "Serve the Console over HTTPS on the daemon host, set coordination.maintainer.rp_id and .origin in config.yaml, restart the daemon, then open the Console at that address.",
          command: HTTPS_COMMAND,
        };
      case "maintainer_not_enrolled":
        return {
          ok: false,
          title: "No passkey is enrolled",
          body: "Print a one-time code on the daemon host, then enrol a passkey in Settings, Your passkeys.",
          command: ENROL_COMMAND,
        };
      case "authentication_required":
        return {
          ok: false,
          title: "This daemon has no bearer token",
          body: "A tokenless daemon refuses every maintainer route. Start it with PSEUDOLIFE_MCP_TOKEN, then store the same token here.",
        };
      default:
        return {
          ok: false,
          title: "Passkey actions are unavailable",
          body: `The daemon says ${words(status.reason) || "they are unavailable"}.`,
        };
    }
  }
  return null;
}

/**
 * Whether a signed action can run from this page now. Fixture mode skips the
 * browser checks: the demo never runs a real ceremony.
 */
export function signReadiness(input: {
  status: MaintainerStatus | null;
  statusError: ApiError | null;
  support: Support;
  pageOrigin: string;
  fixtures: boolean;
  nowMs: number;
}): Readiness {
  // "Not enrolled" is also the answer while the only key is pending or in
  // quarantine: the key list below says which, and what to do.
  const notEnrolled = input.status?.available === false && input.status.reason === "maintainer_not_enrolled";
  const blocked = notEnrolled ? null : statusBlocked(input.status, input.statusError);
  if (blocked) return blocked;
  const keys = input.status?.passkeys ?? [];
  if (!keys.some((k) => usableKey(k, input.nowMs))) {
    const pending = keys.find((k) => k.state === "pending");
    if (pending) {
      return {
        ok: false,
        title: "Your passkey waits for the host confirm",
        body: `Check that the prefix matches the key in Settings, then confirm it on the daemon host.`,
        command: `pseudolife-mcp maintainer confirm ${shortId(pending.credential_id)}`,
      };
    }
    const q = keys.find((k) => inQuarantine(k, input.nowMs));
    if (q) {
      return {
        ok: false,
        title: "Your only active passkey is still in quarantine",
        body: `"${q.label}" can sign from ${new Date((q.active_from ?? 0) * 1000).toLocaleString()}.`,
      };
    }
    return {
      ok: false,
      title: "No passkey is enrolled",
      body: "Print a one-time code on the daemon host, then enrol a passkey in Settings, Your passkeys.",
      command: ENROL_COMMAND,
    };
  }
  if (input.fixtures) return { ok: true };
  if (!input.support.ok) {
    return { ok: false, title: "This browser cannot use a passkey here", body: input.support.message };
  }
  const origin = input.status?.origin;
  if (origin && origin !== input.pageOrigin) {
    return {
      ok: false,
      title: "Open the Console at its passkey address",
      body: `Your passkey belongs to ${origin}; this page is ${input.pageOrigin}. The browser only offers it there.`,
    };
  }
  return { ok: true };
}

/** Whether a first (bootstrap) passkey can be enrolled from this page. */
export function bootstrapReadiness(input: {
  status: MaintainerStatus | null;
  statusError: ApiError | null;
  support: Support;
  pageOrigin: string;
  fixtures: boolean;
}): Readiness {
  const s = input.status;
  if (!(s && !s.available && s.reason === "maintainer_not_enrolled")) {
    const blocked = statusBlocked(s, input.statusError);
    if (blocked) return blocked;
  }
  if (input.fixtures) return { ok: true };
  if (!input.support.ok) {
    return { ok: false, title: "This browser cannot create a passkey here", body: input.support.message };
  }
  if (s?.origin && s.origin !== input.pageOrigin) {
    return {
      ok: false,
      title: "Open the Console at its passkey address",
      body: `Passkeys for this daemon belong to ${s.origin}; this page is ${input.pageOrigin}.`,
    };
  }
  return { ok: true };
}

// ---- errors -------------------------------------------------------------------------

const REASONS: Record<string, string> = {
  maintainer_https_required: "passkeys need HTTPS at the configured address, and this daemon has none set up.",
  maintainer_not_enrolled: "no active passkey is enrolled. Enrol one in Settings, Your passkeys.",
  challenge_expired: "the signature came after the two-minute challenge ran out. Try again.",
  challenge_spent: "that signature was already used once. Try again for a fresh challenge.",
  assertion_invalid:
    "the daemon did not accept the passkey's signature. Check in Settings that this key is active and out of quarantine.",
  recipient_unknown: "the daemon does not know that session any more. Refresh the board.",
  recipient_reserved: "that address is reserved; it cannot take maintainer mail or a role.",
  rate_capped: "the hourly limit of maintainer messages to this session is reached. Try again later.",
  config_protected: "those settings can only be changed in config.yaml on the daemon host.",
  enrolment_closed: "a passkey is already enrolled or waiting for the host confirm, so the one-time code no longer applies.",
  bootstrap_code_invalid:
    "the one-time code was wrong, already used or older than 10 minutes. Print a fresh one on the daemon host.",
  message_not_found: "the daemon has no such maintainer message any more.",
  credential_not_found: "the daemon has no such passkey any more. Refresh the list.",
  invalid_request: "the daemon refused the request as malformed (a label must be one printable line of up to 64 characters).",
  role_changed: "someone changed this role since you opened the dialog; look again and retry.",
  coordination_unavailable: "the board's storage is unavailable. Check the daemon log and the database, then retry.",
  principals_unavailable: "the daemon cannot check tokens right now, so nothing ran. Retry in a moment.",
  network: "the daemon did not answer. Refresh to see whether anything changed.",
  unauthorized: "the daemon did not accept this Console's token.",
};

/** Why a maintainer action failed, as the end of a sentence. */
export function maintainerReason(e: unknown): string {
  if (e instanceof CeremonyError) return e.message.charAt(0).toLowerCase() + e.message.slice(1);
  if (e instanceof ApiError) {
    const known = REASONS[e.code];
    if (known) return known;
    const detail = typeof e.body?.detail === "string" && e.body.detail ? e.body.detail : "";
    return detail ? `${detail}.` : `the daemon answered ${e.status && e.status !== 200 ? `HTTP ${e.status}, ` : ""}${words(e.code)}.`;
  }
  return "something went wrong in the Console. The browser console has the error.";
}

/** "Your message was not sent: the signature came after ..." */
export function failureLine(failure: string, e: unknown): string {
  return `${failure}: ${maintainerReason(e)}`;
}

// ---- the signed exchange ----------------------------------------------------------

/** The payload as an object, when it is canonical JSON text or already an object. */
export function payloadFields(payload: unknown): Record<string, unknown> | null {
  if (payload && typeof payload === "object" && !Array.isArray(payload)) return payload as Record<string, unknown>;
  if (typeof payload !== "string") return null;
  try {
    const v = JSON.parse(payload) as unknown;
    return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

// Free text is compared by the daemon, which may normalise it; the fields
// that choose the target and the effect must come back exactly as asked.
const EXACT = ["purpose", "to", "urgent", "project", "agent_id", "hold", "credential_id", "message_id"];

/** Null when the signed payload says what was asked, else what differs. */
export function payloadMismatch(body: ChallengeBody, payload: unknown): string | null {
  const p = payloadFields(payload);
  if (!p) return null;
  for (const k of EXACT) {
    if (!(k in body)) continue;
    const want = (body as Record<string, unknown>)[k];
    if (p[k] !== want) return k;
  }
  return null;
}

/** When the challenge stops being usable, in epoch seconds, if the payload says. */
export function challengeExpiry(answer: ChallengeAnswer): number | null {
  const v = payloadFields(answer.payload)?.expires_at;
  return typeof v === "number" ? v : null;
}

/**
 * Sign the challenge and complete it. Throws on any refusal, including a 200
 * answer carrying {"error": ...}: success is only ever the daemon's own
 * success answer.
 */
export async function signAndComplete<T>(
  answer: ChallengeAnswer,
  sign: (publicKey: unknown) => Promise<AssertionJSON>,
  complete: (s: Signed) => Promise<T>,
): Promise<T> {
  const assertion = await sign(answer.publicKey);
  const result = await complete({ payload: answer.payload, mac: answer.mac, assertion });
  const refused = softError(result);
  if (refused) throw new ApiError(200, refused, result as Record<string, string>);
  return result;
}

// ---- key-change notices -------------------------------------------------------------

/** Credential ids this browser signed or enrolled with: "the key this Console uses". */
export const OWN_KEYS_KEY = "pl_maintainer_keys";
/** Key changes the maintainer said they made, so their notice stops. */
export const SEEN_CHANGES_KEY = "pl_maintainer_seen_changes";
/** How long a key change the Console did not make stays a notice. */
export const KEY_NOTICE_DAYS = 7;
const LIST_CAP = 50;

/** A stored list of strings; empty when storage is unavailable or garbled. */
export function readList(key: string): string[] {
  try {
    const v: unknown = JSON.parse(readKey(key) || "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

/**
 * Add `value` to `base` (moved to the end if present), keep the newest
 * LIST_CAP, store, return the list: the list still holds for this tab when
 * storage is unavailable.
 */
export function addToList(key: string, value: string, base: string[] = readList(key)): string[] {
  const list = [...base.filter((v) => v !== value), value].slice(-LIST_CAP);
  writeKey(key, JSON.stringify(list));
  return list;
}

export const changeId = (c: KeyChange) => `${c.at}:${c.change}:${c.credential_id ?? ""}`;

/**
 * Made by this Console: signed by one of its keys, or the enrolment and
 * host confirm of a key it enrolled itself.
 */
function ownChange(c: KeyChange, own: Set<string>): boolean {
  if (own.has(c.by)) return true;
  return (c.change === "enrol" || c.change === "confirm") && !!c.credential_id && own.has(c.credential_id);
}

/**
 * The key changes of the last KEY_NOTICE_DAYS that this Console did not make
 * and the maintainer has not acknowledged, newest first.
 */
export function keyNotices(
  changes: KeyChange[] | undefined,
  own: Iterable<string>,
  seen: Iterable<string>,
  nowMs: number,
): KeyChange[] {
  const mine = new Set(own);
  const acked = new Set(seen);
  const since = nowMs / 1000 - KEY_NOTICE_DAYS * 86400;
  return (changes ?? []).filter((c) => c.at >= since && !ownChange(c, mine) && !acked.has(changeId(c)));
}

/** A key change in words, naming the path it came by. */
export function keyChangeText(c: KeyChange): string {
  const key = c.credential_id ? `${c.label ? `“${c.label}” ` : "key "}(${shortId(c.credential_id)})` : "a key";
  const signer = `key ${shortId(c.by)}`;
  switch (c.change) {
    case "enrol":
      return `${key} was enrolled in the Console with a one-time code from the daemon host`;
    case "confirm":
      return `${key} was confirmed on the daemon host`;
    case "add":
      return `${key} was added in the Console, approved by ${signer}`;
    case "cancel":
      return `${key} was cancelled in the Console by ${signer}`;
    case "revoke":
      return c.path === "host" ? `${key} was revoked on the daemon host` : `${key} revoked itself`;
    case "reset":
      return `every passkey was revoked on the daemon host (reset; ${c.revoked ?? 0} revoked)`;
    default:
      return `${words(c.change)}: ${key}${c.path === "host" ? " on the daemon host" : ""}`;
  }
}

// ---- messages ---------------------------------------------------------------------

function listOf(raw: unknown, keys: string[]): Record<string, unknown>[] {
  if (Array.isArray(raw)) return raw as Record<string, unknown>[];
  if (raw && typeof raw === "object") {
    for (const k of keys) {
      const v = (raw as Record<string, unknown>)[k];
      if (Array.isArray(v)) return v as Record<string, unknown>[];
    }
  }
  return [];
}

const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** GET /api/maintainer/sent, as a bare list or wrapped ({messages}, {items}, {sent}). */
export function normalizeSent(raw: unknown): SentMessage[] {
  return listOf(raw, ["messages", "items", "sent"])
    .map((m) => ({
      message_id: str(m.message_id) ?? "",
      to: str(m.to) ?? str(m.recipient_agent_id) ?? "",
      to_label: str(m.to_label) ?? str(m.recipient_label),
      text: str(m.text),
      urgent: m.urgent === true,
      created_at: num(m.created_at) ?? num(m.sent_at) ?? 0,
      label: str(m.label),
      wake: m.wake ?? null,
      repudiated_at: num(m.repudiated_at),
      read_at: num(m.read_at) ?? num(m.first_read_at),
    }))
    .filter((m) => m.message_id && m.to);
}

/** GET /api/maintainer/inbox, as a bare list or wrapped ({messages}, {items}, {inbox}). */
export function normalizeInbox(raw: unknown): InboxMessage[] {
  return listOf(raw, ["messages", "items", "inbox"])
    .map((m) => ({
      message_id: str(m.message_id) ?? "",
      from: str(m.from) ?? str(m.sender_agent_id) ?? str(m.agent_id) ?? "",
      from_label: str(m.from_label) ?? str(m.sender_label) ?? str(m.label),
      text: typeof m.text === "string" ? m.text : "",
      created_at: num(m.created_at) ?? 0,
      reply_to: str(m.reply_to),
    }))
    .filter((m) => m.message_id && m.from);
}

/**
 * What a maintainer message does about waking, for the confirm step. The
 * daemon rings every one whose recipient has a live listener, whatever its
 * park record (spec 2026-10-02, "Wake decision"); the signed ``urgent``
 * field changes nothing, so the Console neither offers nor shows it.
 */
export const SEND_WAKE =
  "It rings the session now if its wake listener is live, even one parked as done; otherwise the session reads it on its next turn.";

/** The wake receipt in words. */
export function wakeText(wake: unknown): string {
  const w =
    typeof wake === "string"
      ? wake
      : wake && typeof wake === "object"
        ? (str((wake as Record<string, unknown>).decision) ?? str((wake as Record<string, unknown>).status) ?? "")
        : "";
  switch (w) {
    case "":
      return "";
    case "rang":
    case "rung":
    case "ring":
    case "woke":
      return "rang its session";
    case "no_path":
      return "no live listener, read on its next turn";
    case "capped":
      return "delivered, not rung: the hourly wake limit is reached";
    case "hinted":
    case "hint":
      return "delivered as a hint";
    case "not_needed":
      return "it was active";
    default:
      return words(w);
  }
}

export interface ThreadItem {
  id: string;
  mine: boolean;
  at: Epoch;
  text: string;
  note: string;
  withdrawn: boolean;
}

/** Messages to and replies from one agent, oldest first, the newest `limit`. */
export function threadFor(agentId: string, sent: SentMessage[], inbox: InboxMessage[], limit = 6): ThreadItem[] {
  const items: ThreadItem[] = [
    ...sent
      .filter((m) => m.to === agentId)
      .map((m) => ({
        id: m.message_id,
        mine: true,
        at: m.created_at,
        text: m.text ?? "",
        note: m.repudiated_at
          ? "withdrawn"
          : [wakeText(m.wake), m.read_at ? "read" : ""].filter(Boolean).join(", "),
        withdrawn: !!m.repudiated_at,
      })),
    ...inbox
      .filter((m) => m.from === agentId)
      .map((m) => ({ id: m.message_id, mine: false, at: m.created_at, text: m.text, note: "", withdrawn: false })),
  ];
  items.sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
  return items.slice(-limit);
}

/** UTF-8 length of a message, against the daemon's 8192-byte limit. */
export const MAX_TEXT_BYTES = 8192;
export function textBytes(s: string): number {
  return new TextEncoder().encode(s).length;
}
