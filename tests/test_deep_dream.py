import pytest

from pseudolife_memory.graph import norm_name
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


def test_dry_run_previews_merge_and_junk(svc):
    # Two synonym entities sharing two entries -> a high-sim near-pair name-contained -> merge preview.
    svc.cortex_write("daemon", "role", "serves MCP", support="user")
    svc.cortex_write("daemon", "note", "the daemon runs in docker", support="user")
    svc.cortex_write("live daemon", "role", "serves MCP", support="user")
    svc.cortex_write("live daemon", "note", "the daemon runs in docker", support="user")
    svc.graph_relate("2", "related-to", "daemon", origin="agent")   # 'live daemon' co-mentions
    out = svc.deep_dream(apply=False)
    assert out["dry_run"] is True
    assert "would_merge_propose" in out and "would_junk" in out
    assert svc._storage.pending_entity_proposals() == []            # dry-run writes nothing


def test_apply_persists_entity_proposals(svc):
    svc.cortex_write("daemon", "role", "serves MCP", support="user")
    svc.cortex_write("daemon", "note", "runs in docker", support="user")
    svc.cortex_write("live daemon", "role", "serves MCP", support="user")
    svc.cortex_write("live daemon", "note", "runs in docker", support="user")
    svc.cortex_write("42", "note", "a bare number entity", support="user")
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert "merge_proposed" in out and "junk_proposed" in out
    assert out["junk_proposed"] >= 1


def test_accept_entity_merge_folds(svc):
    with svc._lock:
        svc._ensure_init()
        a = svc._resolve_or_create_entity("daemon")["id"]
        b = svc._resolve_or_create_entity("live daemon")["id"]
    pid = svc._storage.insert_entity_proposal("merge", b, a, 0.99, "token-subset", __import__("time").time())
    out = svc.graph_accept_entity_merge(pid)
    assert out["accepted"] is True and out["into"] == "daemon"
    # folded away: no distinct 'live-daemon' node survives; the name now resolves
    # (via alias) to the merge target 'daemon'.
    folded = svc._storage.find_entity(norm_name("live daemon"))
    assert folded is not None and folded["id"] == a and folded["canonical"] == "daemon"
    assert svc._storage.pending_entity_proposals() == []


def test_accept_entity_junk_deletes(svc):
    with svc._lock:
        svc._ensure_init()
        n = svc._resolve_or_create_entity("2")["id"]
    pid = svc._storage.insert_entity_proposal("junk", n, None, None, "bare-number", __import__("time").time())
    out = svc.graph_accept_entity_junk(pid)
    assert out["accepted"] is True and out["entity"] == "2"
    assert svc._storage.find_entity(norm_name("2")) is None


def test_reject_entity_proposal(svc):
    with svc._lock:
        svc._ensure_init()
        n = svc._resolve_or_create_entity("merged")["id"]
    pid = svc._storage.insert_entity_proposal("junk", n, None, None, "status-word", __import__("time").time())
    assert svc.graph_reject_entity_proposal(pid)["rejected"] is True
    assert svc._storage.find_entity(norm_name("merged")) is not None    # NOT deleted on reject
    assert svc._storage.pending_entity_proposals() == []
