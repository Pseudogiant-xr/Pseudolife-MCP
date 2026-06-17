# Pluggable LLM extraction + min-viable-model benchmark — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LLM-based memory extraction a first-class, batteries-included path (CPU-small / local-GPU / cloud, all behind the existing OpenAI-compatible interface), add an abstention signal to search, and build the benchmark ladder that finds the minimum viable extraction model.

**Architecture:** Reuse the existing `OpenAICompatExtractor` + dream consolidation (extraction stays off the store hot-path). Add an *opt-in* `pseudolife-extractor` llama.cpp sidecar (compose profile) so any GGUF is a drop-in endpoint. Add a tunable `low_confidence` abstain signal to `memory_search`. A dev-only `evals/` sweep runs the rung ladder and reports the minimum-viable verdict.

**Tech Stack:** Python 3.12, psycopg3 + pgvector, MCP (FastMCP), Docker Compose, llama.cpp-server (GGUF), sentence-transformers (naive-RAG baseline embedder).

**Spec:** `docs/specs/2026-06-18-pluggable-llm-extraction-design.md`

**Branch:** `feat/pluggable-llm-extraction` (already created; design docs committed).

**Test determinism:** prefix pytest with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` (documented gotcha). PG-dependent tests skip cleanly when no test Postgres is reachable.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `pseudolife_memory/memory/abstain.py` | Pure `low_confidence(scores, floor)` helper — no torch/PG, trivially unit-testable | Create |
| `pseudolife_memory/utils/config.py` | `MemoryConfig.search_confidence_floor` knob (0.0 = off) | Modify |
| `pseudolife_memory/service.py` | Wire `low_confidence` into the `search()` return | Modify (`545-552`) |
| `pseudolife_memory/mcp_server.py` | Document `low_confidence`; add `limit` to `memory_dream_run` | Modify |
| `tests/test_abstain.py` | Unit tests for the pure helper | Create |
| `tests/test_extraction_consolidation.py` | Integration: ingest → `dream_run` (stub extractor) → cortex superseded; abstention flag end-to-end | Create |
| `ops/Dockerfile.extractor` | llama.cpp-server image with a baked default GGUF | Create |
| `ops/docker-compose.yml` | Opt-in `pseudolife-extractor` service under a compose `profile` | Modify |
| `evals/ladder_sweep.py` | Rung sweep: configure extractor → ingest → consolidate → measure (reuses Tier B logic) | Create |
| `evals/README.md` | How to run the sweep; rung endpoints; operational caveats | Create |
| `README.md`, `CHANGELOG.md` | Document the sidecar + abstention | Modify |

---

## Task 1: Abstention `low_confidence` signal (pure helper + config + wiring)

**Files:**
- Create: `pseudolife_memory/memory/abstain.py`
- Create: `tests/test_abstain.py`
- Modify: `pseudolife_memory/utils/config.py` (MemoryConfig)
- Modify: `pseudolife_memory/service.py:545-552`
- Modify: `pseudolife_memory/mcp_server.py` (memory_search docstring return)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_abstain.py
from pseudolife_memory.memory.abstain import low_confidence

def test_empty_scores_is_low_confidence():
    assert low_confidence([], floor=0.0) is True          # nothing found -> abstain

def test_floor_off_only_empty_triggers():
    assert low_confidence([0.05, 0.01], floor=0.0) is False  # floor 0 = off

def test_top_below_floor_is_low_confidence():
    assert low_confidence([0.30, 0.10], floor=0.35) is True   # best hit too weak

def test_top_at_or_above_floor_is_confident():
    assert low_confidence([0.42, 0.10], floor=0.35) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_abstain.py -v`
Expected: FAIL — `ModuleNotFoundError: pseudolife_memory.memory.abstain`

- [ ] **Step 3: Write minimal implementation**

```python
# pseudolife_memory/memory/abstain.py
"""Abstention signal for retrieval — a pure, torch-free helper.

``low_confidence`` is True when the search has no confident answer, so the
agent can decline instead of fabricating from weak/distractor hits. ``floor``
is the tunable confidence threshold (0.0 = off; only an empty result abstains).
"""
from __future__ import annotations

from collections.abc import Sequence


def low_confidence(scores: Sequence[float], floor: float) -> bool:
    if not scores:
        return True
    if floor <= 0.0:
        return False
    return max(scores) < floor
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_abstain.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Add the config knob**

In `pseudolife_memory/utils/config.py`, `MemoryConfig` (near `neural_blend_weight`):

```python
    # Abstention: when the top search score is below this floor, memory_search
    # returns low_confidence=True so the agent declines instead of using weak
    # distractor hits. 0.0 = off (only an empty result is low-confidence).
    # Tuned on a dev split by the benchmark ladder; default off to preserve recall.
    search_confidence_floor: float = 0.0
