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


def test_apply_auto_deletes_structureless_junk_and_keeps_evidence_bearing(svc):
    # Guard-passing junk (no edges, <=1 fact) is deleted in the same apply —
    # the snapshot is the undo, the node re-mints on next mention. Junk with
    # any edge stays a proposal for review.
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity("42", display="42")              # structureless
        svc._storage.ensure_entity("43", display="43")              # evidence-bearing
    svc.graph_relate("43", "related-to", "daemon", origin="agent")  # degree 1
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert out["junk_deleted"] >= 1
    assert svc._storage.find_entity(norm_name("42")) is None
    assert svc._storage.find_entity(norm_name("43")) is not None
    pending = [p for p in svc._storage.pending_entity_proposals()
               if p.get("kind") == "junk"]
    assert any(p["entity_id"] == svc._storage.find_entity(norm_name("43"))["id"]
               for p in pending)


def test_apply_scopes_unattributed_entities_from_mentions(svc):
    # Entities without a current fact never get attribution from
    # backfill_entity_sources; the apply pass derives it from the sources of
    # their mentioning entries instead.
    with svc._lock:
        svc._ensure_init()
        svc._resolve_or_create_entity("atlas queue")
    for text in (
        "the atlas queue lists pending graph findings for review",
        "accepting an atlas queue proposal folds the entities together",
    ):
        assert svc.store(text, source="dd-scope-test")["stored"] is True
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True and out["scoped"] >= 1
    eid = svc._storage.find_entity(norm_name("atlas queue"))["id"]
    assert "dd-scope-test" in svc._storage.entity_sources_map().get(eid, [])


def test_accept_link_lifts_quarantined_confidence_past_dubious(svc):
    # An accepted proposal must leave the dubious_edges queue: the reviewed
    # edge is floored at 0.7 (> _DUBIOUS_CONF 0.6), not left at its 0.45
    # quarantine confidence for the next pass to re-flag.
    import time
    with svc._lock:
        svc._ensure_init()
        a = svc._resolve_or_create_entity("quarantine src")["id"]
        b = svc._resolve_or_create_entity("quarantine dst")["id"]
        pid = svc._storage.insert_proposal(
            a, "related-to", b, 0.45, None, "low-confidence dream edge",
            "dream-low-confidence", time.time())
    assert svc.graph_accept_proposal(pid)["accepted"] is True
    edge = next(e for e in svc._storage.load_graph()["edges"]
                if e["src_id"] == a and e["dst_id"] == b)
    assert edge["confidence"] >= 0.7
    # ...and the verdict must survive the NEXT apply: rescore_edges
    # recomputes every origin="agent" edge from names (related-to -> 0.45),
    # so the accepted edge is stored as a confirming action instead.
    assert edge["origin"] != "agent"
    assert svc.deep_dream(apply=True)["applied"] is True
    edge = next(e for e in svc._storage.load_graph()["edges"]
                if e["src_id"] == a and e["dst_id"] == b)
    assert edge["confidence"] >= 0.7


def test_apply_survives_junk_delete_of_a_mentioned_entity(svc):
    # Interaction regression: an entity that is junk-deleted this pass may
    # ALSO be unattributed with mentions — the scope-stamping loop iterates
    # the pre-apply entity list and must skip deleted ids, or the
    # entity_sources upsert FK-violates mid-apply (partial apply, lost
    # snapshot pointer).
    with svc._lock:
        svc._ensure_init()
        svc._storage.ensure_entity("44", display="44")   # bare-number junk
    for text in (
        "build 44 failed on the runner",
        "retrying build 44 after the cache purge",
    ):
        assert svc.store(text, source="dd-junk-scope")["stored"] is True
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert svc._storage.find_entity(norm_name("44")) is None
    # durable audit for the unattended deletion
    audited = [d for d in svc._storage.recent_entity_decisions(limit=50)
               if d.get("decided_by") == "dream-auto" and d.get("entity") == "44"]
    assert audited


