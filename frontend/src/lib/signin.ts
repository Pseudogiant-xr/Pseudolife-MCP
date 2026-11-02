// Console sign-in with a one-time code (docs/specs/2026-11-02-console-owner-signin.md,
// sections 3 and 4). The browser mints its own credential, keeps it as a
// pending token, and sends only its SHA-256 with the code to
// POST /api/console/session. Only a confirmed success moves the pending token
// into "pl_token", so a refused or expired code never touches a working
// sign-in. Everything here takes its fetch, storage and crypto as arguments
// so the state machine is testable without a browser.

import { ApiError } from "./api/client";
import { failureLine } from "./maintainer";
import { findNav, resolveRoute } from "./nav";
import { TOKEN_KEY } from "./storage";
import { b64urlEncode, CeremonyError, webauthnSupport, type AssertionJSON, type WebAuthnEnv } from "./webauthn";

/** Where a minted token waits until the daemon confirms its session. */
export const PENDING_KEY = "pl_token_pending";
/** When the pending token was minted (ms since the epoch). */
export const PENDING_AT_KEY = "pl_token_pending_at";
/**
 * How long a pending token the daemon refuses is kept and asked about again:
 * the daemon's redemption runs inside a 5 s connect and a 5 s statement
 * timeout, so a refusal seen before then may be a sign-in still committing
 * (a design bound from those timeouts, not a measurement).
 */
export const SETTLE_GRACE_MS = 20_000;

export const SESSION_PATH = "/api/console/session";
export const PASSKEY_CHALLENGE_PATH = "/api/console/passkey-challenge";
export const PASSKEY_BIND_PATH = "/api/console/passkey-bind";
export const SESSIONS_PATH = "/api/console/sessions";
export const REVOKE_PATH = "/api/console/sessions/revoke";

/** The command that opens a signed-in Console or prints a code. */
export const SIGNIN_COMMAND = "pseudolife-mcp console";

// Sign-in codes come from the pairing-code generator (principals.py):
// 12 Crockford base32 characters, shown as XXXX-XXXX-XXXX.
export const CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export const CODE_LENGTH = 12;
const LOOKALIKES: Record<string, string> = { O: "0", I: "1", L: "1" };

/**
 * The 12-character canonical form of a typed code, or null when it is not
 * one. Case, dashes and whitespace are ignored, and O reads as 0, I and L as
 * 1, exactly as the daemon's normalize_pairing_code reads them.
 */
export function normalizeCode(text: string | null | undefined): string | null {
  if (typeof text !== "string") return null;
  const code = text
    .replace(/[\s-]+/g, "")
    .toUpperCase()
    .replace(/[OIL]/g, (c) => LOOKALIKES[c] ?? c);
  if (code.length !== CODE_LENGTH) return null;
  for (const c of code) if (!CODE_ALPHABET.includes(c)) return null;
  return code;
}

/** "ABCD-EFGH-JKMN" for a canonical code. */
export function formatCode(code: string): string {
  return code.match(/.{1,4}/g)?.join("-") ?? code;
}

// ---- capability ------------------------------------------------------------

export interface SigninCapability {
  link: boolean;
  passkey: boolean;
}

/**
 * What /health says this daemon offers, or null when it says nothing (an
 * older daemon): then only the pasted token works. Capability is read from
 * here only, never guessed from a 401, 404 or 501.
 */
export function signinCapability(health: unknown): SigninCapability | null {
  if (!health || typeof health !== "object") return null;
  const c = (health as { console_signin?: unknown }).console_signin;
  if (!c || typeof c !== "object") return null;
  const { link, passkey } = c as { link?: unknown; passkey?: unknown };
  return { link: link === true, passkey: passkey === true };
}

/**
 * Whether the dialog offers "Sign in with your passkey": the daemon says it
 * signs in with passkeys, and this page can run a passkey ceremony (a secure
 * context with WebAuthn). The demo's fixture mode needs no browser support.
 */
export function passkeyOffered(cap: SigninCapability | null, env: WebAuthnEnv | null, fixtures = false): boolean {
  if (cap?.passkey !== true) return false;
  return fixtures || (!!env && webauthnSupport(env).ok);
}

/**
 * What the sign-in dialog offers: the code form only when the daemon says it
 * redeems links, a passkey only when it says so and this page can use one
 * (`env`; none given, none offered), and otherwise the pasted token alone,
 * opened rather than tucked under Advanced.
 */
