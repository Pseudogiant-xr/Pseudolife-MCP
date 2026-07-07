"""Unit tests for the pure logic in evals/distill_datagen_sonnet.py."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from distill_datagen_sonnet import (  # noqa: E402
    ingest_question, plan_questions, render_brief,
)

DATASET = [
    {   # KU question: ALL its sessions are forbidden everywhere
        "question_id": "ku_1", "question_type": "knowledge-update",
        "haystack_session_ids": ["s_shared"], "haystack_dates": ["2023/01/01"],
        "haystack_sessions": [[{"role": "user", "content": "irrelevant"}]],
    },
    {
        "question_id": "q_a", "question_type": "single-session-user",
        "haystack_session_ids": ["s_1", "s_shared", "s_2"],
        "haystack_dates": ["2023/03/02", "2023/03/01", "2023/03/03"],
        "haystack_sessions": [
            [{"role": "user", "content": "I adopted a cat named Miso."}],
            [{"role": "user", "content": "forbidden content"}],
            [{"role": "user", "content": "Actually Miso is a dog."}],
        ],
    },
    {
        "question_id": "q_b", "question_type": "single-session-user",
        "haystack_session_ids": ["s_1", "s_3"],
        "haystack_dates": ["2023/03/02", "2023/04/01"],
        "haystack_sessions": [
            [{"role": "user", "content": "I adopted a cat named Miso."}],
            [{"role": "user", "content": "I work at Acme."}],
        ],
    },
]


def test_plan_excludes_forbidden_and_dedups_across_questions():
    plans = plan_questions(DATASET)
    ids = {p["question_id"]: [s["session_id"] for s in p["sessions"]]
           for p in plans}
    assert "ku_1" not in ids                      # KU questions never labeled
    assert ids["q_a"] == ["s_1", "s_2"]           # forbidden s_shared dropped
    assert ids["q_b"] == ["s_3"]                  # s_1 claimed by q_a (sorted order)


def test_plan_orders_sessions_chronologically():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    # s_1 dated 03/02 precedes s_2 dated 03/03 (input order had s_2 last too,
    # but s_shared 03/01 sat between them)
    assert [s["date"] for s in qa["sessions"]] == ["2023/03/02", "2023/03/03"]


def test_render_brief_contains_sessions_and_contract():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    brief = render_brief(qa, recall_prompt="RECALL-PROMPT-SENTINEL")
    assert "RECALL-PROMPT-SENTINEL" in brief
    assert brief.index("s_1") < brief.index("s_2")        # chrono order
    assert "sonnet_out/q_a.jsonl" in brief                # output contract
    assert '{"session_id"' in brief                       # row schema shown


def test_ingest_rewrites_prompt_and_recomputes_vocab():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    answers = {
        "s_1": [{"entity": "Miso", "attribute": "species", "value": "cat",
                 "confidence": 0.9, "source": 1}],
        "s_2": [{"entity": "Miso", "attribute": "species", "value": "dog",
                 "confidence": 0.9, "source": 1}],
    }
    rows = ingest_question(qa, answers)
    assert [r["id"] for r in rows] == ["q_a:s_1", "q_a:s_2"]
    from pseudolife_memory.memory.dream import _SYSTEM_PROMPT
    assert rows[0]["messages"][0]["content"] == _SYSTEM_PROMPT  # no hint yet
    # second session's hint is recomputed from claim 1, not subagent-supplied
    assert "miso.species" in rows[1]["messages"][0]["content"]
    assert rows[1]["messages"][1]["content"].startswith("[1] ")


def test_ingest_rejects_question_on_bad_claim():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    answers = {
        "s_1": [{"entity": "Miso", "attribute": "species", "value": "cat",
                 "confidence": 0.9, "source": 7}],   # citation out of range
        "s_2": [],
    }
    assert ingest_question(qa, answers) is None


def test_ingest_rejects_question_on_missing_session():
    plans = plan_questions(DATASET)
    qa = next(p for p in plans if p["question_id"] == "q_a")
    assert ingest_question(qa, {"s_1": []}) is None   # s_2 unanswered
