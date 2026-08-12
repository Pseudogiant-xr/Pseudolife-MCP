"""Battery-integrity and scoring pins for the misleading-recall probe
(evals/misleading_recall_probe.py — the eval category ContextWeave,
arXiv:2608.04830, showed retrieval-shaped QA benches lack).

The probe's GPU run is a bench; these tests pin the deterministic parts
so a battery edit can't silently break the metric's meaning: unique
ids, both memory blocks present per item, gold/trap substring hygiene
under the scorer's gold-wins-ties ordering, and the arm-specific prompt
construction (the misleading block must appear in exactly one arm)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import misleading_recall_probe as mrp  # noqa: E402


def test_battery_ids_unique_and_kinds_valid():
    ids = [i["id"] for i in mrp.BATTERY]
    assert len(ids) == len(set(ids))
    assert {i["kind"] for i in mrp.BATTERY} <= {"fact", "lesson"}
    assert len(mrp.BATTERY) >= 10


def test_battery_items_carry_all_fields():
    for item in mrp.BATTERY:
        for key in ("evidence", "misleading", "placebo", "question",
                    "gold", "trap"):
            assert item.get(key), (item["id"], key)
        assert isinstance(item["evidence"], list) and item["evidence"]


def test_gold_never_scores_as_trap():
    """Gold-wins-ties ordering: an answer containing the gold value must
    score 'gold' even when the trap string is a substring of it (e.g.
    trap '5 minutes' inside answer '45 minutes')."""
    for item in mrp.BATTERY:
        assert mrp.score(f"the answer is {item['gold']}",
                         item["gold"], item["trap"]) == "gold", item["id"]
        # And the trap alone must actually be detectable.
        if item["gold"].casefold() not in item["trap"].casefold():
            assert mrp.score(f"it is {item['trap']}",
                             item["gold"], item["trap"]) == "trap", item["id"]


def test_trap_is_never_substring_of_gold():
    """A trap contained in the gold string could never be detected —
    such an item measures nothing."""
    for item in mrp.BATTERY:
        assert item["trap"].casefold() not in item["gold"].casefold(), \
            item["id"]


def test_prompt_injects_memory_block_per_arm_only():
    item = mrp.BATTERY[0]
    p_ev = mrp._prompt(item, "evidence")
    p_pl = mrp._prompt(item, "no_memory")
    p_mi = mrp._prompt(item, "misleading")
    p_mo = mrp._prompt(item, "memory_only")
    assert item["misleading"] in p_mi and item["misleading"] in p_mo
    assert item["misleading"] not in p_ev and item["misleading"] not in p_pl
    assert item["placebo"] in p_pl
    assert item["placebo"] not in p_ev and item["placebo"] not in p_mi
    for p in (p_ev, p_pl, p_mi):
        assert item["question"] in p
        for turn in item["evidence"]:
            assert turn in p
    # The memory_only arm is the cortex-only regime: no evidence at all.
    assert item["question"] in p_mo
    for turn in item["evidence"]:
        assert turn not in p_mo


def test_score_other_when_neither_value_appears():
    assert mrp.score("I don't know.", "eu-west-2", "eu-central-1") == "other"
    assert mrp.score("", "x", "y") == "other"
