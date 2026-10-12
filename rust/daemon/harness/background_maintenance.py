"""Model-free pruning scenarios, added to the existing daemon harness."""
from __future__ import annotations

import subprocess
import sys
import time
import copy
import psycopg

import daemons
import dbstate


def register(harness):
    class SweepPruning(harness.Scenario):
        name = "sweep-pruning"
        settle = False
        golden_replay = True
        golden_db_state = True
        config_yaml = ("memory:\n  dream:\n    enabled: false\n    sweep_interval_seconds: 0.2\n"
                       "    runs_keep: 2\n  compaction:\n    enabled: false\n"
                       "  retrieval_log:\n    enabled: true\n    retention_days: 1\n")
        env = {"PSEUDOLIFE_SESSION_REAP_SECONDS": "3600", "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "3600"}

        def prepare_template(self, dsn):
            home = daemons.make_home(daemons.scratch_root(), self.name + "-seed", self.config_yaml)
            code = '''
import os
from pseudolife_memory.storage.postgres import PostgresStorage
s = PostgresStorage(os.environ["PSEUDOLIFE_MCP_DATABASE_URL"])
for n in range(4):
    run = s.start_dream_run(100.0 + n, 0.0, 0, extractor="fixture")
    if n == 3: s.finish_dream_run(run, status="committed", finished_at=200.0, cursor_after=0.0, claims=0, tallies={})
    s.add_dream_run_slot(run, dict(seq=1,entity="fixture",attribute="state",entity_norm="fixture",attribute_norm="state",kind="scalar",new_value="ready",action="noop",at=100.0))
for stamp in [100.0, 9000000000000.0]:
    event = s.add_retrieval_event("fixture", [], now=stamp)
    s.add_lesson_search_event("fixture", [], now=stamp)
s.close()
'''
            log = home / "seed.log"
            env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
            with log.open("wb") as out:
                result = subprocess.run([sys.executable, "-c", code], cwd=daemons.REPO,
                    env=dict(env, PYTHONPATH=str(daemons.REPO)), stdout=out, stderr=subprocess.STDOUT, timeout=60)
            if result.returncode: raise RuntimeError(f"sweep seed exit {result.returncode}; log {log}")
            self.before = dbstate.dump(dsn)

        def timeline(self, procs, holders):
            arms = {}
            for side, daemon in procs.items():
                deadline = time.monotonic() + 120
                while True:
                    state = dbstate.dump(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
                    runs = state["rows"].get("public.dream_runs", {}).get("rows", [])
                    events = state["rows"].get("public.retrieval_events", {}).get("rows", [])
                    lessons = state["rows"].get("public.lesson_search_events", {}).get("rows", [])
                    run_columns = state["rows"].get("public.dream_runs", {}).get("columns", [])
                    event_columns = state["rows"].get("public.retrieval_events", {}).get("columns", [])
                    lesson_columns = state["rows"].get("public.lesson_search_events", {}).get("columns", [])
                    if len(runs) == 2 and all(r[run_columns.index("status")] != "running" for r in runs):
                        if not any(r[event_columns.index("created_at")] == 100.0 for r in events) and not any(r[lesson_columns.index("created_at")] == 100.0 for r in lessons): break
                    if time.monotonic() > deadline: raise RuntimeError(f"{side} sweep milestone not reached; log {daemon.log}")
                    time.sleep(0.1)
                normalized = self.normalize_db(harness.scrub_declared_rows(state, self.before)["state"], self.before, side)
                arms[side] = {"status": 200, "headers": {}, "json": normalized}
            c = harness.case("bank after dreams-off pruning and journal cascade", "OBSERVE", "/background/sweep-state")
            c["_answers"] = (arms.get("python"), arms["rust"])
            return [c]

        def normalize_db(self, state, before, side):
            # Template relation seeds have fresh wall clocks on each replay;
            # no maintenance operation changes those clocks. All other
            # fields and every affected table/sequence remain exact.
            relation_table = "public.relations"
            columns = state["rows"][relation_table]["columns"]
            clock = columns.index("created_at")
            prior = {row[0]: row for row in before["rows"][relation_table]["rows"]}
            for row in state["rows"][relation_table]["rows"]:
                if row[0] in prior and row[clock] != prior[row[0]][clock]:
                    raise RuntimeError("sweep changed a seeded relation clock")
            normalized = dbstate.normalize(state, harness.DB_NONDETERMINISTIC)
            # Catalog version/owner metadata varies between the bench and
            # hosted disposable PostgreSQL. Prove it stayed byte-equal to
            # this arm's seeded baseline before emitting a portable marker.
            # Sequence state remains exact and is compared across arms.
            for key, value in state["catalog"].items():
                if key != "sequences":
                    if value != before["catalog"][key]:
                        raise RuntimeError(f"sweep changed catalog {key}")
                    normalized["catalog"][key] = "<unchanged seeded catalog>"
            return normalized

    class SweepRecovery(SweepPruning):
        name = "sweep-recovery"
        golden_replay = False
        golden_db_state = False
        config_yaml = SweepPruning.config_yaml + "embedding:\n  model_name: fixture/unavailable\n"

        def normalize_db(self, state, before, side):
            return super().normalize_db(state, self.before, side)

        def prepare_template(self, dsn):
            super().prepare_template(dsn)
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute("CREATE FUNCTION pl_w3i_prune_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture pruning refusal'; END $$")
                conn.execute("CREATE TRIGGER pl_w3i_prune_failure BEFORE DELETE ON dream_runs FOR EACH STATEMENT EXECUTE FUNCTION pl_w3i_prune_failure()")
            self.before = dbstate.dump(dsn)

        def timeline(self, procs, holders):
            rows, arms = [], {}
            for side, daemon in procs.items():
                deadline = time.monotonic() + 60
                while "dream sweep error:" not in daemon.log.read_text(errors="replace"):
                    if time.monotonic() > deadline: raise RuntimeError(f"{side} did not report the injected pruning refusal")
                    time.sleep(0.1)
                state = dbstate.dump(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
                for table in ("dream_runs", "dream_run_slots"):
                    if state["rows"]["public." + table] != self.before["rows"]["public." + table]:
                        raise RuntimeError("failed pruning transaction changed durable rows")
                clean = self.normalize_db(harness.scrub_declared_rows(state, self.before)["state"], self.before, side)
                arms[side] = {"status": 200, "headers": {}, "json": clean}
            c = harness.case("bank after refused pruning transaction", "OBSERVE", "/background/sweep-state")
            c["_answers"] = (arms["python"], arms["rust"])
            rows.append(c)
            for daemon in procs.values():
                with psycopg.connect(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"], autocommit=True) as conn:
                    conn.execute("DROP TRIGGER pl_w3i_prune_failure ON dream_runs")
                    conn.execute("DROP FUNCTION pl_w3i_prune_failure()")
            self.before = copy.deepcopy(self.before)
            self.before["catalog"]["functions"] = [r for r in self.before["catalog"]["functions"] if r[1] != "pl_w3i_prune_failure"]
            self.before["catalog"]["triggers"] = [r for r in self.before["catalog"]["triggers"] if r[2] != "pl_w3i_prune_failure"]
            rows += super().timeline(procs, holders)
            return rows

    return {s.name: s for s in (SweepPruning, SweepRecovery)}
