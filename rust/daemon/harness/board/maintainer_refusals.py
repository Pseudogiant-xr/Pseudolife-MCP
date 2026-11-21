"""Boundary fixtures for signed Console refusals, on disposable banks only."""
from __future__ import annotations

import hashlib

from board import scenarios as S
from board.maintainer import (
    MaintainerScenario, SignedNormalizer, add_key, auth, bootstrap, challenge,
    complete, host, post, status,
)
from board.host_fixtures import insert_spare, issue_bootstrap, operator_role, revoke_key

ROLE_PROJECT = "w2f-maintainer-refusal-fixture"


class FixtureNormalizer(SignedNormalizer):
    def value(self, value, key=None):
        # Explicit old fixture timestamps are inputs, compared exactly.
        if key in ("created_at", "expires_at") and value == 1:
            return value
        return super().value(value, key)


class RefusalScenario(MaintainerScenario):
    normalizer = FixtureNormalizer


def registration(ctx, source, name="primary", *, code=False):
    from pseudolife_memory.storage.maintainer import challenge_bytes
    issued = ctx.vals[source]
    out = {"payload": issued["payload"], "mac": issued["mac"],
           "attestation": auth(ctx, name).register(challenge_bytes(issued["payload"]))}
    if code:
        out["code"] = ctx.vals["code"]
    return out


def seed_key(rows, ctx):
    insert_spare(rows, auth(ctx, "spare"), "spare")


def key_update(title, name, assignments):
    return host(title, lambda rows, ctx: rows.conn.execute(
        "UPDATE maintainer_passkeys SET " + assignments + " WHERE credential_id=%s",
        (auth(ctx, name).id,)))


def role_fields(ctx):
    return {"project": ROLE_PROJECT, "agent_id": ctx.id("A"), "hold": 3600}


def mail_fields(ctx):
    return {"to": ctx.id("A"), "text": "Inspect the refusal fixture.", "urgent": False}


def save_id(key):
    return lambda ctx, out: ctx.vals.update({key: out["message_id"]})


class BootstrapRefusals(RefusalScenario):
    name = "maintainer-bootstrap-refusals"

    def steps(self):
        steps = bootstrap()[:2]
        for number in range(5):
            steps.append(post(f"bootstrap wrong guess {number + 1}", "enrol",
                              lambda ctx: {**registration(ctx, "bootstrap", code=True), "code": "0000000000"},
                              expected=403, error="bootstrap_code_invalid"))
        steps.extend([
            post("burned code refuses the correct guess", "enrol",
                 lambda ctx: registration(ctx, "bootstrap", code=True), expected=403,
                 error="bootstrap_code_invalid", rollback=True),
            host("issue replacement code", issue_bootstrap),
            host("expire the host code", lambda rows, _ctx: rows.conn.execute(
                "UPDATE maintainer_bootstrap SET expires_at=1 WHERE used_at IS NULL")),
            post("expired host code", "enrol", lambda ctx: registration(ctx, "bootstrap", code=True),
                 expected=403, error="bootstrap_code_invalid", rollback=True),
            host("replace expired host code", issue_bootstrap),
            host("another key becomes live after preview", seed_key),
            post("bootstrap completion rechecks live keys", "enrol",
                 lambda ctx: registration(ctx, "bootstrap", code=True), expected=409,
                 error="enrolment_closed", rollback=True),
            key_update("retire the competing key", "spare", "state='revoked'"),
            post("revoked credential id cannot be registered again", "enrol",
                 lambda ctx: registration(ctx, "bootstrap", "spare", code=True), expected=403,
                 error="assertion_invalid", rollback=True),
            post("same unspent bootstrap challenge can still enrol", "enrol",
                 lambda ctx: registration(ctx, "bootstrap", code=True)), status()])
        return steps


class KeyRefusals(RefusalScenario):
    name = "maintainer-key-refusals"

    def steps(self):
        return [*bootstrap(), *add_key("spare"),
                key_update("spare quarantine ends", "spare", "active_from=created_at"),
                challenge("cancel mature", "cancel", lambda ctx: {"credential_id": auth(ctx, "spare").id}),
                complete("cancel mature", "cancel", expected=403, error="assertion_invalid", rollback=True),
                host("older primary reenters quarantine", lambda rows, ctx: rows.conn.execute(
                    "UPDATE maintainer_passkeys SET active_from=%s WHERE credential_id=%s",
                    (rows.now + 86400, auth(ctx).id))),
                challenge("newer cannot cancel older", "cancel", lambda ctx: {"credential_id": auth(ctx).id}),
                complete("newer cannot cancel older", "cancel", signer="spare", expected=403,
                         error="assertion_invalid", rollback=True),
                key_update("restore primary activation", "primary", "active_from=created_at"),
                host("host revokes spare", lambda rows, ctx: revoke_key(rows, auth(ctx, "spare").id)),
                challenge("cancel revoked", "cancel", lambda ctx: {"credential_id": auth(ctx, "spare").id}),
                complete("cancel revoked", "cancel", expected=403, error="assertion_invalid", rollback=True),
                challenge("approval invalidated", "enrol-approve", {"label": "third"}),
                complete("approval invalidated", "enrol",
                         save=lambda ctx, out: ctx.vals.update({"third-approved": out})),
                host("host revokes approving primary", lambda rows, ctx: revoke_key(rows, auth(ctx).id)),
                post("registration refuses a revoked approver", "enrol",
                     lambda ctx: registration(ctx, "third-approved", "third"), expected=403,
                     error="assertion_invalid", rollback=True), status()]


