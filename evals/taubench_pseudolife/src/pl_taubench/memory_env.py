"""Expose the memory READ tools on the harness environment.

The harness routes every assistant tool call through the environment
(``orchestrator -> environment.get_response(tool_call) -> use_tool``), dispatching
by name against the domain toolkit. Memory tools belong to no domain, so we
attach them to the environment instance by wrapping ``get_tools`` (advertise the
schemas) and ``get_response`` (route our names to the daemon, delegate the rest).

Doing it this way — rather than intercepting inside the agent — is what keeps a
mixed batch intact: an assistant message that emits a domain call and a memory
call together has both executed, instead of one silently dropped.

Only reads are ever exposed. Writes are refused mid-conversation on principle:
at that point the outcome is unknown, so anything written is a guess that a
later read would treat as experience. The write names are still ROUTED (so a
guessed name gets the defer message rather than an unknown-tool error), just
never advertised.

Memory calls bypass the toolkit and its DB entirely, so they cannot touch the
db-hash the reward is computed from.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from tau2.data_model.message import ToolMessage
from tau2.environment.tool import Tool

from . import prompts
from .client import PLMemoryClient
from .session import current_episode, extract_served_ids

#: What the assistant may call. Reads only, in both routes.
MEMORY_TOOL_NAMES = ("memory_search", "memory_lesson_search")

#: Never advertised, always intercepted: a name the model might guess from the
#: read tools it can see. Intercepting is deliberately wider than advertising.
WRITE_TOOL_NAMES = (
    "memory_store",
    "memory_outcome",
    "memory_dream",
    "memory_fact_set",
    "memory_set_add",
    "memory_set_remove",
    "memory_world_set",
    "memory_graph_relate",
    "memory_reinforce",
    "memory_episode_start",
    "memory_episode_end",
)

_ROUTED = set(MEMORY_TOOL_NAMES) | set(WRITE_TOOL_NAMES)

MAX_RESULT_CHARS = 6000


# ── tool schemas ─────────────────────────────────────────────────────────────
# The signatures and docstrings below define the schema the model sees. Scalar
# parameters only: some clients stringify list-typed optional parameters, which
# turns a `tags` list into a string the daemon rejects — and the tags a model
# would guess add nothing over the query anyway.
def build_memory_tools() -> list:
    """The harness ``Tool`` objects for the memory read surface."""

    def memory_search(query: str, top_k: int = 8) -> str:
        """Search your own experience of earlier work for relevant situations.

        Use it when starting on a request, and again before any step you are not
        certain about, so you reuse what worked and avoid what did not.

        Args:
            query: What you want to recall, phrased as the situation you are in.
            top_k: How many entries to return.

        Returns:
            The most relevant remembered entries, or a note that there are none.
        """
        return _dispatch("memory_search", {"query": query, "top_k": top_k})[0]

    def memory_lesson_search(query: str, top_k: int = 5) -> str:
        """Search the do/avoid guidance distilled from how earlier work turned out.

        Use it alongside `memory_search`: that returns what happened, this
        returns what to do about it.

        Args:
            query: The situation you want guidance for.
            top_k: How many lessons to return.

        Returns:
            The most relevant lessons, or a note that there are none.
        """
        return _dispatch("memory_lesson_search", {"query": query, "top_k": top_k})[0]

    return [Tool(func=memory_search), Tool(func=memory_lesson_search)]


# ── dispatch ─────────────────────────────────────────────────────────────────
def _dispatch(name: str, arguments: Optional[dict]) -> tuple[str, bool]:
    """Run one memory call for the current episode. Returns (text, is_error)."""
    if name in WRITE_TOOL_NAMES:
        return prompts.DEFER_MESSAGE, False

    episode = current_episode()
    client = getattr(episode, "client", None) if episode is not None else None
    if episode is None or client is None:
        # No episode bound means the agent was not built through our factory —
        # surface it as a tool error rather than silently returning nothing,
        # which would read to the model as "memory is empty".
        return "Error: no memory session is bound to this run.", True

    result = client.call_tool(name, dict(arguments or {}))
    episode.note_search(extract_served_ids(result))
    if isinstance(result, dict) and result.get("error"):
        return f"Error: {result['error']}", True
    text = (
        _render_lessons(result)
        if name == "memory_lesson_search"
        else _render_entries(result)
    )
    return text[:MAX_RESULT_CHARS], False


def _render_entries(result: Any) -> str:
    entries = (result or {}).get("entries") or []
    cortex = (result or {}).get("cortex") or []
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source") or "?"
        lines.append(f"- [{source}] {str(entry.get('text') or '').strip()}")
    for fact in cortex:
        if not isinstance(fact, dict):
            continue
        label = " / ".join(
            str(fact.get(k)) for k in ("entity", "attribute") if fact.get(k)
        )
        lines.append(f"- [fact] {label}: {fact.get('value')}")
    if not lines:
        return "Nothing relevant was found in your experience."
    return "\n".join(lines)


def _render_lessons(result: Any) -> str:
    entries = (result or {}).get("entries") or []
    lines: list[str] = []
    for lesson in entries:
        if not isinstance(lesson, dict):
            continue
        mark = "AVOID" if str(lesson.get("polarity") or "") == "-" else "DO"
        about = lesson.get("about") or lesson.get("aspect") or ""
        lines.append(f"- {mark} ({about}): {str(lesson.get('lesson') or '').strip()}")
    if not lines:
        return "No lessons apply to this situation yet."
    return "\n".join(lines)


# ── attachment ───────────────────────────────────────────────────────────────
def attach_memory_tools(
    environment: Any,
    route: Optional[str] = None,
    read_only: Optional[bool] = None,
) -> Any:
    """Wrap ``environment`` so the memory read tools execute against the daemon.

    Idempotent per environment instance. Must run AFTER the environment is built
    and BEFORE the agent, so the agent advertises the tools.

    ``route`` and ``read_only`` default to the environment variables, so the
    harness hook can call this with the environment alone.
    """
    if getattr(environment, "_pl_memory_attached", False):
        return environment
    if route is None:
        route = (os.environ.get("PL_ROUTE") or "entries").strip().lower()
    if read_only is None:
        read_only = _flag("PL_READ_ONLY")

    memory_tools = build_memory_tools()
    original_get_tools = environment.get_tools
    original_get_response = environment.get_response

    def get_tools(*args, **kwargs):
        return list(original_get_tools(*args, **kwargs)) + memory_tools

    def get_response(message):
        name = getattr(message, "name", None)
        if name not in _ROUTED:
            return original_get_response(message)
        text, error = _dispatch(name, getattr(message, "arguments", None))
        return ToolMessage(
            id=message.id,
            content=text,
            requestor=getattr(message, "requestor", "assistant"),
            role="tool",
            error=error,
        )

    environment.get_tools = get_tools
    environment.get_response = get_response
    environment._pl_memory_attached = True
    environment._pl_memory_route = route
    environment._pl_memory_read_only = bool(read_only)
    return environment


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")
