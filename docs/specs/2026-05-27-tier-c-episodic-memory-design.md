# Tier C — Episodic Memory + Tag Slice (Design Spec)

**Status:** Approved 2026-05-27. Targets PseudoLife-MCP after Tier B2 ships.

## Motivation

The literature on long-term LLM agent memory ([Position paper, ICML 2025](https://arxiv.org/abs/2502.06975); [MIRIX, 2024](https://arxiv.org/abs/2507.07957); [HiMem, 2026](https://arxiv.org/abs/2601.06377)) converges on three under-implemented capabilities:

1. **Episodic ↔ semantic distinction** — raw experiences with temporal/contextual binding vs. abstracted facts.
2. **Consolidation** — turning clusters of related episodes into reusable semantic notes. Called out as the single most-important / least-implemented capability.
3. **Contextual binding** — *when/where/why* an event occurred, beyond a bare timestamp.

PseudoLife-MCP today flattens all memory into one shape (text + embedding + source-tag + timestamp). Tier C adds episodic structure and a Claude-driven consolidation workflow without requiring MCP sampling (Claude Code doesn't support it yet — see [feature request #1785](https://github.com/anthropics/claude-code/issues/1785)).

## Scope

In:
- Schema v6 additive fields on `MemoryEntry`: `episode_id`, `episode_title`, `tags`.
- `EpisodeManager` module with start/end/list/get/stamp lifecycle.
- Retrieval filters: `episodes=`, `tags=` on `memory_search` / `memory_recent` / `memory_trace`.
- Atomic `memory_consolidate(replaces, new_text)` that supersedes the old set and stores the new in one call.
- Clustering-based `memory_consolidation_candidates` tool that surfaces ripe-for-consolidation clusters.
- 8 new MCP tools, 3 extended signatures.

Out (deferred):
- Auto-titling episodes from content (needs LLM).
- Reflection via MCP sampling (Claude Code doesn't support it; revisit when it lands).
- Knowledge-vault / verbatim memory typing.
- Procedural memory typing.

## Data model — Schema v6

Additive on `MemoryEntry`:

```python
episode_id: str | None = None     # uuid4 hex; None for entries stored outside any episode
episode_title: str | None = None  # denormalised display label
tags: list[str] = field(default_factory=list)  # deduplicated, lowercased, stripped
```

`SCHEMA_VERSION = 6`. Load path defaults missing fields when reading pre-v6 saves. No migration code beyond defaulting — older saves keep working untouched.

`source: str` stays a single string (project/topic scope). Tags are the multi-valued companion axis.

## EpisodeManager

```python
@dataclass
class Episode:
    id: str            # uuid4 hex (32 chars)
    title: str
    started_at: float  # unix ts
    ended_at: float | None = None
    hint: str | None = None
    closed_by_new_start: bool = False  # set by auto-close path
```

```python
class EpisodeManager:
    episodes: dict[str, Episode]
    current_id: str | None

    def start(self, title: str, hint: str | None = None) -> Episode: ...
    def end(self) -> Episode | None: ...           # closes current if any
    def list(self, limit=20, include_open=True) -> list[Episode]: ...  # newest first
    def get(self, id: str) -> Episode | None: ...
    def stamp(self, entry: MemoryEntry) -> None: ...  # fills episode_id/title if current open
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "EpisodeManager": ...
```

**Auto-close on start-while-open.** Calling `start()` while another episode is open closes the prior one and stamps `closed_by_new_start=True`. Rationale: Claude won't always reliably end episodes — graceful degradation into "current working session" semantics is better than errors.

**Persistence.** Serialised alongside bands in `cms_state.pt` under a top-level `"episodes"` key. Single atomic save/load with the rest of CMS state.

## CMS integration

- `ContinuumMemorySystem.__init__` constructs an `EpisodeManager`.
- `CMS.store(text, embedding, source, tags=None)` — after creating the entry, calls `episodes.stamp(entry)` so it carries the current episode's id/title.
- `CMS.retrieve(...)` and `CMS.retrieve_with_trace(...)` accept `tags: list[str] | None`, `episodes: list[str] | None` filters AND-combined with existing `sources`/`bands`. Applied at the same tier-iteration point so trace records reflect them.
- `CMS.save / CMS.load` serialise/restore the EpisodeManager.

## Tag normalisation

```python
def _normalize_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    seen = set()
    out = []
    for t in tags:
        if not isinstance(t, str):
            continue
        norm = t.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out
```

Stored on the entry. Filters use `set(entry.tags) & set(filter_tags)` non-empty test.

## Consolidation clustering

Pure deterministic greedy algorithm (no LLM):

```python
def cluster_candidates(
    candidates: list[tuple[MemoryEntry, float]],  # (entry, relevance_score)
    *,
    min_cohesion: float = 0.6,
    min_cluster_size: int = 2,
    max_clusters: int = 10,
) -> list[Cluster]:
    """
    1. Sort by relevance desc.
    2. While unclustered candidates remain and len(clusters) < max_clusters:
       a. seed = highest-relevance unclustered
       b. cluster = [seed]
       c. for c in remaining unclustered:
            if cosine(seed.embedding, c.embedding) >= min_cohesion:
              cluster.append(c)
       d. cohesion = mean(cosine(a,b) for all pairs in cluster)
       e. if len(cluster) >= min_cluster_size: keep
       f. mark all cluster members as clustered
    3. Sort clusters by (cohesion × len) desc.
    """
```

`Cluster` carries: `members: list[MemoryEntry]`, `cohesion: float`, `seed_score: float`.

## Service layer

New `MemoryService` methods:

```python
def episode_start(self, title: str, hint: str | None = None) -> dict
def episode_end(self) -> dict
def episode_list(self, limit: int = 20, include_open: bool = True) -> dict
def episode_summary(self, id: str) -> dict       # stats + tag distribution + recent entries
def list_tags(self) -> dict                       # counts like list_sources
def consolidation_candidates(
    self, query: str | None = None, episode: str | None = None,
    top_k: int = 10, min_cohesion: float = 0.6,
) -> dict
def consolidate(
    self, replaces: list[str], new_text: str,
    source: str | None = None, tags: list[str] | None = None,
) -> dict                                          # atomic supersede + store
```

Extended signatures:

```python
def store(text, source="claude", tags=None) -> dict
def search(query, top_k=None, sources=None, bands=None, episodes=None, tags=None,
           min_score=None, disable_recency_boost=False, rerank=None, bm25=None) -> dict
def trace(query, top_k=None, sources=None, bands=None, episodes=None, tags=None,
          rerank=None, bm25=None) -> dict
def recent(n=10, sources=None, episodes=None, tags=None) -> dict
def delete(text=None, substring=None, source=None, episode=None, tag=None) -> dict
```

## MCP tool surface

Eight new tools, three extended.

| Tool | Args | Purpose |
|---|---|---|
| `memory_episode_start` | `title, hint?` | Open episode, auto-close prior if needed |
| `memory_episode_end` | — | Close current |
| `memory_episode_list` | `limit?, include_open?` | List newest-first |
| `memory_episode_summary` | `id` | Stats + tag distribution + recent entries |
| `memory_list_tags` | — | Tag count taxonomy |
| `memory_consolidation_candidates` | `query?, episode?, top_k?, min_cohesion?` | Cluster ripe-for-consolidation memories |
| `memory_consolidate` | `replaces, new_text, source?, tags?` | Atomic supersede + store |
| Extended `memory_store` | + `tags?` | |
| Extended `memory_search`, `memory_trace` | + `episodes?, tags?` | |
| Extended `memory_recent` | + `episodes?, tags?` | |
| Extended `memory_delete` | + `episode?, tag?` | |

## Test plan

| File | New tests | Coverage |
|---|---|---|
| `test_episodes.py` (new) | ~14 | Lifecycle, double-start auto-close, stamp behaviour, persistence round-trip, edge cases |
| `test_consolidation.py` (new) | ~10 | Clustering: cohesion threshold, ordering, min_cluster_size, empty/singleton |
| `test_service.py` (extend) | ~15 | Integration: episode_start → store → search-with-filter, tag flow, consolidate atomicity, schema v6 backward-compat load |
| `test_mcp_server.py` (extend) | ~6 | Dispatch + docstring sanity for 8 new tools |
| **Total new** | **~45** | 87 → ~132 |

## Implementation order (TDD)

1. C-1 Schema v6 additive on `MemoryEntry` + load/save round-trip
2. C-2 `EpisodeManager` module + unit tests
3. C-3 Wire `EpisodeManager` into CMS (stamp on store, save/load alongside bands)
4. C-4 Tag plumbing through store + retrieval filters
5. C-5 Service-level episode lifecycle + tag-filtered search
6. C-6 Consolidation clustering module + unit tests
7. C-7 `memory_consolidate` atomic operation + service plumbing
8. C-8 Wire 8 new MCP tools + extend 3 existing
9. C-9 README + commit + push

## Risks / open questions

- **Episode auto-close on start** is the right default but means a stale open episode can leak in if Claude forgets — mitigated by the `closed_by_new_start` flag and by `memory_episode_list` surfacing currently-open episodes.
- **Cohesion threshold 0.6** is conservative; surfacing too few clusters is failure-by-silence. May want to lower the default after live observation.
- **Clustering cost** is O(N²) on the candidate pool; we keep it bounded by pre-filtering to the top-50 relevance candidates before clustering. Cost on a 40K bank stays sub-100ms.
