"""Offline supersession audits preserve evidence and separate replay from history."""
from __future__ import annotations

import copy
import json

import pytest

from evals.audit_supersessions import audit_snapshot, main


def _entry(id, text, *, source="project", embedding=None, replacement=None,
           slots=None):
    return {
        "id": id, "text": text, "source": source,
        "embedding": [1.0, 0.0] if embedding is None else embedding,
        "timestamp": float(id),
        "superseded_at": 10.0 if replacement is not None else None,
        "superseded_by_text": replacement,
        "slots": slots or [],
    }


def _snapshot(*entries):
    return {"format_version": 1, "metadata": {"test_fixture": True},
            "entries": list(entries)}


def test_replays_actual_detector_on_cpu_copies_without_mutating_snapshot(monkeypatch):
    from pseudolife_memory.memory import contradiction

    new = "the gateway is not ready"
    snapshot = _snapshot(_entry(1, "the gateway is ready", replacement=new),
                         _entry(2, new, source="status"))
    before = copy.deepcopy(snapshot)
    actual = contradiction.detect_contradictions
    calls = []

    def checked(text, vector, entries, **kwargs):
        assert kwargs["device"] == "cpu"
        assert kwargs["nli_scorer"] is None
        assert entries[0].superseded_at is None
        assert vector.device.type == entries[0].embedding.device.type == "cpu"
        calls.append(text)
        return actual(text, vector, entries, **kwargs)

    monkeypatch.setattr(contradiction, "detect_contradictions", checked)
    report = audit_snapshot(snapshot, sample_per_stratum=2, seed=7)

    assert calls == [new]
    assert snapshot == before
    pair = report["pairs"][0]
    assert pair["resolution"] == "unique_exact_text"
    assert pair["superseder_id"] == 2
    assert pair["current_detector_fires"] is True
    assert pair["inferred_first_path"] == "negation_asymmetry"
    assert pair["path_explanation_consistent"] is True
    assert pair["source_relation"] == "cross_source"
    assert pair["source_group"] == "status"
    assert report["summary"]["historical_cause_proven"] is False
    assert report["summary"]["nli_replayed"] is False


def test_missing_and_ambiguous_superseders_are_not_silently_resolved():
    snapshot = _snapshot(
        _entry(1, "first note", replacement="absent replacement"),
        _entry(2, "second note", replacement="repeated replacement"),
        _entry(3, "repeated replacement"),
        _entry(4, "repeated replacement", source="digest"),
    )
    report = audit_snapshot(snapshot)
    by_id = {p["entry_id"]: p for p in report["pairs"]}
    assert by_id[1]["resolution"] == "missing_exact_text"
    assert by_id[2]["resolution"] == "ambiguous_exact_text"
    assert by_id[2]["candidate_superseder_ids"] == [3, 4]
    assert all(p["current_detector_fires"] is None for p in by_id.values())
    assert report["summary"]["replayed_pairs"] == 0


def test_historical_supersession_can_fail_to_replay_without_becoming_a_false_positive_label():
    snapshot = _snapshot(
        _entry(1, "orchard fruit harvest", embedding=[1.0, 0.0],
               replacement="submarine acoustic survey"),
        _entry(2, "submarine acoustic survey", embedding=[0.0, 1.0]),
    )
    report = audit_snapshot(snapshot)
    pair = report["pairs"][0]
    assert pair["current_detector_fires"] is False
    assert pair["inferred_first_path"] is None
    assert report["summary"]["detector_does_not_fire"] == 1
    assert "false_positive" not in pair


def test_persisted_slots_reach_slot_identity_path_even_below_cosine_floors():
    new = "the service moved its host"
    snapshot = _snapshot(
        _entry(1, "old service location", replacement=new,
               slots=[["service", "host", "host-a", "+"]]),
        _entry(2, new, embedding=[0.0, 1.0],
               slots=[["service", "host", "host-b", "+"]]),
    )
    pair = audit_snapshot(snapshot)["pairs"][0]
    assert pair["current_detector_fires"] is True
    assert pair["inferred_first_path"] == "slot_identity"


@pytest.mark.parametrize(("old", "new", "embedding", "path"), [
    ("I have an RTX 4090", "I have an RTX 5090", [1.0, 0.0],
     "affirmative_replacement"),
    ("I have a cat named Jacque", "I gave away Jacque last week", [0.4, 0.916515139],
     "state_transition_slot"),
])
def test_non_negation_paths_are_explained_using_current_helpers(old, new, embedding, path):
    pair = audit_snapshot(_snapshot(
        _entry(1, old, replacement=new),
        _entry(2, new, embedding=embedding)))["pairs"][0]
    assert pair["current_detector_fires"] is True
    assert pair["inferred_first_path"] == path
    assert pair["path_explanation_consistent"] is True


