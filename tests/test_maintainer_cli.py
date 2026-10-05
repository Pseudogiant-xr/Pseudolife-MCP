"""``pseudolife-mcp maintainer``: the host side of the maintainer's passkeys
(schema v54; spec 2026-10-02-maintainer-wake-design.md, "Enrolment").

These reach the bank directly with the database owner's credentials, never
a bearer: what keeps an agent holding only a bearer from enrolling,
confirming or revoking a key.
"""
from __future__ import annotations

import io

import pytest

from pseudolife_memory import maintainer_cli
from pseudolife_memory.storage.maintainer import (
    BOOTSTRAP_CODE_LENGTH, BOOTSTRAP_MAX_FAILURES, code_hash,
)
from tests.maintainer_authenticator import SoftAuthenticator
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture
def bank(pg_url, pg_conn, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    pg_conn.autocommit = True
    return pg_conn


def run(*argv):
    out = io.StringIO()
    code = maintainer_cli.main(list(argv), out=out)
    return code, out.getvalue()


def _insert(conn, auth, state, active_from=None, label="laptop"):
    conn.execute("INSERT INTO maintainer_passkeys (credential_id,public_key,alg,label,"
                 "enrolled_by,state,active_from,created_at) VALUES (%s,%s,-7,%s,'bootstrap',"
                 "%s,%s,0)", (auth.id, auth.cose_key(), label, state, active_from))


def test_enrol_code_prints_a_one_time_code_stored_hashed(bank):
    code, out = run("enrol-code", "--no-wait")
    assert code == 0
    printed = out.split("One-time enrolment code: ", 1)[1].split()[0]
    assert len(printed) == BOOTSTRAP_CODE_LENGTH
    rows = bank.execute("SELECT code_hash,expires_at FROM maintainer_bootstrap").fetchall()
    assert [r[0] for r in rows] == [code_hash(printed)]
    assert printed not in str(rows)


def test_enrol_code_is_refused_while_a_key_is_pending_or_active(bank, capsys):
    _insert(bank, SoftAuthenticator(), "pending")
    code, _ = run("enrol-code", "--no-wait")
    assert code == 1 and "already pending or active" in capsys.readouterr().err


def test_enrol_code_waits_and_names_the_redeeming_key(bank, monkeypatch):
    auth = SoftAuthenticator()
    real = maintainer_cli._enrol_code

    def redeem_then_wait(store, args, out):
        original = store.bootstrap_code

        def code_then_redeem():
            value = original()
            _insert(bank, auth, "pending", label="phone")
            bank.execute("UPDATE maintainer_bootstrap SET used_at=1,credential_id=%s",
                         (auth.id,))
            return value
        store.bootstrap_code = code_then_redeem
        return real(store, args, out)

    monkeypatch.setattr(maintainer_cli, "_enrol_code", redeem_then_wait)
    code, out = run("enrol-code", "--poll", "0.01")
    assert code == 0
    assert f"Enrolled (pending): {auth.id[:12]}  label: phone" in out
    assert f"pseudolife-mcp maintainer confirm {auth.id[:12]}" in out


def test_enrol_code_stops_waiting_when_wrong_guesses_burn_the_code(bank, monkeypatch):
    real = maintainer_cli._enrol_code

    def burn_then_wait(store, args, out):
        original = store.bootstrap_code

        def code_then_burn():
            value = original()
            bank.execute("UPDATE maintainer_bootstrap SET failed_attempts=%s",
                         (BOOTSTRAP_MAX_FAILURES,))
            return value
        store.bootstrap_code = code_then_burn
        return real(store, args, out)

    monkeypatch.setattr(maintainer_cli, "_enrol_code", burn_then_wait)
    code, out = run("enrol-code", "--poll", "0.01")
    assert code == 1 and "burned after too many wrong guesses" in out


def test_confirm_activates_only_a_pending_key(bank, capsys):
    auth = SoftAuthenticator()
    _insert(bank, auth, "pending")
    code, out = run("confirm", auth.id[:8])
    assert code == 0 and "Active" in out
    state, active_from = bank.execute("SELECT state,active_from FROM maintainer_passkeys"
                                      ).fetchone()
    assert state == "active" and active_from is not None
    code, _ = run("confirm", auth.id[:8])   # no longer pending
    assert code == 1 and "no single passkey" in capsys.readouterr().err


def test_an_id_that_starts_with_a_dash_is_a_prefix_not_an_option(bank):
    """base64url ids start with '-' one time in 64: typed as `list` printed
    them, they must still name the key."""
    auth = SoftAuthenticator()
    auth.credential_id = b"\xf8" + auth.credential_id[1:]
    assert auth.id.startswith("-")
    _insert(bank, auth, "pending")
    code, out = run("confirm", auth.id[:8])
    assert code == 0 and "Active" in out
    code, out = run("revoke", auth.id[:8])
    assert code == 0


def test_revoke_revokes_any_key_however_old(bank):
    old, new = SoftAuthenticator(), SoftAuthenticator()
    _insert(bank, old, "active", 0)
    _insert(bank, new, "active", 0, label="phone")
    code, out = run("revoke", old.id[:10])
    assert code == 0 and "Revoked" in out
    states = dict(bank.execute("SELECT credential_id,state FROM maintainer_passkeys").fetchall())
    assert states == {old.id: "revoked", new.id: "active"}
    revoked_by = bank.execute("SELECT revoked_by FROM maintainer_passkeys WHERE "
                              "credential_id=%s", (old.id,)).fetchone()[0]
    assert revoked_by == "host"


@pytest.mark.parametrize("prefix", ["abc", "a%b_cd", "abc def"])
def test_a_short_or_odd_prefix_is_refused(bank, prefix):
    assert run("revoke", prefix)[0] == 1


def test_reset_needs_yes_then_revokes_all_and_rotates_the_secret(bank):
    a, b = SoftAuthenticator(), SoftAuthenticator()
    _insert(bank, a, "active", 0)
    _insert(bank, b, "pending")
    bank.execute("INSERT INTO meta (key,value) VALUES ('maintainer_secret_v1',%s)",
                 ('"' + "0" * 64 + '"',))
    assert run("reset")[0] == 1
    code, out = run("reset", "--yes")
    assert code == 0 and "Revoked 2" in out
    assert {r[0] for r in bank.execute("SELECT state FROM maintainer_passkeys")} == {"revoked"}
    secret = bank.execute("SELECT value FROM meta WHERE key='maintainer_secret_v1'").fetchone()[0]
    assert secret != "0" * 64
    assert run("enrol-code", "--no-wait")[0] == 0   # bootstrap reopened


def test_list_prints_the_keys_and_flags(bank):
    auth = SoftAuthenticator()
    _insert(bank, auth, "active", 0)
    bank.execute("UPDATE maintainer_passkeys SET flagged_at=1")
    code, out = run("list")
    assert code == 0 and auth.id[:12] in out and "FLAGGED" in out


def test_no_bank_is_an_error_without_a_dsn(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    # Never a real daemon container on a test host (tests/test_daemon_exec.py).
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_EXEC", "1")
    assert run("list")[0] == 2
    assert "no bank found" in capsys.readouterr().err


def test_the_console_script_dispatches_the_mode(monkeypatch):
    from pseudolife_memory import cli
    seen = []
    monkeypatch.setattr(maintainer_cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "maintainer", "list"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and seen == [["list"]]
