"""Model-free pruning scenarios, added to the existing daemon harness."""
from __future__ import annotations

import copy
import time

import psycopg
from psycopg import sql

import dbstate
import pgdisposable as pg
from background_seed import seed_bank, seed_pruning_refusal
from rust.harness_support.bank_fixture import assert_fixture_database


PRUNED_TABLES = ("dream_runs", "dream_run_slots", "retrieval_events", "lesson_search_events")


def register(harness):
    class SweepPruning(harness.Scenario):
        name = "sweep-pruning"
        settle = False
        snapshot_settle = False  # model-free timeline observes pruning directly in storage
        start_stages = ("principals",)  # model-free: the sweep runs whether or not init finishes
        native_validation = True
        golden_replay = True
        golden_db_state = True
        config_yaml = ("memory:\n  dream:\n    enabled: false\n    sweep_interval_seconds: 0.2\n"
                       "    runs_keep: 2\n  compaction:\n    enabled: false\n"
                       "  retrieval_log:\n    enabled: true\n    retention_days: 1\n")
        env = {"PSEUDOLIFE_SESSION_REAP_SECONDS": "3600", "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "3600"}

        def prepare_template(self, dsn):
            seed_bank(dsn, "pruning")
            self.before = self.snapshot_db(dsn)

        def snapshot_db(self, dsn):
            state = super().snapshot_db(dsn)
            if pg.base_database():
                read_prunable_rows(dsn, state)
            return state

        def validate_snapshot(self, state, before):
            # SweepRecovery replaces self.before after removing its injected
            # trigger, so both scenarios compare the final catalog as seeded.
            validate_pruning(state, self.before)

        def timeline(self, procs, holders):
            arms = {}
            for side, daemon in procs.items():
                deadline = time.monotonic() + 120
                while True:
                    state = self.snapshot_db(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
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
                clean = harness.scrub_declared_rows(state, self.before)["state"]
                self.validate_snapshot(clean, self.before)
                normalized = self.normalize_db(clean, self.before, side)
                arms[side] = {"status": 200, "headers": {}, "json": normalized}
            c = harness.case("bank after dreams-off pruning and journal cascade", "OBSERVE", "/background/sweep-state")
            c["_answers"] = (None, arms["rust"])
            return [c]

        def normalize_db(self, state, before, side):
            # Template relation seeds have fresh wall clocks on each replay;
            # no maintenance operation changes those clocks. All other
            # fields and every affected table/sequence remain exact.
            relation_table = "public.relations"
            columns = state["rows"][relation_table]["columns"]
            clock = columns.index("created_at")
            prior = {row[0]: row for row in dbstate.prior_rows(
                before, relation_table, state["rows"][relation_table]["rows"])}
            for row in state["rows"][relation_table]["rows"]:
                if row[0] in prior and row[clock] != prior[row[0]][clock]:
                    raise RuntimeError("sweep changed a seeded relation clock")
            normalized = dbstate.normalize(state, harness.DB_NONDETERMINISTIC, before)
            # Only relation seeds have deliberately portable clocks, checked
            # against this run's baseline above. Retrieval seeds stay exact.
            normalized = dbstate.normalize(normalized, {
                ("relations", "created_at"): harness.DB_NONDETERMINISTIC[("relations", "created_at")]})
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
            seed_pruning_refusal(dsn)
            self.before = self.snapshot_db(dsn)

        def timeline(self, procs, holders):
            rows, arms = [], {}
            for side, daemon in procs.items():
                deadline = time.monotonic() + 60
                while "dream sweep error:" not in daemon.log.read_text(errors="replace"):
                    if time.monotonic() > deadline: raise RuntimeError(f"{side} did not report the injected pruning refusal")
                    time.sleep(0.1)
                state = self.snapshot_db(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
                state = harness.scrub_declared_rows(state, self.before)["state"]
                validate_pruning(state, self.before, pruned=False)
                clean = self.normalize_db(state, self.before, side)
                arms[side] = {"status": 200, "headers": {}, "json": clean}
            c = harness.case("bank after refused pruning transaction", "OBSERVE", "/background/sweep-state")
            c["_answers"] = (None, arms["rust"])
            rows.append(c)
            for daemon in procs.values():
                with psycopg.connect(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"]) as conn:
                    assert_fixture_database(conn)
                    conn.execute("DROP TRIGGER pl_w3i_prune_failure ON public.dream_runs")
                    conn.execute("DROP FUNCTION pl_w3i_prune_failure()")
            self.before = copy.deepcopy(self.before)
            self.before["catalog"]["functions"] = [r for r in self.before["catalog"]["functions"] if r[1] != "pl_w3i_prune_failure"]
            self.before["catalog"]["triggers"] = [r for r in self.before["catalog"]["triggers"] if r[2] != "pl_w3i_prune_failure"]
            rows += super().timeline(procs, holders)
            return rows

    return {s.name: s for s in (SweepPruning, SweepRecovery)}


def read_prunable_rows(dsn, state):
    """Read the copied rows the sweep may remove, including unchanged base rows."""
    with psycopg.connect(dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        assert_fixture_database(conn)
        for name in PRUNED_TABLES:
            table = state["rows"]["public." + name]
            ident = sql.Identifier("public", name)
            columns = sql.SQL(", ").join(map(sql.Identifier, table["columns"]))
            table["rows"] = [list(row) for row in conn.execute(
                sql.SQL("SELECT {} FROM {} ORDER BY id::text").format(columns, ident))]
            table.pop("base_missing", None)


def validate_pruning(state, before, *, pruned=True):
    """Expect only the known writer-open and pruning effects on the seeded bank."""
    expected = copy.deepcopy(before)
    rows = expected["rows"]
    for key_value in rows["public.meta"]["rows"]:
        if key_value[0] == "writer_lease_epoch":
            key_value[1] += 1
    if pruned:
        cutoff = time.time() - 86400.0
        runs = rows["public.dream_runs"]
        rid, status, started = map(runs["columns"].index, ("id", "status", "started_at"))
        retained = set(sorted(row[rid] for row in runs["rows"])[-2:])
        runs["rows"] = [row for row in runs["rows"] if row[rid] in retained]
        for row in runs["rows"]:
            if row[status] == "running" and row[started] < cutoff:
                row[status] = "failed"
        slots = rows["public.dream_run_slots"]
        parent = slots["columns"].index("run_id")
        slots["rows"] = [row for row in slots["rows"] if row[parent] in retained]
        for name in ("retrieval_events", "lesson_search_events"):
            events = rows["public." + name]
            stamp = events["columns"].index("created_at")
            events["rows"] = [row for row in events["rows"] if row[stamp] >= cutoff]
    if differences := dbstate.diff(expected, state):
        raise RuntimeError(f"pruning bank differs from the fixture contract: {differences[:8]}")
