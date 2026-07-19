"""Plain-text session-start context for the Claude Code plugin hook.

``GET /api/hook/session-start`` serves what the plugin's SessionStart hook
curls into Claude's context: the standing memory-loop instructions (same
content as ``examples/CLAUDE.memory.md`` — guard-tested in
``tests/test_plugin_packaging.py``) plus, when the request is authorized, the
session briefing. Users can replace the shipped instructions by writing
``<data_dir>/hook-instructions.md``. A briefing must never break a session
start: this module never raises and the endpoint always answers 200.

When the hook passes a ``session_id`` (identity tier 3, spec 2026-07-18),
``hook_session_start`` additionally registers a session episode and the
active-session pointer, and prepends a one-line advertisement of the episode
handle so the agent can pass ``episode=`` on writes from concurrent sessions.
``hook_session_end`` mirrors this on the SessionEnd hook: it closes the
session's episode and clears the pointer (only if still owned). Both are
fail-open — registration/close failures are logged and never surface to the
caller.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("pseudolife-mcp.web")

# Claude Code caps SessionStart hook stdout at 10,000 chars (overflow is
# spilled to a file + preview, which defeats the point) — stay clear of it.
HOOK_CONTEXT_MAX_CHARS = 9_500

MEMORY_LOOP_BLOCK = """\
## Memory — your long-term memory; use it every session (tools: `mcp__pseudolife-memory__*`)
One shared memory bank across all sessions. Treat it as a loop with three
beats: RECALL at the start, CAPTURE as you go, REFLECT at the end. Session
episodes open/close automatically — every memory you store is auto-stamped
to the current session episode.

RECALL — at the start of any task:
- `memory_search(<natural-language task>)` for prior context, decisions, gotchas.
- `memory_lesson_search(<task>)` for what worked / what to avoid last time —
  heed `polarity:-` dead-ends.
- `memory_fact_get(entity, attribute)` for one canonical value. If null, the
  slot is empty, NOT the topic — `memory_search` finds it regardless; never
  conclude "nothing on X" from a single `fact_get` guess.
- `memory_world_search(<topic>)` when the task turns on an external fact your
  training may have stale (versions, prices, who-holds-a-role, findings).
- `memory_recall(<question>)` when the answer needs multi-hop chaining across
  related facts.
- Results are compact (`{id, text, source, tags, score}`). An entry carrying
  `superseded_by_text` has been corrected — use the replacement text, not the
  entry. Pass `verbose=true` only when debugging retrieval.

CAPTURE — as durable things arise (one claim per call):
- Name the session EARLY: `memory_session_title("<project> - <topic>")`.
- `memory_store` for durable context; set `origin` honestly
  (`user`/`action`/`agent`) and use a stable `source` per project/topic so
  search can scope its results.
- `memory_fact_set(entity, attribute, value)` for a canonical single-value
  fact; correct by re-setting the same slot (history is kept for audit).
- `memory_world_set(entity, attribute, value, source_url=, source_quote=)`
  for any EXTERNAL fact you verified via web/docs — route research findings
  here (cited), not into plain `memory_store`.
- Open a named sub-episode with `memory_episode_start(title)` for a big
  multi-step task; `memory_episode_end` pops back.
- Route verbose status/progress/logs under `source="status"` — searchable,
  but excluded from dream extraction so they don't pollute the graph.
- Never store secrets: no tokens, API keys, passwords, or credentials.

REFLECT — at task end, or the moment an outcome lands:
- `memory_outcome(task, outcome, about=, detail=)` whenever something WORKED
  (`success`), was a dead-end (`failure`), or the user corrected you
  (`correction`). These signals are the only feeder for procedural LESSONS —
  the dream distils them into the do/avoid guidance surfaced at your next
  session start. Logging outcomes is how you stop repeating mistakes.

Be judicious — one claim per call; skip fleeting chatter (the surprise gate
drops near-duplicates; `stored=false` is not an error). The first memory call
of a session may lag a few seconds (one-time warmup).

