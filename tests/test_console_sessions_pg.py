"""Console sign-in codes and sessions against PostgreSQL (schema v59; spec
2026-11-02, sections 3 and 4): a code is single use, bound to its origin
and to its issuer, and stores only its hash; a session stores only the
browser token's hash; revoking or re-pairing a principal ends its sessions
in the same transaction; and the refresher loads them and writes their
last use.
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

psycopg = pytest.importorskip("psycopg")

from pseudolife_memory import console_sessions as cs  # noqa: E402
from pseudolife_memory import principal_store as store  # noqa: E402
from pseudolife_memory.principals import secret_sha256  # noqa: E402

ORIGIN = "http://127.0.0.1:8765"
ISSUER = secret_sha256("fixture-env-token")
BROWSER = secret_sha256("fixture-browser-token")


@pytest.fixture
def db(pg_conn, pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        yield conn


def _valid(*_):
    return True


def _issue(pg_url, principal="default", source="environment", issuer=ISSUER, origin=ORIGIN):
    return cs.issue_code(pg_url, principal=principal, source=source, issuer=issuer,
                         origin=origin)


def _redeem(pg_url, code, token_hash=BROWSER, origin=ORIGIN, still_valid=_valid):
    return cs.redeem_code(pg_url, secret_sha256(code), token_hash, origin=origin,
                          label="Firefox on Linux", still_valid=still_valid)


def test_a_code_stores_only_its_hash_and_redeems_once(db, pg_url):
    issued = _issue(pg_url)
    rows = db.execute("SELECT * FROM console_signin_codes").fetchall()
    assert len(rows) == 1 and issued["code"] not in repr(rows)
    session = _redeem(pg_url, issued["code"])
    assert session is not None
    assert (session.principal, session.source, session.issuer, session.method) == (
        "default", "environment", ISSUER, "link")
    assert session.expires_at - session.created_at == pytest.approx(cs.SESSION_MAX_S)
    stored = db.execute("SELECT token_hash FROM console_sessions").fetchall()
    assert stored == [(BROWSER,)]
    assert _redeem(pg_url, issued["code"], token_hash=secret_sha256("another")) is None


def test_a_code_redeems_only_at_its_origin_and_a_miss_does_not_spend_it(db, pg_url):
    issued = _issue(pg_url)
    assert _redeem(pg_url, issued["code"], origin="https://evil.example") is None
    assert _redeem(pg_url, issued["code"]) is not None


def test_an_expired_code_is_refused(db, pg_url):
    issued = _issue(pg_url)
    db.execute("UPDATE console_signin_codes SET expires_at = expires_at - 3600")
    assert _redeem(pg_url, issued["code"]) is None


def test_a_code_whose_issuer_is_gone_is_refused_and_not_spent(db, pg_url):
    issued = _issue(pg_url)
    assert _redeem(pg_url, issued["code"], still_valid=lambda *a: False) is None
    assert db.execute("SELECT used_at FROM console_signin_codes").fetchone() == (None,)


def test_a_browser_token_hash_signs_in_once(db, pg_url):
    assert _redeem(pg_url, _issue(pg_url)["code"]) is not None
    assert _redeem(pg_url, _issue(pg_url)["code"]) is None


def test_listing_and_revoking(db, pg_url):
    one = _redeem(pg_url, _issue(pg_url)["code"])
    two = _redeem(pg_url, _issue(pg_url, principal="laptop", source="store")["code"],
                  token_hash=secret_sha256("other-browser"))
    listed = cs.list_sessions(pg_url)
    assert {r["session_id"] for r in listed} == {one.session_id, two.session_id}
    assert all("token_hash" not in r and "issuer" not in r for r in listed)
    assert [r["session_id"] for r in cs.list_sessions(pg_url, principal="laptop")] == [
        two.session_id]
    assert not cs.revoke_session(pg_url, one.session_id, principal="laptop")
    assert cs.revoke_session(pg_url, one.session_id)
    assert not cs.revoke_session(pg_url, one.session_id)
    assert [r["session_id"] for r in cs.list_sessions(pg_url)] == [two.session_id]


def test_an_idle_session_is_not_listed(db, pg_url):
    session = _redeem(pg_url, _issue(pg_url)["code"])
    db.execute("UPDATE console_sessions SET last_used_at = last_used_at - %s",
               (cs.SESSION_IDLE_S + 60,))
    assert cs.list_sessions(pg_url) == []
    cs.flush_uses(db, {session.session_id: 10_000_000_000.0})
    assert len(cs.list_sessions(pg_url)) == 1


def test_a_use_never_moves_the_last_use_backwards(db, pg_url):
    session = _redeem(pg_url, _issue(pg_url)["code"])
    before = db.execute("SELECT last_used_at FROM console_sessions").fetchone()[0]
    cs.flush_uses(db, {session.session_id: 1.0})
    assert db.execute("SELECT last_used_at FROM console_sessions").fetchone()[0] == before


def test_the_loader_reads_live_sessions_and_active_passkeys(db, pg_url):
    live = _redeem(pg_url, _issue(pg_url)["code"])
    gone = _redeem(pg_url, _issue(pg_url)["code"], token_hash=secret_sha256("gone"))
    cs.revoke_session(pg_url, gone.session_id)
    rows, keys = cs.load_sessions(db)
    assert [r.session_id for r in rows] == [live.session_id] and keys == []


def test_revoking_a_principal_ends_its_sessions(db, pg_url):
    store.create_invite(db, "laptop", tier=None, board=True, ttl_seconds=900, replace=False)
    session = _redeem(pg_url, _issue(pg_url, principal="laptop", source="store")["code"])
    other = _redeem(pg_url, _issue(pg_url)["code"], token_hash=secret_sha256("owner"))
    assert store.revoke(db, "laptop")
    ids = {r.session_id for r in cs.load_sessions(db)[0]}
    assert ids == {other.session_id} and session.session_id not in ids


def test_re_pairing_ends_the_sessions_the_old_token_issued(db, pg_url):
    invite = store.create_invite(db, "laptop", tier=None, board=True, ttl_seconds=900,
                                 replace=False)
    old = secret_sha256("old-paired-token")
    store.redeem(pg_url, secret_sha256(invite["code"]), old)
    by_old = _redeem(pg_url, _issue(pg_url, principal="laptop", source="store",
                                    issuer=old)["code"])
    again = store.create_invite(db, "laptop", tier=None, board=True, ttl_seconds=900,
                                replace=True)
    # Until the new code is redeemed the old token, and its sessions, work.
    assert [r.session_id for r in cs.load_sessions(db)[0]] == [by_old.session_id]
    store.redeem(pg_url, secret_sha256(again["code"]), secret_sha256("new-paired-token"))
    assert cs.load_sessions(db)[0] == []


def test_the_refresher_loads_sessions_and_writes_their_use(db, pg_url):
    session = _redeem(pg_url, _issue(pg_url)["code"])
    principals = store.PrincipalSnapshot()
    sessions = cs.SessionSnapshot()
    refresher = store.PrincipalRefresher(pg_url, principals, auth_configured=True,
                                         sessions=sessions)
    try:
        assert refresher.refresh_once()
        assert sessions.available() and sessions.lookup(BROWSER) is not None
        sessions.note_use(session.session_id, 20_000_000_000.0)
        assert refresher.flush_session_uses(now=1_000.0)
        assert db.execute("SELECT last_used_at FROM console_sessions").fetchone()[0] == (
            20_000_000_000.0)
        # At most once a minute.
        sessions.note_use(session.session_id, 30_000_000_000.0)
        assert not refresher.flush_session_uses(now=1_030.0)
    finally:
        refresher.stop()
        if refresher._conn is not None:
            refresher._conn.close()


def test_spent_codes_are_pruned_after_a_day(db, pg_url):
    _issue(pg_url)
    db.execute("UPDATE console_signin_codes SET expires_at = expires_at - 2 * 86400")
    _issue(pg_url)
    assert db.execute("SELECT count(*) FROM console_signin_codes").fetchone()[0] == 1
