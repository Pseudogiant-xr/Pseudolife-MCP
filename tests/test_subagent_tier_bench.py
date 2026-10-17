"""The subagent tier bench's keys and traps measure the answer, not the
question (review of the 2026-10-14 pilot: several keys passed on words the
prompt itself used, and a trap fired on a correct negation).

For every brief: no key regex matches the brief's own prompt text; a
fixture answer that states every fact matches every key and no trap; the
same answer with one fact removed misses exactly that key; a fixture with a
wrong value trips a trap; and a correct negation of the wrong claim trips
none. The scorer refuses an empty or misnamed results directory instead of
writing an empty table.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "evals" / "subagent_tier_bench"
spec = importlib.util.spec_from_file_location("tier_score", BENCH / "score.py")
tier_score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tier_score)

BRIEFS = {b["id"]: b for b in json.loads((BENCH / "briefs.json").read_text(encoding="utf-8"))["briefs"]}
FLAGS = re.IGNORECASE | re.DOTALL

# One sentence per key, in key order, each stating the fact the key encodes.
CORRECT = {
    "stop-hook": [
        "POLL=5 seconds.", "IDLE_POLL=30 seconds after the fast window.", "MAX_WAIT=1209540 seconds.",
        "MAX_WAKES=20 wakes.", "WAKE_WINDOW=3600 seconds.", "It reads <key>.ring.", "It reads <key>.seen.",
        "fire() exits with code 2.", "The text goes to fd 3, the original stderr.",
        "A fresh token is written to <key>.wake and still_owner compares it.",
    ],
    "digest-preview": [
        "PREVIEW_LIMIT = 5.", "PREVIEW_EXCERPT = 100.", "Line 1 is the watermark.",
        "Each line is `- {message_id} from {sender} ({sender_agent_id[:8]}, {HH:MM}): {excerpt}`.",
        "The hook prints the whole body, not truncated.",
    ],
    "multi-ack": [
        "Ids may be comma-separated.", "Or a JSON array of strings.", "MAX_PAGE = 50 bounds the batch.",
        "Several ids return {\"receipts\": [...], ...}.", "with a \"missing\" list.",
        "A single unknown id raises message_not_found.", "An ambiguous prefix raises ambiguous_message_id.",
    ],
    "lease-check": [
        "Exit 0 means free.", "Exit 1 means held.", "The --json keys are name, held, local and board.",
        "With the board unreachable and the lock free it answers free.", "The lease is delegate:<project>.",
        "A grant lasts up to 7 days.", "and at least 60 seconds.", "The lease_delegate event proves it.",
    ],
    "rust-cli-add": [
        "The shim does not use clap.", "Add the module in cli.rs.", "Copy run_lease, a tokio::main wrapper.",
        "Remove the mode from DEFERRED_MODES.", "Add the line to cli_help.txt.", "wait-mail is in cli/wait_mail.rs.",
        "The help text is byte-identical to Python's _USAGE.", "An unknown mode exits with code 2.",
    ],
    "board-guard": [
        "A subagent's payload carries agent_id.", "It denies update,", "claim,", "release,", "send", "and ack.",
        "It allows receive and list.", "It allows history.", "It fails open on a payload it cannot read.",
        "hooks.json runs bash -n first.", "PSEUDOLIFE_CODEX_HOOK=1 marks Codex.",
        "So does PLUGIN_ROOT equal to CLAUDE_PLUGIN_ROOT.", "The PowerShell path is a no-op.",
    ],
    "suite-lock": [
        "The lock is full-suite.lock.", "The queue is full-suite.queue/.", "Waiters are ordered by arrival time.",
        "PSEUDOLIFE_SUITE_LOCK=fail exits instead.",
        "PSEUDOLIFE_SUITE_LOCK=off skips the lock.", "PSEUDOLIFE_SUITE_SLOTS sets the slots,",
        "else full-suite.slots.", "full-suite.windows refuses native runs;", "PSEUDOLIFE_SUITE_WINDOWS overrides it.",
        "It sets CUDA_VISIBLE_DEVICES=-1.", "An empty string leaves torch.cuda.is_available() True because the driver reads it as unset.",
    ],
    "wake-decision": [
        "An active unparked recipient gets hinted.", "The clearer is whoever park_clear_by names as sender.", "Or park_clear_by == \"anyone\".",
        "Or clears passes _need_matches(clears, park_needs).", "Or urgent, with the sender's authority if any.", "Otherwise withheld.",
        "A done park answers parked_done to peers and can be reopened only by the maintainer or delegate.", "Unparked idle plain mail is not_needed, no_park.",
        "per recipient per hour: 20.", "urgent per sender per hour: 6.", "nightly total: 200.",
        "The shorter list needs a word of four or more letters.",
    ],
}

# A wrong claim per brief, and a correct negation of it that must not trip.
WRONG = {
    "stop-hook": ("It exits 0 to wake the session.", "It does not exit 0 to wake the session; it exits 2."),
    "digest-preview": ("PREVIEW_LIMIT = 50.", "PREVIEW_LIMIT = 5, not 50."),
    "multi-ack": ("At most 100 ids per call.", "At most 50 ids per call."),
    "lease-check": ("When the board is unreachable it returns held.",
                    "When the board is unreachable it still answers free, never held."),
    "rust-cli-add": ("The shim uses clap for dispatch.", "The shim never uses clap; dispatch is hand-written."),
    "board-guard": ("It denies receive from a subagent.", "It denies receive with ack=true only."),
    "suite-lock": ("It sets CUDA_VISIBLE_DEVICES to \"\".", "Setting CUDA_VISIBLE_DEVICES to \"\" would leave the GPU visible."),
    "wake-decision": ("Plain mail wakes an idle peer.", "Plain mail never wakes an idle peer."),
}


@pytest.mark.parametrize("brief_id", sorted(BRIEFS))
def test_no_key_matches_the_prompt_itself(brief_id):
    brief = BRIEFS[brief_id]
    echoed = [k for k in brief["keys"] if re.search(k, brief["prompt"], FLAGS)]
    assert echoed == [], f"{brief_id}: keys satisfied by the question text: {echoed}"


@pytest.mark.parametrize("brief_id", sorted(BRIEFS))
def test_a_correct_answer_matches_every_key_and_no_trap(brief_id):
    brief = BRIEFS[brief_id]
    assert len(CORRECT[brief_id]) == len(brief["keys"]), "one fixture sentence per key"
    result = tier_score.score(" ".join(CORRECT[brief_id]), brief)
    assert result["missed"] == [] and result["traps"] == []


@pytest.mark.parametrize("brief_id", sorted(BRIEFS))
def test_dropping_one_fact_misses_exactly_its_key(brief_id):
    brief = BRIEFS[brief_id]
    for i, key in enumerate(brief["keys"]):
        answer = " ".join(s for j, s in enumerate(CORRECT[brief_id]) if j != i)
        result = tier_score.score(answer, brief)
        assert key in result["missed"], f"{brief_id}: key {i} is satisfied without its sentence: {key}"


@pytest.mark.parametrize("brief_id", sorted(WRONG))
def test_a_wrong_claim_trips_a_trap_and_its_negation_does_not(brief_id):
    brief = BRIEFS[brief_id]
    wrong, negation = WRONG[brief_id]
    assert tier_score.score(wrong, brief)["traps"], f"{brief_id}: no trap for {wrong!r}"
    assert tier_score.score(negation, brief)["traps"] == [], f"{brief_id}: trap fired on {negation!r}"


def test_the_scorer_refuses_an_empty_or_misnamed_results_dir(tmp_path):
    with pytest.raises(SystemExit, match="no <brief>"):
        tier_score.load_runs(tmp_path, BRIEFS)
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="not named"):
        tier_score.load_runs(tmp_path, BRIEFS)
    (tmp_path / "notes.md").unlink()
    (tmp_path / "stop-hook.opus.1.md").write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match="no metadata"):
        tier_score.load_runs(tmp_path, BRIEFS)
    with pytest.raises(SystemExit, match="not a directory"):
        tier_score.load_runs(tmp_path / "missing", BRIEFS)


def test_the_committed_pilot_scores_and_is_scrubbed():
    runs = tier_score.load_runs(BENCH / "results", BRIEFS)
    assert len(runs) == 24
    for path in (BENCH / "results").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "C:\\Users" not in text and "/home/" not in text, path.name
