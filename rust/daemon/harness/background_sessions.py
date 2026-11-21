"""Background lifecycle scenarios for the existing daemon harness.

Each milestone observes the native daemon and dumps the disposable bank.
Close clocks are checked against the daemon's invocation window.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import signal
import time
from collections import Counter, defaultdict

import dbstate
import pgdisposable as pg
import psycopg
from psycopg import sql
from background_seed import seed_bank
from rust.harness_support.bank_fixture import assert_fixture_database


SESSION_TABLES = ("client_sessions", "episodes", "entries", "meta")
DREAM_EXCLUDED = {"consolidation", "reflection", "status", "log", "digest"}


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
            self.before = self.snapshot_db(dsn)
            self.refreshes_graph = bool(self.expected_triggers and pg.base_database() and live_edges(dsn))

        def snapshot_db(self, dsn):
            state = dbstate.dump(dsn)
            if pg.base_database():
                read_session_rows(dsn, state)
            return state

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
                if pg.base_database():
                    read_session_rows(dsn, state)
                    captured = time.time()
                self.validate_expected(state)
                self.closed_states[side] = state
                normalized = normalize_closes(state, self.before, daemon.started_at_unix, captured)
                validate_session_writes(normalized, self.before, self.sweep, self.auto_title,
                                        self.expected_triggers, raw=state,
                                        graph_refresh=self.refreshes_graph, reap_at=captured)
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
                    state = self.snapshot_db(daemon.env["PSEUDOLIFE_MCP_DATABASE_URL"])
                    self.validate_expected(state)
                    # Restart must preserve the original close times exactly.
                    validate_restart_clocks(state, self.closed_states[side])
                    now = time.time()
                    normalized = normalize_closes(state, self.before, self.starts[side], now)
                    validate_session_writes(normalized, self.before, self.sweep, self.auto_title,
                                            self.expected_triggers, boots=2, raw=state,
                                            graph_refresh=self.refreshes_graph, reap_at=now)
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
            now = time.time()
            normalized = normalize_closes(state, before, self.starts["rust"], now)
            self.validate_expected(state)
            validate_session_writes(normalized, before, self.sweep, self.auto_title,
                                    self.expected_triggers, boots=1 + self.restart, raw=state,
                                    graph_refresh=self.refreshes_graph, reap_at=now)

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


def read_session_rows(dsn, snapshot):
    """Materialize session rows that an ordinary copied-bank dump omits."""
    with psycopg.connect(dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        assert_fixture_database(conn)
        for name in SESSION_TABLES:
            table = snapshot["rows"]["public." + name]
            select, first = dbstate._select_list(conn, "public", name)
            order = dbstate._order_columns(conn, "public", name)
            ordering = sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(col))
                                           for col in (order or [first]))
            query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                sql.SQL(select), sql.Identifier("public", name), ordering)
            table["rows"] = [dbstate._plain(list(row)) for row in conn.execute(query)]
            table.pop("base_missing", None)


def session_rows(snapshot, table):
    data = snapshot["rows"]["public." + table]
    return [dict(zip(data["columns"], row)) for row in data["rows"]]


def expected_base_title(started, entries):
    """Derive the title of a generic inherited root from its seeded entries."""
    entries = sorted(entries, key=lambda row: row["id"])
    counts = Counter(row["source"].strip() or "session" for row in entries)
    signal = {source: count for source, count in counts.items()
              if source not in {"status", "log", "claude", "agent", "conversation"}}
    sources = signal or counts
    source = max(sources, key=sources.get)
    first = min(entries, key=lambda row: row["ts"])
    snippet = " ".join(first["text"].split())
    if len(snippet) > 60:
        space = snippet.rfind(" ", 0, 60)
        snippet = snippet[:space if space > 20 else 60].rstrip() + "…"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
    return f"{source} - {stamp}" + (f": {snippet}" if snippet else "")


def apply_base_reap(episodes, clients, entries, deferred, tombs, fixture_roots, now, sweep, dreams):
    """Apply the inherited idle closes and deferred expiry to pre-run rows."""
    by_id = {row["id"]: row for row in episodes}
    by_key = {row["session_key"]: row for row in clients}
    children, held = defaultdict(list), defaultdict(list)
    for row in episodes:
        children[row["parent_id"]].append(row["id"])
    for row in entries:
        held[row["episode_id"]].append(row)
        if dreams and row["source"] not in DREAM_EXCLUDED and row["dream_state"] == "pending":
            row["dream_state"] = "acknowledged"

    def subtree(root_id):
        pending, seen = [root_id], set()
        while pending:
            identifier = pending.pop()
            if identifier in seen:
                continue
            seen.add(identifier)
            pending.extend(children[identifier])
        return seen

    for root in sorted(episodes, key=lambda row: row["started_at"]):
        identifier, key = root["id"], root["session_key"]
        if (identifier in fixture_roots or root["parent_id"] is not None
                or root["ended_at"] is not None or not key):
            continue
        ids = subtree(identifier)
        content = [row for i in ids for row in held[i]]
        own = [row["ts"] for row in held[identifier] if row["ts"] > 0]
        activity = max(max(own, default=root["started_at"]),
                       max((row["ts"] for row in content if row["ts"] > 0), default=0))
        if activity > now:
            continue
        for i in ids:
            if by_id[i]["ended_at"] is None:
                by_id[i]["ended_at"] = f"<close:{i}>"
        if key in by_key:
            client = by_key[key]
            history = client["episode_ids"]
            client.update(ended_at=f"<close:{identifier}>", end_reason="idle",
                          episode_ids=history if identifier in history else [*history, identifier])
        if not any(row["source"] != "digest" for row in content):
            deferred[identifier] = f"<close:{identifier}>"
        if content and re.match(r"^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", root["title"]):
            root["title"] = expected_base_title(root["started_at"], content)
            for row in held[identifier]:
                row["episode_title"] = root["title"]

    # A malformed resume setting stops after the idle closes.
    if not dreams:
        return episodes, False
    deleted = set()
    for identifier in list(deferred):
        root = by_id.get(identifier)
        if root is None or root["ended_at"] is None:
            del deferred[identifier]
            continue
        ended = root["ended_at"]
        if (isinstance(ended, str) and not sweep
                or not isinstance(ended, str) and now - ended <= (0 if sweep else 3600)):
            continue
        del deferred[identifier]
        ids = subtree(identifier)
        if any(row["source"] != "digest" for i in ids for row in held[i]):
            continue
        deleted.update(ids)
        if root["session_key"]:
            tombs[identifier] = {"session_key": root["session_key"], "ended_at": ended,
                                 "title": root["title"]}
    if deleted or sweep:
        # Handle tombstones outlive the session resume window by default.
        def ended_at(tomb):
            ended = tombs[tomb]["ended_at"]
            return now if isinstance(ended, str) else ended

        live = [key for key in sorted(tombs) if now - ended_at(key) <= 2592000]
        keep = set(sorted(live, key=ended_at, reverse=True)[:200 - int(sweep)])
        for key in set(tombs) - keep:
            del tombs[key]
    return [row for row in episodes if row["id"] not in deleted], bool(deleted)


def validate_session_writes(state, before, sweep, auto_title, expected_triggers, *,
                            boots=1, raw=None, graph_refresh=False, reap_at=None):
    """Compare the entire expected bank after closing the captured fixture."""
    if differences := dbstate.diff(before.get("catalog", {}), state.get("catalog", {})):
        raise RuntimeError(f"session lifecycle changed the seeded catalog: {differences[:8]}")
    expected = copy.deepcopy(before["rows"])
    observed = state["rows"]
    clients = session_rows(before, "client_sessions")
    roots = {row["session_key"]: row["episode_ids"][0] for row in clients
             if row["session_key"] in {"empty", "work"}}
    empty, work = roots["empty"], roots["work"]
    order = [[root for root in group if root in roots.values()] for group in state["root_close_order"]]
    if [group for group in order if group] not in ([[empty], [work]], [[empty, work]]):
        raise RuntimeError("the empty root must close before the work root")
    if raw is not None:
        clocks = {row["session_key"]: row["ended_at"] for row in session_rows(raw, "client_sessions")}
        if clocks["work"] - clocks["empty"] >= 0.2:
            raise RuntimeError("the two fixture roots closed in different reaper ticks")

    for row in clients:
        if row["session_key"] in roots:
            row.update(ended_at=f"<close:{roots[row['session_key']]}>", end_reason="idle")
    episodes = session_rows(before, "episodes")
    root = next(row for row in episodes if row["id"] == work)
    # The pinned entry has one source and one snippet. Chrono formats the
    # fixture start minute in the daemon's local zone, which we inherit.
    if auto_title:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(root["started_at"]))
        root["title"] = f"fixture - {stamp}: The background lifecycle fixture retains a nested session…"
    for row in episodes:
        if row["id"] in (empty, work) or row["parent_id"] == work:
            row["ended_at"] = f"<close:{row['id']}>"
    entries = session_rows(before, "entries")
    fixture_timestamps = []
    for row in entries:
        if row["episode_id"] == work:
            row["episode_title"] = root["title"]
            if expected_triggers:
                row["dream_state"] = "acknowledged"
                fixture_timestamps.append(row["ts"])

    has_base = pg.base_database() is not None
    meta = dict(expected["public.meta"]["rows"])
    prior = dict(dbstate.prior_rows(before, "public.meta",
                                  [["cortex_dream_cursor"], ["deferred_empty_roots"], ["episode_tombstones"]]))
    current = dict(observed["public.meta"]["rows"])
    meta["writer_lease_epoch"] += boots
    prior_cursor = prior["cortex_dream_cursor"]
    minimum = max([prior_cursor, *fixture_timestamps])
    cursor = current.get("cortex_dream_cursor", prior_cursor) if has_base else current.get("cortex_dream_cursor")
    if type(cursor) not in (int, float) or not math.isfinite(cursor) or cursor < minimum \
            or (not (expected_triggers and has_base) and cursor != minimum):
        raise RuntimeError("session-end dream left the wrong durable cursor")
    if "cortex_dream_cursor" in meta or cursor != prior_cursor:
        meta["cortex_dream_cursor"] = cursor if expected_triggers and has_base else minimum
    deferred = dict(prior.get("deferred_empty_roots") or {})
    tombs = dict(prior.get("episode_tombstones") or {})
    swept_base = False
    if has_base and reap_at is not None:
        episodes, swept_base = apply_base_reap(
            episodes, clients, entries, deferred, tombs, {empty, work}, reap_at, sweep, expected_triggers)
    close = f"<close:{empty}>"
    if sweep:
        episodes = [row for row in episodes if row["id"] != empty]
        tombs[empty] = {"session_key": "empty", "ended_at": close, "title": "empty fixture"}
    else:
        deferred[empty] = close
    if sweep or swept_base:
        meta["episode_tombstones"] = tombs
    meta["deferred_empty_roots"] = deferred

    for name, rows in (("client_sessions", clients), ("episodes", episodes), ("entries", entries)):
        table = expected["public." + name]
        table["rows"] = [[row[column] for column in table["columns"]] for row in rows]

    if graph_refresh:
        digest = current.get("graph_digest")
        fields = ("id", "label", "size", "cohesion")
        summaries = [{key: row[key] for key in fields} for row in session_rows(state, "communities")]
        if (not isinstance(digest, dict)
                or set(digest) != {"computed_at", "communities", "god_nodes", "surprises", "questions", "totals"}
                or sorted(digest["communities"], key=lambda row: row["id"]) !=
                sorted(summaries, key=lambda row: row["id"])
                or digest.get("totals", {}).get("communities") != len(summaries)):
            raise RuntimeError("graph digest does not describe the persisted community refresh")
        # The remaining digest fields are derived graph output. Their
        # algorithms have independent corpus tests in the Rust crate.
        meta["graph_digest"] = digest
    expected["public.meta"]["rows"] = [[key, value] for key, value in sorted(meta.items())]
    if graph_refresh:
        refresh = {"public.communities", "public.entity_communities", "public.entity_sources", "public.edges"}
        expected = {key: table for key, table in expected.items() if key not in refresh}
        observed = {key: table for key, table in observed.items() if key not in refresh}
    if differences := dbstate.diff(expected, observed):
        raise RuntimeError(f"session bank differs from the fixture contract: {differences[:8]}")


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
