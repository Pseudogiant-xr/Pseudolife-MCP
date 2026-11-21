"""Background lifecycle scenarios for the existing daemon harness.

Each milestone observes the native daemon and dumps the disposable bank.
Close clocks are checked against the daemon's invocation window.
"""
from __future__ import annotations

import copy
import json
import math
import os
import signal
import time

import dbstate
import pgdisposable as pg
from background_seed import seed_bank


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
        native_validation = True

        def prepare_template(self, dsn):
            seed_bank(dsn, "sessions", auto_title=self.auto_title)
            self.before = dbstate.dump(dsn)

        def timeline(self, procs, holders):
            rows = []
            arms = {}
            self.starts = {side: d.started_at_unix for side, d in procs.items()}
            self.closed_states = {}
            for side, daemon in procs.items():
                dsn = daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"]
                deadline = time.monotonic() + 120
                settle, waiting = StableFor(REFRESH_SETTLE_S), "the session milestone"
                while True:
                    waiting = "the session milestone"  # what this pass still lacks
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
                        # ...and, on a bank with live edges, refreshed it and
                        # finished writing (REFRESH_SETTLE_S).
                        if reached:
                            why = refresh_wait(state, daemon.started_at_unix, live_edges(dsn),
                                               settle, time.monotonic())
                            if why:
                                reached, waiting = False, why
                    if self.resume_refusal:
                        # Observe a second failing tick: the first tick's
                        # durable closes survive and neither tick dreams.
                        reached &= daemon.log.read_text(errors="replace").count("session reaper error:") >= 2
                    if reached: break
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{side} did not reach {self.name} milestone (waiting for "
                                           f"{waiting}); log {daemon.log}")
                    time.sleep(0.1)
                captured = time.time()
                self.validate_expected(state)
                self.closed_states[side] = state
                normalized = normalize_closes(state, self.before, daemon.started_at_unix, captured)
                validate_session_writes(normalized, self.before, self.sweep, self.auto_title,
                                        self.expected_triggers)
                scrubbed = keyed_meta(normalize_refresh_clocks(
                    harness.scrub_declared_rows(normalized, self.before)["state"],
                    self.before, [(daemon.started_at_unix, captured)]))
                arms[side] = {"status": 200, "headers": {}, "json": {"db": scrubbed, "events": {"session_end_dream_triggers": triggers}}}
            c = harness.case("bank after idle close" if not self.sweep else "bank after empty sweep", "OBSERVE", "/background/session-state")
            c["_answers"] = (None, arms["rust"])
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
                    validate_session_writes(normalized, self.before, self.sweep, self.auto_title,
                                            self.expected_triggers)
                    triggers = daemon.log.read_text(errors="replace").count("session-end dream trigger:")
                    if triggers:
                        raise RuntimeError("restart triggered a second session-end dream")
                    db = keyed_meta(normalize_refresh_clocks(
                        harness.scrub_declared_rows(normalized, self.before)["state"],
                        self.before, [(self.starts[side], now)]))
                    arms[side] = {"status": 200, "headers": {}, "json": {"db": db, "events": {"session_end_dream_triggers": triggers}}}
                c = harness.case("bank after owned clean stop and restart", "OBSERVE", "/background/session-state")
                c["_answers"] = (None, arms["rust"])
                rows.append(c)
            return rows

        def normalize_db(self, state, before, side):
            state = dbstate.normalize(normalize_closes(state, before, self.starts[side], time.time()),
                                      harness.DB_NONDETERMINISTIC, before,
                                      windows=self.clock_window(side))
            return normalize_refresh_clocks(state, before, self.clock_window(side))

        def validate_snapshot(self, state, before):
            if differences := dbstate.diff(before["catalog"], state["catalog"]):
                raise RuntimeError(f"session lifecycle changed the seeded catalog: {differences[:8]}")
            normalized = normalize_closes(state, before, self.starts["rust"], time.time())
            self.validate_expected(state)
            validate_session_writes(normalized, before, self.sweep, self.auto_title,
                                    self.expected_triggers)

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


