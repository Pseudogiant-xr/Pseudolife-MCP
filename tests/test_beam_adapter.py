"""Unit tests for the BEAM adapter's pure parts (no GPU, no BEAM data).

The judge-prompt extraction is tested against a synthetic prompts.py so the
test never depends on the (uncommitted) BEAM checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import beam_adapter  # noqa: E402
from beam_adapter import (  # noqa: E402
    arms_for, judge_response, load_chat_turns, load_judge_prompt,
    parse_judge_score,
)


def _mini_beam(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "prompts.py").write_text(
        'other = "x"\n'
        'unified_llm_judge_base_prompt = """judge <question> '
        '<rubric_item> <llm_response>"""\n', encoding="utf-8")
    return tmp_path


def test_load_judge_prompt_extracts_without_import(tmp_path):
    prompt = load_judge_prompt(_mini_beam(tmp_path))
    assert "<rubric_item>" in prompt and "<llm_response>" in prompt


def test_load_judge_prompt_missing_is_loud(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "prompts.py").write_text('other = "x"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_judge_prompt(tmp_path)


def test_parse_judge_score_json_fenced_and_regex():
    assert parse_judge_score('{"score": 1.0, "reason": "ok"}') == 1.0
    assert parse_judge_score('```json\n{"score": 0.5}\n```') == 0.5
    assert parse_judge_score('Sure! {"score": "0.5", "x": 1}') == 0.5
    assert parse_judge_score("no score here") is None


def test_arms_for_chronicle_appends_hybrid_ev():
    from longmemeval_bench import ARMS
    assert arms_for(False) == ARMS
    assert arms_for(True) == (*ARMS, "hybrid_ev")


def test_report_derives_arms_from_rows(tmp_path, monkeypatch, capsys):
    """A chronicle run's summary must carry hybrid_ev; a vanilla run's must
    not — report reads the arms off the rows, not a static tuple."""
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "event_ordering", "index": i,
             "rag_score": 0.5, "rag_score_intfaithful": 0.0,
             "cortex_score": 0.0, "cortex_score_intfaithful": 0.0,
             "hybrid_score": 0.5, "hybrid_score_intfaithful": 0.0,
             "hybrid_ev_score": 1.0, "hybrid_ev_score_intfaithful": 1.0}
            for i in range(2)]
    out = tmp_path / "beam-100K-qwen-27b-t.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "t")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-t.summary.json").read_text(
            encoding="utf-8"))
    assert summary["arms"]["hybrid_ev"]["score"] == 1.0
    assert summary["types"]["event_ordering"]["hybrid_ev"] == 1.0
    assert summary["arms"]["hybrid"]["score"] == 0.5


def test_arms_for_only_filters_in_canonical_order():
    assert arms_for(False, only="hybrid,rag") == ("rag", "hybrid")
    assert arms_for(True, only="hybrid_ev") == ("hybrid_ev",)


def test_arms_for_unknown_arm_is_loud():
    with pytest.raises(SystemExit):
        arms_for(False, only="rag,hybrid_ev")   # ev needs --chronicle
    with pytest.raises(SystemExit):
        arms_for(False, only="ragg")


def test_judge_response_uses_injected_chat():
    """The rejudge script swaps the local-server judge for a frontier CLI
    judge by injecting ``chat``; the scoring/failure semantics must not
    change with the transport."""
    calls = []

    def fake_chat(system, user, *, max_tokens=256, **_):
        calls.append(user)
        return '{"score": 0.5}' if len(calls) == 1 else "not json"

    v = judge_response("judge <question> <rubric_item> <llm_response>",
                       "q?", ["item one", "item two"], "an answer",
                       chat=fake_chat)
    assert v["llm_judge_score"] == 0.5           # mean over scored items only
    assert v["llm_judge_score_intfaithful"] == 0.0
    assert v["judge_failures"] == 1
    assert "item one" in calls[0] and "an answer" in calls[0]


def test_report_carries_hybrid_top_k_when_rows_do(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "abstention", "index": i,
             "hybrid_top_k": 6,
             "hybrid_score": 1.0, "hybrid_score_intfaithful": 1.0}
            for i in range(2)]
    out = tmp_path / "beam-100K-qwen-27b-hyb6.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "hyb6")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-hyb6.summary.json").read_text(
            encoding="utf-8"))
    assert summary["hybrid_top_k"] == 6


