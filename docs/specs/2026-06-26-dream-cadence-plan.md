# Dream Cadence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the background dream consolidate ~10 min after the user goes quiet (not 30) by lowering one config default, and document the (intentional) quiescence cadence + on-demand escape hatches.

**Architecture:** Pure default change — `DreamConfig.idle_seconds` 1800 → 600. The dream sweep is daemon-only and `_apply_mcp_defaults` does not override the cadence, so the library default is the single authoritative lever; the `would_fire` logic is untouched. Three mirrors (Console config metadata, an Observatory JS fallback, one test) are updated to match, and the README "Dreaming" section gains the concrete cadence + on-demand guidance.

**Tech Stack:** Python 3.10+ dataclasses (`pseudolife_memory/utils/config.py`), pytest, vanilla JS (no build), Markdown.

## Global Constraints

- Run tests with the offline env (project gotcha — deterministic, fast): `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. Interpreter: `.venv/Scripts/python.exe`.
- **Behaviour-preserving:** only the default *value* moves; the `would_fire` expression (`service.py:1813`) must not change.
- No new dependencies. No new tools. No change to `min_batch` (8) or `sweep_interval_seconds` (600).
- Spec: `docs/specs/2026-06-26-dream-cadence-design.md`.
- Commit style: conventional commits; end the message body with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Out of scope (do NOT do): turn-based trigger; episode-end dream; live-daemon redeploy; per-entry extraction batching.

---

### Task 1: Lower the `idle_seconds` default to 600s (+ UI/config mirrors)

**Files:**
- Modify: `pseudolife_memory/utils/config.py:284-288` (the `DreamConfig` quiescence block)
- Modify: `pseudolife_memory/web/config_io.py:123-126` (the `memory.dream.idle_seconds` knob metadata)
- Modify: `pseudolife_memory/web/static/js/views/observatory.js:132` (config-not-loaded fallback)
- Test: `tests/test_dream.py:88-96` (`test_dream_config_defaults`)

**Interfaces:**
- Consumes: nothing (leaf change).
- Produces: `DreamConfig().idle_seconds == 600.0`. No signature changes — `DreamConfig` keeps the same fields/types.

- [ ] **Step 1: Update the test to expect the new default**

In `tests/test_dream.py`, in `test_dream_config_defaults` (line 95), change the assertion:

```python
    assert c.min_batch == 8 and c.idle_seconds == 600.0
```

(was `idle_seconds == 1800.0`).

- [ ] **Step 2: Run the test to verify it fails**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: FAIL — `assert 1800.0 == 600.0` (the dataclass default is still 1800).

- [ ] **Step 3: Lower the default in `DreamConfig`**

In `pseudolife_memory/utils/config.py`, change the quiescence block (lines 284-286) from:

```python
    # Backlog + quiescence trigger (consumed by dream_status / future sweep).
    min_batch: int = 8
    idle_seconds: float = 1800.0
```

to:

```python
    # Backlog + quiescence trigger (consumed by dream_status + the daemon sweep).
    # idle_seconds is deliberately short-ish: consolidate ~10 min after the user
    # goes quiet, but NEVER mid-session (any store resets idle) — see
    # docs/specs/2026-06-26-dream-cadence-design.md.
    min_batch: int = 8
    idle_seconds: float = 600.0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py::test_dream_config_defaults -v`
Expected: PASS.

- [ ] **Step 5: Update the Console config-editor default (mirror)**

In `pseudolife_memory/web/config_io.py`, the `memory.dream.idle_seconds` entry (lines 123-126), change `"default": 1800.0` to `"default": 600.0`:

```python
    {"path": "memory.dream.idle_seconds", "group": "Dream",
     "label": "Quiescence (s)", "type": "float", "default": 600.0,
     "min": 0.0, "max": 86400.0, "step": 60.0, "restart": False,
     "help": "Idle time required before a dream fires."},
```

- [ ] **Step 6: Update the Observatory JS fallback (mirror)**

In `pseudolife_memory/web/static/js/views/observatory.js` (line 132), change the config-not-loaded fallback from `?? 1800` to `?? 600`:

```javascript
  const idleThresh = knob(cfg, "memory.dream.idle_seconds") ?? 600;
