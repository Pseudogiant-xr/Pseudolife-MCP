"""Serving-policy replay: what would agents still get under a different
serving policy, scored against what they said they used.

Every ``memory_search`` writes one ``retrieval_events`` row (the ranked
served list, the knobs in force), and ``memory_outcome(used_ids=...)``
credits each id the agent names to every search in its session's window
that served it (``retrieval_uses``, ``used_via='outcome'``). This script
replays the LOGGED served lists under a candidate policy and answers one
question: what share of the hits agents actually used would the policy
still have served, and at what cost in rows and entry-text chars?

It is a pure log replay: no model, no daemon, no restored bank. A
policy is a narrower default ``top_k``, a fused-score floor or a source
exclusion. That makes it the gate for PROJECTION-side serving changes (MCP
defaults, payload shape, default-search exclusions), which no other
instrument sees:

* ``evals/regression_gate.ps1`` rebuilds contexts from bank dumps and
  judges answers; by its own header it does NOT exercise CMS candidate
  selection, ``MemoryService.search`` or MCP rendering.
* LongMemEval / BEAM call ``svc.search`` and ``svc.cortex_search``
  directly with their own ``top_k`` (``tests/test_agent_payload_budget.py``
  pins that), so an MCP default can never move them.

A narrower ``top_k`` is SIMULATED from the logged fusion inputs, not read
off as a prefix of the wider list, because the wider search is not a
superset in order. With the shipped shape (weighted-sum fusion, pool
multiplier 1, reranker off) ``cms.retrieve`` takes the dense pool as the
top ``top_k`` entries by COSINE, then applies the superseded (x0.55) and
assistant (x0.85) multipliers and the BM25 boost, then sorts and cuts. So
a width-8 list's first six rows can include an entry at cosine rank 7 that
a width-6 search never pools, and a width-6 search can serve a superseded
entry that width 8 pushed below its cut. ``simulate_width`` rebuilds the
width-K list from each served row's logged ``components``: dense rows at
cosine rank K or more leave the pool and come back only as a BM25-only
injection (``weight x normalised``, refused below a caller's explicit
floor) when they had a lexical score; slot hits past the K-th by slot
CONFIDENCE leave too (``_slot_query_pool`` cuts by confidence before the
BM25 boost reorders them); everything is re-sorted by score and cut to K.
While no more than K slot rows were served, no row the wider search did
not serve can enter the narrower list: at most W - K dense entries leave
the pool, and each unserved candidate had W rows above it. Three stated
gaps. (1) When more than K slot rows were served, the narrower slot pool
may hold candidates the wider search cut and never logged; those events
are counted (``events_slot_cap_bound``). (2) Dense-pool members the wider
search cut are not logged, so a served row's cosine rank can be
understated; ``top_k=6 (max demotion)`` is a sensitivity arm that assumes
every unlogged pool slot outranks every logged row. It is not a bound on
used hits kept: demoting more rows can also lift lower ones into the cut.
(3) A demoted entry could come back through the slot channel, which is not
modelled. Searches logged under another shape (several bands, a pool
multiplier, RRF, a fired reranker or timeline) fall back to the prefix
and are counted. ``top_k=6 (prefix)`` is the 2026-09-23 review's method, kept
for comparison. The artifact's ``width_simulation_check`` scores both
methods against REAL narrower searches: the same query text (compared in
SQL, never fetched) served at a wider and a narrower ``top_k`` in one
session within ten minutes, under the same filters and ranking knobs.
Pairs are drawn from the whole log, not the label window, and most of
them are the 2026-09-03/04 agent-token-ledger runs at 8 -> 3, so the check
validates the method at K = 3 more than at K = 6.

What it cannot do, and what therefore still needs another instrument:

* **Backfill.** Excluding a source frees slots that the real ranker would
  fill with candidates that were never served, so they carry no labels.
  The replay reports the loss without the backfill (a lower bound on what
  an exclusion keeps); a restored-bank replay through the real search
  path (``evals/retrieval_replay.py``) is needed for the rest.
* **Re-ranking.** Anything that changes ORDER — fusion weights, a
  reranker, recency — is out of scope; those go through the regression
  gate and the benchmarks.

Label caveats, read before trusting a number:

* Labels are self-reported and session-scoped: one named id credits every
  search in the session's window that served it (``credit_retrieval_uses``;
  most-recent-only before 2026-09-08). They are not per-query relevance.
* Position bias: agents read rank 0 first, so rank-based cuts are partly
  circular. Only a randomised-order check could separate the two.
* Selection: only sessions that log outcomes carry labels, and absolute use
  rates conditioned on "labelled events" are inflated. Compare policies,
  not absolute rates.
* A logged ``top_k`` of 8 cannot distinguish the MCP default from an
  explicit 8, so the default-width stratum is an upper bound on the reach
  of a default change.

Agent-origin filter (every step is counted in the artifact):

1. the window starts when outcome labels went live (the 2026-09-06 deploy;
   first label on an event at 11:20:59Z) — earlier events cannot be
   labelled;
2. the daemon's own startup probe (``MemoryService.warmup``) is dropped;
3. events with no session identity are dropped — REST callers and deploy
   probes, not MCP agent searches (every MCP shim session carries one);
4. hand-set probe sessions (any session id that is not a client-issued
   32-hex or UUID id) are dropped;
5. ``memory_recall`` fan-out: recall issues its searches back to back
   under one session, and never attaches the cortex block's served facts
   (only the MCP ``memory_search`` handler does). An event inside a run of
   ``--burst-min`` or more same-session events with gaps of at most
   ``--burst-gap`` seconds is dropped when it carries no served facts; a
   burst event WITH facts is a parallel ``memory_search`` (subagents share
   their parent's MCP session) and is kept. Both counts are reported. The
   Console's ``/api/search`` also attaches facts and is logged under the
   active agent's session, so its debounced typing can survive as
   "parallel searches"; it asks for ``top_k`` 25, so it never reaches the
   default-width stratum;
6. the 2026-09-23 fresh-eyes review's own traffic (``REVIEW_2026_09_23``,
   matched on id AND on the review's time window, so the ids cannot
   silently drop events from another bank).

PRIVACY: this is a public repository and real queries carry paths and
names, so the SCRIPT is public but its inputs and per-query outputs stay
private. It never fetches a query or an entry text: the warmup check runs
in SQL, and entries are read as ``length(text)`` only. The artifact holds
aggregates only — no query text, entry text, session id or per-event row.
``evals/agent_token_ledger.py`` documents the same constraint.

READ-ONLY: the connection forces ``default_transaction_read_only=on`` and
every read runs inside ``BEGIN READ ONLY ... ROLLBACK``; the script
refuses to read if the server does not report a read-only transaction. It
reads ``retrieval_events``, ``retrieval_uses`` and ``entries`` only (never
``meta``), and it never calls the daemon, whose search path appends
``retrieval_events`` rows.

Usage::

    set PSEUDOLIFE_METRICS_DSN=postgresql://pseudolife:<password>@127.0.0.1:5433/pseudolife_memory
    python evals/serving_policy_replay.py --until 2026-09-25T02:00:00Z \\
        --out evals/results/serving-policy-replay-20260925-r3.json

The DSN follows ``evals/capture_metrics.py`` (``PSEUDOLIFE_METRICS_DSN``,
defaulting to the stock local stack). Pass ``--until`` so a committed
artifact names the exact window it replayed. The output path is never
overwritten without ``--force``: tag a rerun and promote it deliberately.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DSN = os.environ.get(
    "PSEUDOLIFE_METRICS_DSN",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
)

# Outcome labels went live with the 2026-09-06 deploy: the daemon restarted
# at 11:17Z and the first ``used_via='outcome'`` label landed on an event
# written at 11:20:59Z. An event before this could not be labelled, so it
# would read as "served, never used" and bias every share downward.
LABELS_LIVE_SINCE = "2026-09-06T11:00:00Z"

# The query ``MemoryService.warmup`` searches on every daemon start.
WARMUP_QUERY = "warmup probe"

# The MCP ``memory_search`` default ``top_k`` in force over the replayed
# window. A logged 8 is the default OR an explicit 8 — indistinguishable.
# If the MCP default ever changes, a window after the change must pass the
# new value with ``--default-top-k``; a window that straddles it cannot be
# stratified.
DEFAULT_TOP_K = 8

# memory_recall fan-out detector (the 2026-09-23 review's definition).
BURST_GAP_S = 3.0
BURST_MIN = 3

# Client-issued session ids: the Claude/stdio shim's 32-hex MCP session and
# Codex's UUID. Anything else was set by hand on a scripted probe (for
# example the review's ``review-agent-experience-*`` run).
_CLIENT_SESSION = re.compile(
    r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{12})$")

# The 2026-09-23 fresh-eyes review's own searches, by retrieval_events id
# (handover REPORT.md incident 5). Identified read-only on 2026-09-25: the
# review fleet's MCP session between 05:00Z and 06:40Z (63 events, including
# the zebra off-domain probe 3697 and the 13 events its spot-check
# memory_get / memory_outcome calls labelled), its 28-query
# agent-experience run (3634-3663 under a hand-set session id, including the
# four absent-answer probes 3660-3663), and its REST timing loop (3664-3667,
# no session). The ranges interleave with other sessions' ids, which are
# listed out rather than swallowed: 3611 and 3701 are other sessions and
# 3669 is a warmup.
REVIEW_2026_09_23: frozenset[int] = frozenset(
    set(range(3607, 3611)) | set(range(3612, 3669))
    | set(range(3670, 3701)) | {3702, 3703, 3706})
# Every id above was written inside this window; an id outside it is some
# other bank's event and is left alone.
REVIEW_WINDOW = ("2026-09-23T05:00:00Z", "2026-09-23T06:40:00Z")

# Rank bands for the rank-matched use tables.
RANK_BANDS = (("r0-1", 0, 1), ("r2-3", 2, 3), ("r4+", 4, 10_000))

RESULTS = Path(__file__).resolve().parent / "results"


# ══════════════════════════════════════════════════════════════════════════
# data shapes
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Row:
    """One served entry in one logged search."""

    entry_id: int
    rank: int
    score: float | None        # the fused score as served
    superseded: bool           # superseded AT SERVE TIME (logged multiplier)
    dense: float | None = None  # bi-encoder cosine; None = not a dense hit
    channel: str | None = None  # dense / slot / bm25 (logged components)
    bm25: float | None = None   # normalised BM25; 0.0 = scored nothing
    slot: float | None = None   # slot-channel confidence (before any boost)


@dataclass
class Event:
    id: int
    session_id: str | None
    created_at: float
    is_warmup: bool
    top_k: int | None          # requested width; None = no params logged
    sources_filter: list[str] | None
    rows: list[Row] = field(default_factory=list)
    used: set[int] = field(default_factory=set)   # entry ids labelled used
    # Scores of the cortex facts served above the entries (schema v34),
    # all of them at or above the guard in force (0.2 over this window).
    fact_scores: list[float] = field(default_factory=list)
    # Retrieval shape logged in ``params`` — what ``simulate_width`` needs.
    pool_size: int | None = None
    bm25_weight: float | None = None   # None = BM25 did not run
    bm25_min: float = 0.0
    explicit_floor: float | None = None  # the caller's own min_score
    simulable: bool = False            # weighted_sum, x1 pool, one band


@dataclass(frozen=True)
class EntryInfo:
    source: str | None
    text_len: int


WIDTH_METHODS = ("simulate", "max_demotion", "prefix")

# Insertion order of the channels in ``cms.retrieve`` (band walk, slot
# pool, BM25 injections): the stable sort's tie-break.
_CHANNEL_ORDER = {"dense": 0, "slot": 1, "bm25": 2}


def simulate_width(ev: Event, k: int, method: str = "simulate"
                   ) -> tuple[list[Row], bool]:
    """The rows a width-``k`` search would have served, rebuilt from a
    wider logged search (module docstring). Returns ``(rows, simulated)``;
    ``simulated`` is False when the event was not logged under the shipped
    shape and the prefix was used instead."""
    rows = list(ev.rows)
    if ev.top_k is None or k >= ev.top_k:
        return rows, True
    if method == "prefix":
        return rows[:k], True
    if not ev.simulable or any(r.channel not in _CHANNEL_ORDER for r in rows):
        return rows[:k], False
    dense = sorted((r for r in rows if r.channel == "dense"),
                   key=lambda r: (-(r.dense or 0.0), r.rank))
    unlogged = 0
    if method == "max_demotion" and ev.pool_size is not None:
        unlogged = max(0, ev.pool_size - len(dense))
    cos_rank = {r.entry_id: i + unlogged for i, r in enumerate(dense)}
    # The slot pool keeps its top k by confidence; the BM25 boost that can
    # reorder slot hits is added after that cut.
    slot_rank = {r.entry_id: i for i, r in enumerate(
        sorted((r for r in rows if r.channel == "slot"),
               key=lambda r: (-(r.slot if r.slot is not None
                                else (r.score or 0.0)), r.rank)))}
    pool: list[tuple[tuple, Row]] = []
    for r in rows:
        if r.channel == "dense" and cos_rank[r.entry_id] >= k:
            # Out of the dense pool: back only as a BM25-only injection,
            # which a caller's explicit floor still gates.
            injected = (ev.bm25_weight * r.bm25
                        if ev.bm25_weight is not None and r.bm25
                        and r.bm25 >= ev.bm25_min else None)
            if injected is not None and (ev.explicit_floor is None
                                         or injected >= ev.explicit_floor):
                demoted = Row(r.entry_id, r.rank, injected, r.superseded,
                              None, "bm25", r.bm25)
                pool.append(((2, -r.bm25), demoted))
            continue
        if r.channel == "slot" and slot_rank[r.entry_id] >= k:
            continue
        if r.channel == "dense":
            tie = (0, cos_rank[r.entry_id])
        elif r.channel == "slot":
            tie = (1, slot_rank[r.entry_id])
        else:
            tie = (2, -(r.bm25 or 0.0))
        pool.append((tie, r))
    pool.sort(key=lambda t: (-(t[1].score or 0.0), t[0]))
    return [Row(r.entry_id, i, r.score, r.superseded, r.dense, r.channel,
                r.bm25, r.slot) for i, (_, r) in enumerate(pool[:k])], True


@dataclass(frozen=True)
class Policy:
    """A projection-side serving policy.

    ``top_k`` is a NEW DEFAULT: it applies to default-width events only
    (logged ``top_k == default_top_k``); explicit widths are the caller's
    and stay as served. ``width_method`` picks how the narrower list is
    built (``simulate_width``). ``exclude_sources`` applies to events whose
    caller passed no ``sources`` filter (a caller who asks for digests gets
    them) and does not backfill. ``min_score`` drops rows below a
    fused-score floor on every event.
    """

    name: str
    top_k: int | None = None
    exclude_sources: tuple[str, ...] = ()
    min_score: float | None = None
    width_method: str = "simulate"

    def serve(self, ev: Event, entries: dict[int, EntryInfo],
              default_top_k: int) -> tuple[list[Row], bool]:
        """The rows this policy serves for ``ev``, and whether a width
        change was simulated (False = fell back to the prefix)."""
        rows, simulated = list(ev.rows), True
        if self.top_k is not None and ev.top_k == default_top_k:
            rows, simulated = simulate_width(ev, self.top_k,
                                             self.width_method)
        if self.exclude_sources and not ev.sources_filter:
            rows = [r for r in rows
                    if (entries.get(r.entry_id) is None
                        or entries[r.entry_id].source
                        not in self.exclude_sources)]
        if self.min_score is not None:
            rows = [r for r in rows
                    if r.score is None or r.score >= self.min_score]
        return rows, simulated

    def describe(self) -> dict[str, Any]:
        return {"top_k": self.top_k,
                "width_method": self.width_method if self.top_k else None,
                "exclude_sources": list(self.exclude_sources),
                "min_score": self.min_score}


DEFAULT_POLICIES: tuple[Policy, ...] = (
    Policy("as_served"),
    Policy("top_k=7", top_k=7),
    Policy("top_k=6", top_k=6),
    Policy("top_k=5", top_k=5),
    Policy("top_k=4", top_k=4),
    Policy("top_k=6 (max demotion)", top_k=6, width_method="max_demotion"),
    Policy("top_k=6 (prefix)", top_k=6, width_method="prefix"),
    Policy("exclude_digest", exclude_sources=("digest",)),
    Policy("top_k=6+exclude_digest", top_k=6, exclude_sources=("digest",)),
    Policy("min_score=0.5", min_score=0.5),
)


# ══════════════════════════════════════════════════════════════════════════
# pure core (unit-tested on fixtures; no DB)
# ══════════════════════════════════════════════════════════════════════════

def parse_ts(s: str) -> float:
    """ISO-8601 (``Z`` or offset) -> epoch seconds."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials (None at n=0)."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def is_client_session(session_id: str | None) -> bool:
    return bool(session_id) and bool(_CLIENT_SESSION.match(session_id))


