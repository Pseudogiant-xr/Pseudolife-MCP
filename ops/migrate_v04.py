"""v0.4 one-time migration — eliminate the role / AGE-schema name collision.

The DB role is ``pseudolife``; the original AGE graph was *also* named
``pseudolife``, so AGE created a schema called ``pseudolife``. With the cluster
default ``search_path`` of ``"$user", public`` (``$user`` → the role
``pseudolife``), unqualified table names could resolve to that graph schema, and
a stray ``ensure_schema`` could create empty ``facts``/``entries``/… *shadow*
tables there that mask the real ``public`` bank. v0.4 closes this at the root:

1. Rebuild the graph under a non-colliding name (``pseudolife_graph``) from the
   truth tables (``public.entities`` / ``public.edges``).
2. Drop the old ``pseudolife`` graph (its schema goes with it).
3. Drop any leftover shadow relational tables in the ``pseudolife`` schema
   (only those that have a real ``public`` sibling — a safety guard).
4. Bump ``meta.schema_version`` to current.

GUARDED + BACKUP-FIRST: dry-run is the default; ``--apply`` makes changes. Run
``ops/backup.ps1`` first. This is a deploy-time step, run with a human — never
part of CI. (CI proves the logic on a seeded throwaway DB.)
"""

from __future__ import annotations

import json
import os
import sys

from pseudolife_memory.storage.age import AgeGraph
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

OLD_GRAPH = "pseudolife"  # == the DB role name == the collision
DEFAULT_NEW_GRAPH = "pseudolife_graph"

# Relational tables that could have been shadow-created in the role-named schema.
_SHADOW_TABLES = (
    "entries", "facts", "world_facts", "lessons", "outcome_signals",
    "relations", "edges", "entities", "entity_aliases", "episodes", "meta",
)


def _graph_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (name,)
    ).fetchone()
    return row is not None


def _regclass(conn, qualified: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (qualified,)).fetchone()[0] is not None


def _rebuild_graph(conn, name: str) -> dict:
    """Drop (if present) and rebuild the named AGE graph from the public truth
    tables — the rename mechanism (rebuild-from-truth, same as ``age-sync``)."""
    conn.execute("LOAD 'age'")
    conn.commit()
    if _graph_exists(conn, name):
        with conn.cursor() as cur:
            cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (name,))
        conn.commit()
    age = AgeGraph(conn, name=name)  # constructor creates the (now-absent) graph
    ents = conn.execute(
        "SELECT id, canonical, display, etype FROM public.entities ORDER BY id"
    ).fetchall()
    by_id = {r[0]: (r[1], r[2], r[3]) for r in ents}
    for canon, disp, etype in by_id.values():
        age.upsert_entity(canon, disp, etype)
    n_edges = 0
    for src, rel, dst in conn.execute(
        "SELECT src_id, relation, dst_id FROM public.edges "
        "WHERE superseded_at IS NULL"
    ).fetchall():
        if src in by_id and dst in by_id:
            age.upsert_edge(by_id[src][0], rel, by_id[dst][0])
            n_edges += 1
    return {"entities": len(by_id), "edges": n_edges}


def run(conn, apply: bool = False, new_graph: str | None = None) -> dict:
    """Plan (and, with ``apply=True``, execute) the collision migration on an
    open psycopg connection. Returns the plan dict. Dry-run mutates nothing."""
    new_graph = new_graph or os.environ.get("PSEUDOLIFE_GRAPH_NAME", DEFAULT_NEW_GRAPH)
    conn.execute("LOAD 'age'")
    conn.execute("SET search_path TO public, ag_catalog")
    conn.commit()

    old_exists = _graph_exists(conn, OLD_GRAPH) and OLD_GRAPH != new_graph
    new_exists = _graph_exists(conn, new_graph)
    shadow = [
        t for t in _SHADOW_TABLES
        if _regclass(conn, f"{OLD_GRAPH}.{t}") and _regclass(conn, f"public.{t}")
    ]

    plan: dict = {
        "apply": apply, "old_graph": OLD_GRAPH, "new_graph": new_graph,
        "old_graph_present": old_exists, "shadow_tables": shadow, "steps": [],
    }
    if old_exists:
        plan["steps"].append(
            f"rebuild graph '{new_graph}' from public truth tables, then "
            f"drop_graph('{OLD_GRAPH}')")
    elif not new_exists:
        plan["steps"].append(f"create graph '{new_graph}' from public truth tables")
    if shadow:
        plan["steps"].append("drop shadow tables: " + ", ".join(shadow))
    plan["steps"].append(f"upsert meta.schema_version = {SCHEMA_META_VERSION}")

    print("=== migrate_v04: role/AGE-schema collision elimination ===")
    print("!! BACKUP FIRST: run ops/backup.ps1 before --apply !!")
    print(f"old graph '{OLD_GRAPH}' present: {old_exists}; "
          f"new graph '{new_graph}' present: {new_exists}")
    print(f"shadow tables to drop: {shadow or 'none'}")
    for s in plan["steps"]:
        print("  -", s)
    if not apply:
        print("(dry-run — no changes made; re-run with --apply to execute)")
        return plan

    # --- execute -----------------------------------------------------------
    rebuilt = _rebuild_graph(conn, new_graph)
    plan["rebuilt"] = rebuilt
    print(f"rebuilt '{new_graph}': {rebuilt}")

    if old_exists:
        with conn.cursor() as cur:
            cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (OLD_GRAPH,))
        conn.commit()
        print(f"dropped old graph '{OLD_GRAPH}'")

    # drop_graph cascades the role-named schema; this catches orphan shadow
    # tables that linger without the graph. Guarded by a real public sibling.
    for t in shadow:
        if _regclass(conn, f"{OLD_GRAPH}.{t}"):
            conn.execute(f'DROP TABLE IF EXISTS "{OLD_GRAPH}".{t} CASCADE')
    conn.commit()
    # Remove the now-empty role-named schema if it lingers (RESTRICT: only if
    # empty — surfaces anything unexpected instead of cascading it away).
    try:
        conn.execute(f'DROP SCHEMA IF EXISTS "{OLD_GRAPH}" RESTRICT')
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"note: left schema '{OLD_GRAPH}' in place ({exc})")

    conn.execute(
        "INSERT INTO public.meta (key, value) VALUES ('schema_version', %s::jsonb) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (json.dumps(SCHEMA_META_VERSION),),
    )
    conn.commit()
    print(f"meta.schema_version -> {SCHEMA_META_VERSION}")
    print("=== migration complete ===")
    return plan


def main() -> None:
    import argparse

    import psycopg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="execute the migration (default: dry-run)")
    ap.add_argument("--database-url",
                    default=os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL"),
                    help="Postgres DSN (or set PSEUDOLIFE_MCP_DATABASE_URL)")
    args = ap.parse_args()
    if not args.database_url:
        print("error: provide --database-url or set PSEUDOLIFE_MCP_DATABASE_URL",
              file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(args.database_url) as conn:
        run(conn, apply=args.apply)


if __name__ == "__main__":
    main()
