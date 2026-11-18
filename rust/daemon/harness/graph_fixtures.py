"""Fixed graph regression inputs and the nine locale-dependent read cells.

These SQL fixtures describe test data, not a replacement graph implementation.
The schema belongs to the daemon's committed plan.
"""
import copy

import psycopg

from rust.harness_support.bank_fixture import initialize_bank
from rust.harness_support.database_guard import assert_disposable_database, dsn_database_name, refuse_production_database
from rust.harness_support.schema import ensure_schema


def connect(url):
    refuse_production_database(dsn_database_name(url))
    conn = psycopg.connect(url, autocommit=True)
    try:
        assert_disposable_database(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def seed(url):
    with connect(url) as conn, conn.transaction():
        initialize_bank(conn, fresh=True)
        for canonical, display, etype in [("node-a", "Node A", None),
                                          ("node-b", "Node B", "runtime"), ("node-c", "Node C", None)]:
            conn.execute("INSERT INTO entities (canonical, display, etype, created_at) VALUES (%s,%s,%s,1.0)",
                         (canonical, display, etype))
        conn.execute("INSERT INTO entity_aliases (alias, entity_id) VALUES ('first', 1)")
        for eid, source, origin in [(1, "fixture-source", "manual"), (2, "fixture-source", "derived"),
                                    (1, "carried-source", "derived")]:
            conn.execute("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                         "VALUES (%s,%s,1,%s,1.0)", (eid, source, origin))
        for index, (name, value, subject, obj) in enumerate([
            ("Node A", "Node B", 1, 2), ("node_a", "Node C", None, 3),
            ("G:", "capacity", None, None), ("alpha beta", "x", None, None),
            ("ΟΣ", "Greek", None, None), ("100%_done", "percent", None, None),
            ("AA_İ", "tied-segment", None, None), ("prefix_\uA7CB", "unicode-14-a", None, None),
            ("prefix_\u1C89", "unicode-14-b", None, None), ("prefix_\uA7CC", "unicode-14-c", None, None),
        ]):
            conn.execute("INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, value, "
                         "status, polarity, confidence, asserted_at, last_confirmed, entity_id, object_entity_id) "
                         "VALUES (%s,%s,%s,%s,%s,'current','+',0.7,1.0,1.0,%s,%s)",
                         (name, f"attr-{index}", name.lower(), f"attr-{index}", value, subject, obj))
        conn.execute("INSERT INTO lessons (entity, attribute, entity_norm, attribute_norm, value, about, "
                     "outcome, polarity, status, confidence, asserted_at, last_confirmed, entity_id, object_entity_id) "
                     "VALUES ('Node A','approach','node a','approach','keep','Node B',"
                     "'success','+','current',0.7,1.0,1.0,1,2)")
        for index in range(2):
            conn.execute("INSERT INTO entries (band, text, embedding, surprise, ts, access_count, source, superseded_at) "
                         "VALUES ('flat',%s,%s::vector,0.0,1.0,0,'fixture',%s)",
                         (f"fixture support {index}", str([0.0] * 1024), 1.0 if index else None))


class ReadFixture:
    """Independent SQL effects for the read corpus's fresh relations/edges.

Only the final nine cells depend on collation. All earlier reads still use
the committed golden. The separate bank never consumes candidate output.
"""
    def __init__(self, url):
        self.conn = connect(url)
        try:
            # Opening the native store reapplies the schema plan, including
            # its existing-row repairs. Apply those to the independent arm.
            with self.conn.transaction():
                ensure_schema(self.conn)
                self.conn.execute("UPDATE meta SET value = '2'::jsonb WHERE key = 'writer_lease_epoch'")
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def apply(self, request, now):
        if request["op"] == "upsert_relation":
            self.conn.execute("INSERT INTO relations (name, description, transitive, inverse_of, created_at) "
                              "VALUES (%s,%s,%s,%s,%s)",
                              (request["name"], request["description"], request.get("transitive", False),
                               request.get("inverse_of"), now))
            return {"value": None}
        if request["op"] == "upsert_edge":
            row = self.conn.execute("INSERT INTO edges (src_id, relation, dst_id, confidence, origin, asserted_at) "
                                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, confidence",
                                    (request["src_id"], request["relation"], request["dst_id"],
                                     request.get("confidence", 0.8), request.get("origin"), now)).fetchone()
            return {"value": {"id": row[0], "confidence": row[1]}}
        return None

    def subgraph(self, request, recorded):
        # The fixture has two inverse declarations for z. PostgreSQL's
        # ordering selects the first; everything else stays recorded exactly.
        chosen = self.conn.execute("SELECT name FROM relations WHERE inverse_of = 'z' ORDER BY name LIMIT 1").fetchone()[0]
        expected = copy.deepcopy(recorded)
        for edge in expected["value"]["edges"]:
            if edge.get("via") == ["inverse:z"]:
                edge["relation"] = chosen
        return expected


def read_context(url, request):
    with connect(url) as conn:
        edges = [dict(zip(("src", "relation", "dst", "confidence", "origin"), row))
                 for row in conn.execute("SELECT src_id, relation, dst_id, confidence, origin "
                                         "FROM edges WHERE superseded_at IS NULL ORDER BY id")]
        relations = {name: dict(transitive=transitive, inverse_of=inverse)
                     for name, transitive, inverse in conn.execute(
                         "SELECT name, transitive, inverse_of FROM relations ORDER BY name")}
    return {**request, "op": "build_subgraph", "edges": edges, "relations": relations}
