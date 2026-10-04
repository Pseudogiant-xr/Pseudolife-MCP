// WebAuthn plumbing for the maintainer passkey: base64url codec, the daemon's
// JSON options turned into what navigator.credentials needs, and the
// resulting credential turned back into JSON for the completing route.
//
// The daemon sends `publicKey` as PublicKeyCredentialRequestOptions (or, for
// enrolment, CreationOptions) with base64url strings for every byte field.
// Everything here is pure or takes its browser surface as an argument, so
// vitest can drive it in node with fakes.

// ---- base64url ----------------------------------------------------------------

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const LOOKUP = new Map([...B64].map((c, i) => [c, i]));

/** Bytes as unpadded base64url. */
export function b64urlEncode(data: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    data instanceof ArrayBuffer ? new Uint8Array(data) : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0);
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63];
    if (i + 1 < bytes.length) out += B64[(n >> 6) & 63];
    if (i + 2 < bytes.length) out += B64[n & 63];
  }
  return out;
}

/** base64url (padded or not; plain base64's + and / are accepted too) to bytes. */
export function b64urlDecode(s: string): Uint8Array<ArrayBuffer> {
  if (typeof s !== "string") throw new TypeError("base64url value is not a string");
  const clean = s.replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  if (clean.length % 4 === 1) throw new TypeError("base64url value has an impossible length");
  const out = new Uint8Array(new ArrayBuffer(Math.floor((clean.length * 3) / 4)));
  let bits = 0;
  let acc = 0;
  let j = 0;
  for (const c of clean) {
    const v = LOOKUP.get(c);
    if (v === undefined) throw new TypeError("base64url value has a character outside the alphabet");
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[j++] = (acc >> bits) & 255;
    }
  }
  return out;
}

// ---- options: daemon JSON -> browser ------------------------------------------

type Json = Record<string, unknown>;

function obj(v: unknown, what: string): Json {
  if (!v || typeof v !== "object" || Array.isArray(v)) throw new TypeError(`${what} is not an object`);
  return v as Json;
}

/** Accept the options bare or wrapped once as {publicKey: {...}}. */
function unwrap(json: unknown): Json {
  const o = obj(json, "publicKey");
  if (!("challenge" in o) && o.publicKey && typeof o.publicKey === "object") return obj(o.publicKey, "publicKey");
  return o;
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (list === undefined || list === null) return undefined;
  if (!Array.isArray(list)) throw new TypeError("credential list is not an array");
  return list.map((d) => {
    const o = obj(d, "credential descriptor");
    const out: PublicKeyCredentialDescriptor = {
      type: (o.type as PublicKeyCredentialType) ?? "public-key",
      id: b64urlDecode(o.id as string),
    };
    if (Array.isArray(o.transports)) out.transports = o.transports as AuthenticatorTransport[];
    return out;
  });
}

/** The daemon's JSON request options as navigator.credentials.get({publicKey}) takes them. */
export function requestOptionsFromJSON(json: unknown): PublicKeyCredentialRequestOptions {
  const o = unwrap(json);
  const out: PublicKeyCredentialRequestOptions = {
    ...(o as object),
    challenge: b64urlDecode(o.challenge as string),
  } as PublicKeyCredentialRequestOptions;
  const allow = descriptors(o.allowCredentials);
  if (allow) out.allowCredentials = allow;
  else delete (out as Partial<PublicKeyCredentialRequestOptions>).allowCredentials;
  return out;
}

/** The daemon's JSON creation options as navigator.credentials.create({publicKey}) takes them. */
export function creationOptionsFromJSON(json: unknown): PublicKeyCredentialCreationOptions {
  const o = unwrap(json);
  const user = obj(o.user, "user");
  const out = {
    ...(o as object),
    challenge: b64urlDecode(o.challenge as string),
    user: { ...user, id: b64urlDecode(user.id as string) },
  } as unknown as PublicKeyCredentialCreationOptions;
  const exclude = descriptors(o.excludeCredentials);
  if (exclude) out.excludeCredentials = exclude;
  else delete (out as Partial<PublicKeyCredentialCreationOptions>).excludeCredentials;
  return out;
}

// ---- credential: browser -> daemon JSON ---------------------------------------

