"""Procedural / outcome memory — service-level integration (PG-backed).

Builds a real MemoryService against the throwaway test DB (loads the embedder
offline). Skips cleanly without a PG server.
"""

from __future__ import annotations

import tempfile

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


class StubExtractor:
    """Declarative extractor that yields nothing; synthesises canned lessons."""

    def __init__(self, lessons=None):
        self._lessons = lessons or []

    def extract(self, texts, vocab):
        return []

    def extract_lessons(self, signals):
        return list(self._lessons)


class NoLessonExtractor:
    """Has no extract_lessons — models the NoOpExtractor / plain regex floor."""

    def extract(self, texts, vocab):
        return []


@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def _lesson(task="deploy engine to host", aspect="approach",
            lesson="use tar --no-same-owner", **over):
    c = {"task": task, "aspect": aspect, "lesson": lesson, "about": "tar",
         "polarity": "+", "outcome": "success", "confidence": 0.7}
    c.update(over)
    return c


def test_record_outcome_writes_signal_not_lesson(svc):
    res = svc.record_outcome("deploy engine to host", "failure",
                             about="tar --same-owner",
                             detail="chown errors aborted extract", polarity="-")
    assert res["recorded"] and res["outcome"] == "failure"
    # No lesson written by the signal alone (single-writer).
    assert svc.lessons_dump()["count"] == 0
    # The signal is pending in storage.
    pend = svc._storage.pending_signals()
    assert len(pend) == 1 and pend[0]["task"] == "deploy engine to host"


def test_synthesize_writes_lessons_edges_and_consumes_signals(svc):
    svc.record_outcome("deploy engine to host", "success", about="tar")
    svc.record_outcome("deploy engine to host", "failure",
                       about="tar --same-owner", polarity="-")
    ext = StubExtractor([
        _lesson(),
        _lesson(aspect="pitfall", lesson="tar --same-owner aborts on chown",
                about="tar --same-owner", polarity="-", outcome="failure"),
    ])
    rep = svc.synthesize_lessons(ext)
    assert rep["signals"] == 2 and rep["lessons"] == 2

    dump = svc.lessons_dump()
    assert dump["count"] == 2
    pol = {d["aspect"]: d["polarity"] for d in dump["entries"]}
    assert pol["approach"] == "+" and pol["pitfall"] == "-"

    # Signals consumed — a second synthesis is a no-op.
    assert svc._storage.pending_signals() == []
    assert svc.synthesize_lessons(ext)["lessons"] == 0

    # Graph edges: task-type entity now prefers/avoids the tools.
    nb = svc.graph_neighborhood("deploy engine to host", depth=1)
    assert nb["found"] is True
    rels = {(e["relation"], e["dst"]) for e in nb["edges"]}
    assert ("prefers", "tar") in rels
    assert ("avoids", "tar --same-owner") in rels


def test_empty_synthesis_leaves_signals_pending(svc):
    """2026-07-02 review fix: a valid-but-empty extraction (or a batch where
    every lesson_write failed) must not drain the signal queue — outcome
    signals are the only feeder for procedural memory, so consuming them
    with nothing written silently loses them. Leave them for the next sweep;
    signal retention pruning bounds the retry window."""
    svc.record_outcome("some task", "failure", about="thing", polarity="-")
    rep = svc.synthesize_lessons(StubExtractor([]))
    assert rep["lessons"] == 0
    assert len(svc._storage.pending_signals()) == 1  # retried next sweep


def test_no_extractor_leaves_signals_pending(svc):
    svc.record_outcome("some task", "success", about="thing")
    rep = svc.synthesize_lessons(NoLessonExtractor())
    assert rep["lessons"] == 0 and rep.get("skipped") == "no-extractor"
    assert len(svc._storage.pending_signals()) == 1  # untouched


def test_correction_autotag_on_user_supersede(svc):
    svc.cortex_write("server", "port", "8080", support="user")
    svc.cortex_write("server", "port", "9090", support="user")  # user correction
    pend = svc._storage.pending_signals()
    corr = [s for s in pend if s["outcome"] == "correction"]
    assert len(corr) == 1
    assert corr[0]["about"] == "server"
    assert "8080" in corr[0]["detail"] and "9090" in corr[0]["detail"]


