"""Offline guards for ``evals/coordination_checkin_bench.py``: no model.

The bench spends tokens on one decision per run, so the scenario set, the
decision parser, the scoring rule and the artifact rules are pinned here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals import coordination_checkin_bench as cb
from evals import coordination_checkin_scenarios as fx

# The check-in as master served it at e1dc36b9 (2026-09-27), before the
# "when to send" rules: the bench's ``old`` arm must keep measuring against
# that text, not whatever the constant later becomes.
OLD_SHA256 = "d949883495089d3d9ad620765151632d653b52a3a17c1a2a34fee614388fe233"

# Words a situation must not use: they name the act under test.
_CUES = ("message", "send", "tell", "notify", "broadcast", "mail")


def test_scenario_set_is_four_personas_by_five_rules_by_two_expectations():
    assert len(fx.SCENARIOS) == 40
    assert len({s.id for s in fx.SCENARIOS}) == 40
    assert set(fx.PERSONAS) == {"lab", "agency", "data", "solo"}
    assert set(fx.RULES) == {"status_vs_message", "shared_resource", "host_breakage",
                             "waiting", "status_true"}
    for persona in fx.PERSONAS:
        for rule in fx.RULES:
            expects = {s.expect for s in fx.SCENARIOS if s.persona == persona and s.rule == rule}
            assert expects == {"send", "no_send"}, (persona, rule)


def test_situations_never_name_the_act_and_send_scenarios_name_a_recipient():
    for s in fx.SCENARIOS:
        text = s.situation.lower()
        for cue in _CUES:
            assert cue not in text, (s.id, cue)
        labels = {p.label for p in fx.PERSONAS[s.persona].peers}
        if s.expect == "send":
            assert s.to == "all" or s.to in labels, s.id
        else:
            assert s.to is None, s.id
    # Rule 3 goes to everyone; every other rule to a named peer.
    for s in fx.SCENARIOS:
        if s.expect == "send":
            assert (s.to == "all") == (s.rule == "host_breakage"), s.id


def test_board_overrides_name_known_peers_and_reach_the_prompt():
    """A send situation states only what happened; the peer it matters to is
    found on the board (2026-09-28: situations that named the peer put every
    arm, no check-in included, at ceiling)."""
    for s in fx.SCENARIOS:
        labels = {p.label for p in fx.PERSONAS[s.persona].peers}
        assert {label for label, _ in s.board} <= labels, s.id
        if s.expect == "send" and s.to != "all" and s.rule in ("status_vs_message", "waiting"):
            assert s.to not in s.situation, s.id
    sc = fx.scenario("solo-waiting-send")
    text = cb.build_prompt(sc, "")
    assert "codex-session: task = refactoring the auth module; status = waiting for the " \
           "test suite to be free" in text
    assert fx.PERSONAS["solo"].peers[0].status not in text
    assert len(cb.scenario_digest()) == 12


def test_arm_files_add_ablation_texts_without_shadowing_built_ins(tmp_path):
    text = tmp_path / "cut.txt"
    text.write_text("  ABLATION TEXT\n", encoding="utf-8")
    extra = cb.read_arm_files([f"cut={text}"])
    arms = cb.parse_arms("new,cut,cut@aa", extra)
    assert [a.text for a in arms][1:] == ["ABLATION TEXT", "ABLATION TEXT"]
    for bad in ("old", "x@y"):
        with pytest.raises(SystemExit):
            cb.parse_arms("new", {bad: "t"})
    with pytest.raises(SystemExit):
        cb.read_arm_files(["no-equals-sign"])


def test_both_frames_ask_for_the_same_decision():
    sc = fx.scenario("lab-waiting-send")
    board, task = cb.build_prompt(sc, "X", "board"), cb.build_prompt(sc, "X", "task")
    assert "Decide what you do on the board right now" in board
    assert "What is your next step?" in task and '"next"' in task
    for text in (board, task):
        assert '"action": "message" | "status" | "none"' in text
    with pytest.raises(SystemExit):
        cb.build_prompt(sc, "X", "chat")


def test_arms_are_the_old_text_the_served_text_and_nothing():
    from pseudolife_memory.coordination import CHECKIN_TEXT
    arms = cb.parse_arms("none,old,new,old@aa")
    by = {a.label: a for a in arms}
    assert by["none"].text == ""
    assert by["new"].text == CHECKIN_TEXT
    assert by["old"].text == fx.OLD_CHECKIN_TEXT == by["old@aa"].text
    assert hashlib.sha256(fx.OLD_CHECKIN_TEXT.encode("utf-8")).hexdigest() == OLD_SHA256
    assert "a status line is not a queue" not in fx.OLD_CHECKIN_TEXT
    assert "a status line is not a queue" in by["new"].text
    with pytest.raises(SystemExit):
        cb.parse_arms("old,old")
    with pytest.raises(SystemExit):
        cb.parse_arms("full")


def test_prompt_carries_the_arm_text_the_board_and_the_answer_shape():
    sc = fx.scenario("lab-shared_resource-send")
    text = cb.build_prompt(sc, "RULES SENTINEL")
    assert "RULES SENTINEL" in text
    assert sc.situation in text and sc.status in text
    for p in fx.PERSONAS["lab"].peers:
        assert p.label in text and p.status in text
    assert '"action": "message" | "status" | "none"' in text
    assert "(none)" in cb.build_prompt(sc, "")
    # The prompt itself never hints at the answer beyond the arm's text.
    for cue in ("you should message", "must message", "always message"):
        assert cue not in cb.build_prompt(sc, "").lower()


@pytest.mark.parametrize("raw,expected", [
    ('{"action": "message", "to": ["ana-agent"], "why": "x"}',
     {"action": "message", "to": ["ana-agent"], "why": "x"}),
    ('```json\n{"action": "STATUS", "to": [], "why": "y"}\n```',
     {"action": "status", "to": [], "why": "y"}),
    ('Sure: {"action": "none", "to": "all"}', {"action": "none", "to": ["all"], "why": ""}),
    ('{"action": "ping"}', None),
    ("not json", None),
    (None, None),
])
def test_decisions_parse_from_bare_or_fenced_json(raw, expected):
    assert cb.parse_decision(raw) == expected


def test_scoring_send_and_no_send_and_addressing():
    send = fx.scenario("lab-shared_resource-send")          # to ana-agent
    hold = fx.scenario("lab-shared_resource-no_send")
    every = fx.scenario("data-host_breakage-send")           # to all
    assert cb.score(send, {"action": "message", "to": ["ana-agent"]}) == {
        "correct": 1.0, "addressed": 1.0, "action": "message"}
    assert cb.score(send, {"action": "message", "to": ["ben-agent"]})["addressed"] == 0.0
    assert cb.score(send, {"action": "status", "to": []}) == {
        "correct": 0.0, "addressed": 0.0, "action": "status"}
    assert cb.score(hold, {"action": "status", "to": []}) == {
        "correct": 1.0, "addressed": None, "action": "status"}
    assert cb.score(hold, {"action": "message", "to": ["ana-agent"]})["correct"] == 0.0
    assert cb.score(every, {"action": "message", "to": ["all"]})["addressed"] == 1.0
    assert cb.score(every, {"action": "message",
                            "to": ["etl-agent", "report-agent"]})["addressed"] == 1.0
    assert cb.score(every, {"action": "message", "to": ["etl-agent"]})["addressed"] == 0.0
    assert cb.score(send, None) == {"correct": 0.0, "addressed": 0.0, "action": None}


def _rec(arm, sid, rep, correct, action="message"):
    sc = fx.scenario(sid)
    return {"run_id": f"t-{arm}-{sid}-r{rep}", "arm": arm, "scenario": sid, "replicate": rep,
            "persona": sc.persona, "rule": sc.rule, "expect": sc.expect,
            "decision": {"action": action, "to": [], "why": ""}, "errors": [],
            "timed_out": False, "grade": {"correct": correct, "addressed": None,
                                          "action": action}, "cost": {"usd": 0.01}}


def test_paired_deltas_are_per_rule_and_split_by_expectation():
    recs = []
    for sid in fx.SCENARIO_IDS:
        sc = fx.scenario(sid)
        for rep in range(2):
            recs.append(_rec("old", sid, rep, 0.0 if sc.expect == "send" else 1.0))
            # new fixes every send scenario of rule 1, nothing else
            fixed = sc.rule == "status_vs_message" and sc.expect == "send"
            recs.append(_rec("new", sid, rep, 1.0 if (fixed or sc.expect == "no_send") else 0.0))
    comp = cb.paired(recs, "old", "new")
    assert comp["pairs"] == 80 and comp["scenarios"] == 40
    assert comp["per_rule"]["status_vs_message"]["send"] == pytest.approx(1.0)
    assert comp["per_rule"]["status_vs_message"]["no_send"] == pytest.approx(0.0)
    assert comp["per_rule"]["waiting"]["delta"] == pytest.approx(0.0)
    assert comp["delta"] == pytest.approx(8 / 80)
    s = cb.summarize(recs, ["old", "new"])
    assert s["per_arm"]["old"]["send_recall"] == 0.0
    assert s["per_arm"]["new"]["per_rule"]["status_vs_message"] == pytest.approx(1.0)
    assert s["per_arm"]["new"]["no_send_specificity"] == 1.0


def test_invalid_runs_are_left_out_of_the_pairing():
    recs = [_rec("old", "lab-waiting-send", 0, 0.0), _rec("new", "lab-waiting-send", 0, 1.0)]
    recs[1]["decision"] = None
    assert cb.paired(recs, "old", "new")["pairs"] == 0
    assert cb.summarize(recs, ["old", "new"])["invalid_runs"] == 1


def test_artifact_names_arm_texts_and_the_aa_noise(tmp_path):
    arms = cb.parse_arms("old,new,new@aa")
    recs = []
    for sid in fx.SCENARIO_IDS[:8]:
        for arm in ("old", "new", "new@aa"):
            recs.append(_rec(arm, sid, 0, 1.0))

    class Args:
        model, effort, replicates, seed = "m", "e", 1, 1

    art = cb.build_artifact(recs, arms, Args, "t")
    assert [a["label"] for a in art["arms"]] == ["old", "new", "new@aa"]
    assert art["arms"][0]["text_sha256"] == OLD_SHA256
    assert art["aa_noise"] == pytest.approx(0.0)
    assert "new - old" in art["comparisons"]
    assert cb.render(art).startswith("coordination check-in bench t")


def test_haiku_is_refused_and_artifacts_are_never_overwritten(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="Haiku"):
        cb.main(["run", "--tag", "x", "--model", "claude-haiku-4-5"])
    out = tmp_path / "artifact.json"
    out.write_text("{}")

    class Args:
        tag, work_root, arms, scenarios, replicates, seed = "t", tmp_path / "w", "old", "all", 1, 1
        model, effort, parallel, run_timeout = "m", "e", 1, 1.0
        out = tmp_path / "artifact.json"

    monkeypatch.setattr(cb.mb, "check_work_root", lambda p: Path(p))
    with pytest.raises(SystemExit, match="single-use"):
        cb.Bench(Args).run()
