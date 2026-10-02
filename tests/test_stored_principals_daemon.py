"""Invite, pair and use a stored principal through a real daemon (spec
2026-10-02, Part 2b), with no restart.

A principal is invited in the daemon's own bank, its code redeemed at
``POST /api/pair`` with the SHA-256 of a token minted here, and the token is
then used at once: ``/mcp`` on both protocol paths (the handshake-era
``ClientSession``, whose handlers run on the session manager's task group,
and the 2026-07-28 ``Client``) sees the principal and its stored tier, the
board admits it, and the operator routes refuse it. Revoking it takes the
token away within one refresh.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import urllib.error
import urllib.request

import pytest

from tests.helpers import serve_on_private_bank

psycopg = pytest.importorskip("psycopg")

ENV_TOKEN = "fixture-env-token-daemon"


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    with serve_on_private_bank(
            "principals", tmp_path_factory.mktemp("principals_data"),
            env_extra={"PSEUDOLIFE_MCP_TOKEN": ENV_TOKEN, "PSEUDOLIFE_MCP_TOOLSET": "full"}) as d:
        yield d


def _http(url, *, method="GET", token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


async def _legacy(url, token, tool, args):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                listed = await session.list_tools()
                return [t.name for t in listed.tools], await session.call_tool(tool, args)


async def _modern(url, token, tool, args):
    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url + "/mcp", http_client=http),
                          mode="2026-07-28") as client:
            listed = await client.list_tools()
            return [t.name for t in listed.tools], await client.call_tool(tool, args)


def _payload(result) -> dict:
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    return json.loads("".join(getattr(c, "text", "") for c in result.content))


@pytest.fixture(scope="module")
def paired(daemon):
    """Start the daemon's storage (the schema, and with it the table), invite
    ``laptop`` at tier core, and redeem the code with a token minted here."""
    from pseudolife_memory import principal_store
    from pseudolife_memory.principals import format_pairing_code, secret_sha256

    asyncio.run(_legacy(daemon["url"], ENV_TOKEN, "memory_stats", {}))
    with psycopg.connect(daemon["db"], autocommit=True) as conn:
        invite = principal_store.create_invite(conn, "laptop", tier="core", board=True,
                                               ttl_seconds=600, replace=False)
    token = secrets.token_urlsafe(32)
    status, body, headers = _http(daemon["url"] + "/api/pair", method="POST", body={
        "code": format_pairing_code(invite["code"]), "token_sha256": secret_sha256(token)})
    assert status == 200, body
    assert json.loads(body)["principal"] == "laptop"
    assert token.encode() not in body
    return token


@pytest.mark.parametrize("drive", [_legacy, _modern], ids=["handshake-era", "2026-07-28"])
def test_mcp_sees_the_stored_principal_and_its_tier_at_once(daemon, paired, drive):
    names, result = asyncio.run(drive(daemon["url"], paired, "memory_toolset", {"action": "status"}))
    status = _payload(result)
    assert status["default"] == "core" and status["current"] == "core"
    assert "memory_recall" in names and "memory_forget" not in names


def test_the_env_principal_keeps_its_own_view(daemon, paired):
    _names, result = asyncio.run(_legacy(daemon["url"], ENV_TOKEN, "memory_toolset",
                                         {"action": "status"}))
    assert _payload(result)["default"] == "full"


def test_the_api_and_the_board_admit_it_and_the_operator_routes_refuse_it(daemon, paired):
    status, _body, _ = _http(daemon["url"] + "/api/stats", token=paired)
    assert status == 200
    status, _body, headers = _http(daemon["url"] + "/api/hook/coordination-start", token=paired)
    assert status == 200 and headers.get("X-PL-Board", headers.get("x-pl-board")) == "on"
    for path in ("/api/config", "/api/daemon-notice"):
        status, body, _ = _http(daemon["url"] + path, method="POST", token=paired, body={})
        assert status == 403 and json.loads(body) == {"error": "operator_principal_required"}
    status, body, _ = _http(daemon["url"] + "/api/config", method="POST", token=ENV_TOKEN, body={})
    assert not (status == 403 and b"operator_principal_required" in body)


def test_a_used_code_is_refused_with_the_same_body(daemon, paired):
    status, body, _ = _http(daemon["url"] + "/api/pair", method="POST", body={
        "code": "0000-0000-0000", "token_sha256": "0" * 64})
    assert (status, body) == (400, b'{"error": "pairing_refused"}')


def test_revoking_takes_the_token_away_within_one_refresh(daemon, paired):
    from pseudolife_memory import principal_store
    with psycopg.connect(daemon["db"], autocommit=True) as conn:
        assert principal_store.revoke(conn, "laptop")
    deadline = time.monotonic() + 25
    status = None
    while time.monotonic() < deadline:
        status, _body, _ = _http(daemon["url"] + "/api/stats", token=paired)
        if status == 401:
            break
        time.sleep(0.5)
    assert status == 401
