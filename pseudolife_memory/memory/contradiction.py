"""Contradiction detection and belief revision for the memory system.

When new information contradicts existing memories, this module detects the
conflict and actively decays the contradicted memory's weight rather than
letting both coexist. This mimics belief revision — new evidence weakens
old conflicting beliefs.

Four detection paths are combined:

  1. **Negation asymmetry** — one text has an explicit negation/correction
     cue (not / never / actually / wrong …) and the other doesn't. Triggered
     at a moderate similarity threshold (``NEGATION_SIM_THRESHOLD`` = 0.70).

  2. **Affirmative-replacement** — both texts are affirmative but share a
     strong topic frame (high cosine similarity and high non-stopword token
     overlap) while differing on a "slot value" token (a capitalised word,
     number, or quoted string). Triggered only at a stricter similarity
     threshold (``REPLACEMENT_SIM_THRESHOLD`` = 0.80) so that merely related
     topics are not treated as corrections.

  3. **State transition** — one text asserts possession/affiliation ("I have
     a cat named Jacque") and the other describes a loss of that state
     ("I gave him away", "the cat died", "I sold it"). Fires when the two
     texts share at least one anchor token (a named entity, quoted value,
     or content noun) and the cosine similarity is at least
     ``STATE_TRANSITION_SIM_THRESHOLD`` = 0.35 — low, because state-change
     statements are often terse and semantically distant from the original
     affirmation.

  4. **NLI path** (optional) — a locally bundled CrossEncoder NLI model
     scores the remaining candidate pairs directly for the "contradiction"
     label. Runs only when an :class:`~src.memory.nli.NLIContradictionScorer`
     is supplied and available. Catches cases the heuristics miss, e.g.
     "I have an RTX 4090" → "I upgraded to a 5090" with no shared token
     anchor. Bounded by ``nli_candidate_cap`` entries per call so latency
     stays predictable.

When a contradiction is detected we both decay the old entry's
``surprise_score`` (reducing its eviction resistance) and stamp
``superseded_at`` on it so that :meth:`ContinuumMemorySystem.retrieve`
can hide it from the LLM.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.titans_memory import MemoryEntry

if TYPE_CHECKING:
    from pseudolife_memory.memory.nli import NLIContradictionScorer


# Tunable thresholds — exported for tests and callers.
NEGATION_SIM_THRESHOLD = 0.70
REPLACEMENT_SIM_THRESHOLD = 0.80
REPLACEMENT_TOKEN_OVERLAP_THRESHOLD = 0.50
# State-transition path uses tier-specific cosine floors driven by the
# *kind* of anchor that linked the gain and loss statements. A shared slot
# value (named entity, number, quoted string) is much stronger evidence
# than a generic content-token overlap or a pronoun referent, so the
# slot-anchored floor is the most permissive.
#
# Empirical motivation: real conversation pairs like
# "I have a Ragdoll cat named Jacque" vs "you no longer have Jacque"
# share ``jacque`` as a slot value but have raw cosine sim ≈ 0.34 — barely
# below a uniform 0.35 floor. The slot anchor itself is so specific that
# 0.15 is more than sufficient.
STATE_TRANSITION_SIM_THRESHOLD_SLOT = 0.15
STATE_TRANSITION_SIM_THRESHOLD_CONTENT = 0.35
STATE_TRANSITION_SIM_THRESHOLD_PRONOUN = 0.35
# Backwards-compatible legacy threshold name. Callers/tests still reading
# this get the most conservative floor.
STATE_TRANSITION_SIM_THRESHOLD = STATE_TRANSITION_SIM_THRESHOLD_CONTENT
# Minimum cosine similarity for a candidate to be sent to the NLI scorer.
# Low enough to catch "upgraded to 5090" (no shared anchor) but high enough
# to exclude truly unrelated entries.
NLI_SIM_FLOOR = 0.25

# Negation cues that often indicate contradiction
_NEGATION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bnot\b",
        r"\bno\b",
        r"\bnever\b",
        r"\bdon'?t\b",
        r"\bdoesn'?t\b",
        r"\bwon'?t\b",
        r"\bcan'?t\b",
        r"\bisn'?t\b",
        r"\baren'?t\b",
        r"\bwasn'?t\b",
        r"\bweren'?t\b",
        r"\bneither\b",
        r"\bnor\b",
        r"\bactually\b",
        r"\binstead\b",
        r"\brather\b",
        r"\bcontrary\b",
        r"\bincorrect\b",
        r"\bwrong\b",
        r"\bfalse\b",
        r"\bmistaken\b",
        r"\bcorrect(?:ion|ed|ly)?\b",
        r"\bupdate[ds]?\b",
        r"\bchange[ds]?\b",
        r"\bno longer\b",
        r"\bused to\b",
    ]
]


def _has_negation(text: str) -> bool:
    """Check if text contains negation/correction cues."""
    for p in _NEGATION_PATTERNS:
        if p.search(text):
            return True
    return False


def _negation_asymmetry(text_a: str, text_b: str) -> bool:
    """Check if exactly one of two texts has negation (asymmetric negation).

    If both or neither have negation, it's less likely to be a contradiction.
    """
    a_neg = _has_negation(text_a)
    b_neg = _has_negation(text_b)
    return a_neg != b_neg


# Small stop-word set for token-overlap computation. Kept intentionally small
# — only high-frequency function words — so content-bearing tokens dominate.
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "am", "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their", "this",
    "that", "these", "those", "and", "or", "but", "of", "to", "in", "on",
    "at", "for", "with", "as", "by", "from", "into", "about", "so", "do",
    "does", "did", "have", "has", "had", "will", "would", "could", "should",
    "can", "may", "might", "s", "t", "ll", "re", "ve", "d", "m",
})

# Token splitter: words (including contractions) and integer-like numbers.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d+")

# Matches likely "slot value" tokens: capitalised words (names, places),
# standalone numbers, or quoted strings. These are the tokens most likely
# to differ when a user is correcting a specific fact.
_SLOT_VALUE_RE = re.compile(
    r"""
        "[^"]+"          |   # double-quoted
        '[^']+'          |   # single-quoted
        \b[A-Z][A-Za-z\-]+\b |  # Capitalised word
        \b\d+(?:\.\d+)?\b       # number
    """,
    re.VERBOSE,
)


def _content_tokens(text: str) -> list[str]:
    """Lowercased content tokens with stop-words removed."""
    return [
        tok for tok in (m.group(0).lower() for m in _WORD_RE.finditer(text))
        if tok not in _STOP_WORDS
    ]


def _jaccard_overlap(a: list[str], b: list[str]) -> float:
    """Jaccard similarity over bags of content tokens."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    return len(inter) / len(union)


