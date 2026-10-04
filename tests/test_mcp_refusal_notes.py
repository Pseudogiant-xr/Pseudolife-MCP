"""Refusals that read as successes explain themselves; bad numbers are
refused when the arguments are bound (2026-10-04 tool-description review,
findings M2 and M3).

* A success-shaped refusal or no-op (nothing changed, the call still
  succeeded) carries a one-sentence ``note`` beside its code, so a model
  learns what happened and what to do instead of retrying blind.
* An out-of-range number (``memory_search(top_k=-3)`` used to raise
  "selected index k out of range" from deep in retrieval) is refused by the
  input schema before the tool body runs, and the range is also written in
  the parameter text, because Codex strips minimum/maximum from the schema
  it shows the model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import invoke_tool as _invoke
from tests.helpers import reload_mcp_filemode as _reload


class _Stub:
    """Stands in for the service: every method records its call and answers
    ``{}``, so a binding test never loads a model or reaches storage."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _call(*args: Any, **kwargs: Any) -> dict:
            self.calls.append((name, args, kwargs))
            return {}
        return _call


def _refused(mod, tool: str, args: dict) -> str:
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError) as info:
        asyncio.run(mod.mcp.call_tool(tool, args))
    return str(info.value)


# ── M3: ranges refused at binding ─────────────────────────────────────────

_OUT_OF_RANGE = [
    ("memory_search", {"query": "q", "top_k": -3}),
    ("memory_search", {"query": "q", "top_k": 0}),
    ("memory_recent", {"n": 0}),
    ("memory_world_search", {"query": "q", "top_k": 0}),
    ("memory_lesson_search", {"query": "q", "top_k": 0}),
    ("document_search", {"query": "q", "top_k": 0}),
    ("memory_recall", {"query": "q", "top_k": 0}),
    ("memory_consolidation_candidates", {"query": "q", "top_k": 0}),
    ("memory_consolidation_candidates", {"query": "q", "max_clusters": 0}),
    ("memory_consolidation_candidates", {"query": "q", "min_cluster_size": 0}),
    ("memory_dream", {"action": "pull", "limit": 0}),
    ("memory_fact_set", {"entity": "e", "attribute": "a", "value": "v",
                         "confidence": 1.5}),
    ("memory_fact_set", {"entity": "e", "attribute": "a", "value": "v",
                         "confidence": -0.1}),
    ("memory_world_set", {"entity": "e", "attribute": "a", "value": "v",
                          "confidence": 2}),
    ("memory_graph_relate", {"src": "a", "relation": "uses", "dst": "b",
                             "confidence": 1.01}),
    ("memory_outcome", {"task": "t", "outcome": "success", "polarity": "x"}),
    ("memory_fact_set", {"entity": "  ", "attribute": "a", "value": "v"}),
    ("memory_fact_set", {"entity": "e", "attribute": "", "value": "v"}),
    ("memory_fact_set", {"entity": "e", "attribute": "a", "value": " "}),
]


@pytest.mark.parametrize(("tool", "args"), _OUT_OF_RANGE)
def test_out_of_range_arguments_are_refused_before_the_body_runs(
        tmp_path: Path, monkeypatch, tool: str, args: dict) -> None:
    mod = _reload(tmp_path, monkeypatch)
    stub = _Stub()
    monkeypatch.setattr(mod, "service", stub)
    _refused(mod, tool, args)
    assert stub.calls == [], f"{tool} reached the service with {args}"


