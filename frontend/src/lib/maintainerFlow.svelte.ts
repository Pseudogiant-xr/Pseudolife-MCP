// The one flow every passkey-signed action takes: a confirm dialog that
// fetches the challenge and shows the daemon's preview, the passkey tap,
// the completing route, then a toast that reports only the daemon's own
// success answer. <MaintainerDialog/> (mounted once in App.svelte) drives it.
//
// Also the shared maintainer state the Board and Settings both read: the
// status (passkeys, roles) and the recent sent/inbox messages.

import { untrack } from "svelte";
import { ApiError } from "./api/client";
import {
  maintainerApi,
  type ChallengeAnswer,
  type ChallengeBody,
  type EnrolResult,
  type GrantResult,
  type InboxMessage,
  type KeyChange,
  type MaintainerStatus,
  type RepudiateResult,
  type RevokeRoleResult,
  type SendResult,
  type SentMessage,
  type Signed,
} from "./api/maintainer";
import {
  COORDINATOR_HOLD,
  HOLDS,
  OWN_KEYS_KEY,
  SEEN_CHANGES_KEY,
  addToList,
  bootstrapReadiness,
  changeId,
  failureLine,
  keyNotices,
  normalizeInbox,
  normalizeSent,
  readList,
  SEND_WAKE,
  signReadiness,
  wakeText,
  type Readiness,
  type RoleHolder,
} from "./maintainer";
import { toast } from "./overlay.svelte";
import { loadBoard, store } from "./state.svelte";
import { browserEnv, createCredential, getAssertion, webauthnSupport } from "./webauthn";
import { fmtDateTime } from "./format";

// ---- shared state -------------------------------------------------------------

export const maintainer = $state({
  status: null as MaintainerStatus | null,
  error: null as ApiError | null,
  loading: false,
  sent: [] as SentMessage[],
  inbox: [] as InboxMessage[],
  threadsError: null as ApiError | null,
  threadsAt: 0,
});

function asApiError(e: unknown): ApiError {
  return e instanceof ApiError ? e : new ApiError(0, "client_error", null);
}

// A status code the routes may answer instead of {available: false, reason}.
const UNAVAILABLE = new Set(["maintainer_https_required", "maintainer_not_enrolled"]);
let reloadStatus = false;

/**
 * GET /api/maintainer; untracked so an effect calling it never re-runs on
 * its writes. A load asked for while one is in flight runs once more after
 * it, so the re-read after a role change is never dropped.
 */
export function loadMaintainer(): Promise<void> {
  return untrack(async () => {
    if (maintainer.loading) {
      reloadStatus = true;
      return;
    }
    maintainer.loading = true;
    try {
      maintainer.status = await maintainerApi.status();
      maintainer.error = null;
    } catch (e) {
      const err = asApiError(e);
      if (UNAVAILABLE.has(err.code)) {
        maintainer.status = { available: false, reason: err.code, passkeys: [] };
        maintainer.error = null;
      } else {
        maintainer.error = err;
        if (err.status === 401 || err.status === 403 || err.status === 404) maintainer.status = null;
      }
    } finally {
      maintainer.loading = false;
    }
    if (reloadStatus) {
      reloadStatus = false;
      await loadMaintainer();
    }
  });
}

/** The recent maintainer messages and replies the slot threads show. */
export function loadThreads(): Promise<void> {
  return untrack(async () => {
    try {
      const [s, i] = await Promise.all([maintainerApi.sent(50), maintainerApi.inbox(50)]);
      maintainer.sent = normalizeSent(s);
      maintainer.inbox = normalizeInbox(i);
      maintainer.threadsError = null;
      maintainer.threadsAt = Date.now();
    } catch (e) {
      maintainer.threadsError = asApiError(e);
    }
  });
}

// ---- key-change notices ------------------------------------------------------------

/** This browser's keys and the key changes the maintainer acknowledged (per browser). */
export const keyNotes = $state({ own: readList(OWN_KEYS_KEY), seen: readList(SEEN_CHANGES_KEY) });

/** A key this Console signed with or enrolled: its changes are not notices. */
export function rememberOwnKey(credentialId: string | null | undefined): void {
  if (credentialId) keyNotes.own = addToList(OWN_KEYS_KEY, credentialId, keyNotes.own);
}

/** "I made this change": its notice stops in this browser. */
export function acknowledgeChange(c: KeyChange): void {
  keyNotes.seen = addToList(SEEN_CHANGES_KEY, changeId(c), keyNotes.seen);
}

