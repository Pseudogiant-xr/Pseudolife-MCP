# Pluggable Dream Extractor — Implementation Plan (Phases 1–2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dream pass (MIRAS → cortex consolidation) runnable headless with zero config (Tier 0 regex floor) and high-quality via the agent itself (Tier 1 `/dream`), without assuming any self-hosted LLM.

**Architecture:** A `DreamExtractor` protocol (`memory/dream.py`) with a `RegexExtractor` baseline. One shared driver `MemoryService.dream_run(extractor)` owns the pull → extract → `cortex_write` → `dream_commit` loop and cursor discipline. New read-only/driver MCP tools expose it. Tier 1 is the agent reading `memory_dream_pull` and calling the existing `memory_fact_set`, scripted by a copy-in `/dream` command. Tier 2 (BYO endpoint) and the daemon auto-sweep are **out of scope** here (Phases 3–4 of the design spec).

**Tech Stack:** Python 3.11+, `mcp` FastMCP SDK, dataclass config (`utils/config.py`), pytest with the repo's PG fixtures (`tests/pg_fixtures.py`), MiniLM-L6 embedder.

**Spec:** `docs/specs/2026-06-15-pluggable-dream-extractor-design.md`

---

## Deviations from the design spec (read first)

1. **Eligible-source filter (new).** The spec assumed `dream_pull` consumes the
   recent stream; the current code hard-filters `source == "conversation"`, which
   is empty on a Claude Code bank (deliberate stores use varied sources). This
   plan makes eligibility config-driven (`DreamConfig.exclude_sources`, default
   `{"consolidation", "reflection"}`) and pulls *all other* sources.
   **Back-compat:** any external deployment that relied on the
   `"conversation"`-only behavior should pin
   `memory.dream.eligible_sources: ["conversation"]` in its config to keep the
   narrow behavior. Called out in Task 1 and the migration note.
2. **`memory_dream_run` tool (added).** The spec listed `pull`/`status`/`commit`
   plus agent-side extraction. To make **Tier 0 end-to-end in Phase 1** (no sweep
   yet), we add a `memory_dream_run` tool that runs the configured server-side
   extractor (regex by default). Phase 3's daemon sweep will call the same
   `service.dream_run`.

---

## File structure

- `pseudolife_memory/memory/dream.py` *(new)* — `Claim`, `DreamExtractor`
  protocol, `RegexExtractor`. Self-contained, no service import (testable alone).
- `pseudolife_memory/utils/config.py` *(modify)* — add `DreamConfig`, field on
  `MemoryConfig`, loader branch.
- `pseudolife_memory/service.py` *(modify)* — `dream_run()`, `dream_status()`;
  generalize `dream_pull()` eligibility; delegate `extract_slots_regex` to
  `RegexExtractor` (single source of truth).
- `pseudolife_memory/mcp_server.py` *(modify)* — `memory_dream_pull`,
  `memory_dream_status`, `memory_dream_commit`, `memory_dream_run`.
- `examples/commands/dream.md` *(new)* — copy-in Tier 1 slash command.
- `tests/test_dream.py` *(new)* — extractor + driver + status unit/PG tests.
- `tests/test_mcp_server.py` *(modify)* — extend expected tool list + a dispatch test.
- `README.md`, `CHANGELOG.md` *(modify)* — document tiers, config, privacy/cost.

---

## Task 1: `DreamConfig` and wiring