def burst_ids(events: Iterable[Event], gap_s: float = BURST_GAP_S,
              min_size: int = BURST_MIN) -> set[int]:
    """Ids of events in same-session runs of ``min_size`` or more whose
    consecutive gaps are all ``<= gap_s`` — ``memory_recall`` fan-out."""
    by_session: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.session_id:
            by_session[e.session_id].append(e)
    out: set[int] = set()
    for evs in by_session.values():
        evs.sort(key=lambda e: (e.created_at, e.id))
        run = [evs[0]]
        for e in evs[1:]:
            if e.created_at - run[-1].created_at <= gap_s:
                run.append(e)
                continue
            if len(run) >= min_size:
                out.update(x.id for x in run)
            run = [e]
        if len(run) >= min_size:
            out.update(x.id for x in run)
    return out


def select_agent_events(events: list[Event], *, since: float,
                        until: float | None = None,
                        exclude_ids: frozenset[int] = REVIEW_2026_09_23,
                        gap_s: float = BURST_GAP_S,
                        min_size: int = BURST_MIN,
                        ) -> tuple[list[Event], dict[str, int]]:
    """Apply the agent-origin filter; returns the kept events and a count
    of what each step removed, in order.

    Bursts are detected over the whole windowed log, before the other
    filters, so a recall run is recognised even when a probe interleaves
    with it. A burst event that carries served facts is a parallel
    ``memory_search``, not recall, and is kept (counted as
    ``burst_kept_with_facts``)."""
    counts: dict[str, int] = {}
    window = [e for e in events
              if e.created_at >= since
              and (until is None or e.created_at < until)]
    counts["events_in_window"] = len(window)
    bursts = burst_ids(window, gap_s, min_size)
    review_lo, review_hi = (parse_ts(t) for t in REVIEW_WINDOW)
    kept: list[Event] = []
    steps = Counter()
    labelled = Counter()
    burst_kept = burst_kept_labelled = burst_nothing = 0
    for e in window:
        if e.is_warmup:
            step = "dropped_warmup"
        elif not e.session_id:
            step = "dropped_no_session"
        elif not is_client_session(e.session_id):
            step = "dropped_probe_session"
        elif e.id in bursts and not e.fact_scores:
            step = "dropped_recall_burst"
            burst_nothing += not e.rows
        elif (e.id in exclude_ids
              and review_lo <= e.created_at < review_hi):
            step = "dropped_review_2026_09_23"
        else:
            if e.id in bursts:
                burst_kept += 1
                burst_kept_labelled += bool(e.used)
            kept.append(e)
            continue
        steps[step] += 1
        labelled[step] += bool(e.used)
    for k in ("dropped_warmup", "dropped_no_session",
              "dropped_probe_session", "dropped_recall_burst",
              "dropped_review_2026_09_23"):
        counts[k] = steps.get(k, 0)
        # How many LABELLED events each step removed: a filter that eats
        # labels is the one whose definition matters most.
        counts[f"{k}_labelled"] = labelled.get(k, 0)
    # A parallel memory_search that served nothing at all (the one case
    # low_confidence fires) would look exactly like recall here.
    counts["dropped_recall_burst_served_nothing"] = burst_nothing
    counts["burst_kept_with_facts"] = burst_kept
    counts["burst_kept_with_facts_labelled"] = burst_kept_labelled
    counts["agent_events"] = len(kept)
    return kept, counts


