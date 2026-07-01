"""LessonStore — slot logic, outcome/polarity, supersession, search, forget."""

import torch

from pseudolife_memory.memory.lessons import LessonRecord, LessonStore


def _emb(*xs):
    return torch.tensor(list(xs), dtype=torch.float32)


def test_insert_and_lookup_slot_normalised():
    s = LessonStore()
    action, rec = s.write_fact(
        "Deploy Engine to Host", "approach", "use tar --no-same-owner",
        outcome="success", confidence=0.8, origin="action",
        provenance={"ep-1", "sig-3"},
    )
    assert action == "inserted"
    assert rec.outcome == "success" and rec.polarity == "+"
    # slot identity normalises separators + case
    got = s.lookup("deploy-engine-to-host", "Approach")
    assert got is not None and got.value == "use tar --no-same-owner"
    assert got.provenance == {"ep-1", "sig-3"}


def test_negative_dead_end_is_first_class():
    s = LessonStore()
    _, rec = s.write_fact(
        "deploy engine to host", "pitfall", "tar --same-owner aborts on chown",
        outcome="failure", polarity="-", confidence=0.9,
    )
    assert rec.is_negative
    cur = s.current_records()
    assert len(cur) == 1 and cur[0].polarity == "-" and cur[0].outcome == "failure"


def test_confirm_merges_provenance_and_lifts_confidence():
    s = LessonStore()
    t0 = 1000.0
    s.write_fact("t", "a", "v1", confidence=0.6, provenance={"ep-1"}, now=t0)
    action, rec = s.write_fact(
        "t", "a", "v1", confidence=0.85, provenance={"ep-2"},
        support={"action"}, now=t0 + 500,
    )
    assert action == "confirmed"
    assert rec.last_confirmed == t0 + 500
    assert rec.confidence == 0.85
    assert rec.provenance == {"ep-1", "ep-2"}
    assert rec.support == {"action"}
    assert len(s.current_records()) == 1


def test_newer_lesson_supersedes():
    s = LessonStore()
    s.write_fact("t", "approach", "approach-A", now=1000.0)
    action, rec = s.write_fact("t", "approach", "approach-B", now=2000.0)
    assert action == "superseded"
    assert s.lookup("t", "approach").value == "approach-B"
    assert rec.supersedes_value == "approach-A"
    cur = s.current_records()
    assert len(cur) == 1 and cur[0].value == "approach-B"
    assert any(r.status == "superseded" and r.value == "approach-A" for r in s.records)


def test_stale_hlc_write_does_not_clobber_newer_lesson():
    """HLC is the ordering authority (immune to wall-clock jumps/replays),
    mirroring the personal cortex's _should_supersede: a write carrying an
    OLDER hlc than the current record must not overwrite it, even though it
    arrives later in wall-clock time. Dormant under the shipped single-writer
    (every write gets a fresh monotonic tick); this exercises the multi-writer
    out-of-order case directly by passing an explicit older hlc."""
    s = LessonStore()
    s.write_fact("t", "approach", "approach-A", now=1000.0, hlc=(5, 0))
    action, rec = s.write_fact("t", "approach", "approach-B", now=2000.0, hlc=(3, 0))
    assert action == "stale"
    assert s.lookup("t", "approach").value == "approach-A"


def test_search_ranks_by_cosine():
    s = LessonStore()
    s.write_fact("t1", "a", "v1", embedding=_emb(1.0, 0.0), now=1.0)
    s.write_fact("t2", "a", "v2", embedding=_emb(0.0, 1.0), now=2.0)
    hits = s.search(_emb(0.9, 0.1), top_k=2)
    assert hits[0][0].value == "v1" and hits[0][1] > hits[1][1]


def test_forget_removes_slot():
    s = LessonStore()
    s.write_fact("t", "approach", "v")
    s.write_fact("t", "pitfall", "w")
    assert s.forget("t", "approach") == 1
    assert s.lookup("t", "approach") is None
    assert s.lookup("t", "pitfall") is not None
    assert s.forget("t") == 1  # remaining aspect


def test_stats_counts_negatives():
    s = LessonStore()
    s.write_fact("t", "approach", "v", polarity="+")
    s.write_fact("t", "pitfall", "w", polarity="-")
    st = s.stats()
    assert st["current"] == 2 and st["negative"] == 1 and st["slots"] == 2


def test_unknown_outcome_defaults_to_success():
    s = LessonStore()
    _, rec = s.write_fact("t", "a", "v", outcome="bogus")
    assert rec.outcome == "success"


def test_record_key_property():
    r = LessonRecord(entity="Deploy X", attribute="Tool Choice", value="v")
    assert r.key == ("deploy-x", "tool-choice")
