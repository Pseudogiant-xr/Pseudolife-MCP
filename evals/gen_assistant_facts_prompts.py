"""Regenerate the assistant-facts prompt variants (2026-09-05).

    PYTHONPATH=. python evals/gen_assistant_facts_prompts.py

Both files are the shipped ``_SYSTEM_PROMPT`` VERBATIM (imported, never
retyped) plus one appended instruction block, so an edit to the shipped
prompt shows up as a diff here instead of silently drifting away from the
measured artifacts. ``tests/test_assistant_provenance.py`` pins the
"starts with the shipped prompt" property; edit the blocks below and
re-run rather than hand-editing the .txt files.

Every proper noun in a worked example is INVENTED and registered in
``EXAMPLE_TOKENS``. The first cut of these files built its example around
"Miss Bee Providore ... Bandung", which is the gold answer of LongMemEval
question ``c4f10528`` in the very slice the prompts were measured on (a
counted win for cortex/hybrid/cascade in both variants) — the prompt was
handing the model an answer. ``tests/test_assistant_provenance.py`` now
enforces both halves of the fix: no capitalised word may appear in an
example unless it is a registered token or ordinary sentence English, and
no registered token may occur anywhere in either LongMemEval dataset file.
"""
from pathlib import Path

from pseudolife_memory.memory.dream import _SYSTEM_PROMPT

OUT = Path(__file__).resolve().parent / "prompts"

# Shared instruction: assistant-stated content is extractable, keyed to what
# it is about.
INSTRUCTION = (
    "THE ASSISTANT'S OWN STATEMENTS ARE FACTS TOO: the notes are turns of a "
    "conversation, each rendered as \"role: content\". What the ASSISTANT "
    "asserted, described, recommended, or specified is extractable on exactly "
    "the same terms as what the user said — names, values, descriptions, "
    "specifications, and the choices it presented all qualify. Key each such "
    "claim to WHAT IT IS ABOUT: the entity is the thing described (the "
    "restaurant, the book, the tool, the setting), never \"the assistant\" "
    "and never \"the conversation\". A recommendation the assistant made is a "
    "durable fact about the thing recommended.\n"
)

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

SPEAKER_RULE = (
    "EVERY CLAIM NAMES ITS SPEAKER: add a \"speaker\" field to every claim — "
    "\"user\" when the note stating the fact is a user turn, \"assistant\" "
    "when it is an assistant turn. Each note begins with its role, so read "
    "the role there; never guess it, and never omit the field.\n"
)

PROVENANCE_EXAMPLE = (
    "Example. Notes: [7] assistant: For brunch in Marrowgate I'd suggest "
    "The Quillon Larder on Fendrick Row — its signature dish is the "
    "pepper-brisket bun. [8] user: I went, and the pepper-brisket bun was "
    "too salty for me — I am sticking to vegetarian brunch from now on. "
    "Output: "
    '{"claims":[{"entity":"The Quillon Larder","attribute":"location",'
    '"value":"Fendrick Row, Marrowgate","speaker":"assistant",'
    '"confidence":0.9,"source":7},'
    '{"entity":"The Quillon Larder","attribute":"signature dish",'
    '"value":"pepper-brisket bun","speaker":"assistant","confidence":0.85,'
    '"source":7},'
    '{"entity":"user","attribute":"brunch preference","value":"vegetarian",'
    '"speaker":"user","confidence":0.9,"source":8}]}\n'
)

naive = _SYSTEM_PROMPT + "\n" + INSTRUCTION + NAIVE_EXAMPLE
# The provenance variant carries the SAME instruction, and one worked
# example of the same scenario in which every claim names its speaker —
# the naive example is deliberately NOT included, since a worked example
# without the field would contradict the rule it illustrates.
prov = (_SYSTEM_PROMPT + "\n" + INSTRUCTION + SPEAKER_RULE
        + PROVENANCE_EXAMPLE)

(OUT / "assistant_facts_naive.txt").write_text(naive, encoding="utf-8",
                                               newline="\n")
(OUT / "assistant_facts_provenance.txt").write_text(prov, encoding="utf-8",
                                                    newline="\n")
print("wrote", len(naive), len(prov))
