"""Background lifecycle scenarios for the existing daemon harness.

All seeds use Python service methods. Each milestone observes real daemon
subprocesses and dumps the whole disposable bank; close clocks are checked
against that arm's own invocation window before normalization.
"""
from __future__ import annotations

import copy
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import daemons
import dbstate
import pgdisposable as pg


def register(harness):
    class SessionReap(harness.Scenario):
        name = "session-reap"
        config_yaml = "memory:\n  dream:\n    enabled: false\n  retrieval_log:\n    enabled: false\n"
        env = {"PSEUDOLIFE_SESSION_IDLE_SECONDS": "0", "PSEUDOLIFE_SESSION_REAP_SECONDS": "0.2",
               "PSEUDOLIFE_SESSION_RESUME_SECONDS": "3600", "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS": "3600"}
        sweep = False
        restart = False

        def configure_daemons(self, procs):
            if "python" not in procs: return
            # The brief explicitly defers dream execution. Instrument its
            # trigger, rather than normalizing away cursor/ack writes.
            code = '''
import logging, signal
from pseudolife_memory import mcp_server
mcp_server.service._fire_and_forget_dream = lambda: logging.getLogger("pseudolife-mcp").warning("session-end dream trigger: stub (W3-H)")
# Controlled normal-return exit: uvicorn rethrows captured signals to the
# previous handler; the fixture supplies a returning handler so atexit runs.
# Ordinary CLI signal exit statuses are separate, uninstrumented behavior.
for sig in (signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGTERM)):
    signal.signal(sig, lambda *_: None)
from pseudolife_memory.daemon import run_daemon
run_daemon()
'''
            procs["python"].argv = [sys.executable, "-c", code]

        def prepare_template(self, dsn):
            root = daemons.scratch_root()
            home = daemons.make_home(root, self.name + "-seed", self.config_yaml)
            code = '''
import os
from unittest.mock import patch
from pseudolife_memory.service import MemoryService
svc = MemoryService(data_dir=os.environ["PSEUDOLIFE_MCP_DATA_DIR"])
svc._ensure_init()
with patch("time.time", return_value=1700000000.0):
    svc.episode_start_session("empty", "empty fixture")
    svc.episode_start_session("work", "work fixture")
    svc.episode_start("child", episode=svc._cms.episodes.open_leaf_for("work").id)
    svc.store("The background lifecycle fixture retains a nested session entry.", source="fixture",
              episode=svc._cms.episodes.open_leaf_for("work").parent_id)
    svc.episode_start_session("history", "closed history fixture")
    svc.store("An explicitly closed session remains history after its entries are deleted.", source="fixture",
              episode=svc._cms.episodes.open_leaf_for("history").id)
    svc.episode_end_session("history", run_dream=False)
    svc.delete(episode=next(e.id for e in svc._cms.episodes.episodes.values() if e.session_key == "history"),
               confirm_bulk=True)
svc.flush()
svc._storage.close()
'''
            env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
            log = home / "seed.log"
            with log.open("wb") as out:
                result = subprocess.run([sys.executable, "-c", code], cwd=daemons.REPO,
                                        env=dict(env, PYTHONPATH=str(daemons.REPO)), stdout=out,
                                        stderr=subprocess.STDOUT, timeout=600)
            if result.returncode:
                raise RuntimeError(f"session seed exit {result.returncode}; log {log}")
            self.before = dbstate.dump(dsn)

        def timeline(self, procs, holders):
            rows = []
            arms = {}
            self.starts = {side: d.started_at_unix for side, d in procs.items()}
            self.closed_states = {}
            for side, daemon in procs.items():
                dsn = daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"]
                deadline = time.monotonic() + 120
                while True:
                    state = dbstate.dump(dsn)
                    table = state["rows"]["public.episodes"]
                    episodes = [dict(zip(table["columns"], row)) for row in table["rows"]]
                    empty = [ep for ep in episodes if ep["session_key"] == "empty"]
                    work = [ep for ep in episodes if ep["session_key"] == "work"]
                    reached = bool(work and all(ep["ended_at"] is not None for ep in work))
                    reached &= not empty if self.sweep else bool(empty and empty[0]["ended_at"] is not None)
                    triggers = daemon.log.read_text(errors="replace").count("session-end dream trigger:")
                    reached &= triggers == 1
                    if reached: break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{side} did not reach {self.name} milestone; log {daemon.log}")
                    time.sleep(0.1)
                captured = time.time()
                self.validate_expected(state)
                self.closed_states[side] = state
                normalized = normalize_closes(state, self.before, daemon.started_at_unix, captured)
                scrubbed = harness.scrub_declared_rows(normalized, self.before)["state"]
                arms[side] = {"status": 200, "headers": {}, "json": {"db": scrubbed, "events": {"session_end_dream_triggers": triggers}}}
            c = harness.case("bank after idle close" if not self.sweep else "bank after empty sweep", "OBSERVE", "/background/session-state")
            c["_answers"] = (arms["python"], arms["rust"])
            rows.append(c)
            if self.restart:
                for daemon in procs.values():
                    graceful_stop(daemon)
                    daemon.start(240)
                harness.wait_settled([d.port for d in procs.values()])
                arms = {}
                for side, daemon in procs.items():
                    state = dbstate.dump(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
                    self.validate_expected(state)
                    # Restart must preserve the original close times exactly.
                    validate_restart_clocks(state, self.closed_states[side])
                    normalized = normalize_closes(state, self.before, self.starts[side], time.time())
                    triggers = daemon.log.read_text(errors="replace").count("session-end dream trigger:")
                    arms[side] = {"status": 200, "headers": {}, "json": {"db": harness.scrub_declared_rows(normalized, self.before)["state"], "events": {"session_end_dream_triggers": triggers}}}
                c = harness.case("bank after owned clean stop and restart", "OBSERVE", "/background/session-state")
                c["_answers"] = (arms["python"], arms["rust"])
                rows.append(c)
            return rows

        def normalize_db(self, state, before, side):
            return dbstate.normalize(normalize_closes(state, before, self.starts[side], time.time()),
                                     harness.DB_NONDETERMINISTIC, before)

        def validate_expected(self, state):
            table = state["rows"]["public.episodes"]
            episodes = [dict(zip(table["columns"], row)) for row in table["rows"]]
            if not any(ep["session_key"] == "history" for ep in episodes):
                raise RuntimeError("reaper deleted previously nonempty history")
            meta = {row[0]: row[1] for row in state["rows"]["public.meta"]["rows"]}
            if self.sweep and not meta.get("episode_tombstones"):
                raise RuntimeError("empty sweep did not persist a tombstone")

    class SessionSweep(SessionReap):
        name = "session-sweep"
        env = dict(SessionReap.env, PSEUDOLIFE_SESSION_RESUME_SECONDS="0")
        sweep = True

    class SessionRestart(SessionReap):
        name = "session-restart"
        restart = True

    class TombstoneRestart(SessionSweep):
        name = "session-tombstone-restart"
        restart = True

    return {s.name: s for s in (SessionReap, SessionSweep, SessionRestart, TombstoneRestart)}


def validate_restart_clocks(state, closed):
    for name, key in (("episodes", "id"), ("client_sessions", "session_key")):
        a, b = (snapshot["rows"]["public." + name] for snapshot in (state, closed))
        col = a["columns"].index("ended_at")
        id_col = a["columns"].index(key)
        prior = {row[id_col]: row[col] for row in b["rows"]}
        for row in a["rows"]:
            if row[id_col] in prior and row[col] != prior[row[id_col]]:
                raise RuntimeError("restart rewrote an existing close clock")
    a, b = ({row[0]: row[1] for row in snapshot["rows"]["public.meta"]["rows"]} for snapshot in (state, closed))
    for key in ("deferred_empty_roots", "episode_tombstones"):
        if a.get(key) != b.get(key):
            raise RuntimeError("restart rewrote close metadata")


def normalize_closes(state, before, start, end):
    state = copy.deepcopy(state)
    def records(snapshot, table):
        t = snapshot["rows"]["public." + table]
        return [dict(zip(t["columns"], r)) for r in t["rows"]]
    prior = {e["id"]: e["ended_at"] for e in records(before, "episodes")}
    close_times = {}
    meta = {r[0]: r[1] for r in state["rows"]["public.meta"]["rows"]}
    for ep in records(state, "episodes"):
        if ep["ended_at"] is not None and prior.get(ep["id"]) is None:
            close_times[ep["id"]] = ep["ended_at"]
    for ep in records(state, "episodes"):
        if ep["id"] in close_times and ep["parent_id"] in close_times:
            if close_times[ep["id"]] != close_times[ep["parent_id"]]:
                raise RuntimeError("cascade-close clocks disagree")
    for key in ("deferred_empty_roots", "episode_tombstones"):
        for id, item in meta.get(key, {}).items():
            value = item if key == "deferred_empty_roots" else item["ended_at"]
            if id in close_times and close_times[id] != value:
                raise RuntimeError("episode and metadata disagree on close time")
            close_times[id] = value
    for value in close_times.values():
        if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value) or not start <= value <= end:
            raise RuntimeError("close clock outside the arm's captured window")
    for table in ("episodes", "client_sessions"):
        t = state["rows"]["public." + table]
        idx = t["columns"].index("ended_at")
        for row in t["rows"]:
            if table == "episodes":
                id = row[t["columns"].index("id")]
            else:
                ids = row[t["columns"].index("episode_ids")]
                id = next((i for i in reversed(ids) if i in close_times), None)
            if id in close_times:
                if row[idx] != close_times[id]: raise RuntimeError("client/episode close clocks disagree")
                row[idx] = f"<close:{id}>"
    for key in ("deferred_empty_roots", "episode_tombstones"):
        for id, item in meta.get(key, {}).items():
            if key == "deferred_empty_roots": meta[key][id] = f"<close:{id}>"
            else: item["ended_at"] = f"<close:{id}>"
    return state


def graceful_stop(daemon):
    """Signal only the Popen-owned process/group, then observe its exit."""
    if daemon.proc is None or daemon.proc.poll() is not None:
        raise RuntimeError("owned daemon already exited before clean stop")
    daemon.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    code = daemon.proc.wait(timeout=120)
    if code not in (0, -signal.SIGTERM, -getattr(signal, "SIGBREAK", signal.SIGTERM)):
        raise RuntimeError(f"clean stop exited {code}; log {daemon.log}")
    daemon.stop()
