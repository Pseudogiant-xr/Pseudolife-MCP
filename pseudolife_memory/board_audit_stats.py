"""Coordination telemetry from the board's own records (``board-audit stats``).

Pure functions over three inputs: exported audit events (the shape
``board-audit export`` writes and ``_read_export`` reads: the row's columns
with the payload parsed), rows of the v49 ``coordination_wakes`` table, and
the suite lock's ``full-suite.durations.jsonl`` records. ``compute_stats``
turns them into one JSON-ready report for a time window; nothing here reads
a database or a file.

The report is telemetry, not evidence: it leaves the tool with counts,
seconds, decision and reason names, principal names and version strings.
No message body, no agent id (not even a prefix), no path, no worktree and no
user name is copied from any input. ``verify`` and ``export`` remain the
records; this is what a maintainer reads to see whether mail moves, wakes
land and parks clear.

Every window statistic is "as of ``until``": a send still unread then counts
as unread, a park still standing as open, a ring not yet served as never
served. Events before ``since`` are read only to know which parks stood or
had lapsed when the window opened; a park set before the window is not
counted, even when it clears inside it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math

# The one report shape this module writes; a change to the keys bumps it.
REPORT_SHAPE = 1
# Every wake decision the daemon can make at send (v49), in the order the
# report lists them; a send from before v49 has none and reads ``unknown``.
WAKE_DECISIONS = ("rung", "nudged", "hinted", "withheld", "no_path", "capped", "not_needed",
                  "unknown")
# The decisions that ring a shim, and so have a row in ``coordination_wakes``.
RING_DECISIONS = ("rung", "nudged")
# How soon after a ring the recipient must act on the board for the ring to
# count as precise: the Stop hook fires under a second after the marker, and
# the woken turn's first board call (receive) follows within its first tool
# calls; two minutes covers a slow start and excludes the next natural turn.
PRECISION_WINDOW = 120.0
# A woke marker later than this after a ring was served answers some other
# ring: the hook waits at most an hour.
WOKE_WINDOW = 3600.0
# The model's own board actions: what a woken session does when the wake
# landed. The adapter's own attach, detach, delivery reads (a live channel
# pushing mail in logs ``read`` with ``path: delivery``) and the woke marker
# itself are left out, as they say nothing about whether the turn used the
# mail.
ACTED_EVENTS = frozenset({"update", "read", "ack", "send", "register"})
# How far before ``since`` the caller reads events for the park outcomes: a
# park can stand at most this long (the daemon's PARK_MAX_TTL), so a park
# lapsed or still standing at ``since`` is seen, and the next park is not
# mistaken for a refinement of it.
PARK_LOOKBACK = 7 * 86400


def _percentiles(values):
    """Nearest-rank p50 and p95 of ``values`` (seconds), rounded to a tenth;
    ``None`` for each when there are none."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None}

    def rank(share):
        return ordered[max(0, math.ceil(share * len(ordered)) - 1)]
    return {"n": len(ordered), "p50": round(rank(0.5), 1), "p95": round(rank(0.95), 1)}


def _share(part, whole):
    return round(part / whole, 3) if whole else None


