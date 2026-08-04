"""Pluggable dream extractors — turn recent memory text into cortex claims.

A dream consolidates the recent associative stream into canonical
``(entity, attribute, value)`` facts. The *extraction* step is pluggable:
the ``OpenAICompatExtractor`` (an OpenAI-compatible LLM) is the cortex writer;
``NoOpExtractor`` is the default when none is configured (single-writer cortex:
the LLM dream is the sole *automatic* writer, so no extractor means no automatic
cortex writes). ``RegexExtractor`` remains as an explicit opt-in only — it is
never selected automatically (the store-path auto-promote and the old
``dream_run`` regex fallback are both gone). The shared driver lives in
``MemoryService.dream_run`` so cursor discipline lives in one place.
"""
from __future__ import annotations

import logging
import re
from typing import Protocol, TypedDict

logger = logging.getLogger(__name__)


class _ClaimRequired(TypedDict):
    entity: str
    attribute: str
    value: str
    confidence: float
    origin: str          # "user" | "action" | "agent"


class Claim(_ClaimRequired, total=False):
    # 0-based index into the extract() texts batch this claim came from, for
    # per-claim source attribution (slot->episode traces). Absent when the
    # model didn't cite a note (or cited one out of range).
    source: int
    # Set-membership operation ("add" | "remove"). Absent = scalar supersede.
    # Any other model-emitted value (incl. "set") normalises to absent HERE,
    # at the parse boundary — the 2026-07-31 correction: this field being
    # missing from the parse whitelist silently disabled the whole dream-op
    # path while the model emitted it correctly (c2-gate-verdict.json).
    op: str


class LessonClaim(TypedDict):
    task: str            # the task-type ("deploy engine to host")
    aspect: str          # approach | pitfall | tool-choice | correction
    lesson: str          # the actionable takeaway
    about: str           # the tool/source/approach the lesson concerns
    polarity: str        # "+" do-this | "-" avoid (dead end)
    outcome: str         # success | failure | correction
    confidence: float


class RelationClaim(TypedDict):
    src: str
    relation: str
    dst: str
    confidence: float


class DreamExtractor(Protocol):
    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        """Return canonical claims for ``texts``. ``vocab`` is the existing
        ``entity.attribute`` slot keys, so an extractor can REUSE them instead of
        reinventing variants. ``known_facts`` (when the known-facts window is
        enabled) is ``(entity, attribute, current value)`` triples the batch
        plausibly updates — shown so updates land on the SAME slot. The caller
        only passes it when non-empty, so extractors without the parameter
        keep working on window-off deployments. Must never raise — return
        ``[]`` on any failure."""
        ...


class RegexExtractor:
    """Deterministic no-LLM floor. Wraps ``slots.extract_slots`` (the one regex
    implementation) and shapes its output into ``Claim`` dicts."""

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        from pseudolife_memory.memory.slots import extract_slots
        claims: list[Claim] = []
        for i, t in enumerate(texts or []):
            for s in extract_slots(t or ""):
                value = s.value if s.polarity != "-" else ("NOT " + s.value)
                claims.append(Claim(
                    entity=s.entity, attribute=s.attribute, value=value,
                    confidence=0.55, origin="agent", source=i,
                ))
        return claims


class NoOpExtractor:
    """No-LLM, no-write floor. Returns no claims, so a dream with no configured
    extractor writes nothing to the cortex. Single-writer cortex: the LLM dream
    is the sole *automatic* writer of canonical facts; the regex (``extract_slots``)
    is for the recall-time slot-view only, and ``RegexExtractor`` is an explicit
    opt-in, never reached automatically."""

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        return []


_SYSTEM_PROMPT = (
    "You consolidate numbered notes into canonical facts. Extract durable, "
    'current-state facts as JSON: {"claims":[{"entity":..,"attribute":..,'
    '"value":..,"confidence":0..1,"source":<number of the note the fact came '
    "from>}]}. One slot per real fact; skip narrative, opinions, and obsolete "
    "states. When several notes state or update the SAME fact, use one "
    "consistent entity and attribute for it and emit only the CURRENT value "
    "(source = the note stating it). Reuse existing slot keys when they fit. "
    "When a note quotes or summarizes a DOCUMENT (a spec, policy, protocol, "
    "runbook, or guide), what the document prescribes is itself a durable "
    "fact — extract it with entity = the document's subject, even when other "
    "notes show something different being done.\n"
    "Example. Notes: [1] we moved the deploy target from staging to prod-eu. "
    "[2] the release runbook says every release needs a signed tag. Output: "
    '{"claims":[{"entity":"deploy target","attribute":"environment",'
    '"value":"prod-eu","confidence":0.9,"source":1},'
    '{"entity":"releases","attribute":"documented requirement",'
    '"value":"signed tag (per release runbook)","confidence":0.8,'
    '"source":2}]}\n'
    "When a note adds or removes an item from a COLLECTION the user "
    'maintains (restaurants tried, bikes owned, pending tasks), add an '
    '"op":"add" or "op":"remove" field to that claim instead of a plain '
    "supersede. op is ONLY for collection membership — a value that simply "
    "changed (a new job, a moved city) stays a plain claim with no op. "
    "Example. Notes: [3] tried Rosa's Diner tonight. [4] sold the road bike, "
    'no longer biking to work. Output: {"claims":['
    '{"entity":"user","attribute":"restaurants tried","value":"Rosa\'s '
    'Diner","op":"add","confidence":0.8,"source":3},'
    '{"entity":"user","attribute":"bikes owned","value":"road bike",'
    '"op":"remove","confidence":0.8,"source":4}]}\n'
    "COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a note "
    "states or updates how many of something the user has (a running "
    "count, a total, a follower number, a quantity), emit a plain claim "
    'whose value is the NEW number, with no "op" field — even when the '
    "note also names the item that changed the count. For example, the "
    "note [5] saw a Northern Flicker today, that makes 32 species at "
    "the park now — yields the single claim "
    '{"entity":"user","attribute":"bird species seen at park",'
    '"value":"32","confidence":0.9,"source":5} inside the one claims '
    'array, and NO "op":"add" claim for Northern Flicker.\n'
    'Return {"claims":[]} if nothing qualifies.'
)
# The op block + count-exclusion rule shipped 2026-08-01 (hold reversed by
# maintainer decision after the count-exclusion gate). This prompt must stay
# byte-identical to the measured artifact evals/prompts/ku_op_prompt_v5.txt
# (pinned by test_op_prompt_artifact.py): the v0 op block alone measured
# net-negative on KU-oracle (count updates re-routed into member-adds froze
# stated totals; c2op-gate-verdict.json), and the count rule is what repaired
# it (cascade back to the op-less control, sidecar + ladder validated;
# c2op-count-verdict.json). Edit the prompt only through a new measured
# artifact + gate.


