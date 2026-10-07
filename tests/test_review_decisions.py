"""Automatic nondestructive graph decisions remain reviewable over time."""
from __future__ import annotations

import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    service = MemoryService(data_dir=tmp_path, database_url=pg_url)
    with service._lock:
        service._ensure_init()
    yield service
    service.flush()


class _Review:
    def __init__(self, service, kind, proposal, fingerprint="same", model="model-a"):
        self.service = service
        self.storage = service._storage
        self.kind = kind
        self.current_model = model
        self.extractor = type("Extractor", (), {"served_model": model})()
        self.expected = {str(proposal["id"]): fingerprint}
        self.current_fingerprints = {str(proposal["id"]): fingerprint}
        self.signature_calls = []

    def signatures(self, proposals):
        self.signature_calls.append([p["id"] for p in proposals])
        return {str(p["id"]): self.current_fingerprints[str(p["id"])]
                for p in proposals}


def _merge(svc, left="alpha service", right="alpha svc"):
    st = svc._storage
    a = st.ensure_entity(left.replace(" ", "-"), display=left)
    b = st.ensure_entity(right.replace(" ", "-"), display=right)
    pid = st.insert_entity_proposal("merge", a, b, 0.8, "write-dedup", time.time())
    return next(p for p in st.pending_entity_proposals() if p["id"] == pid)


def _junk(svc, name="ok"):
    st = svc._storage
    entity = st.ensure_entity(name, display=name)
    pid = st.insert_entity_proposal("junk", entity, None, None, "short", time.time())
    return next(p for p in st.pending_entity_proposals() if p["id"] == pid)


def _link(svc, src="alpha-tool", dst="beta-lib"):
    st = svc._storage
    a = st.ensure_entity(src, display=src)
    b = st.ensure_entity(dst, display=dst)
    pid = st.insert_proposal(
        a, "uses", b, 0.7, 0.8, "candidate", "deep-dream", time.time())
    return next(p for p in st.pending_proposals() if p["id"] == pid)


def _auto_reject(svc, review, proposal, action="reject"):
    from pseudolife_memory.memory.review_decisions import (
        capture_proposal_pair, record_proposal_terminal)

    with svc._lock, svc._storage.transaction():
        prior_pair = capture_proposal_pair(review, proposal)
        if review.kind == "link":
            result = svc._graph_reject_proposal_locked(
                proposal["id"], decided_by="dream-judge")
        else:
            result = svc._graph_reject_entity_proposal_locked(
                proposal["id"], decided_by="dream-judge")
        assert result["rejected"]
        recorded = record_proposal_terminal(
            review, proposal, action=action, prior_pair=prior_pair)
    assert recorded["recorded"]
    return recorded["decision_id"]


def test_changed_merge_reject_reopens_without_erasing_audit(svc):
    from pseudolife_memory.memory.review_decisions import (
        decision_record, refresh_proposal_terminals)

    proposal = _merge(svc)
    review = _Review(svc, "merge", proposal)
    decision_id = _auto_reject(svc, review, proposal)
    pair = ("alpha-service", "alpha-svc")
    assert pair in svc._storage.dismissed_pairs()
    assert refresh_proposal_terminals(review, limit=1)["reopened"] == 0
    review.current_fingerprints[str(proposal["id"])] = "changed-evidence"
    out = refresh_proposal_terminals(review, limit=1)
    assert out["considered"] == 1 and out["reopened"] == 1
    row = svc._storage.get_entity_proposal(proposal["id"])
    assert row["status"] == "pending" and row["decided_by"] is None
    assert pair not in svc._storage.dismissed_pairs()
    assert svc._storage.recent_entity_decisions(5)
    assert decision_record(svc._storage, decision_id)["state"] == "reopened"