def served_text_chars(info: EntryInfo | None, cap: int) -> int | None:
    """Chars of entry text the compact projection serves for this row: the
    capped text plus the one-char ellipsis ``_truncate`` appends. None when
    the entry row is gone (evicted) and its length is unknown."""
    if info is None:
        return None
    if cap > 0 and info.text_len > cap:
        return cap + 1
    return info.text_len


def row_class(info: EntryInfo | None) -> str:
    if info is None:
        return "unknown"
    if info.source == "digest":
        return "digest"
    if info.source == "status":
        return "status"
    return "knowledge"


def _session_key(e: Event) -> str:
    return e.session_id or f"event:{e.id}"


def _bootstrap(per_session: dict[str, tuple[float, ...]],
               stat, reps: int, seed: int) -> tuple[float, float] | None:
    """Percentile CI of ``stat(sum of resampled session tuples)``, resampling
    SESSIONS: labels are credited per session, so hits inside one session
    are not independent draws and a Wilson interval over hits is too
    narrow."""
    keys = sorted(per_session)
    if len(keys) < 2 or reps <= 0:
        return None
    rng = random.Random(seed)
    width = len(next(iter(per_session.values())))
    vals = []
    for _ in range(reps):
        tot = [0.0] * width
        for _k in range(len(keys)):
            t = per_session[keys[rng.randrange(len(keys))]]
            for i in range(width):
                tot[i] += t[i]
        v = stat(tot)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    vals.sort()
    lo = vals[int(0.025 * (len(vals) - 1))]
    hi = vals[int(0.975 * (len(vals) - 1))]
    return (lo, hi)


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _r(x: float | None, places: int = 4) -> float | None:
    return None if x is None else round(x, places)