def test_dream_alias_proposal_folds_thin_side_into_evidence_bearing(svc):
    # The alias screen compares a freshly-minted name against existing cortex
    # entities. When the NEW side carries the evidence (facts/edges) and the
    # existing side is a thin shell, the fold direction must still be
    # thin -> rich, not new -> existing verbatim (29 wrong-direction
    # proposals in the 2026-08-05 triage were this shape).
    from pseudolife_memory.graph import norm_name as nn
    svc.cortex_write("deployment pipeline", "role", "ships builds",
                     support="user")                      # thin existing shell
    svc.cortex_write("deploy pipeline", "role", "ships builds", support="user")
    svc.cortex_write("deploy pipeline", "stage", "build then test",
                     support="user")
    svc.graph_relate("deploy pipeline", "uses", "docker", origin="agent")
    rich = svc._storage.find_entity(nn("deploy pipeline"))["id"]
    thin = svc._storage.find_entity(nn("deployment pipeline"))["id"]
    key = None
    for r in svc._cortex.records:
        if r.status == "current" and r.entity == "deployment pipeline":
            key = r.key[0]
    assert key is not None
    filed = svc._propose_dream_alias_candidates(
        {nn("deploy pipeline"): "deploy pipeline"}, {key})
    assert filed == 1
    prop = [p for p in svc._storage.pending_entity_proposals()
            if p.get("kind") == "merge"
            and {p["entity_id"], p["into_id"]} == {rich, thin}]
    assert prop and prop[0]["entity_id"] == thin and prop[0]["into_id"] == rich


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
    # LINK candidates keep pool order and get no differential stamps: their
    # co-occurrence notes ARE the relation evidence (shared_mention_entries).
    assert "low_differential" not in c and "evidence_overlap" not in c


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


def test_deep_response_carries_enriched_merge_proposals(svc):
    import time as _t
    svc.graph_relate("enrich-a.py", "part-of", "enrich-core", origin="user")
    a = svc._storage.find_entity(norm_name("enrich-a.py"))["id"]
    b = svc._storage.ensure_entity("enrich-a", display="enrich a")
    pid = svc._storage.insert_entity_proposal(
        "merge", b, a, 1.0, "write-dedup: test", _t.time())
    out = svc.deep_dream(apply=False)
    mine = next(m for m in out["merge_proposals"] if m["id"] == pid)
    assert {"from", "into", "score", "reason"} <= set(mine)
    assert isinstance(mine["from"]["snippets"], list)
    assert isinstance(mine["into"]["scopes"], list)
    # oriented: 'into' is the higher-degree side (a has 1 edge, b has 0)
    assert mine["into"]["degree"] >= mine["from"]["degree"]
    assert mine["into"]["display"] == "enrich-a.py"


def test_apply_response_also_lists_merge_proposals(svc):
    import time as _t
    svc.graph_relate("enrich-c.py", "uses", "enrich-lib", origin="user")
    c = svc._storage.find_entity(norm_name("enrich-c.py"))["id"]
    d = svc._storage.ensure_entity("enrich-c", display="enrich c")
    pid = svc._storage.insert_entity_proposal(
        "merge", d, c, 0.9, "write-dedup: test2", _t.time())
    out = svc.deep_dream(apply=True)
    assert any(m["id"] == pid for m in out["merge_proposals"])


def test_partition_candidates_variant_conflict_stays_link():
    from pseudolife_memory.memory.graph_consolidation import partition_candidates
    pairs = [{"src_id": 1, "dst_id": 2, "src": "gemma-E4B Q4_K_M",
              "dst": "gemma-E4B", "similarity": 0.99}]
    ents = [{"id": 1, "display": "gemma-E4B Q4_K_M"},
            {"id": 2, "display": "gemma-E4B"}]
    merges, links = partition_candidates(pairs, ents, [])
    assert merges == [] and len(links) == 1
    # control: same-variant containment still partitions as a merge
    pairs2 = [{"src_id": 1, "dst_id": 2, "src": "update.ps1",
               "dst": "ops/update.ps1", "similarity": 0.99}]
    ents2 = [{"id": 1, "display": "update.ps1"},
             {"id": 2, "display": "ops/update.ps1"}]
    merges2, links2 = partition_candidates(pairs2, ents2, [])
    assert len(merges2) == 1 and links2 == []


# --- snippet evidence quality (2026-08-21 shadow comparison findings) ---------
#
# The live shadow-vs-triage comparison (evals/results/judge-shadow-live-
# 20260821.json) found 37% of pending merge proposals carried low-differential
# evidence: a side with no snippets, or both sides showing mostly the same
# entries. These tests pin the fixes: snippet evidence is decoupled from
# vector eligibility, per-side selection prefers exclusive entries, and rows
# that remain low-differential say so.


