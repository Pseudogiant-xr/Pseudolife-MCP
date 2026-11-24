"""The frozen test-login command's statement builders and login-file header.

The ``test-login`` row seeds the role and login file a previous run left
(``test_login._seed_login``) with the frozen command's own statements, so
both arms start from exactly what that command wrote. Copied verbatim from
pseudolife_memory/test_login_cli.py at 6ca817d70 by
verify/w107-transcribe-login-seed.py (EXTENSIONS :95-95, _FILE_HEADER :105-110, scram_verifier :216-229, _literal :232-233, _ident :236-237, _names :240-241, role_statements :331-375, extension_statements :378-388, _extension_versions :391-393); the
package is frozen, so the copy cannot drift from its source. Recopy, never
edit by hand.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


EXTENSIONS = ("vector",)


_FILE_HEADER = """\
# Pseudolife-MCP test login, written by `pseudolife-mcp test-login create`.
# The PostgreSQL role the test suite logs in as: it creates and drops its own
# databases and cannot connect to the bank. Not the bank owner's password.
# Rotate with `pseudolife-mcp test-login create --rotate`.
"""


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = 4096) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (the
    ``PQencryptPasswordConn`` form, RFC 7677). ``ALTER ROLE ... PASSWORD`` takes
    it as is. The password is ASCII, which SASLprep leaves unchanged."""
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _names(values) -> str:
    return ", ".join(_literal(value) for value in values) or "NULL"


def role_statements(role: str, verifier: str, banks: list[str],
                    leftovers: list[str] = ()) -> list[str]:
    """Create or reset the role with the verifier as its password, drop every
    role membership it holds, hand it the leftover run databases (so the
    suite's prune can drop them), and close each bank to PUBLIC and to it.
    One transaction: a statement that fails part way leaves the role's old
    password, which the file being replaced still holds."""
    attributes = ("LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                  f"INHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity' PASSWORD {_literal(verifier)}")
    statements = [
        "BEGIN",
        f"""DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(role)}) THEN
    ALTER ROLE {_ident(role)} WITH {attributes};
  ELSE
    CREATE ROLE {_ident(role)} WITH {attributes};
  END IF;
END $do$""",
        f"""DO $do$ DECLARE grant_row record; BEGIN
  FOR grant_row IN
    SELECT g.rolname AS granted, gr.rolname AS grantor
    FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
    JOIN pg_roles gr ON gr.oid = m.grantor
    WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = {_literal(role)})
  LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I', grant_row.granted,
                   {_literal(role)}, grant_row.grantor);
  END LOOP;
END $do$""",
    ]
    for name in leftovers:  # a pruner may drop one meanwhile
        statements.append(f"""DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = {_literal(name)}) THEN
    ALTER DATABASE {_ident(name)} OWNER TO {_ident(role)};
  END IF;
END $do$""")
    for bank in banks:
        statements.append(f"REVOKE CONNECT ON DATABASE {_ident(bank)} FROM PUBLIC")
        statements.append(f"REVOKE ALL ON DATABASE {_ident(bank)} FROM {_ident(role)}")
    # A session in template1 fails every CREATE DATABASE that copies it, the
    # owner's after a restore's DROP included; copying needs no CONNECT.
    statements.append(f"REVOKE CONNECT ON DATABASE {_ident('template1')} FROM PUBLIC")
    statements.append("SELECT 'ok'")
    statements.append("COMMIT")
    return statements


def extension_statements() -> list[str]:
    """For one connection to template1, which it should hold briefly: a
    CREATE DATABASE fails while any other session is connected to its
    template. The last statement answers ``<before>|<after>``."""
    statements = [f"SELECT set_config('pseudolife.extensions_before', ({_extension_versions()}), false)"]
    for name in EXTENSIONS:
        statements += [f"CREATE EXTENSION IF NOT EXISTS {_ident(name)}",
                       f"ALTER EXTENSION {_ident(name)} UPDATE"]
    statements.append(f"SELECT current_setting('pseudolife.extensions_before') || '|' || "
                      f"({_extension_versions()})")
    return statements


def _extension_versions() -> str:
    return ("SELECT coalesce(string_agg(extname || ' ' || extversion, ', ' ORDER BY extname), '') "
            f"FROM pg_extension WHERE extname IN ({_names(EXTENSIONS)})")
