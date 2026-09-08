"""The ``pl_memory`` agent: read memory during the task, write it back after.

The agent itself is thin on purpose. Retrieval is the model's own decision (the
memory tools are ordinary environment tools; the agent only nudges once), and
every write happens after grading, in ``on_simulation_end``. Nothing is written
mid-conversation: at that point the outcome is unknown, so a write is a guess
that later reads would treat as experience.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from tau2.agent.base_agent import ValidAgentInputMessage
from tau2.agent.llm_agent import LLMAgent, LLMAgentState  # noqa: F401  (state type)
from tau2.data_model.message import UserMessage
from tau2.environment.tool import Tool

from . import prompts, telemetry
from .client import PLMemoryClient
from .context import get_context
from .reflection import default_llm_call, run_reflection
from .session import (
    DEFAULT_WINDOW_SECONDS,
    Episode,
    bind_episode,
    clear_episode,
    new_episode,
)

DEFAULT_URL = "http://127.0.0.1:8765"
VALID_ROUTES = ("entries", "lessons")
VALID_SUPERVISION = ("experience", "instruction")
VALID_DREAM = ("off", "episode", "trial")


class PLMemoryAgent(LLMAgent):
    """An LLM agent that consults its own memory and reflects into it afterwards."""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
        *,
        episode: Episode,
        client: PLMemoryClient,
        route: str = "entries",
        supervision: str = "experience",
        read_only: bool = False,
        dream: str = "off",
        run_tag: str = "",
        task=None,
        reflection_llm: Optional[str] = None,
        reflection_llm_args: Optional[dict] = None,
    ) -> None:
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        self.episode = episode
        self.client = client
        self.route = route
        self.supervision = supervision
        self.read_only = read_only
        self.dream = dream
        self.run_tag = run_tag
        # Read ONLY in on_simulation_end, and only under instruction on a
        # failure. Holding the reference is not a leak; reading it during the
        # conversation would be.
        self.task = task
        self.reflection_llm = reflection_llm or llm
        self.reflection_llm_args = (
            reflection_llm_args if reflection_llm_args is not None else dict(self.llm_args or {})
        )
        self._nudged = False
        self._closed = False

    # ── conversation ─────────────────────────────────────────────────────────
    def generate_next_message(self, message: ValidAgentInputMessage, state):
        return super().generate_next_message(self._maybe_nudge(message), state)

    def _maybe_nudge(self, message: ValidAgentInputMessage) -> ValidAgentInputMessage:
        """Append the retrieval nudge once, to a copy of the first user turn.

        After the request, not before, so the model reads what is being asked
        first and the reminder lands as a trailing instruction rather than as
        framing. Once, not per turn: repeated nudging fragments the model's
        execution. The copy is private to the agent's state — the recorded
        transcript is unchanged.
        """
        if self._nudged or not isinstance(message, UserMessage):
            return message
        content = (message.content or "").strip()
        if not content:
            return message
        self._nudged = True
        return message.model_copy(
            update={"content": f"{message.content}\n\n{prompts.NUDGE}"}
        )

    # ── task end ─────────────────────────────────────────────────────────────
    def on_simulation_end(self, simulation) -> None:
        """Reflect into memory, then close the episode. Best-effort throughout.

        Called by the harness after evaluation, so the ground-truth verdict is
        available. Every failure is swallowed: a write-back problem must never
        change a recorded result.
        """
        if self._closed:
            return
        self._closed = True
        try:
            run_reflection(
                self.client,
                self.episode,
                simulation,
                task=self.task,
                route=self.route,
                supervision=self.supervision,
                read_only=self.read_only,
                llm_call=default_llm_call(self.reflection_llm, self.reflection_llm_args),
                run_tag=self.run_tag,
            )
        except Exception as exc:  # noqa: BLE001
            telemetry.record_call(
                tool="reflection", args_digest={"error": type(exc).__name__},
                ok=False, latency_ms=0.0, result_len=0,
            )
        finally:
            try:
                self.client.episode_end()
            finally:
                clear_episode()


# ── factory ──────────────────────────────────────────────────────────────────
def create_pl_agent(tools, domain_policy, **kwargs) -> PLMemoryAgent:
    """Build the agent for one simulation, minting its memory episode.

    The harness calls this once per (task, trial) on that simulation's worker
    thread, which is what makes the thread-local episode binding correct: the
    environment hook, the conversation, and the reflection all run on it.
    """
    route = _choice("PL_ROUTE", VALID_ROUTES, "entries")
    supervision = _choice("PL_SUPERVISION", VALID_SUPERVISION, "experience")
    dream = _choice("PL_DREAM", VALID_DREAM, "off")
    read_only = _flag("PL_READ_ONLY")
    run_tag = (os.environ.get("PL_RUN_TAG") or "").strip()
    url = (os.environ.get("PL_MCP_URL") or DEFAULT_URL).strip().rstrip("/")
    token = os.environ.get("PL_MCP_TOKEN") or None
    window = _float("PL_USE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)

    task = kwargs.get("task")
    context = get_context()
    task_id = getattr(task, "id", None) or context.get("task_id")
    trial = context.get("trial")

    episode = new_episode(
        task_id=task_id, trial=trial, run=run_tag or None, window_seconds=window
    )
    client = PLMemoryClient(url=url, session_uid=episode.session_uid, token=token,
                            read_only=read_only)
    episode.client = client
    bind_episode(episode)
    client.episode_start(_episode_title(run_tag, task_id, trial))

    llm = kwargs.get("llm")
    llm_args = kwargs.get("llm_args")
    reflection_llm = (os.environ.get("PL_REFLECTION_LLM") or "").strip() or llm
    reflection_llm_args = _json_env("PL_REFLECTION_LLM_ARGS")

    print(
        f"[pl_memory] route={route} supervision={supervision} dream={dream} "
        f"read_only={read_only} run={run_tag or '-'} task={task_id} "
        f"episode={episode.session_uid} url={url}",
        flush=True,
    )

    return PLMemoryAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=llm,
        llm_args=llm_args,
        episode=episode,
        client=client,
        route=route,
        supervision=supervision,
        read_only=read_only,
        dream=dream,
        run_tag=run_tag,
        task=task,
        reflection_llm=reflection_llm,
        reflection_llm_args=reflection_llm_args,
    )


def _episode_title(run_tag: str, task_id, trial) -> str:
    parts = ["taubench"]
    if run_tag:
        parts.append(run_tag)
    if task_id is not None:
        parts.append(f"task {task_id}")
    if trial is not None:
        parts.append(f"trial {trial}")
    return " / ".join(parts)


def _choice(name: str, allowed: tuple, default: str) -> str:
    value = (os.environ.get(name) or "").strip().lower() or default
    if value not in allowed:
        print(f"[pl_memory] unknown {name}={value!r}; using {default!r}", flush=True)
        return default
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _json_env(name: str) -> Optional[dict]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print(f"[pl_memory] {name} is not valid JSON; ignoring it", flush=True)
        return None
    return parsed if isinstance(parsed, dict) else None
