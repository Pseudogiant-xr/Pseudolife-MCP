"""Meta-statement filter — prevents self-referential memory poisoning.

The system can store model responses that describe the memory system's own state
(e.g. "I don't have any cat-related material saved") as memories. These
meta-statements then compete with actual content during retrieval, causing
contradictory results.

This module detects and rejects such content before it enters the memory pipeline.
"""

from __future__ import annotations

import re

# Patterns that indicate the assistant is talking ABOUT its memory/knowledge
# rather than conveying useful information
_META_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bI don'?t have\b.*\b(?:saved|stored|recorded|memory|memories|material|information)\b",
        r"\bnot (?:stored|saved|recorded|in my (?:memory|knowledge))\b",
        r"\bno (?:material|information|data|records?) (?:saved|stored|about)\b",
        r"\bI don'?t (?:recall|remember)\b.*\b(?:any|specific)\b",
        r"\bnot in my (?:memory|knowledge|records?)\b",
        r"\bI (?:have|had) no (?:memory|memories|record|records?) (?:of|about|regarding)\b",
        r"\bmy (?:memory|memories|neural memory|memory banks?) (?:don'?t|doesn'?t|do not)\b",
        r"\bnothing (?:stored|saved|recorded) (?:about|regarding|on)\b",
        r"\bno relevant memories?\b",
        r"\bI (?:can'?t|cannot) find (?:any|that) in my memory\b",
        r"\bmy (?:memory|knowledge) (?:doesn'?t|does not) (?:contain|include|have)\b",
        r"\baccording to my (?:memory|memories|neural memory)\b",
        r"\bbased on (?:my|the) (?:memory|memories|recalled|retrieved)\b",
        r"\bfrom (?:my|the) (?:memory banks?|neural memory|retrieved memories)\b",
        r"\bmy TITANS (?:memory|system)\b",
        r"\bmy (?:instant|short-term|long-term|reference) (?:bank|memory)\b",
        r"\bsurprise (?:score|gating|threshold)\b",
        r"\bmemory (?:bank|banks|system|module|pipeline)\b.*\b(?:stores?|contains?|holds?)\b",
    ]
]

# Minimum content length — very short responses are likely meta
_MIN_CONTENT_LENGTH = 30


def is_meta_statement(text: str, role: str = "assistant") -> bool:
    """Check if text is a self-referential meta-statement about the memory system.

    Only filters assistant-role content. User messages are never filtered
    since they represent genuine information the user wants remembered.

    Args:
        text: The text to check.
        role: The role of the speaker ("user" or "assistant").

    Returns:
        True if the text should be rejected (is a meta-statement).
    """
    if role == "user":
        return False

    # Very short assistant responses are often meta ("I don't know about that")
    # but we still check patterns rather than blanket-rejecting
    stripped = text.strip()
    if not stripped:
        return True

    for pattern in _META_PATTERNS:
        if pattern.search(stripped):
            return True

    return False


def filter_meta_content(text: str, role: str = "assistant") -> str | None:
    """Filter text, returning None if it's a meta-statement.

    This is the main entry point for the meta-filter pipeline.

    Args:
        text: Content to potentially store.
        role: Speaker role.

    Returns:
        The original text if it passes the filter, None if rejected.
    """
    if is_meta_statement(text, role):
        return None
    return text
