"""``EpisodeManager`` unit tests (Tier C C-2).

The EpisodeManager owns episode lifecycle (start / end / list / stamp)
plus persistence as a plain dict suitable for ``torch.save``. Tests
exercise the behaviour-facing surface — every test corresponds to a
guarantee documented in the docstring of the method under test.

Design points exercised here:

* **One open episode at a time.** Starting while another is open
  auto-closes the prior with a ``closed_by_new_start`` flag, rather
  than raising.
* **Stamp is a no-op when nothing is open.** Entries stored outside any
  episode keep ``episode_id=None``.
* **List is newest-first** and respects ``include_open`` / ``limit``.
* **Persistence** round-trips episodes verbatim, including the
  currently-open id.
"""

from __future__ import annotations

import time

import torch

from pseudolife_memory.memory.episodes import Episode, EpisodeManager
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _new_entry(text: str = "x") -> MemoryEntry:
    return MemoryEntry(text=text, embedding=torch.zeros(4))


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_start_returns_episode_with_uuid_and_title() -> None:
    em = EpisodeManager()
    ep = em.start("first session")
    assert isinstance(ep, Episode)
    assert ep.title == "first session"
    assert isinstance(ep.id, str) and len(ep.id) == 32  # uuid4 hex
    assert ep.started_at > 0
    assert ep.ended_at is None
    assert em.current_id == ep.id


def test_start_with_hint_preserves_hint() -> None:
    em = EpisodeManager()
    ep = em.start("debug", hint="reproducing the chain_residual NaN")
    assert ep.hint == "reproducing the chain_residual NaN"


def test_end_closes_current_and_clears_pointer() -> None:
    em = EpisodeManager()
    ep = em.start("work")
    closed = em.end()
    assert closed is not None
    assert closed.id == ep.id
    assert closed.ended_at is not None and closed.ended_at >= closed.started_at
    assert em.current_id is None


def test_end_when_none_open_returns_none() -> None:
    em = EpisodeManager()
    assert em.end() is None


def test_start_while_open_auto_closes_prior_with_flag() -> None:
    em = EpisodeManager()
    first = em.start("first")
    second = em.start("second")
    closed_first = em.get(first.id)
    assert closed_first is not None
    assert closed_first.ended_at is not None
    assert closed_first.closed_by_new_start is True
    assert em.current_id == second.id


# ── Stamping ─────────────────────────────────────────────────────────────


def test_stamp_when_open_fills_entry_fields() -> None:
    em = EpisodeManager()
    ep = em.start("session-alpha")
    entry = _new_entry()
    em.stamp(entry)
    assert entry.episode_id == ep.id
    assert entry.episode_title == "session-alpha"


def test_stamp_when_closed_is_no_op() -> None:
    em = EpisodeManager()
    em.start("session")
    em.end()
    entry = _new_entry()
    em.stamp(entry)
    assert entry.episode_id is None
    assert entry.episode_title is None


def test_stamp_when_never_started_is_no_op() -> None:
    em = EpisodeManager()
    entry = _new_entry()
    em.stamp(entry)
    assert entry.episode_id is None


# ── Listing + lookup ─────────────────────────────────────────────────────


def test_list_returns_newest_first() -> None:
    em = EpisodeManager()
    a = em.start("a")
    em.end()
    time.sleep(0.001)  # ensure timestamp differentiation
    b = em.start("b")
    em.end()
    listing = em.list()
    assert [e.id for e in listing] == [b.id, a.id]


def test_list_respects_limit() -> None:
    em = EpisodeManager()
    for i in range(5):
        em.start(f"e{i}")
        em.end()
    assert len(em.list(limit=3)) == 3


def test_list_can_exclude_open_episodes() -> None:
    em = EpisodeManager()
    em.start("closed")
    em.end()
    open_ep = em.start("open")
    closed_only = em.list(include_open=False)
    assert open_ep.id not in {e.id for e in closed_only}
    assert len(closed_only) == 1


def test_get_returns_episode_by_id_or_none() -> None:
    em = EpisodeManager()
    ep = em.start("foo")
    assert em.get(ep.id) is ep
    assert em.get("nope") is None


# ── Persistence round-trip ───────────────────────────────────────────────


def test_to_dict_from_dict_round_trip_preserves_episodes_and_open_pointer() -> None:
    em = EpisodeManager()
    a = em.start("a")
    em.end()
    b = em.start("b-open")  # leave open
    payload = em.to_dict()
    restored = EpisodeManager.from_dict(payload)
    assert set(restored.episodes.keys()) == {a.id, b.id}
    assert restored.current_id == b.id
    assert restored.episodes[a.id].ended_at is not None
    assert restored.episodes[b.id].ended_at is None


def test_from_dict_handles_empty_payload() -> None:
    """Pre-v6 saves have no ``episodes`` block — ``from_dict({})`` should
    return an empty, valid manager."""
    em = EpisodeManager.from_dict({})
    assert em.episodes == {}
    assert em.current_id is None
