"""Episode lifecycle and tag normalisation for PseudoLife-MCP (Tier C).

An *episode* is a bracket around a logical work session — a project
sprint, a debugging push, a single user task. While an episode is open,
every memory stored through the CMS pipeline is stamped with the
episode's ``id`` / ``title``, enabling retrieval queries like *"what did
we work on Tuesday?"* or *"summarise this session"*.

Design choices
--------------

* **One open episode at a time.** ``start()`` on a manager with an
  already-open episode auto-closes the prior one and stamps it with
  ``closed_by_new_start=True``. The alternative — raising — is
  unfriendly to an LLM client that won't always reliably call
  ``end()``. Graceful auto-close means stale episodes degrade into
  "current working session" semantics instead of crashing.

* **uuid4 hex ids.** Visible to Claude in responses. A 32-char hex is
  ugly but stable and collision-free without coordination.

* **Pure-data persistence.** ``to_dict()`` / ``from_dict()`` produce
  JSON-compatible dicts so the EpisodeManager round-trips through
  ``torch.save`` cleanly alongside the CMS bands. No external storage.

* **Stamp is a method on the manager, not on the entry.** Keeps the
  ``MemoryEntry`` dataclass pure-data. The CMS calls ``stamp(entry)``
  after constructing the entry but before placing it in a band.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from pseudolife_memory.memory.titans_memory import MemoryEntry


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Lowercase / strip / dedupe a tag list, preserving first-seen order.

    Non-string entries are dropped silently. Empty strings (or strings
    that strip to empty) are dropped. The output is suitable to drop
    straight onto ``MemoryEntry.tags`` — no further sanitisation
    required downstream.
    """
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            continue
        norm = raw.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


@dataclass
class Episode:
    """One bracketed work session.

    ``ended_at is None`` means the episode is currently open. There is
    at most one open episode per :class:`EpisodeManager` instance.

    ``closed_by_new_start`` is set to ``True`` when ``EpisodeManager.start``
    auto-closes this episode because a new one began before ``end()`` was
    called — useful for telemetry / debugging stale sessions.
    """

    id: str
    title: str
    started_at: float
    ended_at: float | None = None
    hint: str | None = None
    closed_by_new_start: bool = False
    session_key: str | None = None
    parent_id: str | None = None


class EpisodeManager:
    """Owns the episode log + the current-open pointer.

    Thread-safety: the CMS / service layer holds a coarse lock; this
    class doesn't add its own.
    """

    def __init__(self) -> None:
        self.episodes: dict[str, Episode] = {}
        self.current_id: str | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self, title: str, hint: str | None = None,
              session_key: str | None = None) -> Episode:
        """Open a new episode. Auto-closes any prior open one.

        Returns the freshly-opened episode. The prior open episode (if
        any) has ``ended_at`` set to ``now`` and ``closed_by_new_start=True``
        before the new one is opened.
        """
        if self.current_id is not None:
            prior = self.episodes.get(self.current_id)
            if prior is not None and prior.ended_at is None:
                prior.ended_at = time.time()
                prior.closed_by_new_start = True

        ep = Episode(
            id=uuid.uuid4().hex,
            title=title,
            started_at=time.time(),
            hint=hint,
            session_key=session_key,
        )
        self.episodes[ep.id] = ep
        self.current_id = ep.id
        return ep

    def start_nested(self, title: str, hint: str | None = None) -> Episode:
        """Open a sub-episode under the current open leaf (which stays open).
        Falls back to a root episode when nothing is open."""
        parent = self.open_episode()
        ep = Episode(
            id=uuid.uuid4().hex,
            title=title,
            started_at=time.time(),
            hint=hint,
            parent_id=parent.id if parent is not None else None,
        )
        self.episodes[ep.id] = ep
        self.current_id = ep.id
        return ep

    def end(self) -> Episode | None:
        """Close the current open leaf and pop to its parent (if still open)."""
        if self.current_id is None:
            return None
        ep = self.episodes.get(self.current_id)
        if ep is None:
            self.current_id = None
            return None
        ep.ended_at = time.time()
        parent = self.episodes.get(ep.parent_id) if ep.parent_id else None
        self.current_id = parent.id if (parent and parent.ended_at is None) else None
        return ep

    def end_session(self, session_key: str | None) -> Episode | None:
        """Close the open ROOT episode matching ``session_key`` and cascade-close
        any still-open descendants. Returns the closed root, or None."""
        root = None
        for e in self.episodes.values():
            if (e.ended_at is None and e.parent_id is None
                    and (session_key is None or e.session_key == session_key)):
                root = e
                break
        if root is None:
            return None
        now = time.time()
        # close root + every still-open episode reachable from it
        for e in self.episodes.values():
            if e.ended_at is None and self._descends_from(e, root.id):
                e.ended_at = now
        self.current_id = None
        return root

    def _descends_from(self, ep: Episode, root_id: str) -> bool:
        seen: set[str] = set()
        cur: Episode | None = ep
        while cur is not None and cur.id not in seen:
            if cur.id == root_id:
                return True
            seen.add(cur.id)
            cur = self.episodes.get(cur.parent_id) if cur.parent_id else None
        return False

    def open_episode(self) -> Episode | None:
        """The current open leaf episode, or None when nothing is open."""
        if self.current_id is None:
            return None
        ep = self.episodes.get(self.current_id)
        return ep if (ep is not None and ep.ended_at is None) else None

    # ── Lookup / listing ─────────────────────────────────────────────

    def get(self, id: str) -> Episode | None:
        return self.episodes.get(id)

    def list(
        self,
        limit: int = 20,
        include_open: bool = True,
    ) -> list[Episode]:
        """Episodes newest-first by ``started_at``, capped at ``limit``."""
        eps = list(self.episodes.values())
        if not include_open:
            eps = [e for e in eps if e.ended_at is not None]
        eps.sort(key=lambda e: e.started_at, reverse=True)
        if limit is not None and limit >= 0:
            eps = eps[:limit]
        return eps

    # ── Stamping ─────────────────────────────────────────────────────

    def stamp(self, entry: MemoryEntry) -> None:
        """Fill ``entry.episode_id`` / ``entry.episode_title`` from the
        current open episode. No-op when no episode is open.
        """
        if self.current_id is None:
            return
        ep = self.episodes.get(self.current_id)
        if ep is None:
            return
        entry.episode_id = ep.id
        entry.episode_title = ep.title

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodes": {eid: asdict(ep) for eid, ep in self.episodes.items()},
            "current_id": self.current_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EpisodeManager":
        em = cls()
        if not payload:
            return em
        for eid, ep_dict in (payload.get("episodes") or {}).items():
            # Filter unknown keys defensively — schema may add fields.
            known = {
                k: v
                for k, v in ep_dict.items()
                if k in Episode.__dataclass_fields__
            }
            em.episodes[eid] = Episode(**known)
        em.current_id = payload.get("current_id")
        # Defensive: if the saved current_id points at something that was
        # closed mid-save (or no longer exists), clear it.
        if em.current_id is not None:
            cur = em.episodes.get(em.current_id)
            if cur is None or cur.ended_at is not None:
                em.current_id = None
        return em
