"""Unit tests for evals/longmemeval_bench.py's fact-line composition (Task 6).

``build_contexts`` composes one line per served cortex fact, with garnish
from ``svc.history()``. That composition was factored into
``_compose_fact_line`` specifically so this could run offline — pure
dict-in-dict-out, no GPU, no service, no server — while still pinning the
set-slot extension: a set entry's ``value`` (already composed by
``cortex_search``) is used verbatim, and its garnish becomes "former
members" pulled from the set-shaped ``history()`` "removed" events, rather
than the scalar "earlier values" chain.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from longmemeval_bench import _compose_fact_line  # noqa: E402


def test_scalar_fact_line_has_no_garnish_without_history():
    f = {"entity": "user", "attribute": "city", "value": "Sydney"}
    assert _compose_fact_line(f, []) == "user — city: Sydney"


def test_scalar_fact_line_shows_earlier_values_oldest_first():
    f = {"entity": "payments-db", "attribute": "host", "value": "db-prod-2"}
    versions = [
        {"value": "db-prod-1"}, {"value": "db-prod-2"},
    ]
    got = _compose_fact_line(f, versions)
    assert got == ("payments-db — host: db-prod-2  "
                   "(earlier values, oldest first: db-prod-1)")


def test_scalar_fact_line_dedups_repeat_of_current_value_from_garnish():
    """The last version IS the current value (excluded via versions[:-1]),
    and any earlier entry equal to the current value is filtered too —
    unchanged behavior from before Task 6."""
    f = {"entity": "x", "attribute": "y", "value": "z"}
    versions = [{"value": "z"}, {"value": "z"}]
    assert _compose_fact_line(f, versions) == "x — y: z"


def test_set_fact_line_uses_composed_value_verbatim():
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "road bike; gravel bike (2 members)"}
    assert (_compose_fact_line(f, [])
            == "user — bikes owned: road bike; gravel bike (2 members)")


def test_set_fact_line_garnishes_former_members_oldest_first():
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "gravel bike (1 members)"}
    versions = [
        {"value": "road bike", "event": "added", "at": 1.0},
        {"value": "gravel bike", "event": "added", "at": 2.0},
        {"value": "road bike", "event": "removed", "at": 3.0},
    ]
    got = _compose_fact_line(f, versions)
    assert got == ("user — bikes owned: gravel bike (1 members)  "
                   "(former members: road bike)")


def test_set_fact_line_no_garnish_when_nothing_removed():
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "road bike (1 members)"}
    versions = [{"value": "road bike", "event": "added", "at": 1.0}]
    assert (_compose_fact_line(f, versions)
            == "user — bikes owned: road bike (1 members)")


def test_set_fact_line_multiple_removed_members_ordered_by_versions():
    f = {"entity": "user", "attribute": "tags", "kind": "set",
         "value": "beta (1 members)"}
    versions = [
        {"value": "alpha", "event": "added", "at": 1.0},
        {"value": "beta", "event": "added", "at": 2.0},
        {"value": "gamma", "event": "added", "at": 3.0},
        {"value": "alpha", "event": "removed", "at": 4.0},
        {"value": "gamma", "event": "removed", "at": 5.0},
    ]
    got = _compose_fact_line(f, versions)
    assert got == ("user — tags: beta (1 members)  "
                   "(former members: alpha -> gamma)")
