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
`evals/results/ladder-shimprompt-paired-verdict-threshold.json`. The JSON
schema stays byte-compatible with production apart from the `speaker`
field, which the parser already accepts and ignores when absent.
"""


def _split_body(text: str) -> str:
    """The prompt body the shim would send, given the whole file."""
    return text.split(SEPARATOR, 1)[-1].strip()


def base_body() -> str:
    return _split_body((PROMPTS / BASE_NAME).read_text(encoding="utf-8"))


def compose() -> str:
    """The full v4 file. Body = v2 body + the shipped assistant-facts tail."""
    return HEADER + SEPARATOR + "\n" + base_body() + "\n\n" + TAIL


def write() -> None:
    path = PROMPTS / OUT_NAME
    path.write_text(compose(), encoding="utf-8", newline="\n")
    print("wrote", path.name, len(compose()))


# Writing stays behind ``__main__``. ``gen_assistant_facts_prompts`` used to
# write on IMPORT, so the guard tests that imported it silently regenerated
# the committed artifacts underneath their own assertions — a drifted file
# was repaired by the very suite meant to catch it (found 2026-09-05).
if __name__ == "__main__":
    write()
