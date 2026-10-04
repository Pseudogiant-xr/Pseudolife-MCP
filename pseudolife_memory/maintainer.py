"""The Console's maintainer routes (schema v54).

Specs: docs/superpowers/specs/2026-10-02-maintainer-wake-design.md and its
addendum 2026-10-04-board-roles-passkey.md. The maintainer sends one board
session a message, or changes who holds a project's delegate or coordinator
role, with the maintainer's authority; the proof is a WebAuthn passkey
signature (user verification required) over every input that decides the
outcome, per action. A bearer alone can request challenges and read the
passkey list, the Sent log and the inbox, but cannot produce a maintainer
message, change a role, add a passkey once one exists, or revoke one.

:class:`MaintainerOps` carries the ten service methods the routes call, one
each (the addendum's route contract); ``MemoryService`` inherits it. Every
method needs a configured bearer (a tokenless daemon refuses them all), a
board principal, Postgres, and ``coordination.maintainer.rp_id`` and
``.origin`` set in the config file (else ``409 maintainer_https_required``).
The origin and RP ID come from config only, never from a request header:
Tailscale Serve traffic arrives from 127.0.0.1, so any local caller could
forge ``Tailscale-User-*`` or ``X-Forwarded-*``.

Runs on the coordination mailbox connection under the coordination lock,
like every board call. A key-set change made here (bootstrap enrol, an
added key, cancel, revoke) also posts a daemon notice to the attached
sessions once the lock is released: courtesy only (spec "Enrolment" 4),
since that channel is forgeable; the audit record and the Console's
passkey list are the record. Failures raise :class:`MaintainerError` (its
``status`` and ``code`` are the HTTP answer); nothing else leaves here.
"""
from __future__ import annotations

import hashlib
import logging

from pseudolife_memory.maintainer_webauthn import SUPPORTED_ALGS, b64url_encode
from pseudolife_memory.storage.coordination import (
    COORDINATOR_PREFIX, DELEGATE_PREFIX, DELEGATION_MAX, DELEGATION_MIN, MAINTAINER_PRINCIPAL,
    MAX_TEXT_BYTES, CoordinationError, looks_like_secret,
)
from pseudolife_memory.storage.maintainer import (
    CHALLENGE_PURPOSES, CHALLENGE_TTL, ROLE_PURPOSES, MaintainerError, MaintainerStore,
    challenge_bytes, check_label, key_prefix,
)

logger = logging.getLogger("pseudolife-mcp.maintainer")

WITHDRAWAL_NOTICE = "The maintainer withdrew message {message_id}; do not act on it."
KEY_NOTICE = ("Maintainer passkey change in the Console: {what}. A courtesy notice only; "
              "the Console's Settings, Your passkeys, is the record. Nothing for you to do.")
# The fields each challenge purpose takes besides ``purpose``: anything else
# in the body is refused, so nothing outside the signed payload can matter.
_CHALLENGE_FIELDS = {
    "send": {"to", "text", "urgent"},
    "enrol-approve": {"label"}, "enrol-bootstrap": {"label"},
    "cancel": {"credential_id"}, "revoke-self": {"credential_id"},
    "repudiate": {"message_id"},
    "grant-delegate": {"project", "agent_id", "hold"},
    "assign-coordinator": {"project", "agent_id", "hold"},
    "revoke-delegate": {"project"}, "revoke-coordinator": {"project"},
}
# What a board refusal means to the Console.
_BOARD_CODES = {
    "recipient_not_found": "recipient_unknown", "instance_not_found": "recipient_unknown",
    "agent_revoked": "recipient_unknown", "recipient_reserved": "recipient_reserved",
    "rate_limited": "rate_capped", "queue_full": "rate_capped",
}
_ROLE_PREFIX = {"grant-delegate": DELEGATE_PREFIX, "revoke-delegate": DELEGATE_PREFIX,
                "assign-coordinator": COORDINATOR_PREFIX,
                "revoke-coordinator": COORDINATOR_PREFIX}
# The role a grant or assignment breaks when its recipient holds it.
_OTHER_PREFIX = {"grant-delegate": COORDINATOR_PREFIX, "assign-coordinator": DELEGATE_PREFIX}
LIST_DEFAULT, LIST_MAX = 50, 200


# -- gates and stores ---------------------------------------------------------

