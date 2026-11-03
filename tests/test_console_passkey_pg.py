"""Console sign-in with the maintainer passkey against PostgreSQL (schema
v59; spec 2026-11-02, section 5), with the software authenticator.

The challenge carries the browser token's SHA-256 under the purpose
``sign-in``, so an assertion signs exactly which token the session will
accept; completion runs the maintainer store's checks and creates the
session in the same transaction, as the key's sign-in principal. Only the
signed ``bind-sign-in`` action changes that principal, and a revoked key's
sign-ins end with it.
"""
from __future__ import annotations

import json

import pytest

from pseudolife_memory import console_passkey, console_sessions
from pseudolife_memory.principals import secret_sha256
from pseudolife_memory.storage.maintainer import challenge_bytes
from tests.maintainer_authenticator import ORIGIN, RP_ID, SoftAuthenticator  # noqa: F401
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_maintainer_messages import bootstrap, bound, host, refused, sql, svc  # noqa: F401

TOKEN = "fixture-passkey-browser-token"


@pytest.fixture(autouse=True)
def _principal_is_the_owners(monkeypatch, request):
    """PRINCIPAL is an environment name here, so the owner's: an enrolment
    records it as the key's sign-in principal."""
    if "not_owner" not in request.node.name:
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", f"pk-fixture-token:{PRINCIPAL}")


def _env(name):
    return "environment" if name == PRINCIPAL else None


def _challenge(service, token=TOKEN, label="Firefox on Linux"):
    return console_passkey.issue_signin_challenge(
        service._db_url, token_hash=secret_sha256(token), label=label, rp_id=RP_ID)


def _sign_in(service, auth, token=TOKEN, *, source=_env, challenge=None):
    challenge = challenge or _challenge(service, token)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    return console_passkey.complete_signin(service, body, principal_source=source), body


def test_an_enrolment_records_the_principal_it_signs_in_as(svc):
    auth = bootstrap(svc)
    assert sql(svc, "SELECT signs_in_as FROM maintainer_passkeys WHERE credential_id=%s",
               (auth.id,)) == [(PRINCIPAL,)]


def test_no_challenge_before_a_key_can_sign_and_the_route_never_writes(svc):
    assert _challenge(svc) == {"error": "no_keys"}
    assert sql(svc, "SELECT count(*) FROM meta WHERE key='maintainer_secret_v1'") == [(0,)]


def test_the_challenge_binds_the_token_and_lists_the_signing_keys(svc):
    auth = bootstrap(svc)
    out = _challenge(svc)
    fields = json.loads(out["payload"])
    assert fields["purpose"] == "sign-in" and fields["token_sha256"] == secret_sha256(TOKEN)
    assert fields["label"] == "Firefox on Linux"
    assert [c["id"] for c in out["publicKey"]["allowCredentials"]] == [auth.id]
    assert out["publicKey"]["rpId"] == RP_ID


def test_a_passkey_signs_the_browser_in_as_the_keys_principal(svc):
    auth = bootstrap(svc)
    session, _ = _sign_in(svc, auth)
    assert (session.principal, session.source, session.method, session.issuer) == (
        PRINCIPAL, "environment", "passkey", auth.id)
    assert session.token_hash == secret_sha256(TOKEN)
    with _conn(svc) as conn:
        rows, keys = console_sessions.load_sessions(conn)
    assert [r.session_id for r in rows] == [session.session_id] and keys == [auth.id]


def _conn(service):
    import psycopg
    return psycopg.connect(service._db_url, autocommit=True)


def test_a_replayed_assertion_is_refused(svc):
    auth = bootstrap(svc)
    _, body = _sign_in(svc, auth)
    refused("challenge_spent", console_passkey.complete_signin, svc, body,
            principal_source=_env)


def test_a_challenge_for_another_token_cannot_be_reused(svc):
    auth = bootstrap(svc)
    challenge = _challenge(svc)
    fields = json.loads(challenge["payload"])
    fields["token_sha256"] = secret_sha256("the-attackers-token")
    forged = {**challenge, "payload": json.dumps(fields, sort_keys=True, separators=(",", ":"))}
    refused("assertion_invalid", _sign_in, svc, auth, challenge=forged)


def test_a_payload_for_another_purpose_is_refused(svc):
    from tests.test_maintainer_messages import call
    auth = bootstrap(svc)
    other = call(svc, "challenge", {"purpose": "revoke-self", "credential_id": auth.id})
    refused("assertion_invalid", _sign_in, svc, auth, challenge=other)


def test_a_key_with_no_sign_in_principal_names_the_recovery(svc):
    auth = bootstrap(svc)
    challenge = _challenge(svc)
    sql(svc, "UPDATE maintainer_passkeys SET signs_in_as = NULL")
    # No key can sign in now: the next challenge says so before any prompt.
    assert _challenge(svc) == {"error": "unbound"}
    refused("passkey_signin_unavailable", _sign_in, svc, auth, challenge=challenge)
    # Nothing was spent: the same key signs in once it is bound.
    sql(svc, "UPDATE maintainer_passkeys SET signs_in_as = %s", (PRINCIPAL,))
    assert _sign_in(svc, auth)[0].principal == PRINCIPAL