@pytest.mark.parametrize("embedding", [[], [0.0, 0.0], [float("nan"), 0.0],
                                        [1.0, 0.0, 0.0]])
def test_unusable_vectors_are_reported_without_fabricating_replay(embedding):
    snapshot = _snapshot(_entry(1, "old", replacement="new"),
                         _entry(2, "new", embedding=embedding))
    report = audit_snapshot(snapshot)
    pair = report["pairs"][0]
    assert pair["resolution"] == "unique_exact_text"
    assert pair["replay_error"]
    assert pair["current_detector_fires"] is None
    assert report["summary"]["not_replayed_pairs"] == 1
    assert sum(report["summary"]["replay_errors"].values()) == 1


def test_sampling_is_order_independent_stratified_and_blind():
    entries = []
    for i in range(1, 9):
        source = "status" if i < 5 else "digest"
        later = f"the gateway is not ready for case {i}"
        entries.extend([_entry(i, f"the gateway is ready for case {i}",
                               source=source, replacement=later),
                        _entry(100 + i, later, source=source)])
    snapshot = _snapshot(*entries)
    a = audit_snapshot(snapshot, sample_per_stratum=2, seed=42)
    b = audit_snapshot(_snapshot(*reversed(entries)), sample_per_stratum=2, seed=42)
    assert a["sample"] == b["sample"]
    assert a["sample_key"] == b["sample_key"]
    assert len(a["sample"]) == 4
    assert {r["source_group"] for r in a["sample_key"]} == {"status", "digest"}
    for row in a["sample"]:
        assert set(row) == {"sample_id", "earlier_text", "replacement_text",
                            "label", "notes"}
        assert row["label"] is None
    assert {r["sample_id"] for r in a["sample"]} == {
        r["sample_id"] for r in a["sample_key"]}


@pytest.mark.parametrize("replacement", [None, ""])
def test_sampling_reports_eligible_denominator_and_keeps_missing_links_unknown(replacement):
    excluded = _entry(1, "a note without replacement text", source="status")
    excluded["superseded_at"] = 10.0
    excluded["superseded_by_text"] = replacement
    available = _entry(2, "a note with replacement text", source="status",
                       replacement="replacement absent from the snapshot")
    report = audit_snapshot(_snapshot(excluded, available), sample_per_stratum=2)
    summary = report["summary"]
    stratum = summary["strata"]["unknown/status"]

    assert stratum["recorded_supersessions"] == 2
    assert stratum["sampling"] == {"eligible": 1, "excluded": 1, "selected": 1}
    assert summary["sampling"]["eligible"] == 1
    assert summary["sampling"]["excluded"] == 1
    assert summary["sampling"]["selected"] == 1
    assert len(report["sample"]) == 1
    assert report["sample"][0]["label"] is None
    key = report["sample_key"][0]
    assert key["resolution"] == "missing_exact_text"
    assert key["source_relation"] == "unknown"
    assert key["current_detector_fires"] is None


def test_finite_components_with_overflowing_norm_are_not_replayed():
    snapshot = _snapshot(
        _entry(1, "gateway ready", replacement="gateway not ready"),
        _entry(2, "gateway not ready", embedding=[1e30, 0.0]),
    )
    report = audit_snapshot(snapshot)
    pair = report["pairs"][0]
    assert pair["current_detector_fires"] is None
    assert pair["replay_error"] == "nonfinite_or_zero_embedding"
    assert report["summary"]["replayed_pairs"] == 0
    assert report["summary"]["replay_errors"] == {"nonfinite_or_zero_embedding": 1}


def test_explanation_disagreement_is_visible_instead_of_replacing_detector_verdict(monkeypatch):
    from pseudolife_memory.memory import contradiction

    monkeypatch.setattr(contradiction, "detect_contradictions", lambda *a, **kw: [])
    snapshot = _snapshot(_entry(1, "gateway ready", replacement="gateway not ready"),
                         _entry(2, "gateway not ready"))
    report = audit_snapshot(snapshot)
    assert report["pairs"][0]["current_detector_fires"] is False
    assert report["pairs"][0]["path_explanation_consistent"] is False
    assert report["summary"]["path_explanation_mismatches"] == 1


def test_cli_persists_private_outputs_and_refuses_overwrite_or_repository_output(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot(
        _entry(1, "gateway ready", replacement="gateway not ready"),
        _entry(2, "gateway not ready"))), encoding="utf-8")
    output = tmp_path / "audit"
    args = ["--snapshot", str(snapshot), "--out-dir", str(output)]
    assert main(args) == 0
    for name in ("summary.json", "pairs.json", "manual-sample.json", "sample-key.json"):
        assert json.loads((output / name).read_text(encoding="utf-8"))
    assert main(args) != 0
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert main(["--snapshot", str(snapshot), "--out-dir", str(repo / "audit")]) != 0
    assert not (repo / "audit").exists()
