# Vendored from memcoai/spark-continual-learning-paper-data (MIT), unmodified
# except this header.
"""Gold-vs-trajectory action diff for the task-end reflection (instruction).

Motivation (an internal mechanisms analysis): handed only the gold action *list*, the reflection often
cannot locate the decisive divergence and writes lessons on the wrong axis —
a task's failures were caused by an EXTRA non-gold action while every gold action
was executed; a task's by a wrong ``time_verified`` argument; a task's by a wrong
``account_class``. The harness holds both action lists, so it can compute the diff
deterministically and hand the reflection the one signal it structurally lacks:

- **missing**  — gold actions never executed (with required counts, e.g. "8x")
- **wrong-args** — gold actions executed with different argument values
- **extra**    — consequential non-gold actions that were executed (a likely failure
  cause even when everything required was done)

Everything here is pure and tolerant of BOTH tau2 pydantic objects and the plain
dicts found in ``results.json`` — so it can be tested offline against recorded runs.
Used only under instruction on failures (the only time gold is revealed); the
module itself never touches the evaluator or the reward.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Tools whose execution cannot change graded state; never reported as EXTRA.
# (Memory tools bypass the env DB entirely; the rest are lookups.)
_READ_ONLY_PREFIXES = (
    "kb_search",
    "memory_",
    "get_",
    "list_",
    "find_",
    "search_",
    "lookup_",
    "calculate",
    "think",
)

# Wrapper tools whose consequence depends on the WRAPPED tool: classify by the
# inner tool name carried in this argument. (unlock/give are NOT here — granting
# a tool is consequential in itself: a task failed on an extra give of a
# read-only tool, and grants can enter the graded env state.)
_WRAPPER_TOOL_ARG = {
    "call_discoverable_agent_tool": "agent_tool_name",
    "call_discoverable_user_tool": "discoverable_tool_name",
}

# Tools whose MATCH IDENTITY includes the wrapped tool name: two calls of the
# same wrapper around different inner tools are different actions and must never
# claim each other during gold-vs-executed matching (a call of the pay tool is
# not a partial match for a call of the dispute-history tool just because both
# carry the same user_id).
_INNER_NAME_ARG = {
    "call_discoverable_agent_tool": "agent_tool_name",
    "call_discoverable_user_tool": "discoverable_tool_name",
    "unlock_discoverable_agent_tool": "agent_tool_name",
    "give_discoverable_user_tool": "discoverable_tool_name",
}


def _match_key(action: Dict[str, Any]) -> str:
    """Identity used to pair gold with executed actions (unwraps wrapper tools)."""
    name = action.get("name") or ""
    inner_arg = _INNER_NAME_ARG.get(name.lower())
    if inner_arg:
        inner = (action.get("arguments") or {}).get(inner_arg)
        if isinstance(inner, str) and inner:
            return f"{name}[{inner}]"
    return name


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_args(arguments: Any) -> Dict[str, Any]:
    """Arguments as a dict; JSON-string args (discoverable-tool calls) are parsed."""
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {"_raw": arguments}
        except (json.JSONDecodeError, ValueError):
            return {"_raw": arguments}
    if isinstance(arguments, dict):
        return dict(arguments)
    return {}


def _norm_value(value: Any) -> Any:
    """Normalize a single argument value for comparison (case/space/number-insensitive)."""
    if isinstance(value, str):
        s = value.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, (dict, list)):
                return _norm_structure(parsed)
            value = parsed  # number / bool / plain string
        except (json.JSONDecodeError, ValueError):
            pass
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return " ".join(value.lower().split())
    if isinstance(value, (dict, list)):
        return _norm_structure(value)
    return value


def _norm_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _norm_value(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm_value(v) for v in value]
    return _norm_value(value)


def _is_wildcard(value: Any) -> bool:
    """Gold argument values that assert nothing (empty sparkry etc.) match anything."""
    return value is None or (isinstance(value, str) and not value.strip())


def action_record(action: Any, requestor: str = "assistant") -> Dict[str, Any]:
    """Normalize a gold action or an executed tool call to {name, arguments, requestor}."""
    return {
        "name": _get(action, "name"),
        "arguments": _parse_args(_get(action, "arguments")),
        "requestor": _get(action, "requestor", None) or requestor,
    }


def gold_actions(task: Any) -> List[Dict[str, Any]]:
    """The task's gold action list (``evaluation_criteria.actions``), normalized."""
    ec = _get(task, "evaluation_criteria")
    actions = _get(ec, "actions") if ec is not None else None
    return [action_record(a) for a in (actions or [])]


