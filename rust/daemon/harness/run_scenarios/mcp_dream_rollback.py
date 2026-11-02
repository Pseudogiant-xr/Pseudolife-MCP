"""Portable rollback refusals, journal audit and unchanged cursor.

The six w2e dream-rollback scenarios cover reversal through real writes and
extractor runs. This fixture needs no model and replays the journal boundary.
"""

import json

from mcp_wire import Session, sse_data


def register(harness):
    class McpDreamRollback(harness.Scenario):
        name = "mcp-dream-rollback"
        env = {"PSEUDOLIFE_MCP_TOKEN": harness.T_DEFAULT}
        golden_replay = True
        config_yaml = "memory:\n  dream:\n    enabled: false\n"

        def prepare_home(self, home):
            self.ci_fixture = True
            return super().prepare_home(home)

        def prepare_template(self, dsn):
            import psycopg
            from pseudolife_memory.storage.postgres import PostgresStorage
            PostgresStorage(dsn).close()
            with psycopg.connect(dsn) as conn:
                harness.assert_disposable(conn)
                conn.execute("INSERT INTO meta (key,value) VALUES ('cortex_dream_cursor','42') "
                             "ON CONFLICT (key) DO UPDATE SET value=excluded.value")
                conn.execute("INSERT INTO dream_runs (id,started_at,finished_at,cursor_before,cursor_after,status) "
                             "VALUES (1,10,20,0,42,'committed'), (2,30,NULL,42,NULL,'failed')")
                conn.execute("INSERT INTO dream_run_slots (run_id,seq,entity,attribute,entity_norm,attribute_norm,"
                             "kind,new_value,action,at) VALUES (1,1,'Probe','value','probe','value',"
                             "'scalar','retained','skipped',15)")

        def normalize_db(self, state, before, side):
            return harness.dbstate.normalize(state, {
                **harness.DB_NONDETERMINISTIC,
                ("dream_runs", "rolled_back_at"): "clock: rollback audit timestamp",
            }, before)

        def timeline(self, procs, holders):
            import psycopg
            sessions = {side: Session(proc.port, harness.T_DEFAULT) for side, proc in procs.items()}
            try:
                for label, args in (("newer failed run refuses", {}),
                                    ("stale run id refuses", {"run_id": 99}),
                                    ("large run id refuses", {"run_id": 2**63}),
                                    ("rollback committed journal", {"run_id": 1}),
                                    ("already rolled back refuses", {})):
                    answers = {}
                    for side, session in sessions.items():
                        if label == "rollback committed journal":
                            with psycopg.connect(procs[side].env["PSEUDOLIFE_MCP_DATABASE_URL"]) as conn:
                                harness.assert_disposable(conn)
                                conn.execute("UPDATE dream_runs SET status='rolled_back' WHERE id=2")
                        raw = session.tool("memory_dream", {"action": "rollback", **args})
                        result = json.loads(sse_data(raw)[0])["result"]
                        answers[side] = {"status": raw["status"], "headers": {}, "json": result}
                    row = harness.case(label, "POST", "/mcp")
                    row["_answers"] = (answers.get("python"), answers["rust"])
                    yield row
                answers = {}
                for side, proc in procs.items():
                    with psycopg.connect(proc.env["PSEUDOLIFE_MCP_DATABASE_URL"]) as conn:
                        harness.assert_disposable(conn)
                        cursor = conn.execute("SELECT value FROM meta WHERE key='cortex_dream_cursor'").fetchone()[0]
                        journal = conn.execute("SELECT seq,action FROM dream_run_slots ORDER BY seq").fetchall()
                        audit = conn.execute("SELECT id,status,rolled_back_at IS NOT NULL FROM dream_runs ORDER BY id").fetchall()
                    answers[side] = {"status": 200, "headers": {}, "json": {"cursor": cursor, "journal": journal, "audit": audit}}
                row = harness.case("cursor and journal retained; run audit updated", "POST", "/mcp")
                row["_answers"] = (answers.get("python"), answers["rust"])
                yield row
            finally:
                for session in sessions.values():
                    session.close()

    return [McpDreamRollback]
