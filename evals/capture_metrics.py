"""Memory-loop capture metrics — read-only report over the live bank.

Measures the three beats of the memory loop against the real Postgres bank,
per CLIENT SESSION: RECALL (did the session search early), CAPTURE (did it
store anything substantive), REFLECT (did it log an outcome, and did the
outcome credit the hits it used), plus whether stored entries are ever
retrieved again by a later session.

What counts as a session (fixed 2026-09-25). One client session can leave
two root episodes: the plugin's SessionStart hook opens one keyed by the
client's own session id (a dashed UUID), and the stdio shim opens another,
keyed by a fresh 32-hex uuid and titled after its working directory. Its
memory activity lands on whichever root the agent's writes resolve to. Up to
2026-09-25 this report counted every keyed root episode; that day the bank
held 193 roots in 48 h, 111 of them idle shim roots titled after the shared
shim runtime directory, so every per-session denominator was inflated. Now:

* hook roots (dashed-UUID key) are client sessions whether or not the agent
  used memory — a session that never searched is exactly the miss this
  report exists to count;
* shim roots (32-hex key) count only when they carry memory activity (an
  entry or outcome in their subtree, or a search under their episode or
  session key); idle ones are transport artifacts and are dropped;
* a hook root and an active shim root that started within
  ``PAIR_WINDOW_S`` of each other are ONE session when each is the other's
  only candidate (both can be active: writes that pass ``episode=`` land on
  the hook root while the searches carry the shim's key). Anything less
  certain stays unmerged and is reported as ``ambiguous_pairs``.

A search is attributed by its ``session_id`` (the caller); its
``episode_id`` is only the daemon's process-wide current episode, used when
no session id was recorded.

What the bank cannot show. The daemon DELETES a session's root when it
ends holding no stored entry (prune-on-empty at SessionEnd, or later by the
idle reaper) and keeps no record of the registration. A session that only
searched, set facts or logged outcomes therefore has no root; its searches
and outcomes remain and are counted under ``roots.pruned_with_searches`` /
``pruned_with_outcomes`` instead of ``sessions``, and a session that never
touched memory leaves nothing at all. ``sessions`` is thus the sessions
that stored something or have not ended cleanly yet, and every rate here
is over those survivors: read coverage figures as upper bounds. Counting
every session needs the daemon to keep a registration record.

Headless helper runs (``claude -p`` / ``codex exec`` behind a judge or
extractor shim) fire the same hooks and are indistinguishable from an
interactive session that ignored memory; ``working_sessions`` (at least one
memory interaction) is reported beside ``sessions`` for that reason.

Not recorded by the bank, so reported as null with the reason:
``memory_lesson_search`` calls (no retrieval-log row, no read counter) and
the used_ids an outcome claimed that nothing served (the daemon returns
``used_ids_unmatched`` to the caller but persists only credited ids).

Baseline (2026-07-18, N=89 root episodes since 2026-06-27, BEFORE the
auto-outcome stage deployed). Measured with the root-episode denominator
retired on 2026-09-25, so it is not comparable with ``sessions`` now:

    sessions with >=1 substantive store   88/89  (99%)
    sessions with zero outcome signals    31/89  (35%)
    outcomes by type                      112 success / 25 failure / 23 correction
    failure+correction share              30%
    median stores per session             1      (p90 3.2)

Usage (read-only transaction; safe against the live bank):

    python evals/capture_metrics.py            # table
    python evals/capture_metrics.py --json     # machine-readable
    python evals/capture_metrics.py --since 2026-07-18   # window start (UTC date)

DSN: ``PSEUDOLIFE_METRICS_DSN`` env var, defaulting to the stock local stack
(``postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

DSN = os.environ.get(
    "PSEUDOLIFE_METRICS_DSN",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
)

# A hook root and its shim root start within a couple of seconds of each
# other (the hook fires at SessionStart, the shim at MCP launch). Measured
# 2026-09-25 over 7 days of the live bank: the eight active shim roots with
# a hook root nearby sat 1-8 s from it, the next nearest 135 s. A wider
# window only adds fleet-start candidates, which the uniqueness rule refuses.
PAIR_WINDOW_S = 5.0
# "Searched early": the session's first memory_search within this many
# seconds of its start. A session-start policy asks for recall at task
# start; fifteen minutes covers a slow first turn.
EARLY_SEARCH_S = 900.0
REUSE_WINDOW_S = 14 * 86_400.0
NON_SUBSTANTIVE_SOURCES = ("status", "log")

_HOOK_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHIM_KEY = re.compile(r"^[0-9a-f]{32}$")

NOT_RECORDED = {
    "lesson_search_used": (
        "not recorded by the bank: memory_lesson_search writes no "
        "retrieval_events row and lessons carry no read counter"),
    "used_ids_unmatched": (
        "not recorded by the bank: an outcome's used_ids that nothing served "
        "are returned to the caller as used_ids_unmatched and never persisted"),
}


def root_kind(session_key: str) -> str:
    """``hook`` for a client session id the SessionStart hook registered,
    ``shim`` for the stdio shim's own uuid, ``other`` otherwise."""
    if _HOOK_KEY.match(session_key):
        return "hook"
    if _SHIM_KEY.match(session_key):
        return "shim"
    return "other"


