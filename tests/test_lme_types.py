"""Unit tests for the LongMemEval --types extension (2026-08-02 design doc).

Pure, offline: type parsing, artifact-name slugging (the KU default must
stay byte-identical to pre-extension filenames), question filtering, and
per-type judge-prompt selection including the no-question_type fallback
that keeps old artifacts re-judging byte-identically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from longmemeval_bench import (  # noqa: E402
    ALL_TYPES, DEFAULT_TYPES, _JUDGE_SYSTEM, _JUDGE_SYSTEM_GENERIC,
    bank_dir, out_file, parse_types, types_slug,
)


def test_parse_types_default_and_all():
    assert parse_types("") == DEFAULT_TYPES
    assert parse_types("knowledge-update") == DEFAULT_TYPES
    assert parse_types("all") == ALL_TYPES


def test_parse_types_comma_list_and_unknown():
    assert parse_types("multi-session,temporal-reasoning") == (
        "multi-session", "temporal-reasoning")
    with pytest.raises(SystemExit):
        parse_types("knowledge-update,not-a-type")


def test_types_slug():
    assert types_slug(DEFAULT_TYPES) == "ku"
    assert types_slug(ALL_TYPES) == "all"
    # order-insensitive full set is still "all"
    assert types_slug(tuple(reversed(ALL_TYPES))) == "all"
    assert types_slug(("multi-session", "temporal-reasoning")) == "ms-tr"


def test_default_artifact_names_are_byte_identical():
    # The canonical KU filenames must not move under the extension.
    assert out_file("oracle", "sonnet-5", "smoke0726-qwenjudge").name == \
        "longmemeval-ku-oracle-sonnet-5-smoke0726-qwenjudge.jsonl"
    assert bank_dir("oracle", "sonnet-5", "x").name == "oracle-sonnet-5-x"


def test_extended_artifact_names_carry_the_slug():
    assert out_file("oracle", "opus-5", "t1", "all").name == \
        "longmemeval-all-oracle-opus-5-t1.jsonl"
    assert bank_dir("oracle", "opus-5", "t1", "all").name == "all-oracle-opus-5-t1"


def test_judge_prompt_selection_semantics():
    # KU keeps the update clause; the generic judge drops ONLY that clause
    # and keeps equivalence + abstention verbatim.
    assert "updated knowledge" in _JUDGE_SYSTEM
    assert "updated knowledge" not in _JUDGE_SYSTEM_GENERIC
    for shared in ("contains or is equivalent",
                   "grade yes only if the response abstains"):
        assert shared in _JUDGE_SYSTEM
        assert shared in _JUDGE_SYSTEM_GENERIC


def test_missing_question_type_falls_back_to_ku_judge():
    # answer_and_judge picks the judge from row.get("question_type",
    # "knowledge-update") — pin the fallback expression itself so a
    # refactor cannot silently re-judge old artifacts with the generic
    # prompt.
    src = (Path(__file__).resolve().parents[1] / "evals"
           / "longmemeval_bench.py").read_text(encoding="utf-8")
    assert 'row.get("question_type", "knowledge-update")' in src