**Files:**
- Modify: `pseudolife_memory/utils/config.py` (add after `ReflectionConfig`, ~line 285; field on `MemoryConfig` ~line 369; loader ~line 510)
- Test: `tests/test_dream.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dream.py
from pseudolife_memory.utils.config import DreamConfig, MemoryConfig


def test_dream_config_defaults():
    c = DreamConfig()
    assert c.enabled is True
    assert c.exclude_sources == ["consolidation", "reflection"]
    assert c.eligible_sources is None          # None => all-but-excluded
    assert c.min_batch == 8 and c.idle_seconds == 1800.0
    assert MemoryConfig().dream.max_batch == 40
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: FAIL — `ImportError: cannot import name 'DreamConfig'`.

- [ ] **Step 3: Add the dataclass + wiring**

```python
# config.py — after ReflectionConfig
@dataclass
class DreamConfig:
    """Dream pass — MIRAS→cortex consolidation (pluggable extractor).

    Tier 0 (regex floor) needs no config. ``eligible_sources``/``exclude_sources``
    decide which stored memories a dream consolidates; ``min_batch``/``idle_seconds``
    are the backlog+quiescence trigger used by ``dream_status`` (and, later, the
    daemon sweep). Tier-2 extractor fields are defined now for config stability but
    unused until the OpenAI-compatible extractor lands.
    """
    enabled: bool = True
    # Which stored sources are eligible. None => every source EXCEPT exclude_sources.
    eligible_sources: list[str] | None = None
    exclude_sources: list[str] = field(default_factory=lambda: ["consolidation", "reflection"])
    # Backlog + quiescence trigger (consumed by dream_status / future sweep).
    min_batch: int = 8
    idle_seconds: float = 1800.0
    max_batch: int = 40
    sweep_interval_seconds: float = 600.0   # used by the Phase 3 daemon sweep
    # Tier 2 (Phase 3) — BYO OpenAI-compatible extractor. Unused in Phases 1–2.
    extractor_base_url: str | None = None
    extractor_api_key: str | None = None
    extractor_model: str | None = None
    extractor_max_tokens: int = 400
    extractor_timeout_seconds: float = 20.0
```

```python
# config.py — MemoryConfig, beside `reflection` / `cortex`
    dream: DreamConfig = field(default_factory=DreamConfig)
```

```python
# config.py — in load_config's mem_raw block, beside the cortex branch
        if "dream" in mem_raw:
            config.memory.dream = _dict_to_dataclass(DreamConfig, mem_raw["dream"])
```

- [ ] **Step 4: Run it to confirm it passes**

Run: `pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/utils/config.py tests/test_dream.py
git commit -m "feat(dream): add DreamConfig (eligible sources + trigger thresholds)"
```

---

## Task 2: `RegexExtractor` and the `DreamExtractor` protocol

**Files:**
- Create: `pseudolife_memory/memory/dream.py`
- Modify: `pseudolife_memory/service.py:1233-1249` (`extract_slots_regex` delegates)
- Test: `tests/test_dream.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dream.py
from pseudolife_memory.memory.dream import RegexExtractor


def test_regex_extractor_pulls_slot_claims():
    claims = RegexExtractor().extract(
        ["the build timeout is 4500 seconds", "unrelated chatter"], vocab=[],
    )
    assert any(c["attribute"] == "timeout" and "4500" in c["value"] for c in claims)
    assert all({"entity", "attribute", "value", "confidence", "origin"} <= c.keys()
               for c in claims)


def test_regex_extractor_empty_on_no_slots():
    assert RegexExtractor().extract(["hello there"], vocab=[]) == []
```

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/test_dream.py -k regex_extractor -v`
Expected: FAIL — module `dream` not found.

- [ ] **Step 3: Implement `dream.py`**

