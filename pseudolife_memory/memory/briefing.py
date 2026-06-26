"""Session-start briefing assembly — pure selection + markdown formatting.

No torch, no daemon: takes already-fetched digest/lessons data and produces the
injected markdown block. Kept out of the service so it's unit-testable. ASCII
only — the block is printed to a possibly-cp1252 console by the hook.
"""
from __future__ import annotations

_AVOID_OUTCOMES = {"failure", "correction"}


def _is_avoid(e: dict) -> bool:
    return e.get("polarity") == "-" or e.get("outcome") in _AVOID_OUTCOMES


def select_lessons(entries: list[dict], max_lessons: int) -> list[dict]:
    """Avoid-first (the 'do not repeat this' signal), then the rest in input
    order, capped at ``max_lessons``."""
    avoid, rest = [], []
    for e in entries:
        (avoid if _is_avoid(e) else rest).append(e)
    return (avoid + rest)[:max_lessons]


def _fmt_surprise(s: dict) -> str:
    src, dst = s.get("src", "?"), s.get("dst", "?")
    rel = s.get("relation") or "related-to"
    why = (s.get("why") or "").strip()
    tail = f" -- {why}" if why else ""
    return f"- `{src}` {rel} `{dst}`{tail}"


def _fmt_question(q: dict) -> str:
    text = (q.get("question") or "").strip()
    return f"- {text}" if text else ""


def _fmt_lesson(e: dict) -> str:
    marker = "avoid" if _is_avoid(e) else "prefer"
    text = (e.get("lesson") or "").strip()
    return f"- {marker}: {text}" if text else ""


def format_briefing(surprises: list[dict], questions: list[dict],
                    lessons: list[dict]) -> str:
    """Render the markdown block; empty string when there is nothing to say."""
    parts: list[str] = []
    unsure = [_fmt_surprise(s) for s in surprises]
    unsure += [_fmt_question(q) for q in questions]
    unsure = [ln for ln in unsure if ln]
    if unsure:
        parts.append("## What your memory is unsure about\n" + "\n".join(unsure))
    lesson_lines = [ln for ln in (_fmt_lesson(e) for e in lessons) if ln]
    if lesson_lines:
        parts.append("## Lessons from past work\n" + "\n".join(lesson_lines))
    return "\n\n".join(parts)
