// Typed calls for the maintainer routes (docs/superpowers/specs/
// 2026-10-04-board-roles-passkey.md, section 4, and the "Console contract"
// table of 2026-10-02-maintainer-wake-design.md). Every action is a signed
// purpose: POST /challenge, a passkey ceremony, then the completing route.
// Use lib/maintainerFlow.svelte.ts rather than these calls directly, so
// every action shows its preview and reads the daemon's answer the same way.

import type { Epoch } from "./types";
import type { AssertionJSON, AttestationJSON } from "../webauthn";
import { get, post } from "./client";

export type PasskeyState = "pending" | "active" | "revoked";

export interface Passkey {
  credential_id: string;
  label: string;
  state: PasskeyState | string;
  /** "bootstrap", or the credential_id of the key that approved it. */
  enrolled_by: string;
  /** End of the 24 h quarantine for an added key; null while pending. */
  active_from: Epoch | null;
  created_at: Epoch;
  last_used_at: Epoch | null;
  revoked_at: Epoch | null;
  /** "host" or the cancelling credential_id. */
  revoked_by?: string | null;
  /** Set when a sign count went backwards (a replay or a cloned authenticator). */
  flagged_at?: Epoch | null;
}

export interface DelegateRole {
  agent_id: string;
  expires_at: Epoch | null;
  granted_by: "operator" | "maintainer" | string;
}

export interface CoordinatorRole {
  agent_id: string;
  expires_at: Epoch | null;
}

export interface ProjectRoles {
  delegate: DelegateRole | null;
  coordinator: CoordinatorRole | null;
}

/**
 * One change to the key set, as the daemon's audit log records it (newest
 * first in the status). Anyone with a shell on the daemon host or the
 * database password can change keys, so every change is shown.
 */
export interface KeyChange {
  at: Epoch;
  /** enrol (bootstrap), confirm (host), add (quarantined), cancel, revoke, reset. */
  change: "enrol" | "confirm" | "add" | "cancel" | "revoke" | "reset" | string;
  /** The key changed; null for a reset. */
  credential_id: string | null;
  label: string | null;
  /** The signing key's credential_id, "bootstrap" (a host code) or "host" (the CLI). */
  by: string;
  path: "console" | "host" | string;
  /** The bearer principal of a Console change; null on the host. */
  principal: string | null;
  /** A reset: how many keys it revoked. */
  revoked: number | null;
}

/** GET /api/maintainer */
export interface MaintainerStatus {
  available: boolean;
  /** Why not: maintainer_https_required, maintainer_not_enrolled, ... */
  reason?: string;
  rp_id?: string | null;
  origin?: string | null;
  passkeys?: Passkey[];
  /** Every project with a live role lease. */
  roles?: Record<string, ProjectRoles>;
  /** The recent key-set changes, newest first. */
  key_changes?: KeyChange[];
}

export type RolePurpose = "grant-delegate" | "revoke-delegate" | "assign-coordinator" | "revoke-coordinator";

export type ChallengeBody =
  | { purpose: "send"; to: string; text: string; urgent: boolean }
  | { purpose: "grant-delegate" | "assign-coordinator"; project: string; agent_id: string; hold: number }
  | { purpose: "revoke-delegate" | "revoke-coordinator"; project: string }
  | { purpose: "cancel" | "revoke-self"; credential_id: string }
  | { purpose: "repudiate"; message_id: string }
  | { purpose: "enrol-bootstrap" | "enrol-approve"; label: string };

export type Purpose = ChallengeBody["purpose"];

/**
 * What the daemon says the signature will do, shown before the tap. The
 * contract names its content (recipient name, label, id prefix, principal,
 * host, duplicate_name; for roles the role, project and who is replaced),
 * not its keys, so every field is optional and read defensively.
 */
