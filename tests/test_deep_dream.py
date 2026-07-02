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
    # Junk-shaped names no longer enter via fact writes (write-time gate,
    # 2026-07-02); seed a legacy junk node directly so the deep-dream
    # detection path stays covered.
    svc._storage.ensure_entity("42", display="42")
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert "merge_proposed" in out and "junk_proposed" in out
    assert out["junk_proposed"] >= 1


def _stage_link_pair(svc):
    """Two similar-context entities with NO memory_traces rows, no shared edge
    and no name containment -> a deep-dream LINK candidate whose evidence can
    only come from the token-mention scan (the live-bank shape: graph entities
    rarely have traces)."""
    with svc._lock:
        svc._ensure_init()
        svc._resolve_or_create_entity("atlas queue")
        svc._resolve_or_create_entity("review workbench")
    for text in (
        "the atlas queue lists pending graph findings for human review",
        "accepting an atlas queue proposal folds the entities together",
        "the review workbench shows unsettled graph findings to the operator",
        "accepting a review workbench proposal merges the two entities",
    ):
        assert svc.store(text, source="dd-test")["stored"] is True


def _find_candidate(out, a="atlas queue", b="review workbench"):
    for c in out["candidates"]:
        if {c["src"], c["dst"]} == {a, b}:
            return c
    return None


def test_candidate_snippets_fall_back_to_mention_scan(svc):
    _stage_link_pair(svc)
    out = svc.deep_dream(apply=False)
    c = _find_candidate(out)
    assert c is not None, out["candidates"]
    assert c["src_snippets"] and c["dst_snippets"]     # evidence, not just a score


def test_candidates_respect_dismissed_pairs(svc):
    _stage_link_pair(svc)
    assert _find_candidate(svc.deep_dream(apply=False)) is not None
    svc.graph_dismiss_duplicate("atlas queue", "review workbench")
    assert _find_candidate(svc.deep_dream(apply=False)) is None


def test_dry_run_marks_already_proposed(svc):
    # The apply path dedupes against existing entity_proposals rows (any
    # status); the dry-run preview must say so instead of over-counting.
    import time
    with svc._lock:
        svc._ensure_init()
        a = svc._resolve_or_create_entity("42")["id"]
        svc._resolve_or_create_entity("7")
    svc._storage.insert_entity_proposal("junk", a, None, None, "bare-number", time.time())
    out = svc.deep_dream(apply=False)
    flags = {j["entity"]: j["already_proposed"] for j in out["would_junk"]}
    assert flags["42"] is True
    assert flags["7"] is False


def test_apply_writes_graph_snapshot(svc):
    import json
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True and out["snapshot"]
    snap_dir = svc.data_dir / "graph_snapshots"
    path = snap_dir / out["snapshot"]
    assert path.is_file()
    tables = json.loads(path.read_text(encoding="utf-8"))
    assert set(tables) == {"entities", "edges", "entity_aliases",
                           "edge_proposals", "entity_proposals"}


def test_apply_refuses_when_snapshot_unwritable(svc):
    # A file squatting on the snapshot dir path makes mkdir fail -> apply
    # must refuse and write NOTHING.
    (svc.data_dir / "graph_snapshots").write_text("not a dir", encoding="utf-8")
    with svc._lock:
        svc._ensure_init()
        svc._resolve_or_create_entity("42")        # junk-shaped: would be proposed
    out = svc.deep_dream(apply=True)
    assert out.get("error") == "snapshot_failed"
    assert svc._storage.pending_entity_proposals() == []


def test_apply_prunes_old_snapshots(svc):
    snap_dir = svc.data_dir / "graph_snapshots"
    snap_dir.mkdir()
    for stamp in ("20200101-000001", "20200101-000002", "20200101-000003"):
        (snap_dir / f"graph-{stamp}.json").write_text("{}", encoding="utf-8")
    svc.config.memory.deep_dream.snapshot_keep = 2
    out = svc.deep_dream(apply=True)
    names = sorted(p.name for p in snap_dir.glob("graph-*.json"))
    assert len(names) == 2
    assert out["snapshot"] in names                # the fresh one survives


def test_candidate_snippets_are_truncated(svc):
    _stage_link_pair(svc)
    svc.config.memory.deep_dream.snippet_max_chars = 40
    out = svc.deep_dream(apply=False)
    c = _find_candidate(out)
    assert c is not None
    snips = c["src_snippets"] + c["dst_snippets"]
    assert snips and all(len(s) <= 40 for s in snips)


def test_deep_dream_can_omit_snippets(svc):
    _stage_link_pair(svc)
    out = svc.deep_dream(apply=False, include_snippets=False)
    c = _find_candidate(out)
    assert c is not None
    assert "src_snippets" not in c and "dst_snippets" not in c


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


def test_accept_entity_merge_target_deleted_cascades_proposal_away(svc):
    """A queued merge proposal is protected against a stale endpoint by the
    entity_proposals ON DELETE CASCADE FK on BOTH entity_id and into_id: if the
    `into` entity is junk-deleted after the proposal is queued, the proposal row
    cascades away with it, so graph_accept_entity_merge never sees a proposal
    pointing at a ghost — it returns a graceful `not_pending`, never an FK crash,
    and `from` is untouched. (This is why no accept-time endpoint re-check is
    needed at the caller; the schema enforces it.)"""
    with svc._lock:
        svc._ensure_init()
        frm = svc._resolve_or_create_entity("stale-merge-from")["id"]
        into = svc._resolve_or_create_entity("stale-merge-into")["id"]
    pid = svc._storage.insert_entity_proposal(
        "merge", frm, into, 0.99, "token-subset", __import__("time").time())
    assert svc._storage.delete_entity(into) is True     # target vanishes → CASCADE
    assert svc._storage.get_entity_proposal(pid) is None  # proposal cascaded away

    out = svc.graph_accept_entity_merge(pid)
    assert out["accepted"] is False and out["reason"] == "not_pending"
    assert svc._storage.find_entity(norm_name("stale-merge-from")) is not None


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
