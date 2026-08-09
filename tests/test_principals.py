"""Token -> principal map (spec 2026-08-10, identity re-keying).

Pure module: no MCP SDK, no embedder, no storage.
"""
from __future__ import annotations

from pseudolife_memory.principals import (
    DEFAULT_PRINCIPAL, parse_token_map, resolve_principal,
)


def test_parse_token_map_happy_path():
    m = parse_token_map("aaa111:claude-desktop, bbb222:Hermes")
    assert m == {"aaa111": "claude-desktop", "bbb222": "hermes"}


def test_parse_token_map_malformed_entries_skipped():
    # Missing colon, empty principal, empty token: all skipped, never fatal.
    # A skipped token must NOT authenticate (fail closed) — asserted in the
    # resolve tests below via absence from the map.
    assert parse_token_map("nocolon") == {}
    assert parse_token_map("tok:") == {}
    assert parse_token_map(":principal") == {}
    assert parse_token_map("a:x, nocolon, b:y") == {"a": "x", "b": "y"}
    assert parse_token_map(None) == {}
    assert parse_token_map("") == {}


def test_parse_token_map_duplicate_token_first_wins():
    m = parse_token_map("tok1:first, tok1:second")
    assert m == {"tok1": "first"}


def test_parse_token_map_reserved_default_principal_skipped():
    # "default" is the reserved singular-token principal; a map entry naming
    # it would let a second token impersonate the default identity path.
    assert parse_token_map("tok9:default") == {}
    # Tokens keep their case (they are secrets); principals lowercase.
    assert parse_token_map("tok9:DEFAULT, tokA:ok") == {"tokA": "ok"}


def test_resolve_principal_named_from_map():
    m = {"aaa111": "claude-desktop"}
    assert resolve_principal("Bearer aaa111", m, None) == "claude-desktop"
    assert resolve_principal("Bearer aaa111", m, "single-tok") == "claude-desktop"


def test_resolve_principal_singular_token_maps_to_default():
    assert resolve_principal("Bearer sing1", {}, "sing1") == DEFAULT_PRINCIPAL
    # Map consulted first, singular second.
    m = {"aaa111": "hermes"}
    assert resolve_principal("Bearer sing1", m, "sing1") == DEFAULT_PRINCIPAL


def test_resolve_principal_rejects_unknown_or_missing_bearer():
    m = {"aaa111": "hermes"}
    assert resolve_principal("Bearer nope", m, "sing1") is None
    assert resolve_principal(None, m, "sing1") is None
    assert resolve_principal("", m, "sing1") is None
    assert resolve_principal("Basic aaa111", m, "sing1") is None  # wrong scheme


def test_resolve_principal_open_mode_when_no_tokens_configured():
    # No auth configured at all (loopback open mode): everyone is "default".
    assert resolve_principal(None, {}, None) == DEFAULT_PRINCIPAL
    assert resolve_principal("Bearer anything", {}, None) == DEFAULT_PRINCIPAL