def validate_session_writes(state, before, sweep, auto_title, expected_triggers):
    """Check the fixture's durable row delta after checking the raw close clocks."""
    has_base = pg.base_database() is not None
    written = {"public.client_sessions", "public.episodes", "public.entries", "public.meta"}
    if has_base and expected_triggers:
        # A session-end dream on a populated base also refreshes its graph.
        # normalize_refresh_clocks checks the observed refresh windows.
        written.update(f"public.{table}" for table in
                       ("communities", "entity_communities", "entity_sources", "edges"))
    unchanged = [{name: table for name, table in snap["rows"].items() if name not in written}
                 for snap in (before, state)]
    if differences := dbstate.diff(*unchanged):
        raise RuntimeError(f"session lifecycle changed unrelated durable rows: {differences[:8]}")

    def rows(snapshot, table, key):
        data = snapshot["rows"]["public." + table]
        records = (dict(zip(data["columns"], row)) for row in data["rows"])
        return {row[key]: row for row in records}

    initial_clients = rows(before, "client_sessions", "session_key")
    clients = rows(state, "client_sessions", "session_key")
    roots = {key: initial_clients[key]["episode_ids"][0] for key in ("empty", "work", "history")}
    for key, root in roots.items():
        want = dict(initial_clients[key])
        if key != "history":
            want.update(ended_at=f"<close:{root}>", end_reason="idle")
        if clients.get(key) != want:
            raise RuntimeError(f"unexpected durable client session: {key}")

    episodes = rows(state, "episodes", "id")
    for id, initial in rows(before, "episodes", "id").items():
        key = initial["session_key"]
        if key not in roots or (id != roots[key] and initial["parent_id"] != roots[key]):
            continue
        want = dict(initial)
        if key == "empty" and sweep:
            want = None
        elif key != "history":
            want["ended_at"] = f"<close:{id}>"
            if auto_title and id == roots["work"] and id in episodes:
                want["title"] = episodes[id]["title"]  # validated before the row comparison
        if episodes.get(id) != want:
            raise RuntimeError(f"unexpected durable episode: {key}")

    entries = rows(state, "entries", "id")
    fixture_timestamps = []
    for id, initial in rows(before, "entries", "id").items():
        if initial["episode_id"] == roots["work"]:
            want = dict(initial, episode_title=episodes[roots["work"]]["title"])
            if expected_triggers:
                want["dream_state"] = "acknowledged"
                fixture_timestamps.append(want["ts"])
            if entries.get(id) != want:
                raise RuntimeError("reaper changed the durable fixture entry unexpectedly")

    meta = rows(state, "meta", "key")
    # A base-relative dump omits an unchanged cursor from both snapshots.
    prior_meta = dict(dbstate.prior_rows(before, "public.meta", [["cortex_dream_cursor"]]))
    prior_cursor = prior_meta["cortex_dream_cursor"]
    cursor = (meta.get("cortex_dream_cursor", {"value": prior_cursor}) if has_base
              else meta["cortex_dream_cursor"])["value"]
    minimum = max([prior_cursor, *fixture_timestamps])
    if cursor < minimum or (not (expected_triggers and has_base) and cursor != minimum):
        raise RuntimeError("session-end dream left the wrong durable cursor")

    empty = roots["empty"]
    close = f"<close:{empty}>"
    deferred = meta.get("deferred_empty_roots", {}).get("value", {})
    tombs = meta.get("episode_tombstones", {}).get("value", {})
    if sweep:
        if empty in deferred or tombs.get(empty) != {
            "session_key": "empty", "ended_at": close, "title": "empty fixture",
        }:
            raise RuntimeError("sweep did not move the empty fixture's close to its tombstone")
    elif deferred.get(empty) != close or empty in tombs:
        raise RuntimeError("idle close did not defer the empty fixture for resume")


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


def live_edges(dsn):
    """The arm's bank holds a live edge. Both daemons refresh graph insight
    at the end of every completed dream, the empty-backlog path included
    (service_dream.py ~1560 and ~2399, dream/run.rs ~1972 and ~2275), over
    the graph their load_graph reads, and skip it (empty_graph) when that
    graph has no edges. Asked of the database, not the dump: a base's
    untouched edges are left out of it."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM edges "
                                 "WHERE superseded_at IS NULL)").fetchone()[0])


# After the digest, both dreams still write: the entity-source backfill and
# the link retype (service_dream.py ~2399, dream/run.rs ~2275), in a
# transaction of their own, and neither daemon logs the dream's end. On a
# bank with a graph the capture therefore also needs the dumped rows to stay
# the same this long. That narrows the race; a completion signal from both
# daemons would close it.
REFRESH_SETTLE_S = 3.0


class StableFor:
    """True once the same dumped rows have been seen for ``seconds``."""

    def __init__(self, seconds):
        self.seconds, self.sig, self.since = seconds, None, None

    def __call__(self, state, now):
        sig = json.dumps(state["rows"], sort_keys=True, default=str)
        if sig != self.sig:
            self.sig, self.since = sig, now
            return False
        return now - self.since >= self.seconds


def graph_refresh_pending(state, start):
    """True while the bank has no graph digest computed at or after this
    daemon's ``start``: its session-end dream's graph refresh is still to
    come. In the partial forced run (870fb28fc) the milestone held once both
    had acknowledged the dream's entries, but Rust had rewritten every base
    community row by then and Python none."""
    digest = {r[0]: r[1] for r in state["rows"]["public.meta"]["rows"]}.get("graph_digest")
    at = digest.get("computed_at") if isinstance(digest, dict) else None
    return not (isinstance(at, (int, float)) and not isinstance(at, bool) and at >= start)


def refresh_wait(state, start, live, settle, now):
    """What a capture still waits for after the session milestone, or None:
    on a bank with live edges (``live``), the graph refresh, then
    REFRESH_SETTLE_S of unchanged rows. A bank without live edges, a
    synthetic fixture included, is never refreshed and never waits."""
    if not live:
        return None
    if graph_refresh_pending(state, start):
        return "its graph insight refresh"
    if not settle(state, now):
        return "its bank to settle after the refresh"
    return None


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