def _gate(service) -> str:
    from pseudolife_memory.principals import env_auth, principal_admitted
    from pseudolife_memory.writer_context import request_principal
    token_map, token = env_auth()
    if not token_map and not token:
        raise MaintainerError("authentication_required", check="tokenless")
    principal = request_principal()
    if principal is None:
        raise MaintainerError("authentication_required", check="no_request")
    cfg = service.config.coordination
    if principal == MAINTAINER_PRINCIPAL or not principal_admitted(cfg, principal):
        raise MaintainerError("principal_not_allowed")
    if not cfg.enabled or not getattr(service, "_db_url", None):
        raise MaintainerError("coordination_unavailable", check="board_off")
    problem = cfg.maintainer.problem()
    if problem is not None:
        raise MaintainerError("maintainer_https_required", check=problem,
                              public={"config_problem": problem})
    return principal


def _run(service, handler, *args, full=False, body=False):
    """Gate, then ``handler(service, board, store, *args)`` under the
    coordination lock; rings the recipients a handler names in
    ``_notify`` after the lock is released, as dispatch does, and posts the
    daemon notice it names in ``_notice``."""
    from pseudolife_memory import coordination
    try:
        principal = _gate(service)
        if body and not isinstance(args[0], dict):
            raise MaintainerError("invalid_request", check="body")
        try:
            coordination._ensure_tier(service, full=full)
        except ValueError:
            raise MaintainerError("coordination_unavailable", check="tier") from None
        with service._coordination_lock:
            board = coordination._store(service)
            m = service.config.coordination.maintainer
            store = MaintainerStore(board.storage, rp_id=m.rp_id, origin=m.origin,
                                    clock=board.clock, principal=principal)
            result = handler(service, board, store, *args)
    except MaintainerError as exc:
        if exc.check:
            logger.info("maintainer route refused: %s (%s)", exc.code, exc.check)
        raise
    except Exception as exc:  # noqa: BLE001 - never echo a database error
        logger.error("maintainer route failed (%s)", type(exc).__name__)
        raise MaintainerError("coordination_unavailable") from None
    notify = getattr(service, "_coordination_notifier", None)
    for recipient in result.pop("_notify", []):
        if notify is not None:
            notify(recipient)
    notice = result.pop("_notice", None)
    if notice:
        coordination.daemon_notice(service, KEY_NOTICE.format(what=notice))
    return result


def _identity(service, board):
    """The maintainer's reserved board address, registered lazily like the
    daemon's and kept for the process (registered again if the board
    pruned it)."""
    identity = getattr(service, "_maintainer_board_identity", None)
    if identity is None:
        row = board.register(MAINTAINER_PRINCIPAL, label="maintainer", status="maintainer")
        identity = (row["agent_id"], row["credential"])
        service._maintainer_board_identity = identity
    return identity


def _board_error(exc: CoordinationError) -> MaintainerError:
    return MaintainerError(_BOARD_CODES.get(exc.code, "invalid_request"),
                           check=f"board_{exc.code}")


def _board_send(service, board, *, to, text, urgent, request_id, proof):
    epoch = {}
    if hasattr(service, "_coordination_hlc_epoch"):
        epoch = {"enforce_writer_epoch": True,
                 "expected_writer_epoch": service._coordination_hlc_epoch}
    agent_id, credential = _identity(service, board)
    try:
        return board.send(MAINTAINER_PRINCIPAL, agent_id, credential, to=to, text=text,
                          urgent=urgent, request_id=request_id, maintainer_proof=proof,
                          hlc=":".join(map(str, service._hlc.tick())), **epoch)
    except CoordinationError as exc:
        if exc.code in ("instance_not_found", "invalid_credential"):
            raise MaintainerError("_identity", check=f"board_{exc.code}") from None
        raise _board_error(exc) from None


def _with_identity(service, fn):
    """Run ``fn`` once more with a fresh maintainer address when the cached
    one is gone (the board prunes idle rows). The first attempt rolled back
    whole, so the same assertion is still unspent."""
    for attempt in range(2):
        try:
            return fn()
        except MaintainerError as exc:
            if exc.code != "_identity":
                raise
            service._maintainer_board_identity = None
            if attempt:
                raise MaintainerError("coordination_unavailable", check=exc.check) from None
    raise AssertionError("unreachable")


