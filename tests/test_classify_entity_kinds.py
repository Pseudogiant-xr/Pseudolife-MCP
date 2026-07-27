"""Offline entity-kind classifier -- scoping, batching, robustness.

Scoping is the dominant token lever: an entity only matters if it carries a
transient-looking attribute, because otherwise every one of its facts resolves
evergreen whatever its kind. Measured on the live bank that is 2415 facts ->
1005 entities -> 264 decision-relevant -> 233 needing model judgement.
"""
from __future__ import annotations

import json
from pathlib import Path

from evals import classify_entity_kinds as C

GOLD = Path(__file__).parent / "fixtures" / "entity_kinds_gold.json"


def test_scope_drops_entities_whose_kind_cannot_matter():
    rows = [("daemon", "deploy-status"),      # transient -> in scope
            ("readme", "purpose"),            # durable only -> out of scope
            ("readme", "author")]
    assert C.scope_entities(rows) == ["daemon"]


def test_scope_keeps_an_entity_with_any_transient_attribute():
    rows = [("proj", "language"), ("proj", "current-branch")]
    assert C.scope_entities(rows) == ["proj"]


def test_rule_classifies_confident_artifacts_without_a_model():
    for name in ("0-9-0-release", "2026-07-15-atlas-deploy", "pr-42-review",
                 "commit-eec67b1"):
        assert C.rule_kind(name) == "artifact"


def test_rule_abstains_when_unsure():
    for name in ("daemon", "pseudolife-mcp", "cortex-console"):
        assert C.rule_kind(name) is None


def test_batched_splits_evenly_and_keeps_every_item():
    items = [f"e{i}" for i in range(233)]
    batches = list(C.batched(items, 50))
    assert [len(b) for b in batches] == [50, 50, 50, 50, 33]
    assert [x for b in batches for x in b] == items


def test_parse_batch_reads_a_well_formed_response():
    text = '{"daemon": "system", "0-9-0-release": "artifact"}'
    assert C.parse_batch(text, ["daemon", "0-9-0-release"]) == {
        "daemon": "system", "0-9-0-release": "artifact"}


def test_parse_batch_tolerates_fenced_json():
    text = '```json\n{"daemon": "system"}\n```'
    assert C.parse_batch(text, ["daemon"]) == {"daemon": "system"}


def test_malformed_response_yields_no_labels_rather_than_guesses():
    """Every failure path defaults to evergreen -- which means emitting NO
    kind, so resolve_class falls back. Never invent a label."""
    assert C.parse_batch("I could not classify these.", ["daemon"]) == {}


def test_parse_batch_drops_unknown_kinds_and_unrequested_entities():
    text = '{"daemon": "banana", "not-asked": "system", "ok-one": "concept"}'
    assert C.parse_batch(text, ["daemon", "ok-one"]) == {"ok-one": "concept"}


def test_harness_loads_the_one_canonical_policy_without_torch():
    """The harness must use the SAME resolve_class as the write path, loaded
    by file path because the package __init__ pulls torch. If this regresses
    to a private copy, the backfill can write classes new writes would never
    reproduce -- and the drift would be invisible."""
    import sys
    from pseudolife_memory.memory import freshness
    assert C._freshness.resolve_class is not None
    for attr in ("deploy-status", "deployment-date", "schema-version", "owner"):
        assert (C._is_transient(attr)
                is (freshness.resolve_class("system", attr) == "volatile"))
    # Loaded standalone: the module object is NOT the package-imported one.
    assert C._freshness is not sys.modules.get("pseudolife_memory.memory.freshness")


def test_gold_set_is_well_formed_and_covers_the_ambiguous_class():
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    assert len(gold) >= 40
    assert {g["kind"] for g in gold} <= set(C.KINDS)
    # The whole point: the same attribute on both an artifact and a system.
    kinds_for_version = {g["kind"] for g in gold
                         if "version" in g.get("example_attribute", "")}
    assert {"artifact", "system"} <= kinds_for_version
