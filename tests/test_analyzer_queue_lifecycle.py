"""Analyzer duplicate proposals close their source pair and file regularly.

PG-backed because proposal status, dismissed-pair tombstones, and the regular
scan cursor are durable storage contracts.
"""
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


def _analyzer_link(svc, *, status: str = "pending") -> tuple[int, int, int]:
    st = svc._storage
    src = st.ensure_entity("console-file", display="Console File.py")
    dst = st.ensure_entity("console", display="Console")
    pid = st.insert_proposal(
        src, "implements", dst, 0.7, 0.75,
        "analyzer file/concept pair", "analyzer", time.time())
    assert pid is not None
    if status != "pending":
        # Bypass the public setter to model rows terminal before the lifecycle
        # fix existed.
        with st._txn():
            st.conn.execute(
                "UPDATE edge_proposals SET status=%s, decided_by='human', "
                "decided_at=%s WHERE id=%s", (status, time.time(), pid))
    return pid, src, dst


@pytest.mark.parametrize("decision", ["accept", "reject", "retype"])
def test_terminal_analyzer_link_closes_stored_canonical_pair(svc, decision):
    pid, _src, _dst = _analyzer_link(svc)
    if decision == "accept":
        out = svc.graph_accept_proposal(pid, decided_by="human")
        assert out["accepted"] and out["status"] == "accepted"
    elif decision == "retype":
        out = svc.graph_accept_proposal(
            pid, decided_by="human", relation="uses")
        assert out["accepted"] and out["status"] == "retyped"
    else:
        assert svc.graph_reject_proposal(pid, decided_by="human")["rejected"]

    assert ("console", "console-file") in svc._storage.dismissed_pairs()


def test_analyzer_status_and_pair_close_roll_back_together(svc):
    pid, _src, _dst = _analyzer_link(svc)
    st = svc._storage
    real = st._conn

    class _FailDismiss:
        @property
        def closed(self):
            return real.closed

        @property
        def broken(self):
            return real.broken

        def transaction(self):
            return real.transaction()

        def execute(self, sql, params=None):
            if "INSERT INTO dismissed_pairs" in sql:
                raise RuntimeError("injected dismissal failure")
            return real.execute(sql, params)

    st._conn = _FailDismiss()
    try:
        with pytest.raises(RuntimeError, match="injected dismissal failure"):
            svc.graph_reject_proposal(pid, decided_by="human")
    finally:
        st._conn = real
    assert st.get_proposal(pid)["status"] == "pending"
    assert ("console", "console-file") not in st.dismissed_pairs()


def test_reconcile_legacy_terminal_analyzer_rows_once(svc):
    pid, _src, _dst = _analyzer_link(svc, status="rejected")
    st = svc._storage

    first = svc.reconcile_analyzer_proposals(limit=10)
    assert first == {"considered": 1, "closed": 1, "remaining": 0}
    assert ("console", "console-file") in st.dismissed_pairs()
    assert st.get_proposal(pid)["status"] == "rejected"

    # Removing the pair is an explicit later reversal. The durable high-water
    # mark keeps restart/next-scan reconciliation from resurrecting it.
    with st._txn():
        st.conn.execute(
            "DELETE FROM dismissed_pairs WHERE a_norm=%s AND b_norm=%s",
            ("console", "console-file"))
    assert svc.reconcile_analyzer_proposals(limit=10) == {
        "considered": 0, "closed": 0, "remaining": 0}
    assert ("console", "console-file") not in st.dismissed_pairs()


def _seed_analyzer_pair(svc, prefix="Cortex Console"):
    st = svc._storage
    names = (f"{prefix} web frontend", f"{prefix} frontend")
    ids = [st.ensure_entity(n.lower().replace(" ", "-"), display=n)
           for n in names]
    # Give both nodes structural evidence without using the public relate path,
    # whose write-time duplicate detector would pre-file the merge.
    a = st.ensure_entity("aardvark-dependency", display="aardvark dependency")
    b = st.ensure_entity("zeppelin-runtime", display="zeppelin runtime")
    st.upsert_edge(ids[0], "uses", a, confidence=0.8, origin="agent")
    st.upsert_edge(ids[1], "uses", b, confidence=0.8, origin="agent")
    return names


def test_regular_analyzer_tick_is_bounded_and_independent_of_deep_apply(svc):
    names = _seed_analyzer_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.analyzer_file_duplicates = True
    cfg.judge_batch = 1
    svc.deep_dream = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("regular filing must not invoke deep apply"))

    out = svc.analyzer_duplicate_tick()
    assert out["anchors"] == 1 and out["filed"] <= 1
    rows = svc._storage.pending_entity_proposals()
    assert any(p["kind"] == "merge" and {p["entity"], p["into"]} == set(names)
               for p in rows)

    # The cursor advances and the all-status uniqueness guard keeps later
    # scans idempotent.
    assert svc.analyzer_duplicate_tick()["filed"] == 0