def test_real_review_projection_does_not_immediately_reopen(svc):
    from pseudolife_memory.memory.review_decisions import (
        record_proposal_terminal, refresh_proposal_terminals)
    from pseudolife_memory.memory.review_judgments import ReviewJudgments

    proposal = _merge(svc)
    extractor = type("Extractor", (), {
        "model": "model-a", "served_model": "model-a", "base_url": None})()
    with svc._lock, svc._storage.transaction():
        review = ReviewJudgments(
            svc, "merge", extractor, [proposal], current_model="model-a")
        review.expected = review.signatures([proposal])
        from pseudolife_memory.memory.review_decisions import capture_proposal_pair
        prior_pair = capture_proposal_pair(review, proposal)
        assert svc._graph_reject_entity_proposal_locked(
            proposal["id"], decided_by="dream-judge")["rejected"]
        assert record_proposal_terminal(
            review, proposal, action="reject",
            prior_pair=prior_pair)["recorded"]
        assert refresh_proposal_terminals(review, limit=1)["reopened"] == 0
    assert svc._storage.get_entity_proposal(proposal["id"])["status"] == "rejected"


def test_link_reject_reopens_on_observed_model_change(svc):
    from pseudolife_memory.memory.review_decisions import refresh_proposal_terminals

    proposal = _link(svc)
    review = _Review(svc, "link", proposal, model="served-a")
    _auto_reject(svc, review, proposal)
    review.current_model = "served-b"
    assert refresh_proposal_terminals(review, limit=1)["reopened"] == 1
    row = svc._storage.get_proposal(proposal["id"])
    assert row["status"] == "pending" and row["judge_verdict"] is None


def test_junk_keep_reopens_but_junk_delete_never_does(svc):
    from pseudolife_memory.memory.review_decisions import (
        record_proposal_terminal, refresh_proposal_terminals)

    keep = _junk(svc)
    review = _Review(svc, "junk", keep)
    _auto_reject(svc, review, keep, action="keep")
    review.current_fingerprints[str(keep["id"])] = "policy-changed"
    assert refresh_proposal_terminals(review, limit=1)["reopened"] == 1
    assert svc._storage.get_entity_proposal(keep["id"])["status"] == "pending"
    assert ("junk:ok", "junk:ok") not in svc._storage.dismissed_pairs()

    destructive = _junk(svc, "no")
    destructive_review = _Review(svc, "junk", destructive)
    with svc._lock:
        assert svc._graph_accept_entity_junk_locked(
            destructive["id"], decided_by="dream-judge")["accepted"]
        refused = record_proposal_terminal(
            destructive_review, destructive, action="delete")
    assert refused == {"recorded": False, "reason": "destructive_action"}


def test_legacy_and_human_terminals_are_never_reopened(svc):
    from pseudolife_memory.memory.review_decisions import refresh_proposal_terminals

    legacy = _merge(svc, "legacy service", "legacy svc")
    human = _merge(svc, "human service", "human svc")
    with svc._lock:
        svc._graph_reject_entity_proposal_locked(
            legacy["id"], decided_by="dream-judge")
        svc._graph_reject_entity_proposal_locked(human["id"], decided_by="human")
    review = _Review(svc, "merge", legacy)
    review.current_fingerprints[str(legacy["id"])] = "changed"
    assert refresh_proposal_terminals(review, limit=10)["considered"] == 0
    assert svc._storage.get_entity_proposal(legacy["id"])["status"] == "rejected"
    assert svc._storage.get_entity_proposal(human["id"])["status"] == "rejected"


def test_automatic_reject_never_adopts_preexisting_human_pair(svc):
    from pseudolife_memory.memory.review_decisions import (
        capture_proposal_pair, record_proposal_terminal,
        refresh_proposal_terminals)

    proposal = _merge(svc)
    review = _Review(svc, "merge", proposal)
    with svc._lock, svc._storage.transaction():
        assert svc._graph_dismiss_duplicate_locked(
            "alpha service", "alpha svc")["dismissed"]
        prior_pair = capture_proposal_pair(review, proposal)
        assert prior_pair["dismissed_at"] is not None
        assert svc._graph_reject_entity_proposal_locked(
            proposal["id"], decided_by="dream-judge")["rejected"]
        refused = record_proposal_terminal(
            review, proposal, action="reject", prior_pair=prior_pair)
    assert refused == {"recorded": False, "reason": "preexisting_pair"}
    review.current_fingerprints[str(proposal["id"])] = "changed"
    assert refresh_proposal_terminals(review, limit=1)["considered"] == 0
    assert svc._storage.get_entity_proposal(proposal["id"])["status"] == "rejected"
    assert ("alpha-service", "alpha-svc") in svc._storage.dismissed_pairs()