def policy_table(events: list[Event], entries: dict[int, EntryInfo],
                 policies: Iterable[Policy], *, default_top_k: int,
                 text_cap: int, reps: int, seed: int) -> list[dict[str, Any]]:
    """One row per policy: used hits kept (labelled events), rows and chars
    kept (all events in the stratum)."""
    out = []
    for pol in policies:
        used_tot = used_kept = rows_tot = rows_kept = 0
        chars_tot = chars_kept = rows_unknown_len = 0
        affected = fallbacks = slot_bound = 0
        per_session: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for e in events:
            served, simulated = pol.serve(e, entries, default_top_k)
            fallbacks += not simulated
            slot_bound += bool(
                pol.top_k is not None and e.top_k == default_top_k
                and pol.width_method != "prefix"
                and sum(r.channel == "slot" for r in e.rows) > pol.top_k)
            served_ids = {r.entry_id for r in served}
            affected += [r.entry_id for r in served] != [
                r.entry_id for r in e.rows]
            rows_kept += len(served)
            for row in e.rows:
                info = entries.get(row.entry_id)
                keep = row.entry_id in served_ids
                rows_tot += 1
                c = served_text_chars(info, text_cap)
                if c is None:
                    rows_unknown_len += 1
                else:
                    chars_tot += c
                    chars_kept += c if keep else 0
                if row.entry_id in e.used:
                    used_tot += 1
                    used_kept += keep
                    ps = per_session[_session_key(e)]
                    ps[0] += 1
                    ps[1] += keep
        ci = wilson(used_kept, used_tot)
        boot = _bootstrap({k: tuple(v) for k, v in per_session.items()},
                          lambda t: _ratio(t[1], t[0]), reps, seed)
        out.append({
            "policy": pol.name, **pol.describe(),
            "used_hits": used_tot, "used_kept": used_kept,
            "used_kept_share": _r(_ratio(used_kept, used_tot)),
            "used_kept_wilson95": [_r(x) for x in ci] if ci else None,
            "used_kept_session_bootstrap95": (
                [_r(x) for x in boot] if boot else None),
            "rows": rows_tot, "rows_kept": rows_kept,
            "rows_kept_share": _r(_ratio(rows_kept, rows_tot)),
            "entry_text_chars": chars_tot,
            "entry_text_chars_kept": chars_kept,
            "entry_text_chars_kept_share": _r(_ratio(chars_kept, chars_tot)),
            "rows_without_length": rows_unknown_len,
            "events_changed": affected,
            "events_prefix_fallback": fallbacks,
            "events_slot_cap_bound": slot_bound,
        })
    return out


def rank_curve(events: list[Event], max_rank: int = 10) -> list[dict]:
    """Use rate by served rank over labelled events."""
    n = Counter()
    u = Counter()
    for e in events:
        if not e.used:
            continue
        for row in e.rows:
            if row.rank < max_rank:
                n[row.rank] += 1
                u[row.rank] += row.entry_id in e.used
    return [{"rank": r, "rows": n[r], "used": u[r],
             "use_rate": _r(_ratio(u[r], n[r]))}
            for r in range(max_rank) if n[r]]


def _band(rank: int) -> str:
    for name, lo, hi in RANK_BANDS:
        if lo <= rank <= hi:
            return name
    return RANK_BANDS[-1][0]


def class_use_table(events: list[Event], entries: dict[int, EntryInfo],
                    *, live_only: bool) -> dict[str, Any]:
    """Use rate by entry class and rank band over labelled events."""
    n: Counter = Counter()
    u: Counter = Counter()
    for e in events:
        if not e.used:
            continue
        for row in e.rows:
            if live_only and row.superseded:
                continue
            key = (row_class(entries.get(row.entry_id)), _band(row.rank))
            n[key] += 1
            u[key] += row.entry_id in e.used
    table: dict[str, Any] = {}
    for cls in ("knowledge", "status", "digest", "unknown"):
        cells = {}
        for band, _, _ in RANK_BANDS:
            k = (cls, band)
            if n[k]:
                ci = wilson(u[k], n[k])
                cells[band] = {"rows": n[k], "used": u[k],
                               "use_rate": _r(u[k] / n[k]),
                               "wilson95": [_r(x) for x in ci]}
        if cells:
            table[cls] = cells
    return table


