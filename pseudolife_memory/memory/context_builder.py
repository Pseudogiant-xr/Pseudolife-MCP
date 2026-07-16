"""Context builder - constructs augmented prompts from memory retrievals."""

from __future__ import annotations

import time

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.contradiction import (
    POSSESSION_LOSS_PATTERNS,
    _content_tokens,
    _looks_like_state_transition,
)
# NOTE: the TITANS CMS passes its own ``RetrievalResult`` (with a ``surprises``
# field); importing the legacy hopfield class here used to silently break
# once the conflict-reorder path actually returned a rebuilt result.
from pseudolife_memory.memory.titans_memory import RetrievalResult
from pseudolife_memory.utils.config import ContextConfig


SYSTEM_PROMPT_TEMPLATE = """\
You are Pseudolife — an AI assistant augmented with a MIRAS continuum neural memory \
system: eight parametric memory tiers (working / micro / instant / fast / medium / \
slow / archival / forever) updated by gradient descent at inference time, plus a \
Reference Bank for uploaded documents. Surprise-based gating + contradiction \
detection decide what is worth remembering and what supersedes prior facts.

{memory_context}

Guidelines:
- Reference retrieved memories naturally when they're relevant
- Each memory is labeled with how long ago it was recorded. If two memories \
conflict on the same fact (e.g. different names, places, or numbers for the \
same thing), **trust the more recent one** — the user's latest statement \
supersedes older ones.
- Watch for **state changes**. If one memory says the user has/owns/got \
something and a more recent memory says they gave it away, sold it, lost it, \
it died, or they no longer have it, the newer state wins — the user no \
longer has that thing. Answer based on the latest state, not the first \
mention.
- You can mention that you "recall" information from memory
- If no memories are relevant, respond normally without forcing references
- You have access to a web_search tool — use it when you need current information
"""


# Similarity threshold above which two retrieved memories are considered
# to be talking about the same fact, triggering the recency tie-break.
_CONFLICT_SIM_THRESHOLD = 0.85

# For state-change clustering: minimum content-token Jaccard between two
# entries before we're willing to call them same-topic (even when their
# cosine similarity is below the paraphrase threshold).
_STATE_CLUSTER_TOKEN_OVERLAP = 0.30


