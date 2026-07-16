# Sonnet sidecar cutover — primary/fallback extractor with console visibility

**Date:** 2026-07-11
**Status:** approved (design review in session)
**Context:** the Sonnet-5 ceiling probe (commits 487b7bd, 099cc5b) showed the
Max-plan CLI shim + `evals/prompts/sonnet_extractor_v1.md` is the strongest
extractor measured on LongMemEval-KU (cortex 0.808/0.731/0.782 across three
answer replicas ≈ 0.774 ± 0.04, vs e4b-ft 0.603; ladder gold 1.0 /
stale-leak 0.0). This spec cuts the LIVE dream sidecar over to Sonnet while
keeping the local E4B container as an automatic fallback, with the active
extractor visible and overridable in the Cortex Console.

## Goal

The daemon's dream pass uses Sonnet (via `evals/sonnet_shim.py` on the host)
as its primary extractor, falls back to the in-stack E4B container when the
shim is unreachable or the CLI is logged out, shows which extractor is active
in the console, and lets the user force either side from the console.

## Non-goals

- No change to the bench/eval harness (the cloud rung stays out of
  `LADDER_ORDER`; sovereign-only default sweep stands).
- No Anthropic API path (Max-plan CLI only; no API key handling).
- No per-fact provenance stamping of the extractor (run-level logging only).
- The Arm-1 datagen teacher work is a separate spec.

## Architecture

Two OpenAI-compatible endpoints, one selection step per dream invocation:

```
daemon (Docker) ──auto──► primary  http://host.docker.internal:8082/v1  (sonnet_shim.py, host)
        │  probe fails / mode=fallback
        └────────────────► fallback http://pseudolife-extractor:8081/v1  (E4B container, unchanged)
```

Prompt ownership is unchanged: the daemon always sends the production
`_SYSTEM_PROMPT`; the shim's existing prefix-swap substitutes the v1 prompt
for Sonnet claims extraction (relations/lessons pass through), and the E4B
fallback natively receives the production prompt it was trained on.

## Components

### 1. Config — `pseudolife_memory/utils/config.py` (`DreamConfig`)

Three new fields after the existing extractor block, same env-vs-config
ownership rules (`extractor_source` governs all of them; api_key rule
unchanged):

- `fallback_base_url: str | None = None` — env
  `PSEUDOLIFE_DREAM_FALLBACK_BASE_URL`. **Unset ⇒ the feature is inert**:
  single-extractor behavior is byte-identical to today.
- `fallback_model: str | None = None` — env
  `PSEUDOLIFE_DREAM_FALLBACK_MODEL`.