# -- WebAuthn options ---------------------------------------------------------

def _get_options(store, payload):
    """``PublicKeyCredentialRequestOptions`` as JSON, base64url byte fields."""
    return {"challenge": b64url_encode(challenge_bytes(payload)), "rpId": store.rp_id,
            "userVerification": "required", "timeout": CHALLENGE_TTL * 1000,
            "allowCredentials": [{"type": "public-key", "id": k["credential_id"]}
                                 for k in store.signing_keys()]}


def _create_options(store, payload):
    """``PublicKeyCredentialCreationOptions`` as JSON, base64url byte fields."""
    user_id = hashlib.sha256(("pseudolife-maintainer:" + store.rp_id).encode()).digest()[:16]
    return {"challenge": b64url_encode(challenge_bytes(payload)),
            "rp": {"id": store.rp_id, "name": "Pseudolife maintainer"},
            "user": {"id": b64url_encode(user_id), "name": "maintainer",
                     "displayName": "Pseudolife maintainer"},
            "pubKeyCredParams": [{"type": "public-key", "alg": alg} for alg in SUPPORTED_ALGS],
            "authenticatorSelection": {"userVerification": "required",
                                       "residentKey": "preferred"},
            "attestation": "none", "timeout": CHALLENGE_TTL * 1000,
            "excludeCredentials": [{"type": "public-key", "id": k["credential_id"]}
                                   for k in store.passkeys() if k["state"] != "revoked"]}


# -- field checks ---------------------------------------------------------------

def _send_text(text):
    try:
        ok = (isinstance(text, str) and bool(text.strip()) and "\x00" not in text
              and len(text.encode("utf-8")) <= MAX_TEXT_BYTES)
    except UnicodeEncodeError:
        ok = False
    if not ok or looks_like_secret(text):
        raise MaintainerError("invalid_request", check="text")
    return text


