"""The whole bank state the native clean-exit steps must leave.

Each function takes the dump before a step and the dump after it, builds the
expected dump from the step's own inputs (the stored text, its source and
tags, the search query and the rows it should serve) and returns
``dbstate.diff(expected, after)`` plus the checks a literal cannot make. A
value the daemon computes is taken from ``after`` only once it is checked:
a clock against the step's window, a vector against the offline fixture's
(``embedding_fixture.mean_unit_vector``) or, under another model, for shape,
a surprise against ``w2e.fixture_surprises`` or, on a copied real bank,
for range. So an extra row, a rewritten column or a skipped write in any
table differs.

What each step writes was enumerated by dumping the bank around every step
of counters-restore, search-after-restart and the shutdown arm (offline
fixture, 22 November 2026): a store adds one ``entries`` row and one
``entries_id_seq`` value, a search one ``retrieval_events`` row and one
``retrieval_events_id_seq`` value, and a graceful stop writes only the
deferred ``access_count`` of the entries searches served since the start
(``cms_counters`` rows are outside every dump, ``dbstate.UNPORTED_ROWS``).
"""
from __future__ import annotations

import copy
import json
import math

import dbstate

# A fixture vector component, read back from pgvector's float4 text.
VECTOR_TOL = 1e-6
# A stored surprise, read back from REAL: the stores' own surprise tolerance
# (w2e.COUNTERS_SURPRISE_TOL).
SURPRISE_TOL = 1e-5
# A served dense score: the daemon's f32 cosine against the f64 oracle.
DENSE_TOL = 1e-6
# memory.bm25.weight under the default config (weighted_sum fusion):
# a served score is dense + BM25_WEIGHT * bm25, rounded to 4 places.
BM25_WEIGHT = 0.3

# Every entries column a plain memory_store writes, besides the ones it is
# given or computes (text, source, tags; id, ts, embedding, surprise).
STORED_ENTRY = {"band": "flat", "access_count": 0, "superseded_at": None,
                "superseded_by_text": None, "last_logical_turn": None, "episode_id": None,
                "episode_title": None, "slots": [], "authority": None,
                "distortion_tolerance": None, "dream_state": "pending", "reinforcements": 0,
                "explicit_reinforcements": 0}


def _table(state, name):
    return state["rows"][f"public.{name}"]


def _advance(state, sequence):
    """Advance ``sequence`` in ``state`` by one step; return the new value."""
    row = next(row for row in state["catalog"]["sequences"] if row[:2] == ["public", sequence])
    row[5] = row[3] if row[5] is None else row[5] + row[4]
    return row[5]


def _insert(state, table, row):
    data = _table(state, table)
    data["rows"].append([row[column] for column in data["columns"]])
    key = data["columns"].index("id")
    data["rows"].sort(key=lambda values: str(values[key]))


def _written(after, table, key, value):
    data = _table(after, table)
    column = data["columns"].index(key)
    return [dict(zip(data["columns"], row)) for row in data["rows"] if row[column] == value]


def _in_window(value, window):
    return isinstance(value, float) and window[0] <= value <= window[1]


def _vector_problems(text, embedding, vector):
    try:
        values = json.loads(embedding)
    except (TypeError, ValueError):
        return [f"entry {text[:40]!r}: embedding is not a vector"]
    if len(values) != 1024 or not all(isinstance(v, (int, float)) and math.isfinite(v)
                                      for v in values) or not any(values):
        return [f"entry {text[:40]!r}: invalid durable embedding"]
    if vector is not None and any(abs(a - b) > VECTOR_TOL for a, b in zip(values, vector)):
        return [f"entry {text[:40]!r}: embedding differs from the offline fixture's vector"]
    return []


def store_diffs(before, after, *, text, source, tags, window, surprise, vector):
    """One ``memory_store`` of ``text``: exactly one new entries row and one
    sequence value. ``surprise`` and ``vector`` are the expected values, or
    None where they cannot be known (a copied bank's neighbours, another
    model): those are then checked for range and shape only."""
    expected = copy.deepcopy(before)
    entry_id = _advance(expected, "entries_id_seq")
    problems = []
    written = _written(after, "entries", "id", entry_id)
    row = {**STORED_ENTRY, "id": entry_id, "text": text, "source": source, "tags": list(tags)}
    if len(written) == 1 and written[0]["text"] == text:
        got = written[0]
        if not _in_window(got["ts"], window):
            problems.append(f"entry {text[:40]!r}: clock outside the store's window")
        problems += _vector_problems(text, got["embedding"], vector)
        value = got["surprise"]
        if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
            problems.append(f"entry {text[:40]!r}: surprise {value!r} is outside [0, 1]")
        elif surprise is not None and abs(value - surprise) > SURPRISE_TOL:
            problems.append(f"entry {text[:40]!r}: surprise {value!r}, expected {surprise!r}")
        # Checked above; the literal comparison below covers every other column.
        row.update(ts=got["ts"], embedding=got["embedding"], surprise=value)
    else:
        problems.append(f"store of {text[:40]!r} did not write entry {entry_id}")
        row.update(ts=None, embedding=None, surprise=None)
    _insert(expected, "entries", row)
    return problems + dbstate.diff(expected, after)


