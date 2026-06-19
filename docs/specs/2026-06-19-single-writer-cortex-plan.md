# Single-writer Cortex — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LLM dream pass the sole *automatic* writer of canonical cortex facts — kill the deterministic regex auto-promote (and the `dream_run` regex fallback) that fragments slots — and add a reviewed one-time cleanup for existing sibling slots.

**Architecture:** Flip `cortex.auto_promote` to default-off; replace the unconfigured-extractor default with a `NoOpExtractor` and delete the hardcoded `dream_run` regex fallback so an empty extraction writes nothing; ship the Gemma extractor sidecar default-on in compose; add `CortexStore.dedup_siblings` + `MemoryService.cortex_dedup` + an ops CLI to collapse legacy fragments (dry-run-first, reversible). `extract_slots` stays for the recall-time slot-view only. Feature A (dream slot resolver, schema v8 `slot_embedding`) stays in place, shipped off.

**Tech Stack:** Python 3.11, torch (CPU), PostgreSQL (psycopg), pytest, Docker Compose, llama.cpp sidecar (Gemma 4 E2B).

**Spec:** `docs/specs/2026-06-19-single-writer-cortex-design.md`

**Test invocation (this repo):** `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest <args>`
PG-backed tests use the `pg_conn`/`pg_url` fixtures (`tests/pg_fixtures.py`) and skip cleanly without a test server.

---

## File Structure

- `pseudolife_memory/memory/dream.py` — add `NoOpExtractor`; `build_extractor` returns it when unconfigured.
- `pseudolife_memory/service.py` — remove `dream_run` regex fallback (import + block + docstring); add `cortex_dedup`.
- `pseudolife_memory/memory/cortex.py` — add `dedup_siblings`.
- `pseudolife_memory/utils/config.py` — `CortexConfig.auto_promote` default `True → False`; refresh docstring.
- `pseudolife_memory/mcp_server.py` — startup warning in `start_dream_sweep` when no extractor resolves.
- `ops/docker-compose.yml` — extractor default-on; daemon `PSEUDOLIFE_DREAM_*` + `depends_on`.
- `ops/dedup_cortex.py` — new dry-run/apply CLI.
- `evals/ladder_sweep.py` — make `auto_promote=False` explicit in `build_service`.
- `README.md`, `CHANGELOG.md`, `evals/README.md` — single-writer docs; refresh stale docstrings.
- Tests: `tests/test_dream.py`, `tests/test_cortex_promotion.py`, `tests/test_mcp_server.py`, `tests/test_write_through.py`, `tests/test_cortex_dedup.py` (new).

---

## Task 1: `NoOpExtractor` + `build_extractor` default

**Files:**
- Modify: `pseudolife_memory/memory/dream.py:142-157` (`build_extractor`), add `NoOpExtractor` class near `RegexExtractor` (`dream.py:33`)
- Test: `tests/test_dream.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_dream.py` (near `test_build_extractor_selects_by_config`):

```python
def test_noop_extractor_returns_empty():
    from pseudolife_memory.memory.dream import NoOpExtractor
    assert NoOpExtractor().extract(["the build timeout is 4500 seconds"], vocab=[]) == []
```

And change the unconfigured branch of `test_build_extractor_selects_by_config` to expect the no-op (it currently expects `RegexExtractor`):

```python
    monkeypatch.delenv("PSEUDOLIFE_DREAM_BASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_DREAM_MODEL", raising=False)
    # Unconfigured => no-op (the regex floor is no longer an automatic cortex writer).
    from pseudolife_memory.memory.dream import NoOpExtractor
    assert isinstance(build_extractor(DreamConfig()), NoOpExtractor)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_dream.py::test_noop_extractor_returns_empty tests/test_dream.py::test_build_extractor_selects_by_config -v`
Expected: FAIL (`NoOpExtractor` undefined / build_extractor returns `RegexExtractor`).

- [ ] **Step 3: Implement**