# Events-only prompt for the SEPARATE extraction pass (design doc
# 2026-08-04-separate-pass-events-design.md): the claims call runs
# _SYSTEM_PROMPT byte-identically, so events cannot tax claim quality —
# the v7 combined prompt measured -0.053 (p 0.011) on claims for exactly
# that reason and never shipped. Pinned byte-identical to the measured
# artifact evals/prompts/events_pass_v1.txt (test_events_pass.py); edit
# only through a new measured artifact + gate. Language carries over the
# v7 events section's phrasing, which produced well-formed blocks in
# both serving smokes.
_EVENTS_SYSTEM_PROMPT = (
    "You extract EVENTS from numbered notes — dated occurrences (a trip "
    "taken, a purchase, an adoption, a start or an end), not standing "
    'facts. Return JSON: {"events":[{"description":..,"actor":..,'
    '"date":"YYYY-MM-DD","date_phrase":<the note\'s own words about '
    'when>,"source":<number of the note>}]}. Resolve date from dates '
    "written in the note (including a leading [date] stamp); exact "
    "calendar days only — when the note's words cannot pin an exact "
    "day, set date to null and keep date_phrase verbatim. Never invent "
    "a date. For example, the note [7] [2023/05/14 (Sun) 10:02] user: "
    "we finally adopted the kitten yesterday! — yields the single event "
    '{"description":"adopted a kitten","actor":"user",'
    '"date":"2023-05-13","date_phrase":"yesterday","source":7} inside '
    "the one events array. Skip standing facts, opinions, and "
    "narrative; extract each real occurrence once. Return "
    '{"events":[]} if nothing qualifies.'
)


def _vocab_hint(vocab: list[str]) -> str:
    if not vocab:
        return ""
    # Spell out the shape. These keys are "entity.attribute", but a bare list
    # of them reads as a list of ENTITY names — so extractors periodically
    # emitted {"entity": "0-9-0-release.deployment-status", "attribute":
    # "value"}, minting a dotted entity that duplicates a correctly-shaped
    # fact (2026-07-26). unflatten_slot_key_claims repairs what still slips.
    return ("\n\nExisting slot keys, each written entity.attribute (reuse when "
            'a note updates one — emit the part BEFORE the dot as "entity" and '
            'the part AFTER it as "attribute"; never put a whole key in '
            '"entity", and never use the literal "value" as an attribute): '
            + ", ".join(vocab[:60]))


def unflatten_slot_key_claims(claims: list, vocab: list[str]) -> list:
    """Repair claims that flattened a vocab slot key into the entity name.

    ``cortex.vocab()`` renders keys as ``entity.attribute`` where both halves
    are already separator-collapsed, so a key holds EXACTLY ONE dot and the
    split is unambiguous. An extractor "reusing" such a key sometimes copies
    the whole string into ``entity`` and writes the literal ``"value"`` as the
    attribute; the result is a dotted entity duplicating a correctly-shaped
    fact (``0-9-0-release.deployment-status``, 2026-07-26).

    Splits only when EVERY guard holds — the attribute is literally ``value``,
    the entity contains a dot, and the prefix is a known entity (or the whole
    string is a known key). Genuinely dotted entities (``llama.cpp``,
    ``host.docker.internal``) therefore survive untouched. Pure; the caller
    passes the same vocab it handed the extractor."""
    from pseudolife_memory.memory.cortex import _norm_key

    keys = {str(k) for k in (vocab or [])}
    entities = {k.split(".", 1)[0] for k in keys if "." in k}
    if not entities:
        return claims
    out = []
    for c in claims:
        head, dot, tail = str(c.get("entity", "")).rpartition(".")
        if (dot and head and tail
                and _norm_key(str(c.get("attribute", ""))) == "value"
                and (_norm_key(head) in entities
                     or f"{_norm_key(head)}.{_norm_key(tail)}" in keys)):
            logger.debug("unflattened slot-key claim: %r -> %r . %r",
                         c.get("entity"), head, tail)
            c = {**c, "entity": head, "attribute": tail}
        out.append(c)
    return out


