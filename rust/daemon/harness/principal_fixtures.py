"""Lane-local SQL fixtures, not implementations of principal-store operations.

Call only before a daemon starts on its disposable template.
"""
import hashlib

import psycopg

from rust.harness_support.bank_fixture import assert_fixture_database, initialize_bank, require_fixture_database
from rust.harness_support.database_guard import dsn_database_name, refuse_production_database


def check_target(dsn):
    name = dsn_database_name(dsn)
    if name is None:
        raise ValueError("fixture DSN must name an explicit disposable database")
    refuse_production_database(name)
    require_fixture_database(name)


def prepare_bank(dsn):
    check_target(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        initialize_bank(conn)


def seed_principal(conn, name, *, token=None, tier=None, ttl=3600, revoked=False):
    """Write one fresh pending, paired or revoked fixture row, never upsert."""
    assert_fixture_database(conn)
    code = hashlib.sha256(("principal-fixture:" + name).encode()).hexdigest()[:12].upper()
    digest = hashlib.sha256(code.encode()).hexdigest()
    token_hash = hashlib.sha256(token.encode()).hexdigest() if token is not None else None
    conn.execute("INSERT INTO public.principals "
                 "(principal,tier,board,code_hash,code_expires_at,created_at,token_hash,"
                 "paired_code_hash,paired_at,revoked_at) VALUES "
                 "(%s,%s,TRUE,%s,CASE WHEN %s THEN EXTRACT(EPOCH FROM clock_timestamp())::float8+%s END,"
                 "EXTRACT(EPOCH FROM clock_timestamp())::float8,%s,%s,"
                 "CASE WHEN %s THEN EXTRACT(EPOCH FROM clock_timestamp())::float8 END,"
                 "CASE WHEN %s THEN EXTRACT(EPOCH FROM clock_timestamp())::float8 END)",
                 (name, tier, digest if token is None and not revoked else None,
                  token is None and not revoked, ttl, token_hash,
                  digest if token is not None and not revoked else None, token is not None, revoked))
    return code


def env_auth(environ):
    """Read only the harness's explicit synthetic environment, never os.environ."""
    mapped = {}
    for entry in (environ.get("PSEUDOLIFE_MCP_TOKENS") or "").split(","):
        token, separator, principal = entry.strip().rpartition(":")
        token, principal = token.strip(), principal.strip().lower()
        if separator and token and principal and principal not in {"default", "maintainer"}:
            mapped.setdefault(token, principal)
    return mapped, environ.get("PSEUDOLIFE_MCP_TOKEN") or None


def environment_principal(header, mapped, singular):
    """Classify readiness fixtures; a non-match may require the stored view."""
    if not mapped and singular is None:
        return "default"
    scheme, _, token = (header or "").partition(" ")
    token = token.strip(" \t")
    if scheme.lower() != "bearer" or not token:
        return None
    candidates = [token.encode()]
    try:
        candidates.append(token.encode("latin-1"))
    except UnicodeEncodeError:
        pass
    for expected, name in list(mapped.items()) + ([(singular, "default")] if singular is not None else []):
        if expected.encode() in candidates:
            return name
    return None