Add to `dream.py` (after `RegexExtractor`):

```python
class NoOpExtractor:
    """No-LLM, no-write floor. Returns no claims, so a dream with no configured
    extractor writes nothing to the cortex (single-writer: the LLM dream is the
    sole automatic writer; the regex is for the recall-time slot-view only)."""

    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        return []
```

Change `build_extractor`'s final line (`dream.py:157`):

```python
    return NoOpExtractor()
```

Update the `build_extractor` docstring's "else the regex floor" → "else a no-op (no automatic regex writes; see single-writer-cortex design)".

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_dream.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/dream.py tests/test_dream.py
git commit -m "feat(dream): NoOpExtractor; build_extractor no longer defaults to regex floor"
```

---

## Task 2: Remove the `dream_run` regex fallback (load-bearing)

This is the change that actually makes the LLM the sole automatic writer — a `NoOpExtractor` returning `[]` would otherwise trip the fallback at `service.py:1311-1312` and write regex facts anyway.

**Files:**
- Modify: `pseudolife_memory/service.py:1292-1325` (`dream_run`: remove import `:1297`, remove `if not claims:` block `:1311-1312`, fix docstring `:1293-1296`)
- Test: `tests/test_dream.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_dream.py` (these use the existing `svc` PG fixture and `_StubExtractor`):

```python
def test_dream_with_noop_extractor_writes_nothing(svc):
    from pseudolife_memory.memory.dream import NoOpExtractor
    svc.config.memory.cortex.auto_promote = False   # no store-path promotion either
    svc.store("the build timeout is 4500 seconds", source="notes")
    out = svc.dream_run(NoOpExtractor())
    assert out["pulled"] >= 1
    assert out["inserted"] == 0 and out["confirmed"] == 0
    assert out["cursor"] > 0                          # cursor still advances
    assert svc.cortex_lookup("build", "timeout") is None


def test_dream_empty_llm_claims_write_nothing(svc):
    # An LLM that emitted no parseable claims must NOT fall back to the regex floor.
    svc.config.memory.cortex.auto_promote = False
    svc.store("the relay port is 4001", source="notes")
    out = svc.dream_run(_StubExtractor([]))
    assert out["inserted"] == 0 and out["confirmed"] == 0
    assert svc.cortex_lookup("relay", "port") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_dream.py::test_dream_with_noop_extractor_writes_nothing tests/test_dream.py::test_dream_empty_llm_claims_write_nothing -v`
Expected: FAIL — today `dream_run` falls back to `RegexExtractor`, which extracts `build/timeout` and `relay/port`, so the lookups are non-`None` and `inserted >= 1`. (If the PG fixture is unavailable the tests SKIP — re-run where the test server is reachable; do not mark complete on a skip.)

- [ ] **Step 3: Implement**

In `service.py` `dream_run`:
- Delete the import line `from pseudolife_memory.memory.dream import RegexExtractor` (`:1297`).
- Delete the fallback:
  ```python
  if not claims:
      claims = RegexExtractor().extract(texts, vocab)
  ```
- Update the docstring (`:1293-1296`): replace "extract claims via ``extractor`` (regex floor fallback if it yields nothing)" with "extract claims via ``extractor`` (an extractor that yields nothing writes nothing — single-writer)".

The `try/except` around `extractor.extract` stays (an extractor must never break a dream); on failure `claims = []` and the loop simply writes nothing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_dream.py -v`
Expected: PASS (existing `test_dream_run_promotes_and_advances_cursor` still passes — it passes an explicit `RegexExtractor()`, which is unaffected).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): remove dream_run regex fallback (LLM is sole automatic cortex writer)"
```

---

## Task 3: Flip `auto_promote` default to off

**Files:**
- Modify: `pseudolife_memory/utils/config.py:353` (`auto_promote: bool = True → False`) + docstring `:340-351`
- Modify (tests that assumed the default): `tests/test_cortex_promotion.py`, `tests/test_mcp_server.py`, `tests/test_write_through.py`
- Test: `tests/test_cortex_promotion.py`

- [ ] **Step 1: Write the failing test** (new default behaviour)

In `tests/test_cortex_promotion.py` — use the file's inline construction idiom
(it has **no** `_svc()` factory; every test does
`with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d: svc = MemoryService(data_dir=d)`):

```python
def test_store_does_not_auto_promote_by_default():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        out = svc.store("the gateway port is 8080", source="notes")
        assert out["cortex_promoted"] == 0          # off by default now
        assert svc.cortex_lookup("gateway", "port") is None