def executed_actions(messages: List[Any]) -> List[Dict[str, Any]]:
    """Every tool call actually made in the conversation (agent + customer).

    Memory tools are excluded: they bypass the env DB and are never graded.
    """
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        role = _get(m, "role")
        if role not in ("assistant", "user"):
            continue
        for tc in _get(m, "tool_calls") or []:
            name = _get(tc, "name") or ""
            if name.startswith("memory_"):
                continue
            out.append(action_record(tc, requestor=role if role == "user" else "assistant"))
    return out


def _effective_name(action: Dict[str, Any]) -> str:
    """The consequence-bearing tool name (unwraps discoverable-tool calls)."""
    name = (action.get("name") or "").lower()
    inner_arg = _WRAPPER_TOOL_ARG.get(name)
    if inner_arg:
        inner = action["arguments"].get(inner_arg)
        if isinstance(inner, str) and inner:
            return inner.lower()
    return name


def _is_read_only(action: Dict[str, Any]) -> bool:
    return _effective_name(action).startswith(_READ_ONLY_PREFIXES)


def _maybe_dict(value: Any) -> Optional[Dict[str, Any]]:
    """The value as a dict if it is one (or a JSON-object string), else None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _flatten_asserted(arguments: Dict[str, Any], prefix: str = "") -> List[Tuple[str, Any]]:
    """Flatten gold-asserted arguments to (dotted_path, value), descending into
    nested dicts / JSON-object strings (discoverable-tool ``arguments`` payloads).

    Wildcard values (empty/None — e.g. gold ``sparkry=''``) assert nothing and are
    dropped, so they can never produce a mismatch.
    """
    out: List[Tuple[str, Any]] = []
    for key, value in (arguments or {}).items():
        if _is_wildcard(value):
            continue
        nested = _maybe_dict(value)
        if nested is not None:
            out.extend(_flatten_asserted(nested, prefix=f"{prefix}{key}."))
        else:
            out.append((f"{prefix}{key}", value))
    return out


def _lookup_path(arguments: Dict[str, Any], path: str) -> Any:
    """Fetch a dotted path from executed arguments, descending like _flatten_asserted."""
    current: Any = arguments
    for part in path.split("."):
        current = _maybe_dict(current) if not isinstance(current, dict) else current
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _arg_mismatches(
    gold: Dict[str, Any], executed: Dict[str, Any]
) -> List[Tuple[str, Any, Any]]:
    """(dotted_key, gold_value, actual_value) for every gold-asserted argument that
    differs. Only keys the gold asserts are compared (extra executed args are fine)."""
    mismatches: List[Tuple[str, Any, Any]] = []
    for path, gold_val in _flatten_asserted(gold.get("arguments") or {}):
        actual_val = _lookup_path(executed.get("arguments") or {}, path)
        if _norm_value(gold_val) != _norm_value(actual_val):
            mismatches.append((path, gold_val, actual_val))
    return mismatches


def _match_score(gold: Dict[str, Any], executed: Dict[str, Any]) -> float:
    """Fraction of gold-asserted argument paths the executed call matches
    (1.0 if the gold asserts nothing)."""
    asserted = _flatten_asserted(gold.get("arguments") or {})
    if not asserted:
        return 1.0
    hits = sum(
        1
        for path, value in asserted
        if _norm_value(value) == _norm_value(_lookup_path(executed.get("arguments") or {}, path))
    )
    return hits / len(asserted)


def diff_actions(
    gold: List[Dict[str, Any]], executed: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Compare the gold action list against the executed one.

    Matching is per tool name: each gold action greedily claims the unclaimed
    executed call (same name) whose arguments match best, so repeated calls
    (e.g. two ``open_bank_account_4821`` with different account classes, or 8x
    ``request_human_agent_transfer``) pair up sensibly.

    Returns {missing, wrong_args, extra, matched} where
      missing:    [{action, required, executed}]  (count-aware)
      wrong_args: [{action, mismatches: [(key, gold, actual)]}]
      extra:      [{action, count}]  (consequential non-gold executed actions)
    """
    unclaimed = list(range(len(executed)))
    missing: List[Dict[str, Any]] = []
    wrong_args: List[Dict[str, Any]] = []
    matched_idx: set = set()

 # Group gold actions by match identity to report required-vs-executed counts once.
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for g in gold:
        by_name.setdefault(_match_key(g), []).append(g)

    for key_name, gold_group in by_name.items():
        candidates = [i for i in unclaimed if _match_key(executed[i]) == key_name]
        n_executed = len(candidates)
        unmatched_gold = 0
        for g in gold_group:
            if not candidates:
                unmatched_gold += 1
                continue
            best = max(candidates, key=lambda i: _match_score(g, executed[i]))
 # A gold action that asserts arguments must share at least one of them
 # with the call it claims — otherwise an unrelated same-name call (e.g.
 # unlock of a non-gold tool) would masquerade as a wrong-args match
 # instead of surfacing as MISSING + EXTRA.
            if _match_score(g, executed[best]) == 0.0:
                unmatched_gold += 1
                continue
            candidates.remove(best)
            unclaimed.remove(best)
            matched_idx.add(best)
            mismatches = _arg_mismatches(g, executed[best])
            if mismatches:
                wrong_args.append({"action": g, "mismatches": mismatches})
        if unmatched_gold:
            missing.append(
                {
                    "action": gold_group[0],
                    "required": len(gold_group),
                    "executed": n_executed,
                }
            )

    extra_counts: Dict[str, Dict[str, Any]] = {}
    for i in unclaimed:
        act = executed[i]
        if _is_read_only(act):
            continue
        key = f"{act['name']}({json.dumps(act['arguments'], sort_keys=True, default=str)})"
        entry = extra_counts.setdefault(key, {"action": act, "count": 0})
        entry["count"] += 1

    return {
        "missing": missing,
        "wrong_args": wrong_args,
        "extra": list(extra_counts.values()),
        "matched": len(matched_idx),
    }


