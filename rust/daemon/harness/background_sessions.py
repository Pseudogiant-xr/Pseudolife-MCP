"""Background lifecycle scenarios for the existing daemon harness.

All seeds use Python service methods. Each milestone observes real daemon
subprocesses and dumps the whole disposable bank; close clocks are checked
against that arm's own invocation window before normalization.
"""
from __future__ import annotations

import copy
import json
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
        expected_triggers = 1
        resume_refusal = False
        auto_title = False

        def configure_daemons(self, procs):
            if "python" not in procs: return
            # Both daemons run the session-end dream; the trigger is counted
            # from a log line each side writes when it fires (Python's
            # through this wrapper, Rust's natively), and the capture waits
            # for the dream's acknowledgements on both.
            code = '''
import logging, signal
from pseudolife_memory import mcp_server
_real_dream = mcp_server.service._fire_and_forget_dream
def _counted_dream():
    logging.getLogger("pseudolife-mcp").warning("session-end dream trigger: fired")
    _real_dream()
mcp_server.service._fire_and_forget_dream = _counted_dream
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
            fixture_env = self.prepare_home(home) if self.ci_fixture else {}
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
            if self.auto_title:
                code = code.replace('"work fixture"', '"session - 2023-11-14 22:13"')
            env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
            env.update(fixture_env)
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
                    reached &= triggers == self.expected_triggers
                    if self.expected_triggers:
                        # The fired dream has acknowledged what it pulled.
                        entries = state["rows"].get("public.entries", {"columns": [], "rows": []})
                        col = entries["columns"].index("dream_state") \
                            if "dream_state" in entries["columns"] else None
                        reached &= col is None or all(row[col] != "pending"
                                                      for row in entries["rows"])
                        # ...and, on a bank with a graph, refreshed it.
                        reached &= not graph_refresh_pending(state, self.before, daemon.started_at_unix)
                    if self.resume_refusal:
                        # Observe a second failing tick: the first tick's
                        # durable closes survive and neither tick dreams.
                        reached &= daemon.log.read_text(errors="replace").count("session reaper error:") >= 2
                    if reached: break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{side} did not reach {self.name} milestone; log {daemon.log}")
                    time.sleep(0.1)
                captured = time.time()
                self.validate_expected(state)
                self.closed_states[side] = state
                normalized = normalize_closes(state, self.before, daemon.started_at_unix, captured)
                scrubbed = keyed_meta(normalize_refresh_clocks(
                    harness.scrub_declared_rows(normalized, self.before)["state"],
                    self.before, [(daemon.started_at_unix, captured)]))
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
                    now = time.time()
                    normalized = normalize_closes(state, self.before, self.starts[side], now)
                    triggers = daemon.log.read_text(errors="replace").count("session-end dream trigger:")
                    db = keyed_meta(normalize_refresh_clocks(
                        harness.scrub_declared_rows(normalized, self.before)["state"],
                        self.before, [(self.starts[side], now)]))
                    arms[side] = {"status": 200, "headers": {}, "json": {"db": db, "events": {"session_end_dream_triggers": triggers}}}
                c = harness.case("bank after owned clean stop and restart", "OBSERVE", "/background/session-state")
                c["_answers"] = (arms["python"], arms["rust"])
                rows.append(c)
            return rows

        def normalize_db(self, state, before, side):
            state = dbstate.normalize(normalize_closes(state, before, self.starts[side], time.time()),
                                      harness.DB_NONDETERMINISTIC, before,
                                      windows=self.clock_window(side))
            return normalize_refresh_clocks(state, before, self.clock_window(side))

        def validate_expected(self, state):
            table = state["rows"]["public.episodes"]
            episodes = [dict(zip(table["columns"], row)) for row in table["rows"]]
            if not any(ep["session_key"] == "history" for ep in episodes):
                raise RuntimeError("reaper deleted previously nonempty history")
            if self.auto_title:
                # Children inherit the session key and may sort before the
                # root, but only the root receives an automatic title.
                work = next(ep for ep in episodes
                            if ep["session_key"] == "work" and ep["parent_id"] is None)
                if not work["title"].startswith("fixture - "):
                    raise RuntimeError("reaper did not persist a content-derived root title")
                table = state["rows"]["public.entries"]
                entries = [dict(zip(table["columns"], row)) for row in table["rows"]]
                stamps = [e["episode_title"] for e in entries if e["episode_id"] == work["id"]]
                if not stamps or any(title != work["title"] for title in stamps):
                    raise RuntimeError("reaper did not persist the root entry title stamps")
                if next(ep for ep in episodes if ep["title"] == "child")["parent_id"] != work["id"]:
                    raise RuntimeError("reaper changed child attribution")
            meta = {row[0]: row[1] for row in state["rows"]["public.meta"]["rows"]}
            if self.sweep:
                # A real bank's copy already holds tombstones, and a sweep also
                # tombstones its expired deferred roots: the new one must be
                # this scenario's own empty session's.
                old = (prior_close_maps(state, self.before)["episode_tombstones"]
                       if hasattr(self, "before") else {})
                tombs = meta.get("episode_tombstones") or {}
                if not any(isinstance(tombs[k], dict) and tombs[k].get("session_key") == "empty"
                           for k in set(tombs) - set(old)):
                    raise RuntimeError("empty sweep did not persist a tombstone")

    class SessionSweep(SessionReap):
        name = "session-sweep"
        env = dict(SessionReap.env, PSEUDOLIFE_SESSION_RESUME_SECONDS="0")
        sweep = True

    class ResumeRefusal(SessionReap):
        name = "session-resume-refusal"
        env = dict(SessionReap.env, PSEUDOLIFE_SESSION_RESUME_SECONDS="bad")
        expected_triggers = 0
        resume_refusal = True
        auto_title = True

    class SessionRestart(SessionReap):
        name = "session-restart"
        restart = True
        auto_title = True

    class TombstoneRestart(SessionSweep):
        name = "session-tombstone-restart"
        restart = True

    return {s.name: s for s in (SessionReap, SessionSweep, SessionRestart, TombstoneRestart, ResumeRefusal)}


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


# A session-end dream refreshes graph insight on each daemon. On a real
# bank's copy, which holds a graph, that rewrites the communities, their
# memberships, the derived entity sources and meta's graph_digest at each
# daemon's own now(): in the partial forced run (870fb28fc) the partition and
# the counts agreed, and only these clocks differed. A synthetic fixture
# holds no graph, so nothing is refreshed there.
REFRESH_CLOCKS = {
    ("communities", "computed_at"): "clock: graph insight refresh",
    ("entity_communities", "computed_at"): "clock: graph insight refresh",
    ("entity_sources", "updated_at"): "clock: derived entity source refresh",
}
REFRESH_META_CLOCKS = {"graph_digest": "computed_at"}


def keyed_meta(state):
    """An observed bank with meta as ``by_key`` ({key: value}) instead of
    rows: a meta difference then prints its row's key
    (.json.db.rows.public.meta.by_key.graph_digest...), never an index whose
    row only its value could identify, and row order no longer matters. Used
    only for the session observations; the final DB comparison keeps rows."""
    meta = state["rows"].get("public.meta")
    if not meta or "rows" not in meta:
        return state
    state = copy.deepcopy(state)
    key, value = meta["columns"].index("key"), meta["columns"].index("value")
    state["rows"]["public.meta"] = {
        "columns": meta["columns"],
        "by_key": {row[key]: row[value] for row in meta["rows"]},
        **{k: v for k, v in meta.items() if k not in ("columns", "rows")},
    }
    return state


def graph_refresh_pending(state, before, start):
    """True while a bank that held a graph before the arm ran (a
    graph_digest row in ``before`` or the base) has no digest computed at or
    after this daemon's ``start``: its session-end dream's graph refresh is
    still to come. In the partial forced run (870fb28fc) the milestone held
    once both had acknowledged the dream's entries, but Rust had rewritten
    every base community row by then and Python none. A bank without a graph
    is never refreshed, so a synthetic fixture never waits on this."""
    held = any(r[0] == "graph_digest" for r in before["rows"].get("public.meta", {}).get("rows", [])) \
        or json.dumps("graph_digest") in dbstate.base_keys("public.meta")
    if not held:
        return False
    digest = {r[0]: r[1] for r in state["rows"]["public.meta"]["rows"]}.get("graph_digest")
    at = digest.get("computed_at") if isinstance(digest, dict) else None
    return not (isinstance(at, (int, float)) and not isinstance(at, bool) and at >= start)


def normalize_refresh_clocks(state, before, windows):
    """A refresh's clocks as dbstate's tokens: the columns above as
    ``dbstate.normalize`` treats a clock (a new row's, or a base row's
    inside the arm's window), and graph_digest's computed_at inside the
    window. Every other clock, and every other value, stays raw."""
    state = dbstate.normalize(state, REFRESH_CLOCKS, before, windows=windows)
    spans = dbstate._clock_windows(windows)
    for row in state["rows"].get("public.meta", {}).get("rows", []):
        field = REFRESH_META_CLOCKS.get(row[0])
        value = row[1].get(field) if field and isinstance(row[1], dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
                and any(lo <= value <= hi for lo, hi in spans):
            row[1][field] = f"<{field}>"
    return state


def normalize_closes(state, before, start, end):
    state = copy.deepcopy(state)
    def records(snapshot, table):
        t = snapshot["rows"]["public." + table]
        return [dict(zip(t["columns"], r)) for r in t["rows"]]
    t = before["rows"]["public.episodes"]
    prior = {e["id"]: e["ended_at"] for e in (dict(zip(t["columns"], r)) for r in dbstate.prior_rows(
        before, "public.episodes", state["rows"]["public.episodes"]["rows"]))}
    close_times = {}
    meta = {r[0]: r[1] for r in state["rows"]["public.meta"]["rows"]}
    # Close clocks the bank held before this arm ran. On a copy of a real
    # bank they predate both daemons; an entry carrying one (unchanged, or a
    # sweep's tombstone of an old deferred root) is compared exactly, not
    # window-checked. Any other value is this arm's write.
    carried = {id: value for id, value in prior.items() if value is not None}
    maps = prior_close_maps(state, before)
    carried.update(maps["deferred_empty_roots"])
    carried.update((id, item.get("ended_at")) for id, item in maps["episode_tombstones"].items()
                   if isinstance(item, dict))
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
            if id in carried and carried[id] == value and id not in close_times:
                continue
            close_times[id] = value
    for value in close_times.values():
        if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value) or not start <= value <= end:
            raise RuntimeError("close clock outside the arm's captured window")
    roots = {ep["id"] for ep in records(state, "episodes") if ep["parent_id"] is None}
    roots.update(meta.get("deferred_empty_roots", {}))
    roots.update(meta.get("episode_tombstones", {}))
    # Preserve semantic root-close ordering, including ties, before replacing
    # per-row clocks. Consistent swaps across all associations must still diff.
    groups = {}
    for id in roots & close_times.keys():
        groups.setdefault(close_times[id], []).append(id)
    state["root_close_order"] = [sorted(groups[stamp]) for stamp in sorted(groups)]
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
            if id not in close_times: continue
            if key == "deferred_empty_roots": meta[key][id] = f"<close:{id}>"
            else: item["ended_at"] = f"<close:{id}>"
    return state


def prior_close_maps(state, before):
    """The close metadata maps the bank held before the arm ran: ``before``'s,
    or under ``PL_HARNESS_BASE_DATABASE`` the base's, since a dump leaves out
    a meta row equal to the base."""
    rows = dbstate.prior_rows(before, "public.meta", state["rows"]["public.meta"]["rows"])
    meta = {r[0]: r[1] for r in rows}
    return {key: meta.get(key) if isinstance(meta.get(key), dict) else {}
            for key in ("deferred_empty_roots", "episode_tombstones")}


def graceful_stop(daemon):
    """Signal only the Popen-owned process/group, then observe its exit."""
    if daemon.proc is None or daemon.proc.poll() is not None:
        # Which side, and how it ended: a negative code is the signal (-9 is
        # the OOM killer's SIGKILL), so a crash and a kill read apart.
        kind = getattr(daemon, "kind", "owned")
        code = None if daemon.proc is None else daemon.proc.returncode
        how = "no process" if code is None else f"code {code}"
        if isinstance(code, int) and code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:  # Windows defines no SIGKILL; the runner is Linux
                name = {6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV", 15: "SIGTERM"}.get(-code)
            if name:
                how += f", {name}"
        raise RuntimeError(f"{kind} daemon already exited ({how}) before clean stop; log {daemon.log}")
    daemon.proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    code = daemon.proc.wait(timeout=120)
    if code != 0:
        raise RuntimeError(f"clean stop exited {code}; log {daemon.log}")
    daemon.stop()
