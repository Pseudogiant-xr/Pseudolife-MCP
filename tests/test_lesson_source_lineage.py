"""Source-backed lesson lineage, using an isolated bank and fixed CPU vectors."""
from contextlib import nullcontext

import psycopg
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_correction_identity import _seed, _service


@pytest.fixture
def svc(tmp_path, monkeypatch, pg_conn, pg_url):
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
    from pseudolife_memory.memory.lessons import LessonStore
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    service = _service(tmp_path, monkeypatch, storage, embedding_dim=1024)
    service._lessons = LessonStore()
    service._cortex = CortexStore()
    service._graph = PostgresNetworkxGraphStore(storage)
    service.config.memory.lessons.synthesis_dedup_min_similarity = 0
    yield service
    storage.close()


class Rules:
    def extract_lessons(self, signals):
        return []

    def extract_rules(self, signals):
        return [dict(task=s["task"], aspect="rule", lesson=f"Rule for {s['task']}",
                     about="git", polarity="+", outcome="success")
                for s in signals]


def backed_signal(svc, entry, task="backed"):
    sid = svc._storage.add_signal(task, "success", about="rule:git",
                                  episode_id=entry.episode_id)
    svc._storage.set_signal_used_ids(sid, {
        "credited": [entry.db_id], "unmatched": [], "served_elsewhere": []})
    return sid


def rows(svc):
    return {r["entity"]: r for r in svc._storage.load_lessons()}


def clustered_signal(svc, entry, task):
    """A plain (clustering-route) signal crediting ``entry``."""
    sid = backed_signal(svc, entry, task)
    svc._storage.conn.execute(
        "UPDATE outcome_signals SET about = 'git' WHERE id = %s", (sid,))
    svc._storage.conn.commit()
    return sid


def statuses(svc):
    """Durable and resident status of every lesson, which must agree."""
    durable = {r["entity"]: r["status"] for r in svc._storage.load_lessons()}
    resident = {r.entity: r.status for r in svc._lessons.records}
    assert durable == resident
    return durable


def lineage_sources(svc, task):
    return {i for d in svc._storage.store_decisions("lesson", entity_norm=task)
            if d["action"] == "lineage" for i in d["record"]["source_entry_ids"]}


def test_forget_retires_only_trusted_dependent_lessons(svc):
    source = _seed(svc)
    neighbor = _seed(svc, "Unrelated evidence", episode=source.episode_id)
    sid = backed_signal(svc, source)
    backed_signal(svc, neighbor, "neighbor")
    svc.lesson_write("legacy", "rule", "No reconstructable dependency",
                     provenance=[f"episode:{source.episode_id}",
                                 f"entry:{source.db_id}", f"signal:{sid}"])
    assert svc.synthesize_lessons(Rules())["lessons"] == 2
    assert f"entry:{source.db_id}" in rows(svc)["backed"]["provenance"]
    svc.delete(text=source.text)
    stored = rows(svc)
    assert stored["backed"]["status"] == "retired"
    assert stored["neighbor"]["status"] == "current"
    assert stored["legacy"]["status"] == "current"
    assert {r.entity: r.status for r in svc._lessons.records} == {
        "backed": "retired", "neighbor": "current", "legacy": "current"}
    assert stored["backed"]["provenance"] == sorted({
        f"signal:{sid}", f"episode:{source.episode_id}", f"entry:{source.db_id}"})


@pytest.mark.parametrize("operation", ["forget", "dependencies"])
def test_source_mutation_during_extraction_cannot_publish_or_ack(svc, operation):
    source = _seed(svc)
    sid = backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Previously valid rule")

    class Changed(Rules):
        def extract_rules(self, signals):
            if operation == "forget":
                svc.delete(text=source.text)
            else:
                svc._storage.set_signal_used_ids(sid, {"credited": []})
            return super().extract_rules(signals)

    result = svc.synthesize_lessons(Changed())
    assert result["lessons"] == 0, result
    assert result["skipped"] == "signals-changed"
    assert set(rows(svc)) == {"prior"}
    assert rows(svc)["prior"]["status"] == "current"
    assert [s["id"] for s in svc._storage.pending_signals()] == [sid]


def test_extractor_cannot_mutate_nested_trusted_dependency_snapshot(svc):
    source = _seed(svc)
    sid = backed_signal(svc, source)

    class Annotated(Rules):
        def extract_rules(self, signals):
            signals[0]["used_ids"]["credited"].clear()
            return super().extract_rules(signals)

    assert svc.synthesize_lessons(Annotated())["lessons"] == 1
    assert f"entry:{source.db_id}" in rows(svc)["backed"]["provenance"]
    assert svc._storage.conn.execute(
        "SELECT consumed_at FROM outcome_signals WHERE id = %s", (sid,)).fetchone()[0]


