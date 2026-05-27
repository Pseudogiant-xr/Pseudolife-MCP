"""Structured slot extraction for memory entries.

The v0.6.2 system-prompt recall-grounding directive is a *probability*
nudge — it tells the LLM to mirror user pronouns from recalled memory,
but the LLM can still drift when its prior is strong (English-default
"she" for "cat"). The deterministic fix is to extract structured slots
at store time and inject them into the LLM's context alongside the
prose, so the model gets a typed fact sheet that's much harder to drift
from.

This module implements a **regex-based** first cut: cheap, fast, no LLM
call, runs on every store. Catches the most common patterns (entity
declarations with pronouns, named possessions, model numbers, location
mentions, etc.). A later v0.8 iteration could swap in a small dedicated
extraction LLM for the long tail, but the regex layer covers most of
what hits the cat-Jacque-style supersession bugs.

Output shape
------------
``extract_slots(text)`` returns a list of ``Slot`` namedtuples::

    Slot(entity="Jacque", attribute="type",     value="cat")
    Slot(entity="Jacque", attribute="breed",    value="Ragdoll")
    Slot(entity="Jacque", attribute="gender",   value="male")    # from "him"
    Slot(entity="Jacque", attribute="owned",    value="False")   # from "gave away"

The CMS attaches these to ``MemoryEntry.slots`` (schema v4 additive).
At retrieval time, slots across all surfaced entries are merged into a
single fact sheet per entity and injected into the LLM context as a
small structured block — see :func:`merge_slots_view`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Slot:
    """One ``(entity, attribute, value)`` triple extracted from a memory."""

    entity: str          # the thing being described, e.g. "Jacque" or "my dog"
    attribute: str       # what's being asserted, e.g. "type" / "breed" / "gender"
    value: str           # the asserted value, e.g. "cat" / "Ragdoll" / "male"
    polarity: str = "+"  # "+" assert, "-" negate ("no longer have"/"never had")


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
# These are deliberately conservative — we miss recall in exchange for high
# precision. False positives in slots are far more damaging than misses (they
# get injected into LLM context as facts), so we err on the side of "didn't
# extract" when the pattern is ambiguous.

# "named X" / "called X" — most common entity declaration.
_NAMED_RE = re.compile(
    r"\b(?:named|called)\s+(?P<name>[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)?)",
)

# "my <noun>" — possession declaration where the noun is the entity.
_MY_NOUN_RE = re.compile(
    r"\bmy\s+(?P<noun>[a-z]+(?:'s)?(?:\s+[a-z]+)?)",
    re.IGNORECASE,
)

# "I have a [breed-or-color] cat/dog/car/..." — type declaration with optional
# attribute.  Captures both the type ("cat") and an optional attribute
# ("Ragdoll", "blue").
_HAVE_TYPE_RE = re.compile(
    r"\bI\s+(?:have|own|got|drive)\s+a\s+"
    r"(?:(?P<attr>[A-Z][A-Za-z]+|[a-z]+)\s+)?"
    r"(?P<type>cat|dog|car|laptop|phone|gpu|graphics card|computer|bike)",
    re.IGNORECASE,
)

# Pronoun mentions ("him" / "her" / "they") used immediately as gender hint
# when the preceding entity is known.
_PRONOUN_GENDER = {
    "him": "male", "his": "male", "he": "male",
    "her": "female", "hers": "female", "she": "female",
    "they": "nonbinary", "them": "nonbinary", "their": "nonbinary",
    "it": "neuter", "its": "neuter",
}

# Loss / state-change phrases — produce polarity="-" slots.  Matches verb
# stems with optional past-tense / progressive suffixes so "give him away",
# "gave him away", and "giving away" all fire.
_LOSS_RE = re.compile(
    r"\b(?:giv(?:e|es|ing|en)|gave|"
    r"sell|sells|selling|sold|"
    r"los(?:e|es|ing|t)|"
    r"rehom(?:e|es|ing|ed)|"
    r"donat(?:e|es|ing|ed)|"
    r"no\s+longer\s+have|"
    r"used\s+to\s+(?:have|own)|"
    r"got\s+rid\s+of|"
    r"thr[eo]w(?:s|ing|n)?\s+(?:away|out)|threw\s+(?:away|out))\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_slots(
    text: str,
    *,
    last_entity_context: str | None = None,
) -> list[Slot]:
    """Extract a list of slots from a single text.

    ``last_entity_context`` is the name of the most recent entity mentioned
    in the conversation (e.g. "Jacque"). It's used to attach pronouns that
    appear without an antecedent in the current text — e.g. "I gave him
    away" with ``last_entity_context="Jacque"`` yields
    ``Slot("Jacque", "gender", "male", "+")``.
    """
    slots: list[Slot] = []

    # 1. Named entities — most reliable signal.
    named_entities: list[str] = []
    for m in _NAMED_RE.finditer(text):
        name = m.group("name")
        named_entities.append(name)

    # 2. ``I have a Ragdoll cat`` — type + optional attribute.
    type_entity: str | None = None
    for m in _HAVE_TYPE_RE.finditer(text):
        t = m.group("type").lower()
        attr_value = m.group("attr")
        # Bind to the most recently named entity, or the type itself as
        # a fallback handle ("my dog" or just "cat" if anonymous).
        anchor = named_entities[0] if named_entities else f"my {t}"
        type_entity = anchor
        slots.append(Slot(anchor, "type", t))
        if attr_value:
            # Heuristic: capitalised word → breed/brand;
            # lowercase → color/adjective.
            attr_name = "breed" if attr_value[0].isupper() else "color"
            slots.append(Slot(anchor, attr_name, attr_value))

    # If we got a named entity without a type assertion in this text,
    # still emit a "name exists" anchor so the slot view picks it up.
    if named_entities and type_entity is None:
        for name in named_entities:
            slots.append(Slot(name, "name", name))

    # 3. Pronoun gender hints — only when we have a referent.
    # The referent is whichever entity this text mentions, falling back
    # to ``last_entity_context`` (cross-message coreference).
    referent = (
        named_entities[0] if named_entities
        else type_entity if type_entity
        else last_entity_context
    )
    if referent:
        # Find pronouns as whole tokens (lowercased).  We only emit one
        # gender slot per text — picking the first matched pronoun keeps
        # things deterministic when the text uses multiple pronouns.
        for tok in re.findall(r"\b([A-Za-z]+)\b", text):
            low = tok.lower()
            if low in _PRONOUN_GENDER:
                slots.append(
                    Slot(referent, "gender", _PRONOUN_GENDER[low]),
                )
                break

    # 4. Loss / state-change — emit ``owned=False`` against the referent.
    if referent and _LOSS_RE.search(text):
        slots.append(Slot(referent, "owned", "False"))

    return slots


def merge_slots_view(slot_lists: Iterable[Iterable[Slot]]) -> dict[str, dict[str, str]]:
    """Merge slots from multiple entries into a per-entity fact sheet.

    Later entries override earlier ones on the same ``(entity, attribute)``
    key — so a "gender: female" from a v0.4.x assistant restatement gets
    overridden by a v0.6 user "him" assertion, and the final ``owned``
    flag reflects the most-recent state.

    Negation polarity (``polarity="-"``) wins: ``owned`` flips to
    ``False`` when any loss slot exists for the entity.
    """
    view: dict[str, dict[str, str]] = {}
    for slots in slot_lists:
        for s in slots:
            ent = view.setdefault(s.entity, {})
            if s.polarity == "-":
                # Loss / negation always wins over a prior affirmation.
                ent[s.attribute] = f"NOT {s.value}" if s.value else "False"
            else:
                ent[s.attribute] = s.value
    return view


def format_slots_for_context(
    view: dict[str, dict[str, str]],
    *,
    max_entities: int = 8,
) -> str:
    """Format the merged view as a compact block for LLM-context injection.

    Layout (markdown-y but token-cheap)::

        Known facts:
        - Jacque: type=cat, breed=Ragdoll, gender=male, owned=False
        - my Subaru WRX: type=car, color=blue

    Caps at ``max_entities`` (sorted by attribute count, descending) to
    avoid blowing the prompt budget when the user has hundreds of memories.
    """
    if not view:
        return ""
    items = sorted(view.items(), key=lambda kv: -len(kv[1]))[:max_entities]
    lines = ["Known facts:"]
    for ent, attrs in items:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(attrs.items()))
        lines.append(f"- {ent}: {parts}")
    return "\n".join(lines)
