import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path_factory):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path_factory.mktemp("dd-svc"), database_url=pg_url)


def test_dry_run_writes_nothing(svc):
    svc.graph_relate("user", "runs-on", "windows 11", origin="agent")  # a violation
    before = svc._storage.load_graph()["edges"]
    out = svc.deep_dream(apply=False)
    after = svc._storage.load_graph()["edges"]
    assert out["dry_run"] is True
    assert [e["id"] for e in before] == [e["id"] for e in after]   # nothing superseded


def test_apply_supersedes_violation_and_rescores(svc):
    svc.graph_relate("user", "runs-on", "windows 11", origin="agent")     # violation
    svc.graph_relate("daemon", "runs-on", "docker", origin="agent")       # clean
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert out["superseded"] >= 1
    assert out["rescored"] >= 1
