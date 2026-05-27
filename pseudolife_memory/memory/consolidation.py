"""Greedy clustering of mutually-similar memories for Claude-driven
consolidation (Tier C C-6).

The motivation
--------------

Long-running PseudoLife banks accumulate near-duplicate memories — the
same fact phrased five different ways across five sessions. The
literature on agent memory (HiMem 2026; MIRIX 2024; the ICML 2025
position paper) calls this out as the single most-important
under-implemented capability: **consolidation**, turning episodes into
semantic notes.

We can't run an LLM inside the MCP server (Claude Code doesn't expose
sampling yet — see README "What's not built yet"). But we *can* surface
clusters of related memories so Claude can read them and decide what to
consolidate via the existing ``memory_supersede`` / ``memory_consolidate``
tools.

The algorithm
-------------

Deterministic greedy clustering. Given a candidate pool ranked by
retrieval relevance:

1. Sort candidates by relevance descending (so high-relevance entries
   seed clusters).
2. While unclustered candidates remain and ``len(clusters) <
   max_clusters``:

   a. Pick the highest-relevance unclustered candidate as the seed.
   b. Scan the remaining unclustered candidates; any whose cosine to
      the seed clears ``min_cohesion`` joins the cluster.
   c. Compute the cluster's cohesion as the mean of all intra-cluster
      pairwise cosines.
   d. If ``len(cluster) >= min_cluster_size``, keep it; otherwise
      discard the cluster (the seed and its candidates stay unclustered
      so subsequent loops can re-pair them with other candidates).

3. Sort the kept clusters by ``cohesion × len`` descending — the LLM
   sees the most-promising consolidation first.

The O(N²) cost is bounded by capping the candidate pool upstream
(typically 50). For larger pools the caller batches in pre-filtered
chunks; the function itself is index-agnostic and assumes the pool is
already what should be considered.

The clustering is a *function*, not a class with state — every call is
independent, every output deterministic given the inputs. Embeddings
are assumed normalised (the embedding pipeline does this); the dot
product is the cosine. No NumPy / torch.nn machinery — plain tensor
ops keep this importable in test contexts that don't load the full
embedder.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch

from pseudolife_memory.memory.titans_memory import MemoryEntry


@dataclass
class Cluster:
    """One consolidation-candidate cluster.

    ``members`` is the list of entries, ordered by their relevance
    score within the pool (highest first). ``cohesion`` is the mean
    intra-cluster cosine — a one-shot quality signal for the LLM.
    ``seed_score`` is the relevance score of the highest-relevance
    member, useful for downstream ranking.
    """

    members: list[MemoryEntry]
    cohesion: float
    seed_score: float


def _cosine(a: torch.Tensor, b: torch.Tensor) -> float:
    """Cosine similarity between two vectors. Assumes ``embedding`` is
    a non-zero tensor; falls back to ``0.0`` on zero-norm to keep the
    function total (a zero embedding is malformed, but shouldn't crash).
    """
    a = a.detach().cpu().float()
    b = b.detach().cpu().float()
    an = float(a.norm())
    bn = float(b.norm())
    if an == 0.0 or bn == 0.0:
        return 0.0
    return float(torch.dot(a, b) / (an * bn))


def _cluster_cohesion(members: list[MemoryEntry]) -> float:
    """Mean of all distinct intra-cluster pairwise cosines.

    Returns 0.0 for clusters of size < 2 — the caller drops these via
    ``min_cluster_size`` anyway.
    """
    n = len(members)
    if n < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _cosine(members[i].embedding, members[j].embedding)
            pairs += 1
    return total / pairs if pairs else 0.0


def cluster_candidates(
    candidates: list[tuple[MemoryEntry, float]],
    *,
    min_cohesion: float = 0.6,
    min_cluster_size: int = 2,
    max_clusters: int = 10,
) -> list[Cluster]:
    """Greedily cluster ``candidates`` by mutual cosine similarity.

    Args:
        candidates: ``(entry, relevance_score)`` pairs. Relevance score
            decides seed order — higher scores seed earlier.
        min_cohesion: Minimum cosine between seed and a candidate to
            include the candidate in the cluster. Treated as
            inclusive — a cosine of exactly ``min_cohesion`` *is*
            included.
        min_cluster_size: Drop clusters with fewer members than this
            threshold. ``2`` is the natural floor — a singleton has
            nothing to consolidate against.
        max_clusters: Hard cap on the number of clusters returned. Keeps
            MCP responses bounded for the LLM.

    Returns:
        Clusters sorted by ``cohesion × len`` descending. Empty list
        when no candidates survive ``min_cluster_size`` or the pool
        itself is empty.
    """
    if not candidates:
        return []

    # Stable sort: higher relevance seeds the cluster, ties broken by
    # insertion order. Tuple of (-score, index) gives the right order.
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (-pair[1][1], pair[0]))
    ordered = [pair[1] for pair in indexed]

    clustered: set[int] = set()
    clusters: list[Cluster] = []

    for i, (seed_entry, seed_score) in enumerate(ordered):
        if len(clusters) >= max_clusters:
            break
        if i in clustered:
            continue
        members = [seed_entry]
        member_indices = [i]

        for j in range(i + 1, len(ordered)):
            if j in clustered:
                continue
            cand_entry, _ = ordered[j]
            if _cosine(seed_entry.embedding, cand_entry.embedding) >= min_cohesion:
                members.append(cand_entry)
                member_indices.append(j)

        if len(members) < min_cluster_size:
            # Leave the seed + provisional members unclustered so the
            # algorithm can try other groupings on subsequent loops.
            # In practice the next iteration would just produce the
            # same result, but this avoids accidentally locking the
            # entry out of clustering forever if the threshold ever
            # gets relaxed mid-loop.
            continue

        # Lock the members in; compute cohesion only on the kept set.
        for idx in member_indices:
            clustered.add(idx)
        clusters.append(
            Cluster(
                members=members,
                cohesion=_cluster_cohesion(members),
                seed_score=seed_score,
            )
        )

    clusters.sort(key=lambda c: c.cohesion * len(c.members), reverse=True)
    return clusters
