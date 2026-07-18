"""Auto-outcome inference (spec 2026-07-18): parser + config units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.memory.dream import _parse_outcome_claims
from pseudolife_memory.utils.config import LessonsConfig


def test_parse_outcome_claims_happy_path():
    content = ('noise before {"outcomes": [{"task": "deploy daemon", '
               '"outcome": "success", "about": "ops/update.ps1", '
               '"detail": "health check passed"}]} noise after')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "deploy daemon", "outcome": "success",
                       "about": "ops/update.ps1",
                       "detail": "health check passed"}]


def test_parse_outcome_claims_refuses_bad_enum_and_empty_task():
    content = ('{"outcomes": ['
               '{"task": "x", "outcome": "failed"},'
               '{"task": "", "outcome": "success"},'
               '{"task": "y", "outcome": "correction"}]}')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "y", "outcome": "correction",
                       "about": None, "detail": None}]


def test_parse_outcome_claims_cap():
    items = ",".join(f'{{"task": "t{i}", "outcome": "success"}}'
                     for i in range(5))
    claims = _parse_outcome_claims(f'{{"outcomes": [{items}]}}', cap=2)
    assert [c["task"] for c in claims] == ["t0", "t1"]


def test_parse_outcome_claims_malformed_vs_empty():
    assert _parse_outcome_claims("total garbage", cap=3) is None
    assert _parse_outcome_claims('{"wrong_key": []}', cap=3) is None
    assert _parse_outcome_claims('{"outcomes": []}', cap=3) == []


def test_lessons_config_defaults():
    cfg = LessonsConfig()
    assert cfg.infer_outcomes is True
    assert cfg.infer_outcomes_max_signals == 3
