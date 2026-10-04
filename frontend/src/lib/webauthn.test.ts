import { describe, expect, it, vi } from "vitest";
import {
  assertionToJSON,
  attestationToJSON,
  b64urlDecode,
  b64urlEncode,
  CeremonyError,
  createCredential,
  creationOptionsFromJSON,
  explainCeremonyFailure,
  getAssertion,
  requestOptionsFromJSON,
  webauthnSupport,
  type CredentialLike,
  type WebAuthnEnv,
} from "./webauthn";

const enc = (s: string) => new TextEncoder().encode(s);
const buf = (bytes: number[]) => new Uint8Array(bytes).buffer;

describe("base64url", () => {
  it("matches the RFC 4648 vectors, unpadded and URL-safe", () => {
    const vectors: [string, string][] = [
      ["", ""],
      ["f", "Zg"],
      ["fo", "Zm8"],
      ["foo", "Zm9v"],
      ["foob", "Zm9vYg"],
      ["fooba", "Zm9vYmE"],
      ["foobar", "Zm9vYmFy"],
    ];
    for (const [plain, coded] of vectors) {
      expect(b64urlEncode(enc(plain))).toBe(coded);
      expect(new TextDecoder().decode(b64urlDecode(coded))).toBe(plain);
    }
    // The two characters that differ from plain base64.
    expect(b64urlEncode(buf([0xfb, 0xff]))).toBe("-_8");
    expect([...b64urlDecode("-_8")]).toEqual([0xfb, 0xff]);
  });

  it("round-trips every length and byte value", () => {
    for (let n = 0; n < 40; n++) {
      const bytes = new Uint8Array(n).map((_, i) => (i * 37 + n * 11) & 255);
      expect([...b64urlDecode(b64urlEncode(bytes))]).toEqual([...bytes]);
    }
    const all = new Uint8Array(256).map((_, i) => i);
    expect([...b64urlDecode(b64urlEncode(all))]).toEqual([...all]);
  });

  it("encodes a view over part of a buffer, not the whole buffer", () => {
    const whole = new Uint8Array([1, 2, 3, 4, 5]);
    expect(b64urlEncode(whole.subarray(1, 3))).toBe(b64urlEncode(new Uint8Array([2, 3])));
  });

  it("accepts padding and plain base64, and refuses garbage", () => {
    expect([...b64urlDecode("Zm8=")]).toEqual([...enc("fo")]);
    expect([...b64urlDecode("+/8")]).toEqual([0xfb, 0xff]);
    expect(() => b64urlDecode("Zm9v!")).toThrow(TypeError);
    expect(() => b64urlDecode("Z")).toThrow(TypeError);
  });
});