def test_record_uses_model_observed_from_completed_judgment(svc):
    from pseudolife_memory.memory.review_decisions import (
        capture_proposal_pair, decision_record, record_proposal_terminal)

    proposal = _link(svc)
    review = _Review(svc, "link", proposal, model="probe-model")
    review.observed = lambda _proposal: "response-model"
    with svc._lock, svc._storage.transaction():
        prior_pair = capture_proposal_pair(review, proposal)
        assert svc._graph_reject_proposal_locked(
            proposal["id"], decided_by="dream-judge")["rejected"]
        recorded = record_proposal_terminal(
            review, proposal, action="reject", prior_pair=prior_pair)
    assert decision_record(
        svc._storage, recorded["decision_id"])["served_model"] == "response-model"


def test_human_pair_confirmation_retires_automatic_marker(svc):
    from pseudolife_memory.memory.review_decisions import (
        confirm_human_pair, decision_record, refresh_proposal_terminals)

    proposal = _merge(svc)
    review = _Review(svc, "merge", proposal)
    decision_id = _auto_reject(svc, review, proposal)
    with svc._lock:
        out = confirm_human_pair(svc._storage, "alpha-service", "alpha-svc")
    assert out["superseded"] == 1
    review.current_fingerprints[str(proposal["id"])] = "changed"
    assert refresh_proposal_terminals(review, limit=10)["considered"] == 0
    assert svc._storage.get_entity_proposal(proposal["id"])["status"] == "rejected"
    assert ("alpha-service", "alpha-svc") in svc._storage.dismissed_pairs()
    assert decision_record(svc._storage, decision_id)["state"] == "human_confirmed"


def test_human_terminal_confirmation_retires_marker_when_already_rejected(svc):
    from pseudolife_memory.memory.review_decisions import (
        confirm_human_proposal, refresh_proposal_terminals)

    proposal = _link(svc)
    review = _Review(svc, "link", proposal)
    _auto_reject(svc, review, proposal)
    with svc._lock:
        assert confirm_human_proposal(
            svc._storage, "link", proposal["id"])["superseded"] == 1
    review.current_model = "new-model"
    assert refresh_proposal_terminals(review, limit=1)["considered"] == 0
    assert svc._storage.get_proposal(proposal["id"])["status"] == "rejected"


def test_recent_decisions_is_bounded_and_exposes_only_compact_audit(svc):
    from pseudolife_memory.memory.review_decisions import (
        confirm_human_proposal, recent_decisions)

    first = _link(svc, "alpha-one", "beta-one")
    second = _link(svc, "alpha-two", "beta-two")
    _auto_reject(svc, _Review(svc, "link", first), first)
    _auto_reject(svc, _Review(svc, "link", second), second)
    with svc._lock:
        confirm_human_proposal(svc._storage, "link", first["id"])
    rows = recent_decisions(svc._storage, limit=1)
    assert len(rows) == 1
    assert set(rows[0]) == {
        "queue", "action", "state", "proposal_id", "pair",
        "recorded_at", "ended_at", "end_reason"}
    assert rows[0]["proposal_id"] == first["id"]
    assert rows[0]["state"] == "human_confirmed"


