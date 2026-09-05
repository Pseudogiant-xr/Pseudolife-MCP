"""Regenerate the assistant-facts prompt variants (2026-09-05).

    PYTHONPATH=. python evals/gen_assistant_facts_prompts.py

Since the provenance variant SHIPPED (2026-09-05), the two files are on
opposite sides of the shipped prompt and neither retypes anything:

* ``assistant_facts_provenance.txt`` **is** ``dream._SYSTEM_PROMPT``,
  byte for byte. It is the measured artifact and the live prompt at once,
  so they cannot drift.
* ``assistant_facts_naive.txt`` is the pre-2026-09-05 base
  (``dream._BASE_SYSTEM_PROMPT``) plus the same instruction paragraph and
  a worked example that carries no ``speaker`` field — the unguarded
  comparison arm, kept because the guard's case rests on measuring
  against it.

``tests/test_assistant_provenance.py`` pins both properties and
regenerates the files byte-exactly; edit the blocks (in ``dream.py`` for
the shipped tail, here for the naive example) and re-run rather than
hand-editing the ``.txt`` files.

Every proper noun in a worked example is INVENTED and registered in
``EXAMPLE_TOKENS``. The first cut of these files built its example around
"Miss Bee Providore ... Bandung", which is the gold answer of LongMemEval
question ``c4f10528`` in the very slice the prompts were measured on (a
counted win for cortex/hybrid/cascade in both variants) — the prompt was
handing the model an answer. ``tests/test_assistant_provenance.py`` now
enforces both halves of the fix: no capitalised word may appear in an
example unless it is a registered token or ordinary sentence English, and
no registered token may occur anywhere in either LongMemEval dataset file.
Since the shipped prompt now carries the provenance example, that guard
covers the live extractor, not only an eval-only file.
"""
from pathlib import Path

from pseudolife_memory.memory.dream import (
    _ASSISTANT_FACTS_INSTRUCTION,
    _ASSISTANT_PROVENANCE_EXAMPLE,
    _BASE_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
)

OUT = Path(__file__).resolve().parent / "prompts"

# Shared instruction: assistant-stated content is extractable, keyed to what
# it is about. Imported, not retyped — it ships inside `_SYSTEM_PROMPT`.
INSTRUCTION = _ASSISTANT_FACTS_INSTRUCTION

# The invented proper nouns the worked examples are built from. Nothing
# capitalised may appear in an example unless it is registered here (or
# ordinary sentence English), and nothing registered here may occur in
# either LongMemEval dataset file — both halves are tested, so a future
# edit cannot reintroduce a benchmark answer by hand.
EXAMPLE_TOKENS = (
    "The Quillon Larder",
    "Fendrick Row",
    "Marrowgate",
    "pepper-brisket bun",
)

NAIVE_EXAMPLE = (
    "Example. Notes: [7] assistant: For brunch in Marrowgate I'd suggest "
    "The Quillon Larder on Fendrick Row — it is a garden cafe, and its "
    "signature dish is the pepper-brisket bun. Output: "
    '{"claims":[{"entity":"The Quillon Larder","attribute":"location",'
    '"value":"Fendrick Row, Marrowgate","confidence":0.9,"source":7},'
    '{"entity":"The Quillon Larder","attribute":"signature dish",'
    '"value":"pepper-brisket bun","confidence":0.85,"source":7}]}\n'
)

# Re-exported so the example-token guard reads the SHIPPED block rather than
# a copy of it: this is `dream._ASSISTANT_PROVENANCE_EXAMPLE`.
PROVENANCE_EXAMPLE = _ASSISTANT_PROVENANCE_EXAMPLE

NAIVE_TEXT = _BASE_SYSTEM_PROMPT + "\n" + INSTRUCTION + NAIVE_EXAMPLE
# The provenance variant is the shipped prompt itself. It carries the SAME
# instruction plus the speaker rule, and one worked example of the same
# scenario in which every claim names its speaker — the naive example is
# deliberately NOT included, since a worked example without the field would
# contradict the rule it illustrates.
PROVENANCE_TEXT = _SYSTEM_PROMPT

FILES = {
    "assistant_facts_naive.txt": NAIVE_TEXT,
    "assistant_facts_provenance.txt": PROVENANCE_TEXT,
}


def write() -> None:
    for name, text in FILES.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    print("wrote", *(len(t) for t in FILES.values()))


# Writing is behind __main__ on purpose. The module body used to write on
# IMPORT, which meant the guard tests that import it (for EXAMPLE_TOKENS and
# the worked-example blocks) silently REGENERATED the committed artifacts as
# a side effect — so a hand-edited or drifted .txt was overwritten by the
# very suite that exists to catch it, and the run's second assertion passed
# on the file the first assertion had just failed against. Found 2026-09-05
# while RED-checking the byte-exact pin on the newly shipped prompt.
if __name__ == "__main__":
    write()