def _slot_values(text: str) -> set[str]:
    """Extract likely slot-value tokens from text, normalized for comparison.

    Strips surrounding quotes and lowercases capitalised words so that
    ``"Rex"`` and ``Rex`` both normalise to ``rex``.
    """
    out: set[str] = set()
    for m in _SLOT_VALUE_RE.finditer(text):
        tok = m.group(0)
        if tok.startswith(('"', "'")) and tok.endswith(('"', "'")):
            tok = tok[1:-1]
        out.add(tok.lower())
    return out


# Cues that indicate the speaker has LOST possession / a relationship /
# a state they previously had. Exported so context_builder.py can reuse
# the list for conflict clustering without duplicating patterns.
#
# Most "V X away/out" patterns use ``.{0,40}`` between the verb and the
# particle so phrasal forms like "gave the cat away" match alongside
# pronoun forms like "gave him away".
POSSESSION_LOSS_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in [
        r"\bgave\b.{0,40}?\baway\b",
        r"\bgave\s+up\b",
        r"\bgiv(?:ing|e[ns]?)\b.{0,40}?\baway\b",
        r"\bgot\s+rid\s+of\b",
        r"\bsold\b",
        r"\blost\b",
        r"\brehomed\b",
        r"\bdonated\b",
        r"\bpassed\s+away\b",
        r"\bdied\b",
        r"\bno\s+longer\s+(?:have|own|with|around|together|here)\b",
        r"\bis\s+(?:no\s+longer|not)\s+with\s+(?:us|me|you|them)\b",
        r"\bis\s+gone\b",
        r"\bisn'?t\s+(?:here|around|with)\b",
        r"\bused\s+to\s+have\b",
        r"\bused\s+to\s+own\b",
        r"\bgot\s+divorced\b",
        r"\bbroke\s+up\b",
        r"\bmoved\s+(?:out|away)\b",
        r"\bquit\b",
        r"\bthrew\b.{0,40}?\b(?:away|out)\b",
        r"\bdiscard(?:ed|ing)?\b",
        r"\babandon(?:ed|ing)?\b",
        # "upgraded to / switched to / replaced … with / traded … for" —
        # these signal that an older possession has been swapped out.
        r"\bupgrad(?:ed|ing)\s+(?:to|from)\b",
        r"\bswitch(?:ed|ing)\s+(?:to|from)\b",
        r"\breplac(?:ed|ing)\b.{0,40}?\bwith\b",
        r"\btrad(?:ed|ing)\b.{0,40}?\bfor\b",
    ]
]