def test_agent_supersede_does_not_autotag(svc):
    # Agent/dream-tier supersession must NOT emit a correction (no feedback loop).
    svc.cortex_write("server", "port", "8080", support="agent")
    svc.cortex_write("server", "port", "9090", support="agent")
    pend = svc._storage.pending_signals()
    assert [s for s in pend if s["outcome"] == "correction"] == []


def test_lesson_search_returns_polarity_and_outcome(svc):
    svc.record_outcome("db migration", "failure", about="online ALTER")
    svc.synthesize_lessons(StubExtractor([
        _lesson(task="db migration", aspect="pitfall",
                lesson="online ALTER locks the table under load",
                about="online ALTER", polarity="-", outcome="failure"),
    ]))
    hits = svc.lesson_search("how should I run a database migration", top_k=5)
    assert hits["count"] >= 1
    top = hits["entries"][0]
    assert top["polarity"] == "-" and top["outcome"] == "failure"
    assert top["about"] == "online ALTER"


def test_dream_run_includes_lesson_synthesis(svc):
    svc.record_outcome("deploy engine to host", "success", about="tar")
    out = svc.dream_run(StubExtractor([_lesson()]))
    assert "lessons" in out
    assert out["lessons"]["lessons"] == 1
    assert svc.lessons_dump()["count"] == 1


def test_synthesized_lesson_valid_time_is_signal_event_time(svc):
    """Bitemporal: a lesson's valid_time (when the knowledge became true) is the
    contributing signal's created_at, NOT the dream's write time (tx_time)."""
    import time

    svc.lessons_dump()  # force lazy init so _storage exists
    event_t = time.time() - 100.0  # observed ~100s ago; recent enough to survive prune
    svc._storage.add_signal("deploy engine to host", "success",
                            about="tar", origin="action", now=event_t)
    rep = svc.synthesize_lessons(StubExtractor([_lesson()]))
    assert rep["lessons"] == 1
    rec = svc._lessons.lookup("deploy engine to host", "approach")
    assert rec is not None
    assert abs(rec.valid_time - event_t) < 1e-6      # event time = signal created_at
    assert rec.tx_time is not None and rec.tx_time > rec.valid_time  # written later
    assert rec.valid_time != rec.tx_time             # two distinct anchors


def test_cortex_write_valid_time_defaults_to_tx_time(svc):
    """A plain canonical write has no separate event time → valid_time == tx_time."""
    svc.cortex_write("widget", "color", "blue", support="user")
    rec = svc._cortex.lookup("widget", "color")
    assert rec is not None
    assert rec.valid_time == rec.tx_time


def test_lesson_forget(svc):
    svc.synthesize_lessons(StubExtractor([_lesson()])) if False else None
    svc.record_outcome("t", "success", about="x")
    svc.synthesize_lessons(StubExtractor([_lesson(task="t", about="x")]))
    assert svc.lessons_dump()["count"] == 1
    rem = svc.lesson_forget("t")
    assert rem["removed"] == 1
    assert svc.lessons_dump()["count"] == 0


def test_lessons_flag_re_verify_when_about_facts_changed(svc):
    svc.lesson_write("deploy engine", "approach", "use tar --no-same-owner",
                     about="engine-host", now=100.0)
    svc.cortex_write("engine-host", "os", "ubuntu-24")  # later churn
    got = svc.lesson_search("deploy engine")
    row = got["entries"][0]
    assert row["re_verify"] is True
    assert "engine-host" in row["re_verify_reason"]


def test_lessons_unflagged_when_about_unresolvable_or_quiet(svc):
    svc.lesson_write("random task", "approach", "do the thing",
                     about="totally-unknown-thing", now=100.0)
    got = svc.lessons_dump()
    row = next(r for r in got["entries"] if r["task"] == "random task")
    assert "re_verify" not in row


def test_lessons_unflagged_when_lesson_newer_than_facts(svc):
    svc.cortex_write("box", "ip", "10.0.0.5")
    svc.lesson_write("manage box", "approach", "ssh via ip", about="box")
    got = svc.lessons_dump()
    row = next(r for r in got["entries"] if r["task"] == "manage box")
    assert "re_verify" not in row
