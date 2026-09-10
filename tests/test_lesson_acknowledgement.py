"""Durable lesson acknowledgement faults, using real PostgreSQL and CPU vectors."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


class Vectors:
    def encode_single(self, text):
        vector = torch.zeros(1024)
        vector[0] = 1
        return vector


class Extractor:
    def __init__(self, claims):
        self.claims = claims

    def extract_lessons(self, signals):
        return self.claims


def claim(task="deploy", lesson="Keep a rollback tag", **extra):
    return dict(task=task, aspect="approach", lesson=lesson, about="git",
                polarity="+", **extra)


def make_service(path, pg_url):
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.memory.lessons import LessonStore
    from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.sync import hydrate_lessons

    service = MemoryService(data_dir=path, database_url=pg_url)
    service._storage = PostgresStorage(pg_url)
    service._graph = PostgresNetworkxGraphStore(service._storage)
    service._cms = object()  # initialized, with no model or background workers
    service._embedder = Vectors()
    service._lessons = LessonStore()
    hydrate_lessons(service._lessons, service._storage)
    service.config.memory.lessons.synthesis_dedup_min_similarity = 0
    return service


@pytest.fixture
def svc(tmp_path, pg_conn, pg_url):
    service = make_service(tmp_path, pg_url)
    yield service
    service._storage.close()


def signal(svc, task="deploy", about="git"):
    return svc._storage.add_signal(task, "success", about=about, origin="action")


def pending(svc):
    return [s["id"] for s in svc._storage.pending_signals()]


def durable_values(svc):
    return [r["value"] for r in svc._storage.load_lessons()]


def test_second_claim_failure_rolls_back_first_lesson_graph_and_ack(svc, monkeypatch):
    ids = [signal(svc), signal(svc, "release")]
    original = svc._link_lesson_graph

    def fail_second(task, about, polarity):
        original(task, about, polarity)
        if task == "release":
            svc._storage.conn.execute("SELECT 1 / 0")

    monkeypatch.setattr(svc, "_link_lesson_graph", fail_second)
    result = svc.synthesize_lessons(Extractor([claim(), claim("release", "Check health")]))
    assert result["lessons"] == 0
    assert pending(svc) == ids
    assert durable_values(svc) == []
    assert svc._lessons.records == []
    assert svc._storage.conn.execute("SELECT count(*) FROM entities").fetchone()[0] == 0


def test_ack_failure_after_sql_leaves_everything_retryable(svc, monkeypatch):
    ids = [signal(svc)]
    original = svc._storage.consume_signals

    def fail_ack(ids, now=None):
        original(ids, now=now)
        raise RuntimeError("ack response lost before transaction commit")

    monkeypatch.setattr(svc._storage, "consume_signals", fail_ack)
    try:
        svc.synthesize_lessons(Extractor([claim()]))
    except RuntimeError:
        pass  # old implementation propagated after independently committing
    assert pending(svc) == ids
    assert durable_values(svc) == []
    assert svc._lessons.records == []


def test_dirty_memory_cannot_acknowledge_without_persisting_its_lesson(svc, monkeypatch):
    from pseudolife_memory.service import PersistenceError

    original = svc._storage.replace_slot_lessons

    def fail_save(*args, **kwargs):
        raise RuntimeError("lesson storage unavailable")

    monkeypatch.setattr(svc._storage, "replace_slot_lessons", fail_save)
    with pytest.raises(PersistenceError):
        svc.lesson_write("original task", "approach", "Keep a rollback tag")
    assert svc._lessons.dirty_slots
    assert durable_values(svc) == []
    ids = [signal(svc)]
    svc.config.memory.lessons.synthesis_dedup_min_similarity = 0.9
    svc.synthesize_lessons(Extractor([claim()]))
    assert pending(svc) == ids
    assert svc._lessons.dirty_slots
    monkeypatch.setattr(svc._storage, "replace_slot_lessons", original)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert result["deduped"] == 1
    assert pending(svc) == []
    assert durable_values(svc) == ["Keep a rollback tag"]


def test_overlapping_extraction_does_not_reapply_consumed_work(svc):
    signal(svc)
    entered, release = Event(), Event()

    class Slow(Extractor):
        def extract_lessons(self, signals):
            entered.set()
            assert release.wait(10)
            return self.claims

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(svc.synthesize_lessons, Slow([claim(lesson="Old extraction")]))
        try:
            assert entered.wait(10)
            assert svc.synthesize_lessons(Extractor([claim(lesson="First committed")]))["lessons"] == 1
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result["lessons"] == 0
    assert durable_values(svc) == ["First committed"]
    assert len(svc._lessons.records) == 1


def test_empty_plain_route_stays_pending_when_rule_route_succeeds(svc):
    plain_id = signal(svc)
    rule_id = signal(svc, "release", "rule:git")

    class Mixed(Extractor):
        def extract_rules(self, signals):
            return [dict(task="release", aspect="rule", lesson="Check health", about="git")]

    result = svc.synthesize_lessons(Mixed([]))
    assert result["lessons"] == 1
    assert pending(svc) == [plain_id]
    assert rule_id not in pending(svc)


def test_restart_reads_only_committed_lesson_batch(svc, tmp_path, pg_url, monkeypatch):
    ids = [signal(svc)]
    original = svc._storage.consume_signals

    def fail_ack(ids, now=None):
        original(ids, now=now)
        raise RuntimeError("ack write interrupted")

    monkeypatch.setattr(svc._storage, "consume_signals", fail_ack)
    try:
        svc.synthesize_lessons(Extractor([claim()]))
    except RuntimeError:
        pass
    svc._storage.close()
    restarted = make_service(tmp_path / "restart", pg_url)
    try:
        assert pending(restarted) == ids
        assert restarted._lessons.records == []
        assert restarted.synthesize_lessons(Extractor([claim()]))["lessons"] == 1
        assert pending(restarted) == []
        assert durable_values(restarted) == ["Keep a rollback tag"]
    finally:
        restarted._storage.close()


def test_plain_success_survives_failed_rule_route(svc):
    signal(svc)
    rule_id = signal(svc, "release", "rule:git")

    class Mixed(Extractor):
        def extract_rules(self, signals):
            raise RuntimeError("rule endpoint unavailable")

    assert svc.synthesize_lessons(Mixed([claim()]))["lessons"] == 1
    assert pending(svc) == [rule_id]


def test_valid_empty_rule_response_is_not_acknowledged(svc, monkeypatch):
    from pseudolife_memory.memory.dream import OpenAICompatExtractor

    empty_id = signal(svc, "empty situation", "rule:git")
    signal(svc, "release", "rule:git")
    ext = OpenAICompatExtractor("http://unused.invalid/v1", "synthetic")
    replies = iter([[], [{"task": "release", "lesson": "Check health"}]])
    monkeypatch.setattr(ext, "_lessons_completion", lambda *args: next(replies))
    result = svc.synthesize_lessons(ext)
    assert result["lessons"] == 1
    assert pending(svc) == [empty_id]


def test_connection_loss_cannot_reconnect_and_write_outside_batch(svc, pg_conn, monkeypatch):
    ids = [signal(svc)]
    original = svc._link_lesson_graph

    def lose_connection(task, about, polarity):
        original(task, about, polarity)
        victim = svc._storage.conn.info.backend_pid
        pg_conn.execute("SELECT pg_terminate_backend(%s)", (victim,))
        pg_conn.commit()
        # Make the client observe the broken connection before the next helper.
        try:
            svc._storage.conn.execute("SELECT 1")
        except Exception:
            pass

    monkeypatch.setattr(svc, "_link_lesson_graph", lose_connection)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert result["lessons"] == 0
    assert pending(svc) == ids
    assert durable_values(svc) == []
    assert svc._lessons.records == []


def lose_commit_response(svc, monkeypatch):
    original = svc._storage.lesson_synthesis_transaction

    @contextmanager
    def lost_response(signals):
        with original(signals) as current:
            yield current
        # The real outer transaction committed; the caller cannot tell that
        # from the response. Reconnect before the durable reconciliation read.
        svc._storage.close()
        raise RuntimeError("commit response lost")

    monkeypatch.setattr(svc._storage, "lesson_synthesis_transaction", lost_response)


def test_lost_commit_response_rehydrates_before_retry_and_save(svc, monkeypatch):
    signal(svc)
    lose_commit_response(svc, monkeypatch)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert "error" in result
    assert result["lessons"] == 1
    assert result["deduped"] == 0
    assert svc._lesson_synthesis_recovery is None
    assert pending(svc) == []
    assert [r.value for r in svc._lessons.records] == durable_values(svc) == ["Keep a rollback tag"]
    # A later retry must not confirm the same lesson or create new history.
    before = svc._lessons.records[0].last_confirmed
    assert svc.synthesize_lessons(Extractor([claim(lesson="Must not replay")]))["lessons"] == 0
    svc._save_lessons()
    assert durable_values(svc) == ["Keep a rollback tag"]
    assert svc._lessons.records[0].last_confirmed == before


def test_lost_commit_response_reports_durable_dedup(svc, monkeypatch):
    svc.lesson_write("existing task", "approach", "Keep a rollback tag")
    svc.config.memory.lessons.synthesis_dedup_min_similarity = 0.9
    signal(svc)
    lose_commit_response(svc, monkeypatch)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert result["lessons"] == 0
    assert result["deduped"] == 1
    assert pending(svc) == []
    assert durable_values(svc) == ["Keep a rollback tag"]


def test_unavailable_commit_reconciliation_blocks_reads_and_snapshots(svc, monkeypatch):
    from pseudolife_memory.service import PersistenceError

    signal(svc)
    lose_commit_response(svc, monkeypatch)
    load = svc._storage.load_lessons

    def unavailable():
        raise RuntimeError("reconciliation read unavailable")

    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert result["reconciliation_required"] is True
    assert svc._lesson_synthesis_recovery is not None
    assert svc._lessons.records == []  # stale, explicitly unusable
    with pytest.raises(PersistenceError, match="reconciliation required"):
        svc.lessons_dump()
    with pytest.raises(PersistenceError, match="reconciliation required"):
        svc._save_lessons()
    with pytest.raises(PersistenceError, match="reconciliation required"):
        svc._persist_all(kind="explicit")
    monkeypatch.setattr(svc._storage, "load_lessons", load)
    assert svc.lessons_dump()["count"] == 1
    assert svc._lesson_synthesis_recovery is None
    assert durable_values(svc) == ["Keep a rollback tag"]


@pytest.mark.parametrize("change", ["prune", "retarget"])
def test_changed_rows_during_commit_outage_remain_blocked_until_restart(
        svc, tmp_path, pg_url, monkeypatch, change):
    from pseudolife_memory.service import PersistenceError

    selected_id = signal(svc)
    lose_commit_response(svc, monkeypatch)
    load = svc._storage.load_lessons

    def unavailable():
        raise RuntimeError("extended reconciliation outage")

    monkeypatch.setattr(svc._storage, "load_lessons", unavailable)
    assert svc.synthesize_lessons(Extractor([claim()]))["reconciliation_required"]
    monkeypatch.setattr(svc._storage, "load_lessons", load)
    # An external maintenance action removes the evidence the running daemon
    # needs for commit reconciliation. It must not quietly clear its latch.
    if change == "prune":
        svc._storage.conn.execute("DELETE FROM outcome_signals WHERE id = %s", (selected_id,))
    else:
        svc._storage.conn.execute(
            "UPDATE outcome_signals SET task = %s WHERE id = %s",
            ("retargeted task", selected_id))
    with pytest.raises(PersistenceError, match="signals changed"):
        svc.lessons_dump()
    with pytest.raises(PersistenceError, match="signals changed"):
        svc._persist_all(kind="flush")
    assert svc._lesson_synthesis_recovery is not None
    svc._storage.close()
    restarted = make_service(tmp_path / "recovered", pg_url)
    try:
        assert restarted.lessons_dump()["count"] == 1
        assert durable_values(restarted) == ["Keep a rollback tag"]
        assert pending(restarted) == []
    finally:
        restarted._storage.close()


def test_rollback_at_commit_boundary_preserves_preexisting_dirty_state(svc, monkeypatch):
    svc.lesson_write("deploy", "approach", "Original durable lesson")
    # Represent a prior unsaved confirmation, distinct from this batch's work.
    original_rec = svc._lessons.records[0]
    original_rec.provenance.add("pending-confirmation")
    svc._lessons.dirty_slots.add(original_rec.key)
    signal(svc)
    original = svc._storage.lesson_synthesis_transaction

    @contextmanager
    def abort_at_commit(signals):
        with original(signals) as current:
            yield current
            raise RuntimeError("abort immediately before commit")

    monkeypatch.setattr(svc._storage, "lesson_synthesis_transaction", abort_at_commit)
    result = svc.synthesize_lessons(Extractor([claim()]))
    assert result["lessons"] == 0
    assert svc._lesson_synthesis_recovery is None
    assert svc._lessons.records == [original_rec]
    assert original_rec.status == "current"
    assert svc._lessons.dirty_slots == {original_rec.key}
    assert "pending-confirmation" in original_rec.provenance
    assert durable_values(svc) == ["Original durable lesson"]
    svc._save_lessons()
    assert durable_values(svc) == ["Original durable lesson"]
    assert "pending-confirmation" in svc._storage.load_lessons()[0]["provenance"]


def test_failed_lesson_sql_restores_history_and_graph(svc, monkeypatch):
    svc.lesson_write("deploy", "approach", "Old lesson", about="git")
    svc.lesson_write("deploy", "approach", "Current lesson", about="git")
    original_records = [(r.value, r.status, r.last_confirmed) for r in svc._lessons.records]
    original_rows = [(r["value"], r["status"], r["last_confirmed"]) for r in svc._storage.load_lessons()]
    ids = [signal(svc)]
    original = svc._storage.replace_slot_lessons

    def fail_after_lesson_sql(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("lesson SQL succeeded, later storage step failed")

    monkeypatch.setattr(svc._storage, "replace_slot_lessons", fail_after_lesson_sql)
    result = svc.synthesize_lessons(Extractor([claim(lesson="Tentative replacement")]))
    assert result["lessons"] == 0
    assert pending(svc) == ids
    assert [(r.value, r.status, r.last_confirmed) for r in svc._lessons.records] == original_records
    assert [(r["value"], r["status"], r["last_confirmed"]) for r in svc._storage.load_lessons()] == original_rows
    assert svc._lessons.dirty_slots == set()
    monkeypatch.setattr(svc._storage, "replace_slot_lessons", original)
    svc._save_lessons()
    assert "Tentative replacement" not in durable_values(svc)


@pytest.mark.parametrize("change", ["retarget", "remove", "consume"])
def test_changed_extracted_input_is_not_applied(svc, change):
    signal_id = signal(svc)

    class Changed(Extractor):
        def extract_lessons(self, signals):
            if change == "retarget":
                svc._storage.conn.execute(
                    "UPDATE outcome_signals SET episode_id = %s WHERE id = %s",
                    ("different-episode", signal_id))
            elif change == "remove":
                svc._storage.conn.execute("DELETE FROM outcome_signals WHERE id = %s", (signal_id,))
            else:
                svc._storage.consume_signals([signal_id])
            return self.claims

    result = svc.synthesize_lessons(Changed([claim()]))
    assert result["skipped"] == "signals-changed"
    assert result["lessons"] == 0
    assert durable_values(svc) == []
    assert svc._lessons.records == []


def test_extractor_annotations_and_new_arrivals_do_not_change_selection(svc):
    signal(svc)
    arrivals = []

    class Annotated(Extractor):
        def extract_lessons(self, signals):
            signals[0]["detail"] = "extractor-local annotation"
            arrivals.append(signal(svc, "later outcome"))
            return self.claims

    assert svc.synthesize_lessons(Annotated([claim()]))["lessons"] == 1
    assert pending(svc) == arrivals


def test_unknown_partial_rule_coverage_leaves_route_pending(svc):
    ids = [signal(svc, "A", "rule:git"), signal(svc, "B", "rule:git")]

    class Partial(Extractor):
        def extract_rules(self, signals):
            return [dict(task="A", aspect="rule", lesson="Keep rollback tag")]

    result = svc.synthesize_lessons(Partial([]))
    assert result["lessons"] == 0
    assert "identify every unhandled" in result["error"]
    assert pending(svc) == ids
    assert durable_values(svc) == []


def test_file_mode_still_skips_synthesis(tmp_path):
    from pseudolife_memory.service import MemoryService

    service = MemoryService(data_dir=tmp_path)
    assert service.synthesize_lessons(Extractor([claim()])) == {
        "signals": 0, "lessons": 0, "skipped": "no-storage"}


def test_export_import_preserves_committed_and_pending_partition(svc, tmp_path, pg_conn, pg_url):
    from pseudolife_memory.storage.schema import BENCH_RESET_TABLES, SCHEMA_META_VERSION
    from pseudolife_memory.transfer_cli import perform_export, perform_import

    pending_id = signal(svc)
    signal(svc, "release", "rule:git")

    class Mixed(Extractor):
        def extract_rules(self, signals):
            return [dict(task="release", aspect="rule", lesson="Check health", about="git")]

    assert svc.synthesize_lessons(Mixed([]))["lessons"] == 1
    archive = tmp_path / "lesson-batch.zip"
    perform_export(pg_url, archive)
    svc._storage.close()
    # Both connections belong to this test's private database. Empty it and
    # import the snapshot, with only the inert fixture connection left open.
    pg_conn.execute("TRUNCATE " + ", ".join(BENCH_RESET_TABLES)
                    + " RESTART IDENTITY CASCADE")
    pg_conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb)",
                    (str(SCHEMA_META_VERSION),))
    pg_conn.commit()
    perform_import(pg_url, archive, force=True)
    restarted = make_service(tmp_path / "imported", pg_url)
    try:
        assert pending(restarted) == [pending_id]
        assert durable_values(restarted) == ["Check health"]
        assert [r.value for r in restarted._lessons.records] == ["Check health"]
        assert restarted.synthesize_lessons(Extractor([claim()]))["lessons"] == 1
        assert pending(restarted) == []
        assert sorted(durable_values(restarted)) == ["Check health", "Keep a rollback tag"]
    finally:
        restarted._storage.close()