def events_from_parsed(parsed: dict, n_texts: int) -> list[dict]:
    """Validate the extractor's ``events`` array (chronicle, schema v28).

    Events ride the same batched call as claims and travel back in the
    same list, marked ``kind: "event"`` — the service claim loop routes
    them before slot resolution. Conservative by construction: an event
    without a non-empty description is dropped; ``date`` must be an exact
    ``YYYY-MM-DD`` (anything else — "May 2023", a phrase, a fabricated
    format — degrades to None, keeping the verbatim ``date_phrase``);
    ``source`` maps to the 0-based note index exactly like claims and is
    omitted when out of range. Pure; returns ``[]`` for anything that is
    not a list of dicts."""
    from datetime import datetime

    raw = parsed.get("events", []) if isinstance(parsed, dict) else []
    out: list[dict] = []
    for ev in raw if isinstance(raw, list) else []:
        if not isinstance(ev, dict):
            continue
        description = str(ev.get("description", "")).strip()
        if not description:
            continue
        date = ev.get("date")
        if date is not None:
            date = str(date).strip()
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                date = None
        phrase = ev.get("date_phrase")
        item: dict = {
            "kind": "event",
            "description": description,
            "actor": str(ev.get("actor") or "user").strip() or "user",
            "date": date,
            "date_phrase": (str(phrase).strip() or None
                            if phrase is not None else None),
        }
        try:
            idx = int(ev.get("source")) - 1     # 1-based in the prompt
        except (TypeError, ValueError):
            idx = -1
        if 0 <= idx < n_texts:
            item["source"] = idx
        out.append(item)
    return out


# ── literal-faithfulness gate (2026-08-02 design doc) ────────────────────
# Date-like spans are exempt from gating: format variance ("2026-08-01" vs
# "August 1, 2026") makes digit matching unsafe, and the prompt's
# KEEP-LITERALS rule owns dates. The gate owns fabricated numbers,
# versions, and identifiers. Masking only ever removes tokens from the
# gateable set, so an over-broad date pattern fails open, never drops.
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE_LIKE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"                        # 2026-09-30
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"           # 9/30/26, 30-09-2026
    r"|\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b"             # 2026/9/30
    r"|\b\d{1,2}-[a-z]{3}-\d{2,4}\b"                # 30-Sep-2026
    rf"|\b(?:{_MONTHS})[a-z]*\.?\s+"                # September 30(, 2026)
    r"(?:\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|\d{4})\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?"   # 30(th) (of) September
    rf"(?:{_MONTHS})[a-z]*\.?(?:,?\s+\d{{4}})?\b",
    re.IGNORECASE)
_ORDINAL_RE = re.compile(r"^(\d+)(?:st|nd|rd|th)$")
_STRIP_PUNCT = ".,;:!?()[]{}<>\"'`#*"
# Single-word spelled numbers a note may use where the extractor writes the
# digit ("three week break" -> "3-week"). Compound forms ("twenty-five")
# arrive as hyphen parts and compose from these entries.
_SPELLED_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
    "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
    "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000",
}


def _norm_literal(token: str) -> str:
    """Normalize one token for literal matching: casefold, shed surrounding
    punctuation, currency/percent/approx marks, a trailing ``+`` (``2+`` =
    "2 or more"), thousands separators, a leading ``v`` on a version, and
    ordinal suffixes."""
    t = token.casefold().strip(_STRIP_PUNCT)
    t = t.lstrip("$€£~").rstrip("%+")
    t = t.replace(",", "").replace("_", "")
    if len(t) > 1 and t[0] == "v" and t[1].isdigit():
        t = t[1:]
    return _ORDINAL_RE.sub(r"\1", t)


def _literal_tokens(text: str, *, mask_dates: bool,
                    exempt_approx: bool = False) -> list[str]:
    src = _DATE_LIKE_RE.sub(" ", text) if mask_dates else text
    out = []
    for raw in src.split():
        if (exempt_approx
                and raw.casefold().strip(_STRIP_PUNCT).startswith("~")):
            # A value the extractor itself marks approximate ("~3 months")
            # is not a hard literal — same rationale as the date exemption.
            continue
        t = _norm_literal(raw)
        if not t:
            continue
        if "-" in t.strip("-"):
            # Internal hyphen: ranges ("1-3" ~ "1 to 3") and unit compounds
            # ("3-week", "66-acre") gate on their digit-bearing parts.
            out.extend(p for p in t.split("-")
                       if any(ch.isdigit() for ch in p))
        elif any(ch.isdigit() for ch in t):
            out.append(t)
    return out


def hard_literals(value: str) -> list[str]:
    """The gateable literals in a claim value: normalized digit-bearing
    tokens outside date-like spans, excluding extractor-marked
    approximations. Empty means the gate has nothing to check."""
    return _literal_tokens(value or "", mask_dates=True, exempt_approx=True)


