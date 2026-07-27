"""Applying an entity-kind artifact -- planning is pure and reviewable."""
from __future__ import annotations

import pytest

from evals import apply_entity_kinds as A


@pytest.mark.parametrize("attribute", [
    "deploy-status", "deployment-status", "schema-version", "current-branch",
    "live-url", "build-state", "deployment-date", "commit-hash", "language",
])
@pytest.mark.parametrize("kind", ["system", "artifact", "concept", None])
def test_apply_uses_the_same_policy_as_the_write_path(kind, attribute):
    """The recompute delegates to the one canonical resolve_class. If this
    ever forks into a private copy, the backfill writes classes new writes
    would never reproduce, and nothing would surface the divergence."""
    from pseudolife_memory.memory import freshness
    assert A._resolve(kind, attribute) == freshness.resolve_class(kind, attribute)


def test_plan_marks_only_system_transient_pairs_volatile():
    labels = {"daemon": "system", "0-9-0-release": "artifact"}
    rows = [("daemon", "schema-version"), ("daemon", "language"),
            ("0-9-0-release", "schema-version")]
    assert A.plan_updates(labels, rows) == [("daemon", "schema-version", "volatile")]


def test_plan_is_empty_without_labels():
    rows = [("daemon", "schema-version")]
    assert A.plan_updates({}, rows) == []


def test_plan_skips_pairs_already_at_the_target_class():
    """A pair whose stored class already matches what the policy would
    compute today produces no update -- the recompute is idempotent on a
    bank that's already converged. `daemon/schema-version` resolves to
    "volatile" under the "system" kind (see the parametrized policy test
    above); stashing that same value in `current` must suppress the update.

    (The brief's literal form built `rows` as a list of 3-tuples and
    immediately stripped the third element via a list comprehension to
    reach the same `[("daemon", "schema-version")]` -- a redundant
    round-trip that added no coverage. Written directly here; the actual
    test -- does an already-correct `current` entry suppress the write --
    is unchanged and was verified to genuinely fail if `plan_updates`
    ignored `current`.)"""
    labels = {"daemon": "system"}
    rows = [("daemon", "schema-version")]
    assert A.plan_updates(labels, rows,
                          current={("daemon", "schema-version"): "volatile"}) == []


def test_merged_labels_do_not_revert_an_entity_missing_from_a_rerun():
    """A re-run whose batch failed omits an entity rather than guessing. That
    omission must not revert a previously-correct kind: the artifact is an
    overlay on the stored kinds, not a replacement for them."""
    stored = {"daemon": "system"}
    artifact = {"console": "system"}          # daemon absent from this run
    merged = {**stored, **artifact}
    rows = [("daemon", "schema-version"), ("console", "deploy-status")]

    # With the merge, daemon keeps volatile (already at target -> no update).
    assert A.plan_updates(merged, rows, current={
        ("daemon", "schema-version"): "volatile"}) == [
        ("console", "deploy-status", "volatile")]

    # Without the merge, daemon would be reverted -- the bug this pins.
    assert ("daemon", "schema-version", "evergreen") in A.plan_updates(
        artifact, rows, current={("daemon", "schema-version"): "volatile"})