def test_report_omits_hybrid_top_k_for_legacy_rows(tmp_path, monkeypatch):
    """Pre-flag artifacts have no hybrid_top_k key; their summaries must not
    grow a null field on a --report re-run."""
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "abstention", "index": 0,
             "hybrid_score": 1.0, "hybrid_score_intfaithful": 1.0}]
    out = tmp_path / "beam-100K-qwen-27b-legacy.jsonl"
    out.write_text(json.dumps(rows[0]), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "legacy")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-legacy.summary.json").read_text(
            encoding="utf-8"))
    assert "hybrid_top_k" not in summary


def test_beam_answer_policy_surfaces_contradictions():
    """BEAM plants deliberate contradictions and rubric-checks that the
    answer SAYS the record conflicts; the old prompt ordered silent
    newest-wins resolution, so every arm retrieved the evidence and
    scored 0 (2026-08-22 autopsy). Value updates still resolve to the
    current value — only genuine conflicts get surfaced. The LME answer
    prompt is deliberately NOT changed (the regression gate re-answers
    pinned contexts with it)."""
    s = beam_adapter._BEAM_ANSWER_SYSTEM
    assert "contradict" in s.lower()
    assert "both" in s.lower()
    assert "most CURRENT value" in s          # update semantics retained
    import longmemeval_bench as lme
    assert "contradict" not in lme._ANSWER_SYSTEM.lower()


def test_format_turn_stamps_session_and_turn_ordinals():
    """Ordering metadata rides the stored text (session = BEAM batch,
    turn = per-chat ordinal) — the free signal event_ordering questions
    need, previously discarded at ingest."""
    turn = {"batch": 3, "time_anchor": "March-15-2024", "role": "user",
            "content": "hello"}
    assert beam_adapter.format_turn(turn, 41) == \
        "[March-15-2024] [session 3, turn 41] user: hello"
    bare = {"batch": 1, "time_anchor": None, "role": "assistant",
            "content": "hi"}
    assert beam_adapter.format_turn(bare, 2) == \
        "[session 1, turn 2] assistant: hi"