def _relative_time(timestamp: float, now: float | None = None) -> str:
    """Render a timestamp as a coarse human-readable offset.

    Buckets: "just now", "N minutes ago", "N hours ago", "N days ago".
    Unknown or zero timestamps return "unknown time".
    """
    if not timestamp:
        return "unknown time"
    now = now if now is not None else time.time()
    age = max(now - timestamp, 0.0)
    if age < 60:
        return "just now"
    if age < 3600:
        minutes = int(age // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if age < 86400:
        hours = int(age // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(age // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _has_loss_cue(text: str) -> bool:
    """True if ``text`` contains a possession-loss phrase."""
    for pattern in POSSESSION_LOSS_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _token_jaccard(a: str, b: str) -> float:
    tokens_a = set(_content_tokens(a))
    tokens_b = set(_content_tokens(b))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _is_same_topic_cluster(entry_a, entry_b, sim: float) -> bool:
    """Decide whether two retrieved entries belong to the same topic cluster.

    A pair is clustered when any of the following is true:

    * Their embedding cosine similarity is ≥ :data:`_CONFLICT_SIM_THRESHOLD`
      — i.e. they are near-paraphrases of the same fact (covers Rex/Max).
    * They look like a state-transition pair in the sense used by the
      contradiction detector (gain/loss asymmetry with a shared anchor
      or pronoun fallback). Reusing the detector's heuristic keeps the
      two code paths from drifting apart.
    * They share at least one slot-value token AND their content-token
      Jaccard exceeds :data:`_STATE_CLUSTER_TOKEN_OVERLAP` (a weaker
      fallback for topic clusters that are paraphrastic but below the
      0.85 cosine bar).
    """
    if sim >= _CONFLICT_SIM_THRESHOLD:
        return True
    text_a, text_b = entry_a.text, entry_b.text
    if _looks_like_state_transition(text_a, text_b):
        return True
    if _token_jaccard(text_a, text_b) >= _STATE_CLUSTER_TOKEN_OVERLAP:
        return True
    return False


def _reorder_conflicts_by_recency(result: RetrievalResult) -> RetrievalResult:
    """Tie-break near-duplicate retrievals by recency, newer first.

    After the existing relevance sort, we walk the list and for any pair of
    entries that look like they belong to the same topic cluster
    (see :func:`_is_same_topic_cluster`), we move the newer one ahead of
    the older one. This is a stable, single-pass adjustment — it only
    reorders within same-topic clusters, not across unrelated memories.
    """
    n = len(result.entries)
    if n < 2:
        return result

    # Stack + normalise once for O(n²) pairwise similarity (n is small, ≤ top_k).
    try:
        emb = torch.stack([e.embedding for e in result.entries])
        emb = F.normalize(emb, p=2, dim=1)
        sims = (emb @ emb.T).tolist()
    except Exception:
        return result  # If tensors don't line up, leave order untouched.

    order = list(range(n))
    changed = True
    # Bounded bubble pass: at most n sweeps, each comparing adjacent pairs.
    for _ in range(n):
        if not changed:
            break
        changed = False
        for i in range(len(order) - 1):
            a, b = order[i], order[i + 1]
            if not _is_same_topic_cluster(result.entries[a], result.entries[b], sims[a][b]):
                continue
            if result.entries[b].timestamp > result.entries[a].timestamp:
                order[i], order[i + 1] = order[i + 1], order[i]
                changed = True

    if order == list(range(n)):
        return result
    return RetrievalResult(
        entries=[result.entries[i] for i in order],
        scores=[result.scores[i] for i in order],
        surprises=[result.surprises[i] for i in order],
    )


class ContextBuilder:
    """Builds augmented prompts by injecting retrieved memories into context."""

    def __init__(self, config: ContextConfig) -> None:
        self.config = config

    def build_system_prompt(
        self,
        retrieval_result: RetrievalResult,
        base_system_prompt: str = "",
    ) -> str:
        """Build a system prompt with memory context injected."""
        # Tie-break near-duplicate memories by recency (newer first) so that
        # if two entries on the same fact survive retrieval, the fresh one
        # is rendered first — reinforced by the system-prompt guidance.
        ordered = _reorder_conflicts_by_recency(retrieval_result)
        memory_context = self._format_memories(ordered)

        if not memory_context:
            if base_system_prompt:
                return base_system_prompt
            return "You are a helpful AI assistant."

        prompt = SYSTEM_PROMPT_TEMPLATE.format(memory_context=memory_context)

        if base_system_prompt:
            prompt = base_system_prompt + "\n\n" + prompt

        return prompt

    def _format_memories(self, result: RetrievalResult) -> str:
        """Format retrieved memories into a readable context block.

        Neural memories (instant/short-term/long-term) are shown under
        "Your Memories" — these represent things the AI actually remembers
        from past interactions.

        Reference documents (ChromaDB RAG) are shown separately under
        "Reference Documents" — background knowledge from uploaded files.

        A token budget (max_memory_tokens × 4 chars) prevents context bloat.
        """
        if not result.entries:
            return ""

        # Rough char budget: 4 chars ≈ 1 token
        char_budget = self.config.max_memory_tokens * 4
        used_chars = 0

        neural_lines: list[str] = []
        ref_lines: list[str] = []
        neural_count = 0

        for entry, score in zip(result.entries, result.scores):
            is_ref = getattr(entry, "bank", "") == "reference"
            source_tag = f" [{entry.source}]" if entry.source else ""

            if is_ref:
                header = f"Reference Document{source_tag} (relevance: {score:.3f}):"
                body = f"  {entry.text}"
            else:
                neural_count += 1
                when = _relative_time(entry.timestamp)
                # v0.7.6 cat-Jacque fix: surface supersession in the
                # rendered memory so the LLM can tell current state
                # from outdated facts. Pre-fix, ``superseded_at`` was
                # set on the entry but invisible in the formatted
                # context — the LLM saw "Memory 1: I have a Ragdoll
                # cat named Jacque" and answered "yes, you have a cat"
                # even when the gave-away memory was in the bank but
                # below MIN_SCORE for the query's embedding.
                is_superseded = entry.superseded_at is not None
                state_marker = " **SUPERSEDED — outdated, do not treat as current**" if is_superseded else ""
                header = (
                    f"Memory {neural_count}{source_tag}{state_marker} "
                    f"(relevance: {score:.3f}, {when}, accessed {entry.access_count}x):"
                )
                body_lines = [f"  {entry.text}"]
                # Schema v5 (v0.7.6): when we know the text that
                # superseded this entry, render it inline so the LLM
                # has the correction even when its embedding missed
                # retrieval. Falls back to a generic marker when the
                # superseder's text isn't recorded (pre-v5 entries).
                if is_superseded:
                    sup_text = getattr(entry, "superseded_by_text", None)
                    if sup_text:
                        body_lines.append(
                            f"  ↳ CORRECTED BY (current state): {sup_text!r}"
                        )
                    else:
                        body_lines.append(
                            "  ↳ This fact has been superseded by a newer statement — do not affirm it."
                        )
                body = "\n".join(body_lines)

            entry_text = header + "\n" + body + "\n"
            if used_chars + len(entry_text) > char_budget:
                break

            if is_ref:
                ref_lines.append(entry_text)
            else:
                neural_lines.append(entry_text)
            used_chars += len(entry_text)

        sections: list[str] = []

        if neural_lines:
            sections.append(
                "=== Your Memories ===\n"
                "(Ranked by associative relevance to your query)\n\n"
                + "\n".join(neural_lines)
            )

        if ref_lines:
            sections.append(
                "=== Reference Documents ===\n"
                "(Background knowledge from uploaded files)\n\n"
                + "\n".join(ref_lines)
            )

        return "\n\n".join(sections)