```

(Mirror `test_store_auto_promotes_slot_to_cortex` for imports/construction. This
test needs no knob change — it asserts the new default of non-promotion.)

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_cortex_promotion.py::test_store_does_not_auto_promote_by_default -v`
Expected: FAIL — default is still `True`, so it promotes (`cortex_promoted == 1`).

- [ ] **Step 3: Implement + fix dependent tests**

- `config.py:353`: `auto_promote: bool = False`.
- `config.py:340-351` docstring: the cortex is no longer "populated deterministically from slot-shaped facts on every `store` (`auto_promote`, the no-LLM floor)". Reword to: populated by the LLM dream pass and by explicit `memory_fact_set`; `auto_promote` is an opt-in (default off) regex floor that fragments compound slots — see the single-writer-cortex design.
- Tests that exercise the promotion path must now set the knob explicitly. In each, add `svc.config.memory.cortex.auto_promote = True` before the store:
  - `tests/test_cortex_promotion.py::test_store_auto_promotes_slot_to_cortex`
  - `tests/test_mcp_server.py:164 test_store_auto_promotes_and_search_surfaces_cortex` (asserts `cortex_promoted >= 1`). Note: `:141` (`test_memory_store_via_mcp_dispatch`) only asserts `"cortex_promoted" in out` — membership, not value — so it does **not** break and needs no knob.
  - `tests/test_write_through.py:122` (the `cortex_promoted == 1` assertion)
  - For `test_mcp_server.py`, the knob lives on the module-level `service` — set `service.config.memory.cortex.auto_promote = True` at the start of the relevant test and restore to `False` in a `finally` (or use the existing per-test setup pattern in that file).

- [ ] **Step 4: Run the affected suites to verify they pass**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_cortex_promotion.py tests/test_mcp_server.py tests/test_write_through.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py tests/test_cortex_promotion.py tests/test_mcp_server.py tests/test_write_through.py
git commit -m "feat(cortex): auto_promote default off (regex floor no longer writes cortex)"
```

---

## Task 4: Startup warning when no extractor configured

**Files:**
- Modify: `pseudolife_memory/mcp_server.py:1181-1194` (`start_dream_sweep`)
- Test: `tests/test_mcp_server.py` (or `tests/test_dream.py` — wherever daemon-startup helpers live; if `start_dream_sweep` is awkward to unit-test, a focused log-capture test is acceptable)

- [ ] **Step 1: Write the failing test**

Prefer testing the *condition* without the thread. Add to `tests/test_dream.py`:

```python
def test_no_extractor_resolves_to_noop(monkeypatch):
    from pseudolife_memory.memory.dream import build_extractor, NoOpExtractor
    from pseudolife_memory.utils.config import DreamConfig
    monkeypatch.delenv("PSEUDOLIFE_DREAM_BASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_DREAM_MODEL", raising=False)
    assert isinstance(build_extractor(DreamConfig()), NoOpExtractor)
```

(This guards the predicate the warning keys on. The log line itself is verified by inspection / a `caplog` assertion if the daemon-start path is easily importable.)

- [ ] **Step 2: Run it**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_dream.py::test_no_extractor_resolves_to_noop -v`
Expected: PASS after Task 1 (this codifies the predicate; the implementation step adds the warning).

- [ ] **Step 3: Implement**

