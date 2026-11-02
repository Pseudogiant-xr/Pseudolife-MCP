import { describe, expect, it, vi } from "vitest";
import {
  browserLabel,
  canMint,
  currentSignin,
  formatCode,
  landingHash,
  mintToken,
  normalizeCode,
  PENDING_AT_KEY,
  PENDING_KEY,
  SETTLE_GRACE_MS,
  LINK_QUESTION,
  redeem,
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
import { b64urlDecode } from "./webauthn";

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

/** A fetch that answers the session POST and the sessions GET separately. */
function fakeFetch(routes: { post?: Route; get?: Route }) {
  return vi.fn(async (url: string, init: RequestInit) => {
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

describe("signinCapability", () => {
  it("is null when /health says nothing (an older daemon)", () => {
    expect(signinCapability({ status: "ok" })).toBeNull();
    expect(signinCapability(null)).toBeNull();
    expect(signinCapability({ status: "ok", console_signin: "yes" })).toBeNull();
  });

  it("offers only the pasted token when the capability is absent or link is off", () => {
    expect(signinChoices(signinCapability({ status: "ok" }))).toEqual({ code: false, passkey: false, pasteOnly: true });
    expect(signinChoices({ link: false, passkey: true })).toEqual({ code: false, passkey: true, pasteOnly: true });
    expect(signinChoices({ link: true, passkey: false })).toEqual({ code: true, passkey: false, pasteOnly: false });
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
