"""The ReFind and no-memory arms inside the LongMemEval harness.

LongMemEval splits extraction from answering: contexts are built and
persisted at extract time, then answered (possibly much later, possibly
by rebuild_contexts.py) from the row. Both comparator arms have to
survive that split, and the memory arms' prompts have to stay
byte-identical so every committed artifact re-answers the same way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import longmemeval_bench as lmb  # noqa: E402


_QUESTION = {
    "question_id": "q1",
    "question": "Which bike did I buy?",
    "answer": "Trek Domane",
    "question_date": "2023/06/01 (Thu) 10:00",
    # Deliberately out of chronological order: ingest sorts by date, so the
    # archive must sort the same way or its ordinals mean nothing.
    "haystack_dates": ["2023/05/20 (Sat) 02:03", "2023/04/10 (Mon) 09:00"],
    "haystack_sessions": [
        [{"role": "user", "content": "I picked up the Trek Domane today"},
         {"role": "assistant", "content": "congratulations"}],
        [{"role": "user", "content": "still deciding between bikes"},
         {"role": "assistant", "content": ""}],          # skipped: empty
    ],
}


class _RecordingSvc:
    """Records what ingest_and_dream stores; dreams nothing."""

    def __init__(self):
        self.stored: list[str] = []

    def store(self, text, source="bench"):
        self.stored.append(text)
        return {"stored": True}

    def dream_run(self, extractor, limit=100):
        return {"pulled": 0}


def test_archive_mirrors_what_ingest_stores_turn_for_turn():
    """The ReFind arm must search exactly the corpus the bank holds. Both
    paths format and order turns independently, so they are pinned in
    lockstep here — the same failure rebuild_contexts.py had in 2026-07-30
    when its ranking drifted from the service's."""
    svc = _RecordingSvc()
    lmb.ingest_and_dream(svc, None, _QUESTION, "http://unused")
    archive = lmb.archive_from_lme_question(_QUESTION)
    assert [r.text for r in archive.records] == svc.stored
    assert len(archive) == 3                       # the empty turn is dropped


def test_archive_records_carry_chronological_sessions_and_dates():
    archive = lmb.archive_from_lme_question(_QUESTION)
    first = archive.records[0]
    assert first.text.startswith("[2023/04/10 (Mon) 09:00] user:")
    assert (first.session, first.ordinal, first.date) == ("1", 1, "2023-04-10")
    assert [r.ordinal for r in archive.records] == [1, 2, 3]
    assert archive.records[-1].date == "2023-05-20"


class _StubSvc:
    def search(self, q, top_k, **kw):
        return {"entries": [{"text": f"t{i}"} for i in range(top_k)]}

    def cortex_search(self, q, **kw):
        return {"entries": []}


def _chat_planning(*replies):
    calls = []

    def chat(system, user, *, max_tokens=256, **_):
        calls.append(user)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    chat.calls = calls
    return chat


def test_serve_comparator_arms_adds_both_arms():
    contexts = lmb.build_contexts(_StubSvc(), "Which bike?")
    chat = _chat_planning('{"queries": ["Trek Domane"], "done": true}')
    trace = lmb.serve_comparator_arms(
        contexts, "Which bike?",
        archive=lmb.archive_from_lme_question(_QUESTION),
        refind=True, nomem=True, chat=chat,
        refind_kwargs={"rounds": 1, "top_k": 2})
    assert "Trek Domane" in contexts["refind"]
    assert contexts["nomem"] == ""
    # one archive turn matches that query; the loop serves what it found
    assert trace["served"] == 1 and trace["inspected"] == 1
    assert len(chat.calls) == 1
    assert contexts["rag"].startswith("t0")        # memory arms untouched


def test_serve_comparator_arms_is_inert_by_default():
    """A vanilla LongMemEval run must be byte-identical: no new context
    keys, and not one extra model call."""
    contexts = lmb.build_contexts(_StubSvc(), "q?")

    def boom(*a, **kw):                            # pragma: no cover
        raise AssertionError("planner called on a vanilla run")

    assert lmb.serve_comparator_arms(contexts, "q?", chat=boom) is None
    assert set(contexts) == {"rag", "cortex", "hybrid"}


def test_serve_comparator_arms_refuses_refind_without_an_archive():
    with pytest.raises(SystemExit, match="needs an archive"):
        lmb.serve_comparator_arms({}, "q?", refind=True)


def test_refind_budget_matches_the_rag_control_at_call_time():
    archive = lmb.archive_from_lme_question(_QUESTION)
    chat = _chat_planning('{"queries": ["bike bikes Trek"], "done": true}')
    old = lmb.RAG_TOP_K
    try:
        lmb.RAG_TOP_K = 2
        contexts: dict = {}
        trace = lmb.serve_comparator_arms(contexts, "bikes?", archive=archive,
                                          refind=True, chat=chat,
                                          refind_kwargs={"rounds": 1})
        assert trace["top_k"] == 2 and trace["served"] == 2
    finally:
        lmb.RAG_TOP_K = old


def test_answer_and_judge_prompts_are_byte_identical_for_memory_arms(
        monkeypatch):
    """The regression gate re-answers pinned contexts with this prompt, so
    its text is a contract, not a detail."""
    prompts: list[tuple[str, str]] = []

    def fake_chat(system, prompt, max_tokens=256, **_):
        prompts.append((system, prompt))
        return "yes"

    monkeypatch.setattr(lmb, "_chat", fake_chat)
    row = {"question": "q", "answer": "a", "question_date": "d",
           "question_type": "knowledge-update", "contexts": {"rag": "r"}}
    lmb.answer_and_judge(row)
    system, prompt = prompts[0]
    assert system is lmb._ANSWER_SYSTEM
    assert prompt == "Question date: d\nQuestion: q\n\nMemory context:\nr"
    assert prompts[1][1].startswith("Question: q\nCorrect answer: a")