```python
# pseudolife_memory/memory/dream.py
"""Pluggable dream extractors — turn recent memory text into cortex claims.

A dream consolidates the recent associative stream into canonical
``(entity, attribute, value)`` facts. The *extraction* step is pluggable:
``RegexExtractor`` is the zero-dependency floor; an OpenAI-compatible LLM
extractor (Tier 2) lands in a later phase. The shared driver lives in
``MemoryService.dream_run`` so cursor discipline and fallback live in one place.
"""
from __future__ import annotations

from typing import Protocol, TypedDict


class Claim(TypedDict):
    entity: str
    attribute: str
    value: str
    confidence: float
    origin: str          # "user" | "action" | "agent"


class DreamExtractor(Protocol):
    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        """Return canonical claims for ``texts``. ``vocab`` is the existing
        ``entity.attribute`` slot keys, so an extractor can REUSE them instead of
        reinventing variants. Must never raise — return ``[]`` on any failure."""
        ...


class RegexExtractor:
    """Deterministic no-LLM floor. Wraps ``slots.extract_slots`` (the one regex
    implementation) and shapes its output into ``Claim`` dicts."""

    def extract(self, texts: list[str], vocab: list[str]) -> list[Claim]:
        from pseudolife_memory.memory.slots import extract_slots
        claims: list[Claim] = []
        for t in texts or []:
            for s in extract_slots(t or ""):
                value = s.value if s.polarity != "-" else ("NOT " + s.value)
                claims.append(Claim(
                    entity=s.entity, attribute=s.attribute, value=value,
                    confidence=0.55, origin="agent",
                ))
        return claims
```

- [ ] **Step 4: Point `service.extract_slots_regex` at the single impl**

```python
# service.py — replace the body of extract_slots_regex (keep the signature/docstring)
    def extract_slots_regex(self, texts: list[str]) -> dict[str, Any]:
        """Deterministic no-LLM claim-extraction floor (delegates to
        RegexExtractor so the regex implementation lives in exactly one place)."""
        from pseudolife_memory.memory.dream import RegexExtractor
        claims = RegexExtractor().extract(list(texts or []), vocab=[])
        return {"claims": [{"entity": c["entity"], "attribute": c["attribute"],
                            "value": c["value"], "confidence": c["confidence"]}
                           for c in claims]}
```

- [ ] **Step 5: Run to confirm pass (incl. no regression)**

Run: `pytest tests/test_dream.py -k regex_extractor -v && pytest tests/ -k "slots or extract" -q`
Expected: PASS; existing slot/extract tests still green.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/memory/dream.py pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): RegexExtractor + DreamExtractor protocol; single regex impl"
```

---

## Task 3: generalize `dream_pull` eligibility

**Files:**
- Modify: `pseudolife_memory/service.py:1202-1231` (`dream_pull`)
- Test: `tests/test_dream.py` (PG-backed — uses real embedder)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dream.py
import pytest
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.service import MemoryService
    return MemoryService(data_dir=tmp_path, database_url=pg_url)


def test_dream_pull_includes_non_conversation_sources(svc):
    svc.store("the widget port is 9999", source="notes")     # was excluded before
    svc.store("a consolidated synthesis", source="consolidation")  # stays excluded
    out = svc.dream_pull(limit=10)
    texts = [e["text"] for e in out["entries"]]
    assert any("widget port" in t for t in texts)
    assert all("consolidated synthesis" not in t for t in texts)
```

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/test_dream.py::test_dream_pull_includes_non_conversation_sources -v`
Expected: FAIL — `notes` excluded (old code keeps only `source=="conversation"`).
(If no test Postgres is reachable the test skips — start it with `docker compose -f ops/docker-compose.yml up -d`.)

- [ ] **Step 3: Replace the eligibility check**

```python
# service.py — inside dream_pull, replace the source filter block
            cfg = self.config.memory.dream
            excluded = set(cfg.exclude_sources or [])
            allowed = set(cfg.eligible_sources) if cfg.eligible_sources else None
            cursor = self._cortex.dream_cursor
            rows: list[MemoryEntry] = []
            for band in self._cms.bands:
                for e in band.entries:
                    if allowed is not None:
                        if e.source not in allowed:
                            continue
                    elif e.source in excluded:
                        continue
                    if e.timestamp <= cursor:
                        continue
                    rows.append(e)
