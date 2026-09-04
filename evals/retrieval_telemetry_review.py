"""Learned-reranker telemetry audit: is there enough labelled signal yet?

PR #168 shipped the (query, served) half of the training tuple
(``retrieval_events``) plus implicit relevance labels (``retrieval_uses``:
a ``memory_get`` / ``memory_reinforce`` on a served entry credits the most
recent in-session serving event inside ``use_window_seconds``). PR #200/#201
added the read-side counters (``slot_reads``, ``served_facts``,
``entries.explicit_reinforcements``, ``graduation_candidates``).

This script answers the only question that matters for Phase 1: how many
LABELLED events exist, not how many events exist. It reads one restored
copy of the bank read-only and writes an aggregate artifact.

Signal taxonomy — the distinction the counters do NOT make for you:

* ``retrieval_uses``           CONSUMPTION. Written only when a served
                              entry is later dereferenced or reinforced.
* ``entries.explicit_reinforcements``
                              CONSUMPTION. Moves only on
                              ``memory_reinforce`` (service.py reinforce).
* ``entries.access_count``     SERVE count. ``cms.py`` bumps it for every
                              entry in a merged result set, so it is NOT a
                              downstream-read signal — treating it as one
                              would label ~75% of the bank as "read".
* ``slot_reads.read_count``    SERVE count on the cortex side
                              (``_track_slot_reads``: "count each slot
                              SERVED as an answer").

PRIVACY: query text and entry text are private (public repo). The artifact
carries aggregates and ids only — never a query string or an entry body.

    python evals/retrieval_telemetry_review.py \
        --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD \
        --out evals/results/retrieval-telemetry-review-YYYYMMDD.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).resolve().parent / "results"
# Never audit the live bank or the shared bench DB: the bench harnesses
# truncate the latter, and read-only intent is not enforceable from here.
FORBIDDEN_DBS = {"pseudolife_memory", "pseudolife_memory_bench"}


def guard_dsn(dsn: str) -> None:
    """Refuse the live and shared-bench banks by name, in either DSN
    spelling libpq accepts and regardless of case.

    The 2026-09-04 pre-merge review found the original matched only a
    lower-case URI path segment: ``dbname=pseudolife_memory``, a trailing
    slash, and an upper-cased name each walked through onto the live bank.
    """
    text = re.sub(r"\?.*$", "", dsn.strip())
    names = {text.rstrip("/").rsplit("/", 1)[-1].lower()}
    names.update(m.group(1).lower() for m in re.finditer(
        r"\bdbname\s*=\s*['\"]?([^\s'\"]+)", text, re.IGNORECASE))
    hit = sorted(names & {d.lower() for d in FORBIDDEN_DBS})
    if hit:
        sys.exit(f"refusing to run against {hit[0]!r} — restore a dedicated "
                 "replay copy instead (see the module docstring)")


# ══════════════════════════════════════════════════════════════════════════
# pure summarisers (unit-tested on fixtures; no DB)
# ══════════════════════════════════════════════════════════════════════════

def day_of(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d")


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-day counts, session/episode spread, served-length distribution
    and column coverage over ``retrieval_events`` rows.

    Each row: id, query_text (unused — private), session_id, episode_id,
    served (list), served_facts (list|None), params (dict|None), created_at.
    """
    by_day: dict[str, dict[str, Any]] = {}
    lens: Counter[int] = Counter()
    sessions, episodes = set(), set()
    n_params = n_facts = 0
    n_served_facts_rows = 0
    for e in events:
        d = by_day.setdefault(day_of(e["created_at"]),
                              {"events": 0, "sessions": set()})
        d["events"] += 1
        if e.get("session_id"):
            d["sessions"].add(e["session_id"])
            sessions.add(e["session_id"])
        if e.get("episode_id"):
            episodes.add(e["episode_id"])
        lens[len(e.get("served") or [])] += 1
        if e.get("params") is not None:
            n_params += 1
        sf = e.get("served_facts")
        if sf:
            n_served_facts_rows += 1
            n_facts += len(sf)
    n = len(events)
    return {
        "n_events": n,
        "by_day": [{"day": k, "events": v["events"],
                    "distinct_sessions": len(v["sessions"])}
                   for k, v in sorted(by_day.items())],
        "distinct_sessions": len(sessions),
        "distinct_episodes": len(episodes),
        "served_len_distribution": {str(k): v for k, v in sorted(lens.items())},
        "zero_result_events": lens.get(0, 0),
        "mean_served_len": (round(sum(k * v for k, v in lens.items()) / n, 3)
                            if n else 0.0),
        "params_coverage": {"rows": n_params,
                            "pct": round(100.0 * n_params / n, 2) if n else 0.0},
        "served_facts_coverage": {
            "rows": n_served_facts_rows,
            "pct": round(100.0 * n_served_facts_rows / n, 2) if n else 0.0,
            "total_facts_served": n_facts,
            "mean_facts_per_covered_row": (
                round(n_facts / n_served_facts_rows, 3)
                if n_served_facts_rows else 0.0),
        },
    }


