"""Token -> principal identity map (spec 2026-08-10, identity re-keying).

A principal is the daemon-side name for "whoever holds this bearer token":
it carries writer identity and the default toolset tier, and is the only
identity signal that survives the MCP 2026-07-28 stateless core (custom
``X-PL-*`` headers are shim-internal; ``mcp-session-id`` is removed).

Configuration:

* ``PSEUDOLIFE_MCP_TOKENS`` — comma-separated ``token:principal`` entries.
  Malformed entries are logged (without the token value) and skipped; a
  skipped token never authenticates (fail closed, never a fall-through to
  the default identity).
* ``PSEUDOLIFE_MCP_TOKEN`` — the pre-existing singular token, mapping to
  the reserved principal ``"default"``. Fully supported alongside the map;
  the map is consulted first.

Principals can also be stored in the bank (``principal_store``, schema
v53): ``pseudolife-mcp invite`` creates one and ``pseudolife-mcp pair``
redeems its code. The daemon installs an in-memory snapshot of that table
with :func:`install_store`, and :func:`resolve_principal` consults it after
the environment, which always wins.

Pure module: no MCP SDK import, no storage. Standard library only, so the
client-side commands (``pair``, ``invite``'s host entry) import it from a
shim runtime.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import hmac
import logging
import os
import re

logger = logging.getLogger("pseudolife-mcp.principals")

#: Reserved principal for singular-token and open (no-token) installs. The
#: default principal keeps the legacy writer path (X-PL-Writer / env).
DEFAULT_PRINCIPAL = "default"

#: The board's own sender (``storage.coordination.DAEMON_PRINCIPAL``,
#: pinned equal by tests/test_stored_principals.py): never a client.
DAEMON_PRINCIPAL = "daemon"

#: The board's maintainer sender (``storage.coordination.MAINTAINER_PRINCIPAL``,
#: schema v54): its authority is a passkey signature in the Console, never a
#: bearer, so no token, invite or board admission may name it.
MAINTAINER_PRINCIPAL = "maintainer"

#: Names no stored principal may take: an invite refuses them, the snapshot
#: ignores a row that carries one, and pair never names a file after one.
RESERVED_PRINCIPALS = frozenset({DEFAULT_PRINCIPAL, DAEMON_PRINCIPAL, MAINTAINER_PRINCIPAL})

# A stored principal's name: it becomes a file name on the paired machine
# (``~/.pseudolife-mcp/<principal>.token``), so no separator, no leading dot.
_PRINCIPAL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# Pairing codes: 12 Crockford base32 characters (60 bits), shown as
# XXXX-XXXX-XXXX. The alphabet leaves out I, L, O and U.
PAIRING_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_LENGTH = 12
_CODE_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def valid_principal_name(name) -> bool:
    """True for a name a stored principal may carry:
    ``[a-z0-9][a-z0-9._-]{0,63}``. Reserved names are the caller's check."""
    return isinstance(name, str) and _PRINCIPAL_NAME.fullmatch(name) is not None


def normalize_pairing_code(text) -> str | None:
    """The 12-character canonical form of a typed pairing code, or ``None``
    when it is not one. Case, dashes and surrounding whitespace are ignored,
    and ``O`` reads as ``0``, ``I`` and ``L`` as ``1``."""
    if not isinstance(text, str):
        return None
    code = text.strip().upper().replace("-", "").translate(_CODE_LOOKALIKES)
    if len(code) != PAIRING_CODE_LENGTH or any(c not in PAIRING_CODE_ALPHABET for c in code):
        return None
    return code