def literal_violations(value: str, corpus: str) -> list[str]:
    """Gateable literals in ``value`` that ``corpus`` (source note text)
    does not back. Empty corpus abstains. A token passes on exact
    normalized match, numeric equality (``08`` ↔ ``8``, ``3.20`` ↔ ``3.2``),
    a spelled corpus form (``three week`` backs ``3-week``), or — for
    identifier-like tokens only, never bare numbers — a bidirectional
    substring match (``pr-81`` ↔ ``81``). Hyphenated ranges/compounds gate
    per digit part; ``~``-marked approximations are exempt."""
    if not (corpus or "").strip():
        return []
    gateable = hard_literals(value)
    if not gateable:
        return []
    # The corpus is evidence, not a claim — leave dates unmasked so their
    # digit parts can still back a token; extra tokens only fail open.
    corpus_tokens = set(_literal_tokens(corpus, mask_dates=False))
    # Spelled numbers back their digit forms ("three week" backs "3-week"):
    # exact single-word matches only — "hundreds" does not back 100.
    for raw in corpus.split():
        for word in raw.casefold().strip(_STRIP_PUNCT).split("-"):
            if word in _SPELLED_NUMBERS:
                corpus_tokens.add(_SPELLED_NUMBERS[word])
    bad = []
    for tok in gateable:
        if tok in corpus_tokens:
            continue
        try:
            num = float(tok)
        except ValueError:
            num = None
        for ct in corpus_tokens:
            if num is not None:
                try:
                    if float(ct) == num:
                        break
                except ValueError:
                    pass
            if (not tok.isdigit() and len(tok) >= 2 and len(ct) >= 2
                    and (tok in ct or ct in tok)):
                break
        else:
            bad.append(tok)
    return bad


_FACTS_HINT_HEAD = (
    "\n\nCurrent known facts (for key reuse — if a note updates one of "
    "these, emit the claim under the SAME entity and attribute with the new "
    "current value; never emit a claim the notes do not state):\n"
)


def _facts_hint(known_facts: list[tuple[str, str, str]] | None) -> str:
    if not known_facts:
        return ""
    return _FACTS_HINT_HEAD + "\n".join(
        f"- {e} — {a}: {v}" for e, a, v in known_facts)


_LESSON_SYSTEM_PROMPT = (
    "You consolidate an agent's work-outcome signals into reusable LESSONS. Each "
    "signal records something that happened while doing a task: a success, a "
    "failure/dead-end, or a user correction. Produce durable, actionable lessons "
    'as JSON: {"lessons":[{"task":..,"aspect":..,"lesson":..,"about":..,'
    '"polarity":"+"|"-","outcome":"success"|"failure"|"correction",'
    '"confidence":0..1}]}.\n'
    "- task = the kind of task, reusing stable wording across signals.\n"
    "- aspect = approach | pitfall | tool-choice | correction.\n"
    "- lesson = the actionable takeaway, phrased as what to DO (or what to avoid).\n"
    "- about = the tool/source/approach the lesson concerns.\n"
    "- outcome = the signal class it came from.\n"
    '- polarity = "+" when the lesson is something to DO — an approach that worked, '
    'or the corrected, now-correct way; "-" ONLY when the lesson is something to '
    'AVOID (a dead-end), phrased as "avoid X". A CORRECTION is almost always "+": '
    "state the new correct behavior to follow, never the mistake.\n"
    "Cluster related signals into one lesson. SKIP trivial or non-durable signals "
    "— generic knowledge any competent agent already has (e.g. basic "
    "language/library usage), one-off chatter, or anything a future run would not "
    'benefit from recalling. Return {"lessons":[]} if nothing qualifies.'
)


_OUTCOME_INFER_SYSTEM_PROMPT = (
    "You review the stored record of one work session and infer what "
    "OUTCOMES it reached. Reply with JSON only: {\"outcomes\": [{\"task\": "
    "<short stable task-type phrase>, \"outcome\": \"success\" | "
    "\"failure\" | \"correction\", \"about\": <tool/approach concerned, or "
    "null>, \"detail\": <one sentence of evidence quoted or paraphrased "
    "from the record>}]}.\n"
    "- Claim only outcomes the record actually evidences; prefer fewer, "
    "better-grounded claims.\n"
    "- failure = an approach was TRIED and hit a dead-end; correction = "
    "the USER explicitly corrected the assistant's belief or approach "
    "(an approach failing on its own is failure, not correction); "
    "success = something verifiably worked.\n"
    "- An outcome requires an ATTEMPT: something was tried, deployed, "
    "fixed, or decided, and its result is visible in the record. Sessions "
    "that only read, browse, take notes, or collect facts have NO "
    "outcome. An unfinished task, a deferred decision, or 'revisit "
    "later' is NOT an outcome — abstain. When unsure, abstain — a missed "
    "outcome is cheap, an invented one poisons downstream lessons.\n"
    "- Abstain example: record = 'Session: reading about css grid\\n"
    "- (notes) grid-template-areas allows named layout regions' -> "
    "{\"outcomes\": []} — a fact was noted, nothing was attempted.\n"
    "- If the record shows no clear outcome, return {\"outcomes\": []}."
)


