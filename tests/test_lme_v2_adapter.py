"""Tests for evals/lme_v2_adapter.py bid->label resolution + page context, and
evals/lme_v2_smoke.py's cross-trajectory synthesis pass.

Pure-function tests only: no endpoints, no GPU, no Postgres, no data files.
Small inline accessibility-tree fixtures stand in for the 171 MB corpus.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import lme_v2_adapter as A  # noqa: E402


# --------------------------------------------------------------------------- #
# Inline fixtures — a two-state ServiceNow-style trajectory. State 1's action
# (`click('1269')`) targets a link that only exists in state 0's tree (the
# pre-navigation observation): navigation clicks are the case same-state
# resolution misses, so the adapter must resolve against the PREVIOUS state.
# --------------------------------------------------------------------------- #
TREE_HOME = (
    "RootWebArea 'Home | ServiceNow', focused\n"
    "\t[79] button 'All', clickable, visible, expanded=False\n"
    "\t[113] combobox 'Search', clickable, visible, expanded=False\n"
    "\t[1269] link 'Reports', clickable, visible\n"
    "\theading 'Welcome', visible\n"
)
TREE_REPORTS = (
    "RootWebArea 'Reports | ServiceNow', focused\n"
    "\t[79] button 'All', clickable, visible, expanded=False\n"
    "\t[400] heading 'Reports', visible\n"
    "\t[401] main 'Reports', visible\n"
    "\t[402] link 'View Problems', clickable, visible\n"
)

TRAJ = {
    "id": "fixture01",
    "domain": "enterprise",
    "environment": "workarena",
    "goal": "Reassign problems with a tag",
    "outcome": "success",
    "states": [
        {"state_index": 0, "url": "/home", "action": None,
         "thought": "I'll open the Reports module.",
         "accessibility_tree": TREE_HOME},
        {"state_index": 1, "url": "/reports", "action": "click('1269')",
         "thought": "Now I'll open Problems.",
         "accessibility_tree": TREE_REPORTS},
    ],
}


def test_parse_bid_map_reads_bidded_lines():
    m = A._parse_bid_map(TREE_HOME)
    assert m["113"].startswith("combobox 'Search'")
    assert m["1269"].startswith("link 'Reports'")
    # unbidded lines (heading 'Welcome') are not in the bid map
    assert "heading 'Welcome'" not in "".join(m.values()) or "Welcome" not in m.get("", "")


def test_resolve_bid_returns_role_and_name():
    assert A._resolve_bid("1269", TREE_HOME) == ("link", "Reports")
    assert A._resolve_bid("113", TREE_HOME) == ("combobox", "Search")
    assert A._resolve_bid("999", TREE_HOME) is None


def test_action_bid_resolves_against_previous_state_tree():
    """click('1269') on state 1 targets a link present only in state 0's tree."""
    turns = A.trajectory_to_turns(TRAJ, include_observations=True)
    # turn 0 = task frame; turn 1 = state 0; turn 2 = state 1 (has the action)
    assert len(turns) == 3
    state1_turn = turns[2]
    # The resolved click label must name the link from the PREVIOUS tree.
    assert 'clicked: link "Reports"' in state1_turn


def test_page_context_carries_title_and_headings():
    turns = A.trajectory_to_turns(TRAJ, include_observations=True)
    # state 1's turn should carry the Reports page title + heading (its own tree)
    assert "page: Reports | ServiceNow" in turns[2]
    assert 'heading: "Reports"' in turns[2]


def test_observations_off_is_unchanged_baseline():
    """Default (observations off) must not emit any resolved/page lines."""
    turns = A.trajectory_to_turns(TRAJ)
    joined = "\n".join(turns)
    assert "clicked:" not in joined
    assert "page:" not in joined
    # baseline still carries thought + action
    assert "action: click('1269')" in joined
    assert "thought: I'll open the Reports module." in joined


