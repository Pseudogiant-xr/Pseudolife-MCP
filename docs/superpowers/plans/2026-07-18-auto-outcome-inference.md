# Auto-Outcome Inference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a session episode closes with stored entries but zero outcome signals, the daemon infers up to 3 signals (`origin="inferred"`) from the episode's own contents inside the end-of-session dream, and lessons built purely from inferred signals start at confidence 0.4.

**Architecture:** A new dream stage (`infer_outcomes_stage`) between episode close and lesson synthesis. Cursor + retry bookkeeping live in the `meta` table (**no DDL, no schema bump anywhere in this plan**). The extractor grows one method (`infer_outcomes`) following `extract_lessons`' exact HTTP/JSON pattern. `dream_status.would_fire` learns to count pending inference candidates.

**Tech Stack:** Python 3.10+ stdlib inside `pseudolife_memory/` (no new deps), pytest with the existing PG-backed `pristine_service` fixtures.

**Spec:** `docs/superpowers/specs/2026-07-18-auto-outcome-inference-design.md` — read it first. Verified integration facts from the spec: session close fires the dream for ANY non-empty episode (`service.py:2701`); `synthesize_lessons` is called at `service.py:2321` (no-backlog path) and `service.py:2466` (main path); `add_signal` already takes `origin` and `episode_id` (`storage/postgres.py:520`); `get_meta`/`set_meta` JSONB helpers exist (`storage/postgres.py:624-635`).

## Global Constraints