def summarize_uses(events: list[dict[str, Any]],
                   uses: list[dict[str, Any]]) -> dict[str, Any]:
    """What the implicit labels actually credited: the served rank of each
    used entry, the via channel, and the event→use latency."""
    by_event = {e["id"]: e for e in events}
    rows = []
    ranks: Counter[str] = Counter()
    for u in uses:
        ev = by_event.get(u["event_id"])
        rank = None
        if ev:
            for s in ev.get("served") or []:
                if int(s.get("entry_id", -1)) == int(u["entry_id"]):
                    rank = int(s.get("rank", -1))
                    break
        ranks["rank_%s" % ("miss" if rank is None else rank)] += 1
        rows.append({
            "event_id": u["event_id"],
            "entry_id": u["entry_id"],
            "used_via": u["used_via"],
            "served_rank": rank,
            "served_len": len(ev.get("served") or []) if ev else None,
            "latency_s": (round(float(u["created_at"]) - float(ev["created_at"]), 1)
                          if ev else None),
        })
    return {
        "n_uses": len(uses),
        "by_via": dict(Counter(u["used_via"] for u in uses)),
        "served_rank_histogram": dict(ranks),
        "detail": rows,
    }


def summarize_labels(events: list[dict[str, Any]],
                     uses: list[dict[str, Any]],
                     entry_signal: dict[int, dict[str, int]]) -> dict[str, Any]:
    """How many events carry ANY downstream signal, and how often the
    served top-1 / top-3 was ever consumed.

    ``entry_signal`` maps entry_id -> {"access_count", "explicit_reinforcements",
    "exists"}. Only ``explicit_reinforcements`` (and the use rows) are
    consumption; ``access_count`` is reported for contrast, never as a label.
    """
    used_events = {u["event_id"] for u in uses}
    reinforced = {eid for eid, s in entry_signal.items()
                  if s.get("explicit_reinforcements", 0) > 0}
    n_any = n_top1 = n_top3 = 0
    n_top1_present = n_top3_present = 0
    survivors = dangling = 0
    for e in events:
        served = sorted(e.get("served") or [],
                        key=lambda s: int(s.get("rank", 0)))
        ids = [int(s["entry_id"]) for s in served if s.get("entry_id") is not None]
        for i in ids:
            if entry_signal.get(i, {}).get("exists"):
                survivors += 1
            else:
                dangling += 1
        has_use = e["id"] in used_events
        has_reinf = any(i in reinforced for i in ids)
        if has_use or has_reinf:
            n_any += 1
        if ids:
            n_top1_present += 1
            if ids[0] in reinforced or any(
                    u["event_id"] == e["id"] and int(u["entry_id"]) == ids[0]
                    for u in uses):
                n_top1 += 1
        if ids[:3]:
            n_top3_present += 1
            top3 = set(ids[:3])
            if (top3 & reinforced) or any(
                    u["event_id"] == e["id"] and int(u["entry_id"]) in top3
                    for u in uses):
                n_top3 += 1
    n = len(events)
    return {
        "events_with_any_downstream_signal": n_any,
        "events_with_any_downstream_signal_pct": (
            round(100.0 * n_any / n, 3) if n else 0.0),
        "top1_consumed": n_top1,
        "top1_consumed_pct_of_events_with_a_top1": (
            round(100.0 * n_top1 / n_top1_present, 3) if n_top1_present else 0.0),
        "top3_consumed": n_top3,
        "top3_consumed_pct_of_events_with_a_top3": (
            round(100.0 * n_top3 / n_top3_present, 3) if n_top3_present else 0.0),
        "served_id_join": {
            "served_ids_still_in_entries": survivors,
            "served_ids_dangling": dangling,
        },
        "note": ("consumption = a retrieval_uses row or "
                 "entries.explicit_reinforcements > 0; entries.access_count "
                 "is a SERVE counter (cms.py bumps it on every merged result "
                 "set) and is deliberately excluded"),
    }


