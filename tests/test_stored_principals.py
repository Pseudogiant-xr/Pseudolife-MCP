"""Principals stored in the bank (spec 2026-10-02, Part 2b): the shared
helpers, the one resolver, admission, and the daemon's in-memory snapshot.

Pure logic; the PostgreSQL side (the table, redemption, refresh against a
real database) is tests/test_principal_store_pg.py.
"""
from __future__ import annotations

import hashlib

import pytest

from pseudolife_memory import principals
from pseudolife_memory.principals import (
    DEFAULT_PRINCIPAL, format_pairing_code, normalize_pairing_code,
    secret_sha256, valid_principal_name,
)


# -- the shared helpers -------------------------------------------------------

@pytest.mark.parametrize("name", ["a", "laptop", "box-2.claude_code", "0", "a" * 64])
def test_valid_principal_names(name):
    assert valid_principal_name(name)


@pytest.mark.parametrize("name", [
    "", "A", "Laptop", "-box", ".box", "_box", "a" * 65, "box/../x", "box x", "box\n",
    "..", "bär", None, 7, "box:tok"])
def test_invalid_principal_names(name):
    assert not valid_principal_name(name)


def test_pairing_codes_are_normalised():
    assert normalize_pairing_code("abcd-efgh-jkmn") == "ABCDEFGHJKMN"
    assert normalize_pairing_code("  ABCD-EFGH-JKMN\n") == "ABCDEFGHJKMN"
    # Crockford's reading of the look-alike letters.
    assert normalize_pairing_code("o0o0-iIlL-1111") == "000011111111"
    assert normalize_pairing_code("ABCDEFGHJKMN") == "ABCDEFGHJKMN"


@pytest.mark.parametrize("text", [
    "", "ABCD-EFGH-JKM", "ABCD-EFGH-JKMNP", "ABCD-EFGH-JKMU", "ABCD EFGH JKMN!",
    None, 12, "ABCD-EFGH-JKMÑ"])
def test_malformed_pairing_codes_are_none(text):
    assert normalize_pairing_code(text) is None


def test_pairing_codes_print_in_three_groups():
    assert format_pairing_code("ABCDEFGHJKMN") == "ABCD-EFGH-JKMN"


def test_secret_hash_is_sha256_hex_of_the_utf8_text():
    assert secret_sha256("fixture-token") == hashlib.sha256(b"fixture-token").hexdigest()


def test_reserved_names_match_the_board():
    from pseudolife_memory.storage.coordination import DAEMON_PRINCIPAL
    assert principals.DAEMON_PRINCIPAL == DAEMON_PRINCIPAL
    # "maintainer" is held for a future maintainer-passkey identity.
    assert principals.RESERVED_PRINCIPALS == {DEFAULT_PRINCIPAL, DAEMON_PRINCIPAL, "maintainer"}


def test_a_stored_row_with_a_reserved_name_is_ignored():
    snap = _snapshot([_row("maintainer", TOKEN_A)])
    assert snap.lookup(secret_sha256(TOKEN_A)) is None and not snap.has("maintainer")


# -- the snapshot -------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from pseudolife_memory.principal_store import PrincipalSnapshot, StoredPrincipal  # noqa: E402
from pseudolife_memory.principals import (  # noqa: E402
    PrincipalsUnavailable, install_store, installed_store, is_stored_principal,
    principal_admitted, resolve_principal, stored_tier,
)

TOKEN_A = "fixture-stored-token-alpha"
TOKEN_B = "fixture-stored-token-beta"
ENV_TOKEN = "fixture-env-token"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _row(name, token=None, *, tier=None, board=True, revoked=False):
    return StoredPrincipal(principal=name, token_hash=secret_sha256(token) if token else None,
                           tier=tier, board=board, revoked=revoked)


def _snapshot(rows=(), *, clock=None, shadowed=()):
    snap = PrincipalSnapshot(shadowed=shadowed, clock=clock or _Clock())
    snap.refresh(lambda: (list(rows), None))
    return snap


def _bearer(token):
    return f"Bearer {token}"


@pytest.fixture
def no_installed_store():
    previous = installed_store()
    install_store(None)
    yield
    install_store(previous)