def test_negative_top_k_names_the_parameter(tmp_path: Path, monkeypatch) -> None:
    """The original crash: top_k=-3 reached torch.topk and came back as
    "selected index k out of range"; the refusal now names the field."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "service", _Stub())
    message = _refused(mod, "memory_search", {"query": "q", "top_k": -3})
    assert "top_k" in message and "greater than or equal to 1" in message


def test_in_range_edges_still_bind(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    stub = _Stub()
    monkeypatch.setattr(mod, "service", stub)
    _invoke("memory_recent", {"n": 1})
    _invoke("memory_fact_set", {"entity": "e", "attribute": "a", "value": "v",
                                "confidence": 0})
    _invoke("memory_fact_set", {"entity": "e", "attribute": "a", "value": "v",
                                "confidence": 1})
    _invoke("memory_outcome", {"task": "t", "outcome": "failure",
                               "polarity": "-"})
    assert [c[0] for c in stub.calls] == [
        "recent", "cortex_write", "cortex_write", "record_outcome"]


# Codex shows a tool as a TypeScript declaration and drops minimum/maximum,
# so a range reaches an OpenAI model only through the parameter text.
_STATED = [
    ("memory_search", "top_k", "positive"),
    ("memory_recent", "n", "positive"),
    ("memory_world_search", "top_k", "positive"),
    ("memory_lesson_search", "top_k", "positive"),
    ("document_search", "top_k", "positive"),
    ("memory_recall", "top_k", "positive"),
    ("memory_consolidation_candidates", "top_k", "positive"),
    ("memory_consolidation_candidates", "max_clusters", "positive"),
    ("memory_consolidation_candidates", "min_cluster_size", "positive"),
    ("memory_dream", "limit", "positive"),
    ("memory_fact_set", "confidence", "0..1"),
    ("memory_world_set", "confidence", "0..1"),
    ("memory_graph_relate", "confidence", "0..1"),
    ("memory_history", "as_of", "not relative words"),
]


def test_ranges_are_stated_in_the_parameter_text(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    missing = [(tool, param) for tool, param, word in _STATED
               if word not in (tools[tool].input_schema["properties"][param]
                               .get("description") or "").lower()]
    assert missing == []


# ── M3: memory_history as_of ──────────────────────────────────────────────


def test_relative_as_of_is_refused_with_the_accepted_formats(
        tmp_path: Path, monkeypatch) -> None:
    """``as_of="yesterday"`` raised "Invalid isoformat string" from the
    service; the binding now says which formats it takes."""
    mod = _reload(tmp_path, monkeypatch)
    stub = _Stub()
    monkeypatch.setattr(mod, "service", stub)
    message = _refused(mod, "memory_history",
                       {"entity": "e", "attribute": "a", "as_of": "yesterday"})
    assert "ISO-8601" in message and "epoch seconds" in message
    assert stub.calls == []
    for ok in ("2026-10-01", "2026-10-01T12:30:00+00:00", 1_790_000_000,
               "1790000000"):
        _invoke("memory_history", {"entity": "e", "attribute": "a",
                                   "as_of": ok})
    assert [c[2]["as_of"] for c in stub.calls] == [
        "2026-10-01", "2026-10-01T12:30:00+00:00", 1_790_000_000,
        "1790000000"]


# ── M3: document_ingest names the server's filesystem ─────────────────────


def test_missing_ingest_path_says_it_was_looked_up_on_the_server(
        tmp_path: Path, monkeypatch) -> None:
    from pseudolife_memory.service import MemoryService
    svc = MemoryService(data_dir=str(tmp_path))
    monkeypatch.setattr(svc, "_ensure_init", lambda: None)
    svc._reference = object()
    svc._embedder = object()
    missing = tmp_path / "nope.pdf"
    with pytest.raises(FileNotFoundError) as info:
        svc.ingest_document(str(missing))
    message = str(info.value)
    assert str(missing) in message and "server" in message


# ── M2: notes on success-shaped refusals ──────────────────────────────────


def _with(mod, monkeypatch, method: str, answer: dict) -> None:
    monkeypatch.setattr(mod.service, method,
                        lambda *a, **k: dict(answer))


@pytest.mark.parametrize("reason", [
    "empty", "filtered_meta", "below_surprise_threshold", "rejected"])
def test_store_refusals_carry_a_note(
        tmp_path: Path, monkeypatch, reason: str) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, "store", {"stored": False, "surprise": 0.0,
                                      "reason": reason, "cortex_promoted": 0})
    out = _invoke("memory_store", {"text": "x"})
    assert out["reason"] == reason and out["stored"] is False
    assert out["note"].startswith("Not stored")


def test_filtered_meta_note_says_to_rephrase_as_the_fact(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, "store", {"stored": False, "surprise": 0.0,
                                      "reason": "filtered_meta",
                                      "cortex_promoted": 0})
    out = _invoke("memory_store", {"text": "x"})
    assert "memory system itself" in out["note"]
    assert "Rephrase" in out["note"]


def test_a_stored_memory_carries_no_note(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, "store", {"stored": True, "surprise": 0.9,
                                      "reason": None, "cortex_promoted": 0})
    assert "note" not in _invoke("memory_store", {"text": "x"})


def test_file_mode_outcome_says_it_is_lost_and_not_to_retry(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, "record_outcome",
          {"recorded": False, "reason": "signals require Postgres storage"})
    out = _invoke("memory_outcome", {"task": "t", "outcome": "success"})
    assert out["recorded"] is False
    assert "not kept" in out["note"] and "retry" in out["note"]


@pytest.mark.parametrize(("tool", "method", "answer"), [
    ("memory_get", "get_entry", {"found": False, "faded": True}),
    ("memory_reinforce", "reinforce", {"reinforced": False, "faded": True}),
])
def test_faded_answers_name_both_causes(
        tmp_path: Path, monkeypatch, tool: str, method: str,
        answer: dict) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, method, answer)
    out = _invoke(tool, {"entry_id": 7})
    assert out["faded"] is True
    assert "wrong id" in out["note"] and "Postgres" in out["note"]


@pytest.mark.parametrize(("tool", "method", "action", "words"), [
    ("memory_set_add", "set_add", "member_invalid", "blank"),
    ("memory_set_add", "set_add", "member_capped", "100"),
    ("memory_set_remove", "set_remove", "member_not_found", "memory_fact_get"),
    ("memory_set_remove", "set_remove", "member_remove_refused", "human"),
])
def test_set_member_refusals_carry_a_note(
        tmp_path: Path, monkeypatch, tool: str, method: str, action: str,
        words: str) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, method, {"action": action, "entity": "e",
                                     "attribute": "a", "member": "m",
                                     "members_count": 0})
    out = _invoke(tool, {"entity": "e", "attribute": "a", "member": "m"})
    assert out["action"] == action and words in out["note"]


def test_member_cap_note_follows_the_cortex_constant(
        tmp_path: Path, monkeypatch) -> None:
    from pseudolife_memory.memory import cortex
    mod = _reload(tmp_path, monkeypatch)
    assert str(cortex.MAX_CURRENT_MEMBERS) in mod._SET_NOTES["member_capped"]


@pytest.mark.parametrize(("args", "method", "answer"), [
    ({"action": "dismiss_pair", "src": "a", "dst": "a"},
     "graph_dismiss_duplicate",
     {"dismissed": False, "reason": "bad_pair", "a": "a", "b": "a"}),
    ({"action": "dismiss_slot_pair", "store": "notes", "src": "a|b",
      "dst": "c|d"},
     "curation_dismiss_duplicate",
     {"dismissed": False, "reason": "bad_store", "store": "notes"}),
    ({"action": "accept_link", "proposal_id": 4},
     "graph_accept_proposal",
     {"accepted": False, "reason": "not_pending", "id": 4}),
    ({"action": "restore_slot", "store": "lesson", "src": "t|a"},
     "lesson_restore",
     {"restored": 0, "task": "t", "aspect": "a",
      "reason": "nothing_retired"}),
])
def test_graph_review_codes_carry_a_note(
        tmp_path: Path, monkeypatch, args: dict, method: str,
        answer: dict) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, method, answer)
    out = _invoke("memory_graph_review", args)
    assert out["reason"] == answer["reason"] and out["note"]


def test_graph_review_batch_results_carry_notes(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "graph_accept_proposal",
        lambda pid: ({"accepted": False, "reason": "not_pending", "id": pid}
                     if pid == 2 else {"accepted": True, "id": pid}))
    out = _invoke("memory_graph_review",
                  {"action": "accept_link", "proposal_ids": [1, 2]})
    assert "note" not in out["results"][0]
    assert out["results"][1]["note"] == mod._REVIEW_NOTES["not_pending"]


def test_forget_with_no_match_says_nothing_was_deleted(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _with(mod, monkeypatch, "delete", {"deleted_count": 0,
                                       "deleted_texts": []})
    out = _invoke("memory_forget", {"scope": "memory", "text": "nope"})
    assert out["deleted_count"] == 0 and "Nothing" in out["note"]
    _with(mod, monkeypatch, "cortex_forget",
          {"removed": 0, "entity": "e", "attribute": None})
    out = _invoke("memory_forget", {"scope": "fact", "entity": "e"})
    assert out["removed"] == 0 and "Nothing" in out["note"]
    # A refused bulk delete keeps its own error and hint, no note.
    _with(mod, monkeypatch, "delete", {"deleted_count": 0,
                                       "error": "bulk_confirm_required",
                                       "would_delete": 30, "threshold": 20,
                                       "sample_texts": [], "hint": "h"})
    out = _invoke("memory_forget", {"scope": "memory", "source": "s"})
    assert "note" not in out
    _with(mod, monkeypatch, "delete", {"deleted_count": 2,
                                       "deleted_texts": ["a", "b"]})
    assert "note" not in _invoke("memory_forget",
                                 {"scope": "memory", "source": "s"})


def test_unsafe_source_url_note_says_http_is_required(
        tmp_path: Path, monkeypatch) -> None:
    """Refused before the service takes its lock, so the real file-mode
    service answers without loading a model."""
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_world_set", {"entity": "e", "attribute": "a",
                                       "value": "v",
                                       "source_url": "javascript:alert(1)"})
    assert out["reason"] == "unsafe_source_url"
    assert "http(s)" in out["note"]


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_consolidate"])
def test_correction_refusals_use_reason_and_note(
        tmp_path: Path, monkeypatch, tool: str) -> None:
    """memory_supersede used to invert the convention: ``reason`` held the
    code and ``error`` the sentence. The MCP result now matches every other
    success-shaped refusal (code in ``reason``, sentence in ``note``); the
    Console's REST route keeps the service's ``error`` key."""
    mod = _reload(tmp_path, monkeypatch)
    sentence = "No entries changed. Resolve the reported targets."
    answer = mod.service._correction_noop(
        "target_not_found", sentence,
        [{"entry_id": 9, "reason": "target_not_found"}])
    method = "supersede" if tool == "memory_supersede" else "consolidate"
    _with(mod, monkeypatch, method, answer)
    args = ({"entry_id": 9, "new_text": "n"} if tool == "memory_supersede"
            else {"entry_ids": [9], "new_text": "n"})
    out = _invoke(tool, args)
    assert out["reason"] == "target_not_found"
    assert out["note"] == sentence and "error" not in out
    assert out["target_errors"] == answer["target_errors"]
    assert mod.service._correction_noop("x", "y")["error"] == "y"
