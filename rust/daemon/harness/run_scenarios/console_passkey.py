"""Console sign-in with the maintainer passkey (schema v59; spec 2026-11-02,
section 5; cutover blocker 8): the pre-auth ``POST
/api/console/passkey-challenge``, the passkey body of ``POST
/api/console/session``, ``POST /api/console/passkey-bind`` and the
sessions a revoked or rebound key ends, Python against Rust over HTTP.

Each arm signs its own daemon-issued challenges with the same software
Ed25519 keys (``tests/maintainer_authenticator.py``), so the public keys and
counters compare while every MAC and challenge is verified against its own
payload before its random bytes are normalized. The template carries the
challenge secret and three keys written as the maintainer store writes them:
``primary`` signs in as ``default``, ``ghost`` as a principal that no longer
resolves, ``unbound`` as no one. Live only: the rows carry the template's
clock, and the assertions the arms' own challenges.

The security near misses: a replayed assertion, a challenge for a token
already in use, a payload whose MAC no longer matches (a swapped token), a
wrong-origin assertion, a cross-origin request, a bind completed by another
principal than the one it was issued for, a bind by someone else's
principal, a key that no longer resolves, and a revoked key, whose browser
must be signed out at once.
"""
import base64
import copy
import hashlib
import hmac
import json
import re
import sys
import time

