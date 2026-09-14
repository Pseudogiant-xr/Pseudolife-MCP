"""Atomic safety contracts for automatic lesson/world duplicate curation.

These tests use the real PostgreSQL backend.  The model is a deterministic
stand-in because curation safety depends on durable ordering, not embedding
quality.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

from pseudolife_memory.curation_safety import (
    CurationReconciliationError,
    apply_slot_duplicate,
    can_auto_fold,
    capture_pair_evidence,
    curation_observed_model,
    evidence_fingerprint,
    recover_slot_curation,
    snapshot_record,
)


class Vectors:
    def encode_single(self, text):
        vector = torch.zeros(1024)
        vector[0] = float(len(text) or 1)
        return vector


class Bank:
    bands = []

    def save_weights(self, path):
        pass


def make_service(path, database_url):
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
    from pseudolife_memory.memory.lessons import LessonStore
    from pseudolife_memory.memory.world_cortex import WorldCortexStore
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.sync import hydrate_lessons, hydrate_world_cortex

    service = MemoryService(data_dir=path, database_url=database_url)
    service._storage = PostgresStorage(database_url)
    service._graph = PostgresNetworkxGraphStore(service._storage)
    service._cms = Bank()
    service._cortex = CortexStore()
    service._embedder = Vectors()
    service._lessons = LessonStore()
    service._world = WorldCortexStore()
    hydrate_lessons(service._lessons, service._storage)
    hydrate_world_cortex(service._world, service._storage)
    return service


@pytest.fixture()
def svc(tmp_path, pg_conn, pg_url):  # noqa: F811
    service = make_service(tmp_path, pg_url)
    yield service
    service._storage.close()


def lesson_pair(svc, *, first="Back up before deploy.",
                second="Back up before deploy."):
    svc.lesson_write(
        "deploy daemon", "approach", first, about="postgres",
        outcome="correction", polarity="+", confidence=0.8,
        origin="agent", provenance={"episode-a"}, support={"signal-a"})
    svc.lesson_write(
        "deploy service", "pitfall", second, about="postgres",
        outcome="correction", polarity="+", confidence=0.9,
        origin="agent", provenance={"episode-b"}, support={"signal-b"})
    return capture_pair_evidence(
        "lesson",
        svc._lessons.lookup("deploy daemon", "approach"),
        svc._lessons.lookup("deploy service", "pitfall"),
    )


def current_values(svc, table="lessons"):
    return svc._storage.conn.execute(
        f"SELECT entity_norm, attribute_norm, value FROM {table} "
        "WHERE status = 'current' ORDER BY entity_norm, attribute_norm"
    ).fetchall()


class SlotJudge:
    model = "synthetic-curation-judge"
    served_model = "synthetic-curation-judge"

    def __init__(self, verdict="distinct", keep=None, fold=None, mutate=None):
        self.verdict = verdict
        self.keep = keep
        self.fold = fold
        self.mutate = mutate
        self.seen = []

    def judge_slot_pairs(self, rows):
        self.seen.extend(rows)
        if self.mutate is not None:
            self.mutate(rows)
        return [{"n": row["n"], "verdict": self.verdict,
                 "keep": self.keep, "fold": self.fold,
                 "confidence": 0.99, "note": "synthetic verdict"}
                for row in rows]


def test_snapshot_fingerprint_covers_full_record_metadata(svc):
    evidence = lesson_pair(svc)
    original = evidence.a
    assert original["version"] == 1
    assert original["provenance"] == ["episode-a"]
    assert original["support"] == ["signal-a"]
    assert original["embedding"]["sha256"]

    changed = dict(original)
    changed["last_confirmed"] = float(changed["last_confirmed"]) + 1.0
    assert evidence_fingerprint(changed) != evidence_fingerprint(original)

    changed = dict(original)
    changed["provenance"] = ["episode-a", "episode-new"]
    assert evidence_fingerprint(changed) != evidence_fingerprint(original)


@pytest.mark.parametrize("change", ["missing", "changed"])
def test_prelock_validation_refuses_missing_or_changed_evidence(svc, change):
    evidence = lesson_pair(svc)
    if change == "missing":
        svc.lesson_forget("deploy service", "pitfall")
    else:
        svc.lesson_write(
            "deploy service", "pitfall", "Changed after the judge ran.",
            about="postgres", outcome="correction", polarity="+",
            provenance={"episode-new"})

    before = current_values(svc)
    out = apply_slot_duplicate(svc, evidence, keep="a")
    assert out == {"applied": False, "reason": f"evidence_{change}"}
    assert current_values(svc) == before


def test_locked_validation_refuses_durable_row_changed_behind_ram(svc):
    evidence = lesson_pair(svc)
    svc._storage.conn.execute(
        "UPDATE lessons SET provenance = %s::jsonb "
        "WHERE entity_norm = 'deploy-service' AND attribute_norm = 'pitfall' "
        "AND status = 'current'",
        ('["external-change"]',),
    )

    out = apply_slot_duplicate(svc, evidence, keep="a")
    assert out == {"applied": False, "reason": "evidence_changed"}
    assert len(svc._lessons.current_records()) == 2
    assert len(current_values(svc)) == 2
    assert svc._storage.store_decisions("lesson") == []


def test_apply_lock_check_reads_only_the_two_target_rows(svc, monkeypatch):
    evidence = lesson_pair(svc)
    monkeypatch.setattr(
        svc._storage, "load_lessons",
        lambda: (_ for _ in ()).throw(AssertionError("full-store read")))

    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"] is True
    assert len(current_values(svc)) == 1


@pytest.mark.parametrize("field,value", [
    ("polarity", "-"),
    ("outcome", "failure"),
    ("about", "sqlite"),
    ("origin", "user"),
])
def test_semantic_fold_refuses_conflicting_lesson_category(svc, field, value):
    evidence = lesson_pair(
        svc, first="Take a backup before deployment and verify the rollback snapshot.",
        second="Take a backup before deployment")
    changed = dict(evidence.b)
    changed[field] = value
    evidence = evidence.with_side("b", changed)

    before = current_values(svc)
    out = apply_slot_duplicate(svc, evidence, keep="a")
    assert out == {"applied": False, "reason": f"conflicting_{field}"}
    assert current_values(svc) == before


def test_exact_fold_keeps_survivor_unchanged_and_audits_loser_metadata(svc):
    first = "Take a backup before deployment."
    second = first
    evidence = lesson_pair(svc, first=first, second=second)

    out = apply_slot_duplicate(
        svc, evidence, keep="a",
        reason="ratified semantic duplicate", now=12345.0)

    assert out["applied"] is True
    survivor = svc._lessons.lookup("deploy daemon", "approach")
    assert survivor.value == first
    assert survivor.provenance == {"episode-a"}
    assert survivor.support == {"signal-a"}
    assert survivor.outcome == "correction"
    assert survivor.polarity == "+"
    assert survivor.about == "postgres"
    assert survivor.confidence == pytest.approx(0.8)
    assert svc._lessons.lookup("deploy service", "pitfall") is None

    rows = svc._storage.conn.execute(
        "SELECT status, value FROM lessons ORDER BY id").fetchall()
    assert ("current", first) in rows
    assert ("retired", second) in rows
    decision = svc._storage.store_decisions("lesson")[0]
    assert decision["action"] == "retire"
    assert decision["record"]["lesson"] == second
    assert decision["record"]["provenance"] == ["episode-b"]
    assert decision["record"]["support"] == ["signal-b"]


def test_freeform_or_non_subsuming_fold_is_never_automatic(svc):
    evidence = lesson_pair(
        svc, first="Take a backup before deployment.",
        second="Create a rollback snapshot before release.")
    invented = can_auto_fold(
        evidence,
        {"verdict": "duplicate", "keep": "a", "fold": "Carry both steps."})
    assert invented.allowed is False
    assert invented.reason == "invented_fold"

    non_subsuming = can_auto_fold(
        evidence, {"verdict": "duplicate", "keep": "a", "fold": None})
    assert non_subsuming.allowed is False
    assert non_subsuming.reason == "no_exact_safe_relation"


def test_case_only_code_or_token_change_is_not_an_exact_safe_fold(svc):
    evidence = lesson_pair(
        svc, first="Invoke WidgetFactory() before deploy.",
        second="Invoke widgetfactory() before deploy.")

    decision = can_auto_fold(
        evidence, {"verdict": "duplicate", "keep": "a", "fold": None})

    assert decision.allowed is False
    assert decision.reason == "no_exact_safe_relation"


@pytest.mark.parametrize("scoped", [
    "Do not follow this obsolete advice: restart daemon",
    "If the cache is corrupt, restart daemon",
    "Restart daemon only after taking a snapshot",
    "A user correction says never restart daemon",
])
def test_text_containment_does_not_prove_a_safe_fold(svc, scoped):
    evidence = lesson_pair(svc)
    left = dict(evidence.a)
    right = dict(evidence.b)
    left["value"] = scoped
    right["value"] = "restart daemon"
    evidence = evidence.with_side("a", left).with_side("b", right)

    decision = can_auto_fold(
        evidence, {"verdict": "duplicate", "keep": "a", "fold": None})
    assert decision.allowed is False
    assert decision.reason == "no_exact_safe_relation"


@pytest.mark.parametrize("fail_call", [1, 2])
def test_survivor_or_retire_write_failure_rolls_back_everything(
        svc, monkeypatch, fail_call):
    evidence = lesson_pair(
        svc, first="Take a backup before deployment.",
        second="Take a backup before deployment.")
    original = svc._storage.replace_slot_lessons
    calls = 0

    def fail_after_sql(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == fail_call:
            raise RuntimeError(f"write {fail_call} failed")
        return result

    monkeypatch.setattr(svc._storage, "replace_slot_lessons", fail_after_sql)
    before = current_values(svc)
    out = apply_slot_duplicate(svc, evidence, keep="a")

    assert out["applied"] is False
    assert out["reason"] == "persistence_error"
    assert current_values(svc) == before
    assert sorted(r.value for r in svc._lessons.current_records()) == sorted(
        ["Take a backup before deployment.",
         "Take a backup before deployment."])
    assert svc._storage.store_decisions("lesson") == []


def test_audit_failure_rolls_back_fold_and_retire(svc, monkeypatch):
    evidence = lesson_pair(
        svc, first="Take a backup before deployment.",
        second="Take a backup before deployment.")
    original = svc._storage.record_store_decision

    def fail_after_audit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("audit response failed")

    monkeypatch.setattr(svc._storage, "record_store_decision", fail_after_audit)
    before = current_values(svc)
    out = apply_slot_duplicate(svc, evidence, keep="a")

    assert out["applied"] is False
    assert out["reason"] == "persistence_error"
    assert current_values(svc) == before
    assert svc._storage.store_decisions("lesson") == []


def test_success_commits_preexisting_unrelated_dirty_lesson_state(svc):
    svc.lesson_write("inspect logs", "approach", "Read the first error")
    evidence = lesson_pair(svc)
    unrelated = svc._lessons.lookup("inspect logs", "approach")
    unrelated.support.add("unsaved-signal")
    svc._lessons.dirty_slots.add(unrelated.key)

    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"] is True

    durable = {
        (r["entity_norm"], r["attribute_norm"]): r
        for r in svc._storage.load_lessons()}
    assert durable[("inspect-logs", "approach")]["support"] == ["unsaved-signal"]
    assert svc._lessons.dirty_slots == set()


def test_rollback_preserves_preexisting_unrelated_dirty_lesson_state(
        svc, monkeypatch):
    svc.lesson_write("inspect logs", "approach", "Read the first error")
    evidence = lesson_pair(svc)
    unrelated = svc._lessons.lookup("inspect logs", "approach")
    unrelated.support.add("unsaved-signal")
    svc._lessons.dirty_slots.add(unrelated.key)
    original = svc._storage.replace_slot_lessons

    def fail_after_sql(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fold persistence failed")

    monkeypatch.setattr(svc._storage, "replace_slot_lessons", fail_after_sql)
    out = apply_slot_duplicate(svc, evidence, keep="a")

    assert out["reason"] == "persistence_error"
    unrelated = svc._lessons.lookup("inspect logs", "approach")
    assert unrelated.support == {"unsaved-signal"}
    assert unrelated.key in svc._lessons.dirty_slots
    durable = {
        (r["entity_norm"], r["attribute_norm"]): r
        for r in svc._storage.load_lessons()}
    assert durable[("inspect-logs", "approach")]["support"] == []


def lose_commit_response(monkeypatch):
    import pseudolife_memory.curation_safety as safety

    original = safety.slot_curation_transaction

    @contextmanager
    def lost(storage):
        with original(storage):
            yield
        storage.close()
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(safety, "slot_curation_transaction", lost)


def test_uncertain_commit_reconciles_before_returning(svc, monkeypatch):
    evidence = lesson_pair(svc)
    lose_commit_response(monkeypatch)

    out = apply_slot_duplicate(svc, evidence, keep="a")

    assert out["applied"] is True
    assert out["reconciled"] == "committed"
    assert len(svc._lessons.current_records()) == 1
    assert len(current_values(svc)) == 1
    assert getattr(svc, "_slot_curation_recovery", None) is None


def test_unavailable_uncertain_commit_recovers_on_next_operation(
        svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    evidence = lesson_pair(svc)
    lose_commit_response(monkeypatch)
    hydrate = safety._hydrate_store
    monkeypatch.setattr(
        safety, "_hydrate_store",
        lambda *args: (_ for _ in ()).throw(RuntimeError("reconciliation offline")))

    out = apply_slot_duplicate(svc, evidence, keep="a")
    assert out["applied"] is False
    assert out["reason"] == "reconciliation_required"
    assert getattr(svc, "_slot_curation_recovery", None) is not None

    monkeypatch.setattr(safety, "_hydrate_store", hydrate)
    # The next ordinary operation enters _ensure_init and resolves the latch.
    assert svc.lessons_dump()["count"] == 1
    assert len(svc._lessons.current_records()) == 1
    assert getattr(svc, "_slot_curation_recovery", None) is None


def test_flush_cannot_overwrite_an_uncertain_committed_lesson(
        svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    evidence = lesson_pair(svc)
    lose_commit_response(monkeypatch)
    hydrate = safety._hydrate_store
    monkeypatch.setattr(
        safety, "_hydrate_store",
        lambda *args: (_ for _ in ()).throw(RuntimeError("reconciliation offline")))
    assert apply_slot_duplicate(svc, evidence, keep="a")["reason"] == \
        "reconciliation_required"

    with pytest.raises(CurationReconciliationError, match="reconciliation required"):
        svc.flush()
    assert len(current_values(svc)) == 1

    monkeypatch.setattr(safety, "_hydrate_store", hydrate)
    assert svc.lessons_dump()["count"] == 1


def test_recovery_refuses_a_third_durable_state(svc):
    evidence = lesson_pair(svc)
    from pseudolife_memory.curation_safety import PendingCurationRecovery

    svc._slot_curation_recovery = PendingCurationRecovery.for_evidence(
        evidence, keep="a", expected_survivor=evidence.a)
    svc._storage.conn.execute(
        "UPDATE lessons SET value = 'third state' "
        "WHERE entity_norm = 'deploy-daemon' AND attribute_norm = 'approach' "
        "AND status = 'current'")

    with pytest.raises(CurationReconciliationError, match="neither before nor after"):
        recover_slot_curation(svc)
    assert svc._slot_curation_recovery is not None


def test_restart_can_undo_and_restores_original_loser_metadata(
        svc, tmp_path, pg_url):
    evidence = lesson_pair(
        svc, first="Take a backup before deployment.",
        second="Take a backup before deployment.")
    assert apply_slot_duplicate(
        svc, evidence, keep="a",
        reason="ratified semantic duplicate")["applied"]
    svc._storage.close()

    restarted = make_service(tmp_path / "restart", pg_url)
    try:
        restored = restarted.lesson_restore("deploy service", "pitfall")
        assert restored["restored"] == 1
        loser = restarted._lessons.lookup("deploy service", "pitfall")
        survivor = restarted._lessons.lookup("deploy daemon", "approach")
        assert loser.value == "Take a backup before deployment."
        assert loser.provenance == {"episode-b"}
        assert loser.support == {"signal-b"}
        assert loser.outcome == "correction"
        assert loser.about == "postgres"
        assert survivor.provenance == {"episode-a"}
        assert survivor.support == {"signal-a"}
        assert survivor.confidence == pytest.approx(0.8)
    finally:
        restarted._storage.close()


def test_atomic_curation_undo_rolls_back_record_and_audit_together(
        svc, monkeypatch):
    evidence = lesson_pair(svc)
    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"]
    original = svc._storage.replace_slot_lessons

    def fail_after_restore_sql(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("restore persistence failed")

    monkeypatch.setattr(
        svc._storage, "replace_slot_lessons", fail_after_restore_sql)
    with pytest.raises(CurationReconciliationError, match="restore rolled back"):
        svc.lesson_restore("deploy service", "pitfall")

    assert svc._lessons.lookup("deploy service", "pitfall") is None
    assert len(current_values(svc)) == 1
    latest = svc._storage.store_decisions("lesson")[0]
    assert latest["action"] == "retire"


def test_whole_entity_restore_dispatches_a_single_curated_slot_atomically(
        svc, monkeypatch):
    evidence = lesson_pair(svc)
    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"]
    original = svc._storage.record_store_decision

    def fail_restore_audit(*args, **kwargs):
        if args[3] == "restore":
            raise RuntimeError("restore audit failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        svc._storage, "record_store_decision", fail_restore_audit)
    with pytest.raises(CurationReconciliationError, match="restore rolled back"):
        svc.lesson_restore("deploy service")

    assert svc._lessons.lookup("deploy service", "pitfall") is None
    assert len(current_values(svc)) == 1
    latest = svc._storage.store_decisions("lesson")[0]
    assert latest["action"] == "retire"


def test_whole_entity_restore_with_mixed_slots_requires_an_exact_attribute(svc):
    evidence = lesson_pair(svc)
    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"]
    svc.lesson_write("deploy service", "other", "Keep another lesson")
    assert svc.lesson_forget("deploy service", "other")["retired"] == 1

    out = svc.lesson_restore("deploy service")

    assert out == {"restored": 0,
                   "reason": "curation_restore_requires_attribute"}
    assert svc._lessons.lookup("deploy service", "pitfall") is None
    assert svc._lessons.lookup("deploy service", "other") is None


def test_uncertain_curation_undo_reconciles_the_committed_restore(
        svc, monkeypatch):
    evidence = lesson_pair(svc)
    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"]
    lose_commit_response(monkeypatch)

    out = svc.lesson_restore("deploy service", "pitfall")

    assert out["restored"] == 1
    assert out["source"] == "reconciled"
    assert len(current_values(svc)) == 2
    assert svc._slot_curation_recovery is None


def test_compacted_curation_undo_reconstructs_the_exact_preimage(svc):
    evidence = lesson_pair(svc)
    assert apply_slot_duplicate(svc, evidence, keep="a")["applied"]
    cfg = svc.config.memory.compaction
    cfg.keep_per_slot = 0
    cfg.min_age_days = 0
    assert svc.compact_superseded()["lessons"] >= 1

    out = svc.lesson_restore("deploy service", "pitfall")

    assert out["restored"] == 1
    restored = svc._lessons.lookup("deploy service", "pitfall")
    assert snapshot_record("lesson", restored) == evidence.b


def test_world_exact_duplicate_requires_equivalent_citation_and_fresh_keep(svc):
    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is the current CEO.",
        freshness_class="slow", content_hash="sha256:abc")
    svc.world_write("Acme", "CEO", retrieved_at=100.0, **common)
    svc.world_write("Acme Corp", "chief executive", retrieved_at=200.0, **common)
    evidence = capture_pair_evidence(
        "world", svc._world.lookup("Acme", "CEO"),
        svc._world.lookup("Acme Corp", "chief executive"))

    assert apply_slot_duplicate(svc, evidence, keep="a") == {
        "applied": False, "reason": "stale_world_survivor"}
    out = apply_slot_duplicate(svc, evidence, keep="b", reason="same cited fact")
    assert out["applied"] is True
    survivor = svc._world.lookup("Acme Corp", "chief executive")
    assert survivor.source_url == common["source_url"]
    assert survivor.source_quote == common["source_quote"]
    assert survivor.retrieved_at == 200.0
    assert svc._world.lookup("Acme", "CEO") is None


def test_world_duplicate_refuses_different_source_evidence(svc):
    svc.world_write(
        "Acme", "CEO", "Jane Doe", confidence=0.9,
        source_url="https://example.test/a", source_quote="Jane is CEO")
    svc.world_write(
        "Acme Corp", "chief executive", "Jane Doe", confidence=0.9,
        source_url="https://example.test/b", source_quote="Jane is CEO")
    evidence = capture_pair_evidence(
        "world", svc._world.lookup("Acme", "CEO"),
        svc._world.lookup("Acme Corp", "chief executive"))

    assert apply_slot_duplicate(svc, evidence, keep="b") == {
        "applied": False, "reason": "conflicting_source_url"}
    assert len(svc._world.current_records()) == 2


def test_snapshot_record_matches_locked_postgres_shape(svc):
    evidence = lesson_pair(svc)
    durable = svc._storage.load_lessons()
    by_key = {(r["entity_norm"], r["attribute_norm"]): r for r in durable}
    assert snapshot_record("lesson", by_key[("deploy-daemon", "approach")]) == evidence.a
    assert snapshot_record("lesson", by_key[("deploy-service", "pitfall")]) == evidence.b


def test_curation_judge_captures_full_evidence_and_rechecks_before_memo(svc):
    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "shadow"

    def mutate(rows):
        assert rows[0]["a"]["provenance"] == ["episode-a"]
        assert rows[0]["a"]["support"] == ["signal-a"]
        assert rows[0]["a"]["embedding"]["sha256"]
        svc.lesson_write(
            "deploy service", "pitfall", "Back up before deploy.",
            about="postgres", outcome="correction", polarity="+",
            confidence=0.9, origin="agent",
            provenance={"episode-b", "changed-after-capture"},
            support={"signal-b"})

    judge = SlotJudge("duplicate", keep="a", mutate=mutate)
    out = svc.deep_dream_judge_curation(judge)
    assert out["judged"] == 0
    assert svc._storage.curation_judgments("lesson") == {}
    assert len(svc._lessons.current_records()) == 2


def test_curation_memo_is_bound_to_policy_and_full_record_content(svc):
    evidence = lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "shadow"
    first = SlotJudge()
    assert svc.deep_dream_judge_curation(first)["judged"] == 1
    assert svc.deep_dream_judge_curation(SlotJudge())["judged"] == 0

    # A policy change invalidates the opinion inside its age horizon.
    cfg.curation_distinct_min_confidence += 0.01
    policy_rejudge = SlotJudge()
    assert svc.deep_dream_judge_curation(policy_rejudge)["judged"] == 1
    assert policy_rejudge.seen

    # Reconfirming one record changes timestamps/provenance and invalidates it
    # again even though the value and candidate similarity are unchanged.
    side = evidence.b
    svc.lesson_write(
        side["entity"], side["attribute"], side["value"],
        about=side["about"], outcome=side["outcome"],
        polarity=side["polarity"], confidence=side["confidence"],
        origin=side["origin"], provenance={"new-provenance"})
    content_rejudge = SlotJudge()
    assert svc.deep_dream_judge_curation(content_rejudge)["judged"] == 1
    assert content_rejudge.seen


def test_explicit_curation_rejudge_ignores_an_old_binding(svc):
    lesson_pair(svc)
    svc.config.memory.deep_dream.curation_judge_mode = "shadow"
    assert svc.deep_dream_judge_curation(SlotJudge())["judged"] == 1
    assert svc.deep_dream_judge_curation(SlotJudge())["judged"] == 0

    assert svc.review_rejudge("curation", limit=1)["requeued"] == 1
    judge = SlotJudge()
    assert svc.deep_dream_judge_curation(judge)["judged"] == 1
    assert judge.seen


def test_pending_auto_action_resumes_saved_verdict_without_rejudging(
        svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    apply = safety.apply_auto_distinct
    monkeypatch.setattr(
        safety, "apply_auto_distinct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("interrupted after memo commit")))
    assert "error" in svc.deep_dream_judge_curation(SlotJudge())
    assert svc._storage.curation_judgments("lesson")

    class NoSecondCall(SlotJudge):
        def judge_slot_pairs(self, rows):
            raise AssertionError("a fresh saved verdict must resume without a model call")

    monkeypatch.setattr(safety, "apply_auto_distinct", apply)
    out = svc.deep_dream_judge_curation(NoSecondCall())
    assert out["judged"] == 0
    assert out["applied"] == 1
    assert svc.curation_duplicates()["lesson_duplicates"] == []


def test_pending_auto_action_rejudges_without_current_served_identity(
        svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    first = SlotJudge()
    first.served_model = "observed-first-model"
    monkeypatch.setattr(
        safety, "apply_auto_distinct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("interrupted after observed judgment")))
    assert "error" in svc.deep_dream_judge_curation(first)

    actions = []

    def record_action(*args, **kwargs):
        actions.append((args, kwargs))
        return {"dismissed": 1}

    second = SlotJudge()
    second.served_model = None
    monkeypatch.setattr(safety, "apply_auto_distinct", record_action)
    out = svc.deep_dream_judge_curation(second)

    assert out["judged"] == 1
    assert out["applied"] == 0
    assert out["deferred_identity_unknown"] == 1
    assert second.seen
    assert actions == []
    bindings = svc._storage.get_meta("curation_judgment_evidence_v1")
    assert {item["action"] for item in bindings.values()} == {
        "deferred_identity_unknown"}

    class NoImmediateRecall(SlotJudge):
        served_model = None

        def judge_slot_pairs(self, rows):
            raise AssertionError("unchanged deferred judgment must stay quiet")

    assert svc.deep_dream_judge_curation(NoImmediateRecall())["judged"] == 0

    assert svc.review_rejudge("curation", limit=1)["requeued"] == 1
    explicit = SlotJudge()
    explicit.served_model = None
    out = svc.deep_dream_judge_curation(explicit)
    assert out["judged"] == 1
    assert out["deferred_identity_unknown"] == 1

    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is CEO", freshness_class="slow",
        retrieved_at=100.0)
    svc.world_write("Acme", "CEO", **common)
    svc.world_write("Acme Corp", "chief executive", **common)
    later = SlotJudge()
    later.served_model = None
    out = svc.deep_dream_judge_curation(later, limit=1)
    assert out["judged"] == 1
    assert out["deferred_identity_unknown"] == 1
    assert later.seen[0]["store"] == "world"


@pytest.mark.parametrize("response_model", [None, "served-model-b"])
def test_served_identity_rotation_discards_fresh_verdict_before_memo_or_action(
        svc, monkeypatch, response_model):
    import pseudolife_memory.curation_safety as safety

    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    actions = []

    class RotatingJudge(SlotJudge):
        served_model = "served-model-a"

        def judge_slot_pairs(self, rows):
            verdicts = super().judge_slot_pairs(rows)
            self.served_model = response_model
            return verdicts

    monkeypatch.setattr(
        safety, "apply_auto_distinct",
        lambda *args, **kwargs: actions.append((args, kwargs)))
    out = svc.deep_dream_judge_curation(RotatingJudge())

    assert out == {"judged": 0, "applied": 0,
                   "reason": "served_model_changed",
                   "observed_before": "served-model-a",
                   "observed_after": response_model,
                   "reopened_auto_dismissals": 0}
    assert svc._storage.curation_judgments("lesson") == {}
    assert actions == []


def test_curation_policy_binds_payload_fields_and_observed_model(svc):
    lesson_pair(svc)
    svc.config.memory.deep_dream.curation_judge_mode = "shadow"
    first = SlotJudge()
    first.max_tokens = 100
    first.extra_body = {"reasoning_effort": "high"}
    first.judge_thinking = "high"
    first.served_model = "served-a"
    assert svc.deep_dream_judge_curation(first)["judged"] == 1

    same = SlotJudge()
    same.max_tokens = 100
    same.extra_body = {"reasoning_effort": "high"}
    same.judge_thinking = "high"
    same.served_model = "served-a"
    assert svc.deep_dream_judge_curation(same)["judged"] == 0

    changed_payload = SlotJudge()
    changed_payload.max_tokens = 101
    changed_payload.extra_body = {"reasoning_effort": "high"}
    changed_payload.judge_thinking = "high"
    changed_payload.served_model = "served-a"
    assert svc.deep_dream_judge_curation(changed_payload)["judged"] == 1

    changed_model = SlotJudge()
    changed_model.max_tokens = 101
    changed_model.extra_body = {"reasoning_effort": "high"}
    changed_model.judge_thinking = "high"
    changed_model.served_model = "served-b"
    assert svc.deep_dream_judge_curation(changed_model)["judged"] == 1


def test_policy_change_during_call_blocks_memo_and_action(svc):
    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9

    class MutatingPolicyJudge(SlotJudge):
        max_tokens = 100

        def judge_slot_pairs(self, rows):
            self.max_tokens = 101
            return super().judge_slot_pairs(rows)

    out = svc.deep_dream_judge_curation(MutatingPolicyJudge())
    assert out == {"judged": 0, "reason": "policy_changed",
                   "reopened_auto_dismissals": 0}
    assert svc._storage.curation_judgments("lesson") == {}
    assert len(svc.curation_duplicates()["lesson_duplicates"]) == 1


def test_curation_batch_rotates_first_store_to_prevent_starvation(svc):
    svc.lesson_write("deploy a", "approach", "Back up first")
    svc.lesson_write("deploy b", "approach", "Back up first")
    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is CEO", freshness_class="slow",
        retrieved_at=100.0)
    svc.world_write("Acme", "CEO", **common)
    svc.world_write("Acme Corp", "chief executive", **common)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "shadow"
    cfg.judge_batch = 1

    first, second = SlotJudge(), SlotJudge()
    assert svc.deep_dream_judge_curation(first)["judged"] == 1
    assert first.seen[0]["store"] == "lesson"
    assert svc.deep_dream_judge_curation(second)["judged"] == 1
    assert second.seen[0]["store"] == "world"


def test_pending_actions_share_the_batch_cap_and_rotate_store_first(
        svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    svc.lesson_write("deploy a", "approach", "Back up first")
    svc.lesson_write("deploy b", "approach", "Back up first")
    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is CEO", freshness_class="slow",
        retrieved_at=100.0)
    svc.world_write("Acme", "CEO", **common)
    svc.world_write("Acme Corp", "chief executive", **common)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    cfg.judge_batch = 2

    monkeypatch.setattr(
        safety, "apply_auto_distinct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("interrupted before either saved action applied")))
    assert "error" in svc.deep_dream_judge_curation(SlotJudge())
    assert len(svc._storage.curation_judgments("lesson")) == 1
    assert len(svc._storage.curation_judgments("world")) == 1

    calls = []

    def record_one(_service, evidence, **_kwargs):
        calls.append(evidence.store)
        return {"dismissed": 1}

    class NoSecondCall(SlotJudge):
        def judge_slot_pairs(self, rows):
            raise AssertionError("pending actions replay without a model call")

    monkeypatch.setattr(safety, "apply_auto_distinct", record_one)
    assert svc.deep_dream_judge_curation(NoSecondCall(), limit=1)["applied"] == 1
    assert calls == ["lesson"]
    assert svc.deep_dream_judge_curation(NoSecondCall(), limit=1)["applied"] == 1
    assert calls == ["lesson", "world"]


def test_pending_action_cannot_starve_other_store_judgment(svc, monkeypatch):
    import pseudolife_memory.curation_safety as safety

    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    monkeypatch.setattr(
        safety, "apply_auto_distinct",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("lesson action remains pending")))
    assert "error" in svc.deep_dream_judge_curation(SlotJudge(), limit=1)

    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is CEO", freshness_class="slow",
        retrieved_at=100.0)
    svc.world_write("Acme", "CEO", **common)
    svc.world_write("Acme Corp", "chief executive", **common)
    actions = []

    def record_one(_service, evidence, **_kwargs):
        actions.append(evidence.store)
        return {"dismissed": 1}

    monkeypatch.setattr(safety, "apply_auto_distinct", record_one)
    world = SlotJudge()
    out = svc.deep_dream_judge_curation(world, limit=1)
    assert out["judged"] == 1 and out["applied"] == 1
    assert world.seen[0]["store"] == "world"
    assert actions == ["world"]

    class NoSecondCall(SlotJudge):
        def judge_slot_pairs(self, rows):
            raise AssertionError("the saved lesson verdict should replay")

    assert svc.deep_dream_judge_curation(NoSecondCall(), limit=1)["applied"] == 1
    assert actions == ["world", "lesson"]


def test_served_model_probe_is_outside_lock_once_per_judge_phase(
        svc, monkeypatch):
    from pseudolife_memory.memory import dream

    lesson_pair(svc)
    svc.config.memory.deep_dream.curation_judge_mode = "shadow"
    judge = SlotJudge()
    judge.model = "extractor"
    judge.served_model = None
    judge.base_url = "http://example.test/v1"
    calls = []

    def probe(url):
        assert not svc._lock.locked()
        calls.append(url)
        return None

    monkeypatch.setattr(dream, "fetch_served_model", probe)
    assert curation_observed_model(judge) is None
    calls.clear()
    assert svc.deep_dream_judge_curation(judge)["judged"] == 1
    # Once before memo classification and once after the response.  The
    # number does not grow with the candidate count, and no requested alias
    # is represented as an observed identity when probing fails.
    assert calls == [judge.base_url, judge.base_url]


def test_prior_response_identity_reuses_a_direct_model_memo_without_probe(
        svc, monkeypatch):
    from pseudolife_memory.memory import dream

    lesson_pair(svc)
    svc.config.memory.deep_dream.curation_judge_mode = "shadow"
    first = SlotJudge()
    first.model = "dated-request-model"
    first.base_url = "http://example.test/v1"
    first.served_model = "actual-response-model"
    assert svc.deep_dream_judge_curation(first)["judged"] == 1

    second = SlotJudge()
    second.model = first.model
    second.base_url = first.base_url
    second.served_model = None
    monkeypatch.setattr(
        dream, "fetch_served_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct names are not resolved from /models order")))
    assert curation_observed_model(second) is None
    assert svc.deep_dream_judge_curation(second)["judged"] == 0


def test_changed_input_expires_only_automatic_distinct_dismissal(svc):
    evidence = lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    assert svc.deep_dream_judge_curation(SlotJudge())["applied"] == 1
    assert svc.curation_duplicates()["lesson_duplicates"] == []

    side = evidence.b
    svc.lesson_write(
        side["entity"], side["attribute"], side["value"],
        about=side["about"], outcome=side["outcome"],
        polarity=side["polarity"], confidence=side["confidence"],
        origin=side["origin"], provenance={"new-version"})
    assert len(svc.curation_duplicates()["lesson_duplicates"]) == 1


def test_human_confirmation_makes_an_auto_dismissal_permanent(svc):
    evidence = lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    assert svc.deep_dream_judge_curation(SlotJudge())["applied"] == 1
    svc.curation_dismiss_duplicate(
        "lesson", evidence.a["entity"], evidence.a["attribute"],
        evidence.b["entity"], evidence.b["attribute"])

    side = evidence.b
    svc.lesson_write(
        side["entity"], side["attribute"], side["value"],
        about=side["about"], outcome=side["outcome"],
        polarity=side["polarity"], confidence=side["confidence"],
        origin=side["origin"], provenance={"new-version"})
    assert svc.curation_duplicates()["lesson_duplicates"] == []


@pytest.mark.parametrize("change", ["mode", "served_model"])
def test_closed_auto_distinct_pair_reopens_when_judgment_policy_changes(
        svc, change):
    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    first = SlotJudge()
    first.served_model = "served-model-a"
    assert svc.deep_dream_judge_curation(first)["applied"] == 1
    assert svc.curation_duplicates()["lesson_duplicates"] == []
    marker = next(iter(svc._storage.get_meta(
        "curation_auto_dismissals_v1").values()))
    assert marker["requested_policy"]
    assert marker["observed_model"] == "served-model-a"

    second = SlotJudge()
    second.served_model = "served-model-a"
    if change == "mode":
        cfg.curation_judge_mode = "auto"
    else:
        second.served_model = "served-model-b"
    out = svc.deep_dream_judge_curation(second)

    assert out["reopened_auto_dismissals"] == 1
    assert out["judged"] == 1
    assert out["applied"] == 1
    assert second.seen


def test_unknown_current_identity_keeps_closed_auto_distinct_pair_quiet(svc):
    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    first = SlotJudge()
    first.served_model = "served-model-a"
    assert svc.deep_dream_judge_curation(first)["applied"] == 1
    before = svc._storage.get_meta("curation_auto_dismissals_v1")

    class UnknownIdentity(SlotJudge):
        served_model = None

        def judge_slot_pairs(self, rows):
            raise AssertionError("unknown identity must not reopen or retry")

    out = svc.deep_dream_judge_curation(UnknownIdentity())

    assert out == {"judged": 0, "reopened_auto_dismissals": 0}
    assert svc._storage.get_meta("curation_auto_dismissals_v1") == before
    assert svc.curation_duplicates()["lesson_duplicates"] == []


def test_response_only_model_change_reopens_old_marker_on_next_bounded_tick(svc):
    lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    first = SlotJudge()
    first.served_model = "served-model-a"
    assert svc.deep_dream_judge_curation(first, limit=1)["applied"] == 1

    common = dict(
        value="Jane Doe", confidence=0.9,
        source_url="https://example.test/report",
        source_quote="Jane Doe is CEO", freshness_class="slow",
        retrieved_at=100.0)
    svc.world_write("Acme", "CEO", **common)
    svc.world_write("Acme Corp", "chief executive", **common)

    class ResponseOnlyB(SlotJudge):
        served_model = None

        def judge_slot_pairs(self, rows):
            verdicts = super().judge_slot_pairs(rows)
            self.served_model = "served-model-b"
            return verdicts

    visible = ResponseOnlyB()
    out = svc.deep_dream_judge_curation(visible, limit=1)
    assert out["judged"] == 1 and out["applied"] == 1
    assert visible.seen[0]["store"] == "world"

    # The endpoint probe can lag the real response. The durable B response
    # must still invalidate marker A even when the next pre-call probe says A.
    reconsidered = SlotJudge()
    reconsidered.served_model = "served-model-a"
    out = svc.deep_dream_judge_curation(reconsidered, limit=1)
    assert out["reopened_auto_dismissals"] == 1
    assert out["judged"] == 1 and out["applied"] == 1
    assert reconsidered.seen[0]["store"] == "lesson"


def test_evidence_reopen_count_is_returned_without_constructing_a_judge(
        svc, monkeypatch):
    evidence = lesson_pair(svc)
    cfg = svc.config.memory.deep_dream
    cfg.curation_judge_mode = "auto-distinct"
    cfg.curation_distinct_min_confidence = 0.9
    assert svc.deep_dream_judge_curation(SlotJudge())["applied"] == 1
    loser = evidence.b
    svc.lesson_forget(loser["entity"], loser["attribute"])

    monkeypatch.setattr(
        svc, "_judge_extractor",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no markers or candidates means no judge setup")))
    assert svc.deep_dream_judge_curation() == {
        "judged": 0, "reopened_auto_dismissals": 1}
