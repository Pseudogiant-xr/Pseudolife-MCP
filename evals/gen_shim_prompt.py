"""Regenerate the CLI shim's extraction prompt (2026-09-05).

    PYTHONPATH=. python evals/gen_shim_prompt.py

``evals/claude_shim.py --system-prompt-file`` REPLACES the shipped
``dream._SYSTEM_PROMPT`` prefix with the file's body, keeping only the
harness's appended vocab/known-facts hints. On an install whose primary
extractor is the CLI shim — the default since the 2026-07-11 sidecar
cutover, and what ``ops/.env`` selects with
``PSEUDOLIFE_DREAM_BASE_URL=...:8082/v1`` — that means an instruction added
to ``_SYSTEM_PROMPT`` never reaches the model. The assistant-facts blocks
shipped on 2026-09-05 landed in exactly that blind spot.

``sonnet_extractor_v4.md`` closes it: the v2 body verbatim, plus the SAME
three blocks the shipped prompt carries, imported from ``dream.py`` rather
than retyped. One source of truth for the assistant-facts text, so the
shim path and the daemon path cannot drift in what they ask for.

**Why v4 and not v3.** ``evals/prompts/sonnet_extractor_v3.md`` is already
taken by an unrelated 2026-08-02 lineage (coverage mandates from the
full-78 discordant-pair autopsy, ``docs/superpowers/specs/
2026-08-02-sonnet-v3-coverage-design.md``). It was never adopted as the
shim default — ``ops/install-shim-autostart.ps1`` still ships v2 — and
overwriting a committed measured artifact would strand its gate. v4
stacks on v2, which is the file the deployed config actually names.

``tests/test_shim_prompt.py`` pins the composition, regenerates the file,
and extends the example-token dataset grep to cover it; edit the blocks in
``dream.py`` (or the v2 body) and re-run rather than hand-editing the
``.md``.

``sonnet_extractor_v5.md`` (2026-09-07) is v4 with the v2 body's two
pre-rule worked examples re-cut on invented names — the same re-cut the
daemon's prompt took with the v12 base — so the shim path stops naming a
benchmark answer. The v2 body itself is untouched (it is the pre arm of
two committed gates); v5 is derived from it by ``V5_RECUTS`` below and
written by the same ``write()``.
"""
from pathlib import Path

from pseudolife_memory.memory.dream import (
    _ASSISTANT_FACTS_INSTRUCTION,
    _ASSISTANT_PROVENANCE_EXAMPLE,
    _ASSISTANT_SPEAKER_RULE,
)

PROMPTS = Path(__file__).resolve().parent / "prompts"
BASE_NAME = "sonnet_extractor_v2.md"
OUT_NAME = "sonnet_extractor_v4.md"

# The shim's own split rule, so "what the generator composes" and "what the
# model is actually sent" are the same operation rather than two spellings
# of it (claude_shim.main: raw.split("\n---\n", 1)[-1].strip()).
SEPARATOR = "\n---\n"

# Imported, never retyped — these are the shipped blocks. The whole point of
# the file is that the shim asks for the same thing the daemon's prompt does.
TAIL = (_ASSISTANT_FACTS_INSTRUCTION + _ASSISTANT_SPEAKER_RULE
        + _ASSISTANT_PROVENANCE_EXAMPLE)

# Proper nouns the v2 BODY's worked examples carry, written before the
# invented-token rule existed (`gen_assistant_facts_prompts.EXAMPLE_TOKENS`
# governs what a NEW example may name). v4 inherits them verbatim. These
# lived beside `EXAMPLE_TOKENS` until 2026-09-07 because the daemon prompt
# carried the same two names; the v12 base re-cut them there, so the shim
# lineage is now the only carrier and the lists moved next to the file
# that owns them. v2 is the pre arm of a committed gate
# (`ladder-shimprompt-rule2-paired-verdict-threshold.json`) and is not
# re-cut retroactively; the re-cut shim prompt is v5, below, with its own
# gate (`ladder-shimv5-paired-verdict-threshold.json`).
PRE_RULE_PROPER_NOUNS = frozenset({
    "Northern Flicker",   # COUNTS, TOTALS, AND QUANTITIES example
    "Rosa's Diner",       # COLLECTION MEMBERSHIP example
})

