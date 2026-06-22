# Fresh-eyes review — handoff to the build session

**Date:** 2026-06-21
**Reviewer:** a Claude Code session run *without* memory access (deliberately fresh
eyes — the neural-memory tools were not consulted, so nothing here is colored by
prior stored context). Everything below comes from reading the code/docs in this
repo and running the test suite this session.
**Audience:** the main build session, which has build context this reviewer lacks.
Where this doc infers *intent*, treat it as a question, not a verdict — you may
already have answered it.

---

## How to read this

- **[VERIFIED]** — I ran it or read the exact line(s); the `file:line` is cited.
  Trust these as facts about the tree as of this date.
- **[OPINION]** — a judgement call about design/scope. You have context I don't;
  push back freely.
- Severity: **S1** = correctness/data-loss risk · **S2** = quality/UX/maintenance
  · **S3** = cosmetic/drift.

The headline: **the engineering quality and eval discipline are genuinely high,
and the test suite is fully green.** The findings are about *scope* and *drift*,
not about the build being broken. Two things dominate: (1) a large amount of
machinery is ahead of the present single-user need, and (2) the docs/version
metadata lag the fast-moving code.

---

## Empirical anchors (trust these)

| Check | Result | How |
|---|---|---|
| Full test suite | **381 passed**, 74 warnings, 372s | `.venv` pytest, incl. live Postgres tests (`test_daemon_http`, `test_writer_keying`, `test_collision_migration`, `test_pg_storage`, `test_write_through`, `test_temporal_stamp`) |
| Core file-mode logic subset | **143 passed**, 55s | cortex/hlc/abstain/freshness/service/bm25 |
| Concurrency property | `test_two_clients_no_lost_writes` green | single-writer "no lost writes" is under test, not just asserted |

So: durability, cortex facts, single-writer safety, and consolidation are
**tested and proven**. The findings below are about the parts that are *not*
measured, plus scope and drift.

---

## Findings

### F1 — [OPINION, S2] The headline "8-tier neural memory" is the least-validated subsystem
- The README leads with the neural continuum, but **no eval isolates neural-band
  retrieval vs plain cosine.** Every eval baseline (`evals/`) is "naive top-k
  vector search," and the SUT wins via the **cortex**, not the bands.
- The default `titans` objective is **L2 self-reconstruction** (predict *x* from
  *x*, `memory/miras/objectives.py:44`). In the trained limit that drives
  `neural_scores → exact_scores` (redundant with cosine); undertrained it's
  noise, which is why there's a warmup ramp (`memory/miras/band.py:365`).
- **Crucially (see F-ARCH):** the neural MLP blend (`band.py:407`) only affects
  *retrieval ranking*. It is **not** what feeds the cortex — the dream reads
  stored **text** (`service.py:1543`), not weights. So the hippocampus→cortex
  thesis does not depend on the MLP being good at ranking.
- **Suggested action:** run one eval — neural-blend-ON vs neural-OFF
  (`neural_blend_weight=0`, i.e. exact cosine only) on a real recall corpus. It's
  cleanly separable from the dream pipeline. Either it justifies top billing or
  it gets demoted/defaulted-off. My prior: marginal in the shipped config.

### F2 — [OPINION, S2] v0.4 multi-writer machinery is ahead of the single-writer reality
- HLC clock, bitemporal `valid_time`/`tx_time`, `writer_id`/`session_id`, and a
  dormant OCC seam (`replace_facts_occ` → `raise NotImplementedError`) are built,
  but the system is single-daemon, single-writer today.
- **[VERIFIED]** Every tool call serializes on one process-global mutex — 56
  `with self._lock:` blocks in `service.py`, and the embedder encode + CMS
  store/retrieve run *inside* the lock (`service.py:470/477`, `605/611`). So the
  HLC/session/OCC ordering apparatus currently orders writes that already cannot
  race within the process.
