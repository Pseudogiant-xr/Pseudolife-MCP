"""Pin the shipped extraction prompt's LME-V2-transfer properties.

Two lessons from the 2026-07-20 LongMemEval-V2 arc, applied to the product
prompt (``pseudolife_memory/memory/dream.py::_SYSTEM_PROMPT``):

1. *Documented vs enacted* — a scope-restricted extraction prompt makes the
   model silently discard content classes it doesn't name (LME-V2 Fix E: the
   trajectory prompt's "exactly two kinds and nothing else" dropped protocol
   documents entirely, costing every procedure answer). The shipped prompt
   must name document-stated prescriptions as extractable.
2. *Worked example* — small extractors (the bundled E4B student, qwen-27b)
   follow a demonstrated format far more reliably than imperative
   instructions (two ignored "no explanations" directives vs first-try
   compliance with a tiny example). The example's JSON must actually parse
   to the claims schema, so a prompt edit can't silently demonstrate a
   malformed shape.
"""
from __future__ import annotations

import json
import re

from pseudolife_memory.memory.dream import _SYSTEM_PROMPT


def test_prompt_names_document_prescriptions():
    """A DOCUMENT's prescriptive content must be named as extractable —
    unnamed content classes get silently discarded by obedient extractors."""
    assert re.search(r"document", _SYSTEM_PROMPT, re.I), (
        "_SYSTEM_PROMPT must tell the extractor that document-stated "
        "prescriptions (specs/policies/protocols) are extractable facts, "
        "distinct from what happened in the session (LME-V2 Fix-E lesson)")


def test_prompt_carries_parseable_worked_example():
    """The prompt must demonstrate the output format with an example whose
    JSON parses to the claims schema (entity/attribute/value/confidence/
    source) — the reliable format-compliance lever for small extractors."""
    m = re.search(r"Example\b.*?(\{\"claims\":\[.*?\]\})", _SYSTEM_PROMPT,
                  re.S)
    assert m, "_SYSTEM_PROMPT must contain a worked example with a claims JSON"
    obj = json.loads(m.group(1))
    claims = obj["claims"]
    assert claims, "the worked example must demonstrate at least one claim"
    for c in claims:
        assert set(c) >= {"entity", "attribute", "value", "confidence",
                          "source"}, f"example claim missing schema keys: {c}"
        assert isinstance(c["source"], int)
        assert 0 <= c["confidence"] <= 1
