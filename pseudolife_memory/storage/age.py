"""Apache AGE mirror — optional openCypher layer over the graph tables.

The plain tables (entities / edges) remain the source of truth; AGE is a
lazily-mirrored read surface for ``memory_graph_query``. Absence of the
extension only disables this module (capability-gated by
``ensure_schema``'s ``age_available`` flag) — the graph core never
depends on it.

Mirroring is idempotent (MERGE-based) and best-effort: a mirror failure
logs and moves on, because the next ``pseudolife-mcp age-sync`` rebuilds
the whole AGE graph from the tables anyway.

AGE labels only allow ``[A-Za-z0-9_]``, so relation names are mirrored
with ``-`` folded to ``_`` (``depends-on`` → ``depends_on``). Strong-model
tool: weak-model deployments must not expose Cypher (spec §5.3.5).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

GRAPH_NAME = "pseudolife"

# Read-only screen for memory_graph_query: any mutating clause keyword
# (even inside a string literal — a coarse screen is the point) rejects.
_MUTATING_RE = re.compile(
    r"\b(create|merge|set|delete|detach|remove|drop)\b", re.IGNORECASE,
)

_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


def is_mutating(cypher: str) -> str | None:
    """Return the offending keyword when the query could mutate, else None."""
    m = _MUTATING_RE.search(cypher or "")
    return m.group(1).lower() if m else None


def _q(s: str) -> str:
    """Escape a value for embedding in a single-quoted Cypher string."""
    return (s or "").replace("\\", "\\\\").replace("'", "\\'")


def _label(relation: str) -> str:
    return _IDENT_RE.sub("_", relation)


def _return_arity(cypher: str) -> int:
    """Count the columns of the final RETURN clause (top-level commas,
    ignoring nesting and quotes, stopping at ORDER/LIMIT/SKIP). AGE's SQL
    interface needs the arity declared in the ``AS (...)`` clause."""
    matches = list(re.finditer(r"\breturn\b", cypher, re.IGNORECASE))
    if not matches:
        return 1
    tail = cypher[matches[-1].end():]
    depth = 0
    count = 1
    quote: str | None = None
    i = 0
    while i < len(tail):
        ch = tail[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            if ch == ",":
                count += 1
            elif re.match(r"(?i)\b(order|limit|skip)\b", tail[i:]):
                break
        i += 1
    return count


class AgeGraph:
    """Thin mirror over a live psycopg connection (shared with storage —
    every call runs under the service's coarse lock, so no extra conn)."""

    def __init__(self, conn, name: str = GRAPH_NAME) -> None:
        self.conn = conn
        self.name = name
        with conn.cursor() as cur:
            cur.execute("LOAD 'age';")
            # NOTE: do NOT include "$user" here. The DB role is `pseudolife`, which
            # is ALSO this AGE graph's schema name, so "$user" would put the graph
            # schema ahead of public on the shared daemon connection and shadow the
            # real SCHEMA_SQL tables (facts/entries/...) with same-named tables that
            # a later ensure_schema can create inside the graph schema. All AGE ops
            # below are ag_catalog.-qualified, so the graph schema is not needed on
            # the path. (ag_catalog, public) keeps cypher working AND plain tables
            # resolving to the real bank.
            cur.execute("SET search_path = ag_catalog, public;")
            cur.execute(
                "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (name,),
            )
            if cur.fetchone() is None:
                cur.execute("SELECT ag_catalog.create_graph(%s)", (name,))
        conn.commit()

    def _exec(self, cypher: str, arity: int = 1, limit: int | None = None):
        cols = ", ".join(f"c{i} agtype" for i in range(arity))
        sql = (
            f"SELECT * FROM ag_catalog.cypher('{self.name}', "
            f"$age$ {cypher} $age$) AS ({cols})"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql).fetchall()
        self.conn.commit()
        return rows

    # ── mirroring (best-effort; tables are the truth) ───────────────────

    def upsert_entity(self, canonical: str, display: str,
                      etype: str | None = None) -> None:
        props = f"n.display = '{_q(display)}'"
        if etype:
            props += f", n.etype = '{_q(etype)}'"
        self._exec(
            f"MERGE (n:Entity {{canonical: '{_q(canonical)}'}}) SET {props}",
        )

    def upsert_edge(self, src_canonical: str, relation: str,
                    dst_canonical: str) -> None:
        self._exec(
            f"MATCH (a:Entity {{canonical: '{_q(src_canonical)}'}}), "
            f"(b:Entity {{canonical: '{_q(dst_canonical)}'}}) "
            f"MERGE (a)-[:{_label(relation)}]->(b)",
        )

    def remove_edge(self, src_canonical: str, relation: str,
                    dst_canonical: str) -> None:
        self._exec(
            f"MATCH (a:Entity {{canonical: '{_q(src_canonical)}'}})"
            f"-[r:{_label(relation)}]->"
            f"(b:Entity {{canonical: '{_q(dst_canonical)}'}}) DELETE r",
        )

    def resync(self, storage) -> dict:
        """Drop and rebuild the AGE graph from the tables (full re-sync)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT ag_catalog.drop_graph(%s, true)", (self.name,))
            cur.execute("SELECT ag_catalog.create_graph(%s)", (self.name,))
        self.conn.commit()
        g = storage.load_graph()
        by_id = {e["id"]: e for e in g["entities"]}
        for e in g["entities"]:
            self.upsert_entity(e["canonical"], e["display"], e["etype"])
        for edge in g["edges"]:
            self.upsert_edge(
                by_id[edge["src_id"]]["canonical"],
                edge["relation"],
                by_id[edge["dst_id"]]["canonical"],
            )
        return {"entities": len(g["entities"]), "edges": len(g["edges"])}

    # ── read-only Cypher (memory_graph_query) ───────────────────────────

    def cypher(self, query: str, limit: int = 50) -> list[list[str]]:
        query = (query or "").strip().rstrip(";")
        if "$age$" in query:
            raise ValueError("query may not contain the literal '$age$'")
        rows = self._exec(query, arity=_return_arity(query), limit=limit)
        return [[str(v) for v in row] for row in rows]
