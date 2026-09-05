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
every name-shaped phrase in the prompt body is either a registered invented
token or recorded debt inherited from v2.

That second property is weaker than it first read. The guard originally
grepped only the four registered ``EXAMPLE_TOKENS`` while its docstring
claimed the whole prompt, and the inherited v2 body carries worked examples
of its own — one of which names a LongMemEval answer.

The debt is recorded rather than hidden, in ``KNOWN_CORPUS_COLLISIONS``
(``evals/gen_assistant_facts_prompts.py``). It is **not** a shim-only debt:
the same example is in ``dream._SYSTEM_PROMPT``, so every extractor has had
it, and ``tests/test_assistant_provenance.py`` runs the same two halves over
the shipped prompt. v2 cannot be re-cut here (it is the pre arm of a
committed gate) and re-cutting the shipped prompt is a prompt change needing
its own ladder gate, so neither is attempted in the change that recorded it.
"""
from __future__ import annotations

import importlib
import re
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


# ── nothing the shim prompt names may occur in the measured corpus ───────
#
# tests/test_assistant_provenance.py greps every registered EXAMPLE_TOKENS
# entry against both LongMemEval dataset files, and pins that the SHIPPED
# prompt carries them so the grep guards what runs. The shim prompt is a
# second live carrier of the same worked example, on the path the
# maintainer's own install actually extracts through — so it needs the same
# coverage, or the guard would be blind to exactly the prompt in production.
#
# It needs MORE than that coverage, though, and the first cut of this file
# did not have it. The registered tokens are the names the 2026-09-05
# provenance example invented; v4 is those blocks appended to the whole v2
# body, whose own examples were written long before the rule existed. The
# scan below therefore runs over every capitalised phrase in the body.

def _tokens():
    sys.path.insert(0, str(_REPO / "evals"))
    return importlib.import_module("gen_assistant_facts_prompts").EXAMPLE_TOKENS


def test_the_shim_prompt_is_covered_by_the_example_token_guard():
    text = _read("sonnet_extractor_v4.md")
    missing = [t for t in _tokens() if t not in text]
    assert missing == [], (
        f"registered token(s) absent from the shim prompt: {missing} — the "
        "dataset grep would not be guarding the shim extraction path")


# Ordinary sentence-initial capitals in the v2 body and the shipped blocks.
# Same role as ``_SENTENCE_CAPITALS`` in ``tests/test_assistant_provenance``:
# anything Titlecase that is NOT one of these has to be accounted for. Pinned
# below (``test_every_exempt_capital_is_really_sentence_initial``) — this is
# the one list a careless edit could use to make a real name disappear from
# BOTH halves of the guard.
_SENTENCE_CAPITALS = frozenset({
    "Before", "Do", "Emit", "Example", "Extract", "Facts", "For", "Key",
    "Never", "Notes", "One", "Output", "Precision", "Prefer", "Return",
    "What", "When", "Within", "You",
})

# Titlecase phrase: a capitalised word (ASCII or accented initial — the
# corpus carries accented names), optionally possessive, optionally chained.
#
# ALL-CAPS runs are deliberately OUT OF SCOPE, said here rather than implied
# away, because the whole point of this fold is that a guard claiming more
# than it checks is worse than a narrow one. This prompt family uses ALL-CAPS
# for emphasis throughout ("RECALL FIRST", "COUNTS, TOTALS, AND QUANTITIES",
# "OMIT"), so a caps-inclusive scan needs a ~20-entry exemption list per
# prompt that would rot faster than it guards. A name written in caps, in
# lower case, or built from digits therefore escapes both halves. Titlecase
# is how names are written in these prompts and in the corpus alike.
_PHRASE = re.compile(
    r"[A-Z\u00C0-\u00DE][a-z\u00DF-\u00FF]+(?:['\u2019][A-Za-z]+)?"
    r"(?:\s+[A-Z\u00C0-\u00DE][a-z\u00DF-\u00FF]+(?:['\u2019][A-Za-z]+)?)*")


def _gen_module():
    sys.path.insert(0, str(_REPO / "evals"))
    return importlib.import_module("gen_assistant_facts_prompts")


# ── the pre-rule worked examples, inherited from the v2 body ─────────────
#
# v4 is v2 plus the shipped blocks, and v2 came with worked examples of its
# own that predate the invented-token rule. v2 CANNOT be re-cut here: it is
# the pre arm of a committed gate
# (`ladder-shimprompt-rule2-paired-verdict-threshold.json`), so editing it
# would retroactively change what that gate compared.
#
# The lists themselves live beside `EXAMPLE_TOKENS` in
# `evals/gen_assistant_facts_prompts.py`, NOT here, because the same two
# names are in `dream._SYSTEM_PROMPT` — this is not a shim-only debt, and two
# copies of an allowlist is how one of them goes stale.
# `tests/test_assistant_provenance.py` runs the same two halves over the
# shipped prompt.
def _pre_rule_names():
    return frozenset(_gen_module().PRE_RULE_PROPER_NOUNS)


def _known_collisions():
    return dict(_gen_module().KNOWN_CORPUS_COLLISIONS)


def _proper_nouns(text: str) -> set[str]:
    """Every name-shaped phrase the prompt BODY carries, minus registered
    tokens and ordinary sentence English.

    The body, not the file: everything above ``gen_shim_prompt.SEPARATOR``
    is provenance documentation the shim strips before the model ever sees
    it (``claude_shim.main``), and scanning it only reports the prose of
    this repo ("Gate", "Max", "Sonnet") as if it were prompt content."""
    text = _gen()._split_body(text)
    for token in _tokens():
        text = text.replace(token, " ")
    return {m.group(0) for m in _PHRASE.finditer(text)
            if m.group(0) not in _SENTENCE_CAPITALS}


@pytest.mark.parametrize("dataset", ["longmemeval_oracle.json",
                                     "longmemeval_s_cleaned.json"])
def test_no_shim_prompt_proper_noun_occurs_in_the_measured_dataset(dataset):
    """Evidence half, restated for this carrier and over the WHOLE body.

    The earlier version grepped only the four registered ``EXAMPLE_TOKENS``,
    which are the names the 2026-09-05 provenance example invented. That
    guarded the block this change added and nothing else, while the
    docstring claimed the whole prompt — and the prompt is mostly INHERITED
    v2 text with worked examples of its own. Scanning every capitalised
    phrase is what makes the claim and the check the same statement.

    Streamed with an overlap — the ``_s`` file is ~277 MB."""
    path = _REPO / "evals" / "data" / dataset
    if not path.exists():
        pytest.skip(f"{dataset} not present (evals/data is gitignored)")

    text = _read("sonnet_extractor_v4.md")
    assert [t for t in _tokens() if t in text], (
        "no registered token reaches the shim prompt — vacuous")
    phrases = sorted(_proper_nouns(text))
    assert phrases, "no proper noun found in the shim prompt — vacuous"
    probes = [p.encode("utf-8") for p in phrases]
    overlap = max(len(p) for p in probes)
    counts = {p: 0 for p in probes}
    prev = b""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(8 << 20)
            if not chunk:
                break
            buf = prev + chunk
            for p in probes:
                counts[p] += buf.count(p)
            prev = buf[-overlap:]
    hits = {p.decode(): n for p, n in counts.items() if n}
    # Equality, not a subset: a NEW contaminated phrase fails, and so does an
    # allowlist entry that has stopped hitting. The debt cannot grow and
    # cannot rot into decoration.
    assert sorted(hits) == sorted(_known_collisions()), (
        f"shim-prompt phrase(s) occur in {dataset}: {hits}\n"
        f"known and allowed: {sorted(_known_collisions())}\n"
        "A phrase not on that list means the prompt is naming content the "
        "bench measures — re-cut the example on invented names. A listed "
        "phrase that no longer hits means the debt is paid: delete its "
        "entry.")


def test_every_proper_noun_in_the_shim_prompt_is_registered_or_known_debt():
    """Structural half — runs with or without the datasets.

    A proper noun added anywhere in the shim prompt must either be a
    registered ``EXAMPLE_TOKENS`` entry (which subjects it to the dataset
    grep above) or a phrase already inherited from the v2 body. There is no
    third option, so new contamination cannot be introduced silently."""
    leftovers = _proper_nouns(_read("sonnet_extractor_v4.md"))
    assert leftovers, (
        "no name-shaped phrase found at all — the scan has "
        "degenerated and this test would pass on anything")
    unaccounted = sorted(leftovers - _pre_rule_names())
    assert unaccounted == [], (
        f"unregistered proper noun(s) in the shim prompt: {unaccounted} — "
        "add them to EXAMPLE_TOKENS (which grep-checks them against the "
        "LongMemEval datasets) or rewrite the example")


def test_the_inherited_contamination_allowlist_describes_the_v2_body():
    """The allowlists must stay statements about v2, not a place to park
    anything inconvenient. Every entry has to occur in the v2 body itself,
    and the contamination list has to be a subset of what v2 contributes —
    so a phrase introduced by the SHIPPED blocks can never be excused as
    inherited."""
    v2 = _gen()._split_body(_read("sonnet_extractor_v2.md"))
    absent = sorted(p for p in _pre_rule_names() if p not in v2)
    assert absent == [], (
        f"allowlisted as inherited but absent from the v2 body: {absent}")
    extra = sorted(set(_known_collisions())
                   - _pre_rule_names())
    assert extra == [], (
        f"contamination allowlist names non-v2 phrase(s): {extra}")


# ── the header must cite the gate that validated this file ───────────────

_GATE_RULE2 = ("evals/results/"
               "ladder-shimprompt-rule2-paired-verdict-threshold.json")
_GATE_RULE1 = "evals/results/ladder-shimprompt-paired-verdict-threshold.json"


def test_the_shim_prompt_header_cites_the_gate_that_validated_it():
    """The generated header is where a reader of the prompt itself learns
    which run justified it, and it drifted exactly as the `ops/` launchers
    did (2026-09-05 merge review): the body is generated from
    `_ASSISTANT_SPEAKER_RULE`, the rule-v2 rewrite changed that constant, so
    the rule-v1 verdict describes a body this file no longer has. The
    superseded verdict may still be NAMED — it keeps its evidence — but not
    as the path a reader is pointed at."""
    g = _gen()
    header = _read(g.OUT_NAME).split(g.SEPARATOR, 1)[0]
    assert _GATE_RULE2 in header, (
        f"the v4 header does not cite {_GATE_RULE2}, the gate it rests on")
    assert _GATE_RULE1 not in header, (
        f"the v4 header still points at {_GATE_RULE1}, superseded by the "
        "rule-v2 re-gate")
    assert (_REPO / _GATE_RULE2).exists(), (
        "the cited gate artifact is not in the tree")


def test_every_exempt_capital_is_really_sentence_initial():
    """``_SENTENCE_CAPITALS`` is the one exemption list that can make a real
    name vanish from BOTH halves of the guard, so it gets a pin of its own.

    Every entry must occur somewhere the body actually starts a sentence:
    the start of a line or list item, or after a sentence-ending `.`, `:`
    or em dash. A word parked here to silence a mid-sentence proper noun
    fails. It earned its keep immediately — the first cut of the pattern
    rejected ``Facts`` and ``Return``, which open a ``- `` bullet rather
    than a bare line."""
    body = _gen()._split_body(_read("sonnet_extractor_v4.md"))
    # Start of body or of a line, optionally past a "- "/"* " list marker;
    # or mid-line after a sentence-ending . : or em dash.
    opener = "(?:\\A|\\n)[ \\t]*(?:[-*][ \\t]+)?|[.:\\u2014][ \\t]+"
    misplaced = sorted(
        w for w in _SENTENCE_CAPITALS
        if not re.search("(?:" + opener + ")" + re.escape(w) + "\\b", body))
    assert misplaced == [], (
        f"exempted as sentence English but never sentence-initial: "
        f"{misplaced} - an entry here removes a word from both halves of "
        "the guard, so it must earn its place")
