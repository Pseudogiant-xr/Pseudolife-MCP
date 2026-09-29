"""cross_judge_gates: the cross-model merge-gate simulation behind the
2026-09-29 judge swap. Pure file-in/file-out; no model calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import cross_judge_gates as cjg  # noqa: E402


def _vote(verdict, confidence):
    return {"verdict": verdict, "confidence": confidence}


def _arm(tmp, name, model, rows, replicates=1):
    doc = {"arms": {name: {"model": model, "replicates": replicates,
                           "queues": {"merges": {"per_row": rows}}}}}
    (tmp / f"queue-judge-ladder-t-{name}.json").write_text(json.dumps(doc))


def _setup(tmp, monkeypatch):
    panel = tmp / "panel.json"
    panel.write_text(json.dumps({"merges": [
        {"id": i, "low_differential": False} for i in (1, 2, 3, 4)]}))
    monkeypatch.setattr(cjg, "RESULTS", tmp)
    monkeypatch.setattr(cjg, "PANEL", panel)
    _arm(tmp, "first", "m1", [
        {"id": 1, "label": "reject", "votes": [_vote("reject", 0.8)]},
        {"id": 2, "label": "accept", "votes": [_vote("accept", 0.7)]},
        {"id": 3, "label": "accept", "votes": [_vote("accept", 0.7)]},
        {"id": 4, "label": "reject", "votes": [_vote("accept", 0.6)]},
    ])
    # Row 2's batch failed in the second arm (the ladder writes a null
    # vote); row 3 is missing from it entirely.
    _arm(tmp, "second", "m2", [
        {"id": 1, "label": "reject", "votes": [_vote("reject", 0.6)]},
        {"id": 2, "label": "accept", "votes": [None]},
        {"id": 4, "label": "reject", "votes": [_vote("reject", 0.9)]},
    ])


def test_gates_and_splits_skip_rows_a_failed_batch_left_empty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    out = tmp_path / "cross.json"
    assert cjg.main(["--first", "first", "--arms", "second", "--tag", "t",
                     "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    (rep,) = doc["pairs"]["second"]["replicates"]
    # Row 1: both reject at mean 0.7 -> one correct two-vote reject.
    assert rep["two_vote_reject"] == {"n": 1, "bad": 0, "bad_ids": []}
    assert rep["two_vote_accept_not_lowdiff"]["n"] == 0
    # Only row 4 is a real split; rows 2 (null vote) and 3 (missing) are not.
    assert doc["splits"]["rows"] == [4]
    assert doc["splits"]["correct_by_arm"] == {"first": 0, "second": 1}
    assert doc["calibration"]["second"] == {"reject_votes": 2,
                                            "at_or_above_reject_gate": 1}
