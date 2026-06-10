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
# Dev-fact extraction (v0.2 Phase 0)
# ---------------------------------------------------------------------------
# Lexicon-gated, token-based, precision-first: the attribute position of
# every pattern must end in a known attribute word, so prose like "my point
# is that…" or "Claude Code is the client" never promotes. False positives
# poison the cortex; misses just mean Claude calls memory_fact_set.

_ATTR_LEXICON = frozenset({
    "default", "port", "version", "timeout", "host", "hostname", "address",
    "ip", "path", "dir", "directory", "folder", "branch", "language",
    "status", "name", "url", "endpoint", "model", "device", "scope",
    "threshold", "capacity", "schema", "license", "framework", "editor",
    "shell", "os", "database", "db", "username", "email", "timezone",
    "gpu", "cpu", "machine", "server", "repo", "repository", "transport",
    "protocol", "format", "engine", "runtime", "interval", "limit",
})
_ARTICLES = {"the", "a", "an", "our", "this", "that", "its"}
_VALUE_STOPWORDS = {"that", "because", "when", "why", "how", "what", "if"}
_NEGATION_FIRST = {"not", "no", "never", "n't", "isn't", "aren't"}
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_KV_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z][\w.\- ]{0,59}?)\s*[:=]\s*(?P<value>\S.{0,79})$"
)
_COPULA_RE = re.compile(r"\bis\b", re.IGNORECASE)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|;\s*")


def _dev_tokens(s: str) -> list[str]:
    return re.findall(r"[\w.\-/\\']+", s)


def _attr_suffix(tokens: list[str]) -> int:
    """Index where a 1-2 word lexicon attribute suffix starts, or -1."""
    if tokens and tokens[-1].lower() in _ATTR_LEXICON:
        if len(tokens) >= 2 and tokens[-2].lower() in _ATTR_LEXICON:
            return len(tokens) - 2
        return len(tokens) - 1
    return -1


def _clean_entity(tokens: list[str]) -> str:
    while tokens and tokens[0].lower() in _ARTICLES:
        tokens = tokens[1:]
    if not tokens or len(tokens) > 4:
        return ""
    ent = " ".join(tokens)
    return ent[:-2] if ent.lower().endswith("'s") else ent


def _clean_value(raw: str) -> str:
    v = raw.strip().rstrip(".,;!").strip()
    first = v.split(" ", 1)[0].lower() if v else ""
    if not v or len(v) > 80 or first in _VALUE_STOPWORDS or first in _NEGATION_FIRST:
        return ""
    return v


def _dev_slot_from_copula(sent: str) -> Slot | None:
    """`<entity> <attr-lexicon> is <value>` plus the my / possessive / of forms."""
    if "?" in sent:
        return None
    m = _COPULA_RE.search(sent)
    if m is None:
        return None
    value = _clean_value(sent[m.end():])
    if not value:
        return None
    subject = _dev_tokens(sent[:m.start()])
    if not subject:
        return None
    low = [t.lower() for t in subject]

    # "my <attr> is <value>" → entity "user". The whole remainder must be
    # the attribute (1-2 lexicon words) — "my point is…" never promotes.
    if low[0] == "my":
        rest = subject[1:]
        if rest and _attr_suffix(rest) == 0 and len(rest) <= 2:
            return Slot("user", " ".join(rest).lower(), value)
        return None

    # "the <attr> of <entity> is <value>".
    if "of" in low:
        o = low.index("of")
        attr_part, ent_part = subject[:o], subject[o + 1:]
        stripped = [t for t in attr_part if t.lower() not in _ARTICLES]
        if stripped and _attr_suffix(stripped) == 0 and len(stripped) <= 2:
            entity = _clean_entity(ent_part)
            if entity:
                return Slot(entity, " ".join(stripped).lower(), value)
        return None

    # "<entity…> <attr-lexicon ×1-2> is <value>"  (covers possessives —
    # the tokenizer keeps "GND-Share's" whole and _clean_entity strips 's).
    a = _attr_suffix(subject)
    if a > 0:
        entity = _clean_entity(subject[:a])
        if entity:
            return Slot(entity, " ".join(subject[a:]).lower(), value)
    return None


def extract_dev_slots(text: str) -> list[Slot]:
    """Deterministic dev-fact extraction (no LLM). See ``_ATTR_LEXICON``."""
    text = _FENCE_RE.sub(" ", text or "")
    out: list[Slot] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(slot: Slot | None) -> None:
        if slot is None:
            return
        key = (slot.entity.lower(), slot.attribute, slot.value)
        if key not in seen:
            seen.add(key)
            out.append(slot)

    for line in text.splitlines():
        line = line.strip()
        m = _KV_LINE_RE.match(line)
        if m:
            key_toks = _dev_tokens(m.group("key"))
            a = _attr_suffix(key_toks)
            if a > 0:  # require entity tokens before the attribute
                entity = _clean_entity(key_toks[:a])
                value = _clean_value(m.group("value"))
                if entity and value:
                    _add(Slot(entity, " ".join(key_toks[a:]).lower(), value))
                    continue
        for sent in _SENT_SPLIT_RE.split(line):
            if sent.strip():
                _add(_dev_slot_from_copula(sent.strip()))
    return out


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

    # 5. Dev-shaped facts (v0.2) — lexicon-gated copula / key:value forms.
    existing = {(s.entity.lower(), s.attribute, s.value) for s in slots}
    for s in extract_dev_slots(text):
        if (s.entity.lower(), s.attribute, s.value) not in existing:
            slots.append(s)

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