def test_failed_source_correction_preserves_previously_valid_lesson(svc, monkeypatch):
    from pseudolife_memory.memory.cms import ContinuumMemorySystem

    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    before = {k: v for k, v in rows(svc)["backed"].items() if k != "embedding"}
    original = ContinuumMemorySystem.store

    def rejected(self, text, *args, **kwargs):
        if text == "Rejected replacement":
            return False, 0.0
        return original(self, text, *args, **kwargs)

    monkeypatch.setattr(ContinuumMemorySystem, "store", rejected)
    result = svc.supersede(entry_id=source.db_id, new_text="Rejected replacement")
    assert result["reason"] == "replacement_rejected"
    assert {k: v for k, v in rows(svc)["backed"].items() if k != "embedding"} == before
    assert svc._lessons.records[0].status == "current"
    svc._save_lessons()
    assert rows(svc)["backed"]["status"] == "current"


def test_source_retirement_refresh_failure_cannot_be_undone_by_autosave(svc, monkeypatch):
    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    original = svc._storage.load_lessons

    def unavailable():
        raise RuntimeError("injected retirement reload failure")

    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    assert svc.delete(text=source.text)["deleted_count"] == 1
    assert not any(e.db_id == source.db_id for band in svc._cms.bands for e in band.entries)
    assert original()[0]["status"] == "retired"
    with pytest.raises(RuntimeError, match="retirement reload failure"):
        svc._save_lessons()
    assert original()[0]["status"] == "retired"
    monkeypatch.setattr(svc._storage, "load_lessons", original)
    svc._save_lessons()
    assert svc._lessons.records[0].status == "retired"
    assert original()[0]["status"] == "retired"


@pytest.mark.parametrize("forgotten", [True, False], ids=["forget", "plain-delete"])
def test_storage_source_deletion_retires_lessons_before_autosave(svc, forgotten):
    source = _seed(svc)
    backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Unrelated valid rule")
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    svc._storage.delete_entry_ids([source.db_id], forgotten=forgotten)
    svc._save_lessons()
    assert rows(svc)["backed"]["status"] == ("retired" if forgotten else "current")
    assert rows(svc)["prior"]["status"] == "current"


def test_shipped_rule_parser_keeps_failed_middle_signal_pending_with_exact_lineage(svc, monkeypatch):
    from pseudolife_memory.memory.dream import OpenAICompatExtractor
    from tests.test_lesson_rule_mode import _urlopen_recorder

    ids = []
    for task in ("first", "middle", "last"):
        source = _seed(svc, f"Evidence for {task}", episode=f"episode-{task}")
        ids.append(backed_signal(svc, source, task))
    svc._storage.conn.execute(
        "UPDATE outcome_signals SET detail = %s WHERE id = %s",
        ("MUST INCLUDE: rollback-tag-17", ids[1]))
    svc._storage.conn.commit()

    def reply(task):
        return {"lessons": [{"task": task, "lesson": f"WHEN deploy THEN check {task}",
                             "about": "git", "polarity": "+", "outcome": "success",
                             "provenance": ["signal:999999", "episode:forged"]}]}

    calls = _urlopen_recorder(monkeypatch, [reply("first"), reply("middle"),
                                           {"lessons": []}, reply("last")])
    result = svc.synthesize_lessons(OpenAICompatExtractor("http://mock.invalid/v1", "stub"))
    assert result["lessons"] == 2
    assert len(calls) == 4
    assert [s["id"] for s in svc._storage.pending_signals()] == [ids[1]]
    stored = rows(svc)
    assert set(stored) == {"first", "last"}
    for task, sid in (("first", ids[0]), ("last", ids[2])):
        assert f"signal:{sid}" in stored[task]["provenance"]
        assert f"episode:episode-{task}" in stored[task]["provenance"]
        assert "signal:999999" not in stored[task]["provenance"]


def test_dependency_audit_failure_rolls_back_lesson_and_ack(svc, monkeypatch):
    source = _seed(svc)
    sid = backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Previously valid rule")
    original = svc._storage.record_store_decision

    def unavailable(store, entity, attribute, action, **kwargs):
        if action == "lineage":
            raise RuntimeError("injected lineage persistence failure")
        return original(store, entity, attribute, action, **kwargs)

    monkeypatch.setattr(svc._storage, "record_store_decision", unavailable)
    result = svc.synthesize_lessons(Rules())
    assert result["lessons"] == 0
    assert result["write_errors"] == 1
    assert set(rows(svc)) == {"prior"}
    assert [s["id"] for s in svc._storage.pending_signals()] == [sid]
    assert not svc._storage.store_decisions("lesson")
    assert svc._lessons.records[0].status == "current"