/** The recent key changes this Console did not make, newest first. */
export function keyChangeNotices(nowMs = Date.now()): KeyChange[] {
  return keyNotices(maintainer.status?.key_changes, keyNotes.own, keyNotes.seen, nowMs);
}

/** The daemon declared demo data (health `fixtures: true`): no real ceremony. */
export function fixtureMode(): boolean {
  return store.health.data?.fixtures === true;
}

export function readiness(nowMs = Date.now()): Readiness {
  return signReadiness({
    status: maintainer.status,
    statusError: maintainer.error,
    support: webauthnSupport(browserEnv()),
    pageOrigin: location.origin,
    fixtures: fixtureMode(),
    nowMs,
  });
}

export function enrolReadiness(): Readiness {
  return bootstrapReadiness({
    status: maintainer.status,
    statusError: maintainer.error,
    support: webauthnSupport(browserEnv()),
    pageOrigin: location.origin,
    fixtures: fixtureMode(),
  });
}

// ---- the dialog ------------------------------------------------------------------

export type SignTone = "canon" | "assoc" | "danger" | "neutral";

export interface SignRequest<T = unknown> {
  title: string;
  /** Plain text. */
  body: string;
  /** Lines naming what this displaces (orange). */
  replaces?: string[];
  /** Ask for a hold from HOLDS. */
  askHold?: boolean;
  /** The passkey line under the body. */
  note?: string;
  tone: SignTone;
  /** The verb on the confirm button. */
  confirmLabel: string;
  /** The challenge for the chosen hold (ignored when askHold is false). */
  challenge: (hold: number) => ChallengeBody;
  /** The completing call (several, for adding a key). */
  complete: (s: Signed, answer: ChallengeAnswer) => Promise<T>;
  /** The toast on the daemon's success answer. */
  success: (r: T, hold: number) => string;
  /** The start of the failure toast, e.g. "Your message was not sent". */
  failure: string;
}

export interface PendingSign extends SignRequest {
  resolve: (r: unknown) => void;
}

export const signing = $state<{ current: PendingSign | null }>({ current: null });

/** Open the confirm dialog for a signed action. Resolves the daemon's answer, or null. */
export function askSigned<T>(req: SignRequest<T>): Promise<T | null> {
  signing.current?.resolve(null);
  return new Promise((resolve) => {
    signing.current = { ...(req as SignRequest), resolve: resolve as (r: unknown) => void };
  });
}

/** Close the dialog with an outcome (null: cancelled or failed). */
export function settleSigning(result: unknown = null): void {
  const p = signing.current;
  signing.current = null;
  p?.resolve(result);
}

export const PASSKEY_NOTE = "You confirm with your passkey, so no session can change a role on its own.";
const MESSAGE_NOTE = "Your passkey signs this exact text and recipient. Tap only prompts you started here.";

export async function sign(publicKey: unknown) {
  const assertion = await getAssertion(publicKey, { fixtures: fixtureMode() });
  rememberOwnKey(assertion.id);
  return assertion;
}

function holdLabel(seconds: number): string {
  return HOLDS.find((h) => h.seconds === seconds)?.label ?? `${Math.round(seconds / 3600)} h`;
}

/** After a role change: re-read the roles and the board. */
function changed<T>(r: T | null): T | null {
  if (r) {
    void loadMaintainer();
    void loadBoard();
  }
  return r;
}

/**
 * After a grant or extend: a role holder with no live wake listener still
 * gets the role, but your messages to it wait for its next turn, so say so
 * beside the success toast (maintainer requirement 2026-10-05).
 */
function warnUnreachable(r: GrantResult | null): GrantResult | null {
  if (r && r.reachable === false) {
    toast(r.warning || "It has no live wake listener now: your messages wait for its next turn.", "warn", 12000);
  }
  return r;
}

/** After a passkey change: re-read the key list. */
function keysChanged<T>(r: T | null): T | null {
  if (r) void loadMaintainer();
  return r;
}

// ---- role requests ----------------------------------------------------------------

export interface Target {
  agent_id: string;
  name: string;
  project: string;
}

const q = (s: string) => `“${s}”`;

export function grantDelegate(t: Target, current: { holder: RoleHolder; name: string } | null, isCoordinator: boolean) {
  const replaces: string[] = [];
  if (current && current.holder.agent_id !== t.agent_id) replaces.push(`${q(current.name)} stops being your delegate.`);
  if (isCoordinator) replaces.push("It stops being the coordinator: a session holds one role at a time.");
  return askSigned<GrantResult>({
    title: `Make ${q(t.name)} your delegate?`,
    body: `For ${t.project} it acts with your authority: it can wake any session here, including sessions parked as done, and its urgent mail rings like yours.`,
    replaces,
    askHold: true,
    note: PASSKEY_NOTE,
    tone: "canon",
    confirmLabel: "Confirm with passkey",
    challenge: (hold) => ({ purpose: "grant-delegate", project: t.project, agent_id: t.agent_id, hold }),
    complete: (s) => maintainerApi.role(s),
    success: (_r, hold) => `${q(t.name)} is your delegate for ${holdLabel(hold)}.`,
    failure: "The delegate did not change",
  }).then(changed).then(warnUnreachable);
}