def _stamp(value):
    """An ISO 8601 stamp (as the suite lock writes them) as epoch seconds, or
    ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # the lock writes local time with an offset; be lenient
    return moment.timestamp()


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _in_window(moment, since, until):
    return moment is not None and since <= moment < until


def mail_latency(events, *, since, until, principals):
    """Send to first read, per wake decision and per recipient principal.

    A send is the ``send`` event (one per recipient, v48); its first read is
    the ``read`` event with the same message id, which the daemon logs once.
    The decision comes from the send payload's ``wake`` (v49; ``unknown``
    before it). The recipient's principal is whatever principal the log saw
    act under that address in the window, else ``unknown``."""
    first_read = {}
    for event in events:
        if event["event"] == "read" and event.get("message_id"):
            first_read.setdefault(event["message_id"], event["created_at"])
    by_decision = {name: [] for name in WAKE_DECISIONS}
    by_principal = defaultdict(list)
    sends = 0
    for event in events:
        if event["event"] != "send" or not _in_window(event["created_at"], since, until):
            continue
        sends += 1
        read_at = first_read.get(event.get("message_id"))
        latency = (read_at - event["created_at"]) if read_at is not None else None
        decision = (event.get("payload") or {}).get("wake")
        if decision not in by_decision:
            decision = "unknown"
        by_decision[decision].append(latency)
        by_principal[principals.get(event.get("recipient_agent_id"), "unknown")].append(latency)

    def summary(latencies):
        read = [v for v in latencies if v is not None]
        return {"sends": len(latencies), "read": len(read), "unread": len(latencies) - len(read),
                "seconds": _percentiles(read)}
    every = [v for values in by_decision.values() for v in values]
    return {**summary(every),
            "by_decision": {name: summary(values) for name, values in by_decision.items()},
            "by_principal": {name: summary(by_principal[name]) for name in sorted(by_principal)}}


def wake_precision(wakes, events, *, since, until):
    """How many rings landed: for each ``rung`` or ``nudged`` row decided in
    the window, whether it was served to the recipient's adapter, whether a
    ``woke`` marker followed (the Stop hook fired, the turn started) and
    whether the recipient then acted on the board within PRECISION_WINDOW
    of being served and of waking."""
    by_agent = defaultdict(list)
    woke_at = defaultdict(list)
    for event in events:
        agent = event.get("agent_id")
        if not agent:
            continue
        if event["event"] == "woke":
            woke_at[agent].append(event["created_at"])
        elif event["event"] in ACTED_EVENTS and not (
                event["event"] == "read" and (event.get("payload") or {}).get("path") == "delivery"):
            by_agent[agent].append(event["created_at"])
    for moments in (*by_agent.values(), *woke_at.values()):
        moments.sort()

    def acted(agent, anchor):
        return any(anchor < moment <= anchor + PRECISION_WINDOW for moment in by_agent[agent])

    def first_woke(agent, served):
        return next((m for m in woke_at[agent] if served <= m <= served + WOKE_WINDOW), None)

    rows = {name: [] for name in RING_DECISIONS}
    for wake in wakes:
        created = _seconds(wake.get("created_at"))
        decision = wake.get("decision")
        if decision not in rows or not _in_window(created, since, until):
            continue
        served = _seconds(wake.get("served_at"))
        agent = wake.get("recipient_agent_id")
        woke = first_woke(agent, served) if served is not None else None
        rows[decision].append({
            "served": served is not None,
            "seconds_to_served": (served - created) if served is not None else None,
            "acted_after_served": served is not None and acted(agent, served),
            "woke": woke is not None,
            "seconds_served_to_woke": (woke - served) if woke is not None else None,
            "acted_after_woke": woke is not None and acted(agent, woke),
        })

    def summary(items):
        served = [i for i in items if i["served"]]
        woke = [i for i in items if i["woke"]]
        return {
            "rings": len(items), "served": len(served), "never_served": len(items) - len(served),
            "share_never_served": _share(len(items) - len(served), len(items)),
            "seconds_to_served": _percentiles([i["seconds_to_served"] for i in served]),
            "acted_after_served": sum(i["acted_after_served"] for i in served),
            "share_acted_after_served": _share(sum(i["acted_after_served"] for i in served),
                                               len(served)),
            "woke": len(woke), "share_woke": _share(len(woke), len(served)),
            "seconds_served_to_woke": _percentiles([i["seconds_served_to_woke"] for i in woke]),
            "acted_after_woke": sum(i["acted_after_woke"] for i in woke),
            "share_acted_after_woke": _share(sum(i["acted_after_woke"] for i in woke), len(woke)),
        }
    return {**summary([i for items in rows.values() for i in items]),
            "by_decision": {name: summary(items) for name, items in rows.items()}}


def park_outcomes(events, *, since, until):
    """Parks set in the window, by reason; how long each stood, and how it
    ended: ``send`` when a ring (``rung``) reached the agent between the park
    and its clearing update, ``owner_update`` when the agent cleared it with
    no ring in between (a plain status, or a null reason), ``expiry`` when
    ``park_expires`` passed before any clearing and before ``until``, and
    ``open`` when it still stood at ``until``.

    Read as the daemon applies ``update`` (storage ``CoordinationStore.update``):
    a park is an update setting ``park_reason`` while no park stands; one
    setting it, or any other park field, while a park stands refines that
    park (its expiry moves only when the update names one). A lapsed park
    stays on the row, so the next park's ``before`` still names a reason: it
    is a new park all the same, and the lapsed one ended at its expiry. A
    new park that names no expiry carries the standing (lapsed) one, as the
    daemon does. ``events`` should reach PARK_LOOKBACK before ``since`` so
    parks standing or lapsed at ``since`` are known; a row that names a park
    the events never showed is left alone."""
    standing = {}
    parks = []
    rings = defaultdict(list)
    for event in events:
        moment = event["created_at"]
        if event["event"] == "send" and (event.get("payload") or {}).get("wake") == "rung":
            rings[event.get("recipient_agent_id")].append(moment)
        if event["event"] != "update":
            continue
        payload = event.get("payload") or {}
        fields, before = payload.get("fields") or {}, payload.get("before") or {}
        if not any(key.startswith("park_") for key in fields):
            continue
        agent = event.get("agent_id")
        park = standing.get(agent)
        carried = None
        if park is not None and park["expires"] is not None and park["expires"] <= moment:
            # It lapsed before this update: it ended at its expiry.
            park["ended"], park["how"] = park["expires"], "expiry"
            carried = standing.pop(agent)["expires"]
            park = None
        if "park_reason" in fields and fields["park_reason"] is None:
            if park is not None:
                park["ended"], park["how"] = moment, None  # send or owner_update, below
                standing.pop(agent)
            continue
        if park is not None:
            if "park_expires" in fields:
                park["expires"] = _seconds(fields["park_expires"])
            continue
        if "park_reason" not in fields:
            continue  # a refinement of a park the events never showed
        if carried is None and before.get("park_reason") is not None:
            continue  # the row names a park the events never showed
        expires = (_seconds(fields["park_expires"]) if "park_expires" in fields else carried)
        park = {"reason": fields["park_reason"], "set": moment, "agent": agent,
                "expires": expires, "ended": None, "how": None}
        standing[agent] = park
        if _in_window(moment, since, until):
            parks.append(park)
    for park in parks:
        if park["ended"] is None:
            if park["expires"] is not None and park["expires"] < until:
                park["ended"], park["how"] = park["expires"], "expiry"
            else:
                park["how"] = "open"
        elif park["how"] is None:
            rung = any(park["set"] < m <= park["ended"] for m in rings[park["agent"]])
            park["how"] = "send" if rung else "owner_update"

    def summary(items):
        cleared = [p for p in items if p["how"] in ("send", "owner_update")]
        return {"parks": len(items),
                "cleared": {how: sum(p["how"] == how for p in items)
                            for how in ("send", "owner_update", "expiry", "open")},
                "seconds_to_clear": _percentiles([p["ended"] - p["set"] for p in cleared])}
    reasons = sorted({p["reason"] for p in parks})
    return {**summary(parks),
            "by_reason": {reason: summary([p for p in parks if p["reason"] == reason])
                          for reason in reasons}}


def sends_per_session_hour(events, *, since, until, principals):
    """Sends in the window over attached session-hours, by principal: an
    active-session proxy for how much organic communication the board
    carries. A session runs from an ``attach`` that is not a renewal to the
    same address's ``detach``, or to a ``prune`` that removes the address,
    or to ``until``; clipped to the window. Heartbeats are not logged, so an
    address attached before the window with no fresh attach in it counts no
    hours, though its sends still count."""
    opened = {}
    hours = defaultdict(float)

    def close(agent, moment):
        start = opened.pop(agent, None)
        if start is not None and moment > start:
            hours[principals.get(agent, "unknown")] += (moment - start) / 3600
    for event in events:
        moment = event["created_at"]
        if moment >= until:
            break
        agent = event.get("agent_id")
        kind = event["event"]
        payload = event.get("payload") or {}
        if kind == "attach" and not payload.get("renewed") and agent not in opened:
            opened[agent] = max(moment, since)
        elif kind == "detach":
            close(agent, moment)
        elif kind == "prune":
            for gone in payload.get("agent_ids") or ():
                close(gone, moment)
    for agent in list(opened):
        close(agent, until)
    sends = Counter(event["principal"] or "unknown" for event in events
                    if event["event"] == "send" and _in_window(event["created_at"], since, until))

    def summary(count, span):
        return {"sends": count, "session_hours": round(span, 2),
                "rate": round(count / span, 2) if span else None}
    names = sorted(set(sends) | set(hours))
    return {**summary(sum(sends.values()), sum(hours.values())),
            "by_principal": {name: summary(sends.get(name, 0), hours.get(name, 0.0))
                             for name in names}}


def suite_lock_times(durations, *, since, until):
    """Queue wait and hold of the full-suite runs whose ``ended`` stamp falls
    in the window, from the lock's durations file. The wait is ``started_at``
    minus ``queued_at`` where a record carries both (runs from before those
    stamps count in the hold only). The record's worktree is not read."""
    holds, waits = [], []
    for record in durations:
        if not isinstance(record, dict):
            continue
        ended = _stamp(record.get("ended"))
        if not _in_window(ended, since, until):
            continue
        hold = _seconds(record.get("seconds"))
        if hold is not None and hold >= 0:
            holds.append(hold)
        queued, started = _stamp(record.get("queued_at")), _stamp(record.get("started_at"))
        if queued is not None and started is not None and started >= queued:
            waits.append(started - queued)
    return {"runs": len(holds), "hold": _percentiles(holds), "queue_wait": _percentiles(waits)}


