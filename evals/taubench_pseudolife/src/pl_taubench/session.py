"""The per-simulation memory episode: identity, served ids, and the use window.

One ``Episode`` == one daemon episode == one ``X-PL-Session`` value, minted when
the agent is built and closed when the simulation ends.

Why ``served_ids`` and ``first_search_ts`` live here: ``memory_outcome(used_ids=)``
credits an id only when the search that served it ran under the SAME session
within ``PL_USE_WINDOW_SECONDS`` (daemon default 3600). The episode is the only
object that sees both sides — every search result and the task-end outcome — so
it owns the bookkeeping and the window check.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

#: The daemon's own default for how long a served id stays creditable.
DEFAULT_WINDOW_SECONDS = 3600.0

#: How many ids one ``memory_outcome`` carries. The daemon takes more, but a
#: whole episode's served set is mostly noise by the time it is that long.
MAX_USED_IDS = 50


@dataclass
class Episode:
    """One simulation's memory session."""

    session_uid: str
    task_id: Optional[str] = None
    trial: Optional[int] = None
    run: Optional[str] = None
    served_ids: list[int] = field(default_factory=list)
    first_search_ts: Optional[float] = None
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    #: The client bound to this session uid. The environment hook resolves it
    #: through the episode because it is wired before the agent exists.
    client: Any = None

    def note_search(self, ids, ts: Optional[float] = None) -> None:
        """Record one search: stamp the window start, add newly served ids.

        Called for EVERY search, including ones that returned nothing — the
        window opens at the first search, not at the first credited id.
        Deduped in first-seen order so ``used_ids`` reflects retrieval order.
        """
        if self.first_search_ts is None:
            self.first_search_ts = time.time() if ts is None else ts
        seen = set(self.served_ids)
        for entry_id in ids or []:
            if isinstance(entry_id, bool) or not isinstance(entry_id, int):
                continue
            if entry_id in seen:
                continue
            seen.add(entry_id)
            self.served_ids.append(entry_id)

    def window_ok(self, now: Optional[float] = None) -> bool:
        """True while ids served in this episode are still creditable.

        Strictly less-than: at exactly the window the daemon has already stopped
        crediting, so the label is refused rather than claimed.
        """
        if self.first_search_ts is None:
            return True
        now = time.time() if now is None else now
        return (now - self.first_search_ts) < self.window_seconds

    def used_ids(self, cap: int = MAX_USED_IDS) -> list[int]:
        return list(self.served_ids[:cap])


def new_episode(
    task_id: Optional[str] = None,
    trial: Optional[int] = None,
    run: Optional[str] = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> Episode:
    return Episode(
        session_uid=uuid.uuid4().hex,
        task_id=task_id,
        trial=trial,
        run=run,
        window_seconds=window_seconds,
    )


def extract_served_ids(result: Any) -> list[int]:
    """The entry ids a search result served, in order.

    ``memory_search`` returns ``entries`` carrying integer ids; the cortex rows
    beside them are slot-keyed and have no entry id, and ``memory_lesson_search``
    returns no ids at all — both yield an empty list, which is correct: nothing
    was served that ``used_ids`` can credit.
    """
    if not isinstance(result, dict):
        return []
    out: list[int] = []
    for entry in result.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, bool) or not isinstance(entry_id, int):
            continue
        out.append(entry_id)
    return out


# ── thread-local binding ─────────────────────────────────────────────────────
# The environment hook (memory_env.get_response) runs on the simulation thread
# but is wired BEFORE the agent is built, so it cannot be handed the episode at
# attach time. It resolves it here instead, at call time.
_local = threading.local()


def bind_episode(episode: Optional[Episode]) -> None:
    _local.episode = episode


def current_episode() -> Optional[Episode]:
    return getattr(_local, "episode", None)


def clear_episode() -> None:
    _local.episode = None
