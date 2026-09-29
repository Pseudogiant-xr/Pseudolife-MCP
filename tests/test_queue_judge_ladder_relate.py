"""The merge-judge ladders count ``relate`` as reject-class (2026-09-30).

The sweep gates a relate vote as a reject (the pair is distinct), so every
merge metric the ladders publish must too: a relate on a row the panel
labelled reject is a correct reject, and reject vs relate is agreement, not
a split. Pure over synthetic votes; no model, no evidence pack.
"""
from __future__ import annotations

import argparse

from evals import judge_ladder as JL
from evals import merge_relate_gates as MG
from evals import queue_judge_ladder as QL


def _args():
    return argparse.Namespace(reject_gate=0.8, reject_gate_2=0.7, accept_gate=0.6)


def _row(rid, label, panel_verdict, *, relate=None, low=False):
    return {"id": rid, "label": label, "panel_verdict": panel_verdict,
            "relate": relate, "low_differential": low,
            "from": {"display": f"f{rid}"}, "into": {"display": f"i{rid}"}}


def _v(verdict, conf, relation=None):
    return {"verdict": verdict, "confidence": conf, "relation": relation}


def test_queue_ladder_scores_relate_as_reject_class():
    rows = [_row(1, "reject", "relate",
                 relate={"src": "f1", "relation": "implements", "dst": "i1"}),
            _row(2, "reject", "reject"),
            _row(3, "accept", "accept")]
    rep1 = [_v("relate", 0.9, "implements"), _v("reject", 0.9), _v("relate", 0.9, "uses")]
    rep2 = [_v("reject", 0.8), _v("relate", 0.85, "part-of"), _v("accept", 0.7)]
    out = QL.score_merges(rows, [rep1, rep2], _args())
    # Rows 1 and 2: both votes reject-class -> majority reject, correct.
    assert out["reject_precision"] == {"n": 2, "bad": 0, "precision": 1.0}
    assert out["two_vote_reject"] == {"n": 2, "bad": 0, "precision": 1.0}
    assert out["decided"] == 2          # row 3 splits relate/accept -> leave
    rel = out["relate"]
    assert rel["votes"] == 3
    assert rel["on_label"] == {"reject": 2, "accept": 1}
    assert rel["on_panel_verdict"] == {"relate": 1, "reject": 1, "accept": 1}
    assert rel["panel_relate_rows"] == {"relation_match": 1, "direction_same": 1,
                                        "direction_reversed": 0}


def test_judge_ladder_replicate_maps_relate_to_reject():
    class _Ex:
        def judge_merges(self, proposals):
            return [{"n": 1, "verdict": "relate", "confidence": 0.9,
                     "relation": "uses", "note": ""}]
    rows = [{"from": {"display": "a"}, "into": {"display": "b"},
             "reason": "t", "score": 0.9, "label": "reject"}]
    assert JL.run_replicate(_Ex(), rows, batch=8) == [("reject", 0.9)]


def _arm(name, votes_by_id, labels):
    return {"arm": name, "model": name, "replicates": 1,
            "queues": {"merges": {"per_row": [
                {"id": rid, "label": labels[rid], "votes": [v]}
                for rid, v in votes_by_id.items()]}}}


def test_cross_gates_count_relate_as_reject_class_and_file_links():
    labels = {1: "reject", 2: "reject", 3: "accept", 4: "accept"}
    panel = {"merges": [
        {"id": 1, "panel_verdict": "relate", "low_differential": False},
        {"id": 2, "panel_verdict": "reject", "low_differential": False},
        {"id": 3, "panel_verdict": "accept", "low_differential": False},
        {"id": 4, "panel_verdict": "accept", "low_differential": False}]}
    first = _arm("a", {1: _v("reject", 0.7), 2: _v("reject", 0.9),
                       3: _v("accept", 0.8), 4: _v("accept", 0.9)}, labels)
    second = _arm("b", {1: _v("relate", 0.9, "implements"), 2: _v("reject", 0.9),
                        3: _v("relate", 0.9, "uses"), 4: _v("accept", 0.7)}, labels)
    doc = MG.build(first, second, panel)
    (rep,) = doc["replicates"]
    assert rep["two_vote_reject_class"] == {
        "n": 2, "bad": 0, "bad_ids": [], "links_filed": 1,
        "links_on_panel_relate": 1}
    assert rep["two_vote_accept_not_lowdiff"] == {"n": 1, "bad": 0, "bad_ids": []}
    # accept vs relate is the only split; reject vs relate agrees.
    assert rep["splits"] == {"n": 1, "rows": [3]}
    assert doc["single_vote"]["b"] == [{"n": 3, "bad": 1, "relate": 2}]
    assert doc["vote_confidence"]["b"]["relate"] == {
        "n": 2, "mean": 0.9, "ge_reject_gate": 2, "ge_reject_gate_2": 2}
    assert doc["vote_confidence"]["a"]["reject"]["ge_reject_gate"] == 1
