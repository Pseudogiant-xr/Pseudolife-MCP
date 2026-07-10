"""Unit tests for the pure logic in evals/distill_datagen_sonnet.py."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from distill_datagen_sonnet import (  # noqa: E402
    apply_key_map, canonicalize_claims, ingest_question, plan_questions,
    render_brief, _key_sig,
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


def _claim(entity, attribute, value, source=1):
    return {"entity": entity, "attribute": attribute, "value": value,
            "confidence": 0.9, "source": source}


def test_key_sig_merges_wording_variants():
    # reordering + suffix variants collapse to one signature
    assert _key_sig("pre-approved-loan-amount") == _key_sig("loan pre-approval amount")
    # generic filler tokens are ignored
    assert _key_sig("wake-time") == _key_sig("wake-up-time")


def test_key_sig_keeps_distinct_properties_apart():
    assert _key_sig("bedroom-color") != _key_sig("bedroom-size")


def test_canonicalize_rewrites_to_first_seen_key():
    canon = {}
    first = canonicalize_claims(
        [_claim("user", "pre-approved-loan-amount", "$400,000")], canon)
    later = canonicalize_claims(
        [_claim("user", "loan-pre-approval-amount", "$350,000")], canon)
    assert first[0]["attribute"] == "pre-approved-loan-amount"
    assert later[0]["attribute"] == "pre-approved-loan-amount"  # rewritten


def test_canonicalize_scopes_by_entity():
    canon = {}
    canonicalize_claims([_claim("alice", "team-size", "5")], canon)
    other = canonicalize_claims([_claim("bob", "team-size", "8")], canon)
    assert other[0]["attribute"] == "team-size"      # no cross-entity rewrite


def test_ingest_question_canonical_flag():
    qplan = {"question_id": "q_c", "sessions": [
        {"session_id": "sA", "date": "2023/03/02",
         "notes": ["[2023/03/02] user: pre-approved for $400k"]},
        {"session_id": "sB", "date": "2023/03/03",
         "notes": ["[2023/03/03] user: actually the pre-approval is $350k"]},
    ]}
    answers = {
        "sA": [_claim("user", "pre-approved-loan-amount", "$400,000")],
        "sB": [_claim("user", "loan-pre-approval-amount", "$350,000")],
    }
    rows = ingest_question(qplan, answers, canonical=True)
    target_b = json.loads(rows[1]["messages"][-1]["content"])["claims"][0]
    assert target_b["attribute"] == "pre-approved-loan-amount"
    # session B's vocab hint carries only the canonical key
    sys_b = rows[1]["messages"][0]["content"]
    assert "pre-approved-loan-amount" in sys_b
    assert "loan-pre-approval-amount" not in sys_b
    # flag OFF: variant key survives untouched (arm-B parity)
    rows_off = ingest_question(qplan, answers)
    target_off = json.loads(rows_off[1]["messages"][-1]["content"])["claims"][0]
    assert target_off["attribute"] == "loan-pre-approval-amount"


# --- Task 2R: --key-map controller-supplied rewrite map ---------------------

def test_apply_key_map_rewrites_attribute():
    claims = [_claim("user", "office-location", "Seattle")]
    qmap = {"user": {"office-location": "location"}}
    out = apply_key_map(claims, qmap)
    assert out[0]["attribute"] == "location"
    assert out[0]["value"] == "Seattle"
    assert out[0]["confidence"] == 0.9
    assert out[0]["source"] == 1
    assert claims[0]["attribute"] == "office-location"          # input untouched


def test_apply_key_map_scopes_by_entity():
    claims = [_claim("bob", "office-location", "Seattle")]
    qmap = {"user": {"office-location": "location"}}
    out = apply_key_map(claims, qmap)
    assert out[0]["attribute"] == "office-location"              # no cross-entity rewrite


def test_apply_key_map_normalized_lookup():
    claims = [_claim("User", "Office Location", "Seattle")]
    qmap = {"user": {"office-location": "location"}}
    out = apply_key_map(claims, qmap)
    assert out[0]["attribute"] == "location"


def test_ingest_question_keymap():
    plans = plan_questions(DATASET)
    qb = next(p for p in plans if p["question_id"] == "q_b")
    # q_b's plan is just s_3 (s_1 already claimed by q_a in sorted order)
    answers = {"s_3": [_claim("user", "office-location", "Seattle")]}
    keymap = {"q_b": {"user": {"office-location": "location"}}}
    rows = ingest_question(qb, answers, keymap=keymap)
    target = json.loads(rows[0]["messages"][-1]["content"])["claims"][0]
    assert target["attribute"] == "location"

    # a question_id NOT in the map is untouched
    qa = next(p for p in plans if p["question_id"] == "q_a")
    answers_a = {
        "s_1": [_claim("Miso", "species", "cat")],
        "s_2": [_claim("Miso", "species", "dog")],
    }
    rows_a = ingest_question(qa, answers_a, keymap=keymap)
    target_a = json.loads(rows_a[0]["messages"][-1]["content"])["claims"][0]
    assert target_a["attribute"] == "species"


def test_ingest_question_no_keymap_identity():
    plans = plan_questions(DATASET)
    qb = next(p for p in plans if p["question_id"] == "q_b")
    answers = {"s_3": [_claim("user", "office-location", "Seattle")]}
    rows_none = ingest_question(qb, answers, keymap=None)
    rows_default = ingest_question(qb, answers)
    assert rows_none == rows_default


def test_cli_accepts_key_map_flag(monkeypatch, tmp_path):
    import distill_datagen_sonnet as mod
    captured = {}
    def fake_cmd_ingest(args):
        captured["args"] = args
        return 0
    monkeypatch.setattr(mod, "_cmd_ingest", fake_cmd_ingest)
    km_path = tmp_path / "km.json"
    monkeypatch.setattr(
        sys, "argv", ["prog", "--ingest", "--key-map", str(km_path)])
    assert mod.main() == 0
    assert captured["args"].key_map == km_path
