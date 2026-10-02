"""Stored principals on ``/mcp`` (spec 2026-10-02, "One resolver, every
site"): ``tools/call`` and ``tools/list`` resolve the caller inside the
transport wrap, from the request's own headers, so a stored principal is
seen even on the handshake-era path, whose handlers run on a task group the
gate's context never reaches. The stored tier is the default after
``PSEUDOLIFE_MCP_TIER_MAP``.

tests/test_stored_principals_daemon.py drives the same through a real
daemon on both protocol paths.
"""
from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal
from pseudolife_memory.principals import install_store, installed_store, secret_sha256
from tests.helpers import invoke_tool as _invoke
from tests.test_mcp_server import _FakeReqCtx, _reload_tiered

STORED_TOKEN = "fixture-stored-token"
ENV = {"PSEUDOLIFE_MCP_TOOLSET": "full", "PSEUDOLIFE_MCP_TOKENS": "fixture-env-token:desk",
       "PSEUDOLIFE_MCP_TIER_MAP": "desk:core"}


@pytest.fixture
def stored():
    previous = installed_store()
    snap = PrincipalSnapshot()
    snap.refresh(lambda: ([StoredPrincipal("laptop", secret_sha256(STORED_TOKEN), "minimal", True,
                                           False)], None))
    install_store(snap)
    yield snap
    install_store(previous)


def _ctx(headers, *, shape="request"):
    """The two context shapes ``_headers_of`` reads: the Starlette request
    (``ctx.request.headers``, what the runtime passes) or ``ctx.headers``."""
    if shape == "request":
        return SimpleNamespace(request=SimpleNamespace(headers=headers), session=None,
                               request_id=1, meta=None, method="tools/list")
    return SimpleNamespace(headers=headers, request=None, session=None, request_id=1, meta=None,
                           method="tools/list")


@pytest.mark.parametrize("shape", ["request", "headers"])
def test_tools_list_shows_the_stored_tier(tmp_path, monkeypatch, stored, shape):
    mod = _reload_tiered(tmp_path, monkeypatch, **ENV)
    entry = mod.mcp._lowlevel_server._request_handlers["tools/list"]
    result = asyncio.run(entry.handler(_ctx({"authorization": f"Bearer {STORED_TOKEN}"},
                                            shape=shape), None))
    assert {t.name for t in result.tools} == mod._visible_tool_names("minimal")
    # The environment's map still decides for its own principal.
    result = asyncio.run(entry.handler(_ctx({"authorization": "Bearer fixture-env-token"},
                                            shape=shape), None))
    assert {t.name for t in result.tools} == mod._visible_tool_names("core")


def test_tools_call_binds_the_resolved_principal(tmp_path, monkeypatch, stored):
    """The call wrap resolves the bearer itself and binds the principal:
    no binding from the gate is present in this fresh event loop."""
    mod = _reload_tiered(tmp_path, monkeypatch, **ENV)
    from pseudolife_memory import writer_context
    handlers = mod.mcp._lowlevel_server._request_handlers
    seen = []

    async def probe(ctx, params):
        seen.append((writer_context._REQUEST_PRINCIPAL.get(), writer_context.current_principal(),
                     mod._resolve_principal_tier()))
        return SimpleNamespace(tools=[], model_copy=lambda update: None)

    handlers["tools/call"] = dataclasses.replace(handlers["tools/call"], handler=probe)
    mod._wire_transport_tiering()
    asyncio.run(handlers["tools/call"].handler(
        _ctx({"authorization": f"Bearer {STORED_TOKEN}"}), None))
    asyncio.run(handlers["tools/call"].handler(
        _ctx({"authorization": "Bearer fixture-env-token"}), None))
    assert seen == [("laptop", "laptop", "minimal"), ("desk", "desk", "core")]


def test_the_toolset_floor_is_the_stored_tier(tmp_path, monkeypatch, stored):
    mod = _reload_tiered(tmp_path, monkeypatch, **ENV)
    with _FakeReqCtx({"authorization": f"Bearer {STORED_TOKEN}"}):
        status = _invoke("memory_toolset", {"action": "status"})
        assert status["default"] == "minimal" and status["current"] == "minimal"
        assert _invoke("memory_toolset", {"action": "collapse"})["changed"] is False
    with _FakeReqCtx({"authorization": "Bearer fixture-env-token"}):
        assert _invoke("memory_toolset", {"action": "status"})["default"] == "core"
    assert mod is not None


def test_a_writer_header_naming_a_stored_principal_does_not_take_its_tier(
        tmp_path, monkeypatch, stored):
    """The stored tier follows the bearer, not a client-asserted writer id."""
    mod = _reload_tiered(tmp_path, monkeypatch, **ENV)
    with _FakeReqCtx({"x-pl-writer": "laptop"}):
        assert mod._resolve_principal_tier() == "full"


def test_an_unavailable_view_names_no_stored_principal(tmp_path, monkeypatch):
    previous = installed_store()
    install_store(PrincipalSnapshot())          # never loaded
    try:
        mod = _reload_tiered(tmp_path, monkeypatch, **ENV)
        entry = mod.mcp._lowlevel_server._request_handlers["tools/list"]
        result = asyncio.run(entry.handler(_ctx({"authorization": f"Bearer {STORED_TOKEN}"}),
                                           None))
        assert {t.name for t in result.tools} == mod._visible_tool_names("full")
    finally:
        install_store(previous)
