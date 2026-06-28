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


def test_propose_then_accept_promotes_to_edge(svc):
    out = svc.graph_propose_links([
        {"src": "alpha", "relation": "related-to", "dst": "beta",
         "similarity": 0.9, "rationale": "co-discussed"}])
    assert out["proposed"] == 1
    pid = svc._storage.pending_proposals()[0]["id"]
    acc = svc.graph_accept_proposal(pid)
    assert acc["accepted"] is True
    live = {(e["src_id"], e["relation"], e["dst_id"])
            for e in svc._storage.load_graph()["edges"]}
    a = svc._storage.find_entity("alpha")["id"]
    b = svc._storage.find_entity("beta")["id"]
    assert (a, "related-to", b) in live
    assert svc._storage.pending_proposals() == []


def test_propose_drops_type_violation(svc):
    out = svc.graph_propose_links([
        {"src": "user", "relation": "runs-on", "dst": "windows 11"}])
    assert out["proposed"] == 0 and out["skipped"] == 1


def test_reject_marks_rejected(svc):
    svc.graph_propose_links([{"src": "alpha", "relation": "related-to", "dst": "beta"}])
    pid = svc._storage.pending_proposals()[0]["id"]
    assert svc.graph_reject_proposal(pid)["rejected"] is True
    assert svc._storage.pending_proposals() == []
