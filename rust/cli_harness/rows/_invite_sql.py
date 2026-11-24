"""The frozen invite command's container SQL: what the native client must send
to psql in the Postgres container when the daemon container is stopped,
and the bank-id meta key the invite row's seeds write.

Copied verbatim, by syntax tree, from pseudolife_memory/invite_cli.py at 6ca817d70:
POSTGRES_CONTAINER :80-80, _PSQL :83-84, _NOW_SQL :85-85, _OWNED_SQL :87-87,
LIST_SQL :90-105, ACTIVITY_SQL :108-108, LIST_SQL_V53 :109-114.
Below them, values computed by calling the frozen code once, named where they are defined.
The source is frozen, so the copy cannot drift from it; never edit it by hand.
"""
from __future__ import annotations



POSTGRES_CONTAINER = "pseudolife-mcp-postgres"


_PSQL = ["docker", "exec", "-i", POSTGRES_CONTAINER, "sh", "-c",
         'psql -X -q -tA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"']


_NOW_SQL = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"


_OWNED_SQL = "COALESCE((to_jsonb(p) ->> 'owned')::boolean, TRUE)"


LIST_SQL = (
    "SELECT json_build_object("
    "'principals', (SELECT coalesce(json_agg(json_build_array(l.principal, l.tier, l.board, "
    "l.paired, l.pending, l.code_expires_at, l.created_at, l.paired_at, l.revoked_at, l.now, "
    "l.last_seen_at, l.stored, l.owned) ORDER BY l.principal), '[]'::json) FROM ("
    "SELECT p.principal, p.tier, p.board, p.token_hash IS NOT NULL AS paired, "
    "p.code_hash IS NOT NULL AS pending, p.code_expires_at, p.created_at, p.paired_at, "
    f"p.revoked_at, {_NOW_SQL} AS now, a.last_seen_at, TRUE AS stored, {_OWNED_SQL} AS owned "
    "FROM public.principals p "
    "LEFT JOIN public.principal_activity a ON a.principal = p.principal UNION ALL "
    "SELECT a.principal, NULL, NULL, FALSE, FALSE, NULL, NULL, NULL, NULL, "
    f"{_NOW_SQL}, a.last_seen_at, FALSE, NULL FROM public.principal_activity a WHERE NOT EXISTS "
    "(SELECT 1 FROM public.principals p WHERE p.principal = a.principal)) l), "
    "'rejected', (SELECT coalesce(json_agg(json_build_array(hash_prefix, rejections, "
    "first_seen_at, last_seen_at) ORDER BY last_seen_at DESC, hash_prefix), '[]'::json) "
    "FROM public.rejected_bearers));")


ACTIVITY_SQL = "SELECT to_regclass('public.principal_activity') IS NOT NULL;"


LIST_SQL_V53 = (
    "SELECT json_build_object("
    "'principals', (SELECT coalesce(json_agg(json_build_array(principal, tier, board, "
    f"token_hash IS NOT NULL, code_hash IS NOT NULL, code_expires_at, created_at, paired_at, "
    f"revoked_at, {_NOW_SQL}, NULL::double precision, TRUE, {_OWNED_SQL}) ORDER BY principal), "
    "'[]'::json) FROM public.principals p), 'rejected', '[]'::json);")


# pseudolife_memory.invite_cli.revoke_sql('laptop') at 6ca817d70
REVOKE_LAPTOP_SQL = "UPDATE public.principals SET revoked_at = COALESCE(revoked_at, EXTRACT(EPOCH FROM clock_timestamp())::double precision), code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL WHERE principal = 'laptop' RETURNING principal;"

# pseudolife_memory.storage.coordination.BANK_ID_META_KEY at 6ca817d70
BANK_ID_META_KEY = 'coordination_bank_id'
