import pytest
from pseudolife_memory.memory.relation_quality import infer_type, TYPE_CONSTRAINTS


@pytest.mark.parametrize("name,expected", [
    ("user", "person"), ("the user", "person"),
    ("schema 11", "concept"), ("schema v8", "concept"), ("11", "concept"),
    ("0.2.0", "concept"), ("master", "concept"),
    ("docker compose -f ops/docker-compose.yml up -d", "concept"),
    ("config.yaml", "file"), ("ops/backup.ps1", "file"),
    ("postgres", "datastore"), ("pg", "datastore"), ("chromadb", "datastore"),
    ("docker", "runtime"), ("docker-desktop", "runtime"), ("windows 11", "runtime"),
    ("pseudolife-daemon", "service"), ("daemon", "service"),
    ("gemma 4 e2b sidecar", "service"), ("live daemon", "service"),
    ("memory_recall", "tool"),
    ("cortex console", None), ("networkx", None), ("nightly backup folder", None),
])
def test_infer_type(name, expected):
    assert infer_type(name) == expected


def test_constraints_cover_structural_relations():
    assert set(TYPE_CONSTRAINTS) == {"runs-on", "hosts", "stores-data-in", "part-of"}
    assert TYPE_CONSTRAINTS["runs-on"][1] == {"runtime", "host"}


from pseudolife_memory.memory.relation_quality import edge_confidence


def test_clean_specific_edge():
    # daemon(service) runs-on docker(runtime) — compatible
    assert edge_confidence("the daemon", "runs-on", "docker") == 0.70


def test_related_to_is_low_base():
    assert edge_confidence("x", "related-to", "y") == 0.45


def test_type_violation_penalized():
    # user(person) is not a valid runs-on src
    assert edge_confidence("user", "runs-on", "windows 11") == 0.175
    # schema(concept) is not a valid runs-on dst
    assert edge_confidence("the daemon", "runs-on", "schema 11") == 0.175
    # command-string is not a valid stores-data-in dst
    assert edge_confidence("the daemon", "stores-data-in",
                           "docker compose -f ops/x.yml up") == 0.175


def test_unknown_type_is_neutral():
    # cortex console (unknown) part-of daemon — src unknown -> no penalty
    assert edge_confidence("cortex console", "part-of", "the daemon") == 0.70


def test_non_structural_relation_never_penalized():
    # 'uses' has no constraint even if types look odd
    assert edge_confidence("user", "uses", "schema 11") == 0.70


def test_datastore_can_be_hosted_and_run_on():
    # a runtime hosting a datastore is legitimate (docker hosts postgres) — and
    # the inverse (a datastore running on a runtime). Regression for the backfill
    # dry-run finding: these must NOT be flagged as type-violations.
    assert edge_confidence("docker-desktop", "hosts", "postgres") == 0.70
    assert edge_confidence("postgres", "runs-on", "docker-desktop") == 0.70


from pseudolife_memory.memory.relation_quality import is_hard_type_violation


def test_hard_violation_when_both_typed_and_incompatible():
    # user=person, windows 11=runtime; runs-on src must be service/process/... not person
    assert is_hard_type_violation("user", "runs-on", "windows 11") is True


def test_no_violation_when_compatible():
    # daemon=service, docker=runtime: runs-on service->runtime is allowed
    assert is_hard_type_violation("daemon", "runs-on", "docker") is False


def test_no_violation_when_an_endpoint_is_untyped():
    # an arbitrary junk endpoint is None-typed -> neutral, never a hard violation
    assert is_hard_type_violation("zxqw blob", "runs-on", "docker") is False


def test_no_violation_for_unconstrained_relation():
    # related-to has no TYPE_CONSTRAINTS entry -> never a hard violation
    assert is_hard_type_violation("user", "related-to", "windows 11") is False