def test_forgotten_episode_source_keeps_digest_backed_lesson(svc):
    root = svc._cms.episodes.start_session(
        title="Synthetic source episode", session_key="digest-lineage")
    source = _seed(svc, "Original source evidence", episode=root.id)
    neighbor = _seed(svc, "Surviving source evidence", episode=root.id)
    digest = _seed(svc, "Synthetic episode digest", source="digest", episode=root.id)
    backed_signal(svc, digest, "digest-backed")
    backed_signal(svc, neighbor, "neighbor")
    svc.lesson_write("legacy", "rule", "Unverified source-like tokens",
                     provenance=[f"entry:{digest.db_id}", f"episode:{root.id}"])
    assert svc.synthesize_lessons(Rules())["lessons"] == 2

    svc.delete(text=source.text)
    assert svc._storage.get_entry(digest.db_id)["superseded_at"] is not None
    assert statuses(svc) == {
        "neighbor": "current", "legacy": "current", "digest-backed": "current"}
    svc.delete(text=digest.text)
    assert svc._storage.get_entry(digest.db_id) is None
    assert statuses(svc) == {
        "neighbor": "current", "legacy": "current", "digest-backed": "retired"}
    svc._save_lessons()
    assert rows(svc)["digest-backed"]["status"] == "retired"


@pytest.mark.parametrize("rejection", ["failed", "empty", "accepted"])
def test_stale_rule_source_cannot_veto_independent_valid_neighbor(svc, rejection):
    stale = _seed(svc, "Old source evidence")
    valid = _seed(svc, "Valid neighbor source evidence")
    old_sid = backed_signal(svc, stale, "failed-old")
    new_sid = backed_signal(svc, valid, "valid-new")
    svc.delete(text=stale.text)
    svc.lesson_write("prior", "rule", "Previously valid rule")

    class PartialRules(Rules):
        def extract_rules(self, signals):
            self.last_rule_failed_ids = [old_sid] if rejection == "failed" else []
            self.last_rule_empty_ids = [old_sid] if rejection == "empty" else []
            return super().extract_rules(
                [s for s in signals if s["id"] == new_sid or rejection == "accepted"])

    results = [svc.synthesize_lessons(PartialRules(), limit=2) for _ in range(2)]
    assert [s["id"] for s in svc._storage.pending_signals()] == [old_sid], results
    assert set(rows(svc)) == {"prior", "valid-new"}
    stored = rows(svc)["valid-new"]
    assert stored["status"] == "current"
    assert stored["provenance"] == sorted({
        f"signal:{new_sid}", f"episode:{valid.episode_id}", f"entry:{valid.db_id}"})


@pytest.mark.parametrize("operation", ["direct", "forget"])
@pytest.mark.parametrize("outer_transaction", [False, True], ids=["standalone", "nested"])
def test_source_cascade_connection_loss_rolls_back_source_lesson_and_audit(
        svc, monkeypatch, operation, outer_transaction):
    source = _seed(svc, "Source protected against a split transaction")
    backed_signal(svc, source)
    svc.lesson_write("prior", "rule", "Unrelated valid guidance")
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    before = svc._storage.store_decisions("lesson")
    before_drops = svc._storage.get_meta("capacity_true_drops")
    original = svc._storage.retire_lessons_for_entries

    def disconnect_before_cascade(*args, **kwargs):
        svc._storage._conn.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(svc._storage, "retire_lessons_for_entries", disconnect_before_cascade)
    with pytest.raises(psycopg.OperationalError):
        with svc._storage.transaction() if outer_transaction else nullcontext():
            if operation == "forget":
                svc._storage.forget_entry_ids(
                    [source.db_id], digest_ids=[],
                    digest_cursor=svc._load_digest_cursor(), now=12345.0)
            else:
                svc._storage.delete_entry_ids([source.db_id], forgotten=True)

    surviving_source = svc._storage.get_entry(source.db_id)
    assert surviving_source is not None
    assert surviving_source["superseded_at"] is None
    assert {key: row["status"] for key, row in rows(svc).items()} == {
        "backed": "current", "prior": "current"}
    assert svc._storage.store_decisions("lesson") == before
    assert svc._storage.get_meta("capacity_true_drops") == before_drops
    svc._save_lessons()
    assert rows(svc)["backed"]["status"] == "current"


# Retirement rule, as the v51 forget cascade applies to facts and edges: a
# lesson retires only when an explicit forget removes the LAST of its trusted
# sources. Supersession, consolidation and capacity eviction keep it.


class TwoClusters:
    def extract_lessons(self, signals):
        return [dict(task="first-cluster", lesson="Use current evidence", about="git"),
                dict(task="second-cluster", lesson="Check the source first", about="git")]