export function signinChoices(
  cap: SigninCapability | null,
  env: WebAuthnEnv | null = null,
  fixtures = false,
): { code: boolean; passkey: boolean; pasteOnly: boolean } {
  const code = cap?.link === true;
  const passkey = passkeyOffered(cap, env, fixtures);
  return { code, passkey, pasteOnly: !code && !passkey };
}

// ---- the link -----------------------------------------------------------------

/**
 * The route a sign-in link may land on: a sidebar route or one of the
 * classic aliases the router already accepts, resolved; anything else is
 * ignored (null), so a link cannot send the Console somewhere unexpected.
 */
export function signinTarget(to: string | null | undefined): string | null {
  if (typeof to !== "string" || !to) return null;
  const id = resolveRoute(to);
  return findNav(id) ? id : null;
}

/** The hash a finished sign-in lands on: the target, else the default route. */
export function landingHash(target: string | null): string {
  return target ? `#/${target}` : "#/";
}

// ---- crypto -------------------------------------------------------------------

/** Lowercase hex of a byte buffer. */
export function toHex(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let out = "";
  for (const b of bytes) out += b.toString(16).padStart(2, "0");
  return out;
}

/** True when this page can mint and hash a token (a secure context). */
export function canMint(c: Crypto | undefined): c is Crypto {
  return !!c && typeof c.getRandomValues === "function" && !!c.subtle && typeof c.subtle.digest === "function";
}

/** 32 random bytes as unpadded base64url (43 characters). */
export function mintToken(c: Crypto): string {
  const bytes = new Uint8Array(32);
  c.getRandomValues(bytes);
  return b64urlEncode(bytes);
}

/** SHA-256 of the token's UTF-8 bytes, lowercase hex, as the daemon stores it. */
export async function sha256Hex(c: Crypto, text: string): Promise<string> {
  return toHex(await c.subtle.digest("SHA-256", new TextEncoder().encode(text)));
}

// ---- the label ----------------------------------------------------------------

/** "Firefox on Windows": how Settings lists this browser. At most 64 characters. */
export function browserLabel(ua: string | null | undefined): string {
  const s = ua ?? "";
  const browser = /Edg(e|A|iOS)?\//.test(s)
    ? "Edge"
    : /OPR\/|Opera/.test(s)
      ? "Opera"
      : /Firefox\/|FxiOS\//.test(s)
        ? "Firefox"
        : /Chrome\/|CriOS\/|Chromium\//.test(s)
          ? "Chrome"
          : /Safari\//.test(s)
            ? "Safari"
            : "";
  const os = /Windows/.test(s)
    ? "Windows"
    : /Android/.test(s)
      ? "Android"
      : /iPhone|iPod/.test(s)
        ? "iPhone"
        : /iPad/.test(s)
          ? "iPad"
          : /CrOS/.test(s)
            ? "ChromeOS"
            : /Mac OS X|Macintosh/.test(s)
              ? "macOS"
              : /Linux|X11/.test(s)
                ? "Linux"
                : "";
  const label = browser && os ? `${browser} on ${os}` : browser || (os ? `A browser on ${os}` : "A browser");
  return label.slice(0, 64);
}

// ---- the state machine -------------------------------------------------------

export interface KeyStore {
  read(key: string): string;
  write(key: string, value: string): void;
}

export interface SigninDeps {
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  store: KeyStore;
  crypto: Crypto | undefined;
  userAgent: string;
  /** Milliseconds since the epoch (Date.now when absent). */
  now?: () => number;
  /** Wait before asking again (setTimeout when absent). */
  sleep?: (ms: number) => Promise<void>;
}

const nowOf = (deps: Pick<SigninDeps, "now">): number => (deps.now ?? Date.now)();
const sleepOf = (deps: Pick<SigninDeps, "sleep">, ms: number): Promise<void> =>
  (deps.sleep ?? ((t: number) => new Promise<void>((resolve) => setTimeout(resolve, t))))(ms);

function dropPending(store: KeyStore): void {
  store.write(PENDING_KEY, "");
  store.write(PENDING_AT_KEY, "");
}

export type RefusalReason =
  | "bad_code"
  | "refused"
  | "forbidden_origin"
  | "rate_limited"
  | "unavailable"
  | "not_completed"
  | "passkey_unavailable"
  | "no_answer"
  | "ceremony"
  | "http";

