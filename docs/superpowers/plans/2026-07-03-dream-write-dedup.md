# Dream Near-Duplicate Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `docs/specs/2026-07-03-dream-write-dedup-design.md` — write-time near-duplicate merge proposals (Tier 1), capable-model triage in `/dream deep` with an audit trail (Tier 2).

**Architecture:** A pure name-similarity matcher in `graph_review.py` feeds `entity_proposals` from the dream commit path; the deep-dream response enriches pending merge proposals with evidence; decisions get `decided_by`/`decided_at` stamps (additive schema v21) surfaced as `recent_merges` in review responses and Atlas.

**Tech Stack:** Python 3.11, pytest (PG-backed service tests), vanilla JS.

## Global Constraints

- Branch: `feat/dream-write-dedup` (created; spec committed).
- Schema change limited to two additive nullable columns on `entity_proposals` (`decided_by TEXT`, `decided_at DOUBLE PRECISION`), version 20 → 21.
- No auto-fold in code — merges applied only via `accept_merge` (model/human).
- Detector is advisory: failures log and continue, never block a dream write.
- Tool-description budget ≤1,600/tool, ≤18k total; test command `.venv/Scripts/python.exe -m pytest tests/ -q`.

---

### Task 1: `near_duplicate_names` pure matcher

**Files:** Modify `pseudolife_memory/memory/graph_review.py`; Test `tests/test_graph_review.py`.

**Produces:** `near_duplicate_names(name: str, existing: list[dict], *, min_jaccard: float = 0.6, dismissed: frozenset = frozenset()) -> list[dict]` — `existing` items are `{"id", "canonical", "display", "aliases": [str]}`; returns `[{"entity_id", "display", "score"}]` sorted by score desc; matches against canonical/display AND aliases; `dismissed` holds sorted `(canonical_a, canonical_b)` tuples (same shape as `duplicate_candidates`); the candidate name itself is compared via `_token_set`; empty token sets never match; `min_jaccard <= 0` returns `[]` (disabled).

- [ ] Failing tests: token-identical variants match (`graph_review` vs existing `graph_review.py` → score 1.0); alias match (name close to an alias); dismissed pair suppressed; below-threshold no match; `min_jaccard=0` disables.
- [ ] Implement using `_token_set` + Jaccard; take the best score across canonical/display/aliases per entity.
- [ ] Run `tests/test_graph_review.py -q`; commit `feat(graph-review): near_duplicate_names matcher for write-time dedup`.

### Task 2: Tier 1 — write-time detector in the dream commit path

**Files:** Modify `pseudolife_memory/service.py` (`_resolve_or_create_entity` ~3087, `_link_dream_relations` ~1608, `_ensure_subject_entity` ~700), `pseudolife_memory/utils/config.py` (`DreamConfig`); Test `tests/test_deep_dream.py` or `tests/test_graph_write_gating.py` (follow the existing dream-relation PG test pattern).

**Produces:**
- `_resolve_or_create_entity(name, etype=None, *, propose_dupes=False) -> dict` — result dict gains `"created": bool`; when `created` and `propose_dupes`, calls `_propose_write_dedup(new_id, name)`.
- `_propose_write_dedup(entity_id: int, name: str) -> None` — loads existing entities+aliases and `dismissed_pairs`, runs `near_duplicate_names` with `cfg.write_dedup_min_jaccard`, inserts `entity_proposals` merge rows via `insert_entity_proposal(kind="merge", entity_id=<lower-degree side>, into_id=<other>, score=jaccard, reason=f"write-dedup: {name!r} ~ {match_display!r}")` (the unique index dedupes); wrapped in try/except that logs and continues.
- `DreamConfig.write_dedup_min_jaccard: float = 0.6`.
- Dream call sites pass `propose_dupes=True`: both `_resolve_or_create_entity` calls in `_link_dream_relations`, and the dream-claim subject path (`_ensure_subject_entity` gains the same optional flag, passed only from the dream auto-promote caller — locate with `grep -n "_ensure_subject_entity" pseudolife_memory/service.py` and flag only the dream-side call).

- [ ] Failing PG tests: dream relations ingest (call `_link_dream_relations` directly, following an existing test that does so) minting `graph review` when `graph_review.py` exists files exactly one pending merge proposal with reason prefix `write-dedup:`; second run files nothing (unique index); a dismissed pair files nothing; `write_dedup_min_jaccard=0` files nothing; explicit `graph_relate` mints without proposing.
- [ ] Implement; run the file's suite + `tests/test_graph.py -q`; commit `feat(dream): write-time near-duplicate merge proposals (Tier 1)`.

