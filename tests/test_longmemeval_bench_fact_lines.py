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
         "value": "road bike; gravel bike (2 members)",
         "members": [{"value": "road bike"}, {"value": "gravel bike"}]}
    assert (_compose_fact_line(f, [])
            == "user — bikes owned: road bike; gravel bike (2 members)")


def test_set_fact_line_garnishes_former_members_oldest_first():
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "gravel bike (1 members)",
         "members": [{"value": "gravel bike"}]}
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
         "value": "road bike (1 members)",
         "members": [{"value": "road bike"}]}
    versions = [{"value": "road bike", "event": "added", "at": 1.0}]
    assert (_compose_fact_line(f, versions)
            == "user — bikes owned: road bike (1 members)")


def test_set_fact_line_multiple_removed_members_ordered_by_versions():
    f = {"entity": "user", "attribute": "tags", "kind": "set",
         "value": "beta (1 members)",
         "members": [{"value": "beta"}]}
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


def test_set_fact_line_omits_removed_member_that_is_currently_re_added():
    """F3 (Task 6 review): remove-then-re-add leaves a "removed" event for
    the value AND a fresh current member carrying it (re-adding mints a
    new row rather than resurrecting the old one). The garnish must not
    call a CURRENTLY-current member "former" — filtered against
    ``f["members"]``, normalised so a re-add under different casing still
    matches."""
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "Road Bike; gravel bike (2 members)",
         "members": [{"value": "Road Bike"}, {"value": "gravel bike"}]}
    versions = [
        {"value": "road bike", "event": "added", "at": 1.0},
        {"value": "road bike", "event": "removed", "at": 2.0},
        {"value": "gravel bike", "event": "added", "at": 3.0},
        {"value": "Road Bike", "event": "added", "at": 4.0},   # re-add
    ]
    got = _compose_fact_line(f, versions)
    # "road bike" was removed, but it (as "Road Bike") is currently a
    # member again -- no "former members" garnish should appear at all.
    assert got == "user — bikes owned: Road Bike; gravel bike (2 members)"
    assert "former members" not in got


def test_set_fact_line_still_garnishes_other_removed_members_when_one_is_readded():
    """Companion to the above: a re-added member is dropped from the
    garnish, but an UNRELATED removed member (never re-added) still shows."""
    f = {"entity": "user", "attribute": "bikes owned", "kind": "set",
         "value": "road bike (1 members)",
         "members": [{"value": "road bike"}]}
    versions = [
        {"value": "gravel bike", "event": "added", "at": 1.0},
        {"value": "road bike", "event": "added", "at": 2.0},
        {"value": "gravel bike", "event": "removed", "at": 3.0},
        {"value": "road bike", "event": "removed", "at": 4.0},
        {"value": "road bike", "event": "added", "at": 5.0},   # re-add
    ]
    got = _compose_fact_line(f, versions)
    assert got == ("user — bikes owned: road bike (1 members)  "
                   "(former members: gravel bike)")


def test_make_extractor_threads_system_prompt_file(tmp_path):
    """--system-prompt-file makes prompt-variant runs first-class (the
    extraction-variance baseline runs the control prompt through the
    identical code path). Absent file -> the shipped prompt, untouched."""
    from longmemeval_bench import _make_extractor
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT

    default = _make_extractor("http://127.0.0.1:9/v1", None)
    assert default.system_prompt == _SYSTEM_PROMPT
    p = tmp_path / "variant.txt"
    p.write_text("consolidate notes THE VARIANT WAY", encoding="utf-8")
    variant = _make_extractor("http://127.0.0.1:9/v1", str(p))
    assert variant.system_prompt == "consolidate notes THE VARIANT WAY"