def _render_call(action: Dict[str, Any]) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (action.get("arguments") or {}).items())
    return f"{action['name']}({args})"


def format_action_diff(diff: Dict[str, Any]) -> str:
    """Render the diff as the reflection-prompt block (empty string if no gold)."""
    lines: List[str] = [
        "ACTION DIFF — the CORRECT solution compared against what actually happened:"
    ]
    if diff["missing"]:
        lines.append("- MISSING (required but never executed):")
        for m in diff["missing"]:
            call = _render_call(m["action"])
            if m["required"] > 1 or m["executed"]:
                call += f"  [required {m['required']}x, executed {m['executed']}x]"
            lines.append(f"  - {call}")
    if diff["wrong_args"]:
        lines.append("- WRONG ARGUMENTS (right action, wrong value):")
        for w in diff["wrong_args"]:
 # Report against the consequence-bearing tool (unwrap discoverable-tool
 # calls) and strip the wrapper's "arguments." prefix from the key —
 # "open_bank_account_4821: account_class was ..." reads directly.
            shown_name = _effective_name(w["action"]) or w["action"]["name"]
            for key, gold_val, actual_val in w["mismatches"]:
                shown_key = key.split(".", 1)[1] if "." in key else key
                lines.append(
                    f"  - {shown_name}: {shown_key} was {actual_val!r}, "
                    f"should be {gold_val!r}"
                )
    if diff["extra"]:
        lines.append(
            "- EXTRA (consequential actions taken that are NOT part of the correct "
            "solution — a likely cause of failure even when everything required was done):"
        )
        for e in diff["extra"]:
            call = _render_call(e["action"])
            if e["count"] > 1:
                call += f"  [{e['count']}x]"
            lines.append(f"  - {call}")
    if len(lines) == 1:
        lines.append(
            "- Every required action was executed with matching arguments, and no "
            "extra consequential action was taken. The failure came from the "
            "conversation itself (timing, ordering, or how the customer was guided)."
        )
    return "\n".join(lines)