def test_a_stored_bearer_resolves_after_the_environment():
    snap = _snapshot([_row("laptop", TOKEN_A)])
    assert resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap) == "laptop"
    assert resolve_principal(_bearer(ENV_TOKEN), {ENV_TOKEN: "desk"}, None, snap) == "desk"


def test_the_environment_wins_over_a_stored_row_with_the_same_token():
    snap = _snapshot([_row("laptop", ENV_TOKEN)])
    assert resolve_principal(_bearer(ENV_TOKEN), {ENV_TOKEN: "desk"}, None, snap) == "desk"
    assert resolve_principal(_bearer(ENV_TOKEN), {}, ENV_TOKEN, snap) == DEFAULT_PRINCIPAL


def test_stored_principals_do_not_turn_authentication_on():
    """An open daemon names everyone default, whatever the table holds."""
    snap = _snapshot([_row("laptop", TOKEN_A)])
    assert resolve_principal(_bearer(TOKEN_A), {}, None, snap) == DEFAULT_PRINCIPAL
    assert resolve_principal(None, {}, None, snap) == DEFAULT_PRINCIPAL


def test_an_unknown_bearer_is_none_and_costs_only_a_lookup():
    calls = []

    class Spy:
        def available(self):
            calls.append("available")
            return True

        def lookup(self, token_hash):
            calls.append("lookup")
            return None

    assert resolve_principal(_bearer("fixture-random"), {ENV_TOKEN: "desk"}, None, Spy()) is None
    assert calls and set(calls) <= {"available", "lookup"}


def test_revoked_and_unpaired_rows_never_authenticate():
    snap = _snapshot([_row("gone", TOKEN_A, revoked=True), _row("pending")])
    assert resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap) is None


def test_a_never_loaded_snapshot_is_unavailable_for_a_non_environment_bearer():
    snap = PrincipalSnapshot(clock=_Clock())
    with pytest.raises(PrincipalsUnavailable):
        resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap)
    # The environment still answers, and a request with no bearer is just
    # unauthorized.
    assert resolve_principal(_bearer(ENV_TOKEN), {ENV_TOKEN: "desk"}, None, snap) == "desk"
    assert resolve_principal(None, {ENV_TOKEN: "desk"}, None, snap) is None
    assert resolve_principal("Basic eA==", {ENV_TOKEN: "desk"}, None, snap) is None


def test_a_snapshot_older_than_sixty_seconds_is_unavailable():
    clock = _Clock()
    snap = _snapshot([_row("laptop", TOKEN_A)], clock=clock)
    clock.now += 59.0
    assert resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap) == "laptop"
    clock.now += 2.0

    def fail():
        raise OSError("database down")

    with pytest.raises(OSError):
        snap.refresh(fail)
    with pytest.raises(PrincipalsUnavailable):
        resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap)
    snap.refresh(lambda: ([_row("laptop", TOKEN_A)], None))
    assert resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap) == "laptop"


def test_an_immediate_add_survives_a_refresh_that_started_before_it():
    snap = _snapshot([])
    added = _row("laptop", TOKEN_A)

    def slow_read():
        # The redemption commits and adds while this read is in flight:
        # the read's rows do not include it.
        snap.add(added)
        return [], None

    snap.refresh(slow_read)
    assert snap.lookup(secret_sha256(TOKEN_A)) == added
    # A refresh that started after the add is authoritative.
    snap.refresh(lambda: ([], None))
    assert snap.lookup(secret_sha256(TOKEN_A)) is None


def test_an_immediate_add_replaces_the_principals_previous_hash():
    snap = _snapshot([_row("laptop", TOKEN_A)])
    snap.add(_row("laptop", TOKEN_B))
    assert snap.lookup(secret_sha256(TOKEN_A)) is None
    assert snap.lookup(secret_sha256(TOKEN_B)).principal == "laptop"


def test_a_revocation_lands_at_the_next_refresh():
    snap = _snapshot([_row("laptop", TOKEN_A)])
    snap.refresh(lambda: ([_row("laptop", TOKEN_A, revoked=True)], None))
    assert snap.lookup(secret_sha256(TOKEN_A)) is None
    assert resolve_principal(_bearer(TOKEN_A), {ENV_TOKEN: "desk"}, None, snap) is None