def test_regular_analyzer_tick_walks_past_nonmatching_anchors(svc):
    st = svc._storage
    st.ensure_entity("first-unrelated", display="first unrelated")
    names = _seed_analyzer_pair(svc, "Later Console")
    svc.config.memory.deep_dream.judge_batch = 1

    filed = 0
    for _ in range(4):
        filed += svc.analyzer_duplicate_tick()["filed"]
    assert filed == 1
    assert any({p["entity"], p["into"]} == set(names)
               for p in st.pending_entity_proposals())


def test_regular_analyzer_tick_obeys_filing_kill_switch(svc):
    _seed_analyzer_pair(svc)
    svc.config.memory.deep_dream.analyzer_file_duplicates = False
    out = svc.analyzer_duplicate_tick()
    assert out["fired"] is False and out["reason"] == "disabled"
    assert out["reconciled"]["closed"] == 0
    assert svc._storage.pending_entity_proposals() == []


def test_graph_review_accounts_for_automation_state(svc):
    names = _seed_analyzer_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.analyzer_file_duplicates = False

    out = svc.graph_review()
    duplicate = next(f for f in out["findings"]
                     if f["type"] == "duplicate"
                     and set(f["entities"]) == set(names))
    assert duplicate["automation"] == {
        "state": "gated", "reason": "analyzer filing disabled"}
    assert any(f["automation"]["state"] == "manual"
               for f in out["findings"] if f["type"] != "duplicate")
    assert sum(out["automation"].values()) == len(out["findings"])

    cfg.analyzer_file_duplicates = True
    duplicate = next(f for f in svc.graph_review()["findings"]
                     if f["type"] == "duplicate"
                     and set(f["entities"]) == set(names))
    assert duplicate["automation"]["state"] == "unfiled"

    cfg.judges_enabled = False
    paused = next(f for f in svc.graph_review()["findings"]
                  if f["type"] == "duplicate" and set(f["entities"]) == set(names))
    assert paused["automation"] == {
        "state": "gated", "reason": "review-queue judges disabled"}
    cfg.judges_enabled = True

    st = svc._storage
    by_display = {e["display"]: e["id"] for e in st.load_graph()["entities"]}
    pid = st.insert_entity_proposal(
        "merge", by_display[names[0]], by_display[names[1]], 0.75,
        "analyzer-duplicate: jaccard 0.75", time.time())
    assert pid is not None
    duplicate = next(f for f in svc.graph_review()["findings"]
                     if f["type"] == "duplicate"
                     and set(f["entities"]) == set(names))
    assert duplicate["automation"]["state"] == "pending"

    # A pre-fix terminal row blocks the all-status unique index until the
    # reconciler closes its canonical pair; account for that gate explicitly.
    with st._txn():
        st.conn.execute(
            "UPDATE entity_proposals SET status='rejected', decided_at=%s "
            "WHERE id=%s", (time.time(), pid))
    duplicate = next(f for f in svc.graph_review()["findings"]
                     if f["type"] == "duplicate"
                     and set(f["entities"]) == set(names))
    assert duplicate["automation"] == {
        "state": "terminal", "reason": "already decided; proposal prevents refiling"}
    out = svc.graph_review()
    assert out["automation"]["terminal"] >= 1
    assert sum(out["automation"].values()) == len(out["findings"])


def test_settled_analyzer_pair_does_not_recur_after_service_restart(
        svc, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.graph import norm_name

    names = ("band.py", "band")
    st = svc._storage
    for name in names:
        st.ensure_entity(norm_name(name), display=name)
    svc.config.memory.deep_dream.judge_batch = 1
    assert svc.analyzer_duplicate_tick()["filed"] == 1
    row = next(p for p in st.pending_proposals()
               if p["source"] == "analyzer"
               and {p["src"], p["dst"]} == set(names))
    assert svc.graph_reject_proposal(row["id"])["rejected"]

    st.close()  # the first daemon exits, releasing the bank
    restarted = MemoryService(data_dir=tmp_path / "restart", database_url=pg_url)
    try:
        with restarted._lock:
            restarted._ensure_init()
        assert not any(f["type"] == "duplicate" and set(f["entities"]) == set(names)
                       for f in restarted.graph_review()["findings"])
        assert restarted.analyzer_duplicate_tick()["filed"] == 0
    finally:
        restarted.flush()
        if restarted._storage is not None:
            restarted._storage.close()  # hand the bank back for teardown