# Cues that indicate the speaker currently HAS / OWNS the thing being
# discussed. Kept narrower than the loss set so that only confident
# affirmations anchor a state-transition pair.
POSSESSION_GAIN_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bi\s+have\b",
        r"\bi\s+own\b",
        r"\bi\s+got\b",
        r"\bi'?ve\s+got\b",
        r"\bi'?ve\s+had\b",
        r"\bmy\s+[A-Za-z]",  # "my dog", "my car", etc.
        r"\bnamed\b",
        r"\bcalled\b",
    ]
]


def _matches_any(patterns: list[re.Pattern], text: str) -> bool:
    for p in patterns:
        if p.search(text):
            return True
    return False


def _has_loss_cue(text: str) -> bool:
    """True if text mentions loss of possession / relationship / state."""
    return _matches_any(POSSESSION_LOSS_PATTERNS, text)


def _has_gain_cue(text: str) -> bool:
    """True if text affirms current possession / ownership."""
    return _matches_any(POSSESSION_GAIN_PATTERNS, text)


# Third-person pronouns that can stand in for a previously-named referent.
# A loss statement that uses one of these without its own anchor noun is
# almost certainly referring back to something mentioned earlier.
_REFERENT_PRONOUN_RE = re.compile(
    r"\b(?:it|he|she|him|her|them|they|his|hers|its|their|theirs)\b",
    re.IGNORECASE,
)


def _has_pronoun_referent(text: str) -> bool:
    return bool(_REFERENT_PRONOUN_RE.search(text))


def _shared_anchor(text_a: str, text_b: str) -> bool:
    """Do two texts share a specific anchor — a named entity or content noun?

    An anchor is either:
      - a slot-value token (capitalised name, number, quoted string), or
      - any shared non-stopword content token.

    This is deliberately loose: even a single shared content word is enough
    to tie a state-change statement ("I sold the car") to a specific prior
    fact ("I have a blue car").
    """
    return _shared_anchor_kind(text_a, text_b) is not None


def _shared_anchor_kind(text_a: str, text_b: str) -> str | None:
    """Strength-tier version of :func:`_shared_anchor`.

    Returns:
        * ``"slot"`` if the two texts share a slot-value token (a named
          entity, number, or quoted string).  Strongest signal — almost
          always indicates they're about the same specific thing.
        * ``"content"`` if they only share generic content tokens (common
          nouns, verbs, adjectives) with no slot-value overlap.  Useful
          but ambiguous — multiple statements can share a noun like
          "cat" without being about the same cat.
        * ``None`` if no anchor at all.

    Used by :func:`_state_transition_anchor_kind` to pick a cosine-similarity
    floor that scales with the anchor's specificity.
    """
    slots_a = _slot_values(text_a)
    slots_b = _slot_values(text_b)
    if slots_a & slots_b:
        return "slot"

    tokens_a = set(_content_tokens(text_a))
    tokens_b = set(_content_tokens(text_b))
    if tokens_a & tokens_b:
        return "content"
    return None


def _looks_like_state_transition(text_a: str, text_b: str) -> bool:
    """Backwards-compatible boolean wrapper around
    :func:`_state_transition_anchor_kind`. Returns True iff any anchor (slot,
    content, or pronoun) ties the two texts."""
    return _state_transition_anchor_kind(text_a, text_b) is not None


def _state_transition_anchor_kind(text_a: str, text_b: str) -> str | None:
    """Return the *kind* of anchor that links two texts via gain/loss asymmetry.

    Tiered output (strongest first):
        * ``"slot"`` — both texts mention the same named entity / quoted /
          numeric slot value (e.g. ``"Jacque"`` or ``"RTX 4090"``).
        * ``"content"`` — they share a generic content noun like ``"cat"``
          without sharing a slot value.
        * ``"pronoun"`` — the loss side uses a referent pronoun (``him`` /
          ``her`` / ``it`` / …) and has no slot-value anchor of its own, so
          it must be referring back to the gain side.
        * ``None`` — no gain/loss asymmetry, or no anchor.

    Callers (see :func:`detect_contradictions`) use this to pick a tiered
    cosine-similarity floor — slot anchors are confident enough that a low
    floor still gives high precision.
    """
    a_gain, a_loss = _has_gain_cue(text_a), _has_loss_cue(text_a)
    b_gain, b_loss = _has_gain_cue(text_b), _has_loss_cue(text_b)

    # Need asymmetry: one side gain, the other side loss.
    if not ((a_gain and b_loss) or (b_gain and a_loss)):
        return None

    kind = _shared_anchor_kind(text_a, text_b)
    if kind is not None:
        return kind

    # Pronoun-only fallback. Identify which side is the loss statement,
    # then check that it has a referent pronoun and NO slot-value anchors
    # of its own (so we don't tie unrelated named things together).
    loss_text = text_b if b_loss else text_a
    if _has_pronoun_referent(loss_text) and not _slot_values(loss_text):
        return "pronoun"

    return None