def test_report_carries_rag_top_k_when_rows_do(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(beam_adapter, "RESULTS_DIR", tmp_path)
    rows = [{"chat_id": "1", "type": "abstention", "index": 0,
             "rag_top_k": 16, "hybrid_top_k": 16,
             "rag_score": 1.0, "rag_score_intfaithful": 1.0}]
    out = tmp_path / "beam-100K-qwen-27b-r16.jsonl"
    out.write_text(json.dumps(rows[0]), encoding="utf-8")
    beam_adapter.report("100K", "qwen-27b", "r16")
    summary = json.loads(
        (tmp_path / "beam-100K-qwen-27b-r16.summary.json").read_text(
            encoding="utf-8"))
    assert summary["rag_top_k"] == 16


def test_rag_top_k_validation_is_loud():
    from pathlib import Path
    with pytest.raises(SystemExit, match="positive"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, rag_top_k=0)
    # hybrid budget is validated against the EFFECTIVE rag width
    with pytest.raises(SystemExit, match="exceeds"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, rag_top_k=8, hybrid_top_k=12)


class _ServeSvc:
    """Stub service for context-building tests: search returns top_k texts,
    cortex is empty, no history calls."""

    def search(self, q, top_k, **kw):
        return {"entries": [{"text": f"t{i}"} for i in range(top_k)]}

    def cortex_search(self, q, **kw):
        return {"entries": []}


def test_hybrid_budget_matches_rag_by_default():
    """The 2026-08-21 decision: the hybrid arm is a SUPERSET of the rag arm
    by default, so hybrid-vs-rag deltas isolate the fact spine instead of
    confounding it with a halved raw-turn budget."""
    import longmemeval_bench as lme
    assert lme.HYBRID_TOP_K == lme.RAG_TOP_K


def test_build_contexts_with_parts_exposes_serve_state():
    """Persisted BEAM rows carry the structured serve state so serving-knob
    reruns recompose contexts offline instead of re-paying ingest."""
    import longmemeval_bench as lme
    ctx = lme.build_contexts(_ServeSvc(), "q?", with_parts=True)
    parts = ctx["parts"]
    assert parts["raw"] == [f"t{i}" for i in range(lme.RAG_TOP_K)]
    assert parts["mem"] == parts["raw"]          # default knobs: same call
    assert parts["facts"] == [] and parts["events"] == []
    assert "parts" not in lme.build_contexts(_ServeSvc(), "q?")


def test_dump_chat_bank_writes_facts_with_history(tmp_path):
    import gzip
    import json

    class _Svc(_ServeSvc):
        def cortex_dump(self):
            return {"entries": [
                {"entity": "user", "attribute": "city", "value": "Sydney",
                 "source_entries": ["bulky"]}]}

        def history(self, entity, attribute):
            return {"versions": [{"value": "Perth"}, {"value": "Sydney"}]}

    path = tmp_path / "chat1.json.gz"
    tally = {"turns": 188, "dreams": 6}
    beam_adapter.dump_chat_bank(_Svc(), "1", tally, path)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        bank = json.load(fh)
    assert bank["chat_id"] == "1" and bank["consolidation"] == tally
    assert bank["facts"][0]["history"] == ["Perth", "Sydney"]
    assert "source_entries" not in bank["facts"][0]


def test_hybrid_top_k_is_read_at_call_time():
    """beam_adapter --hybrid-top-k works by setting lme.HYBRID_TOP_K before
    questions are answered; that only holds if build_contexts reads the
    module global at call time rather than binding it at import."""
    import longmemeval_bench as lme

    class _Svc:
        def search(self, q, top_k, **kw):
            return {"entries": [{"text": f"t{i}"} for i in range(top_k)]}

        def cortex_search(self, q, **kw):
            return {"entries": []}

    old = lme.HYBRID_TOP_K
    try:
        lme.HYBRID_TOP_K = 6
        ctx = lme.build_contexts(_Svc(), "q?")
        mems = ctx["hybrid"].split("Relevant memories:\n", 1)[1]
        assert [m for m in mems.split("\n\n") if m] == [
            f"t{i}" for i in range(6)]
    finally:
        lme.HYBRID_TOP_K = old


def test_hybrid_top_k_beyond_rag_budget_is_loud():
    """build_contexts slices mems[:HYBRID_TOP_K] from a top_k=RAG_TOP_K
    search, so a wider request is silently capped at 6 while the rows would
    record the wider number — an artifact asserting a budget that was never
    served (review finding 4). The validation must fire before any server
    probe so a bad flag dies instantly."""
    from pathlib import Path
    with pytest.raises(SystemExit, match="exceeds"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, hybrid_top_k=12)
    with pytest.raises(SystemExit, match="positive"):
        beam_adapter.run(Path("nowhere"), "100K", "qwen-27b", "t",
                         None, None, hybrid_top_k=0)


def test_dream_tally_counts_events():
    class _Svc:
        def __init__(self):
            self.calls = 0
        def dream_run(self, extractor):
            self.calls += 1
            return {"pulled": 3, "claims": 2, "superseded": 0,
                    "literal_dropped": 1, "events_inserted": 2,
                    "events_pass_failed": False}
        def dream_status(self):
            return {"backlog": 0}
    tally = {"turns": 0, "dreams": 0, "claims": 0, "superseded": 0,
             "literal_dropped": 0, "events_inserted": 0,
             "events_pass_failures": 0}
    beam_adapter._dream_until_drained(_Svc(), None, tally)
    assert tally["events_inserted"] == 2
    assert tally["events_pass_failures"] == 0


def test_load_chat_turns_flattens_batches(tmp_path):
    # Real BEAM shape: a "turn" is a LIST of message dicts (an exchange);
    # the bare-dict tolerance is exercised by batch 2.
    import json
    (tmp_path / "chat.json").write_text(json.dumps([
        {"batch_number": 1, "time_anchor": "March-15-2024", "turns": [
            [{"role": "user", "content": "hello", "time_anchor": None},
             {"role": "assistant", "content": "hi there"},
             {"role": "assistant", "content": ""}],        # dropped: empty
        ]},
        {"batch_number": 2, "time_anchor": None, "turns": [
            {"role": "user", "content": "again",
             "time_anchor": "April-01-2024"},
        ]},
    ]), encoding="utf-8")
    turns = load_chat_turns(tmp_path)
    assert [(t["batch"], t["role"]) for t in turns] == [
        (1, "user"), (1, "assistant"), (2, "user")]
    # turn-level anchor wins; batch anchor is the fallback
    assert turns[0]["time_anchor"] == "March-15-2024"
    assert turns[1]["time_anchor"] == "March-15-2024"
    assert turns[2]["time_anchor"] == "April-01-2024"


# ── session-digest arm (spec 2026-08-24) ─────────────────────────────────────


def test_arms_for_digest_appends_hybrid_digest():
    assert arms_for(False, digest=True) == (
        "rag", "cortex", "hybrid", "hybrid_digest")
    assert arms_for(True, digest=True) == (
        "rag", "cortex", "hybrid", "hybrid_ev", "hybrid_digest")
    assert "hybrid_digest" not in arms_for(False)


class _DigestBankSvc:
    """Stub of a bank whose digest outranks every turn: search returns the
    digest at rank 0 followed by beam turns, sliced to top_k. Records each
    call's kwargs."""

    def __init__(self):
        self.calls: list[dict] = []

    def search(self, q, top_k, **kw):
        self.calls.append({"top_k": top_k, **kw})
        digest = {"text": "Session digest: s1\n" + "d" * 80,
                  "source": "digest"}
        turns = [{"text": f"turn-{i}-" + "x" * 20, "source": "beam"}
                 for i in range(20)]
        return {"entries": ([digest] + turns)[:top_k]}

    def cortex_search(self, q, **kw):
        return {"entries": []}


def test_build_contexts_off_is_byte_identical_call_shape():
    """Byte-identity contract: with DIGEST_ARM off the search calls carry
    no sources kwarg and the original top_k — the pre-digest shape."""
    import longmemeval_bench as lme
    svc = _DigestBankSvc()
    ctx = lme.build_contexts(svc, "q?")
    assert "hybrid_digest" not in ctx
    assert all("sources" not in c for c in svc.calls)
    assert all(c["top_k"] == lme.RAG_TOP_K for c in svc.calls[:2])


def test_build_contexts_digest_arm_preserves_control_width(monkeypatch):
    """The 2026-08-25 review finding: the service's sources= filter runs
    post-top-k, so filtering would silently shorten the control. The fix
    over-fetches by the bank's digest count and drops post-hoc — the rag
    control must serve its FULL turn budget, digest-free, in the ranking
    a digest-free bank would produce."""
    import longmemeval_bench as lme
    monkeypatch.setattr(lme, "DIGEST_ARM", True)
    monkeypatch.setattr(lme, "DIGEST_COUNT", 1)
    svc = _DigestBankSvc()
    ctx = lme.build_contexts(svc, "q?", with_parts=True)
    assert all(c["top_k"] == lme.RAG_TOP_K + 1 for c in svc.calls[:2])
    assert all("sources" not in c for c in svc.calls)
    # control preserved: the full budget of beam turns, digest excluded
    assert ctx["parts"]["raw"] == [f"turn-{i}-" + "x" * 20
                                   for i in range(lme.RAG_TOP_K)]
    assert "Session digest:" not in ctx["rag"]
    assert "Session digest:" not in ctx["hybrid"]
    # the digest arm serves the digest, budget-matched by characters
    served = ctx["parts"]["mem_digest"]
    assert served[0].startswith("Session digest:")
    budget = sum(len(t) for t in ctx["parts"]["mem"][:lme.HYBRID_TOP_K])
    assert sum(len(t) for t in served) <= max(budget, len(served[0]))
    assert len(served) <= lme.HYBRID_TOP_K
    assert ctx["hybrid_digest"].endswith("\n\n".join(served))


class _EpisodeIngestSvc:
    """Records the episode/store/digest call sequence for ingest_chat."""

    def __init__(self):
        self.events: list[tuple] = []
        self.digest_calls = 0

    def episode_start_session(self, key, title):
        self.events.append(("start", key))

    def episode_end_session(self, key, run_dream=False):
        self.events.append(("end", key))

    def store(self, text, source):
        self.events.append(("store", source))

    def dream_run(self, extractor):
        return {"pulled": 0}

    def dream_status(self):
        return {"backlog": 0}

    def generate_digests_stage(self, extractor):
        self.digest_calls += 1
        if self.digest_calls == 1:
            return {"scanned": 2, "written": 2}
        return {"scanned": 0, "written": 0}


_TWO_BATCH_TURNS = [
    {"batch": 1, "time_anchor": None, "role": "user", "content": "a"},
    {"batch": 1, "time_anchor": None, "role": "assistant", "content": "b"},
    {"batch": 2, "time_anchor": None, "role": "user", "content": "c"},
]


def test_ingest_chat_digest_wraps_batches_in_episodes():
    svc = _EpisodeIngestSvc()
    tally = beam_adapter.ingest_chat(svc, None, _TWO_BATCH_TURNS,
                                     digest=True)
    assert [e for e in svc.events if e[0] != "store"] == [
        ("start", "beam-1"), ("end", "beam-1"),
        ("start", "beam-2"), ("end", "beam-2")]
    assert tally["digests"] == 2
    assert svc.digest_calls == 2          # drained until scanned == 0


def test_ingest_chat_without_digest_touches_no_episodes():
    svc = _EpisodeIngestSvc()
    tally = beam_adapter.ingest_chat(svc, None, _TWO_BATCH_TURNS)
    assert all(e[0] == "store" for e in svc.events)
    assert "digests" not in tally