export function extendDelegate(t: Target) {
  return askSigned<GrantResult>({
    title: "Extend your delegate?",
    body: `${q(t.name)} keeps your authority for ${t.project} for the time you pick, counted from now.`,
    askHold: true,
    note: PASSKEY_NOTE,
    tone: "canon",
    confirmLabel: "Confirm with passkey",
    challenge: (hold) => ({ purpose: "grant-delegate", project: t.project, agent_id: t.agent_id, hold }),
    complete: (s) => maintainerApi.role(s),
    success: (_r, hold) => `Your delegate now ends in ${holdLabel(hold)}.`,
    failure: "The delegate was not extended",
  }).then(changed).then(warnUnreachable);
}

export function revokeDelegate(t: Target) {
  return askSigned<RevokeRoleResult>({
    title: "Revoke your delegate?",
    body: `${q(t.name)} loses your authority at once. Sessions it already woke keep running.`,
    note: PASSKEY_NOTE,
    tone: "danger",
    confirmLabel: "Revoke the delegate",
    challenge: () => ({ purpose: "revoke-delegate", project: t.project }),
    complete: (s) => maintainerApi.role(s),
    success: (r) => (r.broken ? "Delegate revoked." : "Delegate revoked; no session was holding the role any more."),
    failure: "The delegate was not revoked",
  }).then(changed);
}

export function assignCoordinator(t: Target, current: { holder: RoleHolder; name: string } | null, isDelegate: boolean) {
  const replaces: string[] = [];
  if (current && current.holder.agent_id !== t.agent_id) replaces.push(`${q(current.name)} stops being the coordinator.`);
  if (isDelegate) replaces.push("It stops being your delegate: a session holds one role at a time.");
  return askSigned<GrantResult>({
    title: `Make ${q(t.name)} the coordinator?`,
    body: `It organises the work in ${t.project}: hands out tasks and queues shared resources. It gets no authority to wake sessions parked as done. It holds the role a day at a time and renews it itself.`,
    replaces,
    note: PASSKEY_NOTE,
    tone: "assoc",
    confirmLabel: "Confirm with passkey",
    challenge: () => ({ purpose: "assign-coordinator", project: t.project, agent_id: t.agent_id, hold: COORDINATOR_HOLD }),
    complete: (s) => maintainerApi.role(s),
    success: () => `${q(t.name)} is the coordinator.`,
    failure: "The coordinator did not change",
  }).then(changed).then(warnUnreachable);
}

export function revokeCoordinator(t: Target) {
  return askSigned<RevokeRoleResult>({
    title: "Revoke the coordinator?",
    body: `${q(t.name)} stops coordinating. The next session queued for the role takes it; otherwise any session can claim it again, or you can choose one.`,
    note: PASSKEY_NOTE,
    tone: "neutral",
    confirmLabel: "Revoke with passkey",
    challenge: () => ({ purpose: "revoke-coordinator", project: t.project }),
    complete: (s) => maintainerApi.role(s),
    success: (r) => (r.broken ? "Coordinator revoked." : "Coordinator revoked; no session was holding the role any more."),
    failure: "The coordinator was not revoked",
  }).then(changed);
}

// ---- messages ----------------------------------------------------------------------

export function sendMessage(to: { agent_id: string; name: string; roleName: string }, text: string) {
  return askSigned<SendResult>({
    title: `Send this to ${to.roleName}?`,
    body: `${q(to.name)} gets it with your authority. ${SEND_WAKE}`,
    note: MESSAGE_NOTE,
    tone: "canon",
    confirmLabel: "Send with passkey",
    challenge: () => ({ purpose: "send", to: to.agent_id, text, urgent: false }),
    complete: (s) => maintainerApi.send(s),
    success: (r) => {
      const w = wakeText(r.wake);
      return w ? `Sent to ${to.roleName}; ${w}.` : `Sent to ${to.roleName}.`;
    },
    failure: "Your message was not sent",
  }).then((r) => {
    if (r) void loadThreads();
    return r;
  });
}

