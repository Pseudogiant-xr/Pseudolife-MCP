import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import {
  BIND_EXPLAINER,
  bindFailureLine,
  bindSuccess,
  browserLabel,
  canMint,
  currentSignin,
  formatCode,
  landingHash,
  mintToken,
  normalizeCode,
  PASSKEY_CHALLENGE_PATH,
  passkeyOffered,
  PENDING_AT_KEY,
  PENDING_KEY,
  SETTLE_GRACE_MS,
  LINK_QUESTION,
  redeem,
  redeemPasskey,
  replaceQuestion,
  SESSION_PATH,
  SESSIONS_PATH,
  settlePending,
  sha256Hex,
  signinCapability,
  signinChoices,
  signinTarget,
  sortSessions,
  toHex,
  type ConsoleSession,
  type SigninDeps,
} from "./signin";
import { TOKEN_KEY } from "./storage";
import { b64urlDecode, CeremonyError, type AssertionJSON, type WebAuthnEnv } from "./webauthn";

const CODE = "ABCD-EFGH-JKMN";
const OLD = "the-owners-working-token";

function memStore(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return {
    map: m,
    read: (k: string) => m.get(k) ?? "",
    write: (k: string, v: string) => (v ? m.set(k, v) : m.delete(k)),
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

type Route = (init: RequestInit) => Response | Promise<Response>;

/** A fetch that answers the challenge POST, the session POST and the sessions GET separately. */
function fakeFetch(routes: { challenge?: Route; post?: Route; get?: Route }) {
  return vi.fn(async (url: string, init: RequestInit) => {
    if (url === PASSKEY_CHALLENGE_PATH && init.method === "POST" && routes.challenge) return routes.challenge(init);
    if (url === SESSION_PATH && init.method === "POST" && routes.post) return routes.post(init);
    if (url === SESSIONS_PATH && init.method === "GET" && routes.get) return routes.get(init);
    throw new Error(`unexpected ${init.method} ${url}`);
  });
}

function deps(fetch: SigninDeps["fetch"], store = memStore({ [TOKEN_KEY]: OLD })): SigninDeps & { store: ReturnType<typeof memStore> } {
  return {
    fetch,
    store,
    crypto: globalThis.crypto,
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
    // A fake clock: every wait advances it, so the settle grace runs instantly.
    now: () => clock.t,
    sleep: async (ms: number) => {
      clock.t += ms;
    },
  };
}

const clock = { t: 1_800_000_000_000 };

const network: Route = () => {
  throw new TypeError("Failed to fetch");
};

const sessionsFor = (principal: string) => () =>
  json(200, {
    sessions: [{ session_id: "s1", principal, method: "link", label: "x", created_at: 1, last_used_at: null, expires_at: 9, current: true }],
    operator: true,
  });

function authOf(init: RequestInit): string | undefined {
  return (init.headers as Record<string, string>).Authorization;
}

describe("normalizeCode", () => {
  it("accepts the printed form, lower case, no dashes and stray spaces", () => {
    expect(normalizeCode("ABCD-EFGH-JKMN")).toBe("ABCDEFGHJKMN");
    expect(normalizeCode("abcd-efgh-jkmn")).toBe("ABCDEFGHJKMN");
    expect(normalizeCode(" abcdefghjkmn ")).toBe("ABCDEFGHJKMN");
    expect(normalizeCode("abcd efgh jkmn")).toBe("ABCDEFGHJKMN");
  });

  it("reads O as 0 and I or L as 1, as the daemon does", () => {
    expect(normalizeCode("OIL0-1234-5678")).toBe("011012345678");
  });

  it("refuses a wrong length or a character outside the alphabet", () => {
    expect(normalizeCode("ABCD-EFGH-JKM")).toBeNull();
    expect(normalizeCode("ABCD-EFGH-JKMNP")).toBeNull();
    expect(normalizeCode("ABCD-EFGH-JKMU")).toBeNull(); // U is not in Crockford base32
    expect(normalizeCode("")).toBeNull();
    expect(normalizeCode(undefined)).toBeNull();
  });

  it("formats a canonical code in groups of four", () => {
    expect(formatCode("ABCDEFGHJKMN")).toBe(CODE);
  });
});

describe("token minting", () => {
  it("mints 32 random bytes as unpadded base64url", () => {
    const t = mintToken(globalThis.crypto);
    expect(t).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(b64urlDecode(t)).toHaveLength(32);
    expect(mintToken(globalThis.crypto)).not.toBe(t);
  });

  it("hashes the token to lowercase SHA-256 hex", async () => {
    // SHA-256("abc"), the FIPS 180-2 test vector.
    expect(await sha256Hex(globalThis.crypto, "abc")).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    expect(toHex(new Uint8Array([0, 15, 255]))).toBe("000fff");
  });

  it("needs crypto.subtle (a secure context)", () => {
    expect(canMint(globalThis.crypto)).toBe(true);
    expect(canMint(undefined)).toBe(false);
    expect(canMint({ getRandomValues: globalThis.crypto.getRandomValues } as unknown as Crypto)).toBe(false);
  });
});

describe("signinTarget", () => {
  it("accepts a sidebar route and the router's aliases", () => {
    expect(signinTarget("board")).toBe("board");
    expect(signinTarget("settings")).toBe("settings");
    expect(signinTarget("coordination")).toBe("board");
  });

  it("ignores anything else", () => {
    for (const to of ["", null, undefined, "signin", "nope", "board?x=1", "//evil.example", "https://evil.example", "../board"]) {
      expect(signinTarget(to)).toBeNull();
    }
  });

  it("lands on the target or the default route", () => {
    expect(landingHash("board")).toBe("#/board");
    expect(landingHash(null)).toBe("#/");
  });
});

const SECURE: WebAuthnEnv = {
  isSecureContext: true,
  hasPublicKeyCredential: true,
  credentials: { get: async () => null, create: async () => null },
};

describe("signinCapability", () => {
  it("is null when /health says nothing (an older daemon)", () => {
    expect(signinCapability({ status: "ok" })).toBeNull();
    expect(signinCapability(null)).toBeNull();
    expect(signinCapability({ status: "ok", console_signin: "yes" })).toBeNull();
  });

  it("offers only the pasted token when the capability is absent or link is off", () => {
    expect(signinChoices(signinCapability({ status: "ok" }))).toEqual({ code: false, passkey: false, pasteOnly: true });
    expect(signinChoices({ link: false, passkey: true })).toEqual({ code: false, passkey: false, pasteOnly: true });
    expect(signinChoices({ link: true, passkey: false })).toEqual({ code: true, passkey: false, pasteOnly: false });
    expect(signinChoices({ link: false, passkey: true }, SECURE)).toEqual({ code: false, passkey: true, pasteOnly: false });
    expect(signinChoices({ link: true, passkey: true }, SECURE)).toEqual({ code: true, passkey: true, pasteOnly: false });
  });

  it("reads link and passkey as strict booleans", () => {
    expect(signinCapability({ console_signin: { link: true, passkey: false } })).toEqual({ link: true, passkey: false });
    expect(signinCapability({ console_signin: { link: "true" } })).toEqual({ link: false, passkey: false });
  });
});

describe("browserLabel", () => {
  it("names the browser and the OS", () => {
    expect(browserLabel("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0")).toBe(
      "Firefox on Windows",
    );
    expect(
      browserLabel(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
      ),
    ).toBe("Safari on macOS");
    expect(
      browserLabel("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0"),
    ).toBe("Edge on Linux");
    expect(
      browserLabel("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"),
    ).toBe("Chrome on Android");
  });

  it("falls back plainly and stays within 64 characters", () => {
    expect(browserLabel("")).toBe("A browser");
    expect(browserLabel("curl/8.0")).toBe("A browser");
    expect(browserLabel("x".repeat(500)).length).toBeLessThanOrEqual(64);
  });
});

describe("redeem", () => {
  it("posts exactly {code, token_sha256, label} with no Authorization, then promotes the pending token", async () => {
    let sent: Record<string, unknown> = {};
    let headers: Record<string, string> = {};
    const d = deps(
      fakeFetch({
        post: (init) => {
          sent = JSON.parse(String(init.body)) as Record<string, unknown>;
          headers = init.headers as Record<string, string>;
          // The pending token is stored before the request, never in pl_token.
          expect(d.store.read(PENDING_KEY)).toMatch(/^[A-Za-z0-9_-]{43}$/);
          expect(d.store.read(TOKEN_KEY)).toBe(OLD);
          return json(200, { principal: "owner-pc", session_id: "s1", expires_at: 2_000_000_000 });
        },
      }),
    );
    const pending = () => d.store.map.get(PENDING_KEY);
    const r = await redeem("abcd efgh jkmn", d);

    expect(r).toEqual({ kind: "signed_in", principal: "owner-pc" });
    expect(Object.keys(sent).sort()).toEqual(["code", "label", "token_sha256"]);
    expect(sent.code).toBe("ABCDEFGHJKMN");
    expect(sent.label).toBe("Firefox on Windows");
    expect(sent.token_sha256).toBe(await sha256Hex(globalThis.crypto, d.store.read(TOKEN_KEY)));
    expect(sent.token_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(headers.Authorization).toBeUndefined();
    expect(headers["Content-Type"]).toBe("application/json");
    expect(d.store.read(TOKEN_KEY)).not.toBe(OLD);
    expect(pending()).toBeUndefined();
  });

  it.each([
    [400, "signin_refused", "refused", /expired, was already used, was mistyped, or was made for another address.*pseudolife-mcp console/],
    [403, "forbidden_origin", "forbidden_origin", /another address of this Console/],
    [429, "rate_limited", "rate_limited", /in a minute/],
    [413, "http_413", "http", /HTTP 413/],
    [415, "http_415", "http", /HTTP 415/],
  ])("a definite refusal (%i %s) keeps the old token and drops the pending one", async (status, error, reason, msg) => {
    const get = vi.fn(sessionsFor("someone"));
    const d = deps(fakeFetch({ post: () => json(status, { error }), get }));
    const r = await redeem(CODE, d);
    expect(r.kind).toBe("refused");
    if (r.kind !== "refused") return;
    expect(r.reason).toBe(reason);
    expect(r.message).toMatch(msg);
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(get).not.toHaveBeenCalled();
  });

  it("a malformed code never reaches the daemon", async () => {
    const f = fakeFetch({});
    const d = deps(f);
    const r = await redeem("ABCD-EFGH", d);
    expect(r).toMatchObject({ kind: "refused", reason: "bad_code" });
    expect(f).not.toHaveBeenCalled();
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("stops before minting without crypto.subtle", async () => {
    const f = fakeFetch({});
    const d = { ...deps(f), crypto: undefined };
    const r = await redeem(CODE, d);
    expect(r.kind).toBe("unsupported");
    if (r.kind === "unsupported") expect(r.message).toMatch(/https or on this machine \(localhost\).*Advanced/);
    expect(f).not.toHaveBeenCalled();
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("an ambiguous answer tests the pending token and promotes it when it works", async () => {
    let probed = "";
    const d = deps(
      fakeFetch({
        post: network,
        get: (init) => {
          probed = authOf(init) ?? "";
          return sessionsFor("owner-pc")();
        },
      }),
    );
    const r = await redeem(CODE, d);
    expect(r).toEqual({ kind: "signed_in", principal: "owner-pc" });
    // The probe carried the pending token, not the old one.
    expect(probed).toBe(`Bearer ${d.store.read(TOKEN_KEY)}`);
    expect(d.store.read(TOKEN_KEY)).not.toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("a 5xx is ambiguous too, and a pending token the daemon refuses is dropped", async () => {
    const d = deps(fakeFetch({ post: () => json(502, { error: "bad_gateway" }), get: () => json(401, { error: "unauthorized" }) }));
    const r = await redeem(CODE, d);
    expect(r).toMatchObject({ kind: "refused", reason: "not_completed" });
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("503 signin_unavailable is ambiguous: the daemon may have made the session before failing", async () => {
    const made = deps(fakeFetch({ post: () => json(503, { error: "signin_unavailable" }), get: sessionsFor("owner-pc") }));
    expect(await redeem(CODE, made)).toEqual({ kind: "signed_in", principal: "owner-pc" });

    const notMade = deps(
      fakeFetch({ post: () => json(503, { error: "signin_unavailable" }), get: () => json(401, { error: "unauthorized" }) }),
    );
    const r = await redeem(CODE, notMade);
    expect(r).toMatchObject({ kind: "refused", reason: "unavailable" });
    if (r.kind === "refused") expect(r.message).toMatch(/cannot sign browsers in right now/);
    expect(notMade.store.read(TOKEN_KEY)).toBe(OLD);
    expect(notMade.store.read(PENDING_KEY)).toBe("");
  });

  it("a 200 without the session's body is tested rather than trusted", async () => {
    const get = vi.fn(sessionsFor("owner-pc"));
    const d = deps(fakeFetch({ post: () => new Response("", { status: 200 }), get }));
    const r = await redeem(CODE, d);
    expect(get).toHaveBeenCalledOnce();
    expect(r.kind).toBe("signed_in");
  });

  it("when neither answer arrives, the pending token is kept for the next start", async () => {
    const d = deps(fakeFetch({ post: network, get: network }));
    const r = await redeem(CODE, d);
    expect(r.kind).toBe("unconfirmed");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });
});

describe("settlePending", () => {
  it("does nothing without a pending token", async () => {
    const f = fakeFetch({});
    expect(await settlePending(deps(f))).toBe("none");
    expect(f).not.toHaveBeenCalled();
  });

  it("promotes a pending token that works", async () => {
    const d = deps(fakeFetch({ get: sessionsFor("owner-pc") }), memStore({ [TOKEN_KEY]: OLD, [PENDING_KEY]: "new-token" }));
    expect(await settlePending(d)).toBe("promoted");
    expect(d.store.read(TOKEN_KEY)).toBe("new-token");
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("drops one the daemon refuses and keeps the old token", async () => {
    const d = deps(
      fakeFetch({ get: () => json(401, { error: "unauthorized" }) }),
      memStore({ [TOKEN_KEY]: OLD, [PENDING_KEY]: "new-token" }),
    );
    expect(await settlePending(d)).toBe("dropped");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("keeps it while the daemon cannot be asked", async () => {
    const d = deps(fakeFetch({ get: () => json(503, { error: "x" }) }), memStore({ [PENDING_KEY]: "new-token" }));
    expect(await settlePending(d)).toBe("kept");
    expect(d.store.read(PENDING_KEY)).toBe("new-token");
    expect(d.store.read(TOKEN_KEY)).toBe("");
  });
});

describe("currentSignin", () => {
  it("is null without a stored token", async () => {
    expect(await currentSignin(deps(fakeFetch({}), memStore()))).toBeNull();
  });

  it("names the principal of a working session, and asks before replacing it", async () => {
    const p = await currentSignin(deps(fakeFetch({ get: sessionsFor("owner-pc") })));
    expect(p).toEqual({ state: "works", principal: "owner-pc" });
    expect(replaceQuestion("owner-pc")).toBe("This browser is already signed in as owner-pc. Replace that sign-in?");
  });

  it("a pasted token with no session of its own is 'another account'", async () => {
    const p = await currentSignin(deps(fakeFetch({ get: () => json(200, { sessions: [], operator: true }) })));
    expect(p).toEqual({ state: "works", principal: null });
    expect(replaceQuestion(null)).toBe("This browser is already signed in as another account. Replace that sign-in?");
  });

  it("a token the daemon refuses is not a sign-in to protect", async () => {
    expect(await currentSignin(deps(fakeFetch({ get: () => json(401, { error: "unauthorized" }) })))).toEqual({
      state: "refused",
    });
  });
});

describe("sortSessions", () => {
  const s = (id: string, current: boolean, created: number, used: number | null): ConsoleSession => ({
    session_id: id,
    principal: "p",
    method: "link",
    label: id,
    created_at: created,
    last_used_at: used,
    expires_at: 0,
    current,
  });

  it("puts this browser first, then the most recently used", () => {
    const out = sortSessions([s("old", false, 1, 2), s("me", true, 1, 1), s("new", false, 5, null), s("used", false, 1, 9)]);
    expect(out.map((x) => x.session_id)).toEqual(["me", "used", "new", "old"]);
  });
});

describe("a refusal while the redemption may still be committing", () => {
  it("asks again during the grace and promotes the token once the session exists", async () => {
    let calls = 0;
    const d = deps(
      fakeFetch({
        post: network,
        // The daemon commits the session on the third ask.
        get: () => (++calls < 3 ? json(401, { error: "unauthorized" }) : sessionsFor("owner-pc")()),
      }),
    );
    const r = await redeem(CODE, d);
    expect(r).toEqual({ kind: "signed_in", principal: "owner-pc" });
    expect(calls).toBe(3);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(PENDING_AT_KEY)).toBe("");
  });

  it("drops the token only after the grace has passed", async () => {
    const d = deps(fakeFetch({ post: network, get: () => json(401, { error: "unauthorized" }) }));
    const start = clock.t;
    const r = await redeem(CODE, d);
    expect(r.kind).toBe("refused");
    expect(clock.t - start).toBeGreaterThanOrEqual(SETTLE_GRACE_MS);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
  });

  it("on start, a young pending token is asked about again before it is dropped", async () => {
    let calls = 0;
    const d = deps(
      fakeFetch({ get: () => (++calls < 2 ? json(401, {}) : sessionsFor("owner-pc")()) }),
      memStore({ [TOKEN_KEY]: OLD, [PENDING_KEY]: "new-token", [PENDING_AT_KEY]: String(clock.t) }),
    );
    expect(await settlePending(d)).toBe("promoted");
    expect(d.store.read(TOKEN_KEY)).toBe("new-token");
  });
});

describe("a link always asks first", () => {
  it("names who should continue: only someone who asked for the link", () => {
    expect(LINK_QUESTION).toMatch(/Continue only if you asked for it yourself/);
    expect(LINK_QUESTION).toMatch(/pseudolife-mcp console/);
  });
});

describe("passkeyOffered", () => {
  const cap = { link: true, passkey: true };

  it("needs the daemon's word, a secure context and WebAuthn", () => {
    expect(passkeyOffered(cap, SECURE)).toBe(true);
    expect(passkeyOffered({ link: true, passkey: false }, SECURE)).toBe(false);
    expect(passkeyOffered(null, SECURE)).toBe(false);
    expect(passkeyOffered(cap, { ...SECURE, isSecureContext: false })).toBe(false);
    expect(passkeyOffered(cap, { ...SECURE, credentials: null })).toBe(false);
    expect(passkeyOffered(cap, { ...SECURE, hasPublicKeyCredential: false })).toBe(false);
    expect(passkeyOffered(cap, null)).toBe(false);
  });

  it("the demo's fixture mode needs no browser support, but still the daemon's word", () => {
    expect(passkeyOffered(cap, null, true)).toBe(true);
    expect(passkeyOffered({ link: true, passkey: false }, null, true)).toBe(false);
  });
});

const CHALLENGE = {
  payload: '{"purpose":"sign-in","nonce":"n1"}',
  mac: "mac-1",
  publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ type: "public-key", id: "a2V5" }], rpId: "localhost", userVerification: "preferred", timeout: 120000 },
};

const ASSERTION: AssertionJSON = {
  id: "a2V5",
  rawId: "a2V5",
  type: "public-key",
  response: { clientDataJSON: "Y2Q", authenticatorData: "YWQ", signature: "c2ln", userHandle: null },
  authenticatorAttachment: null,
  clientExtensionResults: {},
};

function passkeyDeps(fetch: SigninDeps["fetch"], sign = vi.fn(async (_pk: unknown) => ASSERTION), store = memStore({ [TOKEN_KEY]: OLD })) {
  return { ...deps(fetch, store), sign };
}

describe("redeemPasskey", () => {
  it("posts exactly the contract's bodies, pre-auth, and promotes the token the challenge named", async () => {
    let challengeBody: Record<string, unknown> = {};
    let sessionBody: Record<string, unknown> = {};
    const heads: Record<string, string>[] = [];
    let signedWith: unknown = null;
    const sign = vi.fn(async (pk: unknown) => {
      signedWith = pk;
      // Nothing is stored while the passkey prompt is up.
      expect(d.store.read(PENDING_KEY)).toBe("");
      return ASSERTION;
    });
    const d = passkeyDeps(
      fakeFetch({
        challenge: (init) => {
          challengeBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          heads.push(init.headers as Record<string, string>);
          expect(d.store.read(PENDING_KEY)).toBe("");
          return json(200, CHALLENGE);
        },
        post: (init) => {
          sessionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
          heads.push(init.headers as Record<string, string>);
          // Pending from before step 3; pl_token still the old one.
          expect(d.store.read(PENDING_KEY)).toMatch(/^[A-Za-z0-9_-]{43}$/);
          expect(d.store.read(PENDING_AT_KEY)).not.toBe("");
          expect(d.store.read(TOKEN_KEY)).toBe(OLD);
          return json(200, { principal: "owner-pc", session_id: "s1", expires_at: 2_000_000_000 });
        },
      }),
      sign,
    );
    const r = await redeemPasskey(d);

    expect(r).toEqual({ kind: "signed_in", principal: "owner-pc" });
    expect(Object.keys(challengeBody).sort()).toEqual(["label", "token_sha256"]);
    expect(challengeBody.label).toBe("Firefox on Windows");
    expect(challengeBody.token_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(challengeBody.token_sha256).toBe(await sha256Hex(globalThis.crypto, d.store.read(TOKEN_KEY)));
    expect(signedWith).toEqual(CHALLENGE.publicKey);
    expect(sessionBody).toEqual({ payload: CHALLENGE.payload, mac: CHALLENGE.mac, assertion: ASSERTION });
    for (const h of heads) {
      expect(h.Authorization).toBeUndefined();
      expect(h["Content-Type"]).toBe("application/json");
    }
    expect(d.store.read(TOKEN_KEY)).not.toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(PENDING_AT_KEY)).toBe("");
  });

  it("a closed passkey prompt is a quiet cancel: nothing sent, nothing stored", async () => {
    const post = vi.fn(() => json(200, { principal: "owner-pc" }));
    const sign = vi.fn(async () => {
      throw new CeremonyError("cancelled", "The passkey prompt was closed or timed out. Nothing was signed or sent.");
    });
    const d = passkeyDeps(fakeFetch({ challenge: () => json(200, CHALLENGE), post }), sign);
    const before = new Map(d.store.map);
    const r = await redeemPasskey(d);
    expect(r).toEqual({ kind: "cancelled", message: "Sign-in cancelled." });
    expect(post).not.toHaveBeenCalled();
    expect(d.store.map).toEqual(before);
  });

  it("another ceremony failure says why, and still changes nothing", async () => {
    const post = vi.fn(() => json(200, { principal: "owner-pc" }));
    const sign = vi.fn(async () => {
      throw new CeremonyError("wrong_site", "The browser refused the passkey for this address.");
    });
    const d = passkeyDeps(fakeFetch({ challenge: () => json(200, CHALLENGE), post }), sign);
    const r = await redeemPasskey(d);
    expect(r).toMatchObject({ kind: "refused", reason: "ceremony", message: "The browser refused the passkey for this address." });
    expect(post).not.toHaveBeenCalled();
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it.each([
    [409, { error: "passkey_signin_unavailable", hint: "No passkey can sign in yet: run `pseudolife-mcp console` on the daemon's host." }, "passkey_unavailable", /^No passkey can sign in yet: run `pseudolife-mcp console` on the daemon's host\.$/],
    [409, { error: "passkey_signin_unavailable" }, "passkey_unavailable", /Use this passkey to sign in/],
    [403, { error: "forbidden_origin" }, "forbidden_origin", /^Passkey sign-in works at your Console's HTTPS address; open the Console there, or sign in with a code\.$/],
    [429, { error: "rate_limited" }, "rate_limited", /in a minute/],
    [400, { error: "invalid_request" }, "http", /HTTP 400, invalid_request/],
    [415, { error: "unsupported_media_type" }, "http", /HTTP 415/],
    [503, { error: "signin_unavailable" }, "unavailable", /cannot sign browsers in right now/],
  ])("a refused challenge (%i) never prompts and changes nothing", async (status, body, reason, msg) => {
    const sign = vi.fn(async () => ASSERTION);
    const d = passkeyDeps(fakeFetch({ challenge: () => json(status, body) }), sign);
    const r = await redeemPasskey(d);
    expect(r).toMatchObject({ kind: "refused", reason });
    if (r.kind === "refused") expect(r.message).toMatch(msg);
    expect(sign).not.toHaveBeenCalled();
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("a lost challenge answer is definite: no session can exist yet", async () => {
    const sign = vi.fn(async () => ASSERTION);
    const d = passkeyDeps(fakeFetch({ challenge: network }), sign);
    const r = await redeemPasskey(d);
    expect(r).toMatchObject({ kind: "refused", reason: "no_answer" });
    expect(sign).not.toHaveBeenCalled();
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
  });

  it.each([
    [400, { error: "signin_refused" }, "refused", /^The passkey sign-in was not accepted\. Try again; if it keeps failing, sign in with a code\.$/],
    [409, { error: "passkey_signin_unavailable", hint: "Sign in on the daemon's host with `pseudolife-mcp console`, then Settings, Your passkeys." }, "passkey_unavailable", /^Sign in on the daemon's host/],
    [403, { error: "forbidden_origin" }, "forbidden_origin", /HTTPS address/],
    [429, { error: "rate_limited" }, "rate_limited", /in a minute/],
  ])("a refused session (%i) keeps pl_token and drops the pending token", async (status, body, reason, msg) => {
    const get = vi.fn(sessionsFor("someone"));
    const d = passkeyDeps(fakeFetch({ challenge: () => json(200, CHALLENGE), post: () => json(status, body), get }));
    const r = await redeemPasskey(d);
    expect(r).toMatchObject({ kind: "refused", reason });
    if (r.kind === "refused") expect(r.message).toMatch(msg);
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(PENDING_AT_KEY)).toBe("");
    expect(get).not.toHaveBeenCalled();
  });

  it("503 signin_unavailable on the session is ambiguous: settled like a code's", async () => {
    const made = passkeyDeps(
      fakeFetch({ challenge: () => json(200, CHALLENGE), post: () => json(503, { error: "signin_unavailable" }), get: sessionsFor("owner-pc") }),
    );
    expect(await redeemPasskey(made)).toEqual({ kind: "signed_in", principal: "owner-pc" });
    expect(made.store.read(TOKEN_KEY)).not.toBe(OLD);

    const notMade = passkeyDeps(
      fakeFetch({
        challenge: () => json(200, CHALLENGE),
        post: () => json(503, { error: "signin_unavailable" }),
        get: () => json(401, { error: "unauthorized" }),
      }),
    );
    const r = await redeemPasskey(notMade);
    expect(r).toMatchObject({ kind: "refused", reason: "unavailable" });
    expect(notMade.store.read(TOKEN_KEY)).toBe(OLD);
    expect(notMade.store.read(PENDING_KEY)).toBe("");
  });

  it("a lost session answer tests the pending token, and words a refusal for the passkey", async () => {
    let probed = "";
    const works = passkeyDeps(
      fakeFetch({
        challenge: () => json(200, CHALLENGE),
        post: network,
        get: (init) => {
          probed = authOf(init) ?? "";
          return sessionsFor("owner-pc")();
        },
      }),
    );
    expect(await redeemPasskey(works)).toEqual({ kind: "signed_in", principal: "owner-pc" });
    expect(probed).toBe(`Bearer ${works.store.read(TOKEN_KEY)}`);

    const refused = passkeyDeps(fakeFetch({ challenge: () => json(200, CHALLENGE), post: network, get: () => json(401, {}) }));
    const r = await redeemPasskey(refused);
    expect(r).toMatchObject({ kind: "refused", reason: "not_completed" });
    if (r.kind === "refused") expect(r.message).toMatch(/passkey sign-in did not complete/);
    expect(refused.store.read(TOKEN_KEY)).toBe(OLD);
  });

  it("when neither answer arrives, the pending token is kept for the next start", async () => {
    const d = passkeyDeps(fakeFetch({ challenge: () => json(200, CHALLENGE), post: network, get: network }));
    const r = await redeemPasskey(d);
    expect(r.kind).toBe("unconfirmed");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
    expect(d.store.read(PENDING_KEY)).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("a challenge answer without payload, mac or options is refused before the prompt", async () => {
    const sign = vi.fn(async () => ASSERTION);
    const d = passkeyDeps(fakeFetch({ challenge: () => json(200, { payload: "p" }) }), sign);
    const r = await redeemPasskey(d);
    expect(r).toMatchObject({ kind: "refused", reason: "http" });
    expect(sign).not.toHaveBeenCalled();
    expect(d.store.read(PENDING_KEY)).toBe("");
  });

  it("stops before minting without crypto.subtle", async () => {
    const f = fakeFetch({});
    const r = await redeemPasskey({ ...passkeyDeps(f), crypto: undefined });
    expect(r.kind).toBe("unsupported");
    expect(f).not.toHaveBeenCalled();
  });
});

describe("Use this passkey to sign in", () => {
  it("explains itself and names the principal on success", () => {
    expect(BIND_EXPLAINER).toBe(
      "Lets the passkey you choose sign this Console in, as the account this Console is signed in as now. " +
        "Use it once, after signing in with `pseudolife-mcp console` on the daemon's host.",
    );
    expect(bindSuccess({ credential_id: "c1", signs_in_as: "owner-pc" })).toBe(
      "This passkey now signs the Console in as owner-pc.",
    );
  });

  it("principal_not_allowed for ownership means this sign-in is not the owner's", () => {
    const line = bindFailureLine(
      new ApiError(403, "principal_not_allowed", { error: "principal_not_allowed", reason: "not_owner" } as never),
    );
    expect(line).toMatch(/Only the bank owner's sign-in can do this: sign in with `pseudolife-mcp console` on the daemon's host, then try again\.$/);
  });

  it("principal_not_allowed from the board gate does not send the person round the same loop", () => {
    const line = bindFailureLine(new ApiError(403, "principal_not_allowed", { error: "principal_not_allowed" }));
    expect(line).not.toMatch(/Only the bank owner's sign-in/);
  });

  it("a 401 asks to sign in again", () => {
    expect(bindFailureLine(new ApiError(401, "unauthorized", { error: "unauthorized" }))).toMatch(/Sign in again/);
  });

  it.each([
    [409, "maintainer_https_required", /passkeys need HTTPS/],
    [403, "assertion_invalid", /did not accept the passkey's signature/],
    [410, "challenge_expired", /two-minute challenge ran out/],
    [410, "challenge_spent", /already used once/],
    [503, "coordination_unavailable", /storage is unavailable/],
  ])("reuses the maintainer routes' wording for %i %s", (status, code, msg) => {
    const line = bindFailureLine(new ApiError(status, code, { error: code }));
    expect(line).toMatch(/^The passkey was not set to sign in: /);
    expect(line).toMatch(msg);
  });

  it("a closed prompt says nothing was signed", () => {
    expect(bindFailureLine(new CeremonyError("cancelled", "The passkey prompt was closed or timed out. Nothing was signed or sent."))).toMatch(
      /closed or timed out/,
    );
  });
});

describe("a passkey sign-in whose dialog closed", () => {
  const challenge = () =>
    json(200, { payload: "{}", mac: "0".repeat(64), publicKey: { challenge: "AA", allowCredentials: [] } });

  it("never opens the passkey prompt after the challenge arrives", async () => {
    const sign = vi.fn(async () => ({}) as never);
    const d = deps(vi.fn(async () => challenge()));
    const r = await redeemPasskey({ ...d, sign, cancelled: () => true });
    expect(r.kind).toBe("cancelled");
    expect(sign).not.toHaveBeenCalled();
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
  });

  it("posts nothing when it closed while the prompt was open", async () => {
    let open = true;
    const sign = vi.fn(async () => {
      open = false;
      return { id: "x" } as never;
    });
    const fetch = vi.fn(async () => challenge());
    const d = deps(fetch);
    const r = await redeemPasskey({ ...d, sign, cancelled: () => !open });
    expect(r.kind).toBe("cancelled");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(d.store.read(PENDING_KEY)).toBe("");
    expect(d.store.read(TOKEN_KEY)).toBe(OLD);
  });
});

describe("the key-change record of a rebind", () => {
  it("names the principal the key now signs in as", async () => {
    const { keyChangeText } = await import("./maintainer");
    const text = keyChangeText({
      at: 1, change: "bind-sign-in", credential_id: "AAAAAAAAAAAAAAAA", label: "laptop", by: "AAAAAAAAAAAAAAAA",
      path: "console", principal: "owner-pc", revoked: null,
    } as never);
    expect(text).toMatch(/now signs the Console in as owner-pc/);
  });
});
