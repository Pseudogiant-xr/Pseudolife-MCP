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
