"""Session-scoped toolset tiers (spec: docs/superpowers/specs/2026-07-11).

Visibility model: every tool registers with FastMCP; the transport's
tools/list handler (mcp_server._wire_transport_tiering) filters by the
session's resolved tier. Ordering: minimal ⊂ core ⊂ full. Resolution:
session override (memory_toolset) → writer map (PSEUDOLIFE_MCP_TIER_MAP)
→ env default (PSEUDOLIFE_MCP_TOOLSET). Visibility is a token lever, not
a security boundary — hidden tools stay callable; auth is the bearer token.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("pseudolife-mcp.tiers")

TIERS: tuple[str, ...] = ("minimal", "core", "full")
_RANK = {t: i for i, t in enumerate(TIERS)}

# Sessions are transient (Claude conversations); 12h comfortably outlives
# one and lets abandoned entries lapse without a reaper thread.
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


class SessionTierState:
    """TTL'd session-tier overrides. Thread-safe: read on the event loop
    (tools/list) and written from tool handlers. Lazy expiry — no reaper."""

    _GLOBAL = "__global__"

    def __init__(self, ttl_s: float = SESSION_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._m: dict[str, tuple[str, float]] = {}

    def get(self, key: str | None) -> str | None:
        k = key or self._GLOBAL
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
        k = key or self._GLOBAL
        now = time.monotonic()
        with self._lock:
            self._m[k] = (tier, now)
            if len(self._m) > 256:  # opportunistic sweep keeps the dict bounded
                cut = now - self._ttl
                for stale in [s for s, (_, ts) in self._m.items() if ts < cut]:
                    del self._m[stale]


def resolve_tier(writer: str | None, session_key: str | None, *,
                 state: SessionTierState, tier_map: dict[str, str],
                 default_tier: str) -> str:
    """Session override → writer map → default."""
    override = state.get(session_key)
    if override is not None:
        return override
    if writer:
        mapped = tier_map.get(writer.strip().lower())
        if mapped:
            return mapped
    return default_tier
