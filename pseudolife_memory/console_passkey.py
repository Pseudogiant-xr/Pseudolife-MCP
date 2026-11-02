"""Console sign-in with the maintainer passkey (schema v59; spec 2026-11-02,
section 5).

At the passkey origin the browser mints a token first, then asks ``POST
/api/console/passkey-challenge`` for a challenge whose MAC'd payload (purpose
``sign-in``) carries that token's SHA-256 and the browser's label, so the
assertion signs exactly which token the session will accept. It signs, and
posts ``{payload, mac, assertion}`` to ``POST /api/console/session``, which
verifies through the maintainer store's ``complete()`` (past quarantine, sign
count forward, nonce spent once) and creates the session in the same
transaction. The session acts as the key's **sign-in principal**
(``maintainer_passkeys.signs_in_as``), never as ``maintainer``.

A key's sign-in principal is set when it is enrolled (the enrolling request's
principal), filled once by the v59 migration from its enrolment event, and
changed only by the signed action **Use this passkey to sign in** (purpose
``bind-sign-in``, ``POST /api/console/passkey-bind``), which binds it to the
caller's principal, an owner principal only.

The challenge route is pre-auth, so it reads the signing keys and the
challenge secret on its own short-lived connection, never the board's
mailbox connection, and never writes: no secret yet means no challenge.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

from pseudolife_memory.maintainer_webauthn import b64url_encode

SIGN_IN = "sign-in"
BIND_SIGN_IN = "bind-sign-in"

#: The refusal text for a key that cannot sign anyone in, naming the way out.
UNAVAILABLE_HINT = (
    "this passkey has no Console sign-in, or the principal it signs in as no longer exists. "
    "On the daemon's host run `pseudolife-mcp console`, open Settings, Your passkeys, and choose "
    "\"Use this passkey to sign in\"; after that this passkey signs the Console in")
NO_KEYS_HINT = (
    "no passkey can sign yet: none is set up (`pseudolife-mcp maintainer setup` on the daemon's "
    "host), the first one still waits for the host's `pseudolife-mcp maintainer confirm "
    "<prefix>`, or a newly added one is still in its 24-hour wait. Meanwhile sign in with "
    "`pseudolife-mcp console`")


def _secret(conn) -> bytes | None:
    """The challenge secret, read only: ``None`` when it does not exist yet
    (an unauthenticated caller must never create it)."""
    from pseudolife_memory.storage.maintainer import SECRET_META_KEY
    row = conn.execute("SELECT value FROM public.meta WHERE key = %s",
                       (SECRET_META_KEY,)).fetchone()
    value = row[0] if row else None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return None
    return bytes.fromhex(value)


def issue_signin_challenge(dsn: str, *, token_hash: str, label: str, rp_id: str,
                           clock=time.time) -> dict:
    """A ``sign-in`` challenge for the browser's ``token_hash`` and its
    WebAuthn request options, listing only the keys that can sign in (active,
    past quarantine, with a sign-in principal); else ``{"error": "no_keys"}``
    (no secret yet, or no key can sign) or ``{"error": "unbound"}`` (keys
    can sign, but none has a sign-in principal), so the person is told before
    the passkey prompt. Read-only, on its own connection."""
    from pseudolife_memory.console_sessions import _connect
    from pseudolife_memory.storage.maintainer import CHALLENGE_TTL, canonical, challenge_bytes

    now = clock()
    with _connect(dsn, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '5s'")
        secret = _secret(conn)
        signing = [(r[0], r[1]) for r in conn.execute(
            "SELECT credential_id, signs_in_as FROM public.maintainer_passkeys "
            "WHERE state = 'active' AND active_from IS NOT NULL AND active_from <= %s "
            "ORDER BY created_at, credential_id", (now,))]
    if secret is None or not signing:
        return {"error": "no_keys"}
    keys = [cid for cid, principal in signing if principal]
    if not keys:
        return {"error": "unbound"}
    payload = canonical({"token_sha256": token_hash, "label": label, "purpose": SIGN_IN,
                         "nonce": secrets.token_hex(16),
                         "expires_at": round(now + CHALLENGE_TTL, 3)})
    mac = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    options = {"challenge": b64url_encode(challenge_bytes(payload)), "rpId": rp_id,
               "userVerification": "required", "timeout": CHALLENGE_TTL * 1000,
               "allowCredentials": [{"type": "public-key", "id": cid} for cid in keys]}
    return {"payload": payload, "mac": mac, "publicKey": options}


def complete_signin(service, body: dict, *, principal_source, publish=None):
    """Verify a ``sign-in`` assertion and create its session, in one
    transaction under the coordination lock, as every maintainer completion
    does. ``principal_source(name)`` is ``environment``, ``store`` or
    ``None`` (no longer resolves). Raises ``MaintainerError``:
    ``assertion_invalid`` / ``challenge_*`` for any bad assertion,
    ``passkey_signin_unavailable`` for a key with no sign-in principal or
    one that no longer resolves. ``publish(session)`` (the daemon's
    snapshot ``add``) runs before the coordination lock is released, so a
    rebind of the same key, which drops its sessions under that lock,
    cannot run between the commit and the publication and be undone by
    it."""
    import psycopg

    from pseudolife_memory import coordination
    from pseudolife_memory.console_sessions import (
        METHOD_PASSKEY, clean_label, insert_session)
    from pseudolife_memory.principals import is_sha256_hex
    from pseudolife_memory.storage.maintainer import MaintainerError, MaintainerStore

    cfg = service.config.coordination
    if not cfg.enabled or not cfg.maintainer.configured:
        raise MaintainerError("maintainer_https_required", check="signin_config")
    try:
        coordination._ensure_tier(service, full=False)
    except ValueError:
        raise MaintainerError("coordination_unavailable", check="tier") from None

    def action(fields, key):
        token_hash, label = fields.get("token_sha256"), clean_label(fields.get("label"))
        if not is_sha256_hex(token_hash) or label is None:
            raise MaintainerError("assertion_invalid", check="signin_fields")
        principal = key.get("signs_in_as")
        source = principal_source(principal) if principal else None
        if source is None:
            raise MaintainerError("passkey_signin_unavailable", check="signs_in_as")
        try:
            return insert_session(board.storage.conn, token_hash=token_hash,
                                  principal=principal, source=source,
                                  issuer=key["credential_id"], method=METHOD_PASSKEY,
                                  label=label)
        except psycopg.errors.UniqueViolation:
            raise MaintainerError("assertion_invalid", check="token_in_use") from None

    with service._coordination_lock:
        board = coordination._store(service)
        # A pre-auth route never creates the challenge secret (the store's
        # MAC check would): no secret, no challenge was ever issued.
        if _secret(board.storage.conn) is None:
            raise MaintainerError("assertion_invalid", check="no_secret")
        store = MaintainerStore(board.storage, rp_id=cfg.maintainer.rp_id,
                                origin=cfg.maintainer.origin, clock=board.clock, principal="")
        session = store.complete(body.get("payload"), body.get("mac"), SIGN_IN,
                                 body.get("assertion"), action)
        if publish is not None:
            publish(session)
        return session


def _bind_challenge(service, board, store, principal):
    from pseudolife_memory.maintainer import _get_options
    payload, mac = store.issue(BIND_SIGN_IN, {"principal": principal})
    return {"payload": payload, "mac": mac, "publicKey": _get_options(store, payload)}


def _bind_complete(service, board, store, body, principal):
    out = store.bind_sign_in(body.get("payload"), body.get("mac"), body.get("assertion"),
                             principal=principal)
    # Still under the coordination lock (maintainer._run), as a passkey
    # sign-in's publication is: the key's other sessions leave the view
    # at once and no sign-in committed before can put one back.
    from pseudolife_memory.console_sessions import installed_sessions
    sessions = installed_sessions()
    if sessions is not None:
        sessions.drop_issuer(out["credential_id"], keep_principal=out["signs_in_as"])
    from pseudolife_memory.storage.maintainer import key_prefix
    return {**out, "_notice": (f"key {key_prefix(out['credential_id'])} now signs the Console "
                               f"in as {out['signs_in_as']}")}


def bind(service, body: dict, principal: str, *, is_owner) -> dict:
    """``POST /api/console/passkey-bind``: ``{}`` asks for a ``bind-sign-in``
    challenge naming the caller's principal; ``{payload, mac, assertion}``
    completes it. Through the maintainer routes' gate and lock. Only an
    owner principal may bind a key (``is_owner(principal)``)."""
    from pseudolife_memory import maintainer
    from pseudolife_memory.storage.maintainer import MaintainerError

    if not is_owner(principal):
        raise MaintainerError("principal_not_allowed", check="bind_not_owner",
                              public={"reason": "not_owner"})
    if not body:
        return maintainer._run(service, _bind_challenge, principal)
    if set(body) != {"payload", "mac", "assertion"}:
        raise MaintainerError("invalid_request", check="bind_body")
    return maintainer._run(service, _bind_complete, body, principal)