def agent_update(title, assignments, params=()):
    return host(title, lambda rows, ctx: rows.conn.execute(
        "UPDATE coordination_agents SET " + assignments + " WHERE agent_id=%s", (*params, ctx.id("A"))))


class RecipientRefusals(RefusalScenario):
    name = "maintainer-recipient-refusals"

    def steps(self):
        def remove(rows, ctx):
            ctx.vals["removed-agent"] = rows.one("SELECT * FROM coordination_agents WHERE agent_id=%s",
                                                 (ctx.id("A"),))
            rows.conn.execute("DELETE FROM coordination_messages WHERE recipient_agent_id=%s "
                              "OR sender_agent_id=%s", (ctx.id("A"), ctx.id("A")))
            rows.conn.execute("DELETE FROM coordination_agents WHERE agent_id=%s", (ctx.id("A"),))
        def restore(rows, ctx):
            rows.insert("coordination_agents", [ctx.vals["removed-agent"]])
        steps = [*bootstrap(), S.reg("register boundary recipient", "A", project=ROLE_PROJECT),
                 challenge("prime mail identity", "send", mail_fields),
                 complete("prime mail identity", "send"),
                 challenge("recipient role", "grant-conductor", role_fields),
                 challenge("recipient mail", "send", mail_fields), host("remove recipient after preview", remove)]
        for suffix, mutation, code in (
            ("removed", None, "recipient_unknown"),
            ("reserved", "principal='daemon'", "recipient_reserved"),
            ("subagent", "principal='default',parent_thread='00000000-0000-0000-0000-000000000001'", "recipient_reserved"),
            ("revoked", "parent_thread=NULL,credential_hash=NULL", "recipient_unknown"),
        ):
            if mutation:
                steps.append(agent_update("make recipient " + suffix, mutation))
            for route, source in (("role", "recipient role"), ("send", "recipient mail")):
                steps.append(complete(f"{route} refuses {suffix}", route, source=source,
                                      expected=404 if code == "recipient_unknown" else 400,
                                      error=code, rollback=True))
            if suffix == "removed":
                steps.append(host("restore removed recipient", restore))
        steps.extend([
            host("restore credential and move project", lambda rows, ctx: rows.conn.execute(
                "UPDATE coordination_agents SET credential_hash=%s,project='other' WHERE agent_id=%s",
                (hashlib.sha256(ctx.agents["A"]["credential"].encode()).hexdigest(), ctx.id("A")))),
            complete("role refuses moved project", "role", source="recipient role", expected=400,
                     error="invalid_request", rollback=True),
            agent_update("restore recipient project", "project=%s", (ROLE_PROJECT,)),
            host("operator changes the signed primary holder",
                 lambda rows, ctx: operator_role(rows, ROLE_PROJECT, ctx.id("A"))),
            complete("role refuses changed primary holder", "role", source="recipient role", expected=409,
                     error="role_changed", rollback=True), status()])
        return steps


def clone_mail(rows, ctx, *, count, old=False):
    """Populate only the rate/queue inputs, copying one genuine signed row."""
    source = rows.one("SELECT * FROM coordination_messages WHERE message_id=%s", (ctx.vals["mail-id"],))
    copies = []
    for index in range(1, count + 1):
        request = f"boundary-fixture-{index}"
        message = hashlib.md5(request.encode("ascii"), usedforsecurity=False).hexdigest()
        copies.append(dict(source, message_id=message, request_id=request,
                           recipient_sequence=source["recipient_sequence"] + index,
                           created_at=1 if old else source["created_at"]))
    rows.insert("coordination_messages", copies)


