"""Shared Codex consent cases against the retained Python oracle.

Only the pure oracle function is loaded: no client home, credentials, package
initialization, CLI, network endpoint or installed registration is consulted.
The Rust unit selection cli::update::clients::tests::shared_consent_cases_match_oracle
reads the same independently specified cases.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((Path(__file__).parent / "fixtures/native_codex_consent.json").read_text(encoding="utf-8"))


def _oracle():
    path = ROOT / "pseudolife_memory/client_updates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_codex_hook_manifest")
    scope = {"json": json}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    return scope["_codex_hook_manifest"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_shared_consent_case_matches_python_oracle(case):
    actual = _oracle()(case["input"].encode("utf-8"))
    expected = case["normalized"]
    if expected is None:
        assert actual is None
    else:
        assert actual == json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