# Floor lookup keyed by the anchor kind returned by
# :func:`_state_transition_anchor_kind`. Slot-anchored gain/loss pairs are
# accepted at a much lower cosine sim than content- or pronoun-anchored
# pairs — see the per-threshold docstring at the top of the module.
_STATE_TRANSITION_FLOOR_BY_KIND: dict[str, float] = {
    "slot": STATE_TRANSITION_SIM_THRESHOLD_SLOT,
    "content": STATE_TRANSITION_SIM_THRESHOLD_CONTENT,
    "pronoun": STATE_TRANSITION_SIM_THRESHOLD_PRONOUN,
}


def _looks_like_replacement(text_a: str, text_b: str) -> bool:
    """Detect affirmative-vs-affirmative fact replacements.

    Returns True when the two texts share a topic frame and differ on at
    least one slot-value token — e.g. "my dog's name is Rex" vs "my dog's
    name is Max", or "I have an RTX 4090" vs "I have an RTX 5090".

    A "topic frame" is satisfied when EITHER:

    * their non-stopword content-token Jaccard is ≥
      :data:`REPLACEMENT_TOKEN_OVERLAP_THRESHOLD` (catches longer parallel
      statements), OR
    * they share at least one slot-value token (catches short parallel
      declarations where the overlap is concentrated in named entities
      or numbers — e.g. "RTX" appears in both but Jaccard on all content
      tokens is below 0.5).
    """
    slots_a = _slot_values(text_a)
    slots_b = _slot_values(text_b)

    # A differing slot is one that appears in exactly one text. Without a
    # differing slot there's no "correction" to make — both texts express
    # the same fact.
    if not (slots_a ^ slots_b):
        return False

    tokens_a = _content_tokens(text_a)
    tokens_b = _content_tokens(text_b)
    if _jaccard_overlap(tokens_a, tokens_b) >= REPLACEMENT_TOKEN_OVERLAP_THRESHOLD:
        return True

    # Fallback: a shared slot-value token is a strong topic anchor even
    # when the overall Jaccard is low (short statements, few content words).
    if slots_a & slots_b:
        return True

    return False