- `extractor_mode: str = "auto"` — env `PSEUDOLIFE_DREAM_EXTRACTOR_MODE`,
  enum `auto | primary | fallback`. `auto` probes then falls back;
  `primary` never falls back (outages hold — today's semantics);
  `fallback` skips the primary entirely (sovereign-only override).

Timeout and max_tokens are shared by both endpoints (no fallback-specific
copies — YAGNI; E4B's 480s deploy override already covers the slow side).

### 2. Selection — `pseudolife_memory/memory/dream.py`

New `build_extractor_with_fallback(cfg) -> tuple[OpenAICompatExtractor, str]`
returning the extractor plus which side it is (`"primary"` / `"fallback"`):

- mode `primary`, or fallback unset → primary, no probe.
- mode `fallback` → fallback (error if fallback unset).
- mode `auto` → probe the primary with a ~3s timeout: GET `/health` at the
  base with any trailing `/v1` stripped (the shim serves `/health` at root);
  if that 404s (plain llama-server endpoints), GET `{base_url}/models`.
  Success → primary; failure/503 → fallback.

Constructed fresh per dream invocation (same as `build_extractor` today), so
recovery is automatic at the next sweep. Mid-dream failures keep the existing
hold/retry/quarantine semantics unchanged, applied to whichever side was
selected. All `build_extractor` call sites that drive the LIVE dream
(daemon sweep, `web/routes.py::_dream_run`, `mcp_server.py`) switch to the
new function; the bench/eval harness keeps constructing extractors directly.

### 3. Shim health honesty — `evals/sonnet_shim.py`

`GET /health` currently returns `{"status": "ok"}` unconditionally. Upgrade:
on first health request (and then cached for 5 minutes), run a trivial
`claude -p` call ("Reply OK"); return `{"status": "ok"}` on success and HTTP
503 `{"status": "cli_error", "detail": ...}` on failure (not logged in,
exe missing). The daemon's probe therefore sees a logged-out CLI as
primary-down and falls back instead of dreaming into 500s.

### 4. Status surface — `pseudolife_memory/service.py`

- The service records `_last_dream_extractor: dict | None`
  (`{"which": "primary"|"fallback", "base_url": ..., "at": epoch}`) whenever
  a dream runs; `dream_run`'s result dict gains `"extractor": which`.
- `dream_status()` gains: `extractor_mode`, `primary_url`, `fallback_url`
  (None when unset), `primary_healthy` (live probe, ~2s timeout, only when a
  fallback is configured — otherwise None), and `last_dream_extractor`.

### 5. Console — schema + badge

- `pseudolife_memory/web/config_io.py`, Extractor group, three entries:
  mode (`enum`, options `["auto", "primary", "fallback"]`, live), fallback
  base URL (`string`/url, suggestions include
  `http://pseudolife-extractor:8081/v1`), fallback model (`string`,
  suggestion `extractor`). The existing settings UI renders them — the
  override needs no bespoke frontend.
- `pseudolife_memory/web/static/js/views/observatory.js`: `dreamPanel` (and
  the `signalsStrip` chip row) show an extractor badge fed by the new
  `dream_status` fields — e.g. "extractor: primary ✓" / "extractor:
  FALLBACK (primary unreachable)" / nothing when no fallback is configured.
- `pseudolife_memory/web/fixtures.py::dream_status` gains the new fields so
  the devserver renders the badge.

### 6. Ops — new `ops/install-shim-autostart.ps1`

A dedicated script (the daemon autostart script is legacy pre-Docker; do not
resurrect it) registering a Scheduled Task "Pseudolife Sonnet Shim" at logon:

```
python evals/sonnet_shim.py --port 8082 \
    --system-prompt-file evals/prompts/sonnet_extractor_v1.md \
    > %USERPROFILE%\.pseudolife-mcp\sonnet-shim.log 2>&1
```

Same pattern as `install-autostart.ps1` (hidden window, restart-on-failure,
repo-venv python detection). README/CHANGELOG get a short "Sonnet sidecar
(optional)" section documenting the three env vars and the cutover values.

### Cutover values (ops, not code)

```
PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:8082/v1
PSEUDOLIFE_DREAM_MODEL=extractor
PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1
PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor
PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto
```

## Error handling

| Failure | Behavior |
|---|---|
| Shim process down | probe fails → fallback dreams this sweep; next sweep re-probes |
| CLI logged out | shim /health 503 → same as above |
| Both endpoints down | existing behavior: dream holds, entries queue, no loss |
| Fallback configured but unreachable in `auto` after primary also failed | selected fallback fails mid-dream → existing hold/retry semantics |
| `mode=fallback` with fallback unset | explicit config error surfaced in dream_run result + console |

## Testing

- Unit (`tests/`): selection matrix (primary up/down × mode × fallback
  set/unset) with a mocked probe; `dream_status` new fields; env-vs-config
  precedence for the three new vars; config round-trip via `config_io`.
- Shim: health-check caching + 503 on CLI failure (subprocess mocked).
- Live validation after deploy (backup first, per deploy discipline):
  dream once → badge "primary"; stop the shim → dream → badge "FALLBACK";
  restart shim → next dream returns to primary.

## Risks / notes

- **Workload transfer**: KU-oracle measured conversational content; the live
  bank is developer-workflow content. Ladder screens say v1 transfers
  (gold 1.0, stale-leak 0.0) but the first week of dream logs should be
  eyeballed.
- **Quality mixing**: outage windows produce E4B-quality facts in a
  Sonnet-quality bank — accepted (that is today's uniform quality).
- **Laptop/offline**: `mode=fallback` is the one-click sovereign override in
  the console.
