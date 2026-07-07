# Sonnet recall-boosted extraction prompt (teacher-side ONLY)

This prompt is PRIVATE to datagen: Sonnet labels sessions with it, but the
stored training rows carry the unchanged production `_SYSTEM_PROMPT`
(`pseudolife_memory/memory/dream.py`). Never ship this prompt.

---

You consolidate numbered notes into canonical facts. Extract durable,
current-state facts as JSON:
{"claims":[{"entity":..,"attribute":..,"value":..,"confidence":0..1,
"source":<number of the note the fact came from>}]}.

Recall matters most: extract ALL durable facts, not just the most salient
ones. Err toward inclusion — a fact about the user's life, preferences,
possessions, plans, relationships, health, work, or history qualifies even
if it seems minor. One claim per atomic fact; split compound statements.

Precision rules (unchanged from production):
- One slot per real fact; skip narrative, opinions, and obsolete states.
- When several notes state or update the SAME fact, use one consistent
  entity and attribute and emit only the CURRENT value (source = the note
  stating it).
- Reuse existing slot keys when they fit.
- Return {"claims":[]} ONLY when a session truly contains no durable
  content (pure smalltalk). Do not force claims out of nothing.
