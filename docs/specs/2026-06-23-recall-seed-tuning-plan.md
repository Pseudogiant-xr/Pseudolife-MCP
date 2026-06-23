# Recall Seed-Tuning (Query-First) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `memory_recall`'s mechanical seeder query-first so it seeds only the question's subject(s), eliminating hit-cross-talk noise (bench: seed precision 1.0 vs 0.262, zero answer-recall loss, ~4× fewer graph calls).

**Architecture:** A one-function change to `MechanicalController.seed_entities` in `recall.py`: match the query first; fall back to hit-derived matches only when the query names no known entity. No contract change, no degree threading (the degree-filter variant tied query-first in the bench → unnecessary). `LLMController` and `memory_search` are untouched.

**Tech Stack:** Python 3.11, stdlib; pytest.

## Global Constraints

- Bench-decided winner = **query-first** (variant A). Do NOT add a degree filter or change the `seed_entities` signature (YAGNI — A+B tied A).
- `memory_search` and every existing retrieval path UNCHANGED.
- `recall` stays READ-ONLY.
- `LLMController.seed_entities` is UNCHANGED (the LLM still resolves from query+hits via the model).
- Tests run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`.
- Task 2 (deploy) is GATED — do not run without explicit user approval.

---

### Task 1: Query-first mechanical seeder

**Files:**
- Modify: `pseudolife_memory/memory/recall.py` (`MechanicalController.seed_entities`)
- Modify: `tests/test_recall.py` (update the seed unit test; add a fallback test)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `_mentions(text, name)` (already in `recall.py`).
- Produces: unchanged signature `MechanicalController.seed_entities(query, hits, vocab) -> list[str]`; new behavior = query-first.

- [ ] **Step 1: Update the test to the query-first contract**

In `tests/test_recall.py`, find the existing test `test_mechanical_seeds_from_query_and_hits` (it currently asserts the *liberal* behavior `== ["alpha", "beta"]`) and REPLACE that whole test function with these two tests:

```python
def test_mechanical_seeds_query_first_subject_only():
    # query names only "alpha"; the hit also mentions "beta", but query-first
    # must seed ONLY the query subject (beta is reached later via the graph).
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does alpha run on",
                            ["alpha depends-on beta"], ["alpha", "beta", "gamma"])
    assert seeds == ["alpha"]


def test_mechanical_seeds_fall_back_to_hits_when_query_bare():
    # query names no known entity -> fall back to hit-derived matches.
    c = rc.MechanicalController()
    seeds = c.seed_entities("what does it run on?",
                            ["alpha depends-on beta"], ["alpha", "beta", "gamma"])
    assert seeds == ["alpha", "beta"]
```

- [ ] **Step 2: Run the tests to verify the new behavior fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -k "mechanical_seeds" -v`
Expected: `test_mechanical_seeds_query_first_subject_only` FAILS — the current liberal seeder returns `["alpha", "beta"]`, not `["alpha"]`. (`fall_back` may already pass.)

- [ ] **Step 3: Implement query-first**

In `pseudolife_memory/memory/recall.py`, replace `MechanicalController.seed_entities` with:

```python
    def seed_entities(self, query: str, hits: list[str],
                      vocab: list[str]) -> list[str]:
        # Query-first: the question names its subject(s); seed those only.
        # On a populous bank, co-mentioning search hits drag in unrelated
        # entities, so hit-derived matches are used ONLY as a fallback when the
        # query names no known entity. (Bench: precision 1.0 vs 0.262, zero
        # recall loss — intermediates are reached via the graph, not seeded.)
        q = [name for name in vocab if _mentions(query, name)]
        if q:
            return q
        return [name for name in vocab if _mentions(" ".join(hits), name)]
```

Also update the class docstring line for `MechanicalController` from the old "seeds = vocab entities word-present in query+hits" to: `"""Deterministic: seeds = vocab entities word-present in the QUERY (hits only as fallback); re-query each newly discovered entity by name."""`

