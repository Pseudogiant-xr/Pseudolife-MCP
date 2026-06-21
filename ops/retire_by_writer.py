"""Retire (mark superseded) the current canonical rows written by a given
writer — and optionally a single session.

Use when a writer polluted the bank: a dogfooding agent that went off the rails,
a misconfigured session, a bad import run. Target by ``writer_id`` (and
optionally ``session_id``) and supersede its ``current`` rows across the
stamped stores in one shot. The v0.4 writer keying makes this precise.

Dry-run by default (prints what it would touch); ``--apply`` makes the change.
BACKUP FIRST (ops/backup.ps1). Run with the daemon STOPPED (or restart it
after): it edits Postgres directly, and the daemon re-hydrates its in-memory
store from Postgres on start — a running daemon would re-snapshot over this.
"""

from __future__ import annotations

import os
import sys
import time

# Stores that carry the writer/session stamp (schema v11).
_TABLES = ("facts", "world_facts", "lessons")


def run(conn, writer_id: str, session_id: str | None = None,
        apply: bool = False) -> dict:
    """Plan (and with ``apply=True`` execute) the retirement on an open psycopg
    connection. Returns the plan dict. Dry-run mutates nothing."""
    conn.execute("SET search_path TO public, ag_catalog")
    where = "writer_id = %s AND status = 'current'"
    params: list = [writer_id]
    if session_id:
        where += " AND session_id = %s"
        params.append(session_id)

    plan: dict = {"apply": apply, "writer_id": writer_id,
                  "session_id": session_id, "counts": {}}
    for t in _TABLES:
        n = conn.execute(
            f"SELECT count(*) FROM public.{t} WHERE {where}", params
        ).fetchone()[0]
        plan["counts"][t] = n
    total = sum(plan["counts"].values())
    plan["total"] = total

    target = f"writer_id={writer_id!r}"
    if session_id:
        target += f", session_id={session_id!r}"
    print(f"=== retire_by_writer: {target} ===")
    print("!! BACKUP FIRST (ops/backup.ps1); run with the daemon stopped !!")
    for t, n in plan["counts"].items():
        print(f"  {t}: {n} current row(s) would be superseded")
    if not apply:
        print(f"(dry-run — {total} row(s) match; re-run with --apply to retire)")
        return plan

    now = time.time()
    retired = 0
    for t in _TABLES:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE public.{t} SET status = 'superseded', superseded_at = %s "
                f"WHERE {where}",
                [now, *params],
            )
            retired += cur.rowcount
    conn.commit()
    plan["retired"] = retired
    print(f"retired {retired} row(s).")
    return plan


def main() -> None:
    import argparse

    import psycopg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("writer_id", help="the writer_id to retire")
    ap.add_argument("--session-id", default=None,
                    help="restrict to a single session_id")
    ap.add_argument("--apply", action="store_true",
                    help="execute (default: dry-run)")
    ap.add_argument("--database-url",
                    default=os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL"),
                    help="Postgres DSN (or set PSEUDOLIFE_MCP_DATABASE_URL)")
    args = ap.parse_args()
    if not args.database_url:
        print("error: provide --database-url or set PSEUDOLIFE_MCP_DATABASE_URL",
              file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(args.database_url) as conn:
        run(conn, args.writer_id, session_id=args.session_id, apply=args.apply)


if __name__ == "__main__":
    main()
