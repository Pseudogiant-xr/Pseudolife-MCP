"""Abandon a native store, signal shutdown while it is pending, then restart."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import signal
import time
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"

import psycopg

import w2e as h
from rust.harness_support.bank_fixture import assert_fixture_database
from w2e_shutdown_seed import replay


SCENARIO = {"config": "memory:\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n"}
TEXT = ("A disconnected store must finish before daemon shutdown. " * 160).strip()
DRAIN_WITNESS = "shutdown: admitted writes still running; the final save runs beside them"
# background.rs gives the HTTP and write drain a combined six seconds and
# keeps the final save available through nine seconds. Hold across the
# former deadline so the daemon reports whether it was tracking the write.
HOLD_AFTER_SIGNAL_SECONDS = 6.5
STORE_PAUSE_SQL = """
CREATE FUNCTION public.w2e_pause_store() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- A try-lock does not run into the daemon writer's lock_timeout.
    WHILE NOT pg_try_advisory_xact_lock(730, 650) LOOP
        PERFORM pg_sleep(0.05);
    END LOOP;
    RETURN NEW;
END
$$;
CREATE TRIGGER w2e_pause_store BEFORE INSERT ON public.entries
FOR EACH ROW EXECUTE FUNCTION public.w2e_pause_store();
"""


def prepare_template(owned):
    name = h.pg.name("pl_cf_w2e_shutdown_t")
    owned.append(name)
    replay(h.pg.create(name), "standalone-shutdown")
    return name


def store_rows(state, text):
    rows = state["rows"]["public.entries"]
    text_column = rows["columns"].index("text")
    return [dict(zip(rows["columns"], row)) for row in rows["rows"]
            if row[text_column] == text]


def durable_diffs(before, stopped, restarted, text, window):
    """The store adds one valid row and one sequence value; restart preserves them."""
    diffs = []
    written = store_rows(stopped, text)
    if len(written) != 1:
        diffs.append("admitted store did not survive shutdown exactly once")
    else:
        row, = written
        stable = {"band": "flat", "source": "fixture", "dream_state": "pending",
                  "episode_id": None, "access_count": 0, "reinforcements": 0,
                  "explicit_reinforcements": 0, "superseded_at": None,
                  "superseded_by_text": None, "tags": [], "slots": []}
        if any(row.get(key) != expected for key, expected in stable.items()):
            diffs.append("shutdown store has incorrect durable attributes")
        if not isinstance(row.get("ts"), float) or not window[0] <= row["ts"] <= window[1]:
            diffs.append("shutdown store clock is outside its invocation window")
        embedding = json.loads(row["embedding"]) if row.get("embedding") else []
        if (len(embedding) != 1024 or not all(math.isfinite(value) for value in embedding)
                or not any(embedding)):
            diffs.append("shutdown store has an invalid durable embedding")

    expected_stop = copy.deepcopy(before)
    entries = expected_stop["rows"]["public.entries"]
    if len(written) == 1:
        entries["rows"].append([written[0][key] for key in entries["columns"]])
        id_column = entries["columns"].index("id")
        entries["rows"].sort(key=lambda row: str(row[id_column]))
    seq = next(row for row in expected_stop["catalog"]["sequences"]
               if row[:2] == ["public", "entries_id_seq"])
    seq[5] = seq[3] if seq[5] is None else seq[5] + seq[4]
    if len(written) == 1 and written[0]["id"] != seq[5]:
        diffs.append("shutdown store used an unexpected entry sequence value")
    diffs.extend(h.dbstate.diff(expected_stop, stopped))

    expected_restart = copy.deepcopy(stopped)
    for item in expected_restart["rows"]["public.meta"]["rows"]:
        if item[0] == "writer_lease_epoch":
            item[1] += 1
    diffs.extend(f"after restart: {diff}" for diff in h.dbstate.diff(expected_restart, restarted))
    return diffs


def run_arm(binary, root, template, label, mutant, owned):
    name = h.pg.name(f"pl_cf_w2e_shutdown_{label}")
    owned.append(name)
    dsn = h.pg.create(name, template=template)
    with psycopg.connect(dsn) as conn:
        assert_fixture_database(conn)
        conn.execute(STORE_PAUSE_SQL)
    home = h.daemons.make_home(root, label, SCENARIO["config"])
    env = h.w1a.rust_env(h.side_env(SCENARIO, dsn))
    if mutant:
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    daemon = h.daemons.rust_daemon(binary, home, h.daemons.free_port(), h.daemons.base_env(home, env))
    try:
        daemon.start(300)
        h.w1a.wait_settled([daemon.port], token=h.TOKEN)
        mcp = h.Mcp(daemon.port)
        mcp.init()
        before = h.dbstate.dump(dsn)
        began = time.time()
        with psycopg.connect(dsn, autocommit=True) as gate:
            gate.execute("SELECT pg_advisory_lock(730, 650)")
            mcp.abandon("memory_store", {"text": TEXT, "source": "fixture"})
            deadline = time.monotonic() + 30
            while not gate.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event = 'PgSleep' "
                "AND query LIKE '%INSERT INTO entries%')"
            ).fetchone()[0]:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{label}: the abandoned store never reached the fixture trigger")
                time.sleep(0.05)
            proc = daemon.proc
            if proc is None or proc.poll() is not None:
                raise RuntimeError(f"{label}: owned daemon exited before the shutdown signal")
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            time.sleep(HOLD_AFTER_SIGNAL_SECONDS)
        exit_code = proc.wait(timeout=120)
        drain_observed = DRAIN_WITNESS in daemon.log.read_text(encoding="utf-8", errors="replace")
        daemon.stop()
        stopped = h.dbstate.dump(dsn)
        ended = time.time()
        daemon.start(300)
        h.w1a.wait_settled([daemon.port], token=h.TOKEN)
        restarted = h.dbstate.dump(dsn)
        evidence = {"pending_at_signal": True, "exit": exit_code, "tracked_drain": drain_observed,
                    "persisted": len(store_rows(stopped, TEXT)),
                    "after_restart": len(store_rows(restarted, TEXT))}
        differences = durable_diffs(before, stopped, restarted, TEXT, (began, ended))
        if exit_code:
            differences.append(f"{label}: owned daemon exited {exit_code}")
        if not drain_observed:
            differences.append(f"{label}: blocked store did not appear in the admitted-write drain")
        return evidence, differences
    finally:
        daemon.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-bin", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mutant")
    args = parser.parse_args()
    if _DATABASE_TURN_MARKER.exists():
        from rust.harness_support.database_turn import require_turn

        require_turn(holder="standalone database harness", contain=True)
    args.rust_bin = args.rust_bin.resolve()
    root = h.run_root("w2e-shutdown")
    arms, owned = {}, []
    try:
        template = prepare_template(owned)
        arms["rust"], differences = run_arm(args.rust_bin, root, template, "rust", None, owned)
        mutant_differences = []
        if args.mutant and not differences:
            arms["mutant"], mutant_differences = run_arm(
                args.rust_bin, root, template, "mutant", args.mutant, owned)
        result = {"arms": arms, "differences": differences,
                  "mutant_differences": mutant_differences, "stats": {}}
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps({"arms": arms, "difference_count": len(differences),
                          "mutant_difference_count": len(mutant_differences), "mutant": args.mutant}))
        if differences:
            return 1
        if args.mutant:
            caught = arms["mutant"]["exit"] == 0 and not arms["mutant"]["tracked_drain"]
            print(f"mutant {args.mutant}: {'caught' if caught else 'MISSED'}")
            return int(not caught)
        return 0
    finally:
        for name in reversed(owned):
            h.pg.drop(name)


if __name__ == "__main__":
    raise SystemExit(main())