def test_rows_shadowed_by_the_environment_are_ignored():
    snap = _snapshot([_row("desk", TOKEN_A), _row("default", TOKEN_B), _row("laptop")],
                     shadowed={"desk", "default", "daemon"})
    assert snap.lookup(secret_sha256(TOKEN_A)) is None
    assert snap.lookup(secret_sha256(TOKEN_B)) is None
    assert not snap.has("desk") and snap.has("laptop")
    assert snap.shadowed_rows == ["default", "desk"]


def test_the_bank_fingerprint_is_loaded_with_the_rows():
    snap = PrincipalSnapshot(clock=_Clock())
    snap.refresh(lambda: ([], "0123456789abcdef"))
    assert snap.bank == "0123456789abcdef"


def test_tiers_outside_the_ladder_are_ignored():
    snap = _snapshot([_row("laptop", TOKEN_A, tier="core"), _row("odd", TOKEN_B, tier="admin")])
    assert snap.tier_of("laptop") == "core"
    assert snap.tier_of("odd") is None


# -- admission and the helpers the gates call ---------------------------------

def _cfg(*names):
    return SimpleNamespace(allowed_principals=list(names))


def test_admission_lists_then_stored_board_rows():
    snap = _snapshot([_row("laptop", TOKEN_A), _row("quiet", TOKEN_B, board=False),
                      _row("gone", "fixture-gone", revoked=True)])
    cfg = _cfg("default", "listed")
    assert principal_admitted(cfg, "listed", store=snap)
    assert principal_admitted(cfg, "laptop", store=snap)
    assert not principal_admitted(cfg, "quiet", store=snap)
    assert not principal_admitted(cfg, "gone", store=snap)
    assert not principal_admitted(cfg, "stranger", store=snap)


def test_the_daemon_principal_is_never_admitted():
    snap = _snapshot([_row("daemon", TOKEN_A)])
    assert not principal_admitted(_cfg("daemon"), "daemon", store=snap)


def test_admission_reads_the_installed_store(no_installed_store):
    cfg = _cfg("default")
    assert not principal_admitted(cfg, "laptop")
    install_store(_snapshot([_row("laptop", TOKEN_A)]))
    assert principal_admitted(cfg, "laptop")
    assert is_stored_principal("laptop") and not is_stored_principal("default")
    assert stored_tier("laptop") is None


def test_is_stored_principal_without_a_store(no_installed_store):
    assert not is_stored_principal("laptop")
    assert stored_tier("laptop") is None


# -- every site goes through the one resolver and the one admission check -------

def _package_sources():
    from pathlib import Path
    root = Path(principals.__file__).parent
    for path in sorted(root.rglob("*.py")):
        yield path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")


def test_no_admission_check_bypasses_principal_admitted():
    """Spec 2026-10-02: ``principal_admitted`` replaced every inline
    ``principal not in cfg.allowed_principals``. A membership test against
    the list anywhere else would admit by the list alone and miss invited
    principals (or, the other way round, skip the daemon refusal)."""
    import ast
    found = []
    for name, text in _package_sources():
        if name == "principals.py":
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Compare):
                continue
            for op, right in zip(node.ops, node.comparators):
                # `x in (cfg.allowed_principals or [])` counts too.
                target = right.values[0] if isinstance(right, ast.BoolOp) else right
                if (isinstance(op, (ast.In, ast.NotIn)) and isinstance(target, (ast.Attribute, ast.Name))
                        and (getattr(target, "attr", None) or getattr(target, "id", None))
                        == "allowed_principals"):
                    found.append(f"{name}:{node.lineno}")
    # Not admission: the daemon's startup warning about a principal *named*
    # daemon, and invite's note that a new name is already listed.
    found = [site for site in found if not site.startswith(("daemon.py:", "invite_cli.py:"))]
    assert found == []


def test_every_bearer_resolution_consults_the_installed_store():
    import re
    calls = [(name, m.group(0)) for name, text in _package_sources() if name != "principals.py"
             for m in re.finditer(r"resolve_principal\((?:[^()]|\([^()]*\))*\)", text)]
    assert calls, "the resolver should have callers"
    assert [c for c in calls if "installed_store()" not in c[1]] == []