@dataclass(eq=False)  # identity, not field equality: two idle roots look alike
class _Session:
    roots: list[str]
    keys: set[str]
    kind: str
    started_at: float
    substantive: int = 0
    total: int = 0
    explicit: int = 0
    inferred: int = 0
    searches: list[float] = field(default_factory=list)
    credited_ids: int = 0

    @property
    def active(self) -> bool:
        return bool(self.total or self.explicit or self.inferred or self.searches)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = q * (len(vs) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (idx - lo)


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def _root_of(conn) -> dict[str, str]:
    """Every episode id mapped to its root episode id."""
    rows = conn.execute("""
        WITH RECURSIVE tree AS (
            SELECT id, id AS root_id FROM episodes WHERE parent_id IS NULL
            UNION ALL
            SELECT e.id, t.root_id FROM episodes e JOIN tree t ON e.parent_id = t.id
        ) SELECT id, root_id FROM tree""").fetchall()
    return {eid: root for eid, root in rows}


def _sessions(conn, since: float, now: float, root_of: dict[str, str]):
    roots = conn.execute(
        "SELECT id, session_key, started_at FROM episodes "
        "WHERE parent_id IS NULL AND session_key IS NOT NULL "
        "AND started_at >= %s AND started_at <= %s ORDER BY started_at, id",
        (since, now)).fetchall()
    by_root = {rid: _Session([rid], {key}, root_kind(key), started)
               for rid, key, started in roots}
    by_key = {key: rid for rid, key, _ in roots}
    pruned_search_keys: set[str] = set()
    pruned_outcome_episodes: set[str] = set()

    def by_episode(episode_id):
        return by_root.get(root_of.get(episode_id)) if episode_id else None

    def by_event(session_id, episode_id):
        # A search row's episode_id is the daemon's process-wide current
        # episode, not the caller's; its session_id is the caller's own.
        if session_id:
            if session_id in by_key:
                return by_root[by_key[session_id]]
            return None
        return by_episode(episode_id)

    for episode_id, source, ts in conn.execute(
            "SELECT episode_id, source, ts FROM entries "
            "WHERE episode_id IS NOT NULL AND ts >= %s", (since,)).fetchall():
        s = by_episode(episode_id)
        if s:
            s.total += 1
            s.substantive += source not in NON_SUBSTANTIVE_SOURCES
    for episode_id, origin in conn.execute(
            "SELECT episode_id, origin FROM outcome_signals "
            "WHERE episode_id IS NOT NULL AND created_at >= %s", (since,)).fetchall():
        s = by_episode(episode_id)
        if s is None:
            if episode_id not in root_of:        # its episode was pruned
                pruned_outcome_episodes.add(episode_id)
            continue
        if origin == "inferred":
            s.inferred += 1
        else:
            s.explicit += 1
    known_keys = set(conn.execute(
        "SELECT session_key FROM episodes WHERE session_key IS NOT NULL").fetchall())
    known_keys = {k for (k,) in known_keys}
    for session_id, episode_id, created in conn.execute(
            "SELECT session_id, episode_id, created_at FROM retrieval_events "
            "WHERE created_at >= %s", (since,)).fetchall():
        s = by_event(session_id, episode_id)
        if s:
            s.searches.append(created)
        elif session_id and session_id not in known_keys:
            pruned_search_keys.add(session_id)
    for session_id, episode_id in conn.execute(
            "SELECT e.session_id, e.episode_id FROM retrieval_uses u "
            "JOIN retrieval_events e ON e.id = u.event_id "
            "WHERE u.used_via = 'outcome' AND u.created_at >= %s", (since,)).fetchall():
        s = by_event(session_id, episode_id)
        if s:
            s.credited_ids += 1

    ordered = list(by_root.values())
    hooks = [s for s in ordered if s.kind == "hook"]
    active_shims = [s for s in ordered if s.kind != "hook" and s.active]
    # Any hook root may pair: a session that passes `episode=` writes to the
    # hook root while its searches carry the shim's key, so both are active.
    near = {id(h): [s for s in active_shims
                    if abs(s.started_at - h.started_at) <= PAIR_WINDOW_S]
            for h in hooks}
    merged: set[int] = set()
    merged_pairs = ambiguous = 0
    for h in hooks:
        candidates = near.get(id(h), [])
        if not candidates:
            continue
        if len(candidates) == 1 and sum(
                any(x is candidates[0] for x in c) for c in near.values()) == 1:
            shim = candidates[0]
            h.roots += shim.roots
            h.keys |= shim.keys
            h.started_at = min(h.started_at, shim.started_at)
            for name in ("substantive", "total", "explicit", "inferred", "credited_ids"):
                setattr(h, name, getattr(h, name) + getattr(shim, name))
            h.searches += shim.searches
            merged.add(id(shim))
            merged_pairs += 1
        else:
            ambiguous += 1
    sessions = hooks + [s for s in active_shims if id(s) not in merged]
    roots_report = {
        "keyed_in_window": len(ordered),
        "hook": len(hooks),
        "shim": sum(s.kind == "shim" for s in ordered),
        "other": sum(s.kind == "other" for s in ordered),
        "idle_shim_dropped": sum(s.kind != "hook" and not s.active for s in ordered),
        "merged_pairs": merged_pairs,
        "ambiguous_pairs": ambiguous,
        # Sessions whose root the daemon deleted (a root that holds no
        # entry is pruned when the session ends) but whose activity
        # remains; they are NOT in `sessions`. One session may show under
        # both counts.
        "pruned_with_searches": len(pruned_search_keys),
        "pruned_with_outcomes": len(pruned_outcome_episodes),
    }
    return sessions, roots_report


def _reuse(conn, since: float, now: float, root_of: dict[str, str]) -> dict:
    """Substantive entries whose 14-day window has closed, and how many a
    DIFFERENT session's search served within 14 days of being stored."""
    entries = conn.execute(
        "SELECT id, episode_id, ts FROM entries WHERE ts >= %s AND ts <= %s "
        "AND source NOT IN ('status', 'log')",
        (since, now - REUSE_WINDOW_S)).fetchall()
    if not entries:
        return {"entries": 0, "retrieved_again": 0, "rate": None, "unattributable": 0}
    key_of_root = dict(conn.execute(
        "SELECT id, session_key FROM episodes WHERE parent_id IS NULL").fetchall())
    served: dict[int, list[tuple[float, str | None, str | None]]] = {}
    for entry_id, session_id, episode_id, created in conn.execute("""
            SELECT (item->>'entry_id')::bigint, e.session_id, e.episode_id, e.created_at
            FROM retrieval_events e, jsonb_array_elements(e.served) item
            WHERE e.created_at >= %s AND item ? 'entry_id'""", (since,)).fetchall():
        served.setdefault(int(entry_id), []).append((created, session_id, episode_id))
    again = ambiguous = 0
    for entry_id, episode_id, ts in entries:
        own_root = root_of.get(episode_id) if episode_id else None
        own_key = key_of_root.get(own_root)
        verdicts = set()
        for created, session_id, ev_episode in served.get(entry_id, ()):
            if not ts < created <= ts + REUSE_WINDOW_S:
                continue
            # Only the caller's session key identifies who searched; a row's
            # episode is the daemon's current one, and a pruned root has no
            # key left to compare.
            if session_id and own_key:
                verdicts.add("other" if session_id != own_key else "own")
            else:
                verdicts.add("unknown")
        if "other" in verdicts:
            again += 1
        elif "unknown" in verdicts:
            ambiguous += 1
    return {"entries": len(entries), "retrieved_again": again,
            "rate": _rate(again, len(entries)),
            "unattributable": ambiguous}


def collect_from(conn, since_epoch: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    root_of = _root_of(conn)
    sessions, roots = _sessions(conn, since_epoch, now, root_of)
    working = [s for s in sessions if s.active]
    substantive = [s for s in sessions if s.substantive]
    with_outcome = [s for s in sessions if s.explicit or s.inferred]
    early = [s for s in sessions
             if any(t <= s.started_at + EARLY_SEARCH_S for t in s.searches)]
    credited = [s for s in with_outcome if s.credited_ids]
    mix = conn.execute(
        "SELECT COALESCE(origin, '') = 'inferred' AS inferred, outcome, COUNT(*) "
        "FROM outcome_signals WHERE created_at >= %s GROUP BY 1, 2 ORDER BY 1, 2",
        (since_epoch,)).fetchall()
    outcome_mix = {("inferred" if inferred else "explicit", outcome): int(n)
                   for inferred, outcome, n in mix}
    total_outcomes = sum(outcome_mix.values()) or 1
    neg = sum(n for (_, o), n in outcome_mix.items() if o in ("failure", "correction"))
    store_counts = [float(s.substantive) for s in sessions]

    def share(group):
        return {"n": len(group), "of_sessions": _rate(len(group), len(sessions)),
                "of_working": _rate(len(group), len(working))}

    return {
        "sessions": len(sessions),
        "working_sessions": len(working),
        "roots": roots,
        "substantive_sessions": len(substantive),
        "capture_coverage": (round(len(substantive) / len(sessions), 3)
                             if sessions else None),
        "outcome_coverage_of_substantive": (
            round(sum(1 for s in substantive if s.explicit or s.inferred)
                  / len(substantive), 3) if substantive else None),
        "loop": {
            "searched_early": share(early),
            "outcome_coverage": share(with_outcome),
            "lesson_search_used": None,
            "used_ids": {
                "sessions_with_credited_ids": len(credited),
                "of_sessions_with_outcome": _rate(len(credited), len(with_outcome)),
                "credited_ids": sum(s.credited_ids for s in sessions),
                "unmatched_ids": None,
            },
        },
        "reuse_14d": _reuse(conn, since_epoch, now, root_of),
        "median_stores": _percentile(store_counts, 0.5),
        "p90_stores": round(_percentile(store_counts, 0.9), 1),
        "outcome_mix": {f"{k[0]}.{k[1]}": v for k, v in sorted(outcome_mix.items())},
        "failure_correction_share": round(neg / total_outcomes, 3),
        "not_recorded": dict(NOT_RECORDED),
        "definitions": {
            "pair_window_s": PAIR_WINDOW_S, "early_search_s": EARLY_SEARCH_S,
            "reuse_window_s": REUSE_WINDOW_S},
    }


def collect(since_epoch: float, *, dsn: str | None = None,
            now: float | None = None) -> dict:
    import psycopg

    with psycopg.connect(dsn or DSN, connect_timeout=10,
                         options="-c default_transaction_read_only=on") as conn:
        return collect_from(conn, since_epoch, now=now)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", default="2026-06-27",
                    help="UTC date (YYYY-MM-DD) the window starts at")
    args = ap.parse_args(argv)
    since_epoch = datetime.strptime(args.since, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp()
    stats = collect(since_epoch)
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    r, loop = stats["roots"], stats["loop"]
    print(f"\nmemory-loop capture metrics (since {args.since})")
    print(f"{'root episodes with a session key':<40}{r['keyed_in_window']:>8}")
    print(f"{'  hook / shim / other':<40}{r['hook']:>8} / {r['shim']} / {r['other']}")
    print(f"{'  idle shim roots dropped':<40}{r['idle_shim_dropped']:>8}")
    print(f"{'  hook+shim pairs merged':<40}{r['merged_pairs']:>8}")
    print(f"{'  ambiguous pairs (upper bound)':<40}{r['ambiguous_pairs']:>8}")
    print(f"{'client sessions (surviving roots)':<40}{stats['sessions']:>8}")
    print(f"{'  + pruned, with searches / outcomes':<40}{r['pruned_with_searches']:>8} / "
          f"{r['pruned_with_outcomes']}   (not in the rates below)")
    print(f"{'  working (>=1 memory interaction)':<40}{stats['working_sessions']:>8}")
    print(f"{'  with >=1 substantive store':<40}{stats['substantive_sessions']:>8}"
          f"   ({_pct(stats['capture_coverage'])})")
    for label, key in (("searched within 15 min", "searched_early"),
                       ("logged an outcome", "outcome_coverage")):
        m = loop[key]
        print(f"{'  ' + label:<40}{m['n']:>8}   "
              f"({_pct(m['of_sessions'])} of sessions, {_pct(m['of_working'])} of working)")
    u = loop["used_ids"]
    print(f"{'  outcome credited used_ids':<40}{u['sessions_with_credited_ids']:>8}"
          f"   ({_pct(u['of_sessions_with_outcome'])} of sessions with an outcome;"
          f" {u['credited_ids']} ids credited)")
    reuse = stats["reuse_14d"]
    print(f"{'entries retrieved again within 14 d':<40}{reuse['retrieved_again']:>8}"
          f"   of {reuse['entries']} ({_pct(reuse['rate'])}; "
          f"{reuse['unattributable']} served to an unidentifiable caller)")
    print(f"{'outcome coverage (substantive)':<40}"
          f"{_pct(stats['outcome_coverage_of_substantive']):>8}")
    print(f"{'median / p90 stores per session':<40}"
          f"{stats['median_stores']:>5.1f} / {stats['p90_stores']}")
    print(f"{'failure+correction share':<40}{stats['failure_correction_share']:>8.0%}")
    print("outcome mix:")
    for k, v in stats["outcome_mix"].items():
        print(f"    {k:<28}{v:>6}")
    print("not recorded by the bank:")
    for k, v in stats["not_recorded"].items():
        print(f"    {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