export interface Preview {
  name?: string | null;
  label?: string | null;
  agent_id?: string | null;
  agent_id_prefix?: string | null;
  agent_prefix?: string | null;
  principal?: string | null;
  host?: string | null;
  duplicate_name?: boolean;
  /** Role previews: "delegate" | "coordinator". */
  role?: string | null;
  project?: string | null;
  /** Role previews: "grant" | "assign" | "revoke". */
  action?: string | null;
  /** Role previews: the agent id holding the role now, or null. */
  current_holder?: string | null;
  /** Grant/assign: the agent id this displaces, or null. */
  replaces?: string | null;
  /** Grant/assign: the other role's lease the recipient loses ("coordinator:<p>"), or null. */
  also_breaks?: string | null;
  [key: string]: unknown;
}

export interface ChallengeAnswer {
  /** Canonical JSON the daemon MACs; sent back verbatim. */
  payload: unknown;
  mac: string;
  /** Request options (CreationOptions for enrolment) as JSON, base64url byte fields. */
  publicKey: Record<string, unknown>;
  preview?: Preview;
}

export interface Signed {
  payload: unknown;
  mac: string;
  assertion: AssertionJSON;
}

/** The board's wake decision for a maintainer message (null on old rows). */
export interface WakeDecision {
  decision: "rung" | "no_path" | "capped" | string;
  reason?: string;
  [key: string]: unknown;
}

export interface SendResult {
  message_id: string;
  wake?: WakeDecision | string | null;
}

export interface RepudiateResult {
  message_id: string;
  repudiated_at: Epoch;
  /** The follow-up notice's message id, sent when the recipient had already acknowledged it. */
  follow_up: string | null;
}

export interface GrantResult {
  name: string;
  agent_id: string;
  fence: number;
  expires_at: Epoch;
  replaced: string | null;
  also_broken?: string | null;
}

export interface RevokeRoleResult {
  name: string;
  broken: boolean;
  was_held_by: string | null;
}

export type RoleResult = GrantResult | RevokeRoleResult;

export interface EnrolResult {
  credential_id: string;
  label: string;
  state: string;
}

/** The enrol-approve step answers with the second (purpose enrol) challenge. */
export type ApproveAnswer = ChallengeAnswer;

/** One maintainer message, newest first in GET /api/maintainer/sent. */
export interface SentMessage {
  message_id: string;
  to: string;
  to_label?: string | null;
  text?: string | null;
  urgent?: boolean;
  created_at: Epoch;
  /** The passkey's label. */
  label?: string | null;
  wake?: unknown;
  repudiated_at?: Epoch | null;
  /** First read by the recipient (the daemon serves it as first_read_at). */
  read_at?: Epoch | null;
}

/** One reply to a maintainer message, newest first in GET /api/maintainer/inbox. */
export interface InboxMessage {
  message_id: string;
  from: string;
  from_label?: string | null;
  text: string;
  created_at: Epoch;
  reply_to?: string | null;
}

export const maintainerApi = {
  status: () => get<MaintainerStatus>("/api/maintainer"),
  challenge: (body: ChallengeBody) => post<ChallengeAnswer>("/api/maintainer/challenge", body),
  /** Bootstrap or the second step of adding a key: {payload, mac, attestation, code?}. */
  enrol: (body: { payload: unknown; mac: string; attestation: AttestationJSON; code?: string }) =>
    post<EnrolResult>("/api/maintainer/enrol", body),
  /** The first step of adding a key: the enrol-approve signature, answered with the enrol challenge. */
  approveEnrol: (s: Signed) => post<ApproveAnswer>("/api/maintainer/enrol", { ...s }),
  send: (s: Signed) => post<SendResult>("/api/maintainer/send", { ...s }),
  /** Grant/assign answers GrantResult, revoke RevokeRoleResult. */
  role: <T extends RoleResult>(s: Signed) => post<T>("/api/maintainer/role", { ...s }),
  cancel: (s: Signed) => post<{ state: string }>("/api/maintainer/cancel", { ...s }),
  revoke: (s: Signed) => post<{ state: string }>("/api/maintainer/revoke", { ...s }),
  repudiate: (s: Signed) => post<RepudiateResult>("/api/maintainer/repudiate", { ...s }),
  sent: (limit = 50) => get<unknown>("/api/maintainer/sent", { limit }),
  inbox: (limit = 50) => get<unknown>("/api/maintainer/inbox", { limit }),
};