- This is in tension with the repo's own stated Karpathy discipline ("no
  speculative features, no premature abstractions"). It's defensible **if**
  multi-process writers are a committed roadmap item; it's complexity tax if not.
- **Suggested action:** decide explicitly. Either keep it and label it "deferred
  infra for Phase-2 multi-writer" in one place, or recognize it as YAGNI and stop
  extending it. (Note: HLC monotonic ordering is cheap correctness hygiene and
  worth keeping regardless; it's the OCC/session surface that's speculative.)

### F3 — [VERIFIED, S2] "No silent fallbacks" holds for the tool path but not the persistence path
- `service.py:16` states the philosophy: errors should surface. True for tool
  calls. But there are **59 `except Exception` warn-and-continue blocks** (48
  annotated `# noqa: BLE001`), **25 of them in `service.py`**, on the
  persistence/lifecycle/mirror paths.
- For a *memory* system this is the dangerous direction: a swallowed
  `_save_cortex()` / `update_access_counts()` / cortex-hydration failure is silent
  data loss whose only trace is a stderr line nobody watches under MCP.
- **Suggested action:** split persistence exceptions from mirror exceptions. A
  failed fact/cortex *save* should surface to the caller or bump a
  health-visible error counter; a failed AGE *mirror* can stay best-effort
  (it's already rebuildable via age-sync).

### F4 — [OPINION, S2] 42 MCP tools is a large surface for tool-selection
- **[VERIFIED]** 42–43 registered `@mcp.tool()` in `mcp_server.py`.
- The repo's own global instructions steer toward ~5 (`search`, `fact_get`,
  `store`, `fact_set`, `facts`). Power-tools (`trace`, `dream_pull/commit`,
  `graph_query`, `relation_define`, the `world_*` / `lesson_*` families,
  `contenders/resolve`) dilute selection accuracy and cost context tokens every
  session.
- **Suggested action:** consider an "expert"/verbose toolset gate, or collapse
  families behind fewer dispatching tools. Not urgent; worth it before adding more.

### F5 — [VERIFIED, S3] Version & schema-number drift
- `pyproject.toml:7` → `version = "0.2.0"`, while the project is functionally
  v0.4 / DB schema v11.