def _insert_merge(svc, frm_name, into_name):
    import time as _t
    st = svc._storage
    st.ensure_entity(frm_name, display=frm_name)
    st.ensure_entity(into_name, display=into_name)
    a = st.find_entity(frm_name)["id"]
    b = st.find_entity(into_name)["id"]
    pid = st.insert_entity_proposal("merge", a, b, 0.9, "test", _t.time())
    assert pid is not None
    return pid


def _merge_row(out, pid):
    return next(m for m in out["merge_proposals"] if m["id"] == pid)


def test_merge_snippets_attach_below_min_mentions(svc):
    # A side mentioned in exactly ONE entry sits below min_entity_mentions
    # (vector eligibility), but that entry is still its evidence — the judge
    # must not see "evidence: none" for it.
    assert svc.store("the epsilon parser handles rotated logs", source="sq")["stored"]
    assert svc.store("epsilon reader scans the cold archive", source="sq")["stored"]
    assert svc.store("an epsilon reader instance runs nightly", source="sq")["stored"]
    pid = _insert_merge(svc, "epsilon parser", "epsilon reader")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    assert row["from"]["snippets"] and row["into"]["snippets"]


def test_merge_snippets_attach_beyond_fallback_cap(svc):
    # max_fallback_mentions guards VECTORS against corpus-centroid entities;
    # it must not strip their displayed evidence (22 of the 27 empty-sided
    # entities on the 2026-08-30 live queue were exactly this class).
    svc.config.memory.deep_dream.max_fallback_mentions = 1
    assert svc.store("gamma hub node fans requests out", source="sq")["stored"]
    assert svc.store("the gamma hub node keeps a routing table", source="sq")["stored"]
    pid = _insert_merge(svc, "gamma hub", "gamma hub node")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    assert row["from"]["snippets"] and row["into"]["snippets"]


def test_merge_snippets_prefer_exclusive_evidence(svc):
    # Both sides are co-mentioned in the earliest entry and each has entries
    # of its own. Selection must lead with the exclusive evidence, not hand
    # both sides the same first-k shared entries.
    svc.config.memory.deep_dream.max_context_snippets = 2
    sh1 = "alpha proc feeds the alpha runner work queue"
    xd1 = "alpha proc parses incoming frames quickly"
    xd2 = "restarting alpha proc hourly keeps memory usage flat"
    xr1 = "the alpha runner restarts nightly after the sweep"
    xr2 = "alpha runner drains its backlog before shutdown"
    for text in (sh1, xd1, xd2, xr1, xr2):
        assert svc.store(text, source="sq")["stored"]
    pid = _insert_merge(svc, "alpha proc", "alpha runner")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    frm = row["from"] if row["from"]["display"] == "alpha proc" else row["into"]
    into = row["into"] if frm is row["from"] else row["from"]
    assert set(frm["snippets"]) == {xd1, xd2}
    assert set(into["snippets"]) == {xr1, xr2}
    assert row["low_differential"] is False
    assert row["evidence_overlap"] == 0.0


def test_merge_row_flags_one_sided_evidence_pool(svc):
    # The bare-vs-qualified shape: every entry mentioning the qualified name
    # also token-matches the bare one, so the qualified side's pool is wholly
    # contained in the bare side's. Exclusive-first selection makes the SHOWN
    # snippets disjoint — the flag must still fire, or diversification would
    # hide exactly the "no evidence of its own" case rule 1 exists for.
    svc.config.memory.deep_dream.max_context_snippets = 2
    for text in (
        "beta gadget service exports the metrics feed",
        "the beta gadget service restarts after deploys",
        "beta gadget parses the wire format alone",
        "a beta gadget instance runs on every host",
        "beta gadget ships as a standalone binary",
    ):
        assert svc.store(text, source="sq")["stored"]
    pid = _insert_merge(svc, "beta gadget", "beta gadget service")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    assert row["evidence_overlap"] == 0.0          # shown snippets ARE disjoint
    assert row["low_differential"] is True         # but one pool contains the other


def test_merge_row_flags_identical_evidence(svc):
    # No exclusive evidence exists for either side -> the row must say so
    # instead of presenting the shared entries as two independent stories.
    assert svc.store("beta gadget service exports the metrics feed", source="sq")["stored"]
    assert svc.store("the beta gadget service restarts after deploys", source="sq")["stored"]
    pid = _insert_merge(svc, "beta gadget", "beta gadget service")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    assert row["low_differential"] is True
    assert row["evidence_overlap"] == 1.0


