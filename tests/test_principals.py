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


def test_resolve_principal_non_ascii_bearer_rejected_not_500(  # review 2026-08-10 finding 1
):
    # hmac.compare_digest raises TypeError on non-ASCII str; the gate must
    # answer 401 (None), never propagate an exception.
    m = {"tok": "x"}
    assert resolve_principal("Bearer café", m, "sing1") is None
    assert resolve_principal("Bearer café", {}, "sing1") is None


def test_resolve_principal_non_ascii_token_still_authenticates():
    # An operator may mint a passphrase-style token with non-ASCII chars;
    # byte-wise comparison must match it, not crash on it.
    m = {"café-token": "hermes-box"}
    assert resolve_principal("Bearer café-token", m, None) == "hermes-box"
    assert resolve_principal("Bearer café-token", {}, "café-tok2") is None


def test_parse_token_map_token_may_contain_colon():
    # rpartition: principals cannot contain ":", tokens may.
    assert parse_token_map("s3cr:et:alice") == {"s3cr:et": "alice"}


def test_misconfigured_tokens_env():
    # review 2026-08-10 finding 2: "configured but nothing parsed" must be
    # distinguishable from "not configured" so the daemon can refuse to
    # start in silent open mode.
    from pseudolife_memory.principals import misconfigured_tokens_env
    assert misconfigured_tokens_env("nocolon", {}) is True
    assert misconfigured_tokens_env("tok9:default", {}) is True   # all skipped
    assert misconfigured_tokens_env(None, {}) is False
    assert misconfigured_tokens_env("", {}) is False
    assert misconfigured_tokens_env("  ", {}) is False
    assert misconfigured_tokens_env("a:x", {"a": "x"}) is False


def test_resolve_principal_matches_non_ascii_token_from_latin1_header():
    # HTTP stacks (Starlette, the daemon's own header maps) decode header
    # bytes as latin-1, so a UTF-8 token arrives as mojibake text. The
    # comparison must still be against the bytes the client sent.
    m = {"café-token": "hermes-box"}
    wire = "café-token".encode("utf-8").decode("latin-1")
    assert resolve_principal("Bearer " + wire, m, None) == "hermes-box"
    assert resolve_principal("Bearer " + wire, {}, "café-token") == DEFAULT_PRINCIPAL
    assert resolve_principal("Bearer " + "café-tok2".encode().decode("latin-1"),
                             m, None) is None


def test_resolve_principal_keeps_trailing_utf8_byte_that_latin1_reads_as_space():
    # UTF-8 "à" is C3 A0 and "ą" is C4 85; decoded as latin-1 the final
    # byte becomes NBSP / NEL, which str.strip() treats as whitespace. Only
    # HTTP whitespace (SP, HTAB) may be trimmed from the presented token.
    for token in ("tok-voilà", "tok-ą"):
        wire = token.encode("utf-8").decode("latin-1")
        assert resolve_principal("Bearer " + wire, {token: "named"}, None) == "named"
        assert resolve_principal("Bearer " + wire + " \t", {token: "named"},
                                 None) == "named"


# -- v59: whose principals are the bank owner's (spec 2026-11-02, section 1) --

def _owner_snapshot():
    from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal
    snap = PrincipalSnapshot(shadowed={"desk"})
    rows = [StoredPrincipal("laptop", "a" * 64, None, True, False),                 # owned
            StoredPrincipal("guest", "b" * 64, None, True, False, owned=False),     # someone else's
            StoredPrincipal("old", "c" * 64, None, True, True),                     # revoked
            StoredPrincipal("pending", None, None, True, False)]                    # not paired yet
    snap.refresh(lambda: (rows, None))
    return snap


def _cfg(not_owner=()):
    from pseudolife_memory.utils.config import CoordinationConfig
    return CoordinationConfig(not_owner_principals=list(not_owner))


def test_the_owner_set_is_environment_owned_stored_and_maintainer():
    from pseudolife_memory.principals import MAINTAINER_PRINCIPAL, owner_principals
    owners = owner_principals(_cfg(), store=_owner_snapshot(),
                              token_map={"t1": "desk", "t2": "codex"}, single_token="t0")
    assert owners == {"desk", "codex", DEFAULT_PRINCIPAL, "laptop", "pending",
                      MAINTAINER_PRINCIPAL}


def test_the_singular_token_alone_makes_default_the_owners():
    from pseudolife_memory.principals import MAINTAINER_PRINCIPAL, owner_principals
    assert owner_principals(_cfg(), store=None, token_map={}, single_token="t0") == {
        DEFAULT_PRINCIPAL, MAINTAINER_PRINCIPAL}
    # Open mode (no token at all): no environment principal is anyone's.
    assert owner_principals(_cfg(), store=None, token_map={}, single_token=None) == {
        MAINTAINER_PRINCIPAL}


def test_not_owner_principals_keeps_an_environment_token_someone_elses():
    from pseudolife_memory.principals import is_owner_principal, owner_principals
    cfg = _cfg(not_owner=[" Friend ", "default"])
    assert cfg.not_owner_principals == ["friend", "default"]
    owners = owner_principals(cfg, store=None, token_map={"t1": "friend", "t2": "codex"},
                              single_token="t0")
    assert "friend" not in owners and DEFAULT_PRINCIPAL not in owners and "codex" in owners
    kw = {"token_map": {"t1": "friend", "t2": "codex"}, "single_token": "t0"}
    assert not is_owner_principal(cfg, "friend", None, **kw)
    assert is_owner_principal(cfg, "codex", None, **kw)


def test_unowned_revoked_and_daemon_principals_are_not_the_owners():
    from pseudolife_memory.principals import DAEMON_PRINCIPAL, is_owner_principal
    snap = _owner_snapshot()
    kw = {"token_map": {"t1": DAEMON_PRINCIPAL}, "single_token": None}
    assert is_owner_principal(_cfg(), "laptop", snap, **kw)
    for name in ("guest", "old", DAEMON_PRINCIPAL, "nobody", None):
        assert not is_owner_principal(_cfg(), name, snap, **kw), name


def test_the_owner_set_reads_the_daemon_environment_and_installed_store(monkeypatch):
    from pseudolife_memory import principals
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "fixture-env-token:codex")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(principals, "_STORE", _owner_snapshot())
    assert principals.owner_principals(_cfg()) == {"codex", "laptop", "pending",
                                                   principals.MAINTAINER_PRINCIPAL}


def test_not_owner_principals_must_be_a_list_of_names():
    import pytest
    from pseudolife_memory.utils.config import CoordinationConfig
    for bad in ("friend", [""], [3]):
        with pytest.raises(ValueError, match="not_owner_principals"):
            CoordinationConfig(not_owner_principals=bad)
    assert CoordinationConfig().not_owner_principals == []


def test_a_stale_stored_view_never_widens_the_owner_set():
    from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal
    now = [1000.0]
    snap = PrincipalSnapshot(max_age=60.0, clock=lambda: now[0])
    snap.refresh(lambda: ([StoredPrincipal("laptop", "a" * 64, None, True, False)], None))
    fresh = owner_principals(_cfg(), store=snap, token_map={}, single_token="t0")
    assert "laptop" in fresh
    now[0] += 61          # the refresher failed past the staleness limit
    stale = owner_principals(_cfg(), store=snap, token_map={}, single_token="t0")
    assert stale == {"default", "maintainer"}