_RELATIONS_PROMPT_HEAD = (
    "You extract durable RELATIONSHIPS between named entities from notes, as "
    'JSON: {"relations":[{"src":..,"relation":..,"dst":..}]}. Use ONLY these '
    "relation names:\n"
)
_RELATIONS_PROMPT_TAIL = (
    "\nAlways prefer the most specific listed relation. Use 'related-to' ONLY "
    "when the text explicitly states a meaningful connection that fits no "
    "listed relation — NEVER for entities that merely appear together in the "
    "same note. When no listed relation fits and no explicit connection is "
    "stated, skip the pair. src and dst are entity names (services, hosts, "
    "tools, components). Skip opinions, chit-chat, and anything with no "
    'entity-to-entity relationship. Return {"relations":[]} if nothing '
    "qualifies."
)


def _relations_prompt(relations: list[tuple[str, str]]) -> str:
    body = "\n".join(f"- {n}: {d}" for n, d in relations)
    return _RELATIONS_PROMPT_HEAD + body + _RELATIONS_PROMPT_TAIL


def _format_signals(signals: list[dict]) -> str:
    """Render outcome signals as compact lines for the synthesis prompt."""
    lines = []
    for s in signals or []:
        parts = [f"[{s.get('outcome', '?')}]", f"task={s.get('task', '')!r}"]
        if s.get("about"):
            parts.append(f"about={s['about']!r}")
        if s.get("detail"):
            parts.append(f"detail={s['detail']!r}")
        if s.get("polarity"):
            parts.append(f"polarity={s['polarity']}")
        line = " ".join(parts)
        if s.get("origin") == "inferred":
            line = f"[machine-inferred] {line}"
        lines.append(line)
    return "\n".join(lines)