In `start_dream_sweep` (`mcp_server.py`), after the `if not service.config.memory.dream.enabled: return` guard and before starting the thread:

```python
    from pseudolife_memory.memory.dream import build_extractor, NoOpExtractor
    if isinstance(build_extractor(service.config.memory.dream), NoOpExtractor):
        logger.warning(
            "dream enabled but no extractor LLM configured "
            "(PSEUDOLIFE_DREAM_BASE_URL/_MODEL unset): cortex auto-population is "
            "disabled; only memory_fact_set writes canonical facts. "
            "Set the extractor sidecar to populate the cortex."
        )
```

- [ ] **Step 4: Run the server suite**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_mcp_server.py -v`
Expected: PASS (no behavioural change beyond a log line).

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_dream.py
git commit -m "feat(daemon): warn at startup when dream enabled but no extractor configured"
```

---

## Task 5: `dedup_siblings` (cortex) + `cortex_dedup` (service)

**Files:**
- Modify: `pseudolife_memory/memory/cortex.py` (add `dedup_siblings` near `forget`/`resolve_slot`)
- Modify: `pseudolife_memory/service.py` (add `cortex_dedup` near `_resolve_dream_slot`)
- Test: `tests/test_cortex_dedup.py` (new)

Reuse: `current_records()` (`cortex.py:386`), `_rank`/`record.origin` for tier precedence (`cortex.py:80,117`), the `_current` rebuild idiom from `forget` (`cortex.py:416-419`), `self._embedder.encode_single`, `self._lock`, `self._save_cortex()`, `self._storage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cortex_dedup.py` (PG-backed via the shared `svc`-style fixture; copy the fixture pattern from `tests/test_dream.py:190-196`):

```python
def test_cortex_dedup_merges_siblings(svc):
    # Two paraphrased slots for one fact (as past auto-promote would have forked).
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.cortex_write("payments database", "host", "db-prod-2", support="agent")
    rep = svc.cortex_dedup(threshold=0.85, dry_run=True)
    assert rep["merged"] >= 1                              # dry-run REPORTS a merge
    # ...but mutates nothing:
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    assert svc.cortex_lookup("payments database", "host")["value"] == "db-prod-2"

    rep2 = svc.cortex_dedup(threshold=0.85, dry_run=False)  # apply
    assert rep2["merged"] >= 1
    survivors = [(r.entity, r.attribute) for r in svc._cortex.current_records()]
    assert len(survivors) == 1                              # one canonical remains


def test_cortex_dedup_leaves_distinct_slots(svc):
    svc.cortex_write("invoice-service", "port", "7000", support="agent")
    svc.cortex_write("invoice-service", "region", "us-west-2", support="agent")
    rep = svc.cortex_dedup(threshold=0.90, dry_run=False)
    assert rep["merged"] == 0
    assert svc.cortex_lookup("invoice-service", "port")["value"] == "7000"
    assert svc.cortex_lookup("invoice-service", "region")["value"] == "us-west-2"


def test_cortex_dedup_canonical_prefers_user_tier(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.cortex_write("payments database", "host", "db-user", support="user")
    svc.cortex_dedup(threshold=0.85, dry_run=False)
    cur = svc._cortex.current_records()
    assert len(cur) == 1 and cur[0].value == "db-user"     # user tier wins
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_cortex_dedup.py -v`
Expected: FAIL — `cortex_dedup` undefined.

- [ ] **Step 3: Implement `dedup_siblings` (cortex.py)**

