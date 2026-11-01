"""Signed Console actions on the board harness's disposable banks.

The software authenticator is a test fixture. Its fixed Ed25519 keys make
public keys comparable; each arm still signs its own daemon-issued payload.
MACs, WebAuthn challenges and stored message proofs are checked before their
random or clock-dependent bytes are normalized.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json

from board import scenarios as S
from board.norm import HEX64, Normalizer

RP_ID = "console.example.com"
ORIGIN = "https://console.example.com:8443"
SECRET = "42" * 32
TABLES = ("maintainer_passkeys", "maintainer_bootstrap", "maintainer_nonces")
CONFIG = S.BASE_CONFIG.replace("  wake:\n", f"  maintainer:\n    rp_id: {RP_ID}\n"
                               f"    origin: {ORIGIN}\n  wake:\n")


def auth(ctx, name="primary"):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tests.maintainer_authenticator import SoftAuthenticator

    key = "auth:" + name
    if key not in ctx.vals:
        signer = SoftAuthenticator(-8, sign_count=1)
        seed = hashlib.sha256(("board-maintainer-fixture:" + name).encode()).digest()
        signer.private = Ed25519PrivateKey.from_private_bytes(seed)
        signer.credential_id = seed[:16]
        ctx.vals[key] = signer
    return ctx.vals[key]


class SignedNormalizer(Normalizer):
    def value(self, value, key=None):
        from pseudolife_memory.maintainer_webauthn import (
            b64url_decode, b64url_encode, verify_assertion)
        from pseudolife_memory.storage.maintainer import canonical, challenge_bytes

        if isinstance(value, (bytes, memoryview)):
            return bytes(value).hex()
        if key in ("active_from", "last_used_at", "revoked_at", "flagged_at", "used_at",
                   "signed_at", "at") and type(value) in (int, float):
            offsets = (0, 86400) if key == "active_from" else (0,)
            return self.windows.match(value, offsets) or self.unmatched(key, value)
        if key == "code_hash" and isinstance(value, str):
            return "<code hash>" if HEX64.fullmatch(value) else "<bad code hash>"
        if key == "payload" and isinstance(value, str):
            fields = json.loads(value)
            if canonical(fields) != value:
                self.problems.append("maintainer payload is not canonical")
            return self.value(fields)
        if isinstance(value, dict) and "payload" in value and "mac" in value:
            value = dict(value)
            payload = value["payload"]
            expected = hmac.new(bytes.fromhex(SECRET), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, value["mac"]):
                self.problems.append("maintainer response MAC does not authenticate its payload")
            value["mac"] = "<verified MAC>"
            if "publicKey" in value:
                options = dict(value["publicKey"])
                if options.get("challenge") != b64url_encode(challenge_bytes(payload)):
                    self.problems.append("WebAuthn options do not challenge the signed payload")
                options["challenge"] = "<verified challenge>"
                value["publicKey"] = options
        if isinstance(value, dict) and {"signature", "client_data_json", "payload"} <= value.keys():
            value = dict(value)
            fixtures = S.Ctx()
            signer = next((auth(fixtures, name) for name in ("primary", "spare", "third")
                           if auth(fixtures, name).id == value.get("credential_id")), None)
            try:
                if signer is None:
                    raise ValueError("unknown proof credential")
                verify_assertion(public_key=signer.cose_key(), alg=signer.alg,
                                 client_data_json=b64url_decode(value["client_data_json"]),
                                 authenticator_data=b64url_decode(value["authenticator_data"]),
                                 signature=b64url_decode(value["signature"]),
                                 challenge=challenge_bytes(value["payload"]), origin=ORIGIN, rp_id=RP_ID)
                client = json.loads(b64url_decode(value["client_data_json"]))
                client["challenge"] = "<verified challenge>"
                value["client_data_json"] = client
                value["signature"] = "<verified signature>"
            except ValueError as exc:
                self.problems.append(f"invalid stored maintainer proof: {exc}")
        return super().value(value, key)


def snapshot(dsn, _ctx):
    """Exact same-arm durable state: a refusal must not spend or mutate it."""
    import psycopg
    from board.run import BOARD_TABLES

    with psycopg.connect(dsn, autocommit=True) as conn:
        return {table: sorted(conn.execute(f"SELECT row_to_json(t)::text FROM {table} t").fetchall())
                for table in (*BOARD_TABLES, *TABLES)}


def post(title, route, body=None, *, expected=200, error=None, save=None, token="default",
         rollback=False):
    step = S.Step(title, path="/api/maintainer/" + route, body={} if body is None else body,
                  token=token, save=save)
    if rollback:
        step.before = snapshot

    def check(dsn, ctx, response, before):
        assert response["status"] == expected, (title, response)
        if error:
            assert response.get("json", {}).get("error") == error, (title, response)
        if rollback:
            assert snapshot(dsn, ctx) == before, f"{title}: refused action changed durable state"
    step.check = check
    return step


def challenge(name, purpose, fields=None, **kwargs):
    def body(ctx):
        values = fields(ctx) if callable(fields) else fields or {}
        return {"purpose": purpose, **values}
    return post("challenge " + name, "challenge", body,
                save=lambda ctx, out: ctx.vals.update({name: out}), **kwargs)


def complete(name, route, *, source=None, signer="primary", edit=None, reuse=False,
             save=None, **kwargs):
    def body(ctx):
        from pseudolife_memory.storage.maintainer import challenge_bytes

        if reuse:
            out = copy.deepcopy(ctx.vals["body:" + name])
        else:
            issued = ctx.vals[source or name]
            out = {"payload": issued["payload"], "mac": issued["mac"],
                   "assertion": auth(ctx, signer).assertion(challenge_bytes(issued["payload"]))}
        if edit:
            edit(ctx, out)
        ctx.vals["body:" + name] = copy.deepcopy(out)
        return out
    return post("complete " + name, route, body, save=save, **kwargs)


def host(title, fn):
    from pseudolife_memory.storage.maintainer import MaintainerStore

    def seed(board, ctx):
        fn(MaintainerStore(board.storage, rp_id=RP_ID, origin=ORIGIN, clock=board.clock), ctx)
    return S.seed(title, seed)


def bootstrap(*, confirm=True, wrong_code=False):
    def prepare(store, ctx):
        from psycopg.types.json import Jsonb
        store.storage.conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s) "
                                   "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                                   ("maintainer_secret_v1", Jsonb(SECRET)))
        ctx.vals["code"] = store.bootstrap_code()

    def registration(ctx):
        from pseudolife_memory.storage.maintainer import challenge_bytes
        issued = ctx.vals["bootstrap"]
        return {"payload": issued["payload"], "mac": issued["mac"], "code": ctx.vals["code"],
                "attestation": auth(ctx).register(challenge_bytes(issued["payload"]))}

    steps = [host("host issues bootstrap code", prepare),
             challenge("bootstrap", "enrol-bootstrap", {"label": "Primary fixture"})]
    if wrong_code:
        steps.append(post("wrong bootstrap code preserves registration nonce", "enrol",
                          lambda ctx: {**registration(ctx), "code": "0000000000"},
                          expected=403, error="bootstrap_code_invalid"))
    steps.append(post("bootstrap enrolment", "enrol", registration))
    if confirm:
        steps.append(host("host confirms primary key", lambda store, ctx: store.confirm(auth(ctx).id)))
    return steps


def add_key(name):
    def registration(ctx, *, origin=None):
        from pseudolife_memory.storage.maintainer import challenge_bytes
        issued = ctx.vals["approved:" + name]
        return {"payload": issued["payload"], "mac": issued["mac"],
                "attestation": auth(ctx, name).register(challenge_bytes(issued["payload"]), origin=origin)}
    return [challenge("approval:" + name, "enrol-approve", {"label": name}),
            complete("approval:" + name, "enrol",
                     save=lambda ctx, out: ctx.vals.update({"approved:" + name: out})),
            post("wrong-origin approved " + name, "enrol",
                 lambda ctx: registration(ctx, origin="https://other.example.com"),
                 expected=403, error="assertion_invalid", rollback=True),
            post("register approved " + name, "enrol", registration)]


def status():
    return S.Step("maintainer status and audit actors", method="GET", path="/api/maintainer")


class MaintainerScenario(S.Scenario):
    config_yaml = CONFIG
    normalizer = SignedNormalizer
    extra_tables = TABLES


class Enrol(MaintainerScenario):
    name = "maintainer-enrol"

    def steps(self):
        return [*bootstrap(confirm=False, wrong_code=True),
                challenge("closed bootstrap", "enrol-bootstrap", {"label": "Too late"},
                          expected=409, error="enrolment_closed", rollback=True),
                host("host confirms primary key", lambda store, ctx: store.confirm(auth(ctx).id)),
                *add_key("spare"),
                challenge("quarantined", "revoke-self", lambda ctx: {"credential_id": auth(ctx, "spare").id}),
                complete("quarantined", "revoke", signer="spare", expected=403,
                         error="assertion_invalid", rollback=True),
                challenge("cancel spare", "cancel", lambda ctx: {"credential_id": auth(ctx, "spare").id}),
                complete("cancel spare", "cancel"),
                challenge("cancel missing", "cancel", {"credential_id": "missing"}),
                complete("cancel missing", "cancel", expected=404, error="credential_not_found", rollback=True),
                challenge("revoke other", "revoke-self", lambda ctx: {"credential_id": auth(ctx, "spare").id}),
                complete("revoke other", "revoke", expected=403, error="assertion_invalid", rollback=True),
                challenge("revoke primary", "revoke-self", lambda ctx: {"credential_id": auth(ctx).id}),
                complete("revoke primary", "revoke"), status()]


class Roles(MaintainerScenario):
    name = "maintainer-roles"

    def steps(self):
        fields = lambda ctx: {"project": "p", "agent_id": ctx.id("A"), "hold": 3600}
        return [*bootstrap(), S.reg("register role recipient", "A", project="p", label="Role fixture"),
                challenge("grant", "grant-conductor", fields), complete("grant", "role"),
                challenge("assign", "assign-coordinator", fields), complete("assign", "role"),
                challenge("stale", "grant-conductor", fields),
                S.seed("operator replaces coordinator after preview",
                       lambda store, ctx: store.break_lease("coordinator:p")),
                complete("stale", "role", expected=409, error="role_changed", rollback=True),
                S.seed("operator restores the approved other holder",
                       lambda store, ctx: store.assign_coordinator("p", ctx.id("A"), hold=3600)),
                complete("stale", "role", reuse=True),
                complete("spent role nonce", "role", source="stale", expected=410,
                         error="challenge_spent", rollback=True),
                challenge("revoke conductor", "revoke-conductor", {"project": "p"}),
                complete("revoke conductor", "role"),
                challenge("legacy", "grant-delegate", fields), complete("legacy", "role"),
                challenge("legacy revoke", "revoke-delegate", {"project": "p"}),
                complete("legacy revoke", "role"),
                challenge("revoke coordinator", "revoke-coordinator", {"project": "p"}),
                complete("revoke coordinator", "role"), status()]


class Mail(MaintainerScenario):
    name = "maintainer-mail"

    def steps(self):
        fields = lambda ctx: {"to": ctx.id("A"), "text": "Please inspect the fixture.", "urgent": False}
        save = lambda key: lambda ctx, out: ctx.vals.update({key: out["message_id"]})
        mid = lambda key: lambda ctx: {"message_id": ctx.vals[key]}
        return [*bootstrap(), S.reg("register mail recipient", "A", project="p", label="Mail fixture"),
                challenge("mail", "send", fields), complete("mail", "send", save=save("mail-id")),
                complete("spent send nonce", "send", source="mail", expected=410,
                         error="challenge_spent", rollback=True),
                challenge("withdraw unread", "repudiate", mid("mail-id")),
                complete("withdraw unread", "repudiate"),
                S.act("receive withdrawn text", "receive", agent="A", body={"ack": True}),
                challenge("ack mail", "send", fields), complete("ack mail", "send", save=save("ack-id")),
                S.act("acknowledge maintainer message", "receive", agent="A", body={"ack": True}),
                challenge("withdraw acknowledged", "repudiate", mid("ack-id")),
                complete("withdraw acknowledged", "repudiate"),
                S.act("receive withdrawal follow-up", "receive", agent="A"),
                S.Step("maintainer sent log", method="GET", path="/api/maintainer/sent"), status()]


class Refusals(MaintainerScenario):
    name = "maintainer-refusals"

    def steps(self):
        fields = lambda ctx: {"project": "p", "agent_id": ctx.id("A"), "hold": 3600}
        def bad_signature(ctx, body):
            from pseudolife_memory.maintainer_webauthn import b64url_encode
            body["assertion"]["response"]["signature"] = b64url_encode(b"\0" * 64)
        def stale_count(ctx, body):
            from pseudolife_memory.storage.maintainer import challenge_bytes
            body["assertion"] = auth(ctx).assertion(challenge_bytes(body["payload"]), sign_count=1)
        def expired(ctx, body):
            from pseudolife_memory.storage.maintainer import canonical, challenge_bytes
            fields = json.loads(body["payload"])
            fields["expires_at"] = ctx.now - 1
            body["payload"] = canonical(fields)
            body["mac"] = hmac.new(bytes.fromhex(SECRET), body["payload"].encode(), hashlib.sha256).hexdigest()
            body["assertion"] = auth(ctx).assertion(challenge_bytes(body["payload"]))
        steps = [*bootstrap(), S.reg("register role recipient", "A", project="p"),
                 challenge("refusals", "grant-conductor", fields)]
        for route in ("enrol", "role", "send", "cancel", "repudiate", "revoke"):
            steps.append(post("bearer alone cannot " + route, route, expected=403,
                              error="assertion_invalid", rollback=route not in ("send", "repudiate")))
        steps.extend([
            complete("bad signature", "role", source="refusals", edit=bad_signature,
                     expected=403, error="assertion_invalid", rollback=True),
            complete("bad MAC", "role", source="refusals",
                     edit=lambda ctx, body: body.update(mac="0" * 64),
                     expected=403, error="assertion_invalid", rollback=True),
            complete("expired challenge", "role", source="refusals", edit=expired,
                     expected=410, error="challenge_expired", rollback=True),
            complete("wrong purpose", "cancel", source="refusals", expected=403,
                     error="assertion_invalid", rollback=True),
            complete("unknown key", "role", source="refusals", signer="third", expected=403,
                     error="assertion_invalid", rollback=True),
            complete("disallowed actor", "role", source="refusals", token="carol", expected=403,
                     error="principal_not_allowed", rollback=True),
            complete("missing bearer", "role", source="refusals", token=None, expected=401,
                     error="unauthorized", rollback=True),
            complete("counter regression", "role", source="refusals", edit=stale_count,
                     expected=403, error="assertion_invalid"),
            complete("still usable after refusals", "role", source="refusals"), status()])
        return steps


SCENARIOS = {scenario.name: scenario for scenario in (Enrol, Roles, Mail, Refusals)}