- [ ] **Step 4: Run the full recall suite**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_recall.py -v`
Expected: all PASS (the two new seed tests, plus the existing `run_recall`/LLM/PG tests — `test_run_recall_reaches_two_hop_terminal` still reaches `gamma` via the graph from the single `alpha` seed; `low_confidence`/`hops`/`max_entities` tests unaffected). If PG is down, the PG tests SKIP — that's fine.

- [ ] **Step 5: CHANGELOG note**

Add to `CHANGELOG.md` under `## [Unreleased]` → `### Changed` (create the `### Changed` subsection if absent, after `### Added`; read the file first to match style):

```markdown
- `memory_recall` mechanical seeder is now query-first — seeds the question's subject(s) and uses search-hit matches only as a fallback, eliminating cross-talk noise on populous banks (bench: seed precision 1.0 vs 0.262, zero answer-recall loss, ~4× fewer graph calls). `recall.driver=llm` unchanged.
```

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/recall.py tests/test_recall.py CHANGELOG.md
git commit -m "feat(recall): query-first mechanical seed selection (precision 1.0)"
```

---

### Task 2: GATED live re-deploy + before/after

> **GATE:** Do NOT run without explicit user approval at execution time. Rebuilds/restarts the live daemon. Read-only tool change; backup first; never `down -v`.

**Files:** none (operational).

- [ ] **Step 1: Capture the live BEFORE seed count**

Via the MCP HTTP client against the running (pre-deploy) daemon (a 2nd `MemoryService` deadlocks on `ensure_schema` — use the http client). Minimal client (host venv has `mcp`):

```python
import anyio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession
async def main():
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("memory_recall", {"query": "what does pseudolife-mcp run on"})
            d = json.loads(res.content[0].text)
            print("BEFORE seeds:", d["seeds"]); print("BEFORE entities:", [e["entity"] for e in d["entities"]])
anyio.run(main)
```
Record the seed count (expected ~7).

- [ ] **Step 2: Merge to master + verify additive**

```bash
git checkout master && git merge --ff-only feat/recall-seed-tuning
git diff --name-only HEAD~1..HEAD | grep -E '^pseudolife_memory/' || echo "(only recall.py)"
```
Expected: the only runtime file changed is `pseudolife_memory/memory/recall.py`.

- [ ] **Step 3: Back up the live bank FIRST**

Run: `pwsh -File ops/backup.ps1`
Confirm a fresh `data/backups/pseudolife_memory-<ts>.sql.gz` exists before proceeding.

- [ ] **Step 4: Rebuild + restart the daemon (preserve volumes)**

```bash
docker compose -f ops/docker-compose.yml build pseudolife-daemon
docker compose -f ops/docker-compose.yml up -d --no-deps pseudolife-daemon
```
Never `down -v`. Then: `curl -s http://127.0.0.1:8765/health` → expect schema 11, ok; `docker ps` → daemon healthy, postgres + extractor untouched.

- [ ] **Step 5: Live AFTER + smoke**

Re-run the Step-1 client snippet. Expected: **seed count drops to ~1** (`["pseudolife-mcp"]`), `entities` still include `docker-desktop`, `low_confidence: false` (bridge preserved). Then confirm `memory_search` unchanged via the live MCP tool.

- [ ] **Step 6: Record + push**

Push master if the user asks (`git push origin master`); delete the merged branch. Update memory (`memory_store`) with the before/after seed counts and the deploy result.

---

## Self-Review

**1. Spec coverage:**
- Query-first seeder (the bench winner) → Task 1. ✓
- No degree threading / no contract change (A+B tied) → Global Constraints + Task 1 (signature unchanged). ✓
- Unit test query-first + fallback → Task 1 Steps 1–4. ✓
- `LLMController`/`memory_search` untouched → Global Constraints; Task 1 touches only `MechanicalController.seed_entities`. ✓
- CHANGELOG note → Task 1 Step 5. ✓
- Gated re-deploy + live before/after → Task 2. ✓
- recall read-only → no writes added. ✓

**2. Placeholder scan:** No TBD/TODO; the seeder code, both tests, and the CHANGELOG line are complete; deploy commands are exact.

**3. Type consistency:** `seed_entities(query, hits, vocab) -> list[str]` signature is unchanged; `_mentions(text, name)` used as defined; the new tests call the same signature. No cross-task type drift (Task 2 is operational).
