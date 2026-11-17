"""Lane-local SQL fixtures, not implementations of principal-store operations.

Call only before a daemon starts on its disposable template. The relation
and writer-epoch fixture also exists in the dream lane until consolidation.
"""
import hashlib
import time

import psycopg

from rust.harness_support.database_guard import (
    assert_disposable_database, dsn_database_name, refuse_production_database,
)
from rust.harness_support.schema import ensure_schema


# Constructor fixture at 7f2e6eacf; the one-time proof compares full catalogs
# and rows, including the writer epoch, for every consuming template.
RELATIONS = (
    ("depends-on", "src requires dst to function", True, None),
    ("part-of", "src is a component of dst", True, None),
    ("hosts", "src is the host/platform for dst", False, None),
    ("runs-on", "src executes on host/platform dst", False, "hosts"),
    ("uses", "src makes use of dst", False, None),
    ("configures", "src sets configuration for dst", False, None),
    ("stores-data-in", "src persists its data in dst", False, None),
    ("implements", "src (a source file) realizes concept/role dst", False, None),
    ("related-to", "untyped catch-all association", False, None),
    ("prefers", "src (a task-type) prefers approach/tool dst (positive lesson)", False, None),
    ("avoids", "src (a task-type) should avoid dead-end dst (negative lesson)", False, None),
)


def check_target(dsn):
    name = dsn_database_name(dsn)
    if name is None:
        raise ValueError("fixture DSN must name an explicit disposable database")
    refuse_production_database(name)


def prepare_bank(dsn):
    check_target(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        ensure_schema(conn)  # Includes the same-connection server guard.
        for name, description, transitive, inverse in RELATIONS:
            conn.execute("INSERT INTO public.relations "
                         "(name,description,transitive,inverse_of,builtin,created_at) "
                         "VALUES (%s,%s,%s,%s,TRUE,%s) ON CONFLICT (name) DO NOTHING",
                         (name, description, transitive, inverse, time.time()))
        conn.execute("INSERT INTO public.meta (key,value) VALUES ('writer_lease_epoch','1'::jsonb) "
                     "ON CONFLICT (key) DO UPDATE SET value = to_jsonb(COALESCE("
                     "CASE WHEN jsonb_typeof(meta.value) = 'number' "
                     "THEN (meta.value #>> '{}')::numeric::bigint END, 0) + 1)")


def seed_principal(conn, name, *, token=None, tier=None, ttl=3600, revoked=False):
    """Write one fresh pending, paired or revoked fixture row, never upsert."""
    assert_disposable_database(conn)
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