def served_rows(hits):
    """The ``served`` list of a dense-channel search over the flat band:
    ``hits`` is (entry_id, dense, bm25, surprise) in rank order."""
    return [{"via": None, "bank": "flat", "rank": rank, "entry_id": entry_id,
             "score": round(dense + BM25_WEIGHT * bm25, 4),
             "components": {"band": "flat", "band_depth": 0, "bm25": bm25, "channel": "dense",
                            "dense": dense, "recency": 0.0, "recency_boost": 0.0,
                            "source_mult": 1.0, "supersession_mult": 1.0, "surprise": surprise}}
            for rank, (entry_id, dense, bm25, surprise) in enumerate(hits)]


def search_params(top_k, tags, candidates):
    """``retrieval_events.params`` of a default-config ``memory_search``."""
    return {"bm25": {"b": 0.75, "k1": 1.5, "top_n": 20, "weight": BM25_WEIGHT, "enabled": True,
                     "min_score": 0.1},
            "top_k": top_k,
            "filters": {"tags": tags, "bands": None, "sources": None, "episodes": None,
                        "min_logical_turn": None},
            "reranker": {"fired": False, "top_n": 20, "enabled": False, "skip_reason": None,
                         "scoring_policy": "complete_pool_or_skip",
                         "candidate_count": candidates, "scored_candidates": 0},
            "timeline": {"fired": False, "enabled": False}, "min_score": 0.25, "band_count": 1,
            "recency_boost": False,
            "candidate_pool": {"fusion": "weighted_sum", "pool_size": candidates,
                               "multiplier": 1, "rerank_position": "after_cut"},
            "hide_superseded": False, "min_score_explicit": False, "contiguity_neighbors": 0,
            "recency_base_half_life_s": 86400.0}


def _served_problems(served, expected):
    if not isinstance(served, list) or len(served) != len(expected):
        return ["search logged a different number of served entries"]
    problems = []
    for got, want in zip(served, expected):
        dense = got.get("components", {}).get("dense") if isinstance(got, dict) else None
        if not isinstance(dense, (int, float)) or abs(dense - want["components"]["dense"]) > DENSE_TOL:
            problems.append(f"served entry {want['entry_id']}: dense {dense!r}, expected "
                            f"{want['components']['dense']!r}")
    return problems


def search_diffs(before, after, *, query, window, expected=None):
    """One ``memory_search`` for ``query``: exactly one retrieval event and
    one sequence value. ``expected`` is {"served": ..., "params": ...} where
    both are known (the offline fixture on an empty bank); otherwise the
    logged rows are left to ``w2e.restart_served_diffs`` and the rest of the
    row still compares literally."""
    state = copy.deepcopy(before)
    event_id = _advance(state, "retrieval_events_id_seq")
    problems = []
    written = _written(after, "retrieval_events", "id", event_id)
    row = {"id": event_id, "query_text": query, "origin": "search", "session_id": None,
           "episode_id": None, "served_facts": None}
    if len(written) == 1 and written[0]["query_text"] == query:
        got = written[0]
        if not _in_window(got["created_at"], window):
            problems.append(f"retrieval event {event_id}: clock outside the search's window")
        served, params = got["served"], got["params"]
        if expected is not None:
            problems += _served_problems(served, expected["served"])
            # The dense scores are checked within DENSE_TOL above; the rest
            # of each served row is the literal below.
            served = copy.deepcopy(expected["served"])
            for want, have in zip(served, got["served"] if isinstance(got["served"], list) else []):
                if isinstance(have, dict) and isinstance(have.get("components"), dict):
                    want["components"]["dense"] = have["components"].get("dense")
            params = expected["params"]
        row.update(served=served, params=params, created_at=got["created_at"])
    else:
        problems.append(f"search for {query!r} did not log retrieval event {event_id}")
        row.update(served=None, params=None, created_at=None)
    _insert(state, "retrieval_events", row)
    return problems + dbstate.diff(state, after)


def stop_diffs(before, stopped, served_counts):
    """A graceful stop: every row as before except the deferred access
    counts, each entry's raised by the times searches served it."""
    expected = copy.deepcopy(before)
    entries = _table(expected, "entries")
    id_column = entries["columns"].index("id")
    count_column = entries["columns"].index("access_count")
    for row in entries["rows"]:
        row[count_column] += served_counts.get(row[id_column], 0)
    return dbstate.diff(expected, stopped)


def logged_served(state, query):
    """The ``served`` list of the newest search event for ``query`` in a dump."""
    events = [row for row in _written(state, "retrieval_events", "query_text", query)
              if row["origin"] == "search"]
    return max(events, key=lambda row: row["id"])["served"] if events else None