def test_merge_row_flags_empty_side(svc):
    # An entity mentioned nowhere ships no snippets — the row must carry the
    # low-differential flag rather than a bare similarity score.
    assert svc.store("theta engine compiles the ruleset", source="sq")["stored"]
    assert svc.store("the theta engine caches compiled rules", source="sq")["stored"]
    pid = _insert_merge(svc, "zeta-orphan", "theta engine")
    row = _merge_row(svc.deep_dream(apply=False), pid)
    assert row["low_differential"] is True


def _file_svc(tmp_path):
    # File-mode service on purpose: these are pure-logic contracts that must
    # stay in the always-green set (no silent skip without the bench PG).
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path)


def test_stale_trace_ids_fall_back_to_mentions(tmp_path):
    # Trace rows pointing at pruned entries must not block the mention
    # fallback: a non-empty-but-unresolvable trace list is not evidence.
    svc = _file_svc(tmp_path)
    entities = [{"id": 1, "canonical": "widget", "display": "widget"},
                {"id": 2, "canonical": "gizmo", "display": "gizmo"}]
    entries = [{"id": 10, "text": "widget does things"},
               {"id": 11, "text": "gizmo spins"}]
    traces = {"widget": [999]}                     # entry 999 was pruned
    mentions = {1: frozenset({10}), 2: frozenset({11})}
    out = svc._attach_candidate_snippets(
        [{"src_id": 1, "dst_id": 2}], entities, entries, traces, 3,
        mentions=mentions, max_chars=None)
    assert out[0]["src_snippets"] == ["widget does things"]
    assert out[0]["dst_snippets"] == ["gizmo spins"]


def test_zero_snippet_budget_ships_no_snippets(tmp_path):
    # k=0 must mean "no snippets", not "one snippet" — the stop check runs
    # before the append.
    svc = _file_svc(tmp_path)
    entities = [{"id": 1, "canonical": "widget", "display": "widget"},
                {"id": 2, "canonical": "gizmo", "display": "gizmo"}]
    entries = [{"id": 10, "text": "widget does things"},
               {"id": 11, "text": "gizmo spins"}]
    mentions = {1: frozenset({10}), 2: frozenset({11})}
    out = svc._attach_candidate_snippets(
        [{"src_id": 1, "dst_id": 2}], entities, entries, {}, 0,
        mentions=mentions, max_chars=None)
    assert out[0]["src_snippets"] == [] and out[0]["dst_snippets"] == []


# --- store curation: lesson / world cross-key near-duplicate REVIEW listings --

_DUP_LESSON = ("Always take a pg_dump backup via ops/backup.ps1 before "
               "deploying the daemon to the homelab host.")


def _stage_lesson_dups(svc):
    svc.lesson_write("deploy daemon to homelab host", "approach", _DUP_LESSON)
    svc.lesson_write("deploy the daemon to the host", "pitfall", _DUP_LESSON)
    svc.lesson_write("train qlora on the 4090", "approach",
                     "Keep torch.compile ON; the fused CE kernel prevents VRAM spill.")


def _lesson_dup_pair(out):
    for c in out["lesson_duplicates"]:
        if {c["a_key"], c["b_key"]} == {"deploy-daemon-to-homelab-host|approach",
                                        "deploy-the-daemon-to-the-host|pitfall"}:
            return c
    return None


def test_dry_run_lists_cross_key_lesson_duplicates(svc):
    _stage_lesson_dups(svc)
    out = svc.deep_dream(apply=False)
    c = _lesson_dup_pair(out)
    assert c is not None, out.get("lesson_duplicates")
    assert c["a"]["value"] and c["b"]["value"]          # evidence, not bare scores
    # the unrelated lesson never pairs with the deploy pair
    keys = {k for p in out["lesson_duplicates"] for k in (p["a_key"], p["b_key"])}
    assert "train-qlora-on-the-4090|approach" not in keys
    # REVIEW listing only: nothing was deleted or superseded
    assert len(svc._lessons.current_records()) == 3


