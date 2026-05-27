"""Text chunking strategies for document ingestion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextChunk:
    """A chunk of text with metadata."""

    text: str
    index: int  # position in original document
    source: str = ""  # optional source identifier


def sliding_window_chunks(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    source: str = "",
) -> list[TextChunk]:
    """Split text into overlapping chunks using a sliding window.

    Uses word boundaries to avoid splitting mid-word.
    chunk_size and chunk_overlap are approximate (in words, not tokens).
    """
    words = text.split()
    if not words:
        return []

    # Ensure overlap doesn't exceed chunk_size (would cause infinite loop)
    effective_overlap = min(chunk_overlap, chunk_size - 1)
    stride = max(chunk_size - effective_overlap, 1)

    chunks = []
    start = 0
    idx = 0

    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk_text = " ".join(words[start:end])
        chunks.append(TextChunk(text=chunk_text, index=idx, source=source))
        idx += 1

        if end >= len(words):
            break
        start += stride

    return chunks


def sentence_chunks(text: str, source: str = "") -> list[TextChunk]:
    """Split text into sentence-level chunks.

    Useful for conversation history where each turn is a natural unit.
    """
    # Simple sentence splitting - handles ., !, ? followed by space + capital
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [
        TextChunk(text=s.strip(), index=i, source=source)
        for i, s in enumerate(sentences)
        if s.strip()
    ]