def format_pairing_code(code: str) -> str:
    """``XXXX-XXXX-XXXX`` for a canonical code."""
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def secret_sha256(value: str) -> str:
    """Lowercase SHA-256 hex of a high-entropy secret (a bearer token or a
    pairing code). Unsalted on purpose, like the board's instance
    credentials: the inputs are random, so the hash is not guessable."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_sha256_hex(value) -> bool:
    return isinstance(value, str) and _SHA256_HEX.fullmatch(value) is not None


def parse_token_map(raw: str | None) -> dict[str, str]:
    """Parse ``PSEUDOLIFE_MCP_TOKENS`` (``"token:principal,token:principal"``).

    Malformed entries are logged and skipped — a config typo must never take
    the daemon down, and a skipped token simply does not authenticate.
    Duplicate tokens keep the first entry. Principals are lowercased (they
    share the writer-id namespace); tokens keep their case. The reserved
    principal ``"default"`` is refused: a mapped token must not impersonate
    the singular-token identity path.
    """
    out: dict[str, str] = {}
    for i, part in enumerate((raw or "").split(",")):
        part = part.strip()
        if not part:
            continue
        # rpartition: principals cannot contain ":", tokens may.
        token, sep, principal = part.rpartition(":")
        token = token.strip()
        principal = principal.strip().lower()
        if not sep or not token or not principal:
            logger.warning(
                "token-map entry #%d malformed (want token:principal) — "
                "skipped; that token will not authenticate", i + 1)
            continue
        if principal == DEFAULT_PRINCIPAL:
            logger.warning(
                "token-map entry #%d names the reserved principal %r — "
                "skipped; use PSEUDOLIFE_MCP_TOKEN for the default identity",
                i + 1, DEFAULT_PRINCIPAL)
            continue
        if principal == MAINTAINER_PRINCIPAL:
            # v54: maintainer messages are proven by a passkey, never a
            # token; a token naming the maintainer must not authenticate.
            logger.warning(
                "token-map entry #%d names the reserved principal %r — "
                "skipped; maintainer messages are proven by a passkey, not a token",
                i + 1, MAINTAINER_PRINCIPAL)
            continue
        if token in out:
            logger.warning(
                "token-map entry #%d duplicates an earlier token — first "
                "entry wins (kept principal %r)", i + 1, out[token])
            continue
        out[token] = principal
    return out


@lru_cache(maxsize=8)
def _env_token_map(raw: str) -> dict[str, str]:
    return parse_token_map(raw)


def env_auth(environ=None) -> tuple[dict[str, str], str | None]:
    """``(token_map, singular_token)`` from ``PSEUDOLIFE_MCP_TOKENS`` and
    ``PSEUDOLIFE_MCP_TOKEN``: the daemon's environment identities, for the
    sites that resolve a bearer without the gate's frozen copy. Parsed once
    per distinct value."""
    environ = os.environ if environ is None else environ
    return (dict(_env_token_map(environ.get("PSEUDOLIFE_MCP_TOKENS") or "")),
            environ.get("PSEUDOLIFE_MCP_TOKEN") or None)


def misconfigured_tokens_env(raw: str | None,
                             token_map: dict[str, str]) -> bool:
    """True when ``PSEUDOLIFE_MCP_TOKENS`` was SET but no entry survived
    parsing. Per-entry skipping is fail-closed, but an entirely-skipped map
    plus no singular token would silently degrade the daemon to OPEN mode —
    the caller must treat this state as a startup error, not "no auth
    configured" (review 2026-08-10, finding 2)."""
    return bool((raw or "").strip()) and not token_map


class PrincipalsUnavailable(Exception):
    """The stored-principal snapshot has never loaded, or is older than its
    staleness limit, and the bearer matched nothing in the environment: the
    caller may hold a valid stored token, so this is not "unauthorized".
    The HTTP gate answers ``503 {"error": "principals_unavailable"}``."""


# The daemon's stored-principal snapshot (principal_store.PrincipalSnapshot),
# installed once at startup; None everywhere else (clients, tests, a daemon
# without Postgres), where resolution is the environment's alone.
_STORE = None


def install_store(store) -> None:
    """Install (or, with ``None``, remove) the process's stored-principal
    snapshot. Called by the daemon at startup and by tests."""
    global _STORE
    _STORE = store


def installed_store():
    return _STORE


def principal_admitted(coordination_config, principal, *, store=None) -> bool:
    """Whether ``principal`` may use the agent board: listed in
    ``coordination.allowed_principals``, or a stored principal whose row has
    ``board = true`` and is not revoked. The board's own sender is never
    admitted, nor (v54) the maintainer's. ``store`` defaults to the
    installed snapshot; this reads memory only, never storage."""
    if not isinstance(principal, str) or principal in (DAEMON_PRINCIPAL, MAINTAINER_PRINCIPAL):
        return False
    if principal in coordination_config.allowed_principals:
        return True
    store = _STORE if store is None else store
    return store is not None and store.admitted(principal)