def test_dry_run_lists_world_slot_duplicates(svc):
    svc.world_write("MCP spec 2026-07-28", "session identity",
                    "protocol sessions are removed; explicit state handles are required",
                    source_url="https://example.com/a")
    svc.world_write("MCP specification", "session-id status",
                    "protocol sessions removed; explicit state handles required",
                    source_url="https://example.com/b")
    out = svc.deep_dream(apply=False)
    assert len(out["world_duplicates"]) == 1
    c = out["world_duplicates"][0]
    assert {c["a"]["entity"], c["b"]["entity"]} == {"MCP spec 2026-07-28",
                                                    "MCP specification"}
    assert c["a"]["source_url"].startswith("https://example.com")


def test_lesson_duplicate_dismissal_persists(svc):
    _stage_lesson_dups(svc)
    assert _lesson_dup_pair(svc.deep_dream(apply=False)) is not None
    out = svc.curation_dismiss_duplicate(
        "lesson", "deploy daemon to homelab host", "approach",
        "deploy the daemon to the host", "pitfall")
    assert out["dismissed"] is True
    assert _lesson_dup_pair(svc.deep_dream(apply=False)) is None


def test_curation_dismiss_rejects_unknown_store_and_self_pair(svc):
    bad = svc.curation_dismiss_duplicate("cortex", "a", "x", "b", "y")
    assert bad["dismissed"] is False and bad["reason"] == "bad_store"
    same = svc.curation_dismiss_duplicate("lesson", "a", "x", "a", "x")
    assert same["dismissed"] is False and same["reason"] == "bad_pair"


def test_curation_duplicates_standing_listing(svc):
    """The Console review drawer's standing listing: the same lesson/world
    pairs the deep dream reports, without the graph-wide dream pass, and
    reflecting dismissals immediately."""
    _stage_lesson_dups(svc)
    out = svc.curation_duplicates()
    c = _lesson_dup_pair(out)
    assert c is not None, out.get("lesson_duplicates")
    assert {"a_key", "b_key", "a", "b", "similarity"} <= set(c)
    # exact label contract the Console renders per lesson side
    assert set(c["a"]) == {"entity", "attribute", "value",
                           "polarity", "outcome", "about"}
    assert out["world_duplicates"] == []
    svc.curation_dismiss_duplicate(
        "lesson", "deploy daemon to homelab host", "approach",
        "deploy the daemon to the host", "pitfall")
    assert _lesson_dup_pair(svc.curation_duplicates()) is None


def test_curation_duplicates_world_side_carries_source_url(svc):
    svc.world_write("MCP spec 2026-07-28", "session identity",
                    "protocol sessions are removed; explicit state handles are required",
                    source_url="https://example.com/a")
    svc.world_write("MCP specification", "session-id status",
                    "protocol sessions removed; explicit state handles required",
                    source_url="https://example.com/b")
    out = svc.curation_duplicates()
    assert len(out["world_duplicates"]) == 1
    # exact label contract the Console renders per world side
    assert set(out["world_duplicates"][0]["a"]) == {"entity", "attribute",
                                                    "value", "source_url"}


def test_apply_lists_store_duplicates_but_never_deletes(svc):
    _stage_lesson_dups(svc)
    svc.world_write("MCP spec 2026-07-28", "session identity",
                    "protocol sessions are removed; explicit state handles are required",
                    source_url="https://example.com/a")
    svc.world_write("MCP specification", "session-id status",
                    "protocol sessions removed; explicit state handles required",
                    source_url="https://example.com/b")
    out = svc.deep_dream(apply=True)
    assert out["applied"] is True
    assert _lesson_dup_pair(out) is not None
    assert len(out["world_duplicates"]) == 1
    # do-not-auto-delete guard: every record is still current after apply
    assert len(svc._lessons.current_records()) == 3
    assert len(svc._world.current_records()) == 2


def test_slot_key_folds_literal_pipes():
    # _norm_key does NOT strip "|" (its separator class is whitespace ._-/),
    # so the "|" slot-key joiner would be ambiguous: ("a|b","c") and
    # ("a","b|c") would join identically. _slot_key folds literal pipes in
    # the components, keeping the encoding injective for both the listing
    # and the dismissal side.
    from pseudolife_memory.service import _slot_key
    assert _slot_key("a-b", "c") == "a-b|c"
    assert _slot_key("a|b", "c") == "a-b|c"          # folded, not ambiguous
    assert _slot_key("a|b", "c") != _slot_key("a", "b|c")