def detect_contradictions(
    new_text: str,
    new_embedding: torch.Tensor,
    existing_entries: list[MemoryEntry],
    similarity_threshold: float = NEGATION_SIM_THRESHOLD,
    device: str = "cpu",
    *,
    nli_scorer: NLIContradictionScorer | None = None,
    nli_candidate_cap: int = 8,
) -> list[MemoryEntry]:
    """Find existing memories that the new text contradicts.

    An entry is flagged when any of these holds:

    1. **Negation-asymmetry path** — cosine similarity ≥ ``similarity_threshold``
       AND exactly one of the two texts contains a negation/correction cue.
    2. **Affirmative-replacement path** — cosine similarity ≥
       :data:`REPLACEMENT_SIM_THRESHOLD` AND the texts share a topic frame
       (content-token Jaccard ≥ :data:`REPLACEMENT_TOKEN_OVERLAP_THRESHOLD`)
       but differ on at least one slot-value token.
    3. **State-transition path** — cosine similarity ≥
       :data:`STATE_TRANSITION_SIM_THRESHOLD` AND one text affirms possession
       while the other reports a loss of that state, against a shared anchor
       token (named entity or content noun).
    4. **NLI path** (when *nli_scorer* is provided and available) — entries
       not flagged by paths 1–3, not superseded, cosine ≥ :data:`NLI_SIM_FLOOR`
       are scored in a single batched call. The top ``nli_candidate_cap``
       by similarity are used to bound latency. Pairs whose contradiction
       probability ≥ ``nli_scorer.threshold`` are flagged.

    Entries already marked superseded are skipped — we never re-contradict
    something that has already been replaced.

    Args:
        new_text: The new content being stored.
        new_embedding: Embedding of the new content.
        existing_entries: List of existing memory entries to check against.
        similarity_threshold: Minimum similarity for the negation path.
            The replacement path uses ``REPLACEMENT_SIM_THRESHOLD`` instead.
        device: Torch device.
        nli_scorer: Optional NLI scorer for path 4. When ``None`` or
            unavailable, path 4 is skipped and only the three heuristic
            paths run.
        nli_candidate_cap: Maximum number of entries to send to the NLI
            scorer per call (sorted by cosine similarity, highest first).

    Returns:
        List of entries that are contradicted by the new text.
    """
    if not existing_entries:
        return []

    # Build matrix of existing embeddings
    embeddings = torch.stack([e.embedding.to(device) for e in existing_entries])
    embeddings = F.normalize(embeddings, p=2, dim=1)

    query = new_embedding.to(device)
    query = F.normalize(query.unsqueeze(0), p=2, dim=1).squeeze(0)

    # Cosine similarities
    sims = (embeddings @ query).tolist()

    contradicted: list[MemoryEntry] = []
    for entry, sim in zip(existing_entries, sims):
        if entry.superseded_at is not None:
            continue  # already replaced; don't double-count

        # Path 1: explicit negation asymmetry at moderate similarity
        if sim >= similarity_threshold and _negation_asymmetry(new_text, entry.text):
            contradicted.append(entry)
            continue

        # Path 2: affirmative-vs-affirmative replacement at stricter similarity
        if sim >= REPLACEMENT_SIM_THRESHOLD and _looks_like_replacement(new_text, entry.text):
            contradicted.append(entry)
            continue

        # Path 3: state transition (gain/loss asymmetry with a shared anchor).
        # The cosine floor scales with the anchor's specificity — sharing a
        # slot value (named entity like "Jacque") clears at a far lower sim
        # than a generic content-token or pronoun-only fallback. Without
        # this tiering, real conversation pairs like
        # "I have a Ragdoll cat named Jacque" vs
        # "you no longer have Jacque" miss the uniform 0.35 floor by ~0.01.
        anchor_kind = _state_transition_anchor_kind(new_text, entry.text)
        if anchor_kind is not None:
            floor = _STATE_TRANSITION_FLOOR_BY_KIND[anchor_kind]
            if sim >= floor:
                contradicted.append(entry)

    # Path 4: NLI — catch cases the heuristics miss (e.g. "I have an RTX
    # 4090" → "I upgraded to a 5090" with no shared token anchor).
    if nli_scorer is not None and nli_scorer.is_available():
        flagged_set = set(id(e) for e in contradicted)
        # Collect unflagged, unsuperseded candidates above the cosine floor.
        nli_candidates: list[tuple[float, MemoryEntry]] = [
            (sim, entry)
            for entry, sim in zip(existing_entries, sims)
            if (
                entry.superseded_at is None
                and id(entry) not in flagged_set
                and sim >= NLI_SIM_FLOOR
            )
        ]
        # Sort by cosine descending, cap at nli_candidate_cap.
        nli_candidates.sort(key=lambda t: t[0], reverse=True)
        nli_candidates = nli_candidates[:nli_candidate_cap]

        if nli_candidates:
            pairs = [(entry.text, new_text) for _, entry in nli_candidates]
            flagged_indices = nli_scorer.flagged_indices(pairs)
            for idx in flagged_indices:
                contradicted.append(nli_candidates[idx][1])

    return contradicted


def decay_contradicted_entries(
    entries: list[MemoryEntry],
    decay_factor: float = 0.3,
    superseding_text: str | None = None,
) -> int:
    """Decay and mark contradicted entries as superseded.

    Three effects:

    * ``surprise_score`` is multiplied by ``decay_factor``, lowering the
      entry's eviction resistance and its weight in promotion decisions.
    * ``superseded_at`` is stamped with the current time so that retrieval
      can filter the entry out (see :meth:`ContinuumMemorySystem.retrieve`).
    * ``superseded_by_text`` is set to ``superseding_text`` (schema v5,
      v0.7.6) — the text of the new memory that triggered this
      supersession. Used by the context builder to render both facts
      together so the LLM can describe the correction even when the
      new memory's own embedding doesn't make it into retrieval.
      Overwrites any prior value because the most recent superseder is
      the most informative.

    Args:
        entries: Entries to decay and mark superseded.
        decay_factor: Multiply surprise score by this factor (0-1).
        superseding_text: Text of the new memory that supersedes these
            entries. ``None`` for callers that don't have it (e.g.
            programmatic supersession or legacy paths).

    Returns:
        Number of entries modified.
    """
    now = time.time()
    count = 0
    for entry in entries:
        entry.surprise_score *= decay_factor
        if entry.superseded_at is None:
            entry.superseded_at = now
        if superseding_text:
            entry.superseded_by_text = superseding_text
        count += 1
    return count