def _parse_outcome_claims(content: str, cap: int) -> list[dict] | None:
    """Parse an outcome-inference reply. ``None`` = malformed (retryable),
    ``[]`` = the model found nothing (valid, advance), else claims.
    Enum violations are dropped, never coerced (record_outcome rule)."""
    import json as _json

    if cap <= 0:
        return []

    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = _json.loads(content[s:e + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict) or "outcomes" not in parsed:
        return None
    raw = parsed["outcomes"]
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        task = str(c.get("task", "")).strip()
        outcome = str(c.get("outcome", "")).strip()
        if not task or outcome not in ("success", "failure", "correction"):
            continue
        out.append({
            "task": task, "outcome": outcome,
            "about": str(c.get("about", "") or "").strip() or None,
            "detail": str(c.get("detail", "") or "").strip() or None,
        })
        if len(out) >= cap:
            break
    return out


class ExtractorError(Exception):
    """An extractor call failed (network, timeout, HTTP error, malformed
    response) — as opposed to succeeding with zero claims. Callers use this to
    distinguish a transient failure (don't advance the dream cursor / leave
    signals pending, retry next sweep) from a genuine empty result."""


class OpenAICompatExtractor:
    """Tier 2 — extract claims via any OpenAI-compatible ``/chat/completions``
    endpoint (Ollama, LM Studio, Anthropic/Haiku, OpenRouter, a self-hosted
    model — all the same slot). Bounded by ``max_tokens`` + a hard timeout. On
    failure (network, timeout, malformed JSON) it **raises** :class:`ExtractorError`
    so the caller can tell failure from a genuine empty result and avoid skipping
    memories (advancing the cursor) on a transient blip. A successful call with no
    extractable claims returns ``[]``. Uses stdlib urllib — no new deps."""

    def __init__(self, base_url: str, model: str, *, api_key: str | None = None,
                 max_tokens: int = 400, timeout_seconds: float = 20.0,
                 system_prompt: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or None
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout_seconds)
        # Base system prompt for claims extraction. Defaults to the shipped
        # ``_SYSTEM_PROMPT`` (the daemon never passes this arg, so its behaviour
        # is byte-identical). Off-label harnesses (e.g. the LME-V2 trajectory
        # smoke) pass a domain-specific variant; the vocab/known-facts hints are
        # still appended, so key-reuse across a batch is preserved.
        self.system_prompt = system_prompt if system_prompt is not None else _SYSTEM_PROMPT

    def extract(self, texts: list[str], vocab: list[str],
                known_facts: list[tuple[str, str, str]] | None = None,
                ) -> list[Claim]:
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system",
                     "content": self.system_prompt + _vocab_hint(vocab)
                                + _facts_hint(known_facts)},
                    # Numbered so the model can cite which note each claim came
                    # from ("source") — per-claim attribution without giving up
                    # the one-batch call that keeps cross-note naming consistent.
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(texts))},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                # Reasoning models (Qwen3, etc.) otherwise spend the entire
                # token budget on a <think> trace and return EMPTY content, so
                # extraction yields nothing and the cortex gets no write this
                # cycle. Templates that don't define this kwarg (e.g. Gemma)
                # just ignore it.
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            # Chatty/reasoning models often wrap the object in ```json fences or
            # emit leading prose; parse the outermost {...} object.
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("claims", []) if isinstance(parsed, dict) else []
            # Chronicle events (schema v28) ride the same call; validated
            # here and routed by the claim loop via their kind marker. An
            # events-less prompt (the shipped v5) simply yields none.
            events = events_from_parsed(parsed, len(texts))
        except Exception as exc:  # noqa: BLE001
            # Signal failure (vs genuine empty) so the dream doesn't advance its
            # cursor past these memories on a transient timeout/network blip.
            raise ExtractorError(f"extract failed: {exc}") from exc
        claims: list[Claim] = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            entity = str(c.get("entity", "")).strip()
            attribute = str(c.get("attribute", "")).strip()
            value = str(c.get("value", "")).strip()
            if not (entity and attribute and value):
                continue
            try:
                conf = max(0.0, min(1.0, float(c.get("confidence", 0.7))))
            except (TypeError, ValueError):
                conf = 0.7
            claim = Claim(entity=entity, attribute=attribute, value=value,
                          confidence=conf, origin="agent")
            if c.get("op") in ("add", "remove"):
                claim["op"] = c["op"]
            try:
                idx = int(c.get("source")) - 1     # 1-based in the prompt
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(texts):
                claim["source"] = idx
            claims.append(claim)
        return claims + events

    def extract_events(self, texts: list[str]) -> list[dict]:
        """The separate events pass: same endpoint and numbered-notes
        message, events-only system prompt (``_EVENTS_SYSTEM_PROMPT``),
        parsed by :func:`events_from_parsed`. Raises
        :class:`ExtractorError` on failure — the caller treats that as
        non-fatal (events are additive enrichment; claims must commit)."""
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _EVENTS_SYSTEM_PROMPT},
                    {"role": "user", "content": "\n\n".join(
                        f"[{i + 1}] {t}" for i, t in enumerate(texts))},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"events pass failed: {exc}") from exc
        return events_from_parsed(parsed, len(texts))

    def extract_lessons(self, signals: list[dict]) -> list[LessonClaim]:
        """Synthesise procedural lessons from outcome signals via the same
        endpoint. Returns ``[]`` on any failure (single-writer: the dream then
        writes no lessons this cycle and the signals stay pending)."""
        import json
        import urllib.request

        if not signals:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _LESSON_SYSTEM_PROMPT},
                    {"role": "user", "content": _format_signals(signals)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("lessons", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            # Raise (vs return []) so synthesize_lessons leaves the signals
            # pending and retries, rather than consuming them on a failed call.
            raise ExtractorError(f"extract_lessons failed: {exc}") from exc
        out: list[LessonClaim] = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict):
                continue
            task = str(c.get("task", "")).strip()
            lesson = str(c.get("lesson", "")).strip()
            if not (task and lesson):
                continue
            aspect = str(c.get("aspect", "") or "lesson").strip() or "lesson"
            about = str(c.get("about", "") or "").strip() or None
            polarity = "-" if str(c.get("polarity", "+")).strip() == "-" else "+"
            outcome = str(c.get("outcome", "success")).strip()
            if outcome not in ("success", "failure", "correction"):
                outcome = "success"
            try:
                conf = max(0.0, min(1.0, float(c.get("confidence", 0.6))))
            except (TypeError, ValueError):
                conf = 0.6
            out.append(LessonClaim(
                task=task, aspect=aspect, lesson=lesson, about=about,
                polarity=polarity, outcome=outcome, confidence=conf))
        return out

    def extract_relations(self, texts: list[str],
                          relations: list[tuple[str, str]]) -> list[RelationClaim]:
        """Extract (src, relation, dst) triples from ``texts`` via the same
        endpoint. ``relations`` are (name, description) pairs seeding the closed
        vocabulary. Raises ExtractorError on failure (vs a genuine empty [])."""
        import json
        import urllib.request

        texts = [t for t in (texts or []) if t]
        if not texts:
            return []
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _relations_prompt(relations)},
                    {"role": "user", "content": "\n\n".join(texts)},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e > s:
                content = content[s:e + 1]
            parsed = json.loads(content)
            raw = parsed.get("relations", []) if isinstance(parsed, dict) else []
        except Exception as exc:  # noqa: BLE001
            raise ExtractorError(f"extract_relations failed: {exc}") from exc
        out: list[RelationClaim] = []
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            src = str(r.get("src", "")).strip()
            rel = str(r.get("relation", "")).strip()
            dst = str(r.get("dst", "")).strip()
            if not (src and rel and dst):
                continue
            try:
                conf = max(0.0, min(1.0, float(r.get("confidence", 0.6))))
            except (TypeError, ValueError):
                conf = 0.6
            out.append(RelationClaim(src=src, relation=rel, dst=dst,
                                     confidence=conf))
        return out

    def infer_outcomes(self, context_text: str, *,
                       cap: int = 3) -> list[dict] | None:
        """Infer outcome signals from one closed episode's stored record.
        Transport failure raises ExtractorError (stage holds its cursor);
        malformed content returns None (bounded retry); [] is a valid
        nothing-found."""
        import json
        import urllib.request

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _OUTCOME_INFER_SYSTEM_PROMPT},
                    {"role": "user", "content": context_text},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
        except Exception as exc:  # noqa: BLE001 — transport, not content
            raise ExtractorError(f"infer_outcomes failed: {exc}") from exc
        return _parse_outcome_claims(content, cap)


_EXTRACTOR_MODES = ("auto", "primary", "fallback")