def _project(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 108:
        raise MaintainerError("invalid_request", check="project")
    return value


def _hold(value):
    if type(value) is not int or not DELEGATION_MIN <= value <= DELEGATION_MAX:
        raise MaintainerError("invalid_request", check="hold")
    return value


def _role_recipient(store, fields):
    """A role's recipient: a session's own registered address on exactly the
    payload's project (a role on another project's session is never read)."""
    row = store.recipient_row(fields.get("agent_id"))
    if row["project"] != fields.get("project"):
        raise MaintainerError("invalid_request", check="project_mismatch")
    return row


def _limit(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return LIST_DEFAULT
    return max(1, min(value, LIST_MAX))


# -- handlers -----------------------------------------------------------------

def _status(service, board, store):
    keys = store.passkeys()
    available = bool(store.signing_keys())
    out = {"available": available}
    if not available:
        out["reason"] = "maintainer_not_enrolled"
    out.update(rp_id=store.rp_id, origin=store.origin, passkeys=keys, roles=store.roles(),
               key_changes=store.key_changes())
    return out


def _role_preview(store, purpose, fields):
    role = "delegate" if purpose.endswith("delegate") else "coordinator"
    prefix = DELEGATE_PREFIX if role == "delegate" else COORDINATOR_PREFIX
    holder = store.role_holder(prefix + fields["project"])
    preview = {"role": role, "project": fields["project"],
               "action": "revoke" if purpose.startswith("revoke") else (
                   "grant" if role == "delegate" else "assign"),
               "current_holder": holder}
    if "agent_id" in fields:
        preview.update(store.recipient(fields["agent_id"]))
        preview["replaces"] = holder if holder != fields["agent_id"] else None
        other = COORDINATOR_PREFIX if role == "delegate" else DELEGATE_PREFIX
        preview["also_breaks"] = (other + fields["project"]
                                  if store.role_holder(other + fields["project"])
                                  == fields["agent_id"] else None)
    return preview


def _challenge(service, board, store, body):
    purpose = body.get("purpose")
    if purpose not in CHALLENGE_PURPOSES:
        raise MaintainerError("invalid_request", check="purpose")
    if set(body) - {"purpose"} - _CHALLENGE_FIELDS[purpose]:
        raise MaintainerError("invalid_request", check="parameters")
    if purpose == "enrol-bootstrap":
        if store.live_keys():
            raise MaintainerError("enrolment_closed")
        payload, mac = store.issue(purpose, {"label": check_label(body.get("label"))})
        return {"payload": payload, "mac": mac, "publicKey": _create_options(store, payload)}
    if not store.signing_keys():
        raise MaintainerError("maintainer_not_enrolled")
    preview = None
    if purpose == "send":
        urgent = body.get("urgent", False)
        if not isinstance(urgent, bool):
            raise MaintainerError("invalid_request", check="urgent")
        preview = store.recipient(body.get("to"))
        fields = {"to": body["to"], "text": _send_text(body.get("text")), "urgent": urgent}
    elif purpose == "enrol-approve":
        fields = {"label": check_label(body.get("label"))}
    elif purpose in ("cancel", "revoke-self"):
        cid = body.get("credential_id")
        if not isinstance(cid, str) or not cid or len(cid) > 1400:
            raise MaintainerError("invalid_request", check="credential_id")
        fields = {"credential_id": cid}
    elif purpose == "repudiate":
        mid = body.get("message_id")
        if not store.is_sent(mid):
            raise MaintainerError("message_not_found")
        fields = {"message_id": mid}
    elif purpose in ("grant-delegate", "assign-coordinator"):
        fields = {"project": _project(body.get("project")), "agent_id": body.get("agent_id"),
                  "hold": _hold(body.get("hold"))}
        _role_recipient(store, fields)
        preview = _role_preview(store, purpose, fields)
    else:  # revoke-delegate, revoke-coordinator
        fields = {"project": _project(body.get("project"))}
        preview = _role_preview(store, purpose, fields)
    if purpose in ROLE_PURPOSES:
        # Who the change replaces or evicts decides its outcome, so it is
        # signed too (review, 2026-10-04): a session that swaps in during
        # the window is not the one the maintainer approved removing.
        fields["holder"] = store.role_holder(_ROLE_PREFIX[purpose] + fields["project"])
    if purpose in ("grant-delegate", "assign-coordinator"):
        # So is the other role's holder, which the change breaks when it is
        # the recipient: a preview that said ``also_breaks: null`` must not
        # break a role the recipient took since (review of #569, 2026-10-05).
        fields["other_holder"] = store.role_holder(_OTHER_PREFIX[purpose] + fields["project"])
    payload, mac = store.issue(purpose, fields)
    out = {"payload": payload, "mac": mac, "publicKey": _get_options(store, payload)}
    if preview is not None:
        out["preview"] = preview
    return out


def _send(service, board, store, body):
    def attempt():
        # Registered (its own transaction) before the signed one opens, so
        # no audit-chain lock is held while send locks agent rows (review,
        # 2026-10-04).
        _identity(service, board)

        def action(fields, key):
            to, urgent = fields.get("to"), fields.get("urgent")
            if not isinstance(to, str) or not isinstance(urgent, bool):
                raise MaintainerError("assertion_invalid", check="payload_fields")
            store.recipient_row(to)
            return _board_send(service, board, to=to, text=fields.get("text"), urgent=urgent,
                               request_id=fields["nonce"], proof=key["_proof"])
        return store.complete(body.get("payload"), body.get("mac"), "send",
                              body.get("assertion"), action)
    result = _with_identity(service, attempt)
    return {"message_id": result["message_id"], "wake": result.get("wake"),
            "_notify": [result["recipient_agent_id"]]}


def _role(service, board, store, body):
    def action(fields, key):
        purpose = fields["purpose"]
        project = _project(fields.get("project"))
        try:
            # Both role leases, in name order (lease rows before any agent
            # row), then the holder the maintainer signed for.
            board._lock_role_leases(project)
            if store.role_holder(_ROLE_PREFIX[purpose] + project) != fields.get("holder"):
                raise MaintainerError("role_changed", check="holder")
            if purpose in ("grant-delegate", "assign-coordinator"):
                if (store.role_holder(_OTHER_PREFIX[purpose] + project)
                        != fields.get("other_holder")):
                    raise MaintainerError("role_changed", check="other_holder")
                _role_recipient(store, fields)
                change = (board.grant_delegate if purpose == "grant-delegate"
                          else board.assign_coordinator)
                return change(_project(project), fields["agent_id"], hold=_hold(fields.get("hold")),
                              actor="maintainer")
            prefix = DELEGATE_PREFIX if purpose == "revoke-delegate" else COORDINATOR_PREFIX
            return board.break_lease(prefix + _project(project), actor="maintainer")
        except CoordinationError as exc:
            raise _board_error(exc) from None
    return store.complete(body.get("payload"), body.get("mac"), ROLE_PURPOSES,
                          body.get("assertion"), action)


def _repudiate(service, board, store, body):
    def attempt():
        sender = _identity(service, board)[0]

        def action(fields, key):
            row = store.mark_repudiated(fields.get("message_id"), sender_agent_id=sender)
            follow_up = None
            if row["acknowledged_at"] is not None:
                # Already acknowledged: tell the recipient in a maintainer
                # message of its own, proven by the repudiation's signature.
                follow_up = _board_send(
                    service, board, to=row["recipient_agent_id"],
                    text=WITHDRAWAL_NOTICE.format(message_id=row["message_id"]),
                    urgent=False, request_id=fields["nonce"], proof=key["_proof"])
            if row["withdrawn_now"]:
                # Last, so the audit-chain lock is not held while the
                # follow-up's send locks rows.
                store.log_repudiation(row)
            return {"message_id": row["message_id"], "repudiated_at": row["repudiated_at"],
                    "follow_up": follow_up and follow_up["message_id"],
                    "_notify": [row["recipient_agent_id"]] if follow_up else []}
        return store.complete(body.get("payload"), body.get("mac"), "repudiate",
                              body.get("assertion"), action)
    return _with_identity(service, attempt)


def _enrol(service, board, store, body):
    if "assertion" in body:
        # Another passkey, step 1: an active key signs the approval; the
        # answer is the ``enrol`` payload and the creation options.
        approved = store.approve_enrol(body.get("payload"), body.get("mac"),
                                       body.get("assertion"))
        approved["publicKey"] = _create_options(store, approved["payload"])
        return approved
    if "code" in body:
        out = store.enrol_bootstrap(body.get("payload"), body.get("mac"),
                                    body.get("attestation"), body.get("code"))
        notice = (f"key {key_prefix(out['credential_id'])} was enrolled with a one-time code from "
                  "the daemon host and waits for the host confirm")
    else:
        out = store.enrol_approved(body.get("payload"), body.get("mac"),
                                   body.get("attestation"))
        notice = (f"key {key_prefix(out['credential_id'])} was added; it cannot sign for 24 hours "
                  "and an older key can cancel it meanwhile")
    return {"credential_id": out["credential_id"], "label": out["label"], "state": out["state"],
            "_notice": notice}


def _cancel(service, board, store, body):
    out = store.cancel(body.get("payload"), body.get("mac"), body.get("assertion"))
    return {"state": out["state"],
            "_notice": f"key {key_prefix(out['credential_id'])} was cancelled by an older key"}


def _revoke(service, board, store, body):
    out = store.revoke_self(body.get("payload"), body.get("mac"), body.get("assertion"))
    return {"state": out["state"],
            "_notice": f"key {key_prefix(out['credential_id'])} revoked itself"}


class MaintainerOps:
    """The service methods behind ``/api/maintainer*``, one per route (the
    addendum's route contract; ``web/fixtures.py`` implements the same
    names for the Console's fixture mode)."""

    def maintainer_status(self) -> dict:
        return _run(self, _status)

    def maintainer_challenge(self, body) -> dict:
        return _run(self, _challenge, body, body=True)

    def maintainer_enrol(self, body) -> dict:
        return _run(self, _enrol, body, body=True)

    def maintainer_send(self, body) -> dict:
        # Board mail is stamped with the reseeded HLC: the full tier.
        return _run(self, _send, body, body=True, full=True)

    def maintainer_role(self, body) -> dict:
        return _run(self, _role, body, body=True)

    def maintainer_cancel(self, body) -> dict:
        return _run(self, _cancel, body, body=True)

    def maintainer_revoke(self, body) -> dict:
        return _run(self, _revoke, body, body=True)

    def maintainer_repudiate(self, body) -> dict:
        return _run(self, _repudiate, body, body=True, full=True)

    def maintainer_sent(self, limit=LIST_DEFAULT) -> dict:
        return _run(self, lambda service, board, store: {"messages": store.sent(_limit(limit))})

    def maintainer_inbox(self, limit=LIST_DEFAULT) -> dict:
        return _run(self, lambda service, board, store: {"messages": store.inbox(_limit(limit))})
