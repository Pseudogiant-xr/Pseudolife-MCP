"""MCP explain projection with the offline embedding fixture.

Real-model ranking, cortex projection and retrieval-event writes are also
compared by w2d_search.py's mcp-search-explain scenario. This small bank
pins the complete structured trace for portable golden replay.
"""

import json

from mcp_wire import Session, sse_data


def register(harness):
    class McpSearchExplain(harness.Scenario):
        name = "mcp-search-explain"
        env = {"PSEUDOLIFE_MCP_TOKEN": harness.T_DEFAULT}
        golden_replay = True
        config_yaml = """memory:
  recency_boost_enabled: false
  search_confidence_floor: 0.0
  search:
    min_score: 0.0
  bm25:
    enabled: false
"""

        def prepare_template(self, dsn):
            import psycopg
            from pseudolife_memory.storage.postgres import PostgresStorage
            PostgresStorage(dsn).close()
            with psycopg.connect(dsn, autocommit=True) as conn:
                harness.assert_disposable(conn)
                for index, direction in enumerate(((0.8, 0.6), (1.0, 0.0), (0.8, -0.6))):
                    vector = json.dumps([*direction] + [0.0] * 1022)
                    conn.execute("INSERT INTO entries (band, text, embedding, ts, source, tags) "
                                 "VALUES ('flat', %s, %s::vector, 1700000000, 'agent', %s)",
                                 (f"memory {index}", vector, json.dumps(["probe"])))

        def prepare_home(self, home):
            # This scenario always uses the fixture, including default replay.
            self.ci_fixture = True
            return super().prepare_home(home)

        def normalize_db(self, state, before, side):
            clocks = {**harness.DB_NONDETERMINISTIC,
                      ("retrieval_events", "created_at"): "clock: per retrieval event"}
            return harness.dbstate.normalize(state, clocks, before, windows=self.clock_window(side))

        def timeline(self, procs, holders):
            sessions = {side: Session(proc.port, harness.T_DEFAULT) for side, proc in procs.items()}
            plain_ranking = {}
            try:
                cases = [
                    ("compact before explain", {"query": "memory"}),
                    ("explain implies verbose", {"query": "memory", "explain": True}),
                    ("search floor is not trace floor", {"query": "memory", "explain": True, "min_score": 1.0}),
                    ("normalized tags", {"query": "memory", "explain": True, "tags": ["PROBE", " probe "]}),
                    ("caller episode order", {"query": "memory", "explain": True, "episodes": ["z", "a", "z"]}),
                    ("empty lists", {"query": "memory", "explain": True, "bands": [], "sources": [], "tags": [], "episodes": []}),
                    ("blank explain", {"query": "   ", "explain": True}),
                    ("invalid band", {"query": "memory", "explain": True, "bands": ["absent"]}),
                    ("subsequent access counts", {"query": "memory", "verbose": True}),
                ]
                for name, args in cases:
                    answers = {}
                    for side, session in sessions.items():
                        raw = session.tool("memory_search", args)
                        result = json.loads(sse_data(raw)[0])["result"]
                        payload = result.get("structuredContent")
                        if name in ("compact before explain", "explain implies verbose"):
                            ranking = [(entry["id"], entry["score"]) for entry in payload.get("entries", [])]
                            if name == "compact before explain":
                                assert ranking, f"{side}: fixture search must return entries"
                                plain_ranking[side] = ranking
                            elif not result.get("isError"):
                                assert ranking == plain_ranking[side], f"{side}: explain changed ids, scores or order"
                        answers[side] = {"status": 422 if result.get("isError") else raw["status"],
                                         "headers": {"content-type": "mcp"},
                                         "json": payload}
                    c = harness.case(name, "POST", "/mcp", body=args)
                    c["_answers"] = (answers.get("python"), answers["rust"])
                    yield c
            finally:
                for session in sessions.values():
                    session.close()

    return [McpSearchExplain]
