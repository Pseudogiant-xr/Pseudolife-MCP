"""Every piece of prompt text the plugin emits.

Deliberately domain-neutral: the plugin runs against whichever scenario set the
operator points it at, so nothing here may name a scenario, a role, a tool of
the graded environment, or the harness itself. A guard test asserts that.

Keeping all prompt strings in one module (rather than beside the code that uses
them) is what makes that guard cheap to enforce and hard to drift.
"""

from __future__ import annotations

# ── read-time nudge ──────────────────────────────────────────────────────────
# Appended once, after the first user turn, so the assistant reads the actual
# request first and the reminder lands as a trailing instruction rather than
# framing the request. One nudge, not one per turn: repeated nudging fragments
# the assistant's execution.
NUDGE = (
    "Before acting on this, consult your own experience from earlier work:\n"
    "- call `memory_search` for what happened in situations like this one;\n"
    "- call `memory_lesson_search` for the do/avoid guidance you already earned.\n"
    "Prefer what your own record says over a guess, and say nothing about having "
    "consulted it. If the request changes shape later in this conversation, "
    "search again for the new situation before you act on it."
)

# ── mid-conversation write attempts ──────────────────────────────────────────
# Writes are never taken mid-conversation: at that point the outcome is unknown,
# so anything saved is a guess that later reads would treat as experience.
DEFER_MESSAGE = (
    "Noted. Nothing is saved to memory during a conversation — what was learned "
    "is recorded once the work is finished and its outcome is known. Continue "
    "with the request."
)

# ── reflection ───────────────────────────────────────────────────────────────
REFLECTION_INTRO = (
    "The work below is finished and has been graded. Write down what a future "
    "attempt at the same kind of request should do, as reusable rules."
)

VERDICT_SUCCESS = "OUTCOME (ground truth): the desired result WAS achieved."
VERDICT_FAILURE = "OUTCOME (ground truth): the desired result was NOT achieved."
VERDICT_UNKNOWN = "OUTCOME: unavailable."

# Success and the supervised-failure path both know the right move, so the rule
# states it. The unsupervised-failure path does not, so its rule may only
# prohibit what was actually done.
FORM_POSITIVE = "`WHEN <situation> THEN <exact action>`."
FORM_NEGATIVE = "`WHEN <situation> do NOT <action taken>`."

NO_INVENTION_CLAUSE = (
    "You have NOT been shown what the right move was, and you must not invent "
    "one. Do NOT write a rule that asserts a correct action, a correct value, or "
    "a correct order of steps — you do not know them. Write only what to avoid, "
    "grounded in what actually happened above."
)

SUPERVISED_CLAUSE = (
    "The verified sequence and its difference from what happened are given "
    "below. Ground the rule in them: name the exact action, value, and order "
    "that the verified sequence uses."
)

ROUTE_CLAUSE = {
    # Rules are saved verbatim and retrieved later as text, so they must stand
    # alone without this conversation.
    "entries": (
        "Each rule is saved as a standalone note and retrieved on its own later, "
        "so it must make sense with none of the context above in view."
    ),
    # Rules are distilled into do/avoid guidance rather than stored verbatim,
    # so the situation label carries the matching weight.
    "lessons": (
        "Each rule is distilled into short guidance rather than kept verbatim, "
        "so put the matching power in `situation` and keep `rule` to one "
        "sentence."
    ),
}

RULE_CONTRACT = """\
Reply with JSON and nothing else, in exactly this shape:

{{"rules": [{{"situation": "...", "rule": "...", "polarity": "+", "about": "...", "must_include": ["..."]}}]}}

- The FIRST rule is required and must carry the whole lesson. Add at most two
  more, and only if they are independently useful.
- The first rule's `rule` must be one sentence in the form {form}
- `situation` names the trigger in words a future attempt can recognise, without
  referring to this episode ("no", "again", "as above" all fail this).
- `polarity` is "+" for something to do and "-" for something to avoid.
- `about` is a short subject label, two to five words.
- `must_include` lists the exact strings the rule text must carry verbatim —
  names, values, identifiers that decide the outcome. An example is not a
  directive: if a value decides the outcome, state it, do not illustrate it.
  Leave the list empty only when no specific value is decisive."""

# Section headers for the evidence blocks. Kept here so the whole prompt surface
# lives in one file.
HEADER_TRANSCRIPT = "WHAT HAPPENED:"
HEADER_GOLD = "VERIFIED SEQUENCE (the steps that solve this correctly):"
HEADER_DIFF = "DIFFERENCE between the verified sequence and what happened:"

# ── the difference block's own wording ───────────────────────────────────────
# The comparison itself is computed elsewhere; these lines are what the model
# reads, so they are prompt surface and live here with the rest of it. Every
# label is generic on purpose — it must fit any scenario set.
DIFF_MISSING = "- NEVER DONE: {call}"
DIFF_MISSING_COUNT = "  [needed {required}x, done {executed}x]"
DIFF_WRONG_VALUE = ("- WRONG VALUE: {name}: {field} was {actual!r}, should be "
                    "{expected!r}")
DIFF_EXTRA = "- NOT PART OF THE SOLUTION but done anyway: {call}"
DIFF_EXTRA_COUNT = "  [{count}x]"
DIFF_NOTHING_TO_FIX = (
    "- Every needed step was done with matching values and nothing extra "
    "was done. The failure came from the exchange itself: timing, order, "
    "or how the person was guided."
)

# ── retry ────────────────────────────────────────────────────────────────────
JSON_RETRY_SUFFIX = (
    "\n\nYour previous reply could not be parsed. Reply with the JSON object "
    "only — no prose before or after it, no code fence."
)