```python
def dedup_siblings(self, threshold: float, *, apply: bool) -> list[dict]:
    """Collapse current slots whose value-free slot embeddings match at cosine
    >= ``threshold`` (paraphrase fragments of one fact). Per cluster, keep the
    canonical (strongest provenance tier, then most-recent) and retire the rest
    (status -> superseded, audit trail kept). Returns a report; only mutates when
    ``apply`` is True. Records without a slot_embedding are skipped — backfill
    first (the service does)."""
    import time as _t
    cands = [r for r in self.current_records() if r.slot_embedding is not None]
    if len(cands) < 2:
        return []
    mat = torch.stack([r.slot_embedding.reshape(-1) for r in cands])
    mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
    sims = mat @ mat.t()                       # NxN cosine
    parent = list(range(len(cands)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if float(sims[i][j]) >= threshold:
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in range(len(cands)):
        clusters.setdefault(find(i), []).append(i)
    report: list[dict] = []
    now = _t.time()
    for members in clusters.values():
        if len(members) < 2:
            continue
        recs = [cands[m] for m in members]
        canonical = max(recs, key=lambda r: (_rank(r.origin),
                                              r.last_confirmed or r.asserted_at))
        losers = [r for r in recs if r is not canonical]
        report.append({
            "canonical": (canonical.entity, canonical.attribute, canonical.value),
            "retired": [(r.entity, r.attribute, r.value) for r in losers],
        })
        if apply:
            for r in losers:
                r.status = "superseded"
                r.superseded_by_value = canonical.value
                r.superseded_at = now
    if apply and report:
        self._current = {}
        for i, r in enumerate(self.records):
            if r.status == "current":
                self._current[r.key] = i
    return report
```

- [ ] **Step 4: Implement `cortex_dedup` (service.py)**

```python
def cortex_dedup(self, threshold: float = 0.90, dry_run: bool = True) -> dict:
    """One-time, reviewed cleanup of paraphrase sibling slots left by past
    regex auto-promotes. Dry-run by default (reports, writes nothing). Reuses the
    value-free slot embedding; backfills any current record missing one. Ops-only
    — back up the bank before --apply."""
    assert self._cortex is not None and self._embedder is not None
    with self._lock:
        for r in self._cortex.current_records():
            if r.slot_embedding is None:
                r.slot_embedding = self._embedder.encode_single(
                    f"{r.entity} {r.attribute}".strip())
        report = self._cortex.dedup_siblings(threshold, apply=not dry_run)
        if not dry_run and report and self._storage is not None:
            self._save_cortex()
    return {
        "dry_run": dry_run,
        "threshold": float(threshold),
        "clusters": report,
        "merged": sum(len(c["retired"]) for c in report),
    }
```

> Note: dry-run backfills slot embeddings in memory (an idempotent lazy heal, same as `_resolve_dream_slot`) but never persists — `_save_cortex` is only called on apply. The dry-run tests assert status/lookups are unchanged, which they are.

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest tests/test_cortex_dedup.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/cortex.py pseudolife_memory/service.py tests/test_cortex_dedup.py
git commit -m "feat(cortex): dedup_siblings + cortex_dedup (reviewed one-time sibling cleanup)"
```

---

## Task 6: `ops/dedup_cortex.py` CLI

**Files:**
- Create: `ops/dedup_cortex.py`
- Test: manual smoke (CLI wiring); the logic is covered by Task 5.

- [ ] **Step 1: Implement the CLI**

Mirror how other ops/daemon entrypoints construct the service (env `PSEUDOLIFE_MCP_DATABASE_URL` / `PSEUDOLIFE_MCP_DATA_DIR`, falling back to config). Keep it small:

```python
"""One-time cortex sibling-slot cleanup (dry-run by default).

Collapses paraphrase fragments past regex auto-promotes left behind. Reversible
(retires, never deletes). BACK UP FIRST (ops/backup.ps1) and prefer running while
the daemon is quiescent/stopped to avoid a concurrent cortex snapshot write.

    python ops/dedup_cortex.py                 # dry-run report
    python ops/dedup_cortex.py --threshold 0.92
    python ops/dedup_cortex.py --apply         # commit (after a backup)
"""
import argparse, json, os
from pseudolife_memory.service import MemoryService


