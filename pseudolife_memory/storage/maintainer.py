"""The maintainer's passkeys, challenges and signed actions (schema v54).

Spec: docs/superpowers/specs/2026-10-02-maintainer-wake-design.md and its
addendum 2026-10-04-board-roles-passkey.md.

Challenges are stateless: ``issue`` returns a canonical JSON payload
(purpose, nonce, expiry and every input that decides the outcome) and
``HMAC-SHA256(secret, payload)``; the WebAuthn challenge is
``sha256(payload)``. Nothing is stored for an unspent challenge, so a bearer
has nothing to fill. A completing call checks, in order: the MAC (constant
time), the purpose, the expiry, the assertion (``maintainer_webauthn`` plus
the credential's state and sign count), and finally spends the nonce, an
INSERT that fails on a duplicate, in the same transaction as the action.

Mutation paths: ``issue`` (creates the bank's secret once), the enrolment
flows, cancel and revoke-self (signed), the host's confirm, revoke and reset
(``pseudolife-mcp maintainer``), the signed actions run by ``complete``
(board mail, repudiation, role changes), sign-count updates and the
regression flag. No derived state.

Every change to the key set (bootstrap enrol, host confirm, an added key,
cancel, revoke from the Console or the host, reset) appends a
``maintainer_key`` event to the board's audit log in the change's own
transaction, naming the path and the key that signed it: anyone with a
shell on the daemon host or the database password can reset and enrol their
own key, which the passkey cannot stop, so a key change is at least loud.
The Console reads them back as ``key_changes`` (``GET /api/maintainer``).

The secret is a meta row (``maintainer_secret_v1``), never config; ``reset``
rotates it, so every payload issued before is dead.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
import unicodedata

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pseudolife_memory.maintainer_webauthn import (
    SUPPORTED_ALGS, AssertionInvalid, b64url_decode, b64url_encode, verify_assertion,
    verify_registration,
)
from pseudolife_memory.storage.coordination import (
    ATTACHED_IDLE_WINDOW, COORDINATOR_PREFIX, DELEGATE_PREFIX, MAINTAINER_ORIGIN,
    MAINTAINER_PRINCIPAL, MESSAGE_ORIGIN, RESERVED_SENDERS, CoordinationStore,
)

logger = logging.getLogger("pseudolife-mcp.maintainer")

SECRET_META_KEY = "maintainer_secret_v1"
# How long a challenge payload stays good: one tap, with room to find the
# key. The spec's value (2026-10-02), not a measurement.
CHALLENGE_TTL = 120
# A key added by another key cannot sign for this long, and any older key
# can cancel it meanwhile (spec "Enrolment" 2). The spec's value.
QUARANTINE_SECONDS = 24 * 3600
# How long a spent nonce is kept past its payload's expiry. Pruned one TTL
# past it, a spend after the clock ran ahead, then a wall-clock step back
# (NTP correction, VM restore) reopened a spent payload for a replay
# (security review, 2026-10-04). A week covers any plausible step; the rows
# are one per signed action.
SPENT_NONCE_RETENTION = 7 * 24 * 3600
# The host's one-time bootstrap code: 10 base32 characters, 10 minutes.
BOOTSTRAP_TTL = 600
BOOTSTRAP_CODE_LENGTH = 10
# Wrong-code redemptions that burn the live code; the host must then issue a
# new one. A wrong code rolls its redemption back, so before this one
# payload could carry unlimited guesses (security review, 2026-10-04). Five
# leaves room for typos; a guess has 1 in 32**10 odds.
BOOTSTRAP_MAX_FAILURES = 5
_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
MAX_PASSKEY_LABEL = 64
# The payload a completing route accepts: the daemon wrote it, so anything
# far larger than a full send payload is not one of ours.
MAX_PAYLOAD_CHARS = 16384
ROLE_PURPOSES = ("grant-delegate", "revoke-delegate", "assign-coordinator",
                 "revoke-coordinator")
CHALLENGE_PURPOSES = ("send", "enrol-approve", "enrol-bootstrap", "cancel", "revoke-self",
                      "repudiate", *ROLE_PURPOSES)
_FULL_AGENT_ID = re.compile(r"[0-9a-f]{32}")
# The audit event a key-set change appends, and how many the Console's
# status reads back (newest first): a design value, enough for weeks of
# ordinary changes.
KEY_CHANGE_EVENT = "maintainer_key"
KEY_CHANGE_LIMIT = 30
KEY_CHANGE_FIELDS = ("change", "credential_id", "label", "by", "path", "revoked")


class MaintainerError(ValueError):
    """A public error code and its HTTP status; ``check`` (the rule that
    failed) goes to the daemon log only, never to the caller. A
    ``ValueError`` so a generic route still answers 400 with the code.
    ``public`` is the one exception, fields set deliberately for the
    answer: which config rule ``maintainer_https_required`` broke (a
    config rule is no verification secret; ``pseudolife-mcp doctor`` reads
    it to tell "off" from "configured wrongly")."""

    STATUS = {
        "maintainer_https_required": 409, "maintainer_not_enrolled": 409,
        "enrolment_closed": 409, "challenge_expired": 410, "challenge_spent": 410,
        "assertion_invalid": 403, "bootstrap_code_invalid": 403,
        "recipient_unknown": 404, "recipient_reserved": 400, "rate_capped": 429,
        "config_protected": 400, "invalid_request": 400, "message_not_found": 404,
        "credential_not_found": 404, "authentication_required": 401, "unauthorized": 401,
        "role_changed": 409,
        "principal_not_allowed": 403, "principals_unavailable": 503,
        "coordination_unavailable": 503, "not_found": 404,
    }

    def __init__(self, code: str, *, check: str | None = None, flag: str | None = None,
                 public: dict | None = None):
        self.code = code
        self.check = check
        self.flag = flag
        self.public = public
        super().__init__(code)

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 400)


def canonical(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def challenge_bytes(payload: str) -> bytes:
    """The WebAuthn challenge for ``payload``: its SHA-256."""
    return hashlib.sha256(payload.encode("utf-8")).digest()


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def normalize_code(code) -> str:
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code.upper() if ch not in " -")


def code_hash(code) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii", "ignore")).hexdigest()


def check_label(label) -> str:
    """A passkey label: shown to agents beside a verified message, so it is
    short, one line and printable."""
    if not isinstance(label, str):
        raise MaintainerError("invalid_request", check="label")
    label = unicodedata.normalize("NFC", label).strip()
    if (not 1 <= len(label) <= MAX_PASSKEY_LABEL
            or any(unicodedata.category(ch)[0] in "CZ" and ch != " " for ch in label)):
        raise MaintainerError("invalid_request", check="label")
    return label


class MaintainerStore:
    """Runs on the board's mailbox connection, under the coordination lock."""

    def __init__(self, storage, *, rp_id: str, origin: str, clock=time.time, principal=""):
        self.storage = storage
        self.rp_id = rp_id
        self.origin = origin
        self.clock = clock
        # The bearer principal of a Console request, for the audit record;
        # empty on the host.
        self.principal = principal

    # -- plumbing ----------------------------------------------------------

    def _one(self, sql, params=()):
        with self.storage.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql, params=()):
        with self.storage.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _secret(self) -> bytes:
        self.storage.conn.execute(
            "INSERT INTO meta (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING",
            (SECRET_META_KEY, Jsonb(secrets.token_hex(32))))
        row = self._one("SELECT value FROM meta WHERE key=%s", (SECRET_META_KEY,))
        value = row and row["value"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise MaintainerError("coordination_unavailable", check="secret")
        return bytes.fromhex(value)

    def _mac(self, payload: str) -> str:
        return hmac.new(self._secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    # -- key-change record ---------------------------------------------------

    def _key_change(self, change, *, credential_id=None, label=None, by, revoked=None):
        """Append one key-set change to the audit log, inside the change's
        transaction. ``by`` is the signing key's credential id, ``bootstrap``
        (a host code redeemed in the Console) or ``host`` (the CLI)."""
        path = "host" if by == "host" else "console"
        actor = {"host": "operator", "bootstrap": "daemon"}.get(by, "maintainer")
        payload = {"change": change, "credential_id": credential_id, "label": label,
                   "by": by, "path": path, "revoked": revoked}
        board = CoordinationStore(self.storage, clock=self.clock)
        board._append([board._event(KEY_CHANGE_EVENT, payload, actor=actor,
                                     principal="" if path == "host" else self.principal)],
                      self.clock())

    def key_changes(self, limit: int = KEY_CHANGE_LIMIT) -> list[dict]:
        """The recent key-set changes, newest first, as the audit log keeps
        them (its retention window bounds how far back they reach)."""
        out = []
        for row in self._all("SELECT created_at,principal,payload FROM coordination_events "
                             "WHERE event=%s ORDER BY seq DESC LIMIT %s",
                             (KEY_CHANGE_EVENT, limit)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            out.append({"at": row["created_at"], "principal": row["principal"] or None,
                        **{key: payload.get(key) for key in KEY_CHANGE_FIELDS}})
        return out

    # -- challenges --------------------------------------------------------

    def issue(self, purpose: str, fields: dict) -> tuple[str, str]:
        """A payload for ``purpose`` carrying ``fields``, and its MAC."""
        now = self.clock()
        payload = canonical({**fields, "purpose": purpose, "nonce": secrets.token_hex(16),
                             "expires_at": round(now + CHALLENGE_TTL, 3)})
        return payload, self._mac(payload)

    def open(self, payload, mac, purpose) -> dict:
        """Steps 1-3: the MAC in constant time, the purpose (one, or a tuple
        of the purposes a route completes), the expiry."""
        allowed = (purpose,) if isinstance(purpose, str) else tuple(purpose)
        if (not isinstance(payload, str) or not isinstance(mac, str)
                or len(payload) > MAX_PAYLOAD_CHARS or not re.fullmatch(r"[0-9a-f]{64}", mac)):
            raise MaintainerError("assertion_invalid", check="mac_shape")
        if not hmac.compare_digest(self._mac(payload), mac):
            raise MaintainerError("assertion_invalid", check="mac")
        try:
            fields = json.loads(payload, object_pairs_hook=_no_duplicates)
        except ValueError:
            raise MaintainerError("assertion_invalid", check="payload") from None
        if not isinstance(fields, dict) or fields.get("purpose") not in allowed:
            raise MaintainerError("assertion_invalid", check="purpose")
        expires = fields.get("expires_at")
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or self.clock() > expires):
            raise MaintainerError("challenge_expired")
        if not isinstance(fields.get("nonce"), str):
            raise MaintainerError("assertion_invalid", check="nonce")
        return fields

    def _spend(self, fields: dict) -> None:
        """Step 5, in the action's transaction: a duplicate is spent. The
        expiry is checked again here, on the same clock reading as the
        insert, and spent rows are kept SPENT_NONCE_RETENTION past their
        expiry: a replay that passed ``open`` just before expiry and reached
        here just after, or one after the clock stepped back, must not find
        its old row pruned (review, 2026-10-04)."""
        now = self.clock()
        if now > fields["expires_at"]:
            raise MaintainerError("challenge_expired")
        self.storage.conn.execute("DELETE FROM maintainer_nonces WHERE expires_at<%s",
                                  (now - SPENT_NONCE_RETENTION,))
        inserted = self.storage.conn.execute(
            "INSERT INTO maintainer_nonces (nonce,expires_at) VALUES (%s,%s) "
            "ON CONFLICT (nonce) DO NOTHING", (fields["nonce"], fields["expires_at"])).rowcount
        if inserted != 1:
            raise MaintainerError("challenge_spent")

    # -- passkeys ----------------------------------------------------------

    @staticmethod
    def _public(row) -> dict:
        return {key: row[key] for key in (
            "credential_id", "label", "state", "enrolled_by", "active_from", "created_at",
            "last_used_at", "revoked_at", "revoked_by", "flagged_at")}

    def passkeys(self) -> list[dict]:
        return [self._public(r) for r in self._all(
            "SELECT * FROM maintainer_passkeys ORDER BY created_at, credential_id")]

    def signing_keys(self) -> list[dict]:
        """Active keys past their quarantine: the ones that can sign."""
        now = self.clock()
        return [r for r in self._all("SELECT * FROM maintainer_passkeys WHERE state='active' "
                                     "ORDER BY created_at, credential_id")
                if r["active_from"] is not None and now >= r["active_from"]]

    def live_keys(self) -> int:
        return self._one("SELECT count(*) AS n FROM maintainer_passkeys "
                         "WHERE state IN ('pending','active')")["n"]

    @staticmethod
    def _decode(response, field, check):
        if not isinstance(response, dict):
            raise AssertionInvalid(check)
        return b64url_decode(response.get(field), check)

    @staticmethod
    def _credential_id(credential) -> bytes:
        if not isinstance(credential, dict):
            raise AssertionInvalid("credential_shape")
        raw = b64url_decode(credential.get("id"), "credential_id")
        if "rawId" in credential and b64url_decode(credential["rawId"], "raw_id") != raw:
            raise AssertionInvalid("raw_id")
        return raw

    def _verify(self, payload: str, assertion) -> dict:
        """Step 4, in the action's transaction: the credential is active and
        past quarantine, the assertion verifies with its stored key and alg,
        and the sign count moves forward (compare-and-set in ``_touch``)."""
        try:
            raw_id = self._credential_id(assertion)
            response = assertion.get("response")
            client_data = self._decode(response, "clientDataJSON", "client_data")
            auth_data = self._decode(response, "authenticatorData", "auth_data")
            signature = self._decode(response, "signature", "signature")
        except AssertionInvalid as exc:
            raise MaintainerError("assertion_invalid", check=exc.check) from None
        credential_id = b64url_encode(raw_id)
        key = self._one("SELECT * FROM maintainer_passkeys WHERE credential_id=%s FOR UPDATE",
                        (credential_id,))
        now = self.clock()
        if key is None:
            raise MaintainerError("assertion_invalid", check="unknown_credential")
        if key["state"] != "active":
            raise MaintainerError("assertion_invalid", check=f"state_{key['state']}")
        if key["active_from"] is None or now < key["active_from"]:
            raise MaintainerError("assertion_invalid", check="quarantined")
        if key["alg"] not in SUPPORTED_ALGS:
            raise MaintainerError("assertion_invalid", check="stored_alg")
        try:
            parsed = verify_assertion(
                public_key=bytes(key["public_key"]), alg=key["alg"],
                client_data_json=client_data, authenticator_data=auth_data,
                signature=signature, challenge=challenge_bytes(payload),
                origin=self.origin, rp_id=self.rp_id)
        except AssertionInvalid as exc:
            raise MaintainerError("assertion_invalid", check=exc.check) from None
        stored = key["sign_count"]
        if (stored or parsed.sign_count) and parsed.sign_count <= stored:
            # A counter that did not move forward: a replayed assertion or
            # a cloned authenticator. Refused, and the key is flagged.
            raise MaintainerError("assertion_invalid", check="sign_count", flag=credential_id)
        key = dict(key)
        key["_new_count"] = parsed.sign_count
        key["_proof"] = {"credential_id": credential_id, "label": key["label"],
                         "authenticator_data": b64url_encode(auth_data),
                         "client_data_json": b64url_encode(client_data),
                         "signature": b64url_encode(signature), "payload": payload,
                         "signed_at": now}
        return key

    def _touch(self, key) -> None:
        updated = self.storage.conn.execute(
            "UPDATE maintainer_passkeys SET sign_count=%s,last_used_at=%s "
            "WHERE credential_id=%s AND sign_count=%s",
            (key["_new_count"], self.clock(), key["credential_id"], key["sign_count"])).rowcount
        if updated != 1:
            raise MaintainerError("assertion_invalid", check="sign_count_race")

    def complete(self, payload, mac, purpose, assertion, action):
        """The completing route's five checks, then ``action(fields, key)``
        in the same transaction: a refused action spends nothing. A
        sign-count regression flags the key for review in a transaction of
        its own, since the failing one rolls back."""
        try:
            fields = self.open(payload, mac, purpose)
            with self.storage._txn():
                key = self._verify(payload, assertion)
                self._spend(fields)
                self._touch(key)
                return action(fields, key)
        except MaintainerError as exc:
            if exc.check:
                logger.warning("maintainer %s refused: %s", purpose, exc.check)
            if exc.flag:
                with self.storage._txn():
                    self.storage.conn.execute(
                        "UPDATE maintainer_passkeys SET flagged_at=%s WHERE credential_id=%s",
                        (self.clock(), exc.flag))
            raise

    # -- registration --------------------------------------------------------

    def _register(self, payload: str, attestation):
        try:
            raw_id = self._credential_id(attestation)
            response = attestation.get("response")
            client_data = self._decode(response, "clientDataJSON", "client_data")
            attestation_object = self._decode(response, "attestationObject", "attestation")
            return verify_registration(
                raw_id=raw_id, client_data_json=client_data,
                attestation_object=attestation_object, challenge=challenge_bytes(payload),
                origin=self.origin, rp_id=self.rp_id)
        except AssertionInvalid as exc:
            logger.warning("maintainer enrol refused: %s", exc.check)
            raise MaintainerError("assertion_invalid", check=exc.check) from None

    def _insert_key(self, registration, *, label, enrolled_by, state, active_from):
        credential_id = b64url_encode(registration.credential_id)
        inserted = self.storage.conn.execute(
            "INSERT INTO maintainer_passkeys (credential_id,public_key,alg,sign_count,label,"
            "enrolled_by,state,active_from,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (credential_id) DO NOTHING",
            (credential_id, registration.public_key, registration.alg,
             registration.sign_count, label, enrolled_by, state, active_from,
             self.clock())).rowcount
        if inserted != 1:
            raise MaintainerError("assertion_invalid", check="credential_exists")
        return {"credential_id": credential_id, "label": label, "state": state,
                "active_from": active_from}

    def enrol_bootstrap(self, payload, mac, attestation, code) -> dict:
        """The first passkey: a host-issued code, zero pending or active keys
        (checked with the insert, in one transaction), and the new key stays
        ``pending`` until the host confirms it."""
        try:
            fields = self.open(payload, mac, "enrol-bootstrap")
            label = check_label(fields.get("label"))
            registration = self._register(payload, attestation)
        except MaintainerError as exc:
            if exc.check:
                logger.warning("maintainer enrol-bootstrap refused: %s", exc.check)
            raise
        try:
            return self._redeem(fields, label, registration, code)
        except MaintainerError as exc:
            if exc.code == "bootstrap_code_invalid":
                self._count_failure()
            raise

    def _count_failure(self) -> None:
        """One wrong guess against the live code, in a transaction of its
        own: the redemption it failed rolled back, nonce and all."""
        with self.storage._txn():
            self.storage.conn.execute(
                "UPDATE maintainer_bootstrap SET failed_attempts=failed_attempts+1 "
                "WHERE used_at IS NULL AND expires_at>=%s AND failed_attempts<%s",
                (self.clock(), BOOTSTRAP_MAX_FAILURES))

    def _redeem(self, fields, label, registration, code) -> dict:
        with self.storage._txn():
            self._spend(fields)
            now = self.clock()
            # The table lock before the code row, the order bootstrap_code
            # takes them in (review, 2026-10-04: the reverse could deadlock
            # a new enrol-code against a redemption).
            self.storage.conn.execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
            row = self._one("SELECT * FROM maintainer_bootstrap WHERE code_hash=%s FOR UPDATE",
                            (code_hash(code),))
            if (row is None or row["used_at"] is not None or now > row["expires_at"]
                    or row["failed_attempts"] >= BOOTSTRAP_MAX_FAILURES):
                raise MaintainerError("bootstrap_code_invalid")
            if self.live_keys():
                raise MaintainerError("enrolment_closed")
            out = self._insert_key(registration, label=label, enrolled_by="bootstrap",
                                   state="pending", active_from=None)
            self.storage.conn.execute(
                "UPDATE maintainer_bootstrap SET used_at=%s,credential_id=%s WHERE code_hash=%s",
                (now, out["credential_id"], row["code_hash"]))
            self._key_change("enrol", credential_id=out["credential_id"], label=label,
                             by="bootstrap")
        return out

    def approve_enrol(self, payload, mac, assertion) -> dict:
        """Step 1 of another passkey: an active key signs the label; the
        answer carries the ``enrol`` payload bound to that approval (the
        approving credential and the approval's nonce)."""
        def action(fields, key):
            label = check_label(fields.get("label"))
            return {"label": label, "approved_by": key["credential_id"],
                    "approval_nonce": fields["nonce"]}
        bound = self.complete(payload, mac, "enrol-approve", assertion, action)
        payload2, mac2 = self.issue("enrol", bound)
        return {"payload": payload2, "mac": mac2, "label": bound["label"]}

    def enrol_approved(self, payload, mac, attestation) -> dict:
        """Step 2: the new key, ``active`` but quarantined for 24 h, as
        long as the approving key still can sign."""
        try:
            fields = self.open(payload, mac, "enrol")
            label = check_label(fields.get("label"))
            registration = self._register(payload, attestation)
        except MaintainerError as exc:
            if exc.check:
                logger.warning("maintainer enrol refused: %s", exc.check)
            raise
        with self.storage._txn():
            self._spend(fields)
            approver = self._one("SELECT * FROM maintainer_passkeys WHERE credential_id=%s "
                                 "FOR UPDATE", (fields.get("approved_by"),))
            now = self.clock()
            if (approver is None or approver["state"] != "active"
                    or approver["active_from"] is None or now < approver["active_from"]):
                logger.warning("maintainer enrol refused: approver")
                raise MaintainerError("assertion_invalid", check="approver")
            out = self._insert_key(registration, label=label,
                                   enrolled_by=approver["credential_id"], state="active",
                                   active_from=now + QUARANTINE_SECONDS)
            self._key_change("add", credential_id=out["credential_id"], label=label,
                             by=approver["credential_id"])
            return out

    def cancel(self, payload, mac, assertion) -> dict:
        """An older active key cancels a newer key still in quarantine."""
        def action(fields, key):
            target = self._one("SELECT * FROM maintainer_passkeys WHERE credential_id=%s "
                               "FOR UPDATE", (fields.get("credential_id"),))
            now = self.clock()
            if target is None:
                raise MaintainerError("credential_not_found")
            if (target["state"] != "active" or target["active_from"] is None
                    or now >= target["active_from"]
                    or (key["created_at"], key["credential_id"])
                    >= (target["created_at"], target["credential_id"])):
                raise MaintainerError("assertion_invalid", check="cancel_target")
            self.storage.conn.execute(
                "UPDATE maintainer_passkeys SET state='revoked',revoked_at=%s,revoked_by=%s "
                "WHERE credential_id=%s", (now, key["credential_id"], target["credential_id"]))
            self._key_change("cancel", credential_id=target["credential_id"],
                             label=target["label"], by=key["credential_id"])
            return {"credential_id": target["credential_id"], "state": "revoked"}
        return self.complete(payload, mac, "cancel", assertion, action)

    def revoke_self(self, payload, mac, assertion) -> dict:
        """A key revokes itself; no key revokes another (older) one."""
        def action(fields, key):
            if fields.get("credential_id") != key["credential_id"]:
                raise MaintainerError("assertion_invalid", check="revoke_other")
            self.storage.conn.execute(
                "UPDATE maintainer_passkeys SET state='revoked',revoked_at=%s,revoked_by=%s "
                "WHERE credential_id=%s", (self.clock(), key["credential_id"],
                                           key["credential_id"]))
            self._key_change("revoke", credential_id=key["credential_id"], label=key["label"],
                             by=key["credential_id"])
            return {"credential_id": key["credential_id"], "state": "revoked"}
        return self.complete(payload, mac, "revoke-self", assertion, action)

    # -- board reads ---------------------------------------------------------

    def recipient_row(self, agent_id) -> dict:
        """The board row a send or a role names: a full agent id, registered
        and not revoked, and a session's own address (never a reserved row
        or a subagent's)."""
        if not isinstance(agent_id, str) or not _FULL_AGENT_ID.fullmatch(agent_id):
            raise MaintainerError("recipient_unknown")
        row = self._one("SELECT * FROM coordination_agents WHERE agent_id=%s "
                        "AND credential_hash IS NOT NULL", (agent_id,))
        if row is None:
            raise MaintainerError("recipient_unknown")
        if row["principal"] in RESERVED_SENDERS or row.get("parent_thread") is not None:
            raise MaintainerError("recipient_reserved")
        return row

    def recipient(self, agent_id) -> dict:
        """The preview a challenge carries, so the maintainer can tell
        sessions apart before the prompt. The board has no separate session
        name on this schema: the label is the name shown. ``host`` is
        reported when the row's capabilities carry one as text (no client
        registers it yet)."""
        row = self.recipient_row(agent_id)
        name = row["label"]
        duplicate = bool(name) and self._one(
            "SELECT 1 AS hit FROM coordination_agents WHERE agent_id<>%s "
            "AND credential_hash IS NOT NULL AND last_activity>%s AND label=%s LIMIT 1",
            (agent_id, self.clock() - ATTACHED_IDLE_WINDOW, name)) is not None
        capabilities = row.get("capabilities") or {}
        host = capabilities.get("host") if isinstance(capabilities.get("host"), str) else None
        return {"agent_id_prefix": agent_id[:12], "name": name, "label": row["label"],
                "principal": row["principal"], "host": host,
                "client": "codex" if "codex" in capabilities else "claude",
                "project": row["project"], "task": row["task"],
                "last_activity": row["last_activity"], "duplicate_name": duplicate}

    def sent(self, limit: int = 50) -> list[dict]:
        """Every maintainer message still on the board, newest first, with
        its wake receipt."""
        return [{
            "message_id": r["message_id"], "recipient_agent_id": r["recipient_agent_id"],
            "recipient_label": r["recipient_label"], "created_at": r["created_at"],
            "label": (r["maintainer_proof"] or {}).get("label"), "text": r["text"],
            "wake": r["wake"], "first_read_at": r["first_read_at"],
            "acknowledged_at": r["acknowledged_at"], "repudiated_at": r["repudiated_at"],
        } for r in self._all(
            "SELECT m.*,a.label AS recipient_label FROM coordination_messages m "
            "LEFT JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id "
            "WHERE m.origin=%s AND m.sender_principal=%s "
            "ORDER BY m.created_at DESC, m.message_id LIMIT %s",
            (MAINTAINER_ORIGIN, MAINTAINER_PRINCIPAL, limit))]

    def is_sent(self, message_id) -> bool:
        return isinstance(message_id, str) and self._one(
            "SELECT 1 AS hit FROM coordination_messages WHERE message_id=%s AND origin=%s "
            "AND sender_principal=%s", (message_id, MAINTAINER_ORIGIN,
                                        MAINTAINER_PRINCIPAL)) is not None

    def inbox(self, limit: int = 50) -> list[dict]:
        """Replies to the maintainer: agent mail, newest first."""
        return [{
            "message_id": r["message_id"], "sender_agent_id": r["sender_agent_id"],
            "sender_label": r["sender_label"], "sender_principal": r["sender_principal"],
            "reply_to": r["reply_to"], "text": r["text"], "created_at": r["created_at"],
            "acknowledged_at": r["acknowledged_at"], "origin": MESSAGE_ORIGIN,
        } for r in self._all(
            "SELECT m.*,s.label AS sender_label FROM coordination_messages m "
            "JOIN coordination_agents r ON r.agent_id=m.recipient_agent_id "
            "LEFT JOIN coordination_agents s ON s.agent_id=m.sender_agent_id "
            "WHERE r.principal=%s ORDER BY m.created_at DESC, m.message_id LIMIT %s",
            (MAINTAINER_PRINCIPAL, limit))]

    def mark_repudiated(self, message_id, *, sender_agent_id=None) -> dict:
        """Inside a completing transaction: withdraw one maintainer message.
        Its mailbox rows (the recipient's, and ``sender_agent_id``, the
        maintainer address a follow-up is sent from) are locked first, in
        agent-id order as ``send`` locks them, then the message row: the
        order ack and receive take (review, 2026-10-04)."""
        row = self._one("SELECT recipient_agent_id FROM coordination_messages "
                        "WHERE message_id=%s AND origin=%s AND sender_principal=%s",
                        (message_id, MAINTAINER_ORIGIN, MAINTAINER_PRINCIPAL))
        if row is None:
            raise MaintainerError("message_not_found")
        agents = sorted({row["recipient_agent_id"], *((sender_agent_id,) if sender_agent_id
                                                      else ())})
        self._all("SELECT agent_id FROM coordination_agents WHERE agent_id=ANY(%s) "
                  "ORDER BY agent_id FOR UPDATE", (agents,))
        row = self._one("SELECT * FROM coordination_messages WHERE message_id=%s FOR UPDATE",
                        (message_id,))
        if row["repudiated_at"] is None:
            row = self._one("UPDATE coordination_messages SET repudiated_at=%s "
                            "WHERE message_id=%s RETURNING *", (self.clock(), message_id))
        return row

    def roles(self) -> dict:
        """Every project with a live role lease: its delegate (with who
        granted it, read from the grant's audit record for the current
        fence; a hold without one grants nothing and is left out) and its
        coordinator."""
        now = self.clock()
        out: dict = {}
        for row in self._all(
                "SELECT l.name,l.holder_agent_id,l.expires_at,(SELECT e.actor FROM "
                "coordination_events e WHERE e.event='lease_delegate' "
                "AND e.agent_id=l.holder_agent_id AND CASE WHEN e.event='lease_delegate' "
                "THEN e.payload::jsonb->>'name'=l.name "
                "AND (e.payload::jsonb->>'fence')::bigint=l.fence ELSE false END "
                "ORDER BY e.seq DESC LIMIT 1) AS granted_by "
                "FROM coordination_leases l WHERE l.holder_agent_id IS NOT NULL "
                "AND l.expires_at>%s AND (l.name LIKE %s OR l.name LIKE %s) ORDER BY l.name",
                (now, DELEGATE_PREFIX + "%", COORDINATOR_PREFIX + "%")):
            if row["name"].startswith(DELEGATE_PREFIX):
                if row["granted_by"] not in ("operator", "maintainer"):
                    continue
                project = row["name"][len(DELEGATE_PREFIX):]
                entry = {"agent_id": row["holder_agent_id"], "expires_at": row["expires_at"],
                         "granted_by": row["granted_by"]}
                slot = "delegate"
            else:
                project = row["name"][len(COORDINATOR_PREFIX):]
                entry = {"agent_id": row["holder_agent_id"], "expires_at": row["expires_at"]}
                slot = "coordinator"
            out.setdefault(project, {"delegate": None, "coordinator": None})[slot] = entry
        return out

    def role_holder(self, name) -> str | None:
        row = self._one("SELECT holder_agent_id FROM coordination_leases WHERE name=%s "
                        "AND holder_agent_id IS NOT NULL AND expires_at>%s",
                        (name, self.clock()))
        return row["holder_agent_id"] if row else None

    # -- host-side operations (pseudolife-mcp maintainer) --------------------

    def bootstrap_code(self) -> str:
        """A one-time code for the first passkey, refused while any key is
        pending or active. Stored hashed; earlier unused codes are dropped."""
        code = "".join(secrets.choice(_BASE32) for _ in range(BOOTSTRAP_CODE_LENGTH))
        with self.storage._txn():
            self.storage.conn.execute("LOCK TABLE maintainer_passkeys IN SHARE ROW EXCLUSIVE MODE")
            if self.live_keys():
                raise MaintainerError("enrolment_closed")
            self.storage.conn.execute("DELETE FROM maintainer_bootstrap WHERE used_at IS NULL")
            self.storage.conn.execute(
                "INSERT INTO maintainer_bootstrap (code_hash,expires_at) VALUES (%s,%s)",
                (code_hash(code), self.clock() + BOOTSTRAP_TTL))
        return code

    def bootstrap_redeemed(self, code) -> dict | None:
        """The key a code admitted, once the Console redeemed it."""
        row = self._one("SELECT b.credential_id,p.label,p.state FROM maintainer_bootstrap b "
                        "JOIN maintainer_passkeys p ON p.credential_id=b.credential_id "
                        "WHERE b.code_hash=%s", (code_hash(code),))
        return row and {"credential_id": row["credential_id"], "label": row["label"],
                        "state": row["state"]}

    def bootstrap_burned(self, code) -> bool:
        """Whether wrong guesses burned this code before it was redeemed."""
        row = self._one("SELECT failed_attempts FROM maintainer_bootstrap WHERE code_hash=%s "
                        "AND used_at IS NULL", (code_hash(code),))
        return row is not None and row["failed_attempts"] >= BOOTSTRAP_MAX_FAILURES

    def _by_prefix(self, prefix, *, state=None):
        if (not isinstance(prefix, str) or len(prefix) < 6
                or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix)):
            raise MaintainerError("invalid_request", check="prefix")
        # ``_`` is a base64url character and a LIKE wildcard: escaped.
        rows = self._all("SELECT * FROM maintainer_passkeys WHERE credential_id LIKE %s"
                         + (" AND state=%s" if state else "") + " FOR UPDATE",
                         (prefix.replace("_", "\\_") + "%", *((state,) if state else ())))
        if len(rows) != 1:
            raise MaintainerError("credential_not_found")
        return rows[0]

    def confirm(self, prefix) -> dict:
        """Host: make a pending (bootstrap) key active."""
        with self.storage._txn():
            row = self._by_prefix(prefix, state="pending")
            self.storage.conn.execute(
                "UPDATE maintainer_passkeys SET state='active',active_from=%s "
                "WHERE credential_id=%s", (self.clock(), row["credential_id"]))
            self._key_change("confirm", credential_id=row["credential_id"], label=row["label"],
                             by="host")
        return {"credential_id": row["credential_id"], "label": row["label"], "state": "active"}

    def revoke(self, prefix) -> dict:
        """Host: revoke any key, however old."""
        with self.storage._txn():
            row = self._by_prefix(prefix)
            if self.storage.conn.execute(
                    "UPDATE maintainer_passkeys SET state='revoked',revoked_at=%s,"
                    "revoked_by='host' WHERE credential_id=%s AND state<>'revoked'",
                    (self.clock(), row["credential_id"])).rowcount:
                self._key_change("revoke", credential_id=row["credential_id"],
                                 label=row["label"], by="host")
        return {"credential_id": row["credential_id"], "label": row["label"], "state": "revoked"}

    def reset(self) -> dict:
        """Host: revoke every key, rotate the secret, reopen bootstrap."""
        with self.storage._txn():
            revoked = self.storage.conn.execute(
                "UPDATE maintainer_passkeys SET state='revoked',revoked_at=%s,revoked_by='host' "
                "WHERE state<>'revoked'", (self.clock(),)).rowcount
            self.storage.conn.execute("DELETE FROM maintainer_bootstrap")
            self.storage.conn.execute("DELETE FROM meta WHERE key=%s", (SECRET_META_KEY,))
            self._secret()
            self._key_change("reset", by="host", revoked=revoked)
        return {"revoked": revoked}