def resolve_endpoints(cfg) -> dict:
    """Resolve primary + fallback endpoint settings honouring the same
    env-vs-config ownership as ``build_extractor``: ``extractor_source ==
    "env"`` (the ops contract) lets PSEUDOLIFE_DREAM_* env vars override the
    dataclass; ``"config"`` uses the config values and ignores env. An
    unknown mode degrades to "auto" (never crash the sweep on a typo'd env
    var). Returns {mode, primary_url, primary_model, fallback_url,
    fallback_model, max_tokens, timeout}."""
    import os

    def _env_num(name, fallback, cast):
        raw = os.environ.get(name)
        if not raw:
            return fallback
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return fallback

    from_config = getattr(cfg, "extractor_source", "env") == "config"
    if from_config:
        out = {
            "primary_url": cfg.extractor_base_url,
            "primary_model": cfg.extractor_model,
            "fallback_url": cfg.fallback_base_url,
            "fallback_model": cfg.fallback_model,
            "mode": cfg.extractor_mode,
            "max_tokens": cfg.extractor_max_tokens,
            "timeout": cfg.extractor_timeout_seconds,
        }
    else:
        out = {
            "primary_url": (os.environ.get("PSEUDOLIFE_DREAM_BASE_URL")
                            or cfg.extractor_base_url),
            "primary_model": (os.environ.get("PSEUDOLIFE_DREAM_MODEL")
                              or cfg.extractor_model),
            "fallback_url": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL")
                             or cfg.fallback_base_url),
            "fallback_model": (os.environ.get("PSEUDOLIFE_DREAM_FALLBACK_MODEL")
                               or cfg.fallback_model),
            "mode": (os.environ.get("PSEUDOLIFE_DREAM_EXTRACTOR_MODE")
                     or cfg.extractor_mode),
            "max_tokens": _env_num("PSEUDOLIFE_DREAM_MAX_TOKENS",
                                   cfg.extractor_max_tokens, int),
            "timeout": _env_num("PSEUDOLIFE_DREAM_TIMEOUT_SECONDS",
                                cfg.extractor_timeout_seconds, float),
        }
    if out["mode"] not in _EXTRACTOR_MODES:
        out["mode"] = "auto"
    # Model-only override (console dreamer picker): wins over BOTH ownership
    # modes, primary only — URLs and the fallback model keep their owner.
    override = getattr(cfg, "extractor_model_override", None)
    if override:
        out["primary_model"] = override
    return out