If this session has NO `memory_*` tools, the MCP transport isn't registered
(this briefing arrives via a hook, a separate channel) — tell the user to run
the repo installer (`ops/install.sh` / `ops\\install.ps1`), which wires it.
"""


ONBOARDING_BLOCK = """\
Your memory bank is EMPTY — this session is where it starts. Seed it as you
work: name the session (`memory_session_title`), store two or three durable
facts about the current project (`memory_store`), set one canonical value
you'll want back (`memory_fact_set`), and log the first `memory_outcome`
when something works or fails. The dream consolidates whatever you capture
into facts, a knowledge graph, and lessons — a seeded bank compounds; an
empty one stays empty."""


def _cold_bank(service: Any) -> bool:
    """True only when the bank is provably empty — any doubt means warm
    (onboarding noise on a working bank is worse than none on a cold one)."""
    try:
        return (service.stats() or {}).get("total_memories") == 0
    except Exception:  # noqa: BLE001 — never break a session start
        return False


def _instructions(service: Any) -> str:
    """The shipped block, unless the user placed an override at
    ``<data_dir>/hook-instructions.md`` (blank/unreadable → shipped block)."""
    try:
        p = Path(getattr(service, "data_dir", "")) / "hook-instructions.md"
        if p.is_file():
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception:  # noqa: BLE001 — never break a session start
        pass
    return MEMORY_LOOP_BLOCK.rstrip()


def session_start_context(service: Any, authorized: bool) -> str:
    """Instructions always; briefing only for authorized callers (the
    instructions are public repo content, the briefing is memory content)."""
    parts = [_instructions(service)]
    if authorized:
        if _cold_bank(service):
            parts.append(ONBOARDING_BLOCK)
        try:
            md = (service.session_briefing() or {}).get("markdown", "") or ""
        except Exception:  # noqa: BLE001 — never break a session start
            md = ""
        md = md.strip()
        if md:
            parts.append(md)
    return "\n\n".join(parts)[:HOOK_CONTEXT_MAX_CHARS]


def _episode_advertisement(session_id: str, source: str | None, service: Any) -> str:
    """Register the session (idempotent per ``session_id``) and return the
    one-line episode-handle advertisement, or "" on any failure (fail-open —
    a registration hiccup must not break session start)."""
    try:
        # Generic-shaped title so the auto-titler recognises and replaces it
        # at close (GENERIC_TITLE_RE) — a literal "session" would stick
        # forever (2026-07-19 whole-branch review, finding 2).
        import time as _t
        ep = service.episode_start_session(
            session_id, _t.strftime("session - %Y-%m-%d %H:%M"))
        service.set_active_session(session_id)
        short = (ep.get("id") or "")[:12]
        if not short:
            return ""
        return (f'Session episode: {short} — pass episode="{short}" on memory '
                f"writes when running concurrent sessions.")
    except Exception:  # noqa: BLE001 — never break a session start
        logger.exception(
            "session-start identity registration failed for session_id=%r "
            "(source=%r)", session_id, source)
        return ""


def hook_session_start(
    service: Any, session_id: str | None = None, source: str | None = None,
    authorized: bool = True,
) -> str:
    """``session_start_context`` plus (when ``session_id`` is given) identity
    registration: opens/re-fires the session's episode, sets it as the active
    session (identity tier 3), and prepends the episode-handle advertisement.
    Without ``session_id`` this is exactly ``session_start_context``'s
    behaviour. Never raises; the endpoint always answers 200."""
    prefix = ""
    if session_id:
        ad = _episode_advertisement(session_id, source, service)
        if ad:
            prefix = ad + "\n\n"
    body = session_start_context(service, authorized)
    return (prefix + body)[:HOOK_CONTEXT_MAX_CHARS]


def hook_session_end(service: Any, session_id: str | None = None) -> dict[str, Any]:
    """Close the session's episode and clear the active-session pointer (only
    if it still names ``session_id`` — the ownership guard means a foreign
    SessionEnd can't clear another session's pointer). Fail-open: errors are
    logged, never raised; always returns ``{"ok": True}``."""
    if session_id:
        try:
            service.episode_end_session(session_id)
        except Exception:  # noqa: BLE001 — never break a session end
            logger.exception(
                "session-end episode close failed for session_id=%r", session_id)
        try:
            service.clear_active_session(session_id)
        except Exception:  # noqa: BLE001 — never break a session end
            logger.exception(
                "session-end pointer clear failed for session_id=%r", session_id)
    return {"ok": True}
