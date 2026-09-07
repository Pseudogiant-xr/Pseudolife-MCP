"""Unit tests for the Cognee adapter's pure parts (no GPU, no BEAM data,
no Cognee install).

``evals/cognee_adapter.py`` runs in its own venv and cannot import
``beam_adapter`` (torch + the live service at module scope), so it carries
VERBATIM copies of the shared helpers — the answer prompt, the judge, the
turn stamp, the chat loaders. The instrument-matching claim ("same
answerer, same judge, same stamping as the committed BEAM rows") is only as
good as those copies staying identical, so the first test here holds each
one AST-identical to its origin. The rest pin the adapter's own machinery:
the whole-result budget fit, envelope unwrapping, and the whole-chat resume
rule.

The module imports ``cognee`` at import time and edits ``os.environ`` for
it; the fixture stubs the package and restores the environment, so nothing
here needs Cognee installed and nothing leaks into later tests (the module
pops ``HF_HUB_OFFLINE``, which the rest of the suite relies on).
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_EVALS = _REPO / "evals"
_ADAPTER = _EVALS / "cognee_adapter.py"

# (name, origin file) — every helper the adapter duplicates.
_DUPLICATED = {
    "beam_adapter.py": ("_BEAM_ANSWER_SYSTEM", "load_judge_prompt", "iter_chats",
                        "load_chat_turns", "load_questions", "_SCORE_RE",
                        "parse_judge_score", "judge_response", "format_turn"),
    "longmemeval_bench.py": ("_chat", "_THINKING_LEVELS", "_SAMPLER_PROTECTED"),
}

_ENV_KEYS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "LLM_PROVIDER",
             "LLM_MODEL", "LLM_ENDPOINT", "LLM_API_KEY", "LLM_INSTRUCTOR_MODE",
             "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
             "TELEMETRY_DISABLED")


def _top_nodes(text: str) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n
    return out


@pytest.mark.parametrize("origin,names", sorted(_DUPLICATED.items()))
def test_every_duplicated_helper_is_ast_identical_to_its_origin(origin, names):
    """Source-level, no import: a docstring edit counts too, because the
    docstrings are where the instrument's rationale lives and a diverging
    one is how the copies start to drift."""
    adapter = _top_nodes(_ADAPTER.read_text(encoding="utf-8"))
    src = _top_nodes((_EVALS / origin).read_text(encoding="utf-8"))
    for name in names:
        assert name in adapter, f"{name} missing from cognee_adapter.py"
        assert name in src, f"{name} missing from {origin}"
        assert ast.dump(adapter[name]) == ast.dump(src[name]), (
            f"cognee_adapter.{name} has drifted from {origin} — copy the "
            "origin verbatim; the Cognee row is only instrument-matched while "
            "these are identical")


class _SearchType:
    CHUNKS = "CHUNKS"
    CHUNKS_LEXICAL = "CHUNKS_LEXICAL"
    SUMMARIES = "SUMMARIES"
    TEMPORAL = "TEMPORAL"


class _StubCognee(types.ModuleType):
    """Records config/add/cognify/search calls; search returns what the test
    queued (cognee 1.5 envelopes included)."""

    def __init__(self):
        super().__init__("cognee")
        self.SearchType = _SearchType
        self.calls: list[tuple] = []
        self.search_results: list = []
        stub = self

        class _Config:
            @staticmethod
            def data_root_directory(p):
                stub.calls.append(("data_root", p))

            @staticmethod
            def system_root_directory(p):
                stub.calls.append(("system_root", p))

        self.config = _Config

    async def add(self, doc, dataset_name=None):
        self.calls.append(("add", dataset_name, doc))

    async def cognify(self, datasets):
        self.calls.append(("cognify", tuple(datasets)))

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return self.search_results


@pytest.fixture
def adapter(monkeypatch):
    """Import the adapter against a stub ``cognee`` with the environment
    snapshotted and restored — the module's import-time env edits must not
    reach the rest of the suite."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    stub = _StubCognee()
    monkeypatch.setitem(sys.modules, "cognee", stub)
    spec = importlib.util.spec_from_file_location("cognee_adapter_under_test",
                                                  _ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        mod._stub = stub
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_import_pops_the_offline_flags_and_sets_cognee_defaults(monkeypatch):
    """The adapter pops HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE at import so
    fastembed can fetch its one-time model, and setdefaults Cognee's LLM env
    to the bench server. Both are import-time side effects, so they are
    pinned by a fresh import here — and it is exactly why the ``adapter``
    fixture snapshots and restores the environment for every other test."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("LLM_ENDPOINT", "http://operator-export:9/v1")
        monkeypatch.setitem(sys.modules, "cognee", _StubCognee())
        spec = importlib.util.spec_from_file_location("cognee_adapter_env_probe",
                                                      _ADAPTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "HF_HUB_OFFLINE" not in os.environ
        assert "TRANSFORMERS_OFFLINE" not in os.environ
        assert os.environ["LLM_PROVIDER"] == "custom"          # defaulted
        assert os.environ["LLM_ENDPOINT"] == "http://operator-export:9/v1"  # export wins
        assert mod.SEARCH_TYPES["chunks"] == "CHUNKS"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── budget fit: whole results, rank-prefix per type, no type starved ──────

def test_fit_to_budget_serves_whole_results_within_the_cap(adapter):
    retrieved = {"chunks": ["c" * 40, "d" * 40], "summaries": ["s" * 40]}
    order = ["chunks", "summaries"]
    cap = len(adapter._assemble({"chunks": ["c" * 40], "summaries": ["s" * 40]},
                                order))
    admitted = adapter._fit_to_budget(retrieved, order, cap)
    assert admitted == {"chunks": ["c" * 40], "summaries": ["s" * 40]}
    assert len(adapter._assemble(admitted, order)) <= cap


def test_fit_to_budget_is_a_rank_prefix_per_type(adapter):
    """Once rank k of a type does not fit, rank k+1 is NOT admitted even if
    it would — serving rank 3 because rank 2 was fat would rewrite the
    system's own ranking under the guise of budgeting."""
    small, fat, tiny = "a" * 20, "b" * 500, "c" * 5
    retrieved = {"chunks": [small, fat, tiny]}
    cap = len(adapter._assemble({"chunks": [small, tiny]}, ["chunks"]))
    admitted = adapter._fit_to_budget(retrieved, ["chunks"], cap)
    assert admitted == {"chunks": [small]}


def test_fit_to_budget_interleaves_types_so_none_is_starved(adapter):
    """A wide first type cannot eat the whole budget before the second is
    consulted: ranks are admitted round-robin across types."""
    retrieved = {"chunks": ["x" * 30] * 10, "summaries": ["y" * 30] * 10}
    order = ["chunks", "summaries"]
    cap = len(adapter._assemble({"chunks": ["x" * 30] * 2,
                                 "summaries": ["y" * 30] * 2}, order))
    admitted = adapter._fit_to_budget(retrieved, order, cap)
    assert len(admitted["chunks"]) == 2 and len(admitted["summaries"]) == 2


def test_fit_to_budget_zero_means_uncapped(adapter):
    retrieved = {"chunks": ["a", "b"], "summaries": ["c"]}
    assert adapter._fit_to_budget(retrieved, ["chunks", "summaries"], 0) == retrieved


# ── search-result handling ───────────────────────────────────────────────

def test_flatten_results_unwraps_cognee_envelopes(adapter):
    """cognee 1.5 wraps hits per dataset; the first smoke served a 355K-char
    envelope as one result. Envelopes are unwrapped, bare hits pass through."""
    results = [{"dataset_id": 1, "search_result": ["hit1", {"text": "hit2"}]},
               "bare"]
    assert adapter._flatten_results(results) == ["hit1", {"text": "hit2"}, "bare"]
    assert adapter._flatten_results(None) == []


def test_render_result_handles_strings_dicts_and_triplets(adapter):
    assert adapter._render_result("plain") == "plain"
    assert adapter._render_result({"text": "t", "other": 1}) == "t"
    assert adapter._render_result({"summary": "s"}) == "s"
    assert adapter._render_result(("a", {"chunk": "b"})) == "a — b"
    assert json.loads(adapter._render_result({"k": 1})) == {"k": 1}


def test_batch_documents_is_one_document_per_beam_batch_in_stamped_form(adapter):
    turns = [
        {"batch": 1, "time_anchor": "2024-01-01", "role": "user", "content": "hi"},
        {"batch": 1, "time_anchor": None, "role": "assistant", "content": "yo"},
        {"batch": 2, "time_anchor": None, "role": "user", "content": "later"},
    ]
    docs = adapter.batch_documents(turns, None)
    assert docs == [
        "[2024-01-01] [session 1, turn 1] user: hi\n[session 1, turn 2] assistant: yo",
        "[session 2, turn 3] user: later",
    ]
    assert adapter.batch_documents(turns, 1) == docs[:1]


# ── resume rule: the whole chat, never a partial bank ────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_ingest_reuses_a_marker_for_the_same_batch_count(adapter, tmp_path):
    root = tmp_path / "chat7"
    root.mkdir()
    (root / ".cognified").write_text(json.dumps({"batches": 2, "cognify_seconds": 9}))
    tally = _run(adapter.ingest_chat("7", ["d1", "d2"], tmp_path))
    assert tally == {"batches": 2, "reused": True, "cognify_seconds": 9}
    assert not any(c[0] in ("add", "cognify") for c in adapter._stub.calls)
    # The redirect is the load-bearing half of reuse: without it, searches
    # would hit cognee's default root, not the reused bank.
    assert ("data_root", str(root / "data")) in adapter._stub.calls
    assert ("system_root", str(root / "system")) in adapter._stub.calls


def test_ingest_discards_a_marker_for_fewer_batches_than_requested(adapter, tmp_path):
    """The 2026-09-01 smoke left a 2-of-3-batch bank behind; reusing it would
    serve two thirds of a chat while the row claimed a whole one."""
    root = tmp_path / "chat7"
    (root / "data").mkdir(parents=True)
    (root / "data" / "old").write_text("x")
    (root / ".cognified").write_text(json.dumps({"batches": 2}))
    tally = _run(adapter.ingest_chat("7", ["d1", "d2", "d3"], tmp_path))
    assert tally["reused"] is False and tally["batches"] == 3
    assert tally["discarded_partial"] is True, "the audit flag must ride the row"
    assert not (root / "data" / "old").exists(), "stale partial bank survived"
    adds = [c for c in adapter._stub.calls if c[0] == "add"]
    assert [c[2] for c in adds] == ["d1", "d2", "d3"]
    assert ("cognify", ("beam_chat_7",)) in adapter._stub.calls
    assert json.loads((root / ".cognified").read_text())["batches"] == 3


def test_ingest_wipes_a_populated_root_that_has_no_marker(adapter, tmp_path):
    """No marker + populated root = a cognify that died mid-flight. cognee.add
    is additive, so resuming onto it would double part of the corpus."""
    root = tmp_path / "chat3"
    (root / "system").mkdir(parents=True)
    (root / "system" / "half").write_text("x")
    tally = _run(adapter.ingest_chat("3", ["d1"], tmp_path))
    assert not (root / "system" / "half").exists()
    assert (root / ".cognified").exists()
    assert tally["discarded_partial"] is False   # no marker was discarded


def test_build_context_records_when_the_legacy_search_signature_was_used(adapter):
    """The old positional ``search(query_type, query_text)`` drops the
    dataset scope and ``top_k``; a row served that way must say so rather
    than record a ``top_k`` it never applied. A TypeError raised INSIDE
    cognee's search must not be mistaken for the old signature."""
    stub = adapter._stub
    stub.search_results = ["hit"]
    ctx, meta = _run(adapter.build_context("1", "q?", ["chunks"], 0, 5))
    assert meta["search_legacy_signature"] is False
    assert stub.calls[-1][1]["top_k"] == 5 and stub.calls[-1][1]["datasets"] == ["beam_chat_1"]

    async def old_style(*args, **kwargs):
        if kwargs:
            raise TypeError("search() got an unexpected keyword argument 'datasets'")
        return ["legacy-hit"]
    stub.search = old_style
    ctx, meta = _run(adapter.build_context("1", "q?", ["chunks"], 0, 5))
    assert meta["search_legacy_signature"] is True and "legacy-hit" in ctx

    async def broken(**kwargs):
        raise TypeError("unsupported operand type(s) inside cognee")
    stub.search = broken
    with pytest.raises(TypeError, match="inside cognee"):
        _run(adapter.build_context("1", "q?", ["chunks"], 0, 5))


def test_default_work_dir_is_under_the_gitignored_banks_tree(adapter):
    """The first smoke defaulted to a root-level scratch dir that then sat
    untracked in the checkout for a week."""
    wd = adapter.default_work_dir("100K", "smoke")
    assert wd == _EVALS / "results" / "banks" / "cognee-100K-smoke"
    ignore = (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "evals/results/banks/" in ignore
    assert ".venv-cognee/" in ignore, "the adapter's venv must stay out of the tree"


def test_search_types_are_retrieval_only(adapter):
    """Completion modes answer with Cognee's own LLM and would un-match the
    instrument; the whitelist must never grow one."""
    assert not any("COMPLETION" in v for v in adapter.SEARCH_TYPES.values())
    assert set(adapter.SEARCH_TYPES) == {"chunks", "chunks_lexical",
                                         "summaries", "temporal"}