def digest_rank_matched(events: list[Event], entries: dict[int, EntryInfo],
                        *, live_only: bool, reps: int,
                        seed: int, max_rank: int = 10) -> dict[str, Any]:
    """Observed digest uses over the uses expected if each served digest
    were used at the non-digest rate FOR ITS EXACT RANK (indirect
    standardisation). 1.0 = digests are used like everything else at the
    same rank; the 2026-09-23 review estimated ~0.6."""
    # Per-session counts, laid out as [digest rows at rank r, digest used
    # at r, other rows at r, other used at r] for r < max_rank.
    per_session: dict[str, list[float]] = defaultdict(
        lambda: [0.0] * (4 * max_rank))
    for e in events:
        if not e.used or e.sources_filter:
            continue
        ps = per_session[_session_key(e)]
        for row in e.rows:
            if row.rank >= max_rank or (live_only and row.superseded):
                continue
            cls = row_class(entries.get(row.entry_id))
            if cls == "unknown":
                continue
            base = 0 if cls == "digest" else 2
            ps[4 * row.rank + base] += 1
            ps[4 * row.rank + base + 1] += row.entry_id in e.used

    def stat(t: list[float]) -> float | None:
        observed = expected = 0.0
        for r in range(max_rank):
            d_n, d_u, o_n, o_u = t[4 * r: 4 * r + 4]
            if d_n and o_n:
                observed += d_u
                expected += d_n * (o_u / o_n)
        return observed / expected if expected else None

    tot = [0.0] * (4 * max_rank)
    for v in per_session.values():
        for i, x in enumerate(v):
            tot[i] += x
    d_rows = sum(tot[4 * r] for r in range(max_rank))
    d_used = sum(tot[4 * r + 1] for r in range(max_rank))
    o_rows = sum(tot[4 * r + 2] for r in range(max_rank))
    o_used = sum(tot[4 * r + 3] for r in range(max_rank))
    boot = _bootstrap({k: tuple(v) for k, v in per_session.items()},
                      stat, reps, seed)
    return {
        "scope": ("unfiltered labelled searches; "
                  + ("live (not superseded at serve time) rows"
                     if live_only else "all rows")),
        "digest_rows": int(d_rows), "digest_used": int(d_used),
        "digest_use_rate": _r(_ratio(d_used, d_rows)),
        "other_rows": int(o_rows), "other_used": int(o_used),
        "other_use_rate": _r(_ratio(o_used, o_rows)),
        "rank_matched_ratio": _r(stat(tot), 3),
        "rank_matched_ratio_session_bootstrap95": (
            [_r(x, 3) for x in boot] if boot else None),
    }


def digest_presence(events: list[Event],
                    entries: dict[int, EntryInfo]) -> dict[str, Any]:
    """How much of default (unfiltered) search is digests, and what share of
    the hits agents used were digests."""
    rows = digest_rows = used = digest_used = searches = with_digest = 0
    for e in events:
        if e.sources_filter:
            continue
        searches += 1
        has = False
        for row in e.rows:
            is_d = row_class(entries.get(row.entry_id)) == "digest"
            rows += 1
            digest_rows += is_d
            has |= is_d
            if row.entry_id in e.used:
                used += 1
                digest_used += is_d
        with_digest += has
    return {
        "unfiltered_searches": searches,
        "searches_serving_a_digest": with_digest,
        "searches_serving_a_digest_share": _r(_ratio(with_digest, searches)),
        "served_rows": rows, "digest_rows": digest_rows,
        "digest_row_share": _r(_ratio(digest_rows, rows)),
        "used_hits": used, "digest_used_hits": digest_used,
        "digest_used_share": _r(_ratio(digest_used, used)),
    }


