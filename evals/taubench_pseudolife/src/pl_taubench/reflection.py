"""Task-end reflection: turn one graded episode into memory.

Two write routes, chosen by ``PL_ROUTE``:

``entries``
    Each rule is saved as a standalone note (``memory_store``), then ONE
    ``memory_outcome`` credits the searches that served this episode.

``lessons``
    No note is stored. One ``memory_outcome`` carries the same detail block with
    ``about`` prefixed ``"rule: "``, which routes the signal to the daemon's
    rule-mode synthesis.

Either way exactly one outcome is sent per episode, because ``used_ids`` credit
is per-signal: splitting it across several signals would divide the credit for
one retrieval among rules that did not each depend on it.

Supervision (``PL_SUPERVISION``) decides what the reflection is allowed to see on
a failure: ``experience`` gets only the correct/incorrect bit and may write only
a prohibition; ``instruction`` is additionally handed the verified sequence and
its difference from what happened. The verified sequence is read ONLY here, after
grading — never during the conversation.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from . import prompts, telemetry
from .reflection_diff import (
    diff_actions,
    executed_actions,
    gold_actions,
    rule_required_strings,
)
from .reflection_diff import _is_read_only as _is_read_only_action
from .session import MAX_USED_IDS, Episode

__all__ = [
    "verdict_from",
    "gold_actions",
    "executed_actions",
    "format_actions_taken",
    "format_gold_text",
    "format_action_diff",
    "render_transcript",
    "build_reflection_prompt",
    "parse_reflection_json",
    "build_detail",
    "run_reflection",
]

#: Reward is a float; the harness treats 1.0 as solved. Compare with a tolerance
#: rather than ``== 1.0`` so a float round-trip through JSON cannot flip it.
SUCCESS_THRESHOLD = 0.999

MAX_TRANSCRIPT_CHARS = 16000
MAX_RULES = 3


# ── verdict ──────────────────────────────────────────────────────────────────
def verdict_from(simulation: Any) -> dict:
    """The ground-truth correct/incorrect bit, and nothing more.

    Never carries the expected action: under ``experience`` that is the whole
    point, and under ``instruction`` the verified sequence is revealed
    separately so the two paths cannot drift apart.
    """
    reward_info = getattr(simulation, "reward_info", None)
    reward = getattr(reward_info, "reward", None) if reward_info is not None else None
    if reward is None:
        return {"success": False, "reward": None, "reason": prompts.VERDICT_UNKNOWN}
    success = float(reward) >= SUCCESS_THRESHOLD
    return {
        "success": success,
        "reward": float(reward),
        "reason": prompts.VERDICT_SUCCESS if success else prompts.VERDICT_FAILURE,
    }


# ── rendering (all generic: no scenario vocabulary) ──────────────────────────
def _render_call(action: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (action.get("arguments") or {}).items())
    return f"{action.get('name')}({args})"


def format_actions_taken(messages: list) -> str:
    """The consequential actions actually taken, in order.

    Read-only calls are left out: they cannot change the graded state, so they
    are never the cause of an outcome. Listing this grounds a success rule in
    the decisive action rather than the last one to happen.
    """
    lines: list[str] = []
    for action in executed_actions(messages):
        if _is_read_only_action(action):
            continue
        who = "user" if action.get("requestor") == "user" else "assistant"
        lines.append(f"{who}: {_render_call(action)}")
    return "\n".join(lines)


def format_gold_text(gold: list) -> str:
    return "\n".join(f"{i + 1}. {_render_call(a)}" for i, a in enumerate(gold or []))


def format_action_diff(diff: dict) -> str:
    """Render the gold-vs-executed diff in neutral wording.

    The vendored module computes the diff; the wording is ours and lives in
    ``prompts`` — these lines reach the model, so they are prompt surface and
    belong where the guards can see them.
    """
    lines: list[str] = []
    for item in diff.get("missing") or []:
        call = _render_call(item["action"])
        if item.get("required", 0) > 1 or item.get("executed"):
            call += prompts.DIFF_MISSING_COUNT.format(
                required=item["required"], executed=item["executed"])
        lines.append(prompts.DIFF_MISSING.format(call=call))
    for item in diff.get("wrong_args") or []:
        name = item["action"].get("name")
        for key, expected, actual in item.get("mismatches") or []:
            shown = key.split(".", 1)[1] if "." in key else key
            lines.append(prompts.DIFF_WRONG_VALUE.format(
                name=name, field=shown, actual=actual, expected=expected))
    for item in diff.get("extra") or []:
        call = _render_call(item["action"])
        if item.get("count", 1) > 1:
            call += prompts.DIFF_EXTRA_COUNT.format(count=item["count"])
        lines.append(prompts.DIFF_EXTRA.format(call=call))
    if not lines:
        lines.append(prompts.DIFF_NOTHING_TO_FIX)
    return "\n".join(lines)


def render_transcript(messages: list, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    lines: list[str] = []
    for message in messages or []:
        role = _attr(message, "role") or ""
        content = (_attr(message, "content") or "").strip()
        if role == "system":
            continue
        if role == "user" and content:
            lines.append(f"User: {content}")
        elif role == "assistant":
            if content:
                lines.append(f"Assistant: {content}")
            for call in _attr(message, "tool_calls") or []:
                lines.append(
                    f"Assistant action: {_attr(call, 'name')}({_attr(call, 'arguments')})"
                )
        elif role == "tool" and content:
            lines.append(f"Result: {content[:2000]}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        # Keep the tail: the resolution decides the outcome, the greeting does not.
        text = "...\n" + text[-max_chars:]
    return text


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ── prompt ───────────────────────────────────────────────────────────────────
def build_reflection_prompt(
    *,
    route: str,
    supervision: str,
    verdict: dict,
    transcript_text: str,
    diff_text: str,
    gold_text: str,
) -> str:
    """Assemble the one reflection prompt.

    The verified sequence and the diff are included ONLY on a supervised
    failure; on an unsupervised failure they are withheld and the prompt
    explicitly forbids inventing a right answer in their place.
    """
    success = bool(verdict.get("success"))
    supervised_failure = (not success) and supervision == "instruction"

    sections: list[str] = [prompts.REFLECTION_INTRO, verdict.get("reason") or prompts.VERDICT_UNKNOWN]

    if transcript_text:
        sections.append(f"{prompts.HEADER_TRANSCRIPT}\n{transcript_text}")

    if supervised_failure:
        if gold_text:
            sections.append(f"{prompts.HEADER_GOLD}\n{gold_text}")
        if diff_text:
            sections.append(f"{prompts.HEADER_DIFF}\n{diff_text}")
        sections.append(prompts.SUPERVISED_CLAUSE)
    elif not success:
        sections.append(prompts.NO_INVENTION_CLAUSE)

    form = prompts.FORM_NEGATIVE if (not success and supervision != "instruction") else prompts.FORM_POSITIVE
    sections.append(prompts.ROUTE_CLAUSE.get(route, prompts.ROUTE_CLAUSE["entries"]))
    sections.append(prompts.RULE_CONTRACT.format(form=form))
    return "\n\n".join(s for s in sections if s)


# ── JSON parsing ─────────────────────────────────────────────────────────────
def parse_reflection_json(raw: str) -> Optional[dict]:
    """Parse the reply, tolerating a code fence or surrounding prose."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        braced = re.search(r"\{.*\}", text, re.DOTALL)
        if braced:
            text = braced.group(0)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rules_from(parsed: Optional[dict]) -> list[dict]:
    rules = (parsed or {}).get("rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict) and str(r.get("rule") or "").strip()][:MAX_RULES]


def _fallback_rule(raw: str, episode: Episode) -> dict:
    """One rule salvaged from an unparseable reply.

    Losing the reflection entirely costs the episode's whole signal, so the raw
    text is kept as the rule and the outcome is still sent — a weak rule that is
    credited beats no record of what happened.
    """
    text = " ".join((raw or "").split())[:600] or "no reflection text was produced"
    first_line = next((ln.strip() for ln in (raw or "").splitlines() if ln.strip()), "")
    return {
        "situation": (first_line[:120] or f"task {episode.task_id}"),
        "rule": text,
        "polarity": "-",
        "about": f"task {episode.task_id}",
        "must_include": [],
    }


# ── detail block ─────────────────────────────────────────────────────────────
def build_detail(
    *,
    situation: str,
    verdict_text: str,
    actions_text: str,
    gold_text: str,
    diff_text: str,
    must_include: list,
) -> str:
    """The ``memory_outcome`` detail block.

    Fixed section order so a later reader (or the lesson synthesiser) can find
    each part without parsing prose. Empty sections are omitted rather than
    written blank, which would train the synthesiser on empty fields.
    """
    parts = [f"SITUATION: {situation}", f"VERDICT: {verdict_text}"]
    if actions_text:
        parts.append(f"ACTIONS TAKEN: {actions_text}")
    if gold_text:
        parts.append(f"CORRECT SOLUTION: {gold_text}")
    if diff_text:
        parts.append(f"ACTION DIFF: {diff_text}")
    if must_include:
        parts.append("MUST INCLUDE: " + "; ".join(str(m) for m in must_include))
    return "\n".join(parts)


# ── the LLM call ─────────────────────────────────────────────────────────────
def default_llm_call(llm: str, llm_args: Optional[dict] = None) -> Callable[[str], str]:
    """A plain text-in/text-out completion.

    litellm directly, not the harness's ``generate``: ``generate`` returns an
    assistant message wired for tool calling, and the reflection must never be
    able to emit a tool call.
    """

    def _call(prompt: str) -> str:
        import litellm  # imported lazily: only the reflection needs it

        response = litellm.completion(
            model=llm,
            messages=[{"role": "user", "content": prompt}],
            **(llm_args or {}),
        )
        return (response.choices[0].message.content or "").strip()

    return _call


# ── the reflection ───────────────────────────────────────────────────────────
def run_reflection(
    client: Any,
    episode: Episode,
    simulation: Any,
    task: Any = None,
    *,
    route: str = "entries",
    supervision: str = "experience",
    read_only: bool = False,
    llm_call: Optional[Callable[[str], str]] = None,
    llm: Optional[str] = None,
    llm_args: Optional[dict] = None,
    run_tag: str = "",
    now: Optional[float] = None,
) -> dict:
    """Reflect on one graded episode and write it back. Returns a summary dict."""
    verdict = verdict_from(simulation)
    messages = _attr(simulation, "messages") or []

    if read_only:
        # The store is frozen: no writes, and no reflection call to pay for
        # either, since its output would be discarded.
        window_ok = episode.window_ok(now)
        telemetry.record_episode(
            episode, verdict=verdict, window_ok=window_ok, outcome_result=None,
            extra={"route": route, "supervision": supervision, "read_only": True},
        )
        return {
            "read_only": True, "parsed": None, "window_ok": window_ok,
            "verdict": verdict, "rules": [], "outcome_result": None,
        }

    supervised_failure = (not verdict["success"]) and supervision == "instruction"
    gold_text = ""
    diff_text = ""
    required: list[str] = []
    if supervised_failure and task is not None:
        try:
            gold = gold_actions(task)
            if gold:
                diff = diff_actions(gold, executed_actions(messages))
                gold_text = format_gold_text(gold)
                diff_text = format_action_diff(diff)
                required = rule_required_strings(diff) or []
        except Exception:  # noqa: BLE001 - a diff failure must not cost the write-back
            gold_text = gold_text or ""
            diff_text = diff_text or ""

    prompt = build_reflection_prompt(
        route=route, supervision=supervision, verdict=verdict,
        transcript_text=render_transcript(messages),
        diff_text=diff_text, gold_text=gold_text,
    )

    if llm_call is None:
        llm_call = default_llm_call(llm or "", llm_args)

    raw = _safe_call(llm_call, prompt)
    parsed = parse_reflection_json(raw)
    rules = _rules_from(parsed)
    parsed_ok = bool(rules)
    if not parsed_ok:
        # One retry, then keep whatever text came back rather than lose the signal.
        raw = _safe_call(llm_call, prompt + prompts.JSON_RETRY_SUFFIX)
        parsed = parse_reflection_json(raw)
        rules = _rules_from(parsed)
        parsed_ok = bool(rules)
    if not rules:
        rules = [_fallback_rule(raw, episode)]

    head = rules[0]
    must_include = _merge_required(required, head.get("must_include"))

    store_results = []
    if route == "entries":
        for rule in rules:
            store_results.append(
                _safe_tool(
                    client, "memory_store",
                    {
                        "text": _rule_text(rule),
                        "source": "taubench",
                        "tags": _rule_tags(run_tag, episode.task_id),
                        "distortion_tolerance": "constraint",
                        "episode": episode.session_uid,
                    },
                )
            )

    window_ok = episode.window_ok(now)
    about = str(head.get("about") or "").strip() or f"task {episode.task_id}"
    outcome_result = _safe_tool(
        client, "memory_outcome",
        {
            "task": str(head.get("situation") or "").strip() or about,
            "outcome": _outcome_label(verdict["success"], supervision),
            "about": f"rule: {about}" if route == "lessons" else about,
            "detail": build_detail(
                situation=str(head.get("situation") or "").strip() or about,
                verdict_text=verdict["reason"],
                actions_text=format_actions_taken(messages),
                gold_text=gold_text,
                diff_text=diff_text,
                must_include=must_include,
            ),
            "used_ids": episode.used_ids(MAX_USED_IDS),
            "episode": episode.session_uid,
        },
    )

    telemetry.record_episode(
        episode, verdict=verdict, window_ok=window_ok, outcome_result=outcome_result,
        extra={
            "route": route, "supervision": supervision, "read_only": False,
            "parsed": parsed_ok, "n_rules": len(rules), "n_stored": len(store_results),
        },
    )
    return {
        "read_only": False,
        "parsed": parsed_ok,
        "window_ok": window_ok,
        "verdict": verdict,
        "rules": rules,
        "store_results": store_results,
        "outcome_result": outcome_result,
    }


def _outcome_label(success: bool, supervision: str) -> str:
    if success:
        return "success"
    # A supervised failure carries the right answer alongside the wrong one, so
    # it is a correction, not a bare failure.
    return "correction" if supervision == "instruction" else "failure"


def _rule_text(rule: dict) -> str:
    text = str(rule.get("rule") or "").strip()
    return text if text.startswith("RULE: ") else f"RULE: {text}"


def _rule_tags(run_tag: str, task_id: Optional[str]) -> list[str]:
    return ["taubench", "rule", f"run:{run_tag}", f"task:{task_id}"]


def _merge_required(from_diff: list, from_rule: Any) -> list[str]:
    """Diff-derived requirements first: they are measured, the rule's are claimed."""
    out: list[str] = []
    for source in (from_diff or [], from_rule if isinstance(from_rule, list) else []):
        for item in source:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def _safe_call(llm_call: Callable[[str], str], prompt: str) -> str:
    try:
        return llm_call(prompt) or ""
    except Exception:  # noqa: BLE001 - a reflection failure must not break a run
        return ""


def _safe_tool(client: Any, name: str, args: dict) -> dict:
    start = time.perf_counter()
    try:
        return client.call_tool(name, args) or {}
    except Exception as exc:  # noqa: BLE001
        telemetry.record_call(
            tool=name, args_digest={"error": type(exc).__name__}, ok=False,
            latency_ms=(time.perf_counter() - start) * 1000.0, result_len=0,
        )
        return {"error": str(exc)}