- **No DDL, no schema bump.** Cursor state = `meta` key `outcome_inference_cursor` with shape `{"ts": float, "retry": {episode_id: int}}`.
- Refuse-don't-coerce: an inferred claim whose `outcome` is not exactly `success|failure|correction` is dropped, never mapped (mirrors `record_outcome`, `service.py:1573-1577`).
- Distinguish three extractor results: **transport failure** (raise → stage halts, cursor held), **malformed output** (`None` → bounded retry, 2 attempts, then advance past the episode), **clean empty** (`[]` → valid nothing, advance immediately).
- Inference input includes `status`/`log` source entries (deliberate exception to `dream.exclude_sources`; that list protects fact extraction only).
- Config: `memory.lessons.infer_outcomes: bool = True`, `memory.lessons.infer_outcomes_max_signals: int = 3`.
- Lessons from an all-inferred signal batch: `confidence=0.4`, `provenance |= {"inferred"}`. Mixed batches unchanged (0.6 default).
- The global lock is NOT reentrant — extractor calls must happen outside it (locked pull → unlocked extract → locked commit, the dream's existing discipline).
- Tests offline where possible; PG-backed tests use the existing fixtures (bench Postgres at 127.0.0.1:5433) and must not touch the live bank.
- Commits: conventional style + trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Run pytest via `.venv\Scripts\python.exe -m pytest`.
- CHANGELOG entry required (behavior change) — Task 7.

---

### Task 1: extractor method + parser (`dream.py`) and config knobs

**Files:**
- Modify: `pseudolife_memory/memory/dream.py` (add prompt constant, `_parse_outcome_claims`, `OpenAICompatExtractor.infer_outcomes`)
- Modify: `pseudolife_memory/utils/config.py` (two fields on `LessonsConfig`, ~line 441, plus the yaml plumbing where other lessons fields load)
- Test: `tests/test_outcome_inference.py` (new)

**Interfaces:**
- Produces: `_parse_outcome_claims(content: str, cap: int) -> list[dict] | None` — `None` = malformed (unparseable JSON / wrong shape), `[]` = clean empty, else claims `{task, outcome, about, detail}`.
- Produces: `OpenAICompatExtractor.infer_outcomes(context_text: str, *, cap: int = 3) -> list[dict] | None` — raises `ExtractorError` on transport failure; otherwise returns `_parse_outcome_claims` output. `NoOpExtractor` gets NO such method (stage skips via `getattr`, same pattern as `extract_lessons` at `service.py:1854`).
- Produces: `LessonsConfig.infer_outcomes: bool = True`, `LessonsConfig.infer_outcomes_max_signals: int = 3`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outcome_inference.py`:

```python
"""Auto-outcome inference (spec 2026-07-18): parser + config units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.memory.dream import _parse_outcome_claims
from pseudolife_memory.utils.config import LessonsConfig


def test_parse_outcome_claims_happy_path():
    content = ('noise before {"outcomes": [{"task": "deploy daemon", '
               '"outcome": "success", "about": "ops/update.ps1", '
               '"detail": "health check passed"}]} noise after')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "deploy daemon", "outcome": "success",
                       "about": "ops/update.ps1",
                       "detail": "health check passed"}]


def test_parse_outcome_claims_refuses_bad_enum_and_empty_task():
    content = ('{"outcomes": ['
               '{"task": "x", "outcome": "failed"},'
               '{"task": "", "outcome": "success"},'
               '{"task": "y", "outcome": "correction"}]}')
    claims = _parse_outcome_claims(content, cap=3)
    assert claims == [{"task": "y", "outcome": "correction",
                       "about": None, "detail": None}]


def test_parse_outcome_claims_cap():
    items = ",".join(f'{{"task": "t{i}", "outcome": "success"}}'
                     for i in range(5))
    claims = _parse_outcome_claims(f'{{"outcomes": [{items}]}}', cap=2)
    assert [c["task"] for c in claims] == ["t0", "t1"]


def test_parse_outcome_claims_malformed_vs_empty():
    assert _parse_outcome_claims("total garbage", cap=3) is None
    assert _parse_outcome_claims('{"wrong_key": []}', cap=3) is None
    assert _parse_outcome_claims('{"outcomes": []}', cap=3) == []


def test_lessons_config_defaults():
    cfg = LessonsConfig()
    assert cfg.infer_outcomes is True
    assert cfg.infer_outcomes_max_signals == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_outcome_inference.py -v`
Expected: FAIL — `ImportError: cannot import name '_parse_outcome_claims'`

- [ ] **Step 3: Implement**

In `pseudolife_memory/memory/dream.py`, near `_LESSON_SYSTEM_PROMPT`, add:

```python
_OUTCOME_INFER_SYSTEM_PROMPT = (
    "You review the stored record of one work session and infer what "
    "OUTCOMES it reached. Reply with JSON only: {\"outcomes\": [{\"task\": "
    "<short stable task-type phrase>, \"outcome\": \"success\" | "
    "\"failure\" | \"correction\", \"about\": <tool/approach concerned, or "
    "null>, \"detail\": <one sentence of evidence quoted or paraphrased "
    "from the record>}]}.\n"
    "- Claim only outcomes the record actually evidences; prefer fewer, "
    "better-grounded claims.\n"
    "- failure = an approach hit a dead-end; correction = the user "
    "corrected the assistant; success = something verifiably worked.\n"
    "- If the record shows no clear outcome, return {\"outcomes\": []}."
)


def _parse_outcome_claims(content: str, cap: int) -> list[dict] | None:
    """Parse an outcome-inference reply. ``None`` = malformed (retryable),
    ``[]`` = the model found nothing (valid, advance), else claims.
    Enum violations are dropped, never coerced (record_outcome rule)."""
    import json as _json

    s, e = content.find("{"), content.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        parsed = _json.loads(content[s:e + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict) or "outcomes" not in parsed:
        return None
    raw = parsed["outcomes"]
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        task = str(c.get("task", "")).strip()
        outcome = str(c.get("outcome", "")).strip()
        if not task or outcome not in ("success", "failure", "correction"):
            continue
        out.append({
            "task": task, "outcome": outcome,
            "about": str(c.get("about", "") or "").strip() or None,
            "detail": str(c.get("detail", "") or "").strip() or None,
        })
        if len(out) >= cap:
            break
    return out
```

On `OpenAICompatExtractor` (next to `extract_lessons`, same HTTP pattern — reuse its exact request-building code with the new prompt):

```python
    def infer_outcomes(self, context_text: str, *,
                       cap: int = 3) -> list[dict] | None:
        """Infer outcome signals from one closed episode's stored record.
        Transport failure raises ExtractorError (stage holds its cursor);
        malformed content returns None (bounded retry); [] is a valid
        nothing-found."""
        import json
        import urllib.request

        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _OUTCOME_INFER_SYSTEM_PROMPT},
                    {"role": "user", "content": context_text},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"] or ""
        except Exception as exc:  # noqa: BLE001 — transport, not content
            raise ExtractorError(f"infer_outcomes failed: {exc}") from exc
        return _parse_outcome_claims(content, cap)
```

In `pseudolife_memory/utils/config.py`, on `LessonsConfig` (after `enabled: bool = True`):

```python
    # Auto-outcome inference (spec 2026-07-18): infer signals for episodes
    # that close with entries but zero explicit outcomes. origin="inferred";
    # lessons from all-inferred batches start at confidence 0.4.
    infer_outcomes: bool = True
    infer_outcomes_max_signals: int = 3
```

Then find where `LessonsConfig` is built from yaml (search `LessonsConfig(` in `config.py`) and thread both keys with the same `raw.get(..., default)` style as the surrounding fields.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_outcome_inference.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add pseudolife_memory/memory/dream.py pseudolife_memory/utils/config.py tests/test_outcome_inference.py
git commit -m "feat(dream): outcome-inference extractor method + parser + config knobs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: candidate scan, episode context builder, cursor IO (`service.py`)

**Files:**
- Modify: `pseudolife_memory/service.py` (three private helpers + one storage helper)
- Modify: `pseudolife_memory/storage/postgres.py` (one COUNT helper)
- Test: `tests/test_outcome_inference.py` (append PG-backed tests)

**Interfaces:**
- Consumes: `get_meta`/`set_meta` (`postgres.py:624-635`), `_episode_entry_counts`, `em._descends_from` (both used in `_close_session_locked`, `service.py:2686-2691`).
- Produces (service, all called under the lock):
  - `_load_infer_cursor() -> dict` / `_save_infer_cursor(cur: dict) -> None` — meta key `outcome_inference_cursor`, shape `{"ts": float, "retry": {str: int}}`.
  - `_pending_inference_candidates(*, limit: int = 8) -> list[dict]` — each `{"root_id": str, "ended_at": float, "context": str}`, sorted by `ended_at` ascending; only closed session-keyed roots with `ended_at > cursor ts`, ≥1 entry in subtree, zero signals in subtree.
  - `_episode_inference_context(root, subtree: set[str]) -> str` — titles + all subtree entries (INCLUDING `status`/`log` sources) sorted by timestamp, `[superseded]` marked.
- Produces (storage): `count_signals_for_episodes(episode_ids: list[str]) -> int`.

- [ ] **Step 1: Write the failing tests**

First read the top of `tests/test_episodes.py` and `tests/conftest.py` to see how a session episode is opened/closed in tests (the `pristine_service` fixture and the service's session-episode API — reuse the exact helper the existing episode tests use for opening a session-keyed episode and closing it; do not invent new fixtures). Then append to `tests/test_outcome_inference.py` — the test bodies below are normative for setup-agnostic parts (assertions, cursor shape); adapt ONLY the open/store/close calls to the real API you just read:

```python
import pytest


@pytest.fixture
def closed_zero_signal_episode(pristine_service):
    """One session episode holding two entries (one status-source) and no
    outcome signals, then closed. Returns (service, root_episode_id)."""
    svc = pristine_service
    # open a session-keyed episode + store entries + close, using the same
    # calls tests/test_episodes.py uses (session hook path). One entry must
    # use source="status" to pin the status-inclusion behavior.
    ...
    return svc, root_id


def test_candidates_finds_zero_signal_closed_episode(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    with svc._lock:
        cands = svc._pending_inference_candidates()
    assert [c["root_id"] for c in cands] == [root_id]
    ctx = cands[0]["context"]
    assert "status" in ctx          # status-source entries ARE included
    assert ctx.startswith("Session:")


def test_candidates_skips_episode_with_signals(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    svc._storage.add_signal(task="t", outcome="success",
                            episode_id=root_id)
    with svc._lock:
        assert svc._pending_inference_candidates() == []


def test_candidates_respects_cursor(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    with svc._lock:
        end = svc._pending_inference_candidates()[0]["ended_at"]
        svc._save_infer_cursor({"ts": end, "retry": {}})
        assert svc._pending_inference_candidates() == []


def test_cursor_roundtrip_defaults(pristine_service):
    svc = pristine_service
    with svc._lock:
        assert svc._load_infer_cursor() == {"ts": 0.0, "retry": {}}
        svc._save_infer_cursor({"ts": 12.5, "retry": {"e1": 1}})
        assert svc._load_infer_cursor() == {"ts": 12.5, "retry": {"e1": 1}}
```

- [ ] **Step 2: Run to verify failure** — `.venv\Scripts\python.exe -m pytest tests/test_outcome_inference.py -v` (with bench Postgres up). Expected: new tests FAIL (`AttributeError: _pending_inference_candidates`), Task-1 tests still pass.

- [ ] **Step 3: Implement**

`pseudolife_memory/storage/postgres.py` (next to `add_signal`):

```python
    def count_signals_for_episodes(self, episode_ids: list[str]) -> int:
        if not episode_ids:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outcome_signals WHERE episode_id = ANY(%s)",
            (episode_ids,),
        ).fetchone()
        return int(row[0])
```

`pseudolife_memory/service.py` (near the episode-close helpers, ~line 2660):

```python
    _INFER_CURSOR_KEY = "outcome_inference_cursor"

    def _load_infer_cursor(self) -> dict:
        raw = self._storage.get_meta(self._INFER_CURSOR_KEY) \
            if self._storage else None
        if isinstance(raw, dict):
            return {"ts": float(raw.get("ts", 0.0)),
                    "retry": dict(raw.get("retry", {}))}
        return {"ts": 0.0, "retry": {}}

    def _save_infer_cursor(self, cur: dict) -> None:
        if self._storage is not None:
            self._storage.set_meta(self._INFER_CURSOR_KEY, cur)

    def _episode_inference_context(self, root, subtree: set[str]) -> str:
        """All daemon-visible session context, INCLUDING status/log-source
        entries — dream.exclude_sources protects fact extraction, not this
        (spec 2026-07-18, decision 2)."""
        assert self._cms is not None
        em = self._cms.episodes
        lines = [f"Session: {root.title or '(untitled)'}"]
        for e in em.episodes.values():
            if e.id in subtree and e.id != root.id and e.title:
                lines.append(f"Sub-task: {e.title}")
        entries = [en for band in self._cms.bands for en in band.entries
                   if en.episode_id in subtree]
        entries.sort(key=lambda en: en.timestamp)
        for en in entries:
            mark = " [superseded]" if en.superseded_at else ""
            lines.append(f"- ({en.source}){mark} {en.text}")
        return "\n".join(lines)

    def _pending_inference_candidates(self, *, limit: int = 8) -> list[dict]:
        """Caller MUST hold the lock. Closed session roots past the cursor
        with >=1 subtree entry and zero subtree outcome signals."""
        assert self._cms is not None
        if self._storage is None:
            return []
        cur = self._load_infer_cursor()
        em = self._cms.episodes
        counts = self._episode_entry_counts()
        roots = sorted(
            (e for e in em.episodes.values()
             if e.parent_id is None and e.session_key
             and e.ended_at is not None and e.ended_at > cur["ts"]),
            key=lambda e: e.ended_at)
        out: list[dict] = []
        for root in roots:
            subtree = {root.id} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, root.id)}
            if sum(counts.get(i, 0) for i in subtree) == 0:
                continue
            if self._storage.count_signals_for_episodes(list(subtree)) > 0:
                continue
            out.append({"root_id": root.id, "ended_at": root.ended_at,
                        "context": self._episode_inference_context(
                            root, subtree)})
            if len(out) >= limit:
                break
        return out
```

- [ ] **Step 4: Run to verify pass** — same command. Expected: all pass.
- [ ] **Step 5: Commit** — `feat(service): inference candidate scan + episode context + meta cursor` (+ trailer).

---

### Task 3: the dream stage + wiring into both `dream_run` call sites

**Files:**
- Modify: `pseudolife_memory/service.py`
- Test: `tests/test_outcome_inference.py` (append)

**Interfaces:**
- Consumes: Task 2 helpers; `add_signal(..., origin=, episode_id=)` (`postgres.py:520`).
- Produces: `infer_outcomes_stage(extractor) -> dict` with keys `{"scanned", "written"}` plus optional `"skipped"`; wired immediately BEFORE `self.synthesize_lessons(extractor)` at `service.py:2321` and `service.py:2466`, its stats added to both return dicts under key `"outcome_inference"`.

- [ ] **Step 1: Write the failing tests**

Append (uses the Task-2 fixture; the fake extractor is offline):

```python
class _FakeInferExtractor:
    def __init__(self, script):
        self.script = list(script)   # each: list | None | Exception
        self.calls = 0

    def infer_outcomes(self, context_text, *, cap=3):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _pending_inferred(svc, root_id):
    return [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == root_id]


def test_stage_writes_inferred_signals_and_advances(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    fake = _FakeInferExtractor([[{"task": "deploy", "outcome": "success",
                                  "about": None, "detail": "verified"}]])
    stats = svc.infer_outcomes_stage(fake)
    assert stats == {"scanned": 1, "written": 1}
    sigs = _pending_inferred(svc, root_id)
    assert len(sigs) == 1 and sigs[0]["origin"] == "inferred"
    # structurally idempotent: episode now has signals, cursor advanced
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([])) == {"scanned": 0, "written": 0}


def test_stage_clean_empty_advances_without_writes(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([[]])) == {"scanned": 1, "written": 0}
    assert _pending_inferred(svc, root_id) == []
    with svc._lock:
        assert svc._pending_inference_candidates() == []   # cursor moved


def test_stage_malformed_retries_twice_then_advances(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        assert svc._load_infer_cursor()["retry"] == {root_id: 1}
        assert len(svc._pending_inference_candidates()) == 1   # still pending
    assert svc.infer_outcomes_stage(
        _FakeInferExtractor([None])) == {"scanned": 1, "written": 0}
    with svc._lock:
        cur = svc._load_infer_cursor()
        assert cur["retry"] == {}                # cleared
        assert svc._pending_inference_candidates() == []   # advanced past


def test_stage_transport_failure_holds_cursor(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    from pseudolife_memory.memory.dream import ExtractorError
    stats = svc.infer_outcomes_stage(
        _FakeInferExtractor([ExtractorError("down")]))
    assert stats["written"] == 0
    with svc._lock:
        assert len(svc._pending_inference_candidates()) == 1   # untouched


def test_stage_respects_kill_switch(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    svc.config.memory.lessons.infer_outcomes = False
    stats = svc.infer_outcomes_stage(_FakeInferExtractor([]))
    assert stats.get("skipped") == "disabled"


def test_stage_skips_extractor_without_method(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    stats = svc.infer_outcomes_stage(object())
    assert stats.get("skipped") == "no-extractor"
```

- [ ] **Step 2: Run to verify failure** — expected `AttributeError: infer_outcomes_stage`.

- [ ] **Step 3: Implement**

In `service.py` (before `synthesize_lessons`):

```python
    def infer_outcomes_stage(self, extractor) -> dict[str, Any]:
        """Dream stage (spec 2026-07-18): infer outcome signals for closed
        zero-signal episodes. Locked pull -> unlocked extract -> locked
        commit; transport failure halts with cursor held; malformed output
        gets 2 attempts then the cursor advances past the episode."""
        cfg = self.config.memory.lessons
        if self._storage is None:
            return {"scanned": 0, "written": 0, "skipped": "no-storage"}
        if not (cfg.enabled and cfg.infer_outcomes):
            return {"scanned": 0, "written": 0, "skipped": "disabled"}
        fn = getattr(extractor, "infer_outcomes", None)
        if fn is None:
            return {"scanned": 0, "written": 0, "skipped": "no-extractor"}
        with self._lock:
            self._ensure_init()
            candidates = self._pending_inference_candidates()
        scanned = written = 0
        for cand in candidates:
            try:                                   # unlocked: extractor call
                claims = fn(cand["context"],
                            cap=cfg.infer_outcomes_max_signals)
            except Exception as exc:  # noqa: BLE001 — transport: hold cursor
                logger.warning(
                    "outcome inference halted (%s); cursor held", exc)
                break
            scanned += 1
            with self._lock:
                cur = self._load_infer_cursor()
                rid = cand["root_id"]
                if claims is None:                 # malformed: bounded retry
                    attempts = int(cur["retry"].get(rid, 0)) + 1
                    if attempts >= 2:
                        cur["retry"].pop(rid, None)
                        cur["ts"] = cand["ended_at"]
                        self._save_infer_cursor(cur)
                        logger.warning(
                            "outcome inference: advancing past episode %s "
                            "after %d malformed attempts", rid, attempts)
                        continue
                    cur["retry"][rid] = attempts
                    self._save_infer_cursor(cur)
                    logger.warning(
                        "outcome inference: malformed output for episode "
                        "%s (attempt %d); will retry next dream",
                        rid, attempts)
                    break                          # keep episode order
                for c in claims:
                    self._storage.add_signal(
                        task=c["task"], outcome=c["outcome"],
                        about=c.get("about"), detail=c.get("detail"),
                        origin="inferred", episode_id=rid)
                    written += 1
                cur["retry"].pop(rid, None)
                cur["ts"] = cand["ended_at"]
                self._save_infer_cursor(cur)
        return {"scanned": scanned, "written": written}
```

Wire both call sites — at `service.py:2321` and `service.py:2466`, immediately before each `lessons = self.synthesize_lessons(extractor)` line insert:

```python
            outcome_inference = self.infer_outcomes_stage(extractor)
```

(unindented appropriately at the 2466 site) and add `"outcome_inference": outcome_inference,` to the corresponding return dicts.

- [ ] **Step 4: Run to verify pass** — full new test file green; also run `tests/test_dream.py` to confirm no dream regression.
- [ ] **Step 5: Commit** — `feat(service): infer_outcomes dream stage — locked pull, bounded retry, origin=inferred` (+ trailer).

---

### Task 4: `dream_status` counts pending inference (would_fire)

**Files:**
- Modify: `pseudolife_memory/service.py:2522-2546` (`dream_status`)
- Test: `tests/test_outcome_inference.py` (append)

**Interfaces:**
- Consumes: `_pending_inference_candidates` (Task 2).
- Produces: `dream_status()` result gains `"infer_outcomes": {"pending": int, "retry_pending": int}`, and `would_fire` becomes true when `pending >= 1` (spec: a fired end-of-session dream must not no-op past candidates).

- [ ] **Step 1: Write the failing test**

```python
def test_dream_status_counts_inference_pending(closed_zero_signal_episode):
    svc, _root = closed_zero_signal_episode
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 1
    assert st["would_fire"] is True
    svc.config.memory.lessons.infer_outcomes = False
    st = svc.dream_status()
    assert st["infer_outcomes"]["pending"] == 0
```

- [ ] **Step 2: Run to verify failure** — KeyError `infer_outcomes`.

- [ ] **Step 3: Implement**

In `dream_status` (`service.py:2522`), after `backlog` is computed and before `would_fire`:

```python
        lessons_cfg = self.config.memory.lessons
        if lessons_cfg.enabled and lessons_cfg.infer_outcomes:
            with self._lock:
                self._ensure_init()
                infer_pending = len(self._pending_inference_candidates())
                retry_pending = len(self._load_infer_cursor()["retry"])
        else:
            infer_pending = retry_pending = 0
```

Extend `would_fire`:

```python
        would_fire = bool(cfg.enabled and (
            backlog >= cfg.min_batch
            or (backlog >= 1 and idle >= cfg.idle_seconds)
            or infer_pending >= 1
        ))
```

And the return dict gains:

```python
                "infer_outcomes": {"pending": infer_pending,
                                   "retry_pending": retry_pending},
```

Caution: `dream_status` currently takes no lock for its own body (`dream_pull` locks internally) — the snippet above takes the lock only around the candidate scan; do not wrap `dream_pull` in it (non-reentrant lock).

- [ ] **Step 4: Run to verify pass**; also `tests/test_deep_dream.py -k status` if present, plus any test matching `dream_status` (`.venv\Scripts\python.exe -m pytest tests/ -k dream_status -v`).
- [ ] **Step 5: Commit** — `feat(service): dream_status counts pending outcome inference; would_fire includes it` (+ trailer).

---

### Task 5: confidence discount for all-inferred batches (+ signal labeling)

**Files:**
- Modify: `pseudolife_memory/service.py:1834-1889` (`synthesize_lessons`)
- Modify: `pseudolife_memory/memory/dream.py` (`_format_signals` — locate with grep; it renders the signal list for the lesson prompt)
- Test: `tests/test_outcome_inference.py` (append)

**Interfaces:**
- Consumes: `pending_signals` rows (verify they include `origin` — check `_SIGNAL_COLS` in `postgres.py`; if `origin` is missing from the SELECT, add it there as part of this task).
- Produces: when EVERY drained signal has `origin == "inferred"`: each written lesson uses `confidence=0.4` and `provenance |= {"inferred"}`. Mixed or explicit batches unchanged. `_format_signals` prefixes inferred signals with `[machine-inferred]`.

- [ ] **Step 1: Write the failing test**

```python
def test_all_inferred_batch_discounts_lesson_confidence(closed_zero_signal_episode):
    svc, root_id = closed_zero_signal_episode
    svc._storage.add_signal(task="deploy daemon", outcome="failure",
                            detail="rollback needed", origin="inferred",
                            episode_id=root_id)

    class _LessonExtractor:
        def extract_lessons(self, signals):
            assert all(s.get("origin") == "inferred" for s in signals)
            return [{"task": "deploy daemon", "aspect": "process",
                     "lesson": "verify health before rollback",
                     "polarity": "-", "outcome": "failure"}]

    res = svc.synthesize_lessons(_LessonExtractor())
    assert res["lessons"] == 1
    row = svc.lesson_search("deploy daemon", top_k=1, verbose=True)
    lesson = row["entries"][0]
    assert lesson["confidence"] == pytest.approx(0.4)
    assert "inferred" in (lesson.get("provenance") or [])
```

(If `lesson_search(verbose=True)` doesn't expose `confidence`/`provenance`, read `tests/` for how existing lesson tests assert on stored lessons — e.g. via `svc._lessons` records — and assert through that route instead; the 0.4 + provenance assertions are the normative part.)

- [ ] **Step 2: Run to verify failure** — confidence asserts 0.6 ≠ 0.4.

- [ ] **Step 3: Implement**

In `synthesize_lessons`, after `signals = ...` (line 1851) add:

```python
        all_inferred = bool(signals) and all(
            s.get("origin") == "inferred" for s in signals)
```

In the `lesson_write` call (line 1871-1878) replace the `confidence=` and `provenance=` arguments:

```python
                    confidence=(0.4 if all_inferred
                                else float(c.get("confidence", 0.6))),
                    origin=c.get("origin", "agent"),
                    provenance=(set(c.get("provenance") or [])
                                | ({"inferred"} if all_inferred else set())),
```

In `dream.py`'s `_format_signals`, prefix each signal line whose `origin == "inferred"` with `[machine-inferred] ` (adapt to the function's existing line-building style; grep `def _format_signals`). Verify `pending_signals`' SELECT includes `origin` and add it if not.

- [ ] **Step 4: Run to verify pass**; also `tests/ -k lesson -v` for regressions.
- [ ] **Step 5: Commit** — `feat(lessons): all-inferred signal batches yield confidence-0.4 lessons with inferred provenance` (+ trailer).

---

### Task 6: bench rung — inference fixtures in `lesson_synthesis_bench.py`

**Files:**
- Modify: `evals/lesson_synthesis_bench.py`
- No unit tests (bench is dev-tooling, same as existing rungs); the deliverable is the rung running against a live extractor endpoint.

**Interfaces:**
- Consumes: `OpenAICompatExtractor.infer_outcomes` (Task 1); the bench's existing extractor construction/reporting conventions (read the file first and mirror them).

- [ ] **Step 1: Read `evals/lesson_synthesis_bench.py` end to end** (it is small) — note how fixtures, extractor endpoints, and the report table are structured.
- [ ] **Step 2: Add the fixture set and scoring**

Add `INFER_FIXTURES`: 8 entries, each `{"name", "context", "expect"}` where `expect` is `{"outcomes": set of (task-ish keyword, outcome) pairs}` or `"abstain"`. Cover: deploy-succeeded, dead-end-hit, user-corrected-me, mixed-session (success+failure), ambiguous-must-abstain ×2, status-only-session, single-entry-thin. Write the contexts in the same shape `_episode_inference_context` produces (`Session: ...` header, `- (source) text` lines). Example fixture (write the other seven in the same style, each 3–6 lines):

```python
    {"name": "dead-end-hit",
     "context": ("Session: fix flaky websocket test\n"
                 "- (status) tried raising the timeout to 30s — still flaky\n"
                 "- (pseudolife) Root cause: the test polls before the server "
                 "binds; timeout changes are a dead-end, poll readiness "
                 "instead"),
     "expect": {("timeout", "failure")}},
    {"name": "ambiguous-1",
     "context": ("Session: reading about vector databases\n"
                 "- (notes) pgvector supports HNSW and IVFFlat indexes"),
     "expect": "abstain"},
```

Scoring per fixture: call `extractor.infer_outcomes(context, cap=3)`; **abstain fixtures** score 1.0 iff result is `[]`; outcome fixtures score fraction of expected pairs matched (keyword `in` claimed task, exact outcome match), with a penalty flag when extra claims exceed expected count by more than 1. Report a table (fixture, score, claims) plus mean score, following the bench's existing report style, behind a new `--infer` CLI flag so existing rungs are unchanged.

- [ ] **Step 3: Smoke it** against whichever extractor endpoint is up (`--infer` with the sidecar on :8081 or Sonnet shim; if none is running, `--infer --dry-run` should print the fixtures and exit 0 — add that flag). Record output in the commit message body.
- [ ] **Step 4: Commit** — `feat(evals): outcome-inference rung in lesson synthesis bench (8 fixtures, E4B target)` (+ trailer).

---### Task 7: docs + CHANGELOG

**Files:**
- Modify: `docs/guide/episodes.md` (feature section), `docs/guide/configuration.md` (two config rows in the tuned-defaults/env table area for lessons), `docs/guide/dreaming.md` (one line in the stage list if it enumerates stages), `CHANGELOG.md` (`[Unreleased]`).

- [ ] **Step 1: episodes.md** — add a short section "Inferred outcomes at session close": what triggers (closed session, entries, zero signals), what's written (`origin="inferred"`, ≤3), the trust discount (lessons at 0.4), the status-sources exception sentence, and the kill switch (`memory.lessons.infer_outcomes: false`). Match surrounding tone; no absolutes that will drift.
- [ ] **Step 2: configuration.md** — document `memory.lessons.infer_outcomes` (default `true`) and `infer_outcomes_max_signals` (default `3`) wherever `lessons.*` knobs are listed (grep `lessons.` in the file; follow the row format).
- [ ] **Step 3: CHANGELOG** under `## [Unreleased]`, matching the `### Type (date — desc)` house style:

```markdown
### Added (2026-07-18 — auto-outcome inference at episode close)
- **The daemon now infers outcome signals for silent sessions**: when a
  session episode closes with stored entries but zero `memory_outcome`
  calls (35% of real sessions, measured 2026-07-18), a new dream stage
  infers up to 3 signals (`origin="inferred"`) from the episode's own
  record — including status-source entries — and the same dream
  synthesizes lessons from them at confidence 0.4 (vs 0.6 explicit).
  Cursor + bounded retry live in `meta` (no schema change).
  `dream_status` gains an `infer_outcomes` block and `would_fire` counts
  pending inference. Kill switch: `memory.lessons.infer_outcomes: false`.
```

- [ ] **Step 4: Guard tests** — `.venv\Scripts\python.exe -m pytest tests/test_release_ux.py -q` → all pass.
- [ ] **Step 5: Commit** — `docs: auto-outcome inference — episodes guide, config knobs, changelog` (+ trailer).

---

### Task 8: full-suite verification

- [ ] **Step 1:** `$env:HF_HUB_OFFLINE = "1"; .venv\Scripts\python.exe -m pytest tests/` with bench Postgres up (127.0.0.1:5433). Expected: all green (state whether PG tests ran).
- [ ] **Step 2:** Fix anything red; commit fixes.

---

### Task 9: ladder, deploy, live verify (MAIN SESSION — not subagent work)

Dream-write-path change → the standing ladder rule applies, and deploy touches the live bank.

- [ ] **Step 1: Ladder screen** — coordinate GPU tenancy (another session may hold the 4090), then run the extractor ladder rung for the shipped E4B model per `evals/README.md`; stale-leak must stay 0.0.
- [ ] **Step 2: Bench rung** — run Task 6's `--infer` rung against the shipped E4B sidecar; record the mean score in the ledger. No hard gate (first baseline), but an obviously-broken score (<0.5 with abstain failures) blocks deploy pending prompt iteration.
- [ ] **Step 3: Deploy** via `ops/update.ps1` only (backup → rollback tag → daemon-only `--no-deps` rebuild → health). Never `docker compose down -v`.
- [ ] **Step 4: Live verify beyond /health** — in a scratch session: store one entry, force-close the session episode (or wait out the reaper), trigger `memory_dream(action="run")`, then confirm via psql/Console that an `origin='inferred'` signal exists for that episode and `dream_status.infer_outcomes` reports sanely.
- [ ] **Step 5: Memory capture** — `memory_store` the deploy (source `pseudolife-mcp`), `memory_outcome` for the feature; diary note to re-measure the live coverage metrics (65% → ≥90% target, failure share > 30%) in 2–3 weeks.

---

## Self-review notes (done at plan-writing time)

- Spec coverage: stage+cursor+gating (Tasks 2-3), would_fire integration (Task 4 — the spec's boxed integration warning), trust plumbing (Tasks 3+5), config (Task 1), bench rung (Task 6), live success criterion + ladder + deploy verify (Task 9), docs/observability (Tasks 4+7). Out-of-scope items untouched.
- The two places the plan directs the implementer to adapt to existing test helpers (Task 2 fixture setup, Task 5 lesson-readback route) are deliberate: inventing those APIs cold risks wrong names; the normative assertions are fully specified.
- Type consistency: `_parse_outcome_claims -> list[dict] | None` is honored by `_FakeInferExtractor` scripts (`None`/`[]`/claims/Exception) and the stage's three-way branch; `{"scanned", "written"}` shape consistent across Tasks 3-4.