def probe_endpoint(base_url: str, timeout: float = 3.0) -> bool:
    """Is an OpenAI-compatible endpoint alive? GET /health at the base with
    any trailing /v1 stripped (the sonnet shim serves /health at root and
    answers 503 when its CLI is logged out); a 404 there means a plain
    llama-server, so retry as GET {base_url}/models. Only HTTP 200 counts."""
    import urllib.error
    import urllib.request

    root = base_url.rstrip("/")
    root = root.removesuffix("/v1")
    for url in (f"{root}/health", f"{base_url.rstrip('/')}/models"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404 and url.endswith("/health"):
                continue                      # llama-server: try /models
            return False
        except Exception:  # noqa: BLE001 — connection refused, timeout, DNS
            return False
    return False


def _host_resolves(hostname: str) -> bool:
    import socket
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


_HOST_GATEWAY_NAME = "host.docker.internal"


def startup_extractor_warnings(cfg) -> list[str]:
    """Config-sanity checks for daemon startup — the misconfigurations that
    leave the dream pass silently on the wrong extractor (issues #11/#12).
    Returns human-readable warning strings; the caller logs them. The stock
    single-extractor default (in-stack sidecar, no fallback) stays silent."""
    r = resolve_endpoints(cfg)
    urls = [u for u in (r["primary_url"], r["fallback_url"]) if u]
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    out: list[str] = []
    if (any(_HOST_GATEWAY_NAME in u for u in urls)
            and not _host_resolves(_HOST_GATEWAY_NAME)):
        out.append(
            f"an extractor URL uses {_HOST_GATEWAY_NAME} but the name does not "
            "resolve — on Linux Docker Engine the daemon needs the extra_hosts "
            f"'{_HOST_GATEWAY_NAME}:host-gateway' entry in ops/docker-compose.yml "
            "(shipped enabled; restore it if removed). Until it resolves, every "
            "probe fails and dreams silently run on the fallback (or fail).")
    if (r["mode"] == "auto" and not has_fallback
            and r["primary_url"] and _HOST_GATEWAY_NAME in r["primary_url"]):
        out.append(
            f"dream primary {r['primary_url']} is host-side but no fallback is "
            "configured — extractor_mode=auto is inert (single-extractor, no "
            "probe) and dreams fail while the endpoint is down. Set "
            "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL/_MODEL to keep the in-stack "
            "sidecar as automatic fallback; verify with "
            'memory_dream(action="status").')
    if has_fallback and r["primary_url"] == r["fallback_url"]:
        out.append(
            f"dream primary and fallback are the same endpoint "
            f"({r['primary_url']}) — the intended primary is never used; "
            "point PSEUDOLIFE_DREAM_BASE_URL at the primary and verify with "
            'memory_dream(action="status").')
    return out


# Seconds between the two probe attempts in auto mode (tests zero this).
_probe_retry_delay = 2.0


def _probe_primary(url: str) -> bool:
    """Probe with ONE retry: the first probe after a daemon container restart
    reliably fails (host-gateway cold start) while the endpoint is healthy —
    2/2 live dreams on 2026-07-19 fell back spuriously on a healthy shim."""
    import time

    if probe_endpoint(url):
        return True
    time.sleep(_probe_retry_delay)
    return probe_endpoint(url)


def build_extractor_with_fallback(cfg) -> tuple["DreamExtractor", str]:
    """Selection step for the LIVE dream path: returns (extractor, which)
    with which in {"primary", "fallback"}. Fallback unset => exactly
    ``build_extractor`` (no probe, single-extractor behavior). Mode "auto"
    probes the primary per invocation — recovery is automatic at the next
    sweep. Raises ValueError for mode "fallback" with no fallback URL.
    The bench/eval harness never calls this — it constructs extractors
    directly so runs stay pinned to one endpoint."""
    import os

    r = resolve_endpoints(cfg)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if r["mode"] == "fallback":
        if not (r["fallback_url"] and r["fallback_model"]):
            raise ValueError(
                "extractor_mode=fallback but no fallback endpoint is "
                "configured (fallback_base_url/fallback_model)")
        return OpenAICompatExtractor(
            r["fallback_url"], r["fallback_model"], api_key=api_key,
            max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
        ), "fallback"
    if not (r["fallback_url"] and r["fallback_model"]) or r["mode"] == "primary":
        return build_extractor(cfg), "primary"
    # mode == "auto" with a configured fallback: probe (with one retry —
    # see _probe_primary), then choose.
    if r["primary_url"] and _probe_primary(r["primary_url"]):
        return build_extractor(cfg), "primary"
    logger.warning("dream primary extractor %s unreachable — using fallback %s",
                   r["primary_url"], r["fallback_url"])
    return OpenAICompatExtractor(
        r["fallback_url"], r["fallback_model"], api_key=api_key,
        max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
    ), "fallback"


def _status_extractor_fields(cfg, last_dream_extractor) -> dict:
    """Extractor-visibility block for ``dream_status`` (console badge).
    Probes the primary ONLY when a fallback is configured — the inert
    single-extractor deploy pays no probe cost on a status poll."""
    r = resolve_endpoints(cfg)
    has_fallback = bool(r["fallback_url"] and r["fallback_model"])
    return {
        "extractor_mode": r["mode"],
        "primary_url": r["primary_url"],
        "primary_model": r["primary_model"],
        "fallback_url": r["fallback_url"] if has_fallback else None,
        "fallback_model": r["fallback_model"] if has_fallback else None,
        "extractor_source": getattr(cfg, "extractor_source", "env"),
        "model_override": getattr(cfg, "extractor_model_override", None),
        "primary_healthy": (probe_endpoint(r["primary_url"], timeout=2.0)
                            if has_fallback and r["primary_url"] else None),
        "last_dream_extractor": last_dream_extractor,
    }


def build_extractor(cfg) -> DreamExtractor:
    """Pick the extractor from config: an OpenAI-compatible endpoint when a
    base-URL + model are set, else a no-op (no automatic regex writes —
    single-writer cortex; see the 2026-06-19 design).

    ``cfg.extractor_source`` decides who owns the endpoint settings:
    ``"env"`` (default, the documented ops contract) lets the
    ``PSEUDOLIFE_DREAM_BASE_URL`` / ``_MODEL`` / ``_TIMEOUT_SECONDS`` /
    ``_MAX_TOKENS`` env vars override the dataclass; ``"config"`` (set by
    the Console's Extractor panel) uses the config values and ignores those
    env vars — otherwise a UI change would silently lose to the env defaults
    the compose file always sets. ``PSEUDOLIFE_DREAM_API_KEY`` is honoured
    in both modes (secrets stay out of config.yaml).

    Resolution is delegated to :func:`resolve_endpoints` — the single
    authority the status display also reads, so what ``dream_status`` shows
    (including the model-only override) is what this builder constructs.
    A private copy of the env-vs-config logic here previously let the two
    drift."""
    import os

    r = resolve_endpoints(cfg)
    api_key = os.environ.get("PSEUDOLIFE_DREAM_API_KEY") or cfg.extractor_api_key
    if r["primary_url"] and r["primary_model"]:
        return OpenAICompatExtractor(
            r["primary_url"], r["primary_model"], api_key=api_key,
            max_tokens=r["max_tokens"], timeout_seconds=r["timeout"],
        )
    return NoOpExtractor()


def run_sweep_once(service) -> dict:
    """One headless sweep tick: if dreaming is enabled and the backlog+quiescence
    trigger would fire, run a dream with the configured extractor. Session-
    agnostic by construction (it keys on the cursor, not on session lifecycle).
    Returns ``{"fired": bool, ...}``; never raises into the daemon's timer."""
    cfg = service.config.memory.dream
    if not cfg.enabled:
        return {"fired": False, "reason": "disabled"}
    # Superseded-row compaction rides every tick (spec 2026-07-14) — it must
    # run even when no dream fires, or a quiet bank never compacts. The v27
    # dream-run journal retention rides the same tick for the same reason.
    compacted = service.compact_superseded().get("total", 0)
    runs_pruned = service.prune_dream_runs()
    status = service.dream_status()
    if not status["would_fire"]:
        return {"fired": False, "reason": "below_threshold",
                "backlog": status["backlog"], "compacted": compacted,
                "runs_pruned": runs_pruned}
    result = service.dream_run_auto()
    logger.info("dream sweep fired: %s", result)
    return {"fired": True, "compacted": compacted,
            "runs_pruned": runs_pruned, **result}