### Task 3: Audit trail — decided_by/decided_at + recent_merges

**Files:** Modify `pseudolife_memory/storage/schema.py` (additive ALTERs + version 21), `pseudolife_memory/storage/postgres.py` (`set_entity_proposal_status` ~997, new `recent_entity_decisions(limit=20)`), `pseudolife_memory/service.py` (`graph_accept_entity_merge` ~3931, `graph_reject_entity_proposal` ~3958, `graph_review` ~3126), `pseudolife_memory/mcp_server.py` (`memory_graph_review` dispatch passes `decided_by="agent"`), `pseudolife_memory/web/routes.py` + `web/fixtures.py` + `web/static/js/atlas_review.js` (render `recent_merges` read-only list); Test `tests/test_entity_proposals.py`, `tests/test_graph_review.py` (route shape via fixture contract if pinned).

**Produces:**
- `set_entity_proposal_status(proposal_id, status, *, decided_by: str | None = None, decided_at: float | None = None) -> bool` (stamps when provided).
- `recent_entity_decisions(limit: int = 20) -> list[dict]` — decided (`status != 'pending'`) merge proposals newest-first by `decided_at`, each `{id, entity, into, status, score, reason, decided_by, decided_at}` (joins entities for display names; LEFT JOIN survives deleted rows).
- `graph_accept_entity_merge(proposal_id, *, decided_by="human")` / `graph_reject_entity_proposal(proposal_id, *, decided_by="human")` — stamp on decision; MCP dispatch passes `decided_by="agent"`; Console REST keeps the `"human"` default.
- `service.graph_review()` response gains `"recent_merges": [...]`.
- Atlas: a dim "recent merge decisions" list under the queue (name → name, decision badge, decider, `fmtAge`).

- [ ] Failing tests: accept via MCP-style call stamps `decided_by="agent"` + `decided_at`; Console default stamps `"human"`; `recent_entity_decisions` returns newest-first and includes rejections; `graph_review()["recent_merges"]` present.
- [ ] Implement schema ALTERs (`ADD COLUMN IF NOT EXISTS`, bump version constant to 21), storage, service, MCP, route/fixture/JS (node --check the JS).
- [ ] Run `tests/test_entity_proposals.py tests/test_graph_review.py tests/test_fixture_contract.py -q`; commit `feat(audit): decided_by/decided_at on entity proposals + recent_merges surface (schema v21)`.

### Task 4: Tier 2 — deep-dream merge-proposal evidence + driver docs

**Files:** Modify `pseudolife_memory/service.py` (deep response assembly ~3750-3790), `examples/commands/dream.md`, `docs/runbooks/deep-dream.md`; Test `tests/test_deep_dream.py`, `tests/test_tool_consolidation.py`.

**Produces:** the deep response (both dry-run and apply) gains `"merge_proposals"`: pending `kind="merge"` rows enriched per side with `{display, etype, scopes, degree, snippets}` — snippets via `_attach_candidate_snippets`-style lookup (reuse `traces`/`mentions` already computed in the deep pass; respect `include_snippets` and `snippet_max_chars`), oriented so `into` is the higher-degree side (swap `entity_id`/`into_id` in the *presentation* only, keeping proposal ids). Driver docs instruct per-pair: accept_merge / dismiss_pair + reject_entity / leave pending, with judgment criteria (same referent vs distinct artifacts; heed project scopes).

- [ ] Failing PG test: seed a pending write-dedup proposal, run `memory_deep_dream` dry-run, assert `merge_proposals[0]` carries both sides' `display`, `degree`, `snippets` list (possibly empty), and proposal `id`.
- [ ] Implement enrichment + docs; run `tests/test_deep_dream.py tests/test_tool_consolidation.py -q`; commit `feat(deep-dream): merge-proposal evidence payload + Step-C merge triage driver`.

### Task 5: Integration pass

- [ ] Full suite green; `CHANGELOG.md` entry (Tier 1 detector, audit columns v21, Tier 2 evidence + driver); `README.md` blurbs (`memory_dream` deep response, `memory_graph_review` accept_merge stamping); commit `docs: dream near-duplicate correction (write-dedup + deep-dream triage + audit)`.