```

- [ ] **Step 4: Run to confirm pass**

Run: `pytest tests/test_dream.py::test_dream_pull_includes_non_conversation_sources -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): config-driven eligible-source filter in dream_pull"
```

---

## Task 4: the `dream_run` driver

**Files:**
- Modify: `pseudolife_memory/service.py` (add after `dream_commit`, ~line 1260)
- Test: `tests/test_dream.py` (PG-backed)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dream.py
from pseudolife_memory.memory.dream import RegexExtractor


def test_dream_run_promotes_and_advances_cursor(svc):
    svc.store("the gadget version is 3.2", source="notes")
    out = svc.dream_run(RegexExtractor())
    assert out["pulled"] >= 1
    assert out["inserted"] + out["confirmed"] >= 1
    assert out["cursor"] > 0
    fact = svc.cortex_lookup("gadget", "version")
    assert fact is not None and "3.2" in fact["value"]
    # Idempotent: a second run over the same (now-consolidated) tail is a no-op.
    again = svc.dream_run(RegexExtractor())
    assert again["pulled"] == 0
```

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/test_dream.py::test_dream_run_promotes_and_advances_cursor -v`
Expected: FAIL — `AttributeError: 'MemoryService' object has no attribute 'dream_run'`.

- [ ] **Step 3: Implement the driver**

```python
# service.py — after dream_commit
    def dream_run(self, extractor, *, limit: int | None = None) -> dict[str, Any]:
        """One dream cycle: pull eligible unconsolidated memories, extract claims
        via ``extractor`` (regex floor fallback if it yields nothing), write each
        to the cortex, advance the dream cursor. Returns a summary. The single
        consolidation path shared by the MCP tool and (later) the daemon sweep."""
        from pseudolife_memory.memory.dream import RegexExtractor
        cap = int(limit if limit is not None else self.config.memory.dream.max_batch)
        pulled = self.dream_pull(limit=cap)
        entries = pulled["entries"]
        if not entries:
            return {"pulled": 0, "inserted": 0, "confirmed": 0, "contested": 0,
                    "cursor": pulled["cursor"]}
        texts = [e["text"] for e in entries]
        vocab = self.cortex_vocab().get("vocab", [])
        try:
            claims = extractor.extract(texts, vocab)
        except Exception as exc:  # noqa: BLE001 — an extractor must never break a dream
            logger.warning("dream extractor failed (%s); using regex floor", exc)
            claims = []
        if not claims:
            claims = RegexExtractor().extract(texts, vocab)
        tally = {"inserted": 0, "confirmed": 0, "contested": 0, "superseded": 0}
        for c in claims:
            res = self.cortex_write(
                c["entity"], c["attribute"], c["value"],
                confidence=float(c.get("confidence", 0.55)),
                support=c.get("origin", "agent"),
            )
            tally[res["action"]] = tally.get(res["action"], 0) + 1
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        return {"pulled": len(entries), "claims": len(claims),
                "cursor": newest, **tally}
```

> Check `cortex_vocab`'s return key (`service.py:1107`); if it returns a bare
> list, adjust the `vocab = ...` line accordingly.

- [ ] **Step 4: Run to confirm pass**

Run: `pytest tests/test_dream.py::test_dream_run_promotes_and_advances_cursor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): dream_run driver (pull->extract->cortex_write->commit)"
```

---

## Task 5: `dream_status` (backlog + quiescence)

**Files:**
- Modify: `pseudolife_memory/service.py` (add beside `dream_run`)
- Test: `tests/test_dream.py` (PG-backed)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dream.py
def test_dream_status_would_fire_on_idle(svc, monkeypatch):
    svc.config.memory.dream.min_batch = 100        # never fires on batch
    svc.config.memory.dream.idle_seconds = 0.0     # everything counts as idle
    svc.store("the relay port is 4001", source="notes")
    st = svc.dream_status()
    assert st["backlog"] >= 1
    assert st["would_fire"] is True
    assert "dream_cursor" in st and "idle_seconds" in st
```

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/test_dream.py::test_dream_status_would_fire_on_idle -v`
Expected: FAIL — no `dream_status`.

- [ ] **Step 3: Implement**

```python
# service.py — beside dream_run
    def dream_status(self) -> dict[str, Any]:
        """Backlog (eligible unconsolidated memories), idle seconds since the most
        recent store, and whether the trigger would fire. Read-only — safe for a
        SessionStart nudge hook."""
        import time as _t
        cfg = self.config.memory.dream
        backlog = self.dream_pull(limit=10**9)["count"]
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._cortex is not None
            latest = max((e.timestamp for b in self._cms.bands for e in b.entries),
                         default=0.0)
            cursor = self._cortex.dream_cursor
        idle = (_t.time() - latest) if latest else 0.0
        would_fire = bool(cfg.enabled and (
            backlog >= cfg.min_batch
            or (backlog >= 1 and idle >= cfg.idle_seconds)
        ))
        return {"backlog": backlog, "idle_seconds": idle,
                "dream_cursor": cursor, "would_fire": would_fire}