def is_stored_principal(principal) -> bool:
    """Whether ``principal`` names a principal stored in the bank (paired,
    pending or revoked), as opposed to an environment one. Rows the
    environment shadows are not stored principals."""
    store = _STORE
    return store is not None and isinstance(principal, str) and store.has(principal)


def stored_tier(principal) -> str | None:
    """The tier a stored principal's row sets, or ``None`` (no row, no tier,
    no store): the toolset falls back to it after PSEUDOLIFE_MCP_TIER_MAP."""
    store = _STORE
    if store is None or not isinstance(principal, str):
        return None
    return store.tier_of(principal)


#: Where :func:`resolve_principal_detailed` found the principal.
SOURCE_OPEN = "open"
SOURCE_ENVIRONMENT = "environment"
SOURCE_STORE = "store"


def resolve_principal(auth_header: str | None,
                      token_map: dict[str, str],
                      single_token: str | None,
                      store=None) -> str | None:
    """The principal :func:`resolve_principal_detailed` resolves, without
    its source."""
    return resolve_principal_detailed(auth_header, token_map, single_token, store)[0]


def resolve_principal_detailed(auth_header: str | None,
                               token_map: dict[str, str],
                               single_token: str | None,
                               store=None) -> tuple[str | None, str | None]:
    """``(principal, source)`` for an ``Authorization`` header value:
    ``(None, None)`` when the caller is unauthorized. ``source`` is
    :data:`SOURCE_OPEN`, :data:`SOURCE_ENVIRONMENT` or :data:`SOURCE_STORE`,
    so a gate can tell a stored principal by how it authenticated.

    * No auth configured at all (open loopback mode): everyone is
      :data:`DEFAULT_PRINCIPAL`. Stored principals do not turn
      authentication on.
    * Bearer matches a map entry: that entry's principal.
    * Bearer matches the singular token: :data:`DEFAULT_PRINCIPAL`.
    * Bearer's SHA-256 matches a row of ``store`` (the stored-principal
      snapshot; callers pass :func:`installed_store`): that row's principal.
      The environment is always consulted first. A store that is not
      available raises :class:`PrincipalsUnavailable` for a bearer the
      environment did not match; the lookup is a dict read, never I/O.
    * Anything else (missing/wrong scheme/unknown token): ``None``.

    Comparisons are constant-time (``hmac.compare_digest``).
    """
    if not token_map and single_token is None:
        return DEFAULT_PRINCIPAL, SOURCE_OPEN
    if not auth_header:
        return None, None
    scheme, _, presented = auth_header.partition(" ")
    # HTTP whitespace only: str.strip() would also drop NBSP/NEL, which is
    # how latin-1 text renders the last byte of UTF-8 "à" (C3 A0) or "ą" (C4 85).
    presented = presented.strip(" \t")
    if scheme.lower() != "bearer" or not presented:
        return None, None
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which
    # would 500 the gate instead of 401ing (review 2026-08-10, finding 1).
    # HTTP header text is usually latin-1-decoded (Starlette, the daemon's
    # header maps), so a UTF-8 token also arrives as its latin-1 text: its
    # latin-1 encoding is the bytes the client sent.
    candidates = [presented.encode("utf-8")]
    try:
        candidates.append(presented.encode("latin-1"))
    except UnicodeEncodeError:
        pass

    def matches(token: str) -> bool:
        token_b = token.encode("utf-8")
        return any([hmac.compare_digest(c, token_b) for c in candidates])

    for token, principal in token_map.items():
        if matches(token):
            return principal, SOURCE_ENVIRONMENT
    if single_token is not None and matches(single_token):
        return DEFAULT_PRINCIPAL, SOURCE_ENVIRONMENT
    if store is None:
        return None, None
    if not store.available():
        raise PrincipalsUnavailable()
    for candidate in dict.fromkeys(candidates):
        row = store.lookup(hashlib.sha256(candidate).hexdigest())
        if row is not None:
            return row.principal, SOURCE_STORE
    return None, None
