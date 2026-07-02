"""ReferenceBank scoring (2026-07-02 review M1)."""

from pseudolife_memory.memory.reference_bank import cosine_similarity_from_distance


def test_chroma_distance_to_similarity():
    """ChromaDB cosine distance is 1 - cos (range [0,2]); similarity must be
    1 - dist. The old 1 - dist/2 mapped an ORTHOGONAL chunk to 0.5 — above
    the 0.25 retrieval floor — so unrelated documents were appended to
    essentially every search result."""
    assert cosine_similarity_from_distance(0.0) == 1.0     # identical
    assert cosine_similarity_from_distance(1.0) == 0.0     # orthogonal -> floor-fails
    assert cosine_similarity_from_distance(2.0) == 0.0     # opposite, clamped
    assert cosine_similarity_from_distance(0.3) == 0.7
