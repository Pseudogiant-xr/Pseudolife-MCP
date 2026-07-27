"""The entity-kind -> freshness policy (schema v24).

Whether a fact rots is decided by what KIND of thing it is about, not by the
attribute name. `0-9-0-release / schema-version` is evergreen (a release is
frozen in time); `daemon / schema-version` is volatile. Same attribute,
opposite class -- that pair is the whole design in one test.
"""
from __future__ import annotations

import pytest

from pseudolife_memory.memory import freshness


def test_the_motivating_pair_same_attribute_opposite_class():
    assert freshness.resolve_class("artifact", "schema-version") == "evergreen"
    assert freshness.resolve_class("system", "schema-version") == "volatile"


@pytest.mark.parametrize("attribute", [
    "deploy-status", "current-branch", "schema-version", "running-model",
    "health-check-status", "live-url", "build-state", "deployment-status",
])
def test_system_entities_yield_volatile_for_transient_attributes(attribute):
    assert freshness.resolve_class("system", attribute) == "volatile"


@pytest.mark.parametrize("attribute", ["language", "owner", "purpose", "licence"])
def test_system_entities_stay_evergreen_for_durable_attributes(attribute):
    assert freshness.resolve_class("system", attribute) == "evergreen"


@pytest.mark.parametrize("attribute", [
    "deployment-date", "merge-date", "commit-date", "commit-hash",
    "asserted-at", "replicate-count", "cortex-score",
])
def test_event_attributes_never_decay_even_on_a_live_system(attribute):
    """An EVENT is permanently true; only STATE rots. "deployment-date" says
    when a deploy happened and stays correct forever, so it must not inherit
    the volatility of the "deployment" prefix -- the event suffix wins."""
    assert freshness.resolve_class("system", attribute) == "evergreen"


@pytest.mark.parametrize("kind", ["artifact", "concept"])
@pytest.mark.parametrize("attribute", ["deploy-status", "schema-version", "current-branch"])
def test_artifacts_and_concepts_never_decay(kind, attribute):
    """Structural guarantee: the harmful error direction -- a durable fact
    silently decaying -- cannot reach these 282 facts at all."""
    assert freshness.resolve_class(kind, attribute) == "evergreen"


@pytest.mark.parametrize("kind", [None, "", "nonsense", "SYSTEM_TYPO"])
def test_unknown_kind_defaults_evergreen_not_volatile(kind):
    """normalize_class sends unknown to volatile -- right for world facts and
    the exact inversion here. An unclassified personal fact must not decay."""
    assert freshness.resolve_class(kind, "deploy-status") == "evergreen"


def test_kind_matching_is_case_and_whitespace_insensitive():
    assert freshness.resolve_class("  System ", "deploy-status") == "volatile"


def test_entity_kinds_vocabulary_is_exported():
    assert freshness.ENTITY_KINDS == ("artifact", "system", "concept")