def test_removed_pair_supersedes_marker_without_recreating_it(svc):
    from pseudolife_memory.memory.review_decisions import (
        decision_record, refresh_proposal_terminals)

    proposal = _merge(svc)
    review = _Review(svc, "merge", proposal)
    decision_id = _auto_reject(svc, review, proposal)
    st = svc._storage
    with st.transaction():
        st.conn.execute(
            "DELETE FROM dismissed_pairs WHERE a_norm=%s AND b_norm=%s",
            ("alpha-service", "alpha-svc"))
    review.current_fingerprints[str(proposal["id"])] = "changed"
    out = refresh_proposal_terminals(review, limit=1)
    assert out["superseded"] == 1 and out["reopened"] == 0
    assert st.get_entity_proposal(proposal["id"])["status"] == "rejected"
    assert decision_record(st, decision_id)["state"] == "externally_superseded"
    assert ("alpha-service", "alpha-svc") not in st.dismissed_pairs()


def test_reopen_status_and_owned_pair_roll_back_together(svc):
    from pseudolife_memory.memory.review_decisions import (
        decision_record, refresh_proposal_terminals)

    proposal = _merge(svc)
    review = _Review(svc, "merge", proposal)
    decision_id = _auto_reject(svc, review, proposal)
    review.current_fingerprints[str(proposal["id"])] = "changed"
    st = svc._storage
    real = st._conn

    class _FailPairDelete:
        closed = property(lambda self: real.closed)
        broken = property(lambda self: real.broken)

        def transaction(self):
            return real.transaction()

        def execute(self, sql, params=None):
            if "DELETE FROM dismissed_pairs" in sql:
                raise RuntimeError("injected pair delete failure")
            return real.execute(sql, params)

    st._conn = _FailPairDelete()
    try:
        with pytest.raises(RuntimeError, match="injected pair delete"):
            refresh_proposal_terminals(review, limit=1)
    finally:
        st._conn = real
    assert st.get_entity_proposal(proposal["id"])["status"] == "rejected"
    assert ("alpha-service", "alpha-svc") in st.dismissed_pairs()
    assert decision_record(st, decision_id)["state"] == "active"


def test_reconsideration_is_bounded_fair_and_enriches_once_per_batch(svc):
    from pseudolife_memory.memory.review_decisions import (
        has_active_decisions, refresh_proposal_terminals)

    proposals = []
    for i in range(3):
        proposal = _link(svc, f"tool-{i}", f"lib-{i}")
        _auto_reject(svc, _Review(svc, "link", proposal), proposal)
        proposals.append(proposal)
    assert has_active_decisions(svc._storage, "link")
    assert not has_active_decisions(svc._storage, "candidate")
    combined = _Review(svc, "link", proposals[0])
    combined.expected = {}
    combined.current_fingerprints = {
        str(proposal["id"]): "changed" for proposal in proposals}

    reopened = []
    for _ in range(3):
        before = {p["id"] for p in svc._storage.pending_proposals()}
        out = refresh_proposal_terminals(combined, limit=1)
        after = {p["id"] for p in svc._storage.pending_proposals()}
        assert out["considered"] == 1 and out["reopened"] == 1
        reopened.extend(after - before)
    assert set(reopened) == {p["id"] for p in proposals}
    assert all(len(call) == 1 for call in combined.signature_calls)


