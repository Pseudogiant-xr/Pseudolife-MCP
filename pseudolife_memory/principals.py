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

Pure module: no MCP SDK import, no storage.
"""
from __future__ import annotations

import hmac
import logging

logger = logging.getLogger("pseudolife-mcp.principals")

#: Reserved principal for singular-token and open (no-token) installs. The
#: default principal keeps the legacy writer path (X-PL-Writer / env).
DEFAULT_PRINCIPAL = "default"


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
        token, sep, principal = part.partition(":")
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
        if token in out:
            logger.warning(
                "token-map entry #%d duplicates an earlier token — first "
                "entry wins (kept principal %r)", i + 1, out[token])
            continue
        out[token] = principal
    return out


def resolve_principal(auth_header: str | None,
                      token_map: dict[str, str],
                      single_token: str | None) -> str | None:
    """Principal for an ``Authorization`` header value, or ``None`` when the
    caller is unauthorized.

    * No auth configured at all (open loopback mode): everyone is
      :data:`DEFAULT_PRINCIPAL`.
    * Bearer matches a map entry: that entry's principal.
    * Bearer matches the singular token: :data:`DEFAULT_PRINCIPAL`.
    * Anything else (missing/wrong scheme/unknown token): ``None``.

    Comparisons are constant-time (``hmac.compare_digest``).
    """
    if not token_map and single_token is None:
        return DEFAULT_PRINCIPAL
    if not auth_header:
        return None
    scheme, _, presented = auth_header.partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented:
        return None
    for token, principal in token_map.items():
        if hmac.compare_digest(presented, token):
            return principal
    if single_token is not None and hmac.compare_digest(presented, single_token):
        return DEFAULT_PRINCIPAL
    return None