export function withdrawMessage(messageId: string) {
  return askSigned<RepudiateResult>({
    title: "Withdraw this message?",
    body: "The session sees it as withdrawn on its next receive and is told not to act on it. If it already read it, a follow-up notice goes out.",
    note: "Your passkey signs the withdrawal.",
    tone: "danger",
    confirmLabel: "Withdraw with passkey",
    challenge: () => ({ purpose: "repudiate", message_id: messageId }),
    complete: (s) => maintainerApi.repudiate(s),
    success: (r) =>
      r.follow_up
        ? "Message withdrawn; it had already been acknowledged, so a follow-up notice went out."
        : "Message withdrawn.",
    failure: "The message was not withdrawn",
  }).then((r) => {
    if (r) void loadThreads();
    return r;
  });
}

// ---- passkeys ----------------------------------------------------------------------

export function cancelKey(k: { credential_id: string; label: string }) {
  return askSigned<{ state: string }>({
    title: `Cancel the new key ${q(k.label)}?`,
    body: "It is still in quarantine, so it has never signed anything. An older active key signs the cancel, and it cannot be undone.",
    note: "Tap an older key when the browser asks.",
    tone: "danger",
    confirmLabel: "Cancel the key with passkey",
    challenge: () => ({ purpose: "cancel", credential_id: k.credential_id }),
    complete: (s) => maintainerApi.cancel(s),
    success: () => `Key ${q(k.label)} cancelled.`,
    failure: "The key was not cancelled",
  }).then(keysChanged);
}

export function revokeOwnKey(k: { credential_id: string; label: string }) {
  return askSigned<{ state: string }>({
    title: `Revoke ${q(k.label)}?`,
    body: "This key signs its own revocation and can never sign again. No key can revoke an older one; only the daemon host can, with pseudolife-mcp maintainer revoke.",
    note: "Tap this key when the browser asks.",
    tone: "danger",
    confirmLabel: "Revoke with this key",
    challenge: () => ({ purpose: "revoke-self", credential_id: k.credential_id }),
    complete: (s) => maintainerApi.revoke(s),
    success: () => `Key ${q(k.label)} revoked.`,
    failure: "The key was not revoked",
  }).then(keysChanged);
}

/** Add another key: an active key approves, then the new key is created and enrolled. */
export function addKey(label: string) {
  return askSigned<EnrolResult>({
    title: "Approve a new passkey?",
    body: `An active key approves ${q(label)}. The browser then asks you to create the new key on the authenticator you choose. It stays in quarantine for 24 hours: it cannot sign anything until then, and any older key can cancel it.`,
    note: "Two prompts: first tap an existing key, then create the new one.",
    tone: "canon",
    confirmLabel: "Approve with passkey",
    challenge: () => ({ purpose: "enrol-approve", label }),
    complete: async (s) => {
      const second = await maintainerApi.approveEnrol(s);
      if (!second || !second.payload || !second.publicKey) {
        throw new ApiError(200, (second as { error?: string })?.error || "invalid_response", null);
      }
      const attestation = await createCredential(second.publicKey, { fixtures: fixtureMode() });
      return maintainerApi.enrol({ payload: second.payload, mac: second.mac, attestation });
    },
    success: (r) => `${q(r.label || label)} was added; it is ${r.state === "active" ? "in its 24-hour quarantine" : r.state}.`,
    failure: "The new key was not added",
  }).then((r) => {
    rememberOwnKey(r?.credential_id);
    return keysChanged(r);
  });
}

/**
 * The first passkey: the one-time code from `pseudolife-mcp maintainer
 * enrol-code`, a label, then the create ceremony. The key stays pending until
 * the host confirms it. No assertion exists yet, so there is no dialog: the
 * form's button is the intent.
 */
export async function enrolBootstrap(code: string, label: string): Promise<EnrolResult | null> {
  try {
    const answer = await maintainerApi.challenge({ purpose: "enrol-bootstrap", label });
    const attestation = await createCredential(answer.publicKey, { fixtures: fixtureMode() });
    const r = await maintainerApi.enrol({ payload: answer.payload, mac: answer.mac, attestation, code });
    const refused = (r as { error?: unknown }).error;
    if (typeof refused === "string" && refused) throw new ApiError(200, refused, null);
    rememberOwnKey(r.credential_id);
    toast(`Passkey ${q(r.label || label)} enrolled; it waits for the host confirm.`, "ok", 6000);
    void loadMaintainer();
    return r;
  } catch (e) {
    toast(failureLine("The passkey was not enrolled", e), "danger", 8000);
    void loadMaintainer();
    return null;
  }
}

export function quarantineEnds(activeFrom: number | null): string {
  return activeFrom ? fmtDateTime(activeFrom) : "";
}