# The subset of `PRE_RULE_PROPER_NOUNS` that ACTUALLY occurs in the measured
# corpus, i.e. real contamination. Recorded 2026-09-05 by the merge review of
# the shim-prompt gate; the guards treat this as an EQUALITY, so the debt can
# neither grow nor rot into decoration.
#
# The count-exclusion example reads "[5] saw a Northern Flicker today, that
# makes 32 species at the park now" and yields the value "32". LongMemEval
# question `affe2881` (knowledge-update) asks how many bird species the user
# has seen in their local park; its gold answer is "32", and all 13
# occurrences of "Northern Flicker" in EACH dataset file sit inside that
# question's own sessions.
KNOWN_CORPUS_COLLISIONS = {
    "Northern Flicker": (
        "LongMemEval affe2881 (knowledge-update, gold '32'); the "
        "count-exclusion example states the same number. Recorded "
        "2026-09-05."),
}

# ── v5: the v2 body with its pre-rule examples re-cut on invented names ──
#
# v4 inherits v2's two worked examples verbatim, and one of them names a
# LongMemEval answer (`KNOWN_CORPUS_COLLISIONS` above). The daemon's prompt
# paid that debt on 2026-09-07 with the v12 base
# (`evals/prompts/ku_op_prompt_v12_count_source_example.txt`); v5 pays it
# on the shim path the same way — the SAME invented names, registered once
# in `gen_assistant_facts_prompts.BASE_EXAMPLE_TOKENS` under the
# zero-occurrence contract, so there is one registry and one grep. The v2
# body is not edited: it stays the pre arm of two committed gates, and v4
# stays generated from it verbatim.
#
# Each pair is applied to every occurrence in the v2 body (the note AND the
# JSON that echoes it); `v5_body` refuses a pair whose left side is absent,
# so a re-cut cannot silently become a no-op if v2 ever changes.
V5_NAME = "sonnet_extractor_v5.md"

# The re-cut table — (old, new) pairs applied in order to the v2 body. It is
# the v12 base's re-cut (PR #279) transposed onto the v2 wording: the same
# invented names, the same replacement number, nothing else. Every proper
# noun on the right side is registered in
# `gen_assistant_facts_prompts.BASE_EXAMPLE_TOKENS` or is plain English; the
# v5 tests hold the result to that and to an EMPTY corpus-collision list.
V5_RECUTS: tuple[tuple[str, str], ...] = (
    # COLLECTION MEMBERSHIP example — "road bike" is the gold of LongMemEval
    # 89941a94 and gpt4_e414231f (note [6] and the JSON that echoes it).
    ("road bike", "penny-farthing"),
    # COUNTS, TOTALS, AND QUANTITIES example — the note paraphrased
    # affe2881's answer turn and stated its gold "32" (note [7], the claim's
    # attribute, its value, and the closing "NO op:add" sentence).
    ("Northern Flicker", "Gallowmere Teal"),
    ("makes 32", "makes 41"),
    ('"value":"32"', '"value":"41"'),
    ("species at the park now", "species at Kelmarsh Reserve now"),
    ('"bird species seen at park"', '"bird species seen at Kelmarsh Reserve"'),
)

# Proper nouns v5 still carries from before the invented-token rule. These
# are kept on purpose — none occurs in the measured corpus, so there is no
# debt to pay — and `tests/test_shim_prompt.py` holds the list equal to
# what the v5 body actually says.
V5_PRE_RULE_PROPER_NOUNS = frozenset({
    "Rosa's Diner",       # COLLECTION MEMBERSHIP example
})

# v5 exists to make this empty, and the dataset grep holds it there.
V5_KNOWN_CORPUS_COLLISIONS: dict[str, str] = {}

