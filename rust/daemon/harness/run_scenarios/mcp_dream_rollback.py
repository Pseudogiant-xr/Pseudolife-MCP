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
        # The seeded committed run's id; the failed run is the next one. On
        # an empty bank 1, as the golden records; on a copy of a real bank
        # (PL_HARNESS_BASE_DATABASE) one past the bank's own runs, so that
        # the seeded runs are the latest and no call reverts a real run.
        first_run = 1

        def prepare_home(self, home):
            self.ci_fixture = True
            return super().prepare_home(home)

        def prepare_template(self, dsn):
            import psycopg
            from pseudolife_memory.storage.postgres import PostgresStorage
            PostgresStorage(dsn).close()
            with psycopg.connect(dsn) as conn:
                harness.assert_disposable(conn)
                self.first_run = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM dream_runs").fetchone()[0]
                conn.execute("INSERT INTO meta (key,value) VALUES ('cortex_dream_cursor','42') "
                             "ON CONFLICT (key) DO UPDATE SET value=excluded.value")
                conn.execute("INSERT INTO dream_runs (id,started_at,finished_at,cursor_before,cursor_after,status) "
                             "VALUES (%s,10,20,0,42,'committed'), (%s,30,NULL,42,NULL,'failed')",
                             (self.first_run, self.first_run + 1))
                conn.execute("INSERT INTO dream_run_slots (run_id,seq,entity,attribute,entity_norm,attribute_norm,"
                             "kind,new_value,action,at) VALUES (%s,1,'Probe','value','probe','value',"
                             "'scalar','retained','skipped',15)", (self.first_run,))

        def calls(self):
            """The rollback calls in order: (label, arguments, SQL each bank
            runs first, or None). The last names the reverted run: with no
            committed run left both daemons answer ``no_committed_run``
            before they read the id, and on a real bank's copy, where the
            bank's own latest committed run comes back, ``stale_run_id``
            instead of reverting it."""
            first = self.first_run
            return [("newer failed run refuses", {}, None),
                    ("stale run id refuses", {"run_id": first + 98}, None),
                    ("large run id refuses", {"run_id": 2**63}, None),
                    ("rollback committed journal", {"run_id": first},
                     ("UPDATE dream_runs SET status='rolled_back' WHERE id=%s", (first + 1,))),
                    ("already rolled back refuses", {"run_id": first}, None)]

        def normalize_db(self, state, before, side):
            state = super().normalize_db(state, before, side)
            # Rollback writes this clock on the existing seeded run, whose
            # rolled_back_at starts NULL. Preserve its null/non-null shape.
            return harness.dbstate.normalize(state, {
                ("dream_runs", "rolled_back_at"): "clock: rollback audit timestamp",
            })

        def timeline(self, procs, holders):
            import psycopg
            sessions = {side: Session(proc.port, harness.T_DEFAULT) for side, proc in procs.items()}
            try:
                for label, args, setup in self.calls():
                    answers = {}
                    for side, session in sessions.items():
                        if setup:
                            with psycopg.connect(procs[side].env["PSEUDOLIFE_MCP_DATABASE_URL"]) as conn:
                                harness.assert_disposable(conn)
                                conn.execute(*setup)
                        raw = session.tool("memory_dream", {"action": "rollback", **args})
                        result = json.loads(sse_data(raw)[0])["result"]
                        answers[side] = {"status": raw["status"], "headers": {}, "json": result}
                    row = harness.case(label, "POST", "/mcp")
                    row["_answers"] = (answers.get("python"), answers["rust"])
                    yield row
                answers = {}
                seeded = [self.first_run, self.first_run + 1]
                for side, proc in procs.items():
                    with psycopg.connect(proc.env["PSEUDOLIFE_MCP_DATABASE_URL"]) as conn:
                        harness.assert_disposable(conn)
                        cursor = conn.execute("SELECT value FROM meta WHERE key='cortex_dream_cursor'").fetchone()[0]
                        journal = conn.execute("SELECT seq,action FROM dream_run_slots WHERE run_id = %s "
                                               "ORDER BY seq", (self.first_run,)).fetchall()
                        audit = conn.execute("SELECT id,status,rolled_back_at IS NOT NULL FROM dream_runs "
                                             "WHERE id = ANY(%s) ORDER BY id", (seeded,)).fetchall()
                    answers[side] = {"status": 200, "headers": {}, "json": {"cursor": cursor, "journal": journal, "audit": audit}}
                row = harness.case("cursor and journal retained; run audit updated", "POST", "/mcp")
                row["_answers"] = (answers.get("python"), answers["rust"])
                yield row
            finally:
                for session in sessions.values():
                    session.close()

    return [McpDreamRollback]