```

- [ ] **Step 7: Run the affected suites to confirm nothing else pinned the old default**

Run: `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -m pytest tests/test_dream.py tests/test_web.py tests/test_phase0_config.py -v`
Expected: PASS (no other test asserts `idle_seconds == 1800`).

- [ ] **Step 8: Commit**

```bash
git add pseudolife_memory/utils/config.py pseudolife_memory/web/config_io.py pseudolife_memory/web/static/js/views/observatory.js tests/test_dream.py
git commit -m "feat(dream): idle_seconds default 1800->600 (consolidate ~10min after quiet)

Quiescence gate unchanged; only the default value moves. Mirrors updated
(config_io knob, observatory.js fallback, test). Per
docs/specs/2026-06-26-dream-cadence-design.md (P1.4).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Document the cadence + on-demand path in the README

**Files:**
- Modify: `README.md` (the "Dreaming — consolidating memories into facts" section; insert after the `memory.dream` paragraph that currently ends at line 846, before `**Privacy & cost.**`)

**Interfaces:**
- Consumes: nothing.
- Produces: README prose only. No code.

- [ ] **Step 1: Insert the cadence + on-demand subsection**

In `README.md`, immediately after the paragraph that ends:

```
What gets consolidated and when is configurable under `memory.dream`
(`eligible_sources` / `exclude_sources`, and the `min_batch` / `idle_seconds`
backlog+quiescence thresholds that `memory_dream_status` reports).
```

insert a blank line and this new content:

```markdown
**Cadence — quiescence-gated, daemon-only.** The auto-sweep (Tier 2) fires when:

```
backlog ≥ min_batch (8)   OR   (backlog ≥ 1 AND idle ≥ idle_seconds (600s))
```

polled every `sweep_interval_seconds` (600s). It runs **only in the daemon** — the
embedded stdio mode never sweeps. There is **no turn-based trigger** (the cortex
does not "dream every N turns"), by design: consolidating mid-session would distil
half-formed, still-changing state into canonical facts and burn the CPU extractor
during your foreground work. So during an active session, prose-stored facts stay
in the searchable bands and reach the cortex once you go quiet (~10 min idle) or a
backlog of 8 accumulates.

**Want a fact canonical *now*, mid-session?** Two on-demand paths bypass the wait:
`memory_fact_set` writes a canonical fact instantly, and `memory_dream_run` forces
a full consolidation sweep on the spot (the `/dream` command wraps it).
`memory_search` finds the original prose the entire time regardless.
```

- [ ] **Step 2: Verify the new content is present and the old misconception is gone**

Run: `grep -n "no turn-based trigger\|canonical \*now\*\|idle ≥ idle_seconds" README.md`
Expected: matches the three inserted phrases. Then confirm no stray "every ~12 turns" / "every N turns" claim remains:
Run: `grep -ni "every .*turns" README.md`
Expected: no match (or only unrelated matches outside the Dreaming section — review each).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): document dream cadence (quiescence-gated, daemon-only) + on-demand path

States the concrete trigger (8 / 600s / 600s), why quiescence is intentional,
that there is no turn-based trigger, and the fact_set / memory_dream_run escape
hatches. Per docs/specs/2026-06-26-dream-cadence-design.md (P1.4).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Companion change (out-of-repo, optional — apply only with the user's nod)

Not a git task — the user's **global** `~/.claude/CLAUDE.md` is outside this repo.
With explicit confirmation, add one line to its memory section:

> The background **dream** is quiescence-gated (won't fire mid-active-session) — for
> a fact you want canonical *now*, use `memory_fact_set` (instant) or
> `memory_dream_run` (force a sweep).

---

## Self-Review

**Spec coverage:**
- Spec "Change" (idle_seconds 1800→600 + 3 mirrors) → Task 1. ✓
- Spec "Documentation" (README) → Task 2. ✓
- Spec "Documentation" (global CLAUDE.md) → Companion section (out-of-repo, gated). ✓
- Spec "Out of scope" → Global Constraints "do NOT do" list. ✓
- Spec "Testing" (updated default assertion + would_fire untouched) → Task 1 Steps 1-4, 7. ✓
- Spec "Success criteria" 1 (default + mirrors) → Task 1; 2 (would_fire byte-identical) → Global Constraints + no logic step; 3 (README) → Task 2; 4 (suite green) → Task 1 Step 7. ✓

**Placeholder scan:** none — every step has the exact code/command/expected output.

**Type consistency:** no new types/signatures; `DreamConfig` fields unchanged; the JS `idleThresh` and the `idle_seconds` knob path match the existing code.
