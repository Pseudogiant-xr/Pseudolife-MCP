"""Session identity contract (spec 2026-07-18): tier resolution units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.writer_context import (
    reset_writer_context, resolve_writer, resolve_writer_detailed,
    set_writer_context,
)


def test_detailed_override_maps_to_header_slot():
    tok = set_writer_context("w1", "sessA")
    try:
        assert resolve_writer_detailed("dflt") == ("w1", "sessA", None)
        assert resolve_writer("dflt") == ("w1", "sessA")
    finally:
        reset_writer_context(tok)


def test_detailed_no_context_returns_default_and_nones():
    assert resolve_writer_detailed("dflt") == ("dflt", None, None)
    assert resolve_writer("dflt") == ("dflt", None)


# ── Service-side tier resolution + persistent active-session pointer
# (PG-backed) ─────────────────────────────────────────────────────────

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
from tests.test_outcome_inference import pg_service  # noqa: F401


def test_resolver_prefers_header_then_pointer_then_transport(pg_service):
    svc = pg_service
    svc.set_active_session("hookSess")
    tok = set_writer_context("w", "headerSess")
    try:
        assert svc._resolve_writer() == ("w", "headerSess")
    finally:
        reset_writer_context(tok)
    # no header: pointer wins (transport can't be simulated without the MCP
    # request context — its tier is covered by resolve_writer_detailed units)
    assert svc._resolve_writer()[1] == "hookSess"
    svc.set_active_session(None)
    assert svc._resolve_writer()[1] is None


def test_pointer_persists_and_clear_only_if_owner(pg_service):
    svc = pg_service
    svc.set_active_session("s1")
    assert svc._storage.get_meta("active_session_pointer")["session_id"] == "s1"
    assert svc.clear_active_session("someone-else") is False
    assert svc._resolve_writer()[1] == "s1"
    assert svc.clear_active_session("s1") is True
    assert svc._resolve_writer()[1] is None
    assert svc._storage.get_meta("active_session_pointer") is None


# ── Ownership guards on both episode-close paths (Task 3) ────────────────────
# The observed bug: `episode_end_session(None)` used to force-close ANY open
# root, and `episode_end()`'s no-identity fallback popped whatever episode
# happened to be globally "current" — either way, one workstream could pop
# another's session episode out from under it.


def test_end_session_never_pops_foreign_root(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")          # non-empty -> survives close-prune
    tok = set_writer_context("w", None)            # resolver yields no identity
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "attacker-key")  # identity that owns nothing
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "victim-key")    # the owner can close it
    try:
        res = svc.episode_end_session(None)
        assert res.get("id")
    finally:
        reset_writer_context(tok)


def test_episode_end_fallthrough_guarded(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")
    tok = set_writer_context("w", "attacker-key")
    try:
        res = svc.episode_end()               # no open sub-episode for attacker
        # attacker has a resolved identity but owns nothing open, so
        # open_leaf_for("attacker-key") is None before the ownership-mismatch
        # branch is even reached -> plain no-op, not the mismatch dict.
        assert res == {}
    finally:
        reset_writer_context(tok)
    with svc._lock:
        open_roots = [e for e in svc._cms.episodes.episodes.values()
                      if e.parent_id is None and e.ended_at is None
                      and e.session_key == "victim-key"]
    assert len(open_roots) == 1


def test_episode_end_no_identity_never_pops_foreign_root(pg_service):
    """The real fallthrough: no resolved identity at all, while another
    session's root is the globally "current" episode. `Episodes.open_episode`
    would hand back that root regardless of ownership if the guard weren't
    there."""
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")
    tok = set_writer_context("w", None)
    try:
        res = svc.episode_end()
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    with svc._lock:
        open_roots = [e for e in svc._cms.episodes.episodes.values()
                      if e.parent_id is None and e.ended_at is None
                      and e.session_key == "victim-key"]
    assert len(open_roots) == 1
