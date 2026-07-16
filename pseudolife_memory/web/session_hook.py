"""Plain-text session-start context for the Claude Code plugin hook.

``GET /api/hook/session-start`` serves what the plugin's SessionStart hook
curls into Claude's context: the standing memory-loop instructions (same
content as ``examples/CLAUDE.memory.md`` — guard-tested in
``tests/test_plugin_packaging.py``) plus, when the request is authorized, the
session briefing. A briefing must never break a session start: this module
never raises and the endpoint always answers 200.
"""
from __future__ import annotations

from typing import Any

# Claude Code caps SessionStart hook stdout at 10,000 chars (overflow is
# spilled to a file + preview, which defeats the point) — stay clear of it.
HOOK_CONTEXT_MAX_CHARS = 9_500

MEMORY_LOOP_BLOCK = """\
## Memory — use it every session (tools: `mcp__pseudolife-memory__*`)
RECALL at task start:
- `memory_search(<task>)` for prior context/decisions/gotchas;
  `memory_lesson_search(<task>)` for what worked / what to avoid (heed `polarity:-`);
  `memory_fact_get(entity, attribute)` for one canonical value;
  `memory_world_search(<topic>)` when an external fact may be stale.
CAPTURE as durable things arise (one claim per call):
- `memory_store` for durable context (set `origin`: user/action/agent);
  `memory_fact_set` for a canonical single-value fact (correct by re-setting the slot);
  `memory_world_set(..., source_url=, source_quote=)` for a verified EXTERNAL fact (cite it);
  open a named sub-episode with `memory_episode_start` for a big multi-step task.
  Route verbose status/logs under `source="status"` (searchable, but excluded from
  the dream so they don't pollute the graph).
REFLECT at task end / when an outcome lands:
- `memory_outcome(task, outcome, about=, detail=)` for a success / dead-end / correction —
  the dream distils these into the lessons surfaced at your next session start.
"""


def session_start_context(service: Any, authorized: bool) -> str:
    """Instructions always; briefing only for authorized callers (the
    instructions are public repo content, the briefing is memory content)."""
    parts = [MEMORY_LOOP_BLOCK.rstrip()]
    if authorized:
        try:
            md = (service.session_briefing() or {}).get("markdown", "") or ""
        except Exception:  # noqa: BLE001 — never break a session start
            md = ""
        md = md.strip()
        if md:
            parts.append(md)
    return "\n\n".join(parts)[:HOOK_CONTEXT_MAX_CHARS]