/** What the completing routes take as `assertion`. */
export interface AssertionJSON {
  id: string;
  rawId: string;
  type: string;
  response: {
    clientDataJSON: string;
    authenticatorData: string;
    signature: string;
    userHandle: string | null;
  };
  authenticatorAttachment?: string | null;
  clientExtensionResults: Record<string, unknown>;
}

/** What /api/maintainer/enrol takes as `attestation`. */
export interface AttestationJSON {
  id: string;
  rawId: string;
  type: string;
  response: {
    clientDataJSON: string;
    attestationObject: string;
    transports?: string[];
  };
  authenticatorAttachment?: string | null;
  clientExtensionResults: Record<string, unknown>;
}

/** The shape of a PublicKeyCredential this module reads (a real one, or a test fake). */
export interface CredentialLike {
  id: string;
  rawId: ArrayBuffer;
  type: string;
  authenticatorAttachment?: string | null;
  response: {
    clientDataJSON: ArrayBuffer;
    authenticatorData?: ArrayBuffer;
    signature?: ArrayBuffer;
    userHandle?: ArrayBuffer | null;
    attestationObject?: ArrayBuffer;
    getTransports?: () => string[];
  };
  getClientExtensionResults?: () => Record<string, unknown>;
}

function extensions(c: CredentialLike): Record<string, unknown> {
  try {
    return c.getClientExtensionResults?.() ?? {};
  } catch {
    return {};
  }
}

export function assertionToJSON(c: CredentialLike): AssertionJSON {
  const r = c.response;
  if (!r.authenticatorData || !r.signature) throw new TypeError("the credential is not an assertion");
  return {
    id: c.id,
    rawId: b64urlEncode(c.rawId),
    type: c.type,
    response: {
      clientDataJSON: b64urlEncode(r.clientDataJSON),
      authenticatorData: b64urlEncode(r.authenticatorData),
      signature: b64urlEncode(r.signature),
      userHandle: r.userHandle ? b64urlEncode(r.userHandle) : null,
    },
    authenticatorAttachment: c.authenticatorAttachment ?? null,
    clientExtensionResults: extensions(c),
  };
}

export function attestationToJSON(c: CredentialLike): AttestationJSON {
  const r = c.response;
  if (!r.attestationObject) throw new TypeError("the credential is not an attestation");
  const out: AttestationJSON = {
    id: c.id,
    rawId: b64urlEncode(c.rawId),
    type: c.type,
    response: {
      clientDataJSON: b64urlEncode(r.clientDataJSON),
      attestationObject: b64urlEncode(r.attestationObject),
    },
    authenticatorAttachment: c.authenticatorAttachment ?? null,
    clientExtensionResults: extensions(c),
  };
  try {
    const t = r.getTransports?.();
    if (Array.isArray(t)) out.response.transports = t;
  } catch {
    // Optional; some browsers throw on a detached response.
  }
  return out;
}

// ---- support ------------------------------------------------------------------

/** The slice of `window` this module needs; injected so tests can fake it. */
export interface WebAuthnEnv {
  isSecureContext: boolean;
  hasPublicKeyCredential: boolean;
  credentials?: Pick<CredentialsContainer, "get" | "create"> | null;
}

export function browserEnv(): WebAuthnEnv {
  const w = globalThis as unknown as { isSecureContext?: boolean; PublicKeyCredential?: unknown; navigator?: Navigator };
  return {
    isSecureContext: w.isSecureContext === true,
    hasPublicKeyCredential: typeof w.PublicKeyCredential === "function",
    credentials: w.navigator?.credentials ?? null,
  };
}

export type Unsupported = { ok: false; why: "insecure" | "no_webauthn"; message: string };
export type Support = { ok: true } | Unsupported;

/** Whether this browser can run a passkey ceremony here, and if not, why in plain words. */
export function webauthnSupport(env: WebAuthnEnv): Support {
  if (!env.isSecureContext) {
    return {
      ok: false,
      why: "insecure",
      message:
        "This page is not a secure context, so the browser offers no passkeys here. Open the Console over HTTPS at its configured address, or at http://localhost on the daemon host.",
    };
  }
  if (!env.hasPublicKeyCredential || !env.credentials) {
    return {
      ok: false,
      why: "no_webauthn",
      message: "This browser has no WebAuthn support, so it cannot use a passkey. Use a current Chrome, Edge, Safari or Firefox.",
    };
  }
  return { ok: true };
}