def test_a_principal_that_no_longer_resolves_is_refused(svc):
    auth = bootstrap(svc)
    refused("passkey_signin_unavailable", _sign_in, svc, auth, source=lambda name: None)


@pytest.mark.parametrize("how", ["host-revoke", "host-reset"])
def test_a_revoked_key_ends_its_sign_ins(svc, how):
    auth = bootstrap(svc)
    session, _ = _sign_in(svc, auth)
    if how == "host-revoke":
        host(svc).revoke(auth.id[:12])
    else:
        host(svc).reset()
    with _conn(svc) as conn:
        rows, keys = console_sessions.load_sessions(conn)
    assert session.session_id not in {r.session_id for r in rows} and auth.id not in keys


def test_binding_moves_the_keys_sign_in_and_ends_its_other_sessions(svc):
    auth = bootstrap(svc)
    sql(svc, "UPDATE maintainer_passkeys SET signs_in_as = 'laptop'")
    old, _ = _sign_in(svc, auth, source=lambda name: "store")
    assert old.principal == "laptop"
    with bound(PRINCIPAL):
        challenge = console_passkey.bind(svc, {}, PRINCIPAL, is_owner=lambda name: True)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    with bound(PRINCIPAL):
        out = console_passkey.bind(svc, body, PRINCIPAL, is_owner=lambda name: True)
    assert out == {"credential_id": auth.id, "signs_in_as": PRINCIPAL}
    assert sql(svc, "SELECT signs_in_as FROM maintainer_passkeys") == [(PRINCIPAL,)]
    assert sql(svc, "SELECT revoked_at IS NOT NULL FROM console_sessions WHERE session_id=%s",
               (old.session_id,)) == [(True,)]
    changes = sql(svc, "SELECT payload FROM coordination_events WHERE event='maintainer_key' "
                       "ORDER BY seq DESC LIMIT 1")
    assert json.loads(changes[0][0])["change"] == "bind-sign-in"


def test_only_the_principal_a_bind_was_issued_for_can_complete_it(svc):
    auth = bootstrap(svc)
    with bound(PRINCIPAL):
        challenge = console_passkey.bind(svc, {}, PRINCIPAL, is_owner=lambda name: True)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    # Completed by another principal whose own gate passes.
    svc.config.coordination.allowed_principals = [PRINCIPAL, "laptop"]
    with bound("laptop"):
        refused("assertion_invalid", console_passkey.bind, svc, body, "laptop",
                is_owner=lambda name: True)


def test_only_an_owner_principal_may_bind(svc):
    bootstrap(svc)
    with bound("visitor"):
        refused("principal_not_allowed", console_passkey.bind, svc, {}, "visitor",
                is_owner=lambda name: False)


def test_a_passkey_session_is_published_before_the_coordination_lock_is_released(svc):
    auth = bootstrap(svc)
    seen = []
    challenge = _challenge(svc)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    session = console_passkey.complete_signin(
        svc, body, principal_source=_env,
        publish=lambda row: seen.append((row.session_id, svc._coordination_lock.locked())))
    assert seen == [(session.session_id, True)]
    assert not svc._coordination_lock.locked()


def test_an_enrolment_by_a_principal_not_the_owners_records_no_sign_in_not_owner(svc):
    # PRINCIPAL is admitted to the board but is neither an environment name
    # nor a stored principal here, so it is not the bank owner's.
    auth = bootstrap(svc)
    assert sql(svc, "SELECT signs_in_as FROM maintainer_passkeys WHERE credential_id=%s",
               (auth.id,)) == [(None,)]


def test_completion_never_creates_the_challenge_secret(svc):
    body = {"payload": '{"purpose":"sign-in"}', "mac": "0" * 64, "assertion": {}}
    refused("assertion_invalid", console_passkey.complete_signin, svc, body,
            principal_source=_env)
    assert sql(svc, "SELECT count(*) FROM meta WHERE key='maintainer_secret_v1'") == [(0,)]


def test_a_rebind_posts_a_notice_and_names_the_principal_in_the_record(svc):
    auth = bootstrap(svc)
    with bound(PRINCIPAL):
        challenge = console_passkey.bind(svc, {}, PRINCIPAL, is_owner=lambda name: True)
    body = {"payload": challenge["payload"], "mac": challenge["mac"],
            "assertion": auth.assertion(challenge_bytes(challenge["payload"]))}
    with bound(PRINCIPAL):
        out = console_passkey.bind(svc, body, PRINCIPAL, is_owner=lambda name: True)
    assert "_notice" not in out    # posted by _run, never returned
    [(principal,)] = sql(svc, "SELECT principal FROM coordination_events "
                              "WHERE event='maintainer_key' ORDER BY seq DESC LIMIT 1")
    assert principal == PRINCIPAL