def test_clustered_lessons_retire_only_when_last_batch_source_is_forgotten(
        svc, monkeypatch):
    first = _seed(svc, "Clustered evidence A")
    second = _seed(svc, "Clustered evidence B")
    clustered_signal(svc, first, "a")
    clustered_signal(svc, second, "b")
    svc.lesson_write("prior", "rule", "Unrelated guidance")
    assert svc.synthesize_lessons(TwoClusters())["lessons"] == 2
    assert lineage_sources(svc, "first-cluster") == {first.db_id, second.db_id}
    original_load = svc._storage.load_lessons
    reloads = []

    def counted_load():
        reloads.append(True)
        return original_load()

    monkeypatch.setattr(svc._storage, "load_lessons", counted_load)
    svc.delete(text=first.text)
    assert reloads == []  # nothing retired, so nothing to mirror
    assert statuses(svc) == {
        "prior": "current", "first-cluster": "current", "second-cluster": "current"}
    reloads.clear()
    svc.delete(text=second.text)
    assert reloads == [True]
    assert statuses(svc) == {
        "prior": "current", "first-cluster": "retired", "second-cluster": "retired"}
    retirements = [d for d in svc._storage.store_decisions("lesson")
                   if d["action"] == "retire"]
    assert sorted((d["entity_norm"], d["reason"]) for d in retirements) == [
        ("first-cluster", "source_forgotten"), ("second-cluster", "source_forgotten")]


def test_already_evicted_batch_source_counts_as_gone_at_the_last_forget(svc):
    terminal = len(svc._cms.bands) - 1
    svc._cms.bands[terminal].max_entries = 1
    evicted = _seed(svc, "Clustered evidence later evicted", band=terminal)
    kept = _seed(svc, "Clustered evidence later forgotten", band=0)
    clustered_signal(svc, evicted, "a")
    clustered_signal(svc, kept, "b")
    assert svc.synthesize_lessons(TwoClusters())["lessons"] == 2
    _seed(svc, "Incoming entry at capacity", band=terminal)
    assert svc._storage.get_entry(evicted.db_id) is None
    assert statuses(svc) == {"first-cluster": "current", "second-cluster": "current"}
    svc.delete(text=kept.text)
    assert statuses(svc) == {"first-cluster": "retired", "second-cluster": "retired"}


def test_superseded_source_keeps_its_lesson_until_forgotten(svc):
    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    svc.supersede(entry_id=source.db_id, new_text="Corrected deployment evidence")
    assert svc._storage.get_entry(source.db_id)["superseded_at"] is not None
    assert statuses(svc) == {"backed": "current"}
    assert not [d for d in svc._storage.store_decisions("lesson") if d["action"] == "retire"]
    # The superseded entry still exists as history and still supports the
    # lesson; forgetting it removes the last source.
    svc.delete(text=source.text)
    assert statuses(svc) == {"backed": "retired"}


def test_consolidated_duplicate_keeps_dependent_lesson(svc):
    source = _seed(svc, "Deploys go through the blue gateway")
    duplicate = _seed(svc, "Deploys go through the blue gateway again")
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    result = svc.consolidate(entry_ids=[source.db_id, duplicate.db_id],
                             new_text="Deploys go through the blue gateway (merged)")
    assert result["superseded_count"] == 2
    assert statuses(svc) == {"backed": "current"}


def test_rederived_legacy_lesson_never_gains_lineage(svc):
    source = _seed(svc)
    svc.lesson_write("backed", "rule", "Rule for backed")
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    # The confirmation merges the token into provenance, but no audit makes it
    # a verified dependency of a lesson that never had lineage.
    assert f"entry:{source.db_id}" in rows(svc)["backed"]["provenance"]
    assert lineage_sources(svc, "backed") == set()
    svc.delete(text=source.text)
    assert statuses(svc) == {"backed": "current"}


def test_confirmation_adds_a_supporting_source_to_existing_lineage(svc):
    original = _seed(svc, "Original rule evidence")
    extra = _seed(svc, "Later confirming evidence")
    backed_signal(svc, original)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    backed_signal(svc, extra)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    assert len(svc._lessons.records) == 1  # confirmed, not superseded
    assert lineage_sources(svc, "backed") == {original.db_id, extra.db_id}
    svc.delete(text=original.text)
    assert statuses(svc) == {"backed": "current"}
    svc.delete(text=extra.text)
    assert statuses(svc) == {"backed": "retired"}


def test_continuum_forget_without_service_cascade_is_a_forget(svc):
    source = _seed(svc)
    backed_signal(svc, source)
    assert svc.synthesize_lessons(Rules())["lessons"] == 1
    assert svc._cms.delete_entries(text=source.text) == [source.text]
    assert svc._storage.get_entry(source.db_id) is None
    svc._save_lessons()
    assert statuses(svc) == {"backed": "retired"}