describe("options from the daemon's JSON", () => {
  const challenge = b64urlEncode(enc("sha256-of-payload-32-bytes-long!"));
  const credId = b64urlEncode(buf([1, 2, 3, 250]));

  it("decodes the request options' byte fields and keeps the rest", () => {
    const o = requestOptionsFromJSON({
      challenge,
      rpId: "console.example.com",
      timeout: 120000,
      userVerification: "required",
      allowCredentials: [{ type: "public-key", id: credId, transports: ["internal"] }],
    });
    expect(o.challenge).toBeInstanceOf(Uint8Array);
    expect(new TextDecoder().decode(o.challenge as Uint8Array)).toBe("sha256-of-payload-32-bytes-long!");
    expect(o.rpId).toBe("console.example.com");
    expect(o.userVerification).toBe("required");
    expect(o.timeout).toBe(120000);
    expect([...(o.allowCredentials![0].id as Uint8Array)]).toEqual([1, 2, 3, 250]);
    expect(o.allowCredentials![0].transports).toEqual(["internal"]);
  });

  it("unwraps options sent as {publicKey: {...}}", () => {
    const o = requestOptionsFromJSON({ publicKey: { challenge, rpId: "localhost" } });
    expect(o.rpId).toBe("localhost");
    expect(o.allowCredentials).toBeUndefined();
  });

  it("decodes the creation options' challenge, user id and exclusions", () => {
    const o = creationOptionsFromJSON({
      challenge,
      rp: { id: "localhost", name: "Pseudolife" },
      user: { id: b64urlEncode(enc("maintainer")), name: "maintainer", displayName: "Maintainer" },
      pubKeyCredParams: [
        { type: "public-key", alg: -7 },
        { type: "public-key", alg: -8 },
        { type: "public-key", alg: -257 },
      ],
      authenticatorSelection: { userVerification: "required", residentKey: "preferred" },
      attestation: "none",
      excludeCredentials: [{ type: "public-key", id: credId }],
    });
    expect(new TextDecoder().decode(o.user.id as Uint8Array)).toBe("maintainer");
    expect(o.user.name).toBe("maintainer");
    expect(o.rp.id).toBe("localhost");
    expect(o.pubKeyCredParams.map((p) => p.alg)).toEqual([-7, -8, -257]);
    expect(o.attestation).toBe("none");
    expect([...(o.excludeCredentials![0].id as Uint8Array)]).toEqual([1, 2, 3, 250]);
  });

  it("refuses options with no usable challenge", () => {
    expect(() => requestOptionsFromJSON({ rpId: "x" })).toThrow(TypeError);
    expect(() => requestOptionsFromJSON(null)).toThrow(TypeError);
  });
});

function fakeAssertion(): CredentialLike {
  return {
    id: "AQID-g",
    rawId: buf([1, 2, 3, 250]),
    type: "public-key",
    authenticatorAttachment: "platform",
    response: {
      clientDataJSON: enc('{"type":"webauthn.get"}').buffer as ArrayBuffer,
      authenticatorData: buf([9, 9, 9]),
      signature: buf([48, 69, 2]),
      userHandle: null,
    },
    getClientExtensionResults: () => ({}),
  };
}

describe("credentials to the daemon's JSON", () => {
  it("encodes an assertion's byte fields as base64url", () => {
    const j = assertionToJSON(fakeAssertion());
    expect(j).toEqual({
      id: "AQID-g",
      rawId: "AQID-g",
      type: "public-key",
      response: {
        clientDataJSON: b64urlEncode(enc('{"type":"webauthn.get"}')),
        authenticatorData: "CQkJ",
        signature: "MEUC",
        userHandle: null,
      },
      authenticatorAttachment: "platform",
      clientExtensionResults: {},
    });
  });

  it("keeps a user handle when the authenticator returns one", () => {
    const c = fakeAssertion();
    c.response.userHandle = enc("maintainer").buffer as ArrayBuffer;
    expect(assertionToJSON(c).response.userHandle).toBe(b64urlEncode(enc("maintainer")));
  });

  it("encodes an attestation with its transports", () => {
    const j = attestationToJSON({
      id: "AQID-g",
      rawId: buf([1, 2, 3, 250]),
      type: "public-key",
      response: {
        clientDataJSON: buf([123, 125]),
        attestationObject: buf([0xa3, 0x63]),
        getTransports: () => ["internal", "hybrid"],
      },
    });
    expect(j.response).toEqual({ clientDataJSON: "e30", attestationObject: "o2M", transports: ["internal", "hybrid"] });
    expect(j.rawId).toBe("AQID-g");
  });

  it("refuses to mix up the two kinds", () => {
    expect(() => attestationToJSON(fakeAssertion())).toThrow(TypeError);
    const att = { ...fakeAssertion(), response: { clientDataJSON: buf([1]), attestationObject: buf([2]) } };
    expect(() => assertionToJSON(att)).toThrow(TypeError);
  });
});