```

And in `load_config`'s `MemoryConfig(...)` construction, add:
```python
            search_confidence_floor=mem_raw.get("search_confidence_floor", 0.0),
```

- [ ] **Step 6: Wire it into `service.search`**

In `pseudolife_memory/service.py`, replace the return at `545-552` with:

```python
            from pseudolife_memory.memory.abstain import low_confidence
            return {
                "query": query,
                "count": len(result.entries),
                "low_confidence": low_confidence(
                    list(result.scores),
                    self.config.memory.search_confidence_floor,
                ),
                "entries": [
                    _entry_to_dict(e, s)
                    for e, s in zip(result.entries, result.scores)
                ],
            }
```

Also add `"low_confidence": True` to the empty-query early return at line `527`:
```python
                return {"entries": [], "query": "", "count": 0, "low_confidence": True}
```

- [ ] **Step 7: Document on the tool + cortex guard**

In `pseudolife_memory/mcp_server.py`, `memory_search` docstring `Returns:` block, add a sentence: `low_confidence=True means no confident match — prefer to abstain ("I don't have that") rather than answer from the weak entries.`

**Correctness guard:** the `memory_search` tool surfaces a `cortex` block of canonical facts. A confident cortex hit must NOT be flagged low-confidence. First verify *where* `cortex` is merged (grep `"cortex"` in `mcp_server.py` / `service.py`). If it's merged at the tool layer, override there: `result["low_confidence"] = result["low_confidence"] and not result.get("cortex")`. If merged inside `service.search`, fold the cortex check into the `low_confidence(...)` call site there. Add a test for "weak entries + a cortex fact ⇒ low_confidence is False".

- [ ] **Step 8: Run the abstain tests + a quick import smoke**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_abstain.py -v`
Expected: PASS. Then `.venv/Scripts/python.exe -c "import pseudolife_memory.service"` → no error.

- [ ] **Step 9: Commit**

```bash
git add pseudolife_memory/memory/abstain.py tests/test_abstain.py pseudolife_memory/utils/config.py pseudolife_memory/service.py pseudolife_memory/mcp_server.py
git commit -m "feat(search): tunable low_confidence abstention signal"
```

---

## Task 2: `memory_dream_run(limit=)` for one-shot full consolidation

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (`memory_dream_run`, ~`560-572`)
- Create/extend: `tests/test_extraction_consolidation.py`

