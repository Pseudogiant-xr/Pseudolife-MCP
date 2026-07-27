"""Applying an entity-kind artifact -- planning is pure and reviewable."""
from __future__ import annotations

import pytest

from evals import apply_entity_kinds as A
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


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

    # Even without the merge, plan_updates' own unlabelled-entity guard (see
    # test_unlabelled_entity_does_not_revert_a_deliberate_volatile) now
    # refuses to revert daemon directly -- the merge remains needed to
    # *propagate* a previously-known kind onto facts plan_updates hasn't
    # recomputed yet, not to prevent this particular revert.
    assert ("daemon", "schema-version", "evergreen") not in A.plan_updates(
        artifact, rows, current={("daemon", "schema-version"): "volatile"})


def test_unlabelled_entity_does_not_revert_a_deliberate_volatile():
    """A batch failure yields no label. "Unclassified" is not evidence for
    evergreen -- reverting a deliberate marking changes data on a failure
    path, which is exactly what this design forbids."""
    rows = [("pseudolife-mcp", "deployed-schema-version")]
    assert A.plan_updates({}, rows, current={
        ("pseudolife-mcp", "deployed-schema-version"): "volatile"}) == []


def test_stored_slow_is_never_touched():
    """resolve_class can only emit evergreen or volatile, so a stored `slow`
    is provably deliberate and must survive every apply."""
    rows = [("proj", "release-cadence")]
    assert A.plan_updates({"proj": "system"}, rows, current={
        ("proj", "release-cadence"): "slow"}) == []
    assert A.plan_updates({}, rows, current={
        ("proj", "release-cadence"): "slow"}) == []


def test_evergreen_to_volatile_flip_still_happens():
    """The guard must not neuter the backfill's actual purpose."""
    rows = [("daemon", "deploy-status")]
    assert A.plan_updates({"daemon": "system"}, rows, current={
        ("daemon", "deploy-status"): "evergreen"}) == [
        ("daemon", "deploy-status", "volatile")]


def test_precondition_fails_clearly_on_a_pre_v24_bank(pg_conn):
    """entity_kinds ships in schema v24. A bank that hasn't been migrated
    yet must get a named cause and remedy, not a raw UndefinedTable
    traceback -- and this check must run before the dry-run SELECT that
    would otherwise hit the missing table first."""
    pg_conn.execute("DROP TABLE entity_kinds")
    pg_conn.commit()
    with pytest.raises(SystemExit, match="schema v24"):
        A._require_entity_kinds_table(pg_conn)


def test_precondition_passes_when_entity_kinds_exists(pg_conn):
    A._require_entity_kinds_table(pg_conn)  # must not raise
