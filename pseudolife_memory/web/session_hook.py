"""Plain-text session-start context for the Claude Code plugin hook.

``GET /api/hook/session-start`` serves what the plugin's SessionStart hook
curls into Claude's context: the standing memory-loop instructions (same
content as ``examples/CLAUDE.memory.md`` — guard-tested in
``tests/test_plugin_packaging.py``) plus, when the request is authorized, the
session briefing. Users can replace the shipped instructions by writing
``<data_dir>/hook-instructions.md``. A briefing must never break a session
start: this module never raises and the endpoint always answers 200.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
"""


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
        try:
            md = (service.session_briefing() or {}).get("markdown", "") or ""
        except Exception:  # noqa: BLE001 — never break a session start
            md = ""
        md = md.strip()
        if md:
            parts.append(md)
    return "\n\n".join(parts)[:HOOK_CONTEXT_MAX_CHARS]