def quantile(values: list[float], q: float) -> float | None:
    """Linear-interpolated quantile (the ``capture_metrics`` convention)."""
    if not values:
        return None
    vs = sorted(values)
    idx = q * (len(vs) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (idx - lo)


# (search_confidence_floor, cortex guard) pairs to price. (0.70, 0.65) is
# the pair docs recommended from the 2026-06-19 sweep on the old MiniLM
# embedder; the others relax one knob at a time.
ABSTENTION_GRID = ((0.70, 0.65), (0.60, 0.65), (0.50, 0.65), (0.70, 0.2),
                   (0.50, 0.2), (0.40, 0.2))

# The only known-absent queries on the live log: the 2026-09-23 review's
# four in-domain absent-answer probes (plausible questions about this
# project whose answer is not in the bank) and its off-domain zebra probe.
# Tiny, and excluded from every agent statistic above; reported only as the
# other side of a floor's trade-off.
ABSENT_ANSWER_PROBES = (3660, 3661, 3662, 3663)
OFF_DOMAIN_PROBES = (3697,)


def _flagged(e: Event, floor: float, guard: float) -> bool:
    """Would ``low_confidence`` fire on this logged search under a floor
    and guard? The service half compares the top served (fused) score, not
    the dense cosine; the cortex half keeps facts at or above the guard."""
    top = max((r.score for r in e.rows if r.score is not None), default=None)
    weak = top is None or top < floor
    return weak and not any(f >= guard for f in e.fact_scores)


def abstention_report(events: list[Event],
                      probes: Iterable[Event] = ()) -> dict[str, Any]:
    """When ``low_confidence`` fires on real agent searches, and what a
    score floor would do.

    The MCP flag is ``service low_confidence AND no cortex fact``; the
    service half is ``no entries`` at the shipped floor 0 and ``top served
    score < floor`` above it (``memory/abstain.py``), and the cortex half
    keeps only facts scoring at or above the guard. Both halves are in the
    log: the served entry scores and the served facts' scores. Facts were
    logged at the guard in force (0.2), so only guards at or above it
    replay. ``flagged_labelled`` counts searches whose hits the agent then
    reported using: flagging those is a false abstention. ``probes`` are
    known-absent searches (``ABSENT_ANSWER_PROBES``), where flagging is
    the point."""
    n = len(events)
    probes = list(probes)
    nothing = no_entries = 0
    top_dense: list[float] = []
    min_dense: list[float] = []
    fact_scores: list[float] = []
    grid = Counter()
    grid_labelled = Counter()
    labelled = sum(1 for e in events if e.used)
    for e in events:
        if not e.rows:
            no_entries += 1
            nothing += not e.fact_scores
        fact_scores.extend(e.fact_scores)
        dense = [r.dense for r in e.rows if r.dense is not None]
        if dense:
            top_dense.append(max(dense))
            min_dense.append(min(dense))
        for floor, guard in ABSTENTION_GRID:
            if _flagged(e, floor, guard):
                grid[(floor, guard)] += 1
                grid_labelled[(floor, guard)] += bool(e.used)
    return {
        "searches": n,
        "served_no_entries": no_entries,
        "served_nothing": nothing,
        "note": ("served_nothing = no entries AND no cortex facts, the only "
                 "case low_confidence fires at the shipped floor 0"),
        "top_dense_cosine": {
            "searches": len(top_dense),
            **{f"p{int(q * 100):02d}": _r(quantile(top_dense, q))
               for q in (0.05, 0.10, 0.25, 0.50)},
            "below_0.40": sum(1 for v in top_dense if v < 0.40),
            "below_0.45": sum(1 for v in top_dense if v < 0.45),
        },
        "lowest_served_dense_cosine": {
            "searches": len(min_dense),
            **{f"p{int(q * 100):02d}": _r(quantile(min_dense, q))
               for q in (0.01, 0.05, 0.50)},
            "below_0.30": sum(1 for v in min_dense if v < 0.30),
        },
        "labelled_searches": labelled,
        "floor_grid": [
            {"search_confidence_floor": floor, "guard_min_score": guard,
             "flagged": grid[(floor, guard)],
             "flagged_share": _r(_ratio(grid[(floor, guard)], n)),
             "flagged_labelled": grid_labelled[(floor, guard)],
             "flagged_labelled_share": _r(_ratio(
                 grid_labelled[(floor, guard)], labelled)),
             "absent_probes_flagged": sum(
                 _flagged(p, floor, guard) for p in probes
                 if p.id in ABSENT_ANSWER_PROBES),
             "off_domain_probes_flagged": sum(
                 _flagged(p, floor, guard) for p in probes
                 if p.id in OFF_DOMAIN_PROBES)}
            for floor, guard in ABSTENTION_GRID],
        "absent_answer_probes": {
            "found": sum(1 for p in probes if p.id in ABSENT_ANSWER_PROBES),
            "top_dense_cosine": sorted(
                _r(max((r.dense for r in p.rows if r.dense is not None),
                       default=0.0))
                for p in probes if p.id in ABSENT_ANSWER_PROBES),
            "top_fact_score": sorted(
                _r(max(p.fact_scores, default=0.0))
                for p in probes if p.id in ABSENT_ANSWER_PROBES),
        },
        "off_domain_probes": {
            "found": sum(1 for p in probes if p.id in OFF_DOMAIN_PROBES),
            "served_dense_cosine_range": [
                _r(f(r.dense for p in probes if p.id in OFF_DOMAIN_PROBES
                     for r in p.rows if r.dense is not None))
                for f in (min, max)
            ] if any(r.dense is not None for p in probes
                     if p.id in OFF_DOMAIN_PROBES for r in p.rows) else None,
        },
        "served_facts": len(fact_scores),
        "served_facts_below_0.65": sum(1 for f in fact_scores if f < 0.65),
        "served_facts_below_0.65_share": _r(_ratio(
            sum(1 for f in fact_scores if f < 0.65), len(fact_scores))),
    }


def simulation_check(pairs: list[tuple[Event, int, list[int]]]
                     ) -> dict[str, Any]:
    """Score ``simulate_width`` and the prefix against real narrower
    searches. Each pair is (wide event, narrow top_k, the entry ids the
    narrow search actually served)."""
    c = Counter()
    widths = Counter()
    for ev, k, truth in pairs:
        widths[f"{ev.top_k}->{k}"] += 1
        sim, ok = simulate_width(ev, k, "simulate")
        pre, _ = simulate_width(ev, k, "prefix")
        s_ids, p_ids = [r.entry_id for r in sim], [r.entry_id for r in pre]
        c["pairs"] += 1
        c["simulated"] += ok
        c["truth_rows"] += len(truth)
        c["simulate_exact_set"] += set(s_ids) == set(truth)
        c["prefix_exact_set"] += set(p_ids) == set(truth)
        c["simulate_rows_matched"] += len(set(s_ids) & set(truth))
        c["prefix_rows_matched"] += len(set(p_ids) & set(truth))
        if set(s_ids) != set(p_ids):
            c["methods_disagree"] += 1
            c["disagree_simulate_exact"] += set(s_ids) == set(truth)
            c["disagree_prefix_exact"] += set(p_ids) == set(truth)
    n, rows = c["pairs"], c["truth_rows"]
    return {
        **{k: c[k] for k in (
            "pairs", "simulated", "truth_rows", "simulate_exact_set",
            "prefix_exact_set", "simulate_rows_matched",
            "prefix_rows_matched", "methods_disagree",
            "disagree_simulate_exact", "disagree_prefix_exact")},
        "simulate_exact_set_share": _r(_ratio(c["simulate_exact_set"], n)),
        "prefix_exact_set_share": _r(_ratio(c["prefix_exact_set"], n)),
        "simulate_rows_matched_share": _r(_ratio(
            c["simulate_rows_matched"], rows)),
        "prefix_rows_matched_share": _r(_ratio(
            c["prefix_rows_matched"], rows)),
        "widths": dict(sorted(widths.items())),
        "scope": ("whole log, not the label window: same query, session, "
                  "filters and ranking knobs within ten minutes"),
    }


def top_k_distribution(events: list[Event]) -> list[dict[str, Any]]:
    c = Counter(e.top_k for e in events)
    n = len(events)
    return [{"top_k": k, "searches": v, "share": _r(v / n)}
            for k, v in sorted(c.items(), key=lambda kv: (kv[0] is None,
                                                          kv[0] or 0))]


def build_report(events: list[Event], entries: dict[int, EntryInfo], *,
                 since: float, until: float | None,
                 policies: Iterable[Policy] = DEFAULT_POLICIES,
                 default_top_k: int = DEFAULT_TOP_K,
                 text_cap: int | None = None,
                 reps: int = 2000, seed: int = 20260925,
                 gap_s: float = BURST_GAP_S,
                 min_size: int = BURST_MIN,
                 pairs: list[tuple[Event, int, list[int]]] | None = None,
                 ) -> dict[str, Any]:
    """Everything the artifact carries — aggregates only."""
    if text_cap is None:
        from pseudolife_memory.utils.config import McpConfig
        text_cap = McpConfig().entry_text_chars
    policies = tuple(policies)
    agent, counts = select_agent_events(
        events, since=since, until=until, gap_s=gap_s, min_size=min_size)
    default_width = [e for e in agent if e.top_k == default_top_k]
    strata = {"default_width": default_width, "all_agent": agent}
    labelled = [e for e in agent if e.used]
    sessions = {e.session_id for e in agent}
    labelled_sessions = {e.session_id for e in labelled}
    report: dict[str, Any] = {
        "window": {"since": iso(since),
                   "until": iso(until) if until is not None else None},
        "filter": {**counts,
                   "burst_gap_s": gap_s, "burst_min": min_size,
                   "review_2026_09_23_ids": len(REVIEW_2026_09_23)},
        "labels": {
            "used_via": "outcome",
            "agent_sessions": len(sessions),
            "labelling_sessions": len(labelled_sessions),
            "labelled_events": len(labelled),
            "used_hits": sum(len(e.used & {r.entry_id for r in e.rows})
                             for e in labelled),
        },
        "config": {"default_top_k": default_top_k,
                   "entry_text_cap": text_cap,
                   "bootstrap_reps": reps, "bootstrap_seed": seed},
        "requested_top_k": top_k_distribution(agent),
        "width_simulation_check": (simulation_check(pairs)
                                   if pairs is not None else None),
        "abstention": abstention_report(agent, [
            e for e in events
            if e.id in ABSENT_ANSWER_PROBES + OFF_DOMAIN_PROBES
            and parse_ts(REVIEW_WINDOW[0]) <= e.created_at
            < parse_ts(REVIEW_WINDOW[1])]),
        "strata": {},
        "digests": {
            "presence": digest_presence(agent, entries),
            "rank_matched_all_rows": digest_rank_matched(
                agent, entries, live_only=False, reps=reps, seed=seed),
            "rank_matched_live_rows": digest_rank_matched(
                agent, entries, live_only=True, reps=reps, seed=seed),
            "use_by_class_and_rank_live_rows": class_use_table(
                agent, entries, live_only=True),
        },
        "privacy": ("aggregates only: no query text, entry text, session id "
                    "or per-event row"),
    }
    for name, evs in strata.items():
        report["strata"][name] = {
            "searches": len(evs),
            "labelled_searches": sum(1 for e in evs if e.used),
            "rank_curve": rank_curve(evs),
            "policies": policy_table(
                evs, entries, policies, default_top_k=default_top_k,
                text_cap=text_cap, reps=reps, seed=seed),
        }
    return report


# ══════════════════════════════════════════════════════════════════════════
# DB read (read-only)
# ══════════════════════════════════════════════════════════════════════════

_EVENTS_SQL = """
SELECT id, session_id, created_at, (query_text = %(warmup)s) AS is_warmup,
       served, params, served_facts
FROM retrieval_events
WHERE created_at >= %(since)s AND (%(until)s::float8 IS NULL
                                   OR created_at < %(until)s::float8)
ORDER BY id
"""

_USES_SQL = """
SELECT u.event_id, u.entry_id
FROM retrieval_uses u JOIN retrieval_events e ON e.id = u.event_id
WHERE u.used_via = 'outcome' AND e.created_at >= %(since)s
  AND (%(until)s::float8 IS NULL OR e.created_at < %(until)s::float8)
"""

# length(text), never text: entry bodies are private and are not needed.
_ENTRIES_SQL = "SELECT id, source, length(text) FROM entries"

# The same query served twice in one session, within ten minutes, under the
# same filters, at a wider and a narrower top_k. query_text is only
# COMPARED; the narrow search contributes its served entry ids alone.
_PAIRS_SQL = """
SELECT w.id, w.session_id, w.created_at, false, w.served, w.params,
       w.served_facts, (n.params->>'top_k')::int,
       (SELECT coalesce(array_agg((x->>'entry_id')::bigint), '{}')
          FROM jsonb_array_elements(n.served) x)
FROM retrieval_events w
JOIN retrieval_events n
  ON n.query_text = w.query_text
 AND n.id <> w.id
 AND n.session_id IS NOT DISTINCT FROM w.session_id
 AND abs(n.created_at - w.created_at) < 600
 AND (n.params->>'top_k')::int < (w.params->>'top_k')::int
 AND coalesce(n.params->'filters', 'null'::jsonb)
     = coalesce(w.params->'filters', 'null'::jsonb)
 AND coalesce(n.params->'bm25', 'null'::jsonb)
     = coalesce(w.params->'bm25', 'null'::jsonb)
 AND (n.params->>'min_score') IS NOT DISTINCT FROM (w.params->>'min_score')
 AND (n.params->>'min_score_explicit')
     IS NOT DISTINCT FROM (w.params->>'min_score_explicit')
 AND (n.params->'reranker'->>'enabled')
     IS NOT DISTINCT FROM (w.params->'reranker'->>'enabled')
WHERE w.params IS NOT NULL AND n.params IS NOT NULL
  AND w.query_text <> %(warmup)s
  AND (%(until)s::float8 IS NULL OR w.created_at < %(until)s::float8)
"""


def _requested_top_k(params: dict | None) -> int | None:
    if not isinstance(params, dict) or params.get("top_k") is None:
        return None
    try:
        return int(params["top_k"])
    except (TypeError, ValueError):
        return None


def _sources_filter(params: dict | None) -> list[str] | None:
    if not isinstance(params, dict):
        return None
    f = params.get("filters") or {}
    s = f.get("sources") if isinstance(f, dict) else None
    return list(s) if s else None


def _row(served: dict) -> Row | None:
    if served.get("entry_id") is None:
        return None
    comps = served.get("components") or {}
    if not isinstance(comps, dict):
        comps = {}
    mult = comps.get("supersession_mult")
    dense = comps.get("dense")
    bm25 = comps.get("bm25")
    score = served.get("score")
    return Row(entry_id=int(served["entry_id"]),
               rank=int(served.get("rank", 0)),
               score=None if score is None else float(score),
               superseded=mult is not None and float(mult) < 1.0,
               dense=None if dense is None else float(dense),
               channel=comps.get("channel"),
               bm25=None if bm25 is None else float(bm25),
               slot=(None if comps.get("slot") is None
                     else float(comps["slot"])))


def _shape(params: dict | None) -> dict[str, Any]:
    """The retrieval shape ``simulate_width`` needs, from ``params``."""
    if not isinstance(params, dict):
        return {}
    pool = params.get("candidate_pool") or {}
    rer = params.get("reranker") or {}
    tl = params.get("timeline") or {}
    bm = params.get("bm25") or {}
    # A row logged before the pool-shape knobs (2026-09-04) has no
    # candidate_pool block; it ran the shipped shape, which the knobs'
    # defaults reproduce byte for byte (config.py, candidate_pool_multiplier).
    shipped = ("candidate_pool" not in params
               or (pool.get("fusion") == "weighted_sum"
                   and int(pool.get("multiplier") or 0) == 1))
    out: dict[str, Any] = {
        "pool_size": (int(pool["pool_size"])
                      if pool.get("pool_size") is not None else None),
        # One band: several bands each pool their own top k, so a global
        # cosine rank would not say which rows leave the pool.
        "simulable": bool(
            shipped and int(params.get("band_count") or 1) == 1
            and not rer.get("fired") and not tl.get("fired")
            and not params.get("contiguity_neighbors")),
        "explicit_floor": (float(params["min_score"])
                           if params.get("min_score_explicit")
                           and params.get("min_score") is not None
                           else None),
    }
    if bm.get("enabled"):
        out["bm25_weight"] = float(bm.get("weight", 0.0))
        out["bm25_min"] = float(bm.get("min_score", 0.0))
    return out


def _fact_scores(served_facts: list | None) -> list[float]:
    return [float(f["score"]) for f in (served_facts or [])
            if isinstance(f, dict) and f.get("score") is not None]


def events_from_rows(ev_rows: list[tuple], use_rows: list[tuple]
                     ) -> list[Event]:
    events = {}
    for eid, sid, created, warm, served, params, facts in ev_rows:
        rows = [r for r in (_row(s) for s in (served or [])) if r]
        events[int(eid)] = Event(
            id=int(eid), session_id=sid, created_at=float(created),
            is_warmup=bool(warm), top_k=_requested_top_k(params),
            sources_filter=_sources_filter(params), rows=rows,
            fact_scores=_fact_scores(facts), **_shape(params))
    for eid, entry_id in use_rows:
        ev = events.get(int(eid))
        if ev is not None:
            ev.used.add(int(entry_id))
    return [events[k] for k in sorted(events)]


def fetch(dsn: str, since: float, until: float | None
          ) -> tuple[list[Event], dict[int, EntryInfo], str,
                     list[tuple[Event, int, list[int]]]]:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn, connect_timeout=10, autocommit=True,
                         options="-c default_transaction_read_only=on"
                         ) as conn:
        conn.execute("BEGIN READ ONLY")
        try:
            ro = conn.execute("SHOW transaction_read_only").fetchone()[0]
            if ro != "on":
                raise SystemExit("refusing to read: the transaction is not "
                                 "read-only")
            db = conn.execute("SELECT current_database()").fetchone()[0]
            q = {"since": since, "until": until, "warmup": WARMUP_QUERY}
            ev_rows = conn.execute(_EVENTS_SQL, q).fetchall()
            use_rows = conn.execute(_USES_SQL, q).fetchall()
            entries = {int(i): EntryInfo(source=s, text_len=int(n or 0))
                       for i, s, n in conn.execute(_ENTRIES_SQL).fetchall()}
            pair_rows = conn.execute(_PAIRS_SQL, q).fetchall()
        finally:
            conn.execute("ROLLBACK")
    pairs = [(events_from_rows([r[:7]], [])[0], int(r[7]),
              [int(x) for x in r[8]]) for r in pair_rows]
    return events_from_rows(ev_rows, use_rows), entries, db, pairs


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def _fmt(x: float | None, pct: bool = True) -> str:
    if x is None:
        return "   -  "
    return f"{100 * x:5.1f}%" if pct else f"{x:.3f}"