```

- [ ] **Step 4: Run to confirm pass**

Run: `pytest tests/test_dream.py::test_dream_status_would_fire_on_idle -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/service.py tests/test_dream.py
git commit -m "feat(dream): dream_status (backlog/idle/would_fire trigger probe)"
```

---

## Task 6: MCP tools + registration test

**Files:**
- Modify: `pseudolife_memory/mcp_server.py` (add after `memory_world_forget`, ~line 527)
- Modify: `tests/test_mcp_server.py` (expected list ~line 57; new dispatch test)

- [ ] **Step 1: Extend the expected-tools test (failing)**

```python
# tests/test_mcp_server.py — add to the expected set after the world tools
        # Dream — MIRAS->cortex consolidation.
        "memory_dream_pull",
        "memory_dream_status",
        "memory_dream_commit",
        "memory_dream_run",
```

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/test_mcp_server.py::test_all_tools_registered -v`
Expected: FAIL — expected names not registered.

- [ ] **Step 3: Add the tools**

```python
# mcp_server.py — after memory_world_forget
@mcp.tool()
def memory_dream_status() -> dict[str, Any]:
    """Read-only: how much unconsolidated memory is waiting for a dream.

    Returns ``{backlog, idle_seconds, dream_cursor, would_fire}``. Safe to call
    from a SessionStart hook to decide whether to nudge a ``/dream``.
    """
    return service.dream_status()


@mcp.tool()
def memory_dream_pull(limit: int = 40) -> dict[str, Any]:
    """Eligible memories not yet consolidated (timestamp > dream_cursor),
    oldest-first. The agent reads these, extracts canonical facts, writes them
    with ``memory_fact_set``, then calls ``memory_dream_commit``.

    Returns ``{cursor, count, entries:[{text, timestamp, episode_id}, ...]}``.
    """
    return service.dream_pull(limit=limit)


@mcp.tool()
def memory_dream_commit(cursor: float) -> dict[str, Any]:
    """Advance the dream cursor (monotonic) after consolidating up to ``cursor``
    (the newest timestamp from the pull). Returns ``{dream_cursor}``.
    """
    return service.dream_commit(cursor)


@mcp.tool()
def memory_dream_run() -> dict[str, Any]:
    """Run one server-side dream with the regex floor (Tier 0, no LLM): pull ->
    extract -> fact_set -> commit. For higher quality, the agent should instead
    use ``memory_dream_pull`` + ``memory_fact_set`` (the ``/dream`` command).

    Returns ``{pulled, claims, inserted, confirmed, contested, cursor}``.
    """
    from pseudolife_memory.memory.dream import RegexExtractor
    return service.dream_run(RegexExtractor())
```

- [ ] **Step 4: Add a dispatch test**