export type RedeemResult =
  | { kind: "signed_in"; principal: string | null }
  /** Nothing changed: pl_token is untouched and no pending token is left. */
  | { kind: "refused"; reason: RefusalReason; message: string }
  /** The answer was lost and the daemon could not be asked: the pending
   *  token is kept, and the next start of the Console settles it. */
  | { kind: "unconfirmed"; message: string }
  /** This page cannot mint a token (not a secure context). */
  | { kind: "unsupported"; message: string }
  /** The person closed the passkey prompt: nothing was sent or changed. */
  | { kind: "cancelled"; message: string };

export const MESSAGES = {
  badCode: "A sign-in code has 12 letters and digits, like ABCD-EFGH-JKMN. Check it and try again.",
  refused: `That code has expired, was already used, was mistyped, or was made for another address of this Console. Run \`${SIGNIN_COMMAND}\` again for a new one, from where you use this Console.`,
  forbiddenOrigin:
    "This code was made for another address of this Console. Open the link at the address it names, or run " +
    `\`${SIGNIN_COMMAND}\` again from where you want to use the Console.`,
  rateLimited: "Too many sign-in attempts in a short time. Try again in a minute.",
  unavailable:
    "This daemon cannot sign browsers in right now. Try again in a moment; if it keeps failing, check the daemon log.",
  notCompleted: `The sign-in did not complete. Run \`${SIGNIN_COMMAND}\` again for a new code.`,
  unconfirmed:
    "The daemon did not answer, so this sign-in is not confirmed yet. The Console checks again when you reload it.",
  unsupported:
    "Sign-in needs this Console at https or on this machine (localhost); open it there, or use Advanced.",
  cancelled: "Sign-in cancelled.",
  passkeyForbiddenOrigin:
    "Passkey sign-in works at your Console's HTTPS address; open the Console there, or sign in with a code.",
  passkeyRefused: "The passkey sign-in was not accepted. Try again; if it keeps failing, sign in with a code.",
  /** Only when the daemon's 409 carries no hint of its own. */
  passkeyUnavailable:
    `No passkey can sign this Console in yet. Sign in on the daemon's host with \`${SIGNIN_COMMAND}\`, then in ` +
    'Settings, Your passkeys, choose "Use this passkey to sign in".',
  passkeyNoAnswer: "The daemon did not answer, so nothing changed. Check that it is running, then try again.",
  passkeyNotCompleted: "The passkey sign-in did not complete. Try again; if it keeps failing, sign in with a code.",
} as const;

export type Probe =
  | { state: "works"; principal: string | null }
  | { state: "refused" }
  | { state: "unknown" };

/**
 * Ask a bearer-gated read whether a token signs in: 200 works (with the
 * principal of the token's own session, when it is one), 401 and 404 do not
 * (no session exists for it), anything else is unknown. Never sends
 * "pl_token" unless it is the token being asked about.
 */
