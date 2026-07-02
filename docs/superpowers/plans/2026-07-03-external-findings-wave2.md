# External Findings Wave 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship features C (lesson staleness "re-verify" flags) and F (causal chain — "what led to X") from `docs/specs/2026-07-03-external-findings-design.md`.

**Architecture:** C is a read-time annotation joining lessons against cortex fact churn (no stored state). F is a new service method `chain(entity)` merging four dated event streams (facts, entries, edges, lessons), surfaced by making `memory_history`'s `attribute` optional, a `GET /api/chain` route, and a timeline block in the Console's entity-provenance drawer.

**Tech Stack:** Python 3.11, pytest, torch, vanilla JS.

## Global Constraints

- Branch: `feat/external-findings-wave2` (created off post-wave-1 master).
- No schema migrations. Tool-description budget ≤1,600/tool, ≤18k total.
- Graceful degradation: failed/empty enrichment → un-enriched result, never an error.
- Test command: `.venv/Scripts/python.exe -m pytest tests/ -q`.

---

### Task 1: C — Lesson staleness annotation

**Files:**
- Modify: `pseudolife_memory/service.py` (`lesson_search` ~line 1698, `lessons_dump` ~line 1713; new private helpers)
- Modify: `pseudolife_memory/memory/briefing.py` (`_fmt_lesson`)
- Test: `tests/test_lessons_service.py`, `tests/test_briefing.py`

**Interfaces:**
- Produces: `MemoryService._annotate_lesson_staleness(rows: list[dict]) -> list[dict]` — mutates/returns rows, adding `re_verify: True` and `re_verify_reason: str` where stale.
- Staleness rule: resolve `row["about"]` (fallback `row["task"]`) to a cortex entity — exact `_norm_key` match, else graph alias → canonical; if the latest cortex change for that entity (`max(asserted_at, superseded_at)` over ALL records, current + superseded) is newer than `max(row["asserted_at"], row["last_confirmed"])`, flag it.

**Steps:**
- [ ] Failing tests: service-level (write fact → outcome-derived lesson older than a later fact churn at the same entity gets flagged; unrelated lesson unflagged; unresolvable `about` unflagged); briefing `_fmt_lesson` renders `⚠ re-verify` suffix when `re_verify` set.
- [ ] Implement `_cortex_change_index()` (norm-entity → latest change ts, one pass over `self._cortex.records`) + `_annotate_lesson_staleness(rows)`; call from `lesson_search` and `lessons_dump` return paths.
- [ ] Briefing: `_fmt_lesson` appends `" ⚠ re-verify (facts changed since)"` when `e.get("re_verify")`.
- [ ] Run `tests/test_lessons_service.py tests/test_briefing.py tests/test_lessons.py -q`; commit `feat(lessons): read-time re-verify staleness flags`.

### Task 2: F — `chain()` service method + MCP surface

**Files:**
- Modify: `pseudolife_memory/service.py` (new `chain` method near `history` ~line 1810)
- Modify: `pseudolife_memory/mcp_server.py` (`memory_history` ~line 415 — `attribute` becomes optional)
- Test: `tests/test_graph.py` (PG-backed, `svc` fixture), `tests/test_tool_consolidation.py`

**Interfaces:**
- Produces: `MemoryService.chain(entity: str, limit: int = 20) -> dict` = `{found, entity, count, events}`; each event `{t: float, kind: fact_set|superseded|entry|edge|lesson, summary: str, refs: dict}`, sorted ascending by `t`, truncated to the most recent `limit`.
- Streams: (1) cortex records for the alias-resolved entity — `fact_set` at `asserted_at`, plus `superseded` at `superseded_at` with `superseded_by_value`; (2) `storage.entries_for_entity(eid)` — `entry` at `ts`, summary = first 160 chars, refs carry `entry_id` + `episode_title` when present; (3) `load_graph` edges touching `eid` — `edge` at `asserted_at`; (4) lessons whose `about`/`task` norm-matches — `lesson` at `asserted_at`.
- Missing storage/graph node → facts + lessons streams only; unknown entity with no data → `{found: False}`.
- MCP: `memory_history(entity, attribute=None)`; `attribute=None` → `service.chain(entity)`; docstring documents both modes within budget.

**Steps:**
- [ ] Failing PG test in `tests/test_graph.py`: relate entities + set/supersede a fact + store an entry mentioning the entity, assert chain events are time-ordered, kinds present, supersession event carried.
- [ ] Implement `chain()`; wire MCP optional-attribute dispatch.
- [ ] Run `tests/test_graph.py tests/test_tool_consolidation.py -q`; commit `feat(chain): 'what led to X' causal chain (service + memory_history entity-only mode)`.

### Task 3: F — Console surface (`/api/chain` + drawer timeline)

**Files:**
- Modify: `pseudolife_memory/web/routes.py` (register `GET /api/chain`)
- Modify: `pseudolife_memory/web/static/js/atlas_review.js` (provenance drawer gains a "chain" section fetched lazily)
- Modify: `pseudolife_memory/web/fixtures.py` (fixture chain payload for devserver)
- Test: `tests/test_fixture_contract.py` (or the routes test file if one exists — follow existing `/api/graph/entity-provenance` test pattern)

**Steps:**
- [ ] Route: `g("/api/chain", lambda q, b: svc.chain(_s(q, "entity"), _i(q, "limit", 20)))` (+ FixtureService.chain).
- [ ] Drawer: after provenance body, append a lazily-fetched timeline list — `t` (date), `kind` badge, `summary` — from `/api/chain?entity=<name>`; failures render `chain unavailable`.
- [ ] Node syntax check + run route/fixture tests; commit `feat(console): /api/chain + entity chain timeline in provenance drawer`.

### Task 4: Integration pass

- [ ] Full suite `.venv/Scripts/python.exe -m pytest tests/ -q` green.
- [ ] `CHANGELOG.md` wave-2 entry; `README.md` `memory_history` + `memory_lesson_search` blurbs.
- [ ] Commit `docs: wave-2 external findings (lesson re-verify, causal chain)`.
