"""The review-queue judges on OpenAICompatExtractor (2026-09-02 design):
``judge_links`` / ``judge_candidates`` / ``judge_junk`` / ``judge_slot_pairs``
share the merge judge's transport (one JSON-object call per batch, verdicts
keyed by proposal number) and its contract: validated verdict dicts, rows
the model skipped simply absent, ``ExtractorError`` on transport/parse
failure so the caller can tell failure from an empty result.

Pure: urlopen is mocked and the captured request body is inspected.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from pseudolife_memory.memory import dream as D


@pytest.fixture()
def wire():
    """Capture request bodies; reply with the canned verdicts set per test."""
    state = {"bodies": [], "reply": '{"verdicts": []}', "fail": False}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": state["reply"]}}]}
            ).encode()

    def fake_urlopen(req, timeout=None):
        if state["fail"]:
            raise OSError("connection refused")
        state["bodies"].append(json.loads(req.data.decode()))
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        yield state


def _ex():
    return D.OpenAICompatExtractor("http://x/v1", "m", max_tokens=400)


# ── links ─────────────────────────────────────────────────────────────────

_LINK = {"n": 1, "src": "tests/test_bm25.py", "relation": "tests",
         "dst": "BM25", "rationale": "cross-project: ['a'] x ['b']",
         "src_edges": ["tests> rebuild.py"], "dst_edges": [],
         "src_scopes": ["pseudolife-mcp"], "dst_scopes": ["re-evidence-hub"],
         "co_mentions": ["[pseudolife-mcp] tests/test_bm25.py covers BM25"],
         "src_mentions": [], "dst_mentions": []}


def test_judge_links_serializes_evidence_and_parses_verdicts(wire):
    wire["reply"] = json.dumps({"verdicts": [
        {"id": 1, "verdict": "retype", "confidence": 0.9,
         "note": "direction", "relation": "implements"},
        {"id": 2, "verdict": "accept", "confidence": 0.8, "note": "ok"},
        {"id": 3, "verdict": "bogus", "confidence": 0.8, "note": "x"},
        {"id": 9, "verdict": "accept", "confidence": 0.8, "note": "x"},
    ]})
    rows = [_LINK, {**_LINK, "n": 2, "src": "a", "dst": "b"},
            {**_LINK, "n": 3, "src": "c", "dst": "d"}]
    out = _ex().judge_links(rows)
    body = wire["bodies"][0]
    assert "LINK PROPOSALS" in body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    assert "tests/test_bm25.py" in user and "covers BM25" in user
    assert "re-evidence-hub" in user                      # scopes are evidence
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # validated: unknown verdicts and unknown ids dropped, relation only on retype
    assert out == [
        {"n": 1, "verdict": "retype", "confidence": 0.9, "note": "direction",
         "relation": "implements"},
        {"n": 2, "verdict": "accept", "confidence": 0.8, "note": "ok",
         "relation": None},
    ]


def test_judge_links_retype_without_relation_degrades_to_leave(wire):
    wire["reply"] = json.dumps({"verdicts": [
        {"id": 1, "verdict": "retype", "confidence": 0.9, "note": "?"}]})
    out = _ex().judge_links([_LINK])
    assert out[0]["verdict"] == "leave" and out[0]["relation"] is None


def test_judge_links_empty_input_makes_no_call(wire):
    assert _ex().judge_links([]) == []
    assert wire["bodies"] == []


def test_judge_links_transport_failure_raises(wire):
    wire["fail"] = True
    with pytest.raises(D.ExtractorError):
        _ex().judge_links([_LINK])


# ── candidates ────────────────────────────────────────────────────────────

_CAND = {"n": 1, "src": "stalker-2", "dst": "DLSS 4.5", "similarity": 0.91,
         "src_snippets": ["Stalker 2 config: DLSS preset L"],
         "dst_snippets": ["DLSS 4.5 preset L"]}


def test_judge_candidates_parses_propose_with_direction(wire):
    wire["reply"] = json.dumps({"verdicts": [
        {"id": 1, "verdict": "propose", "confidence": 0.7,
         "relation": "uses", "src": "stalker-2", "dst": "DLSS 4.5",
         "rationale": "the game uses the upscaler"},
        {"id": 2, "verdict": "dismiss", "confidence": 0.6,
         "rationale": "co-mention"},
        {"id": 3, "verdict": "propose", "confidence": 0.7,
         "rationale": "no relation given"},
    ]})
    rows = [_CAND, {**_CAND, "n": 2}, {**_CAND, "n": 3}]
    out = _ex().judge_candidates(rows)
    assert "LINK CANDIDATES" in wire["bodies"][0]["messages"][0]["content"]
    assert out[0] == {"n": 1, "verdict": "propose", "confidence": 0.7,
                      "relation": "uses", "src": "stalker-2",
                      "dst": "DLSS 4.5", "rationale": "the game uses the upscaler"}
    assert out[1]["verdict"] == "dismiss" and out[1]["relation"] is None
    # a propose without a relation cannot be filed: degraded to leave
    assert out[2]["verdict"] == "leave"


# ── junk ──────────────────────────────────────────────────────────────────

_JUNK = {"n": 1, "display": "evals/a.py, evals/b.py", "reason": "list-artifact",
         "degree": 1, "edges": ["wire an arm -prefers-> evals/a.py, evals/b.py [action]"],
         "facts": 0, "fact_text": [], "lesson_object": True,
         "scopes": ["pseudolife-mcp"], "mentions": ["[digest] wired the arm"]}


def test_judge_junk_parses_and_validates(wire):
    wire["reply"] = json.dumps({"verdicts": [
        {"id": 1, "verdict": "delete", "confidence": 0.9, "note": "lesson-object list"},
        {"id": 2, "verdict": "keep", "confidence": 0.95, "note": "real branch"},
        {"id": 3, "verdict": "merge", "confidence": 0.9, "note": "not a junk verdict"},
    ]})
    rows = [_JUNK, {**_JUNK, "n": 2, "display": "fix/x"}, {**_JUNK, "n": 3}]
    out = _ex().judge_junk(rows)
    sys_prompt = wire["bodies"][0]["messages"][0]["content"]
    assert "JUNK" in sys_prompt
    user = wire["bodies"][0]["messages"][1]["content"]
    assert "lesson-minted" in user.lower() or "lesson object" in user.lower()
    assert [v["verdict"] for v in out] == ["delete", "keep"]
    assert out[0]["confidence"] == 0.9


# ── store curation ────────────────────────────────────────────────────────

_PAIR = {"n": 1, "store": "lesson", "similarity": 0.88,
         "a": {"entity": "deploy daemon to host", "attribute": "approach",
               "value": "verify live", "polarity": "+", "outcome": "success",
               "about": "deploy"},
         "b": {"entity": "deploy the daemon", "attribute": "pitfall",
               "value": "verify live, not /health", "polarity": "+",
               "outcome": "success", "about": "deploy"}}


def test_judge_slot_pairs_parses_keep_and_fold(wire):
    wire["reply"] = json.dumps({"verdicts": [
        {"id": 1, "verdict": "duplicate", "keep": "b", "fold": "carry X",
         "confidence": 0.85, "note": "re-mint"},
        {"id": 2, "verdict": "distinct", "confidence": 0.9, "note": "siblings"},
        {"id": 3, "verdict": "duplicate", "confidence": 0.9, "note": "no keep"},
    ]})
    rows = [_PAIR, {**_PAIR, "n": 2}, {**_PAIR, "n": 3}]
    out = _ex().judge_slot_pairs(rows)
    assert "DUPLICATE" in wire["bodies"][0]["messages"][0]["content"].upper()
    user = wire["bodies"][0]["messages"][1]["content"]
    assert "verify live, not /health" in user and "pitfall" in user
    assert out[0] == {"n": 1, "verdict": "duplicate", "keep": "b",
                      "fold": "carry X", "confidence": 0.85, "note": "re-mint"}
    assert out[1]["verdict"] == "distinct" and out[1]["keep"] is None
    # a duplicate verdict that names no survivor cannot be applied: leave
    assert out[2]["verdict"] == "leave"


def test_judge_batches_floor_max_tokens_per_row(wire):
    """Every judge shares the merge judge's per-row output floor so a small
    extraction budget cannot truncate a large batch."""
    wire["reply"] = '{"verdicts": []}'
    ex = D.OpenAICompatExtractor("http://x/v1", "m", max_tokens=100)
    ex.judge_junk([{**_JUNK, "n": i + 1} for i in range(5)])
    assert wire["bodies"][-1]["max_tokens"] == 120 * 5


def test_judge_request_records_the_served_model(wire):
    """The distinct-second-model check compares what the endpoint SERVED,
    not the configured name (a name-agnostic endpoint serves one model
    under any requested name)."""
    ex = _ex()
    assert ex.served_model is None
    wire["reply"] = '{"verdicts": []}'

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"model": "really-served-x",
                               "choices": [{"message": {"content": '{"verdicts": []}'}}]}).encode()

    with mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp()):
        ex.judge_junk([_JUNK])
    assert ex.served_model == "really-served-x"