```python
# tests/test_mcp_server.py
def test_memory_dream_run_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib, pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    _invoke("memory_store", {"text": "the beacon port is 7777", "source": "notes"})
    out = _invoke("memory_dream_run", {})
    assert "pulled" in out and "cursor" in out
    got = _invoke("memory_fact_get", {"entity": "beacon", "attribute": "port"})
    assert got["record"] is not None and "7777" in got["record"]["value"]
```

- [ ] **Step 5: Run both to confirm pass**

Run: `pytest tests/test_mcp_server.py::test_all_tools_registered tests/test_mcp_server.py::test_memory_dream_run_via_mcp_dispatch -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pseudolife_memory/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(dream): expose memory_dream_pull/status/commit/run MCP tools"
```

---

## Task 7: `/dream` command + docs (Tier 1 default)

**Files:**
- Create: `examples/commands/dream.md`
- Modify: `README.md` (new "Dreaming — consolidating memories into facts" section)
- Modify: `CHANGELOG.md` (Unreleased)

- [ ] **Step 1: Write the command file**

```markdown
<!-- examples/commands/dream.md
Copy to .claude/commands/dream.md in any project to get /dream. -->
---
description: Consolidate recent PseudoLife memories into canonical cortex facts
---
Run a dream pass over the PseudoLife-MCP bank:

1. Call `memory_dream_status`. If `would_fire` is false and there is no backlog,
   report "nothing to consolidate" and stop.
2. Call `memory_dream_pull` (default limit).
3. From the pulled text, extract only **durable, current-state, slot-shaped**
   facts as `(entity, attribute, value)`. Skip narrative, in-progress work, and
   superseded states. Reuse existing slot keys where they fit.
4. Write each with `memory_fact_set` (origin `user` only for things the human
   stated; otherwise `agent`).
5. Call `memory_dream_commit` with the newest timestamp from the pull.
6. Report inserted / confirmed / contested counts. Surface any `contested`
   results to the user — those are conflicts to settle, not silent overwrites.
```

- [ ] **Step 2: Document in README**

Add a section covering: the three tiers (Tier 0 `memory_dream_run` / regex floor,
Tier 1 `/dream` install via copy-in + a scheduled-routine recipe, Tier 2 noted as
upcoming), the `memory.dream` config keys, and the privacy/cost note (Tier 0 = on
box/free; Tier 1 = agent tokens; Tier 2 cloud = text leaves box).

- [ ] **Step 3: CHANGELOG (Unreleased)**

```markdown
### Added
- **Dream consolidation (Tiers 0–1).** `memory_dream_run` (regex floor, headless,
  no LLM) and `memory_dream_pull`/`memory_dream_status`/`memory_dream_commit` for
  agent-driven dreaming, plus a copy-in `/dream` command (`examples/commands/`).
  Eligible sources and the backlog+idle trigger are configurable under
  `memory.dream`.
```

- [ ] **Step 4: Sanity-run the command path end-to-end**

Run: `pytest tests/test_dream.py tests/test_mcp_server.py -q`
Expected: PASS. Manually skim `examples/commands/dream.md` for correct tool names.

- [ ] **Step 5: Commit**

```bash
git add examples/commands/dream.md README.md CHANGELOG.md
git commit -m "docs(dream): /dream command + tier/config/privacy docs"
```

---

## Final verification

- [ ] Full suite green (with test Postgres up):
  `pytest tests/ -q` — expect all prior tests + the new dream tests passing.
- [ ] Offline determinism check (per the repo's known flakiness):
  `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 pytest tests/ -q`.
- [ ] Tool count: `test_all_tools_registered` reflects +4 tools.
- [ ] Manual smoke: store a slot-shaped fact, `memory_dream_run`, confirm it
  appears via `memory_fact_get` and a second run reports `pulled: 0`.

## Migration note (back-compat)

The eligible-source change broadens the default dream beyond
`source == "conversation"`. Any external deployment that wants to keep
consolidating only auto-captured turns should pin
`memory.dream.eligible_sources: ["conversation"]` in its config. No schema
change; cursor semantics unchanged.
