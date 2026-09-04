"""Regenerate the assistant-facts prompt variants (2026-09-05).

    PYTHONPATH=. python evals/gen_assistant_facts_prompts.py

Both files are the shipped ``_SYSTEM_PROMPT`` VERBATIM (imported, never
retyped) plus one appended instruction block, so an edit to the shipped
prompt shows up as a diff here instead of silently drifting away from the
measured artifacts. ``tests/test_assistant_provenance.py`` pins the
"starts with the shipped prompt" property; edit the blocks below and
re-run rather than hand-editing the .txt files.
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

NAIVE_EXAMPLE = (
    "Example. Notes: [7] assistant: For brunch in Bandung I'd suggest Miss "
    "Bee Providore on Jalan Progo — it is a garden cafe, and its signature "
    "dish is the smoked-beef bowl. Output: "
    '{"claims":[{"entity":"Miss Bee Providore","attribute":"location",'
    '"value":"Jalan Progo, Bandung","confidence":0.9,"source":7},'
    '{"entity":"Miss Bee Providore","attribute":"signature dish",'
    '"value":"smoked-beef bowl","confidence":0.85,"source":7}]}\n'
)

SPEAKER_RULE = (
    "EVERY CLAIM NAMES ITS SPEAKER: add a \"speaker\" field to every claim — "
    "\"user\" when the note stating the fact is a user turn, \"assistant\" "
    "when it is an assistant turn. Each note begins with its role, so read "
    "the role there; never guess it, and never omit the field.\n"
)

PROVENANCE_EXAMPLE = (
    "Example. Notes: [7] assistant: For brunch in Bandung I'd suggest Miss "
    "Bee Providore on Jalan Progo — its signature dish is the smoked-beef "
    "bowl. [8] user: I went, and the smoked-beef bowl was too salty for me — "
    "I am sticking to vegetarian brunch from now on. Output: "
    '{"claims":[{"entity":"Miss Bee Providore","attribute":"location",'
    '"value":"Jalan Progo, Bandung","speaker":"assistant","confidence":0.9,'
    '"source":7},'
    '{"entity":"Miss Bee Providore","attribute":"signature dish",'
    '"value":"smoked-beef bowl","speaker":"assistant","confidence":0.85,'
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
