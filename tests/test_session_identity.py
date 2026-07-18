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