export async function probeToken(token: string, deps: Pick<SigninDeps, "fetch">): Promise<Probe> {
  let res: Response;
  try {
    res = await deps.fetch(SESSIONS_PATH, {
      method: "GET",
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    return { state: "unknown" };
  }
  if (res.status === 401 || res.status === 404) return { state: "refused" };
  if (!res.ok) return { state: "unknown" };
  const body = await readJson(res);
  return { state: "works", principal: currentPrincipal(body) };
}

function currentPrincipal(body: unknown): string | null {
  const sessions = (body as { sessions?: unknown } | null)?.sessions;
  if (!Array.isArray(sessions)) return null;
  const mine = sessions.find((s) => s && typeof s === "object" && (s as { current?: unknown }).current === true) as
    | { principal?: unknown }
    | undefined;
  return typeof mine?.principal === "string" && mine.principal ? mine.principal : null;
}

async function readJson(res: Response): Promise<unknown> {
  try {
    const text = await res.text();
    return text ? (JSON.parse(text) as unknown) : null;
  } catch {
    return null;
  }
}

function promote(store: KeyStore, token: string): void {
  store.write(TOKEN_KEY, token);
  dropPending(store);
}

function refusal(status: number, code: string): { reason: RefusalReason; message: string } {
  if (status === 400 || code === "signin_refused") return { reason: "refused", message: MESSAGES.refused };
  if (status === 403 && code === "forbidden_origin") return { reason: "forbidden_origin", message: MESSAGES.forbiddenOrigin };
  if (status === 429) return { reason: "rate_limited", message: MESSAGES.rateLimited };
  return {
    reason: "http",
    message: `The daemon refused the sign-in (HTTP ${status}${code ? `, ${code}` : ""}). Reload the Console and try again.`,
  };
}

type Refuse = (status: number, code: string, body: unknown) => { reason: RefusalReason; message: string };

function errorOf(body: unknown): string {
  const e = (body as { error?: unknown } | null)?.error;
  return typeof e === "string" ? e : "";
}

function hintOf(body: unknown): string {
  const h = (body as { hint?: unknown } | null)?.hint;
  return typeof h === "string" ? h.trim() : "";
}

/** Keep a minted token as the pending one, stamped now. */
function keepPending(store: KeyStore, token: string, deps: Pick<SigninDeps, "now">): void {
  store.write(PENDING_KEY, token);
  store.write(PENDING_AT_KEY, String(nowOf(deps)));
}

/**
 * POST /api/console/session for the pending token (already stored) and read
 * the answer, for the code and the passkey alike:
 * - 200 with the session's principal: the pending token becomes "pl_token";
 * - a definite refusal (any 4xx): the pending token is dropped and
 *   "pl_token" is untouched, and `refuse` words it;
 * - an ambiguous answer (no response, any 5xx, a 200 without a body): the
 *   pending token is tested on a bearer-gated read and promoted only if it
 *   works; if the daemon cannot be asked either, it stays pending. The
 *   daemon's 503 signin_unavailable is ambiguous too: it answers that when
 *   redeeming raised, so the session may exist.
 */
async function postSession(
  token: string,
  body: Record<string, unknown>,
  deps: SigninDeps,
  refuse: Refuse,
  notCompleted: string,
): Promise<RedeemResult> {
  let res: Response | null = null;
  try {
    // Pre-auth: no Authorization header, even when this browser holds a token.
    res = await deps.fetch(SESSION_PATH, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    res = null;
  }

  const ifRefused = { reason: "not_completed" as RefusalReason, message: notCompleted };
  if (res) {
    const answer = await readJson(res);
    const errCode = errorOf(answer);
    if (res.ok) {
      const principal = (answer as { principal?: unknown } | null)?.principal;
      if (typeof principal === "string" && principal) {
        promote(deps.store, token);
        return { kind: "signed_in", principal };
      }
      // A 200 without the session's body is an answer we cannot read: test it.
    } else if (res.status >= 400 && res.status < 500) {
      dropPending(deps.store);
      return { kind: "refused", ...refuse(res.status, errCode, answer) };
    } else if (errCode === "signin_unavailable") {
      return settleToken(token, deps, { reason: "unavailable", message: MESSAGES.unavailable });
    }
  }
  return settleToken(token, deps, ifRefused);
}

/** Redeem a typed or linked code (see postSession for the outcomes). */
export async function redeem(rawCode: string, deps: SigninDeps): Promise<RedeemResult> {
  const code = normalizeCode(rawCode);
  if (!code) return { kind: "refused", reason: "bad_code", message: MESSAGES.badCode };
  if (!canMint(deps.crypto)) return { kind: "unsupported", message: MESSAGES.unsupported };

  const token = mintToken(deps.crypto);
  const tokenSha = await sha256Hex(deps.crypto, token);
  keepPending(deps.store, token, deps);
  return postSession(
    token,
    { code, token_sha256: tokenSha, label: browserLabel(deps.userAgent) },
    deps,
    (status, errCode) => refusal(status, errCode),
    MESSAGES.notCompleted,
  );
}

// ---- the passkey -------------------------------------------------------------

/** The passkey's step: sign the daemon's request options (webauthn.ts getAssertion). */
export type SignAssertion = (publicKey: unknown) => Promise<AssertionJSON>;

interface PasskeyChallenge {
  payload: string;
  mac: string;
  publicKey: Record<string, unknown>;
}

function passkeyRefusal(status: number, code: string, body: unknown, step: "challenge" | "session") {
  if (code === "passkey_signin_unavailable" || (status === 409 && step === "session")) {
    return { reason: "passkey_unavailable" as const, message: hintOf(body) || MESSAGES.passkeyUnavailable };
  }
  if (status === 403 && code === "forbidden_origin") {
    return { reason: "forbidden_origin" as const, message: MESSAGES.passkeyForbiddenOrigin };
  }
  if (status === 429) return { reason: "rate_limited" as const, message: MESSAGES.rateLimited };
  if (step === "session" && (status === 400 || code === "signin_refused")) {
    return { reason: "refused" as const, message: MESSAGES.passkeyRefused };
  }
  if (step === "challenge" && code === "signin_unavailable") {
    return { reason: "unavailable" as const, message: MESSAGES.unavailable };
  }
  return {
    reason: "http" as const,
    message: `The daemon refused the passkey sign-in (HTTP ${status}${code ? `, ${code}` : ""}). Reload the Console and try again.`,
  };
}

/**
 * Sign in with a passkey (spec section 5). The token is minted first and
 * only its SHA-256 goes into the challenge, so the signature names exactly
 * the token the session will accept. Nothing is stored until the passkey has
 * signed: a refused challenge or a closed prompt leaves this browser as it
 * was. From just before the signed POST the token is the pending one, and
 * the answer is read exactly as a code's (postSession).
 */
export async function redeemPasskey(
  deps: SigninDeps & { sign: SignAssertion; cancelled?: () => boolean },
): Promise<RedeemResult> {
  // The dialog closed meanwhile: stop before the passkey prompt opens and
  // before anything is posted, with nothing changed.
  const stopped = (): boolean => !!deps.cancelled?.();
  if (!canMint(deps.crypto)) return { kind: "unsupported", message: MESSAGES.unsupported };
  const token = mintToken(deps.crypto);
  const tokenSha = await sha256Hex(deps.crypto, token);

  // Step 1: the challenge. No session can exist yet, so every failure here
  // is definite, a lost answer included.
  let res: Response;
  try {
    res = await deps.fetch(PASSKEY_CHALLENGE_PATH, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ token_sha256: tokenSha, label: browserLabel(deps.userAgent) }),
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    return { kind: "refused", reason: "no_answer", message: MESSAGES.passkeyNoAnswer };
  }
  const answer = await readJson(res);
  if (!res.ok) return { kind: "refused", ...passkeyRefusal(res.status, errorOf(answer), answer, "challenge") };
  const ch = answer as Partial<PasskeyChallenge> | null;
  if (!ch || typeof ch.payload !== "string" || typeof ch.mac !== "string" || !ch.publicKey || typeof ch.publicKey !== "object") {
    const code = errorOf(answer);
    return { kind: "refused", ...passkeyRefusal(res.status, code || "invalid_response", answer, "challenge") };
  }

  if (stopped()) return { kind: "cancelled", message: MESSAGES.cancelled };

  // Step 2: the passkey signs. A closed prompt is the person's choice, said quietly.
  let assertion: AssertionJSON;
  try {
    assertion = await deps.sign(ch.publicKey);
  } catch (e) {
    if (e instanceof CeremonyError && e.code === "cancelled") return { kind: "cancelled", message: MESSAGES.cancelled };
    const message =
      e instanceof CeremonyError ? e.message : "The passkey prompt failed before anything was sent. Try again.";
    return { kind: "refused", reason: "ceremony", message };
  }

  if (stopped()) return { kind: "cancelled", message: MESSAGES.cancelled };

  // Step 3: the signed session request, for the token now kept as pending.
  keepPending(deps.store, token, deps);
  return postSession(
    token,
    { payload: ch.payload, mac: ch.mac, assertion },
    deps,
    (status, errCode, body) => passkeyRefusal(status, errCode, body, "session"),
    MESSAGES.passkeyNotCompleted,
  );
}

async function settleToken(
  token: string,
  deps: SigninDeps,
  ifRefused: { reason: RefusalReason; message: string } = { reason: "not_completed", message: MESSAGES.notCompleted },
): Promise<RedeemResult> {
  // A refusal soon after the token was minted may be a redemption still
  // committing on the daemon: ask again, backing off, until the grace has
  // passed, and only then drop the token (its code is spent or expired).
  const minted = Number(deps.store.read(PENDING_AT_KEY));
  let wait = 1_000;
  for (;;) {
    const probe = await probeToken(token, deps);
    if (probe.state === "works") {
      promote(deps.store, token);
      return { kind: "signed_in", principal: probe.principal };
    }
    if (probe.state === "unknown") return { kind: "unconfirmed", message: MESSAGES.unconfirmed };
    const left = Number.isFinite(minted) && minted > 0 ? minted + SETTLE_GRACE_MS - nowOf(deps) : 0;
    if (left <= 0) {
      dropPending(deps.store);
      return { kind: "refused", ...ifRefused };
    }
    await sleepOf(deps, Math.min(wait, left));
    wait *= 2;
  }
}

export type PendingSettle = "none" | "promoted" | "dropped" | "kept";

/**
 * On start: a pending token left by an interrupted sign-in is tested and
 * promoted if it works, dropped if the daemon refuses it, and kept for the
 * next start if the daemon cannot be asked.
 */
export async function settlePending(deps: SigninDeps): Promise<PendingSettle> {
  const token = deps.store.read(PENDING_KEY);
  if (!token) return "none";
  const r = await settleToken(token, deps);
  return r.kind === "signed_in" ? "promoted" : r.kind === "refused" ? "dropped" : "kept";
}

/** Whether this browser's current token works, and as whom, before a link replaces it. */
export async function currentSignin(deps: Pick<SigninDeps, "fetch" | "store">): Promise<Probe | null> {
  const token = deps.store.read(TOKEN_KEY);
  if (!token) return null;
  return probeToken(token, deps);
}

/**
 * The question asked before a link signs a browser in that holds no working
 * sign-in: a link someone else sent could otherwise sign this browser in as
 * them without a click.
 */
export const LINK_QUESTION =
  "Sign in to this Console with the link you opened? Continue only if you asked for it yourself, " +
  `by running \`${SIGNIN_COMMAND}\` or from the installer.`;

/** The question asked before a link replaces a working sign-in. */
export function replaceQuestion(principal: string | null): string {
  return `This browser is already signed in as ${principal ?? "another account"}. Replace that sign-in?`;
}

// ---- "Use this passkey to sign in" (Settings, Your passkeys) ------------------

/** What the bind action is for, under its button. */
export const BIND_EXPLAINER =
  "Lets the passkey you choose sign this Console in, as the account this Console is signed in as now. " +
  `Use it once, after signing in with \`${SIGNIN_COMMAND}\` on the daemon's host.`;

export interface BindResult {
  credential_id: string;
  signs_in_as: string;
}

export function bindSuccess(r: BindResult): string {
  return `This passkey now signs the Console in as ${r.signs_in_as}.`;
}

const BIND_FAILURE = "The passkey was not set to sign in";

/**
 * Why binding failed, as a toast line. The maintainer routes' codes keep
 * their maintainer.ts wording, except two that mean something specific here.
 */
export function bindFailureLine(e: unknown): string {
  if (e instanceof ApiError) {
    const reason = (e.body as { reason?: unknown } | null)?.reason;
    if (e.code === "principal_not_allowed" && reason === "not_owner") {
      return (
        `${BIND_FAILURE}. Only the bank owner's sign-in can do this: sign in with \`${SIGNIN_COMMAND}\` ` +
        "on the daemon's host, then try again."
      );
    }
    if (e.code === "unauthorized" || e.status === 401) {
      return `${BIND_FAILURE}: this Console's sign-in was not accepted. Sign in again, then try again.`;
    }
  }
  return failureLine(BIND_FAILURE, e);
}

// ---- Settings' list ---------------------------------------------------------

export interface ConsoleSession {
  session_id: string;
  principal: string;
  method: "link" | "passkey" | string;
  label: string;
  created_at: number;
  last_used_at: number | null;
  expires_at: number;
  current: boolean;
}

export interface ConsoleSessions {
  sessions: ConsoleSession[];
  /** False: this Console was signed in from a paired machine's token, so
   *  it cannot change the daemon's settings. */
  operator: boolean;
  /** False when this daemon cannot hold sessions (no token, no Postgres bank). */
  available?: boolean;
}

export function methodText(method: string): string {
  if (method === "link") return "sign-in code";
  if (method === "passkey") return "passkey";
  return method;
}

/** This browser first, then the most recently used. */
export function sortSessions(list: ConsoleSession[]): ConsoleSession[] {
  return [...list].sort(
    (a, b) =>
      Number(b.current) - Number(a.current) ||
      (b.last_used_at ?? b.created_at) - (a.last_used_at ?? a.created_at),
  );
}
