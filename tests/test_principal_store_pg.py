"""The ``principals`` table against PostgreSQL (schema v53; spec
2026-10-02, Part 2b): invite, redemption, revocation, the snapshot's
loader and the refresh thread.

Redemption is one conditional UPDATE on the database clock, so it is
single use under concurrency; a repeat from the client that minted the
token is answered for ten minutes. Neither a token nor a code is ever
stored in plaintext.
"""
from __future__ import annotations

import threading
import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

psycopg = pytest.importorskip("psycopg")

from pseudolife_memory import principal_store as store  # noqa: E402
from pseudolife_memory.principals import secret_sha256  # noqa: E402

TOKEN_A = "fixture-paired-token-alpha"
TOKEN_B = "fixture-paired-token-beta"


@pytest.fixture
def db(pg_conn, pg_url):
    """An autocommit connection on the freshly reset bank, the mode
    ``invite`` uses: every statement is visible to the redeemer at once."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        yield conn


def _invite(conn, name="laptop", **kw):
    params = {"tier": None, "board": True, "ttl_seconds": 900, "replace": False}
    params.update(kw)
    return store.create_invite(conn, name, **params)


def _row(conn, name):
    return conn.execute("SELECT * FROM principals WHERE principal = %s", (name,)).fetchone()


def _db_now(conn) -> float:
    return conn.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision").fetchone()[0]


def test_the_table_has_the_v53_columns(db):
    cols = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'principals'")}
    assert cols == {"principal", "token_hash", "tier", "board", "code_hash", "code_expires_at",
                    "paired_code_hash", "created_at", "paired_at", "revoked_at"}


def test_an_invite_stores_only_the_codes_hash(db):
    invite = _invite(db, tier="core")
    assert invite["state"] == "new"
    row = _row(db, "laptop")
    assert invite["code"] not in repr(row)
    assert row is not None
    found = db.execute("SELECT code_hash, token_hash, tier, board FROM principals").fetchone()
    assert found == (secret_sha256(invite["code"]), None, "core", True)


def test_redemption_pairs_once_and_never_stores_the_token(db, pg_url):
    invite = _invite(db)
    code_hash = secret_sha256(invite["code"])
    won = store.redeem(pg_url, code_hash, secret_sha256(TOKEN_A))
    assert won is not None and won.principal == "laptop" and won.board
    row = _row(db, "laptop")
    assert TOKEN_A not in repr(row) and invite["code"] not in repr(row)
    assert db.execute("SELECT token_hash, code_hash FROM principals").fetchone() == (
        secret_sha256(TOKEN_A), None)
    # The code is spent: a different token cannot take it.
    assert store.redeem(pg_url, code_hash, secret_sha256(TOKEN_B)) is None


def test_a_retry_with_the_same_token_hash_is_answered_again(db, pg_url):
    invite = _invite(db)
    code_hash = secret_sha256(invite["code"])
    first = store.redeem(pg_url, code_hash, secret_sha256(TOKEN_A))
    again = store.redeem(pg_url, code_hash, secret_sha256(TOKEN_A))
    assert again == first


def test_the_retry_window_closes_after_ten_minutes(db, pg_url):
    invite = _invite(db)
    code_hash = secret_sha256(invite["code"])
    assert store.redeem(pg_url, code_hash, secret_sha256(TOKEN_A)) is not None
    db.execute("UPDATE principals SET paired_at = paired_at - 601")
    db.commit()
    assert store.redeem(pg_url, code_hash, secret_sha256(TOKEN_A)) is None
    # The housekeeping cleared the spent code's hash.
    assert db.execute("SELECT paired_code_hash FROM principals").fetchone() == (None,)


def test_expiry_follows_the_database_clock(db, pg_url, monkeypatch):
    invite = _invite(db)
    # This host's clock is a day ahead: the code is still valid by the
    # database's clock.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 86400)
    assert store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A)) is not None
    monkeypatch.setattr(time, "time", real)

    second = _invite(db, "desk")
    db.execute("UPDATE principals SET code_expires_at = %s WHERE principal = 'desk'",
                    (_db_now(db) - 1,))
    db.commit()
    # This host's clock is a day behind: the code expired by the database's.
    monkeypatch.setattr(time, "time", lambda: real() - 86400)
    assert store.redeem(pg_url, secret_sha256(second["code"]), secret_sha256(TOKEN_B)) is None


def test_the_expiry_is_written_from_the_database_clock(db):
    before = _db_now(db)
    invite = _invite(db, ttl_seconds=120)
    assert before + 119 < invite["expires_at"] < _db_now(db) + 121


def test_two_concurrent_redeemers_cannot_both_win(db, pg_url):
    invite = _invite(db)
    db.commit()
    code_hash = secret_sha256(invite["code"])
    # Hold the row so both redeemers queue on it, then let them race.
    holder = psycopg.connect(pg_url)
    holder.execute("SELECT 1 FROM principals WHERE principal = 'laptop' FOR UPDATE")
    results = {}

    def run(label, token):
        results[label] = store.redeem(pg_url, code_hash, secret_sha256(token))

    threads = [threading.Thread(target=run, args=("a", TOKEN_A)),
               threading.Thread(target=run, args=("b", TOKEN_B))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        waiting = db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query LIKE '%%UPDATE public.principals%%'").fetchone()[0]
        if waiting >= 2:
            break
        time.sleep(0.05)
    assert waiting >= 2, "both redeemers should be queued on the row"
    holder.rollback()
    holder.close()
    for thread in threads:
        thread.join(15)
    winners = [label for label, row in results.items() if row is not None]
    assert len(winners) == 1, results
    token = TOKEN_A if winners == ["a"] else TOKEN_B
    assert db.execute("SELECT token_hash FROM principals").fetchone() == (secret_sha256(token),)


def test_a_revoked_principal_cannot_redeem_and_can_be_invited_again(db, pg_url):
    invite = _invite(db)
    assert store.revoke(db, "laptop")
    assert store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A)) is None
    listed = store.list_principals(db)
    assert [(p["principal"], p["state"]) for p in listed] == [("laptop", "revoked")]

    again = _invite(db)
    assert again["state"] == "revoked"
    assert db.execute("SELECT token_hash, revoked_at FROM principals").fetchone() == (None, None)
    assert store.redeem(pg_url, secret_sha256(again["code"]), secret_sha256(TOKEN_A)) is not None


def test_revoking_a_paired_principal_keeps_its_row_and_clears_the_code(db, pg_url):
    invite = _invite(db)
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    _invite(db, replace=True)  # a pending replacement code
    assert store.revoke(db, "laptop")
    assert db.execute(
        "SELECT code_hash, code_expires_at, revoked_at IS NOT NULL FROM principals").fetchone() == (
            None, None, True)
    assert not store.revoke(db, "nobody")


def test_a_paired_name_needs_replace_and_keeps_its_token_until_redeemed(db, pg_url):
    invite = _invite(db)
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    with pytest.raises(store.InviteRefused, match="--replace"):
        _invite(db)
    replacement = _invite(db, replace=True)
    assert replacement["state"] == "paired"
    assert db.execute("SELECT token_hash FROM principals").fetchone() == (secret_sha256(TOKEN_A),)
    won = store.redeem(pg_url, secret_sha256(replacement["code"]), secret_sha256(TOKEN_B))
    assert won is not None
    assert db.execute("SELECT token_hash FROM principals").fetchone() == (secret_sha256(TOKEN_B),)


def test_a_pending_name_gets_a_fresh_code_and_the_old_one_dies(db, pg_url):
    first = _invite(db)
    second = _invite(db)
    assert second["state"] == "pending"
    assert store.redeem(pg_url, secret_sha256(first["code"]), secret_sha256(TOKEN_A)) is None
    assert store.redeem(pg_url, secret_sha256(second["code"]), secret_sha256(TOKEN_A)) is not None


def test_a_token_hash_already_in_use_is_refused(db, pg_url):
    one, two = _invite(db, "one"), _invite(db, "two")
    assert store.redeem(pg_url, secret_sha256(one["code"]), secret_sha256(TOKEN_A)) is not None
    assert store.redeem(pg_url, secret_sha256(two["code"]), secret_sha256(TOKEN_A)) is None
    # The refusal rolled back: two's code is still pending.
    assert store.redeem(pg_url, secret_sha256(two["code"]), secret_sha256(TOKEN_B)) is not None


def test_the_list_never_shows_a_token_or_a_code(db, pg_url):
    invite = _invite(db, tier="minimal", board=False)
    pending = store.list_principals(db)
    assert pending[0]["state"] == "pending" and pending[0]["tier"] == "minimal"
    assert pending[0]["board"] is False and pending[0]["code_expires_at"] is not None
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    listed = store.list_principals(db)
    text = repr(listed)
    assert invite["code"] not in text and TOKEN_A not in text
    assert secret_sha256(TOKEN_A) not in text and secret_sha256(invite["code"]) not in text
    assert listed[0]["state"] == "paired" and listed[0]["paired_at"] is not None


# -- the snapshot's loader and the refresh thread ------------------------------

def test_the_loader_reads_rows_and_the_bank_fingerprint(db, pg_url):
    from pseudolife_memory.storage.coordination import BANK_ID_META_KEY

    invite = _invite(db, tier="core")
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    db.execute("INSERT INTO meta (key, value) VALUES (%s, to_jsonb(%s::text))",
                    (BANK_ID_META_KEY, "fixture-bank-id"))
    db.commit()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        rows, bank = store.load_rows(conn)
    assert [(r.principal, r.token_hash, r.tier, r.board, r.revoked) for r in rows] == [
        ("laptop", secret_sha256(TOKEN_A), "core", True, False)]
    assert bank == store.bank_fingerprint("fixture-bank-id")


def test_a_missing_table_loads_as_empty(db, pg_url):
    """A daemon upgraded to v53 whose storage has not started yet has no
    table: the view is loaded and empty, and nothing creates it. (The reset
    fixture recreates it for the next test.)"""
    db.execute("DROP TABLE principals")
    db.commit()
    snapshot = store.PrincipalSnapshot()
    refresher = store.PrincipalRefresher(pg_url, snapshot, auth_configured=True)
    assert refresher.refresh_once()
    assert snapshot.available() and len(snapshot) == 0
    assert db.execute("SELECT to_regclass('public.principals')").fetchone() == (None,)


def test_the_refresh_thread_runs_off_the_event_loop_and_sees_a_revocation(db, pg_url):
    import asyncio

    invite = _invite(db)
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    snapshot = store.PrincipalSnapshot()
    refresher = store.PrincipalRefresher(pg_url, snapshot, auth_configured=True, interval=0.05)
    seen = []
    original = refresher._read

    def spy():
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append(threading.current_thread().name)
        return original()

    refresher._read = spy
    thread = refresher.start()
    try:
        deadline = time.monotonic() + 10
        while not snapshot.available() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert snapshot.lookup(secret_sha256(TOKEN_A)) is not None
        store.revoke(db, "laptop")
        while snapshot.lookup(secret_sha256(TOKEN_A)) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert snapshot.lookup(secret_sha256(TOKEN_A)) is None
    finally:
        refresher.stop()
        thread.join(5)
    assert seen and set(seen) == {"pl-principals"}


def test_a_failing_refresh_ages_the_view_to_unavailable(pg_url):
    clock = [1000.0]
    snapshot = store.PrincipalSnapshot(clock=lambda: clock[0])
    refresher = store.PrincipalRefresher(pg_url, snapshot, auth_configured=True)
    assert refresher.refresh_once()
    refresher._dsn = "postgresql://fixture@127.0.0.1:1/none"
    refresher._conn.close()
    clock[0] += 61
    assert not refresher.refresh_once()
    assert not snapshot.available()


def test_the_startup_warnings_name_principals_never_tokens(db, pg_url, caplog):
    invite = _invite(db, "desk")
    store.redeem(pg_url, secret_sha256(invite["code"]), secret_sha256(TOKEN_A))
    _invite(db, "laptop")
    snapshot = store.PrincipalSnapshot(shadowed={"desk"})
    refresher = store.PrincipalRefresher(pg_url, snapshot, auth_configured=False)
    with caplog.at_level("WARNING", logger="pseudolife-mcp.principals"):
        assert refresher.refresh_once()
    text = caplog.text
    assert "no PSEUDOLIFE_MCP_TOKEN" in text and "'desk' is shadowed" in text
    assert TOKEN_A not in text and secret_sha256(TOKEN_A) not in text
