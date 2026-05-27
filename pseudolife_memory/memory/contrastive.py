"""Contrastive retrieval objective — learn from negative signals.

When the user says "no, that's wrong" / "are you sure" / "I never said
that" after a recall, the system should *learn* from that. This module:

1. **Detects** negative signals on the user's current message via
   anchored regex patterns (start of message after normalisation).

2. **Selects** the target — the top-1 retrieval against the *previous*
   user message, which is the memory most likely to have informed the
   wrong reply.

3. **Suppresses** the target at entry level (``superseded_at=now()``)
   AND applies a small negative-gradient step on the owning band so
   the MLP learns to map similar patterns slightly away from themselves.

4. **Audits** every fire by storing a ``source="correction"`` marker
   so the operator can inspect what got contrastive'd via
   ``/api/memory/search``.

All failures caught and logged at DEBUG. The chat path must never
break because contrastive had a bad day.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only imports.
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.utils.config import ContrastiveConfig

logger = logging.getLogger(__name__)


# Anchored regex patterns matching negative-signal openings. Order doesn't
# matter — first match wins, no overlap considerations.
_NEGATIVE_PATTERNS = [
    # Direct disagreement.
    re.compile(r"^(no|nope)[\s,.!?]"),
    re.compile(r"^(that('?s|\s+is)|you'?re|you\s+are)\s+wrong\b"),
    re.compile(r"^(that|this)\s+is\s+incorrect\b"),
    re.compile(r"^incorrect[\s,.!?]"),
    # Doubt / probe.
    re.compile(r"^are\s+you\s+sure\b"),
    re.compile(r"^is\s+that\s+(right|correct|true)\b"),
    re.compile(r"^really\?"),
    # Correction prefix.
    re.compile(r"^actually[\s,]"),
    # Explicit denial of what bot attributed.
    re.compile(r"^i\s+(never|didn'?t|did\s+not|haven'?t)\s+(say|said|tell|told|mention|mentioned)\b"),
    re.compile(r"^that'?s\s+not\s+(what|true|right|correct)\b"),
]


class NegativeSignalDetector:
    """Pure function on the user's current message.

    Always check ``config.enabled`` before consulting patterns so a
    disabled detector returns False without paying the regex cost.
    """

    def __init__(self, config: "ContrastiveConfig") -> None:
        self.config = config

    def detect(self, text: str) -> bool:
        if not self.config.enabled:
            return False
        if not text:
            return False
        # Lowercase + strip leading whitespace and common openings so
        # patterns can be anchored to start.
        normalised = text.lstrip(" \t\n\"'*_-").lower()
        # Bound the head we examine — patterns are anchored, no need to
        # scan the whole message.
        head = normalised[:200]
        return any(p.match(head) for p in _NEGATIVE_PATTERNS)


class ContrastiveUpdater:
    """Applies suppression + contrastive band update on negative signal.

    Cheap to construct, cheap to call ``apply`` on every chat turn
    (the detector short-circuits when the signal is absent).
    """

    def __init__(
        self,
        config: "ContrastiveConfig",
        embedder: "EmbeddingPipeline",
    ) -> None:
        self.config = config
        self.embedder = embedder
        self.detector = NegativeSignalDetector(config)
        # Telemetry: number of contrastive fires this process lifetime.
        self.fire_count: int = 0

    # ── Public entry point ────────────────────────────────────────────────

    def apply(
        self,
        cms: "ContinuumMemorySystem",
        current_message: str,
        previous_user_message: str | None,
    ) -> int:
        """Run the contrastive pipeline. Returns count of targets updated.

        Returns 0 (no-op) when:
        * Detector disabled or didn't fire.
        * No previous user message to anchor target selection.
        * Top-1 retrieval score below ``min_target_score``.
        * Any internal error (logged at DEBUG).
        """
        try:
            if not self.detector.detect(current_message):
                return 0
            if not previous_user_message or not previous_user_message.strip():
                return 0
            target = self._select_target(cms, previous_user_message)
            if target is None:
                return 0
            self._suppress_entry(target)
            self._contrast_band(cms, target)
            self._write_audit_marker(cms, target, current_message)
            self.fire_count += 1
            logger.info(
                "Contrastive fire #%d: target=%r",
                self.fire_count, (target.text or "")[:60],
            )
            return 1
        except Exception as exc:  # noqa: BLE001 — silent fallback.
            logger.debug("ContrastiveUpdater.apply failed: %s", exc)
            return 0

    # ── Target selection ──────────────────────────────────────────────────

    def _select_target(
        self,
        cms: "ContinuumMemorySystem",
        previous_user_message: str,
    ) -> "MemoryEntry | None":
        """Re-retrieve on the previous query; return top-1 above min score."""
        emb = self.embedder.encode_single(previous_user_message)
        try:
            result = cms.retrieve(emb, query_text=previous_user_message)
        except TypeError:
            result = cms.retrieve(emb)
        if not result or not result.entries:
            return None
        top_entry = result.entries[0]
        top_score = result.scores[0] if result.scores else 0.0
        if top_score < self.config.min_target_score:
            logger.debug(
                "Contrastive target rejected: top score %.3f < min %.3f",
                top_score, self.config.min_target_score,
            )
            return None
        return top_entry

    # ── Suppression + band update ─────────────────────────────────────────

    @staticmethod
    def _suppress_entry(entry: "MemoryEntry") -> None:
        """Mark the entry as superseded so retrieval hides it.

        Reuses the v0.7.3 supersession mechanism — identical effect to
        a contradiction-detected supersession, just triggered by a
        different signal source.
        """
        entry.superseded_at = time.time()

    def _contrast_band(
        self,
        cms: "ContinuumMemorySystem",
        target: "MemoryEntry",
    ) -> None:
        """Apply contrastive update on whichever band owns the target."""
        for band in getattr(cms, "bands", []):
            # Identity check — entries live in exactly one band.
            for entry in band.entries:
                if entry is target:
                    try:
                        band.contrastive_update(target.embedding, scale=self.config.scale)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Band %r contrastive_update failed: %s",
                            band.name, exc,
                        )
                    return
        # Target not found in any band — entry was evicted between
        # retrieval and this call. Suppression still applies on the
        # detached MemoryEntry, but band update is moot.
        logger.debug("Contrastive: target not found in any band; band update skipped.")

    def _write_audit_marker(
        self,
        cms: "ContinuumMemorySystem",
        target: "MemoryEntry",
        current_message: str,
    ) -> None:
        """Store an audit-readable correction marker via the CMS pipeline.

        Marker text is human-friendly so ``/api/memory/search?source=correction``
        produces a useful audit log.
        """
        marker_text = (
            f"[correction] User signal {current_message[:60]!r} "
            f"contrasted target {(target.text or '')[:80]!r}"
        )
        try:
            embedding = self.embedder.encode_single(marker_text)
            cms.store(marker_text, embedding, source="correction")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Correction marker store failed: %s", exc)