describe("support", () => {
  const credentials = { get: vi.fn(), create: vi.fn() };

  it("explains an insecure page", () => {
    const s = webauthnSupport({ isSecureContext: false, hasPublicKeyCredential: true, credentials });
    expect(s.ok).toBe(false);
    expect(s.ok === false && s.why).toBe("insecure");
    expect(s.ok === false && s.message).toMatch(/HTTPS/);
  });

  it("explains a browser without WebAuthn", () => {
    const s = webauthnSupport({ isSecureContext: true, hasPublicKeyCredential: false, credentials: null });
    expect(s.ok === false && s.why).toBe("no_webauthn");
  });

  it("is ok in a secure context with WebAuthn", () => {
    expect(webauthnSupport({ isSecureContext: true, hasPublicKeyCredential: true, credentials }).ok).toBe(true);
  });
});

describe("ceremonies", () => {
  const challenge = b64urlEncode(enc("c"));

  function env(get: (o: CredentialRequestOptions) => Promise<unknown>, create = vi.fn()): WebAuthnEnv {
    return {
      isSecureContext: true,
      hasPublicKeyCredential: true,
      credentials: { get: vi.fn(get) as never, create: create as never },
    };
  }

  it("passes decoded options to credentials.get and returns the JSON", async () => {
    let seen: CredentialRequestOptions | undefined;
    const e = env(async (o) => {
      seen = o;
      return fakeAssertion();
    });
    const j = await getAssertion({ challenge, rpId: "localhost" }, { env: e });
    expect(seen?.publicKey?.challenge).toBeInstanceOf(Uint8Array);
    expect(seen?.publicKey?.rpId).toBe("localhost");
    expect(j.rawId).toBe("AQID-g");
  });

  it("turns a closed prompt into a plain cancellation", async () => {
    const e = env(async () => {
      throw new DOMException("denied", "NotAllowedError");
    });
    const err = await getAssertion({ challenge }, { env: e }).catch((x: unknown) => x);
    expect(err).toBeInstanceOf(CeremonyError);
    expect((err as CeremonyError).code).toBe("cancelled");
    expect((err as CeremonyError).message).toMatch(/Nothing was signed/);
  });

  it("names a wrong site and an authenticator that already holds a key", () => {
    expect(explainCeremonyFailure(new DOMException("x", "SecurityError"), "get").code).toBe("wrong_site");
    expect(explainCeremonyFailure(new DOMException("x", "InvalidStateError"), "create").code).toBe("already_enrolled");
    expect(explainCeremonyFailure(new Error("boom"), "get").code).toBe("failed");
  });

  it("refuses to start on an insecure page", async () => {
    const get = vi.fn();
    const e: WebAuthnEnv = { isSecureContext: false, hasPublicKeyCredential: true, credentials: { get, create: vi.fn() } };
    await expect(getAssertion({ challenge }, { env: e })).rejects.toMatchObject({ code: "unsupported" });
    expect(get).not.toHaveBeenCalled();
  });

  it("in fixture mode sends a dummy without touching the browser", async () => {
    const get = vi.fn();
    const create = vi.fn();
    const e: WebAuthnEnv = { isSecureContext: false, hasPublicKeyCredential: false, credentials: { get, create } };
    const a = await getAssertion({ challenge }, { fixtures: true, env: e });
    const c = await createCredential({ challenge }, { fixtures: true, env: e });
    expect(get).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(a.response.signature).toBeTruthy();
    expect(c.response.attestationObject).toBeTruthy();
  });

  it("creates a credential from decoded creation options", async () => {
    let seen: CredentialCreationOptions | undefined;
    const create = vi.fn(async (o: CredentialCreationOptions) => {
      seen = o;
      return {
        id: "AQID-g",
        rawId: buf([1, 2, 3, 250]),
        type: "public-key",
        response: { clientDataJSON: buf([1]), attestationObject: buf([2]) },
      };
    });
    const e = env(async () => null, create);
    const j = await createCredential(
      {
        challenge,
        rp: { id: "localhost", name: "Pseudolife" },
        user: { id: b64urlEncode(enc("m")), name: "m", displayName: "m" },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      },
      { env: e },
    );
    expect(seen?.publicKey?.user.id).toBeInstanceOf(Uint8Array);
    expect(j.response.attestationObject).toBe("Ag");
  });
});