def print_summary(report: dict[str, Any]) -> None:
    f = report["filter"]
    print(f"window {report['window']['since']} .. "
          f"{report['window']['until'] or 'now'}")
    print(f"events {f['events_in_window']} -> agent {f['agent_events']} "
          f"(warmup -{f['dropped_warmup']}, no session "
          f"-{f['dropped_no_session']}, probe sessions "
          f"-{f['dropped_probe_session']}, recall bursts "
          f"-{f['dropped_recall_burst']}, 09-23 review "
          f"-{f['dropped_review_2026_09_23']}); burst events kept as "
          f"parallel searches {f['burst_kept_with_facts']}")
    lab = report["labels"]
    print(f"labelled searches {lab['labelled_events']} from "
          f"{lab['labelling_sessions']}/{lab['agent_sessions']} sessions; "
          f"used hits {lab['used_hits']}")
    for name, st in report["strata"].items():
        print(f"\n[{name}] searches {st['searches']}, labelled "
              f"{st['labelled_searches']}")
        print(f"  {'policy':<24}{'used kept':>11}{'wilson95':>16}"
              f"{'session-boot95':>16}{'rows':>8}{'chars':>8}")
        for p in st["policies"]:
            w = p["used_kept_wilson95"]
            b = p["used_kept_session_bootstrap95"]
            print(f"  {p['policy']:<24}{_fmt(p['used_kept_share']):>11}"
                  f"{(_fmt(w[0]) + '-' + _fmt(w[1]).strip()) if w else '-':>16}"
                  f"{(_fmt(b[0]) + '-' + _fmt(b[1]).strip()) if b else '-':>16}"
                  f"{_fmt(p['rows_kept_share']):>8}"
                  f"{_fmt(p['entry_text_chars_kept_share']):>8}")
    sc = report.get("width_simulation_check")
    if sc:
        print(f"\nwidth simulation vs {sc['pairs']} real narrower searches: "
              f"exact set {sc['simulate_exact_set']} (prefix "
              f"{sc['prefix_exact_set']}); rows matched "
              f"{_fmt(sc['simulate_rows_matched_share'])} (prefix "
              f"{_fmt(sc['prefix_rows_matched_share'])})")
    a = report["abstention"]
    print(f"\nabstention: served nothing {a['served_nothing']}/"
          f"{a['searches']}; top dense cosine p05/p50 "
          f"{a['top_dense_cosine']['p05']}/{a['top_dense_cosine']['p50']}")
    for g in a["floor_grid"]:
        print(f"  floor {g['search_confidence_floor']:.2f} + guard "
              f"{g['guard_min_score']:.2f} would flag "
              f"{_fmt(g['flagged_share'])} of agent searches, "
              f"{_fmt(g['flagged_labelled_share'])} of those with a used "
              f"hit; absent probes {g['absent_probes_flagged']}/"
              f"{a['absent_answer_probes']['found']}")
    d = report["digests"]
    for key in ("rank_matched_all_rows", "rank_matched_live_rows"):
        rm = d[key]
        ci = rm["rank_matched_ratio_session_bootstrap95"]
        print(f"\ndigests ({key}): ratio {rm['rank_matched_ratio']} "
              f"(95% {ci}), digest rows {rm['digest_rows']} used "
              f"{rm['digest_used']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--since", default=LABELS_LIVE_SINCE,
                    help="window start, ISO-8601 UTC")
    ap.add_argument("--until", default=None,
                    help="window end, ISO-8601 UTC (pin it for an artifact)")
    ap.add_argument("--default-top-k", type=int, default=DEFAULT_TOP_K,
                    help="the MCP default top_k in force over the window")
    ap.add_argument("--burst-gap", type=float, default=BURST_GAP_S)
    ap.add_argument("--burst-min", type=int, default=BURST_MIN)
    ap.add_argument("--reps", type=int, default=2000,
                    help="session bootstrap replicates (0 = off)")
    ap.add_argument("--text-cap", type=int, default=None,
                    help="entry text cap in chars (default: the library "
                         "McpConfig.entry_text_chars; pass the deployed "
                         "value if it differs)")
    ap.add_argument("--out", default=None,
                    help="artifact path; omitted = print only")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing --out")
    args = ap.parse_args(argv)
    since = parse_ts(args.since)
    until = parse_ts(args.until) if args.until else None
    out = Path(args.out) if args.out else None
    if out is not None and out.exists() and not args.force:
        sys.exit(f"{out} exists — tag the rerun with a new name, or pass "
                 "--force to replace it deliberately")
    events, entries, db, pairs = fetch(args.dsn, since, until)
    report = build_report(
        events, entries, since=since, until=until,
        default_top_k=args.default_top_k, reps=args.reps,
        text_cap=args.text_cap,
        gap_s=args.burst_gap, min_size=args.burst_min, pairs=pairs)
    report = {
        "generated_for": ("serving-policy replay: logged served lists vs "
                          "memory_outcome used_ids labels"),
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source_db": f"{db} (read-only transaction)",
        "read_only": True,
        **report,
    }
    print_summary(report)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