def main() -> None:
    ap = argparse.ArgumentParser(description="Cortex sibling-slot dedup (dry-run by default).")
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument("--apply", action="store_true", help="commit the merges (back up first)")
    args = ap.parse_args()

    svc = MemoryService(
        data_dir=os.environ.get("PSEUDOLIFE_MCP_DATA_DIR"),
        database_url=os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL"),
    )
    if args.apply:
        print("APPLY mode — ensure you ran ops/backup.ps1 first.")
    rep = svc.cortex_dedup(threshold=args.threshold, dry_run=not args.apply)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    print(f"\n{rep['merged']} slot(s) "
          f"{'retired' if args.apply else 'would be retired'} "
          f"across {len(rep['clusters'])} cluster(s) at threshold {rep['threshold']}.")
    svc.flush()


if __name__ == "__main__":
    main()
```

(Confirm the `MemoryService(...)` kwargs against `ops/Dockerfile.daemon`'s entry / `mcp_server.py` service construction; match how `data_dir`/`database_url` defaults resolve there.)

- [ ] **Step 2: Smoke-test the CLI**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python ops/dedup_cortex.py --help`
Expected: usage prints, exit 0.

(A live dry-run against the real bank is a manual verification step in Task 10, gated on the daemon's Postgres being reachable.)

- [ ] **Step 3: Commit**

```bash
git add ops/dedup_cortex.py
git commit -m "feat(ops): dedup_cortex CLI (dry-run-first sibling-slot cleanup)"
```

---

## Task 7: Extractor sidecar default-on (compose)

**Files:**
- Modify: `ops/docker-compose.yml:45-100`

- [ ] **Step 1: Edit compose**

- In `pseudolife-daemon.depends_on`, add (alongside `pseudolife-pg`):
  ```yaml
      pseudolife-extractor:
        condition: service_started
  ```
- In `pseudolife-daemon.environment`, uncomment / set:
  ```yaml
      PSEUDOLIFE_DREAM_BASE_URL: http://pseudolife-extractor:8081/v1
      PSEUDOLIFE_DREAM_MODEL: extractor
  ```
- On `pseudolife-extractor`, delete the line `profiles: ["extractor"]` so it starts with the stack. Update the stale comments above it ("Opt-in ... OFF by default") to "Default-on extractor sidecar (single-writer cortex)".

- [ ] **Step 2: Validate compose syntax**

Run: `docker compose -f ops/docker-compose.yml config >/dev/null && echo OK`
Expected: `OK` (no schema errors). If `docker` is unavailable in this environment, validate by inspection and note it for manual verification.

- [ ] **Step 3: Commit**

```bash
git add ops/docker-compose.yml
git commit -m "ops: extractor sidecar default-on; wire daemon dream extractor"
```

---

## Task 8: Eval harness — make `auto_promote=False` explicit

`ingest()` populates cortex via `consolidate()` (the dream), not auto-promote, so the new default already gives a cleaner "dream-only" measurement. Make the intent explicit and robust to the default.

**Files:**
- Modify: `evals/ladder_sweep.py:263-275` (`build_service`)

- [ ] **Step 1: Edit `build_service`**

Add next to the existing `protect_provenance = False` line:

```python
    # Single-writer: measure the DREAM extractor alone, not the regex auto-promote
    # floor (which fragments compound slots). Explicit so the bench is independent
    # of the shipped default.
    svc.config.memory.cortex.auto_promote = False
```

- [ ] **Step 2: Smoke-run the floor rung** (no LLM needed)

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python evals/ladder_sweep.py --report` (floor rung only, or the smallest configured) and confirm it runs without error and the cortex is populated by the dream (gold/stale numbers print). LLM rungs are sidecar-gated (Task 10).

- [ ] **Step 3: Commit**

```bash
git add evals/ladder_sweep.py
git commit -m "test(evals): pin auto_promote=False in build_service (measure dream alone)"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `evals/README.md`
- Modify (stale docstrings): `pseudolife_memory/utils/config.py:340-351` (done in Task 3), `pseudolife_memory/memory/dream.py:5-7` and `:69-70`, `pseudolife_memory/service.py:1293-1296` (done in Task 2)

- [ ] **Step 1: README** — add a "single-writer cortex" note where cortex/auto-promote is described: the LLM dream pass is the sole automatic cortex writer; `auto_promote` is opt-in (default off) and reintroduces regex fragmentation; the extractor sidecar is now a default stack component; no-LLM deployments populate cortex only via `memory_fact_set`. Document `ops/dedup_cortex.py` (dry-run-first, back up before `--apply`).

- [ ] **Step 2: CHANGELOG** — under `[Unreleased]`: `auto_promote` now defaults off (single-writer cortex); `dream_run` regex fallback removed; `NoOpExtractor`; extractor sidecar default-on; `ops/dedup_cortex.py` cleanup tool. Reference the design doc.

- [ ] **Step 3: evals/README** — note `build_service` now pins `auto_promote=False` so the sweep measures the dream extractor alone (cleaner than the prior auto-promote-contaminated runs).

- [ ] **Step 4: Stale docstrings** — `dream.py` module docstring (`:5-7`) and `OpenAICompatExtractor` (`:69-70`) still assert a "regex floor" fallback; reword to the single-writer reality (no automatic regex floor; `RegexExtractor` is explicit opt-in only). While editing `CortexConfig` (Task 3), also fix its docstring header "schema v7" (`config.py:340`) — the live schema is v8.

- [ ] **Step 5: Flag (do not auto-edit) the user's global file** — print a reminder in the final report that `~/.claude/CLAUDE.md`'s line "memory_store() automatically promotes slot-shaped facts to cortex" is now stale and the user may want to update it.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md evals/README.md pseudolife_memory/memory/dream.py
git commit -m "docs: single-writer cortex (auto_promote off, sole LLM writer, dedup tool)"
```

---

## Task 10: Full verification + finish branch

- [ ] **Step 1: Run the whole suite**

Run: `PYTHONPATH=. TORCHDYNAMO_DISABLE=1 .venv/Scripts/python -m pytest -q`
Expected: all pass (PG-backed tests require the test server; do not accept silent skips on the new dream/dedup tests — run where Postgres is reachable).

- [ ] **Step 2: Behaviour-preservation sanity** — confirm a default-config `store()` no longer promotes (`cortex_promoted == 0`) and that explicit `memory_fact_set` still writes immediately.

- [ ] **Step 3: Live cleanup dry-run (manual, gated on the daemon bank)** — with the daemon's Postgres reachable, run `python ops/dedup_cortex.py` (dry-run) and eyeball the proposed clusters against the live bank before any `--apply`. Back up (`ops/backup.ps1`) before `--apply`.

- [ ] **Step 4: Optional sidecar re-calibration** — if the extractor sidecar is up, re-run `--supersede` / `--abstain` under the new default and refresh `evals/README.md` findings (the single-writer numbers should be cleaner). Gated on sidecar availability; not a blocker.

- [ ] **Step 5: Finish the branch** — use superpowers:finishing-a-development-branch (tests verified → push, per the project's no-PR preference for solo work, unless told otherwise).

---

## Notes for the implementer

- **Branch base:** this work depends on the schema-v8 `slot_embedding` + backfill added on `feat/supersession-abstention-tuning` (the cleanup reuses it), so branch off that branch's tip (`feat/single-writer-cortex`), not bare `master`.
- **YAGNI:** do NOT add an eager-dream freshness trigger (Approach B) — tabled in the spec. Do NOT touch the Feature A resolver (stays off). Do NOT remove `extract_slots` or `RegexExtractor` (slot-view + explicit opt-in respectively).
- **TDD discipline:** every code task starts with a failing test; run it red before implementing.
- **Reversibility:** `dedup_siblings` retires (never deletes); `forget` is the only hard-delete and is out of scope here.