class MailRefusals(RefusalScenario):
    name = "maintainer-mail-refusals"

    def steps(self):
        return [*bootstrap(), S.reg("register mailbox recipient", "A", project="p"),
                challenge("initial", "send", mail_fields), complete("initial", "send", save=save_id("mail-id")),
                challenge("capacity", "send", mail_fields),
                host("fill sender minute rate", lambda rows, ctx: clone_mail(rows, ctx, count=60)),
                complete("sender cap", "send", source="capacity", expected=429, error="rate_capped", rollback=True),
                host("clear synthetic rate rows", lambda rows, _ctx: rows.conn.execute(
                    "DELETE FROM coordination_messages WHERE request_id LIKE 'boundary-fixture-%'")),
                host("fill recipient queue with old unacknowledged mail",
                     lambda rows, ctx: clone_mail(rows, ctx, count=256, old=True)),
                complete("mailbox cap", "send", source="capacity", expected=429, error="rate_capped", rollback=True),
                host("free recipient queue", lambda rows, _ctx: rows.conn.execute(
                    "DELETE FROM coordination_messages WHERE request_id LIKE 'boundary-fixture-%'")),
                complete("mailbox cap", "send", reuse=True),
                challenge("vanishing message", "repudiate", lambda ctx: {"message_id": ctx.vals["mail-id"]}),
                host("remove message after withdrawal preview", lambda rows, ctx: rows.conn.execute(
                    "DELETE FROM coordination_messages WHERE message_id=%s", (ctx.vals["mail-id"],))),
                complete("missing withdrawal message", "repudiate", source="vanishing message", expected=404,
                         error="message_not_found", rollback=True),
                challenge("revoked sender mail", "send", mail_fields),
                complete("revoked sender mail", "send", save=save_id("revoke-id")),
                challenge("revoked withdrawal", "repudiate", lambda ctx: {"message_id": ctx.vals["revoke-id"]}),
                host("revoke withdrawal signer", lambda rows, ctx: revoke_key(rows, auth(ctx).id)),
                complete("revoked withdrawal", "repudiate", expected=403, error="assertion_invalid", rollback=True),
                status()]


class AssertionRefusals(RefusalScenario):
    name = "maintainer-assertion-refusals"

    def steps(self):
        def assertion(**kwargs):
            def edit(ctx, body):
                from pseudolife_memory.storage.maintainer import challenge_bytes
                values = {key: value(ctx) if callable(value) else value for key, value in kwargs.items()}
                signed_over = values.pop("challenge", challenge_bytes(body["payload"]))
                body["assertion"] = auth(ctx).assertion(signed_over, **values)
            return edit
        mutations = [
            ("missing UV", assertion(flags=0x01)),
            ("wrong origin", assertion(origin="https://other.example.com")),
            ("wrong RP", assertion(auth_data=lambda ctx: auth(ctx).auth_data(rp_id="other.example.com"))),
            ("wrong WebAuthn challenge", assertion(challenge=b"other challenge")),
            ("malformed credential", lambda ctx, body: body["assertion"].update(id="!")),
            ("mismatched raw credential", lambda ctx, body: body["assertion"].update(rawId=auth(ctx, "spare").id)),
            ("missing response", lambda ctx, body: body["assertion"].pop("response")),
            ("missing signature", lambda ctx, body: body["assertion"]["response"].pop("signature")),
            ("malformed client data", lambda ctx, body: body["assertion"]["response"].update(clientDataJSON="!")),
            ("truncated authenticator data", assertion(auth_data=b"\0" * 36)),
        ]
        steps = [*bootstrap(), S.reg("register assertion recipient", "A", project=ROLE_PROJECT),
                 challenge("assertions", "grant-conductor", role_fields)]
        for name, edit in mutations:
            steps.append(complete(name, "role", source="assertions", edit=edit, expected=403,
                                  error="assertion_invalid", rollback=True))
        for route in ("enrol", "role", "send", "cancel", "repudiate", "revoke"):
            steps.append(complete("disallowed principal on " + route, route, source="assertions",
                                  token="carol", expected=403, error="principal_not_allowed", rollback=True))
        steps.extend([
            key_update("primary becomes pending", "primary", "state='pending',active_from=NULL"),
            complete("pending key cannot sign", "role", source="assertions", expected=403,
                     error="assertion_invalid", rollback=True),
            key_update("primary becomes revoked", "primary", "state='revoked'"),
            complete("revoked key cannot sign", "role", source="assertions", expected=403,
                     error="assertion_invalid", rollback=True), status()])
        return steps


class ZeroCounterReplay(RefusalScenario):
    name = "maintainer-zero-counter-replay"

    def steps(self):
        setup = bootstrap()
        return [*setup[:2], host("authenticator has no counter", lambda store, ctx: setattr(auth(ctx), "sign_count", 0)),
                *setup[2:], S.reg("register zero-counter recipient", "A", project="p"),
                challenge("zero-counter mail", "send", mail_fields), complete("zero-counter mail", "send"),
                complete("zero-counter mail", "send", reuse=True, expected=410,
                         error="challenge_spent", rollback=True), status()]


SCENARIOS = {scenario.name: scenario for scenario in (
    BootstrapRefusals, KeyRefusals, RecipientRefusals, MailRefusals, AssertionRefusals, ZeroCounterReplay)}