- `daemon.py:129` `_health()` hardcodes `"schema": 8`; `storage/schema.py:19`
  is `SCHEMA_META_VERSION = 11`; `memory/cortex.py:40` is `SCHEMA_VERSION = 8`
  (a different subsystem's number). Three "schema" numbers in play.
- `mcp_server.py:1–29` module docstring still describes the obsolete v0.1 model
  ("Transport: stdio … no network port, no auth") — the shipped path is the HTTP
  daemon with a bearer-token gate (`daemon.py:48`).
- **Suggested action (quick wins):** bump pyproject version; derive `/health`
  schema from `SCHEMA_META_VERSION`; refresh the `mcp_server.py` header.

---

## F-ARCH — Architecture reframing (from the build owner this session)

The owner clarified the intent, which recalibrates F1 and is worth recording:

> The design is a **hippocampus + cortex** system: the MIRAS bands are the fast,
> surprise-gated, decaying episodic store (hippocampus); the **Gemma 4 2B sidecar
> "dreams" over them** to populate the canonical cortex. The regex floor existed
> but was **disabled because unreliable** (it mis-splits compound entities and
> fragments slots).

This is coherent and, honestly, the right shape. It's well-supported in code:
`cortex.auto_promote=False` by default (`config.py:357`), dream-as-sole-writer
(`memory/dream.py` `NoOpExtractor` when no endpoint), and the regex
fragmentation rationale is documented
(`docs/specs/2026-06-19-single-writer-cortex-design.md`).

**The nuance that survives the reframing** (and sharpens F1 rather than dissolving
it): the architecture justifies **bands-as-episodic-substrate**, but it does
**not** by itself justify the **gradient-updated MLP** on top of them — because
the dream consumes **text** (`dream_pull` → `e.text`, `service.py:1543`), not the
MLP weights. The hippocampus→dream→cortex pipeline would behave identically if the
bands stored text with plain cosine and no test-time-learning. So the open
question is narrow and isolable: *does the neural retrieval blend earn its keep?*
— which is exactly the F1 eval.

---

## F-DREAM — Dream cadence: code vs recollection

The owner recalled the dream firing "every ~12 turns." **[VERIFIED]** that is not
what's implemented or documented; the cadence is **store-volume + quiescence**,
never turn-based.

The actual gate (`service.py:1677`, `dream_status` → `would_fire`):

```python
would_fire = enabled AND (
    backlog >= min_batch                        # min_batch = 8
    OR (backlog >= 1 AND idle >= idle_seconds)  # idle_seconds = 1800  (30 min)
)
```

- Polled by a background thread every `sweep_interval_seconds = 600` (10 min),
  config defaults at `config.py:304`.
- **[VERIFIED]** No turn-based trigger exists anywhere. The only per-turn counter,
  `_logical_turn_count` (`cms.py:371`), drives **band-internal** promotion (moving
  entries between hippocampal tiers every `update_interval` turns) — a different
  mechanism, not the Gemma dream. Nearest number to "12" is `min_batch = 8`.
- **[VERIFIED]** The auto-sweep is **daemon-only**: `daemon.run_daemon` calls
  `start_dream_sweep()` (`daemon.py:135`), but the embedded-stdio entry point does
  **not** (`mcp_server.py:1304` starts durability only). In stdio mode the cortex
  is populated solely by explicit `memory_dream_run` / `memory_fact_set`.

**Documentation status:** the *mechanism* is in the README ("Tier 2 — headless
auto-sweep," ~L732–761, naming `min_batch`/`idle_seconds`), but the **concrete
numbers (8 / 30 min / 10 min) are only in the `DreamConfig` defaults**, and
nothing frames it as a turn cadence.

**UX implication worth a decision:** with the regex floor off and the dream
quiescence-gated, **during an active session facts accumulate in the hippocampus
but the cortex may stay empty until you pause (30 min idle) or 8 pile up.** If the
desired feel is "facts reach the cortex mid-session," that's a config/logic change
(lower `min_batch`, or add an explicit turn-keyed nudge), not current behavior.

---

## Prioritized actions

**Quick wins (cosmetic/correctness hygiene, < 1 hr each):**
1. F5 — bump `pyproject` version; compute `/health` `schema` from
   `SCHEMA_META_VERSION`; rewrite the stale `mcp_server.py` header.
2. F-DREAM — write the concrete cadence (8 / 1800s / 600s, OR-logic, daemon-only)
   into the README so future-you doesn't misremember it.

**Worth a deliberate decision:**
3. F1 — run the neural-ON/OFF recall eval; let data decide the MLP's billing.
4. F-DREAM — if mid-session cortex population is wanted, lower `min_batch` or add
   a turn-aware nudge; otherwise document the quiescence behavior as intended.
5. F3 — separate persistence-path exceptions (surface/health-count) from
   mirror-path exceptions (best-effort).

**Bigger / strategic:**
6. F2 — commit to or shelve the multi-writer (OCC/session) infra; keep HLC.
7. F4 — shrink/gate the 42-tool surface before it grows further.

---

## Open questions for the build session (you have the context I don't)

- F2: Is multi-process/multi-writer a real roadmap item, or was the v0.4 temporal
  work mainly about *correctness* (HLC ordering, bitemporal audit) with
  multi-writer as a nice-to-have framing? That answers whether OCC/session is
  YAGNI or deferred-infra.
- F1: Was the neural blend ever measured against plain cosine at any point? If a
  result exists, F1 collapses to "document it."
- F-DREAM: Is the quiescence gate intentional (don't disturb active work) or a
  leftover from before the sidecar was default-on? That decides action #4.