// ---- ceremonies ---------------------------------------------------------------

/** A ceremony that did not produce a credential, explained. Nothing was sent. */
export class CeremonyError extends Error {
  readonly code: "cancelled" | "wrong_site" | "already_enrolled" | "unsupported" | "failed";
  constructor(code: CeremonyError["code"], message: string) {
    super(message);
    this.name = "CeremonyError";
    this.code = code;
  }
}

/** A DOMException (or anything else) from navigator.credentials, in plain words. */
export function explainCeremonyFailure(e: unknown, kind: "get" | "create"): CeremonyError {
  if (e instanceof CeremonyError) return e;
  const name = e && typeof e === "object" && "name" in e ? String((e as { name: unknown }).name) : "";
  switch (name) {
    case "NotAllowedError":
    case "AbortError":
      return new CeremonyError(
        "cancelled",
        kind === "get"
          ? "The passkey prompt was closed or timed out. Nothing was signed or sent."
          : "The passkey prompt was closed or timed out. No passkey was created.",
      );
    case "SecurityError":
      return new CeremonyError(
        "wrong_site",
        "The browser refused the passkey for this address. Open the Console at the address configured as coordination.maintainer.origin.",
      );
    case "InvalidStateError":
      return new CeremonyError(
        "already_enrolled",
        "This authenticator already holds a passkey for this Console. Use another one, or revoke the old key first.",
      );
    case "NotSupportedError":
      return new CeremonyError("unsupported", "This authenticator does not support the key types the daemon accepts.");
    default:
      return new CeremonyError("failed", "The passkey ceremony failed before anything was sent.");
  }
}

// A placeholder credential for the fixture devserver only. The demo service
// accepts it; a real daemon verifies every byte and refuses it.
const FIXTURE_BYTES = new TextEncoder().encode("fixture-demo-only");

function fixtureAssertion(): AssertionJSON {
  const b = b64urlEncode(FIXTURE_BYTES);
  return {
    id: b,
    rawId: b,
    type: "public-key",
    response: { clientDataJSON: b, authenticatorData: b, signature: b, userHandle: null },
    authenticatorAttachment: null,
    clientExtensionResults: {},
  };
}

function fixtureAttestation(): AttestationJSON {
  const b = b64urlEncode(FIXTURE_BYTES);
  return {
    id: b,
    rawId: b,
    type: "public-key",
    response: { clientDataJSON: b, attestationObject: b },
    authenticatorAttachment: null,
    clientExtensionResults: {},
  };
}

export interface CeremonyOpts {
  /** The daemon declared fixture data: skip the browser and send a dummy. */
  fixtures?: boolean;
  env?: WebAuthnEnv;
}

/** Ask the authenticator to sign the daemon's request options (the tap). */
export async function getAssertion(publicKey: unknown, opts: CeremonyOpts = {}): Promise<AssertionJSON> {
  if (opts.fixtures) return fixtureAssertion();
  const env = opts.env ?? browserEnv();
  const support = webauthnSupport(env);
  if (!support.ok) throw new CeremonyError("unsupported", support.message);
  let cred: Credential | null;
  try {
    cred = await env.credentials!.get({ publicKey: requestOptionsFromJSON(publicKey) });
  } catch (e) {
    throw explainCeremonyFailure(e, "get");
  }
  if (!cred) throw new CeremonyError("cancelled", "No passkey answered. Nothing was signed or sent.");
  return assertionToJSON(cred as unknown as CredentialLike);
}

/** Create a new passkey from the daemon's creation options. */
export async function createCredential(publicKey: unknown, opts: CeremonyOpts = {}): Promise<AttestationJSON> {
  if (opts.fixtures) return fixtureAttestation();
  const env = opts.env ?? browserEnv();
  const support = webauthnSupport(env);
  if (!support.ok) throw new CeremonyError("unsupported", support.message);
  let cred: Credential | null;
  try {
    cred = await env.credentials!.create({ publicKey: creationOptionsFromJSON(publicKey) });
  } catch (e) {
    throw explainCeremonyFailure(e, "create");
  }
  if (!cred) throw new CeremonyError("cancelled", "No passkey was created.");
  return attestationToJSON(cred as unknown as CredentialLike);
}