def test_observations_do_not_dump_raw_tree():
    """The fix must NOT reintroduce the multi-KB raw tree dump."""
    turns = A.trajectory_to_turns(TRAJ, include_observations=True)
    joined = "\n".join(turns)
    # raw tree rows like "[401] main 'Reports', visible" must not be dumped verbatim
    assert "clickable, visible, expanded=False" not in joined
    assert "observation:" not in joined


def test_observation_block_is_char_capped():
    big_tree = "RootWebArea 'Big | ServiceNow'\n" + "".join(
        f"\theading 'H{i} {'x' * 50}', visible\n" for i in range(200))
    traj = {"id": "big", "domain": "web", "environment": "e", "goal": "g",
            "outcome": "success",
            "states": [{"state_index": 0, "url": "/", "action": None,
                        "thought": "t", "accessibility_tree": big_tree}]}
    turns = A.trajectory_to_turns(traj, include_observations=True,
                                  observation_chars=300)
    # the appended observation block for the single state stays bounded
    obs_part = turns[1].split("thought: t", 1)[-1]
    assert len(obs_part) <= 400  # 300 cap + the "...[capped]" marker + slack


# --------------------------------------------------------------------------- #
# Fix C — cross-trajectory synthesis (in the smoke harness, not product code)
# --------------------------------------------------------------------------- #
def test_synthesize_procedures_prefers_success_on_conflict():
    import lme_v2_smoke as S
    # Two success trajectories agree on Reports->Problems; one failure disagrees.
    claims = [
        {"entity": "reassign problems by tag", "attribute": "modules used (in order)",
         "value": "Reports; Problems", "outcome": "success"},
        {"entity": "reassign problems by tag", "attribute": "modules used (in order)",
         "value": "Reports; Problems", "outcome": "success"},
        {"entity": "reassign problems by tag", "attribute": "modules used (in order)",
         "value": "Problems; Reports", "outcome": "failure"},
    ]
    out = S.synthesize_procedures(claims)
    assert len(out) == 1
    canon = out[0]
    assert canon["attribute"].startswith("typical workflow")
    # success majority wins the ordering
    assert canon["value"] == "Reports; Problems"
    assert canon["support"] == 2          # two success trajectories
    assert canon["conflicts"] == 1


def test_synthesize_procedures_falls_back_to_majority_without_success():
    import lme_v2_smoke as S
    claims = [
        {"entity": "task A", "attribute": "modules used (in order)",
         "value": "X; Y", "outcome": "failure"},
        {"entity": "task A", "attribute": "modules used (in order)",
         "value": "X; Y", "outcome": "failure"},
        {"entity": "task A", "attribute": "modules used (in order)",
         "value": "Y; X", "outcome": "failure"},
    ]
    out = S.synthesize_procedures(claims)
    assert len(out) == 1
    assert out[0]["value"] == "X; Y"      # majority of the failures


def test_extractor_default_system_prompt_is_byte_identical():
    """Adding the optional system_prompt arg must not change the shipped path."""
    from pseudolife_memory.memory.dream import OpenAICompatExtractor, _SYSTEM_PROMPT
    default = OpenAICompatExtractor("http://x/v1", "m")
    assert default.system_prompt == _SYSTEM_PROMPT
    custom = OpenAICompatExtractor("http://x/v1", "m", system_prompt="ZZZ")
    assert custom.system_prompt == "ZZZ"


def test_synthesize_procedures_clusters_by_task():
    import lme_v2_smoke as S
    claims = [
        {"entity": "task A", "attribute": "modules used (in order)",
         "value": "X; Y", "outcome": "success"},
        {"entity": "task B", "attribute": "modules used (in order)",
         "value": "P; Q", "outcome": "success"},
    ]
    out = S.synthesize_procedures(claims)
    assert len(out) == 2
    ents = {c["entity"] for c in out}
    assert ents == {"task A", "task B"}