def verdict(labels: dict[str, Any], n_events: int,
            phase1_target: int = 300) -> dict[str, Any]:
    """The Phase-1 go/no-go. The plan's 'a few hundred logged events' is
    read here as LABELLED events — an unlabelled event is an input with no
    target and trains nothing."""
    n_lab = labels["events_with_any_downstream_signal"]
    return {
        "phase1_labelled_event_target": phase1_target,
        "labelled_events": n_lab,
        "logged_events": n_events,
        "trainable": n_lab >= phase1_target,
        "shortfall": max(0, phase1_target - n_lab),
    }


# ══════════════════════════════════════════════════════════════════════════
# DB read
# ══════════════════════════════════════════════════════════════════════════

def fetch(dsn: str) -> dict[str, Any]:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, session_id, episode_id, served, served_facts, "
            "params, created_at FROM retrieval_events ORDER BY id")
        cols = ("id", "session_id", "episode_id", "served", "served_facts",
                "params", "created_at")
        events = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("SELECT event_id, entry_id, used_via, created_at "
                    "FROM retrieval_uses ORDER BY event_id")
        uses = [dict(zip(("event_id", "entry_id", "used_via", "created_at"), r))
                for r in cur.fetchall()]
        cur.execute("SELECT id, access_count, explicit_reinforcements "
                    "FROM entries")
        entry_signal = {int(r[0]): {"exists": 1, "access_count": int(r[1] or 0),
                                    "explicit_reinforcements": int(r[2] or 0)}
                        for r in cur.fetchall()}
        cur.execute("SELECT count(*), COALESCE(sum(read_count), 0), "
                    "count(*) FILTER (WHERE read_count > 0) FROM slot_reads")
        sr = cur.fetchone()
        cur.execute("SELECT count(*) FROM facts WHERE status = 'current'")
        n_current_facts = cur.fetchone()[0]
        cur.execute("SELECT count(*), COALESCE(sum(access_count), 0), "
                    "COALESCE(sum(explicit_reinforcements), 0) FROM entries")
        ent = cur.fetchone()
    return {
        "events": events, "uses": uses, "entry_signal": entry_signal,
        "bank": {
            "entries": int(ent[0]),
            "entries_access_count_total": int(ent[1]),
            "entries_explicit_reinforcements_total": int(ent[2]),
            "current_facts": int(n_current_facts),
            "slot_reads_rows": int(sr[0]),
            "slot_reads_total": int(sr[1]),
            "slot_reads_rows_nonzero": int(sr[2]),
        },
    }


def build_report(raw: dict[str, Any], dsn_db: str,
                 phase1_target: int) -> dict[str, Any]:
    events, uses = raw["events"], raw["uses"]
    ev = summarize_events(events)
    us = summarize_uses(events, uses)
    lb = summarize_labels(events, uses, raw["entry_signal"])
    return {
        "generated_for": "learned-reranker Phase 1 go/no-go",
        "source_db": dsn_db,
        "read_only": True,
        "bank": raw["bank"],
        "events": ev,
        "uses": us,
        "labels": lb,
        "verdict": verdict(lb, ev["n_events"], phase1_target),
        "signal_taxonomy": {
            "consumption": ["retrieval_uses",
                            "entries.explicit_reinforcements"],
            "serve_count_only": ["entries.access_count (cms.py:1398, bumped "
                                 "for every entry in a merged result set)",
                                 "slot_reads.read_count (service.py "
                                 "_track_slot_reads: 'count each slot SERVED "
                                 "as an answer')"],
        },
        "privacy": "aggregates and ids only; no query or entry text",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--phase1-target", type=int, default=300)
    ap.add_argument("--out", default=str(
        RESULTS / "retrieval-telemetry-review.json"))
    args = ap.parse_args(argv)
    guard_dsn(args.dsn)
    db = re.sub(r"\?.*$", "", args.dsn).rsplit("/", 1)[-1]
    report = build_report(fetch(args.dsn), db, args.phase1_target)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
