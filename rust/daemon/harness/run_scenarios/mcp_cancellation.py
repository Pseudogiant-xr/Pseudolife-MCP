"""Cancel a request while its shielded store is blocked on a database lock.

The lock and pg_stat_activity provide an observable in-flight boundary:
the notification is sent only after the store reached the locked table.
No sleep duration is used as evidence that the request started.
"""

import concurrent.futures
import json
import time

from mcp_wire import Session, request, sse_data


def register(harness):
    class McpCancellation(harness.Scenario):
        name = "mcp-cancellation"
        env = {"PSEUDOLIFE_MCP_TOKEN": harness.T_DEFAULT}
        golden_replay = True
        config_yaml = "memory:\n  dream:\n    enabled: false\n"

        def prepare_home(self, home):
            self.ci_fixture = True
            return super().prepare_home(home)

        def timeline(self, procs, holders):
            import psycopg
            answers, committed = {}, {}
            for side, proc in procs.items():
                session = Session(proc.port, harness.T_DEFAULT)
                dsn = proc.env["PSEUDOLIFE_MCP_DATABASE_URL"]
                try:
                    with psycopg.connect(dsn, autocommit=True) as probe, psycopg.connect(dsn) as lock:
                        harness.assert_disposable(probe)
                        harness.assert_disposable(lock)
                        lock.execute("LOCK TABLE entries IN ACCESS EXCLUSIVE MODE")
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            future = pool.submit(session.tool, "memory_store", {"text": "memory cancellation probe"})
                            try:
                                deadline = time.monotonic() + 30
                                while not probe.execute("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                                         "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                                                         "AND query ILIKE '%INSERT INTO entries%' "
                                                         "AND pid <> pg_backend_pid())").fetchone()[0]:
                                    if future.done():
                                        raise AssertionError(f"{side}: store completed before the lock witness")
                                    if time.monotonic() >= deadline:
                                        raise TimeoutError(f"{side}: store never reached the locked table")
                                    time.sleep(0.01)
                                cancelled = request(proc.port, "POST", {
                                    "jsonrpc": "2.0", "method": "notifications/cancelled",
                                    "params": {"requestId": 2, "reason": "fixture cancellation"}}, session._headers())
                                assert cancelled["status"] == 202
                            finally:
                                lock.rollback()
                            raw = future.result(timeout=60)
                        payloads = sse_data(raw)
                        assert len(payloads) == 1, f"{side}: expected one transport settlement"
                        answers[side] = {"status": raw["status"], "headers": {}, "json": json.loads(payloads[0])}
                        rows = probe.execute("SELECT text FROM entries WHERE text = %s", ("memory cancellation probe",)).fetchall()
                        committed[side] = {"status": 200, "headers": {}, "json": {"stored": [row[0] for row in rows]}}
                        assert rows == [("memory cancellation probe",)], f"{side}: shielded write was lost"
                finally:
                    session.close()
            for name, values in (("cancelled request settlement", answers), ("shielded store committed", committed)):
                c = harness.case(name, "POST", "/mcp")
                c["_answers"] = (values.get("python"), values["rust"])
                yield c

    return [McpCancellation]