def consequential_actions(messages: List[Any]) -> List[str]:
    """The consequential (non-read-only) actions actually taken, in order, rendered
    with requestor prefixes — the candidate causes of a graded outcome.

    Motivation (a success-reflection failure mode): the SUCCESS reflection banked "transfer to
    a specialized team" as the winning rule because the passing conversation happened
    to END in a transfer — the decisive actions (log_verification + the customer's
    submit_referral) went uncredited, and the mis-banked rule was anti-gold at the
    next trial. Listing the consequential actions grounds "bank the win" in the
    graded trajectory instead of the model's guess about what mattered.
    """
    out: List[str] = []
    for act in executed_actions(messages):
        if _is_read_only(act):
            continue
        prefix = "customer" if act.get("requestor") == "user" else "agent"
        out.append(f"{prefix}: {_render_call(act)}")
    return out


def gold_trajectory_diff(task: Any, messages: List[Any]) -> Optional[str]:
    """Convenience: the rendered diff block for a task + conversation, or None.

    None when the task carries no gold actions (nothing to diff against).
    """
    gold = gold_actions(task)
    if not gold:
        return None
    return format_action_diff(diff_actions(gold, executed_actions(messages)))


# ── write-time rule validation (the write-path analysis) ────────────────────────────
# Customer identifiers become placeholders in rules per the reflection contract,
# so their gold values are never REQUIRED verbatim. Everything else the diff
# flags (card types, account classes, timestamps, reasons, tool names) is
# decision-critical and must survive into the rule text — the the write-path analysis 0→0
# analysis found reflections systematically demoting exactly these to
# placeholders ("<current_time>") or examples ("e.g., 'Platinum Rewards Card'").
CUSTOMER_IDENTIFIER_KEYS = {
    "name", "customer_name", "user_id", "annual_income", "email", "new_email",
    "phone_number", "address", "date_of_birth", "sparkry",
}

_EXAMPLE_MARKER = re.compile(r"(?:e\.?g\.?|for example|such as)[^A-Za-z0-9]{0,8}$", re.I)


def rule_required_strings(diff: Dict[str, Any], cap: int = 12) -> List[str]:
    """The verbatim strings a diff-informed VERIFIED RULE must contain.

    From the structured diff: every WRONG-ARGUMENTS gold value (string-typed,
    non-customer-identifier keys), then the effective tool name of every EXTRA
    consequential action (the thing to prohibit), then of every MISSING action.
    Ordered by decisiveness; capped so 15-step chains don't produce unusable
    rejection messages.
    """
    required: List[str] = []

    def _add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in required:
            required.append(text)

    def _name_of(action: Dict[str, Any]) -> Optional[str]:
 # Prefer the wrapped tool's name (matching _match_key's identity): a rule
 # about an extra ``give_discoverable_user_tool(get_referral_link)`` must
 # name get_referral_link — the wrapper name alone prohibits nothing.
        inner_arg = _INNER_NAME_ARG.get(((action or {}).get("name") or "").lower())
        if inner_arg:
            inner = ((action or {}).get("arguments") or {}).get(inner_arg)
            if isinstance(inner, str) and inner:
                return inner
        return _effective_name(action) or (action or {}).get("name")

    for wrong in diff.get("wrong_args", []):
        for key, gold_val, _actual in wrong.get("mismatches", []):
            leaf = key.rsplit(".", 1)[-1]
            if leaf in CUSTOMER_IDENTIFIER_KEYS:
                continue
            if isinstance(gold_val, str):
                _add(gold_val)
    for extra in diff.get("extra", []):
        _add(_name_of(extra.get("action") or {}))
    for missing in diff.get("missing", []):
        _add(_name_of(missing.get("action") or {}))
    return required[:cap]


def rule_content_gaps(rule_text: str, required: List[str]) -> List[str]:
    """The required strings the rule text fails to carry as directives.

    A requirement is unmet when it is absent (case/whitespace-insensitive), or
    when every occurrence is prefixed by an example marker ("e.g.", "for
    example", "such as") — an example is not a directive (the one task's
    "(e.g., 'Platinum Rewards Card')" failure mode).
    """
    haystack = " ".join((rule_text or "").split())
    haystack_lower = haystack.lower()
    gaps: List[str] = []
    for req in required:
        needle = " ".join(req.split()).lower()
        if not needle:
            continue
        starts = [m.start() for m in re.finditer(re.escape(needle), haystack_lower)]
        if not starts:
            gaps.append(req)
            continue
        if all(_EXAMPLE_MARKER.search(haystack_lower[max(0, s - 24):s]) for s in starts):
            gaps.append(req)
    return gaps