def test_candidate_dismissal_ignores_own_pair_and_reopens_on_drift(svc):
    from pseudolife_memory.memory.review_decisions import (
        decision_record, record_candidate_dismissal,
        refresh_candidate_dismissals)
    from pseudolife_memory.memory.review_judgments import candidate_generation

    st = svc._storage
    src_id = st.ensure_entity("stored-alpha", display="Alpha Display")
    dst_id = st.ensure_entity("stored-beta", display="Beta Display")
    candidate = {"src": "Alpha Display", "dst": "Beta Display",
                 "src_id": src_id, "dst_id": dst_id, "similarity": 0.8,
                 "src_snippets": ["alpha evidence"],
                 "dst_snippets": ["beta evidence"]}
    with svc._lock:
        generation = candidate_generation(svc, decision_inputs=False)
        assert svc._graph_dismiss_duplicate_locked(
            "Alpha Display", "Beta Display")["dismissed"]
        assert candidate_generation(svc, decision_inputs=False) == generation
        recorded = record_candidate_dismissal(
            svc, candidate, decision_fingerprint="candidate-fp",
            policy_fingerprint="policy-a", generation_fingerprint=generation,
            served_model="served-a")
    assert recorded["recorded"]
    same = refresh_candidate_dismissals(
        svc, policy_fingerprint="policy-a",
        generation_fingerprint=generation, served_model="served-a", limit=1)
    assert same["reopened"] == 0

    svc.store("gamma has unrelated new context", source="test")
    with svc._lock:
        unrelated_generation = candidate_generation(svc, decision_inputs=False)
    assert unrelated_generation != generation
    unrelated = refresh_candidate_dismissals(
        svc, policy_fingerprint="policy-a",
        generation_fingerprint=unrelated_generation,
        served_model="served-a", limit=1)
    assert unrelated["reopened"] == 0

    svc.store("Alpha Display gained relevant endpoint evidence", source="test")
    changed = refresh_candidate_dismissals(
        svc, policy_fingerprint="policy-a",
        generation_fingerprint=unrelated_generation,
        served_model="served-a", limit=1)
    assert changed["reopened"] == 1
    assert ("stored-alpha", "stored-beta") not in st.dismissed_pairs()
    assert decision_record(st, recorded["decision_id"])["state"] == "reopened"


def test_candidate_evidence_snapshots_each_source_once_per_batch(svc, monkeypatch):
    from pseudolife_memory.memory.review_decisions import (
        candidate_evidence_fingerprints)

    st = svc._storage
    for name in ("alpha", "beta", "gamma", "delta"):
        st.ensure_entity(name, display=name)
    candidates = [{"src": "alpha", "dst": "beta"},
                  {"src": "gamma", "dst": "delta"}]
    calls = {}
    for name in ("load_graph", "load_entries", "entity_sources_map",
                 "traces_by_entity_norm", "entity_fact_counts",
                 "pending_proposals", "pending_entity_proposals"):
        original = getattr(st, name)

        def counted(original=original, name=name):
            calls[name] = calls.get(name, 0) + 1
            return original()

        monkeypatch.setattr(st, name, counted)
    with svc._lock:
        fingerprints = candidate_evidence_fingerprints(svc, candidates)
    assert len(fingerprints) == 2 and fingerprints[0] != fingerprints[1]
    assert calls == {name: 1 for name in (
        "load_graph", "load_entries", "entity_sources_map",
        "traces_by_entity_norm", "entity_fact_counts",
        "pending_proposals", "pending_entity_proposals")}


def test_new_pending_relationship_reopens_candidate_dismissal(svc):
    from pseudolife_memory.memory.review_decisions import (
        record_candidate_dismissal, refresh_candidate_dismissals)

    st = svc._storage
    src_id = st.ensure_entity("alpha", display="Alpha")
    dst_id = st.ensure_entity("beta", display="Beta")
    candidate = {"src": "Alpha", "dst": "Beta",
                 "src_id": src_id, "dst_id": dst_id,
                 "similarity": 0.8}
    with svc._lock:
        assert svc._graph_dismiss_duplicate_locked(
            "Alpha", "Beta", _review_guard=lambda: True)["dismissed"]
        recorded = record_candidate_dismissal(
            svc, candidate, decision_fingerprint="candidate-fp",
            policy_fingerprint="policy-a", served_model="served-a")
    assert recorded["recorded"]
    assert refresh_candidate_dismissals(
        svc, policy_fingerprint="policy-a",
        served_model="served-a", limit=1)["reopened"] == 0

    st.insert_proposal(
        src_id, "uses", dst_id, 0.9, 0.9,
        "new relationship evidence", "memory-dream", time.time())
    assert refresh_candidate_dismissals(
        svc, policy_fingerprint="policy-a",
        served_model="served-a", limit=1)["reopened"] == 1
