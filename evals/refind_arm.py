"""ReFind arm — an agentic LEXICAL search loop over the raw entry archive.

ReFind (arXiv 2608.12888) reports that an agent given nothing but keyword
search over the raw conversation beats most structured memory systems.
Single-shot BM25 is *not* that baseline and badly understates it, so a
ladder without this arm cannot say what the structural stack (bands,
cortex, graph) is worth: the honest comparator is the loop, not one query.

The three mechanisms the paper credits, all implemented here:

* **temporal narrowing** — a round may restrict the search to a date
  range. A record that provably falls outside the window is dropped; an
  UNDATED record can never be shown to fall outside one, so it stays
  eligible (narrowing must not hide evidence it cannot place).
* **skip-already-inspected** — turns read in an earlier round are never
  returned again, so re-issuing a query that worked surfaces the NEXT
  best matches instead of the same ones. The BM25 index is built over the
  temporal WINDOW and exclusion is applied to its results, so IDF does
  not drift as rounds accumulate (an index rebuilt over "what is left"
  would silently re-weight every surviving turn).
* **session-aware fusion** — a turn's score is fused with the evidence
  mass its session carries, so a weak hit inside a session that already
  yielded strong evidence outranks an equally weak hit standing alone.
  Fusion runs TWICE and the second one is the one that matters: once
  within a query, to pick what that query inspects, and again at serve
  time over the union of everything inspected, to pick what is served.
  Normalising per query would put every query's best hit at exactly 1.0 —
  a lone weak hit from round 3 would tie the strongest hit of round 1 and
  win on tie-break (2026-09-01 review; pinned by
  ``test_serve_ranking_fuses_across_rounds_not_per_query``). Raw BM25
  scores are the cross-query currency, so the serve-time pass fuses those.
  One honest caveat: scores from differently-narrowed windows come from
  slightly different IDF bases, since a window changes N and df. The
  distortion is small next to the per-query one it replaces, and it is
  not corrected here.

Deliberate scoping, so this stays a *comparator* and not a second
answering stack:

* The loop only RETRIEVES. The context it assembles is answered by the
  harness's own answerer and graded by the harness's own judge — the same
  rule the Cognee adapter follows (their retrieval modes, never their
  completion modes), because an arm that brings its own answerer measures
  a different instrument.
* The served context is **budget-matched** to the rag control by default
  (``top_k`` = the caller's raw-turn budget). Any win has to come from
  the loop, not from a wider window.
* Everything outside the model call is deterministic: scores are pure
  functions of the archive, and every ordering has an explicit tie-break
  (fused score, then session, then turn ordinal).

The search tool reuses the engine's BM25 (``pseudolife_memory.memory.
bm25``) rather than a second implementation, so the lexical baseline is
the same scorer the product ships.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pseudolife_memory.memory.bm25 import (  # noqa: E402
    BM25Index, normalize_scores,
)

# Loop shape. None of these are measured values — ReFind publishes no
# fusion weight and our own sweep has not run — so they are the arm's
# declared defaults, each exposed as a flag so a sweep can measure them
# before anything is claimed from a number this arm produces.
SESSION_FUSION_WEIGHT = 0.3
DEFAULT_ROUNDS = 3
DEFAULT_PER_ROUND_K = 8
DEFAULT_MAX_QUERIES = 3
DEFAULT_TOP_K = 6            # budget-matched to longmemeval_bench.RAG_TOP_K
# Display-only: how much of a turn the planner sees per snippet. Not a
# retrieval knob, so it stays a constant rather than a flag.
DEFAULT_SNIPPET_CHARS = 400
# One BM25 index per temporal window is cached, and the archive lives for
# a whole conversation while every planner-proposed window mints a new
# key, so the narrowed windows are capped at this many, evicting the
# oldest first (FIFO — within a question the planner narrows a handful of
# times, so recency ranking would buy nothing over insertion order). The
# unnarrowed index is kept unconditionally on top of the cap: every query
# that does not narrow hits it, so evicting it would rebuild the largest
# index of all, repeatedly.
WINDOW_CACHE = 4


@dataclass(frozen=True)
class ArchiveRecord:
    """One archived turn, exactly as the memory arms stored it.

    ``session`` is the conversation session (a BEAM batch, a LongMemEval
    session) — the fusion key. ``ordinal`` is the per-conversation turn
    number and doubles as reading order. ``date`` is an ISO date or None
    (see the undated-record rule in the module docstring).
    """
    text: str
    session: str
    ordinal: int
    date: str | None = None


@dataclass(frozen=True)
class Hit:
    index: int
    record: ArchiveRecord
    lexical: float
    fused: float


@dataclass(frozen=True)
class _IndexedText:
    """What BM25Index indexes: the turn text plus its archive position
    (the index scores ``.text`` and hands the object back, so the position
    rides along instead of being recovered by identity)."""
    text: str
    pos: int


_ANCHOR_FORMATS = ("%Y-%m-%d", "%B-%d-%Y", "%b-%d-%Y", "%Y/%m/%d %H:%M",
                   "%Y/%m/%d", "%B %d %Y", "%d-%B-%Y")


def parse_anchor(raw: str | None) -> str | None:
    """Normalise a benchmark time anchor to ``YYYY-MM-DD``; None when the
    shape is not a date (BEAM writes ``March-15-2024``, LongMemEval writes
    ``2023/04/10 (Mon) 02:03``). Unparseable anchors are not guessed at —
    the record simply becomes undated, and undated records stay eligible
    for every window."""
    if not raw:
        return None
    cleaned = re.sub(r"\s*\(\w+\)\s*", " ", str(raw)).strip()
    for fmt in _ANCHOR_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _norm_window(value) -> str | None:
    """A planner-supplied window edge, normalised to an ISO date. Junk
    becomes None — a bad date must widen the search, never silently empty
    it."""
    if not value or not isinstance(value, str):
        return None
    return parse_anchor(value)


def _minmax(values: dict[str, float]) -> dict[str, float]:
    """Min-max to [0, 1]; a single key (or an all-equal set) collapses to
    1.0, matching ``bm25.normalize_scores``."""
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo <= 0.0:
        return {k: 1.0 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def fuse(scored: list[tuple[int, ArchiveRecord, float]],
         session_weight: float = SESSION_FUSION_WEIGHT) -> list[Hit]:
    """Rank one set of scored turns: min-max the lexical scores, add the
    min-maxed evidence mass of each turn's session, and sort with an
    explicit tie-break (fused, then session, then ordinal).

    Used for one query's candidates inside ``LexicalArchive.search`` and
    again over the union of everything inspected at serve time — the same
    function both times, so the two rankings cannot drift apart.
    """
    if not scored:
        return []
    normed = normalize_scores([(pos, raw) for pos, _, raw in scored])
    by_pos = {pos: score for pos, score in normed}
    records = {pos: rec for pos, rec, _ in scored}
    raws = {pos: raw for pos, _, raw in scored}
    sessions: dict[str, float] = {}
    for pos, score in normed:
        sess = str(records[pos].session)
        sessions[sess] = sessions.get(sess, 0.0) + score
    session_norm = _minmax(sessions)
    hits = [
        Hit(index=pos, record=records[pos], lexical=raws[pos],
            fused=((1.0 - session_weight) * by_pos[pos]
                   + session_weight * session_norm[str(records[pos].session)]))
        for pos, _ in normed
    ]
    hits.sort(key=lambda h: (-h.fused, str(h.record.session),
                             h.record.ordinal))
    return hits


class LexicalArchive:
    """BM25 over raw archived turns, with temporal narrowing, exclusion of
    already-inspected turns, and session-aware fusion."""

    def __init__(self, records: Iterable[ArchiveRecord], *,
                 k1: float = 1.5, b: float = 0.75) -> None:
        self.records: list[ArchiveRecord] = list(records)
        self._k1, self._b = k1, b
        # One index per temporal window (see WINDOW_CACHE for the policy).
        self._windows: dict[tuple[str | None, str | None],
                            tuple[BM25Index, list[_IndexedText]]] = {}

    def __len__(self) -> int:
        return len(self.records)

    def span(self) -> tuple[str | None, str | None]:
        """Earliest and latest dated record — what the planner is told so
        it can narrow at all."""
        dates = sorted(r.date for r in self.records if r.date)
        return (dates[0], dates[-1]) if dates else (None, None)

    def _window(self, since: str | None, until: str | None
                ) -> tuple[BM25Index, list[_IndexedText]]:
        key = (since, until)
        cached = self._windows.get(key)
        if cached is not None:
            return cached
        docs = [
            _IndexedText(text=r.text, pos=i)
            for i, r in enumerate(self.records)
            if r.date is None or ((since is None or r.date >= since)
                                  and (until is None or r.date <= until))
        ]
        built = (BM25Index(docs, k1=self._k1, b=self._b), docs)
        narrowed = [k for k in self._windows if k != (None, None)]
        while len(narrowed) >= WINDOW_CACHE and key != (None, None):
            del self._windows[narrowed.pop(0)]
        self._windows[key] = built
        return built

    def search(self, query: str, *, top_k: int = DEFAULT_PER_ROUND_K,
               since: str | None = None, until: str | None = None,
               exclude: Iterable[int] = (),
               session_weight: float = SESSION_FUSION_WEIGHT) -> list[Hit]:
        index, docs = self._window(since, until)
        if not docs:
            return []
        scored = index.score(query, top_k=len(docs))
        excluded = set(exclude)
        kept = [(d.pos, self.records[d.pos], s)
                for d, s in scored if d.pos not in excluded]
        return fuse(kept, session_weight)[:top_k]


_REFIND_SYSTEM = (
    "You are searching the archive of a long conversation to find the "
    "turns that answer a question. You cannot read the archive directly: "
    "you issue keyword searches and read what they return.\n"
    "The search is LEXICAL — it matches words, not meanings. Use "
    "distinctive words that would appear verbatim in the archived turns, "
    "and try more than one wording. Turns you have already read are never "
    "returned again, so repeating a query that worked surfaces the next "
    "best matches. You may narrow to a date range when the question is "
    "about a period.\n"
    "Reply with JSON and nothing else:\n"
    '{"queries": ["...", "..."], "since": "YYYY-MM-DD" or null, '
    '"until": "YYYY-MM-DD" or null, "done": false}\n'
    "Set \"done\" to true only when what you have already read answers the "
    "question."
)


def parse_plan(raw: str) -> dict | None:
    """Parse a planner reply into ``{queries, since, until, done}``.

    Tolerant in the same way as the BEAM judge parser (fences, a JSON
    object embedded in prose) and honest about failure: None means the
    reply was not a plan, which the loop records rather than papering
    over with an empty round.
    """
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001 — fall through to brace extraction
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start:end + 1])
            except Exception:  # noqa: BLE001
                obj = None
    if not isinstance(obj, dict):
        return None
    queries = obj.get("queries")
    if isinstance(queries, str):
        # A single query written as a bare string, not a list. Iterating
        # it would shred "trek domane" into ten single-character queries,
        # each of which returns junk at a top rank instead of failing.
        queries = [queries]
    elif not isinstance(queries, list):
        queries = []
    return {
        "queries": [q.strip() for q in queries
                    if isinstance(q, str) and q.strip()],
        "since": obj.get("since"),
        "until": obj.get("until"),
        "done": bool(obj.get("done")),
    }


def plan_prompt(question: str, span: tuple[str | None, str | None],
                issued: Sequence[str], read: Sequence[str],
                round_no: int, rounds: int,
                snippet_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    """The planner's view: the question, the archive's span (it cannot
    narrow temporally without knowing it), what it already asked, and what
    it already read — most relevant first, each truncated to
    ``snippet_chars``. The prompt is bounded by
    rounds x queries x per_round_k snippets: ~72 x 400 chars (~7k tokens)
    at the defaults, on a bench server configured for 240k."""
    first, last = span
    when = (f"The archive spans {first} to {last} (ISO dates). Some turns "
            "are undated and are searched in every window."
            if first else "The archive turns carry no usable dates.")
    reads = "\n".join(f"- {t[:snippet_chars]}" for t in read) or "- (nothing yet)"
    asked = ", ".join(f'"{q}"' for q in issued) or "(none yet)"
    return (f"Question: {question}\n"
            f"{when}\n"
            f"Search round {round_no} of {rounds}.\n"
            f"Queries already issued: {asked}\n"
            f"Turns read so far ({len(read)}):\n{reads}\n\n"
            "Reply with this round's JSON plan.")


def refind_search(archive: LexicalArchive, question: str, *,
                  chat: Callable[..., str],
                  rounds: int = DEFAULT_ROUNDS,
                  top_k: int = DEFAULT_TOP_K,
                  per_round_k: int = DEFAULT_PER_ROUND_K,
                  session_weight: float = SESSION_FUSION_WEIGHT,
                  max_queries: int = DEFAULT_MAX_QUERIES,
                  snippet_chars: int = DEFAULT_SNIPPET_CHARS,
                  max_tokens: int = 512) -> tuple[str, dict]:
    """Run the agentic lexical loop and return ``(context, trace)``.

    ``chat`` is the harness's own answer-call transport (injected so the
    loop is unit-testable and so the arm always drives the same model the
    other arms are answered with). The trace is persisted with the row:
    an arm whose search behaviour cannot be audited afterwards is not a
    measurement.
    """
    trace = {"rounds": 0, "rounds_budget": rounds, "queries": [],
             "windows": [], "inspected": 0, "served": 0, "plan_failures": 0,
             "fallback": False, "top_k": top_k, "per_round_k": per_round_k,
             "max_queries": max_queries, "session_weight": session_weight}
    if not len(archive):
        return "", trace
    span = archive.span()
    # position -> (record, raw lexical). Raw scores are what the
    # serve-time fusion ranks; the per-query fused value decided only
    # which turns this query inspected. Each position is found at most
    # once, because every search excludes what earlier rounds inspected.
    inspected: dict[int, tuple[ArchiveRecord, float]] = {}

    def _ranked() -> list[Hit]:
        return fuse([(pos, rec, raw)
                     for pos, (rec, raw) in inspected.items()],
                    session_weight)

    issued: list[str] = []
    for round_no in range(1, rounds + 1):
        read = [h.record.text for h in _ranked()]
        raw = chat(_REFIND_SYSTEM,
                   plan_prompt(question, span, issued, read, round_no,
                               rounds, snippet_chars),
                   max_tokens=max_tokens)
        trace["rounds"] = round_no
        plan = parse_plan(raw)
        if plan is None:
            trace["plan_failures"] += 1
            if issued:
                break          # already have material; do not re-issue blind
            plan = {"queries": [question], "since": None, "until": None,
                    "done": False}
        stop_after = bool(plan["done"])
        if stop_after and issued:
            break
        queries = plan["queries"][:max_queries] or (
            [question] if not issued else [])
        if not queries:
            break
        since, until = _norm_window(plan["since"]), _norm_window(plan["until"])
        trace["windows"].append({"since": since, "until": until})
        for query in queries:
            issued.append(query)
            for hit in archive.search(query, top_k=per_round_k, since=since,
                                      until=until, exclude=set(inspected),
                                      session_weight=session_weight):
                inspected[hit.index] = (hit.record, hit.lexical)
        if stop_after:
            break
    if not inspected:
        # Every round came back empty — most likely a window that parsed
        # cleanly but sat outside the archive. Serving nothing here scores
        # like a genuine miss and looks like one in the artifact, so take
        # one unrestricted look at the question and record that we did.
        trace["fallback"] = True
        for hit in archive.search(question, top_k=per_round_k,
                                  session_weight=session_weight):
            inspected[hit.index] = (hit.record, hit.lexical)
    served = sorted(_ranked()[:top_k],
                    key=lambda h: (h.record.ordinal, str(h.record.session)))
    trace["queries"] = issued
    trace["inspected"] = len(inspected)
    trace["served"] = len(served)
    return "\n\n".join(h.record.text for h in served), trace