RP_ID = "console.example.com"
ORIGIN = "https://console.example.com:8443"
SECRET = "42" * 32
T_ALICE = "tok-alice-passkey-0002"
T_FRIEND = "tok-friend-passkey-0003"
KEYS = {"primary": "default", "ghost": "ghost", "unbound": None}
TABLES = ("console_sessions", "maintainer_passkeys", "maintainer_nonces")
CONFIG = f"""coordination:
  allowed_principals: [default, alice, friend]
  not_owner_principals: [friend]
  maintainer:
    rp_id: {RP_ID}
    origin: {ORIGIN}
"""
SESSION_ID = re.compile(r"[0-9a-f]{16}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def signer(name: str):
    """The fixture key ``name`` at sign count 1, one per arm."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tests.maintainer_authenticator import SoftAuthenticator
    out = SoftAuthenticator(-8, rp_id=RP_ID, origin=ORIGIN, sign_count=1)
    seed = hashlib.sha256(("console-passkey-fixture:" + name).encode()).digest()
    out.private = Ed25519PrivateKey.from_private_bytes(seed)
    out.credential_id = seed[:16]
    return out


def _clock(value):
    return "<clock>" if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def scrub_challenge(resp):
    """A ``{payload, mac, publicKey}`` answer: the MAC must authenticate the
    payload under the template's secret and the WebAuthn challenge must be the
    payload's SHA-256; then the nonce, expiry, MAC and challenge become
    tokens and the payload's signed fields stay compared."""
    body = resp.get("json")
    if not (isinstance(body, dict) and isinstance(body.get("payload"), str)
            and isinstance(body.get("mac"), str)):
        return resp
    payload = body["payload"]
    expected = hmac.new(bytes.fromhex(SECRET), payload.encode(), hashlib.sha256).hexdigest()
    body["mac"] = "<verified MAC>" if hmac.compare_digest(expected, body["mac"]) else "<BAD MAC>"
    try:
        fields = json.loads(payload)
    except ValueError:
        fields = None
    if isinstance(fields, dict):
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if canonical != payload:
            fields["<not canonical>"] = True
        if isinstance(fields.get("nonce"), str) and re.fullmatch(r"[0-9a-f]{32}", fields["nonce"]):
            fields["nonce"] = "<nonce>"
        fields["expires_at"] = _clock(fields.get("expires_at"))
        body["payload"] = fields
    options = body.get("publicKey")
    if isinstance(options, dict):
        challenge = b64(hashlib.sha256(payload.encode()).digest())
        options["challenge"] = ("<verified challenge>" if options.get("challenge") == challenge
                                else "<BAD challenge>")
    return resp


def register(harness):
    T_DEFAULT = harness.T_DEFAULT

    class ConsolePasskeySignin(harness.Scenario):
        """The passkey half of Console sign-in, end to end, on both arms."""
        name = "console-passkey-signin"
        config_yaml = CONFIG
        live_only = True

        def __init__(self):
            self.env = {"PSEUDOLIFE_MCP_TOKEN": T_DEFAULT,
                        "PSEUDOLIFE_MCP_TOKENS": f"{T_ALICE}:alice,{T_FRIEND}:friend"}

        def prepare_template(self, dsn: str) -> None:
            import psycopg
            from psycopg.types.json import Jsonb
            sys.path.insert(0, str(harness.REPO))
            from pseudolife_memory.storage.postgres import PostgresStorage
            PostgresStorage(dsn).close()   # the bank, its v59 tables included
            now = time.time()
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("INSERT INTO meta (key, value) VALUES ('maintainer_secret_v1', %s) "
                             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                             (Jsonb(SECRET),))
                for i, (name, principal) in enumerate(KEYS.items()):
                    key = signer(name)
                    conn.execute(
                        "INSERT INTO maintainer_passkeys (credential_id, public_key, alg, "
                        "sign_count, label, enrolled_by, state, active_from, created_at, "
                        "signs_in_as) VALUES (%s, %s, -8, 1, %s, 'bootstrap', 'active', %s, %s, %s)",
                        (key.id, key.cose_key(), f"Fixture {name}", now - 500 + i,
                         now - 1000 + i, principal))

        def normalize_db(self, state: dict, before: dict, side: str) -> dict:
            """The tables the passkey routes own, compared exactly but for
            their clocks and random values (session ids, nonces); the
            maintainer key changes from the audit log by their fields; the
            rest of the board (the rebind's daemon notice) is the board
            harness's to compare."""
            state = super().normalize_db(state, before, side)
            rows = state["rows"]
            kept = {}
            for name in TABLES:
                table = copy.deepcopy(rows.get(f"public.{name}"))
                if not table:
                    continue
                cols = table["columns"]
                for row in table["rows"]:
                    for i, col in enumerate(cols):
                        if col.endswith("_at") or (name == "maintainer_nonces" and col == "nonce"):
                            row[i] = _clock(row[i]) if col.endswith("_at") else "<nonce>"
                        elif col == "session_id" and isinstance(row[i], str) \
                                and SESSION_ID.fullmatch(row[i]):
                            row[i] = "<new session>"
                table["rows"].sort(key=lambda r: json.dumps(r, sort_keys=True))
                kept[f"public.{name}"] = table
            events = rows.get("public.coordination_events")
            if events:
                cols = events["columns"]
                picked = []
                for row in events["rows"]:
                    record = dict(zip(cols, row))
                    if record.get("event") != "maintainer_key":
                        continue
                    payload = record.get("payload")
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    picked.append([record.get("actor"), record.get("principal"), payload])
                kept["maintainer_key events"] = {"columns": ["actor", "principal", "payload"],
                                                 "rows": picked}
            state["rows"] = kept
            return state

        def timeline(self, procs, holders):
            js = ("Content-Type", "application/json")
            at_origin = [("Origin", ORIGIN), js]
            arms = ("python", "rust")
            keys = {arm: {name: signer(name) for name in KEYS} for arm in arms}
            issued: dict = {arm: {} for arm in arms}
            out = []

            def step(label, method, path, headers, body=None, save=None):
                """One request on both arms; ``body(arm)`` builds an arm's
                own body (its own challenge, its own signature)."""
                answers = {}
                for arm in arms:
                    raw = body(arm) if callable(body) else body
                    if isinstance(raw, (dict, list)):
                        raw = json.dumps(raw).encode()
                    answers[arm] = harness.call(procs[arm].port, method, path, list(headers), raw)
                    if save and isinstance(answers[arm].get("json"), dict):
                        issued[arm][save] = answers[arm]["json"]
                c = harness.case(label, method, path, headers)
                c["_answers"] = (answers["python"], answers["rust"])
                out.append(c)

            def challenge(label, token, save, origin=ORIGIN, headers=None):
                body = {"token_sha256": sha(token), "label": "Firefox  on\tLinux"}
                step(label, "POST", "/api/console/passkey-challenge",
                     headers if headers is not None else [("Origin", origin), js], body, save)

            def signed(name, save, *, origin=ORIGIN, edit=None):
                def body(arm):
                    got = copy.deepcopy(issued[arm].get(save) or {})
                    payload = got.get("payload", "")
                    out = {"payload": payload, "mac": got.get("mac", ""),
                           "assertion": keys[arm][name].assertion(
                               hashlib.sha256(payload.encode()).digest(), origin=origin)}
                    if edit:
                        edit(out)
                    issued[arm]["body:" + save] = copy.deepcopy(out)
                    return out
                return body

            def again(save):
                return lambda arm: issued[arm]["body:" + save]

            env = harness.bearer(T_DEFAULT)
            alice = harness.bearer(T_ALICE)

            step("health offers the passkey sign-in", "GET", "/health", [])
            # The challenge route: pre-auth, at the passkey origin only.
            challenge("challenge without Origin", "pk-browser-a", "x", headers=[js])
            challenge("challenge text/plain", "pk-browser-a", "x",
                      headers=[("Origin", ORIGIN), ("Content-Type", "text/plain")])
            challenge("challenge from another site", "pk-browser-a", "x",
                      origin="https://evil.example.com")
            challenge("challenge from the passkey host's other port", "pk-browser-a", "x",
                      origin="https://console.example.com")
            step("challenge malformed", "POST", "/api/console/passkey-challenge", at_origin,
                 {"token_sha256": sha("x"), "label": "l", "principal": "default"})
            step("challenge GET", "GET", "/api/console/passkey-challenge", [("Origin", ORIGIN)])
            challenge("challenge for browser a", "pk-browser-a", "a")
            # Sign in: a wrong-origin assertion, then the real one.
            step("assertion from another origin", "POST", "/api/console/session", at_origin,
                 signed("primary", "a", origin="https://evil.example.com"))
            step("passkey body at a loopback origin", "POST", "/api/console/session",
                 [("Origin", "http://127.0.0.1:8765"), js], signed("primary", "a"))
            step("passkey signs browser a in", "POST", "/api/console/session", at_origin,
                 signed("primary", "a"))
            step("browser a's token works at once", "GET", "/api/nope",
                 [harness.bearer("pk-browser-a")])
            step("the same assertion replayed", "POST", "/api/console/session", at_origin,
                 again("a"))
            challenge("challenge again for browser a", "pk-browser-a", "a2")
            step("a token already in use", "POST", "/api/console/session", at_origin,
                 signed("primary", "a2"))

            def swap_token(body):
                if not body["payload"]:
                    return  # no challenge was issued: the difference shows there
                fields = json.loads(body["payload"])
                fields["token_sha256"] = sha("pk-attacker")
                body["payload"] = json.dumps(fields, sort_keys=True, separators=(",", ":"))

            challenge("challenge for browser b", "pk-browser-b", "b")
            step("a payload whose token was swapped", "POST", "/api/console/session", at_origin,
                 signed("primary", "b", edit=swap_token))
            step("the attacker's token does not work", "GET", "/api/nope",
                 [harness.bearer("pk-attacker")])
            # A key whose sign-in principal no longer resolves.
            challenge("challenge for the ghost key", "pk-browser-g", "g")
            step("a key whose principal is gone", "POST", "/api/console/session", at_origin,
                 signed("ghost", "g"))
            # Use this passkey to sign in.
            step("bind by someone else's principal", "POST", "/api/console/passkey-bind",
                 [harness.bearer(T_FRIEND), js], {})
            step("bind with a partial body", "POST", "/api/console/passkey-bind", [env, js],
                 {"payload": "p", "mac": "m"})
            step("bind challenge for alice", "POST", "/api/console/passkey-bind", [alice, js], {},
                 save="bind")
            step("alice's bind completed by default", "POST", "/api/console/passkey-bind",
                 [env, js], signed("primary", "bind"))
            step("alice completes her bind", "POST", "/api/console/passkey-bind", [alice, js],
                 again("bind"))
            step("browser a signed out by the rebind", "GET", "/api/nope",
                 [harness.bearer("pk-browser-a")])
            challenge("challenge for browser c", "pk-browser-c", "c")
            step("the rebound key signs browser c in as alice", "POST", "/api/console/session",
                 at_origin, signed("primary", "c"))
            step("operator lists the sessions", "GET", "/api/console/sessions", [env])
            # The key revokes itself: its browser is signed out at once.
            step("revoke-self challenge", "POST", "/api/maintainer/challenge", [env, js],
                 lambda arm: {"purpose": "revoke-self", "credential_id": keys[arm]["primary"].id},
                 save="revoke")
            step("the key revokes itself", "POST", "/api/maintainer/revoke", [env, js],
                 signed("primary", "revoke"))
            step("browser c signed out at once", "GET", "/api/nope",
                 [harness.bearer("pk-browser-c")])
            challenge("challenge after the revoke", "pk-browser-d", "d")
            step("health still offers the passkey", "GET", "/health", [])
            return out

    def scrub_signin_or_challenge(resp):
        return scrub_challenge(resp)

    harness.register_response_scrub("/api/console/passkey-challenge", scrub_signin_or_challenge)
    harness.register_response_scrub("/api/console/passkey-bind", scrub_signin_or_challenge)
    harness.register_response_scrub("/api/maintainer/challenge", scrub_signin_or_challenge)
    return [ConsolePasskeySignin]