HEADER = """\
# Sonnet-tuned dream extraction prompt — v4 (2026-09-05)

v2 plus the assistant-facts blocks that shipped in
`pseudolife_memory/memory/dream.py` on 2026-09-05
(`_ASSISTANT_FACTS_INSTRUCTION`, `_ASSISTANT_SPEAKER_RULE`,
`_ASSISTANT_PROVENANCE_EXAMPLE`). Everything above them is v2 verbatim.

Generated — do not hand-edit. `PYTHONPATH=. python evals/gen_shim_prompt.py`
imports the three blocks from `dream.py`, so the shim path asks for exactly
what the daemon's own prompt asks for. `--system-prompt-file` REPLACES the
shipped prefix, which is why the daemon-side change alone does not reach an
install whose extractor is the shim.

v3 is a different, unadopted lineage (2026-08-02 coverage mandates); this
file stacks on v2, the variant the deployed config actually names.

Gate: ladder `opus-5` rung (the Max-plan CLI shim on its dedicated port,
same model the shim autostart serves), v2 vs v4 —
`evals/results/ladder-shimprompt-rule2-paired-verdict-threshold.json`. That
is the re-gate: the speaker rule was rewritten after the first gate ran and
this file is generated from it, so `ladder-shimprompt-paired-verdict-threshold.json`
is superseded (it measured a body no longer shipped) and stays committed as
retired evidence. The JSON schema stays byte-compatible with production
apart from the `speaker` field, which the parser already accepts and
ignores when absent.
"""


HEADER_V5 = """\
# Sonnet-tuned dream extraction prompt — v5 (2026-09-07)

v4 with the v2 body's two pre-rule worked examples re-cut on invented
names — the same re-cut the daemon's shipped prompt took with the v12 base
on 2026-09-07 (`ku_op_prompt_v12_count_source_example.txt`), so the shim
path no longer names a LongMemEval answer. Everything else is v4: the
rest of the v2 body verbatim plus the assistant-facts blocks imported from
`pseudolife_memory/memory/dream.py`.

Generated — do not hand-edit. `PYTHONPATH=. python evals/gen_shim_prompt.py`
derives this file from `sonnet_extractor_v2.md` through `V5_RECUTS` and
writes it beside v4. `--system-prompt-file` REPLACES the shipped prefix,
which is why the daemon-side re-cut alone does not reach an install whose
extractor is the shim.

Gate: ladder `opus-5` rung (the Max-plan CLI shim on its dedicated port,
same model the shim autostart serves), v4 vs v5, two replicates per arm —
`evals/results/ladder-shimv5-paired-verdict-threshold.json`.
"""


def _split_body(text: str) -> str:
    """The prompt body the shim would send, given the whole file."""
    return text.split(SEPARATOR, 1)[-1].strip()


def base_body() -> str:
    return _split_body((PROMPTS / BASE_NAME).read_text(encoding="utf-8"))


def compose() -> str:
    """The full v4 file. Body = v2 body + the shipped assistant-facts tail."""
    return HEADER + SEPARATOR + "\n" + base_body() + "\n\n" + TAIL


def v5_body() -> str:
    """The v2 body with every `V5_RECUTS` pair applied, each to all of its
    occurrences. A missing left side is an error, not a no-op."""
    body = base_body()
    for old, new in V5_RECUTS:
        if old not in body:
            raise ValueError(f"v5 re-cut target {old!r} is not in the v2 body")
        body = body.replace(old, new)
    return body


def compose_v5() -> str:
    """The full v5 file. Body = re-cut v2 body + the shipped tail."""
    return HEADER_V5 + SEPARATOR + "\n" + v5_body() + "\n\n" + TAIL


def write() -> None:
    for name, text in ((OUT_NAME, compose()), (V5_NAME, compose_v5())):
        path = PROMPTS / name
        path.write_text(text, encoding="utf-8", newline="\n")
        print("wrote", path.name, len(text))


# Writing stays behind ``__main__``. ``gen_assistant_facts_prompts`` used to
# write on IMPORT, so the guard tests that imported it silently regenerated
# the committed artifacts underneath their own assertions — a drifted file
# was repaired by the very suite meant to catch it (found 2026-09-05).
if __name__ == "__main__":
    write()
