"""The CLI shim's extraction prompt carries the shipped assistant-facts ask.

``evals/claude_shim.py --system-prompt-file`` REPLACES the shipped
``dream._SYSTEM_PROMPT`` prefix. On an install whose primary extractor is
the shim — the default since the 2026-07-11 cutover, and what ``ops/.env``
selects via ``PSEUDOLIFE_DREAM_BASE_URL=...:8082/v1`` — an instruction
added to ``_SYSTEM_PROMPT`` therefore never reaches the model. The
assistant-facts blocks that shipped on 2026-09-05 landed in that blind
spot; ``evals/prompts/sonnet_extractor_v4.md`` closes it.

These tests pin the two properties that make the fix real rather than a
copy: the file is v2 plus the SHIPPED blocks (imported, not retyped), and
the example-token dataset guard covers it, so the shim prompt cannot smuggle
a benchmark answer into the extractor either.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROMPTS = _REPO / "evals" / "prompts"


def _gen():
    sys.path.insert(0, str(_REPO / "evals"))
    return importlib.import_module("gen_shim_prompt")


def _read(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


# ── composition: v4 is v2 plus the shipped blocks ────────────────────────

def test_the_shim_prompt_is_v2_plus_the_shipped_assistant_blocks():
    """The load-bearing pin. Composed from ``dream.py``'s own constants, so
    an edit to the shipped ask that does not reach the shim file fails here
    rather than silently leaving the shim path on the old instruction."""
    from pseudolife_memory.memory.dream import (
        _ASSISTANT_FACTS_INSTRUCTION,
        _ASSISTANT_PROVENANCE_EXAMPLE,
        _ASSISTANT_SPEAKER_RULE,
    )

    g = _gen()
    tail = (_ASSISTANT_FACTS_INSTRUCTION + _ASSISTANT_SPEAKER_RULE
            + _ASSISTANT_PROVENANCE_EXAMPLE)
    body = g._split_body(_read(g.OUT_NAME))
    base = g._split_body(_read(g.BASE_NAME))
    assert body == (base + "\n\n" + tail).strip(), (
        "sonnet_extractor_v4.md is no longer the v2 body plus the shipped "
        "assistant-facts blocks — re-run "
        "`PYTHONPATH=. python evals/gen_shim_prompt.py`")


def test_what_the_shim_would_send_carries_every_shipped_block_verbatim():
    """Composition asserted through the shim's OWN parse rule: a separator
    or strip() change that broke the body extraction would leave the file
    correct and the wire wrong."""
    from pseudolife_memory.memory.dream import (
        _ASSISTANT_FACTS_INSTRUCTION,
        _ASSISTANT_PROVENANCE_EXAMPLE,
        _ASSISTANT_SPEAKER_RULE,
    )

    # exactly what claude_shim.main() computes for --system-prompt-file
    override = _read("sonnet_extractor_v4.md").split("\n---\n", 1)[-1].strip()
    for block in (_ASSISTANT_FACTS_INSTRUCTION, _ASSISTANT_SPEAKER_RULE,
                  _ASSISTANT_PROVENANCE_EXAMPLE):
        assert block.strip() in override, (
            "a shipped assistant-facts block does not survive the shim's "
            "own body extraction")
    assert "DOCUMENTS PRESCRIBE" in override, "the v2 body was lost"


def test_regenerating_would_reproduce_the_shim_prompt_exactly():
    """The file is generated, so the generator is the thing under test.
    Compared against the generator's composition rather than by re-running
    it — a hand-edited file fails here instead of being overwritten by the
    guard meant to catch it. Text, not bytes: ``core.autocrlf`` is true on
    Windows, so the checkout's line endings are not the blob's."""
    g = _gen()
    assert _read(g.OUT_NAME) == g.compose(), (
        f"{g.OUT_NAME} differs from what the generator would write — re-run "
        "`PYTHONPATH=. python evals/gen_shim_prompt.py`")


def test_importing_the_generator_does_not_write_anything():
    """``gen_assistant_facts_prompts`` used to write on import, so any suite
    importing it regenerated the committed artifacts underneath its own
    assertions (found 2026-09-05). This generator must not repeat it."""
    names = ("sonnet_extractor_v2.md", "sonnet_extractor_v4.md")
    stamps = {n: (_PROMPTS / n).stat().st_mtime_ns for n in names}
    # reload, not import: import_module is cached, and a cached module would
    # pass this without ever re-running the body.
    importlib.reload(_gen())
    assert {n: (_PROMPTS / n).stat().st_mtime_ns for n in names} == stamps


def test_v2_stays_the_unguarded_production_comparator():
    """v2 is what the deployed autostart shipped when the gate ran, and the
    pre arm of that gate. It must not acquire the speaker rule by a stray
    edit, or the comparison stops being pre-vs-post."""
    assert "speaker" not in _read("sonnet_extractor_v2.md")


def test_the_unadopted_v3_lineage_is_left_alone():
    """``sonnet_extractor_v3.md`` is a different 2026-08-02 lineage
    (coverage mandates) that was never adopted as the shim default. The
    provenance work stacks on v2 deliberately; this pins that v3 was not
    quietly repurposed as the carrier."""
    v3 = _read("sonnet_extractor_v3.md")
    assert "speaker" not in v3
    assert "coverage mandates" in v3


# ── the example-token guard extends to the shim prompt ───────────────────
#
# tests/test_assistant_provenance.py greps every registered EXAMPLE_TOKENS
# entry against both LongMemEval dataset files, and pins that the SHIPPED
# prompt carries them so the grep guards what runs. The shim prompt is now a
# second live carrier of the same worked example, on the path the
# maintainer's own install actually extracts through — so it needs the same
# coverage, or the guard would be blind to exactly the prompt in production.

def _tokens():
    sys.path.insert(0, str(_REPO / "evals"))
    return importlib.import_module("gen_assistant_facts_prompts").EXAMPLE_TOKENS


def test_the_shim_prompt_is_covered_by_the_example_token_guard():
    text = _read("sonnet_extractor_v4.md")
    missing = [t for t in _tokens() if t not in text]
    assert missing == [], (
        f"registered token(s) absent from the shim prompt: {missing} — the "
        "dataset grep would not be guarding the shim extraction path")


@pytest.mark.parametrize("dataset", ["longmemeval_oracle.json",
                                     "longmemeval_s_cleaned.json"])
def test_no_shim_prompt_proper_noun_occurs_in_the_measured_dataset(dataset):
    """Evidence half, restated for this carrier: nothing the shim prompt
    names may appear in the corpus the ladder and the LongMemEval runs
    measure. Streamed with an overlap — the ``_s`` file is ~277 MB."""
    path = _REPO / "evals" / "data" / dataset
    if not path.exists():
        pytest.skip(f"{dataset} not present (evals/data is gitignored)")

    text = _read("sonnet_extractor_v4.md")
    tokens = [t.encode("utf-8") for t in _tokens() if t in text]
    assert tokens, "no registered token reaches the shim prompt — vacuous"
    overlap = max(len(t) for t in tokens)
    counts = {t: 0 for t in tokens}
    prev = b""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = prev + chunk
            for t in tokens:
                counts[t] += buf.count(t)
            prev = buf[-overlap:]
    hits = {t.decode(): n for t, n in counts.items() if n}
    assert hits == {}, (
        f"shim-prompt token(s) occur in {dataset}: {hits} — the prompt is "
        "naming content the bench measures")