def test_answer_and_judge_gives_the_nomem_arm_the_question_alone(monkeypatch):
    prompts: list[tuple[str, str]] = []

    def fake_chat(system, prompt, max_tokens=256, **_):
        prompts.append((system, prompt))
        return "yes"

    monkeypatch.setattr(lmb, "_chat", fake_chat)
    row = {"question": "q", "answer": "a", "question_date": "d",
           "question_type": "knowledge-update",
           "contexts": {"nomem": "", "rag": "r"}}
    out = lmb.answer_and_judge(row)
    import nomem_arm
    system, prompt = prompts[0]
    # this harness's length policy, not BEAM's (see test_nomem_arm.py)
    assert system == nomem_arm.nomem_system(nomem_arm.LENGTH_ONE_SENTENCE)
    assert nomem_arm.LENGTH_ONE_SENTENCE in system
    assert prompt == "Question date: d\nQuestion: q"
    assert "Memory context" not in prompt and "(empty)" not in prompt
    assert out["nomem_correct"] is True
    # its context is empty, recorded in the same units as every other arm
    # (approx_tokens floors at 1, so an empty context reads 1, not 0)
    assert out["nomem_context_tokens"] == 1
    # the memory arm beside it keeps the shared framing
    assert prompts[2][0] is lmb._ANSWER_SYSTEM


def test_extract_phase_probes_the_answerer_when_refind_is_on(monkeypatch):
    """The ReFind loop drives the ANSWERER model, so --phase extract
    --refind needs that server even though a plain extract phase does not.
    Without this the run dies mid-question after paying a full ingest,
    writing no row and so resuming from nothing."""
    probed: list[str] = []

    def fake_probe(url):
        probed.append(url)
        return url != lmb.QWEN_URL          # extractor up, answerer down

    # gemma-e2b is served on its own port; qwen-27b IS the answerer's, so
    # only a separate-endpoint extractor exercises the phase split.
    monkeypatch.setattr(lmb, "probe", fake_probe)
    with pytest.raises(SystemExit, match="answer/judge server"):
        lmb.run_extract("oracle", 1, "gemma-e2b", do_answer=False,
                        refind=True)
    assert lmb.QWEN_URL in probed


def test_extract_phase_without_refind_does_not_need_the_answerer(monkeypatch):
    """The phase split is the point: a plain extract must still run with
    only the extractor endpoint up."""
    monkeypatch.setattr(lmb, "probe", lambda url: url != lmb.QWEN_URL)
    monkeypatch.setattr(lmb, "load_questions", lambda *a, **kw: [])
    lmb.run_extract("oracle", 1, "gemma-e2b", do_answer=False)  # no SystemExit


def _judged_row(qid, **flags):
    row = {"question_id": qid, "question": "which bike?",
           "answer": "Trek Domane", "question_type": "knowledge-update",
           "consolidation": {"superseded": 0}, "cortex_response": "x",
           "abstention": False, "gold_in_question": False}
    for arm, ok in flags.items():
        row[f"{arm}_correct"] = ok
        row[f"{arm}_context_tokens"] = 10
        row.setdefault(f"{arm}_response", "x")
    return row


def test_report_covers_the_comparator_arms_and_the_leak_check(tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    out = lmb.out_file("oracle", "qwen-27b", "cmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [_judged_row("q1", rag=True, cortex=True, hybrid=True,
                        refind=True, nomem=False),
            _judged_row("q2", rag=False, cortex=False, hybrid=False,
                        refind=True, nomem=False)]
    rows[1]["gold_in_question"] = True             # a leaked row
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    lmb.report("oracle", "qwen-27b", "cmp")
    summary = json.loads(out.with_name(
        out.name.removesuffix(".jsonl") + ".summary.json").read_text(
            encoding="utf-8"))
    assert summary["arms"]["refind"]["accuracy"] == 1.0
    assert summary["arms"]["nomem"]["accuracy"] == 0.0
    assert summary["leak_check"]["n_leaked"] == 1
    # the leaked row is excluded from the leak-free read of every arm
    assert summary["leak_check"]["arms"]["rag"]["leak_free"] == 1.0


def test_report_is_loud_when_rows_disagree_about_their_arms(tmp_path,
                                                            monkeypatch):
    """Resuming a run with different arm flags than it started with leaves
    a file whose rows carry different arms. Reporting the intersection
    would quietly drop an arm; reporting rows[0]'s arms would raise a bare
    KeyError. Say what happened instead."""
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    out = lmb.out_file("oracle", "qwen-27b", "mixed")
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [_judged_row("q1", rag=True, cortex=True, hybrid=True,
                        refind=True),
            _judged_row("q2", rag=True, cortex=True, hybrid=True)]
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    with pytest.raises(SystemExit, match="refind"):
        lmb.report("oracle", "qwen-27b", "mixed")


def test_report_omits_the_leak_check_for_legacy_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(lmb, "RESULTS_DIR", tmp_path)
    out = lmb.out_file("oracle", "qwen-27b", "legacy")
    out.parent.mkdir(parents=True, exist_ok=True)
    row = _judged_row("q1", rag=True, cortex=True, hybrid=True)
    row.pop("gold_in_question")
    out.write_text(json.dumps(row), encoding="utf-8")
    lmb.report("oracle", "qwen-27b", "legacy")
    summary = json.loads(out.with_name(
        out.name.removesuffix(".jsonl") + ".summary.json").read_text(
            encoding="utf-8"))
    assert "leak_check" not in summary