- [ ] **Step 1: Write the failing test** (monkeypatch the service so it's PG-free)

```python
# tests/test_extraction_consolidation.py
import pseudolife_memory.mcp_server as srv

def test_dream_run_passes_limit(monkeypatch):
    seen = {}
    def fake_dream_run(extractor, *, limit=None):
        seen["limit"] = limit
        return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                "contested": 0, "superseded": 0, "cursor": 0.0}
    monkeypatch.setattr(srv.service, "dream_run", fake_dream_run)
    srv.memory_dream_run(limit=500)
    assert seen["limit"] == 500
```

- [ ] **Step 2: Run test to verify it fails**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_extraction_consolidation.py::test_dream_run_passes_limit -v`
Expected: FAIL — `memory_dream_run() got an unexpected keyword argument 'limit'`

- [ ] **Step 3: Implement**

In `mcp_server.py`, change `memory_dream_run` to accept `limit`:
```python
def memory_dream_run(limit: int | None = None) -> dict[str, Any]:
    # ...docstring: add "limit: max memories to consolidate this call (default
    # config max_batch). Loop with a large limit to drain the full backlog."
    from pseudolife_memory.memory.dream import build_extractor
    return service.dream_run(build_extractor(service.config.memory.dream), limit=limit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_extraction_consolidation.py::test_dream_run_passes_limit -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_extraction_consolidation.py
git commit -m "feat(dream): memory_dream_run limit arg for one-shot full sweep"
```

---

## Task 3: Extraction-consolidation integration test (stub extractor)

Proves the consolidation path end-to-end without a live model: a stub extractor returns a corrected claim, and the cortex must supersede the stale value. Skips when no test PG.

**Files:**
- Extend: `tests/test_extraction_consolidation.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from tests.pg_fixtures import resolve_test_db_url   # existing helper

psycopg = pytest.importorskip("psycopg")

def _pg_ok(url):
    try:
        with psycopg.connect(url, connect_timeout=3): return True
    except Exception: return False

def test_consolidation_supersedes_via_stub_extractor(tmp_path):
    url = resolve_test_db_url()
    if not _pg_ok(url):
        pytest.skip("no test Postgres")
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.memory.dream import Claim

    svc = MemoryService(data_dir=str(tmp_path), database_url=url)
    svc.store("checkout-service default port note", source="t")
    svc.store("checkout-service default port changed", source="t")

    class StubExtractor:
        def extract(self, texts, vocab):
            return [Claim(entity="checkout-service", attribute="default port",
                          value="9090", confidence=0.8, origin="agent")]
    # consolidate everything
    while svc.dream_run(StubExtractor(), limit=100)["pulled"]:
        pass
    rec = svc.fact_get("checkout-service", "default port")
    assert rec["record"] and rec["record"]["value"] == "9090"
```

(Adjust `fact_get`/`store` call signatures to the actual `MemoryService` API if they differ — verify against `pseudolife_memory/service.py` before writing.)

- [ ] **Step 2: Run to verify it fails or skips**

Run: `HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_extraction_consolidation.py -v`
Expected: PASS if PG up (the path already exists), else SKIP. If it FAILS, that's a real consolidation bug — fix before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_extraction_consolidation.py
git commit -m "test(dream): consolidation supersedes via stub extractor"
```

---

## Task 4: Opt-in extractor sidecar (llama.cpp, compose profile)

**Files:**
- Create: `ops/Dockerfile.extractor`
- Modify: `ops/docker-compose.yml`

- [ ] **Step 1: Write the extractor Dockerfile**

```dockerfile
# ops/Dockerfile.extractor
# Optional CPU llama.cpp server exposing an OpenAI-compatible API for the
# dream extractor. Default GGUF baked in so the stack is batteries-included;
# override by mounting a different GGUF + setting MODEL for the benchmark.
FROM ghcr.io/ggml-org/llama.cpp:server
ARG MODEL_URL=https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf
ARG MODEL_PATH=/models/extractor.gguf
RUN mkdir -p /models && curl -fSL "$MODEL_URL" -o "$MODEL_PATH"
ENV LLAMA_ARG_MODEL=$MODEL_PATH
EXPOSE 8081
# llama.cpp server speaks OpenAI-compatible /v1/chat/completions; enable JSON.
CMD ["--host","0.0.0.0","--port","8081","--ctx-size","8192","--jinja"]
```

(Pin the exact image tag + verify the current llama.cpp server flags before building; the `--jinja` flag enables chat templating/JSON. Confirm the GGUF URL resolves.)

- [ ] **Step 2: Add the opt-in compose service**

In `ops/docker-compose.yml`, add under `services:` (note the `profiles:` — it stays OFF unless `--profile extractor`):

```yaml
  pseudolife-extractor:
    build:
      context: ..
      dockerfile: ops/Dockerfile.extractor
    image: pseudolife-extractor:gemma4-e4b
    container_name: pseudolife-extractor
    restart: unless-stopped
    profiles: ["extractor"]            # opt-in: docker compose --profile extractor up -d
    # internal-only; the daemon reaches it by service name, never published to host
    expose: ["8081"]
```

And document that to use it, the daemon gets:
```yaml
      PSEUDOLIFE_DREAM_BASE_URL: http://pseudolife-extractor:8081/v1
      PSEUDOLIFE_DREAM_MODEL: extractor
```
(Add these as commented lines in the `pseudolife-daemon` `environment:` block — uncomment to enable.)

- [ ] **Step 3: Validate compose**

Run: `docker compose -f ops/docker-compose.yml config --quiet && echo OK`
Expected: `OK` (no schema errors; the profiled service parses).

- [ ] **Step 4: Build + smoke the sidecar (manual/ops)**

Run: `docker compose -f ops/docker-compose.yml --profile extractor build pseudolife-extractor`
Then bring it up on the compose network and smoke a completion:
`docker compose -f ops/docker-compose.yml --profile extractor up -d pseudolife-extractor`
Smoke (from a throwaway container on the network): POST `/v1/chat/completions` with a 1-line JSON-extraction prompt; expect a parseable `choices[0].message.content`.
Expected: a JSON object back. If the build/download is heavy, note the image size.

- [ ] **Step 5: Commit**

```bash
git add ops/Dockerfile.extractor ops/docker-compose.yml
git commit -m "feat(ops): opt-in llama.cpp extractor sidecar (compose profile)"
```

---

## Task 5: Benchmark ladder sweep (`evals/`)

Promotes the throwaway Tier-B script into a reusable, dev-only sweep that runs each reachable rung and emits the per-rung table + minimum-viable verdict, plus the abstention sub-sweep.

**Files:**
- Create: `evals/ladder_sweep.py`
- Create: `evals/README.md`

- [ ] **Step 1: Port the Tier-B screen** into `evals/ladder_sweep.py` with a rung registry:

```python
RUNGS = {
  "floor":     {"base_url": None,                              "model": None},          # deterministic
  "gemma-e2b": {"base_url": "http://127.0.0.1:8081/v1",        "model": "extractor"},   # sidecar (swap GGUF)
  "gemma-e4b": {"base_url": "http://127.0.0.1:8081/v1",        "model": "extractor"},
  "qwen-a3b":  {"base_url": "http://<homelab-5800x3d>:<port>/v1","model": "qwen3.6-35b-a3b"},
  "qwen-27b":  {"base_url": "http://<4090-host>:<port>/v1",    "model": "qwen3.6-27b"},
  "cloud":     {"base_url": "<cloud>/v1",                       "model": "<haiku-class>"},
}
```

Per rung: spin an isolated bench DB + daemon (as in the 2026-06-17 screen), set `PSEUDOLIFE_DREAM_BASE_URL`/`_MODEL` for the rung, ingest the corpus, **loop `memory_dream_run(limit=100)` until `pulled==0`**, then run the Tier-B measures (stale-leak, gold-recoverable, tokens/query, latency) + extraction wall-time. Skip unreachable rungs (report N/A).

- [ ] **Step 2: Add the abstention sub-sweep**

With the best rung fixed, sweep `search_confidence_floor ∈ {0.0, 0.15, 0.25, 0.35}` (via config / per-call) on a dev split; emit abstention precision/recall vs gold-recoverable. Output the recommended default.

- [ ] **Step 3: Write `evals/README.md`**

Document: prerequisites (sidecar via `--profile extractor`; rung-3 needs the homelab serving Qwen 3.6 35B A3B on the LAN; rung-4 needs the 4090 llama.cpp up **+ user go-ahead**), the exact run command, and how to read the verdict.

- [ ] **Step 4: Dry-run the cheap rungs**

Run `floor` (always available) and, if the sidecar is up, `gemma-e4b`. Confirm the table prints and the verdict logic works. (Rungs 3/4/5 are run later, with the user, against live endpoints.)

- [ ] **Step 5: Commit**

```bash
git add evals/ladder_sweep.py evals/README.md
git commit -m "feat(evals): extractor ladder sweep + abstention sub-sweep"
```

---

## Task 6: Docs + full verification

**Files:**
- Modify: `README.md`, `CHANGELOG.md`

- [ ] **Step 1: README** — under deployment/config, document the opt-in extractor sidecar (`--profile extractor`, the two daemon env vars) and the `low_confidence`/`search_confidence_floor` abstention knob.

- [ ] **Step 2: CHANGELOG** — Unreleased: "Pluggable LLM extraction sidecar (opt-in), search abstention signal, extractor-ladder benchmark."

- [ ] **Step 3: Full test suite**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest -q`
Expected: all pass (new tests included; PG/daemon tests skip cleanly if no test PG). Record the count.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: extractor sidecar + abstention; changelog"
```

---

## Done criteria

- `low_confidence` returns from `memory_search`, tunable via `search_confidence_floor`, unit-tested.
- `memory_dream_run(limit=)` drains the backlog; consolidation supersedes via a stub extractor (integration test).
- Opt-in `pseudolife-extractor` sidecar builds + smokes; daemon can point at it with two env vars; default stack unaffected.
- `evals/ladder_sweep.py` runs the cheap rungs and prints the per-rung table + verdict; ready to run rungs 3/4/5 against live endpoints with the user.
- Full suite green; README/CHANGELOG updated.

## Out of scope (deferred, per spec)

Deterministic-floor hardening; graph-from-free-text; making the sidecar default-on (post-benchmark decision); running the paid/cloud + GPU rungs (execution-time, needs user go-ahead).
