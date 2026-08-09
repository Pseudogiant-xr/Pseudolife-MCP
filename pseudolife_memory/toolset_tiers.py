"""Principal-scoped toolset tiers (specs: 2026-07-11, re-keyed 2026-08-10).

Visibility model: every tool registers with FastMCP; the transport's
tools/list handler (mcp_server._wire_transport_tiering) filters by the
PRINCIPAL's resolved tier — the named principal from the bearer token, or
the writer id for single-token installs (principals share the writer-id
namespace). The MCP 2026-07-28 tools spec forbids per-connection variance
and sanctions exactly this per-authorization axis. Ordering: minimal ⊂
core ⊂ full. Resolution: principal override (memory_toolset) → tier map
(PSEUDOLIFE_MCP_TIER_MAP, principal:tier) → env default
(PSEUDOLIFE_MCP_TOOLSET). Visibility is a token lever, not a security
boundary — the server does not reject hidden-tool calls, but Claude
clients gate calls against their own tools/list, so in practice a session
expands its tier first; auth is the bearer token.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("pseudolife-mcp.tiers")

TIERS: tuple[str, ...] = ("minimal", "core", "full")
_RANK = {t: i for i, t in enumerate(TIERS)}

# Overrides are working-day-scoped; 12h comfortably outlives a stretch of
# sessions and lets abandoned entries lapse without a reaper thread.
SESSION_TTL_S = 12 * 3600.0


def rank(tier: str) -> int:
    return _RANK[tier]


def normalize_tier(value: str | None, *, warn_context: str = "") -> str:
    """Lenient tier parse: unset -> full (the historical default); unknown
    values warn and fall back to full rather than hiding tools by surprise."""
    v = (value or "").strip().lower()
    if v in _RANK:
        return v
    if v:
        ctx = f" ({warn_context})" if warn_context else ""
        logger.warning("unknown toolset tier %r%s — falling back to 'full'", value, ctx)
    return "full"


def parse_tier_map(raw: str | None) -> dict[str, str]:
    """Parse PSEUDOLIFE_MCP_TIER_MAP ("writer:tier,writer:tier"). Malformed
    entries are logged and skipped — a config typo must never take the
    daemon down or hide tools unpredictably."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        writer, sep, tier = part.partition(":")
        writer = writer.strip().lower()
        tier = tier.strip().lower()
        if not sep or not writer or tier not in _RANK:
            logger.warning("tier-map entry %r malformed (want writer:tier) — skipped", part)
            continue
        out[writer] = tier
    return out


def step(tier: str, delta: int, floor: str = "minimal") -> str:
    """One rung up/down the ladder, clamped to [floor, full]."""
    i = max(_RANK[floor], min(len(TIERS) - 1, _RANK[tier] + delta))
    return TIERS[i]


class PrincipalTierState:
    """TTL'd principal-tier overrides. Thread-safe: read on the event loop
    (tools/list) and written from tool handlers. Lazy expiry — no reaper.
    Keys normalize to the lowercased writer-id namespace; ``None``/empty is
    the shared bucket for key-less callers (embedded stdio, tests, direct
    HTTP with neither a named token nor a writer id)."""

    _GLOBAL = "__global__"

    def __init__(self, ttl_s: float = SESSION_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._m: dict[str, tuple[str, float]] = {}

    @classmethod
    def _key(cls, key: str | None) -> str:
        return (key or "").strip().lower() or cls._GLOBAL

    def get(self, key: str | None) -> str | None:
        k = self._key(key)
        now = time.monotonic()
        with self._lock:
            row = self._m.get(k)
            if row is None:
                return None
            tier, ts = row
            if now - ts >= self._ttl and self._ttl >= 0:
                del self._m[k]
                return None
            return tier

    def set(self, key: str | None, tier: str) -> None:
        k = self._key(key)
        now = time.monotonic()
        with self._lock:
            self._m[k] = (tier, now)
            if len(self._m) > 256:  # opportunistic sweep keeps the dict bounded
                cut = now - self._ttl
                for stale in [s for s, (_, ts) in self._m.items() if ts < cut]:
                    del self._m[stale]


def resolve_tier(principal: str | None, *,
                 state: PrincipalTierState, tier_map: dict[str, str],
                 default_tier: str) -> str:
    """Principal override → tier map → default (spec 2026-08-10; the
    session axis no longer participates)."""
    override = state.get(principal)
    if override is not None:
        return override
    if principal:
        mapped = tier_map.get(principal.strip().lower())
        if mapped:
            return mapped
    return default_tier