def agent_principals(events):
    """Which principal each address acted under, from every event in which it
    acted (the acting agent's principal); an address seen only as a
    recipient stays unknown."""
    principals = {}
    for event in events:
        if event.get("agent_id") and event.get("principal"):
            principals.setdefault(event["agent_id"], event["principal"])
    return principals


def compute_stats(events, wakes, durations, *, since, until, now=None, version="",
                  sources=None):
    """The whole report for ``[since, until)``. ``events`` are export rows in
    chain order, from PARK_LOOKBACK before ``since`` (only the park outcomes
    read that far back; every other figure reads the window) up to
    ``until``, later ones being dropped; ``wakes`` rows of
    ``coordination_wakes``; ``durations`` the lock's records. Nothing from
    the inputs but counts, seconds, decision, reason and principal names
    reaches the result."""
    events = [e for e in events if isinstance(e.get("created_at"), (int, float))
              and e["created_at"] < until]
    events.sort(key=lambda e: (e["created_at"], e.get("seq", 0)))
    wakes = list(wakes)
    principals = agent_principals(events)
    in_window = [e for e in events if e["created_at"] >= since]
    return {
        "report": "board-audit stats", "shape": REPORT_SHAPE, "version": version,
        "generated_at": round(now if now is not None else until, 3),
        "window": {"since": since, "until": until, "hours": round((until - since) / 3600, 3)},
        "sources": dict(sources or {}),
        "events": {"total": len(in_window),
                   "by_kind": dict(sorted(Counter(e["event"] for e in in_window).items()))},
        "mail_latency": mail_latency(in_window, since=since, until=until, principals=principals),
        "wake_precision": wake_precision(wakes, in_window, since=since, until=until),
        "park_outcomes": park_outcomes(events, since=since, until=until),
        "sends_per_session_hour": sends_per_session_hour(in_window, since=since, until=until,
                                                         principals=principals),
        "suite_lock": suite_lock_times(durations, since=since, until=until),
    }
