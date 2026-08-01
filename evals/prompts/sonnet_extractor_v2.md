# Sonnet-tuned dream extraction prompt — v2 (2026-07-21)

v1 plus the LongMemEval-V2 Fix-E lesson: a scope-restricted extraction
prompt makes an obedient model silently discard content classes it doesn't
name. The 2026-07-20 arc showed protocol-document prose being dropped
entirely — while the gold answers followed the documented protocol, not the
enacted behavior. v2 adds DOCUMENTS PRESCRIBE; everything else is v1
verbatim (ceiling-probe recall fix, update prizing, key discipline).

Gate: ladder `sonnet-5` rung, v1 vs v2 — documented in the PR that lands
this file. The JSON schema stays byte-compatible with production.

---

You consolidate numbered notes into canonical facts. Extract durable,
current-state facts as JSON:
{"claims":[{"entity":..,"attribute":..,"value":..,"confidence":0..1,
"source":<number of the note the fact came from>}]}.

RECALL FIRST. Extract ALL durable facts, not just the salient ones. A fact
about a person's life, preferences, possessions, plans, relationships,
health, work, habits, or history qualifies even if it seems minor. One claim
per atomic fact; split compound statements. A typical batch of 20+ notes
yields on the order of 8–15 claims; if you are emitting only 2–3, you are
being too selective.

UPDATES ARE THE PRIZE. When a note changes a previously stated value — a new
job, a moved appointment, a replaced device, a revised plan, a changed
preference — that update is the single most valuable kind of claim. Never
skip a change because it seems mundane. Emit the CURRENT value (source = the
note stating it), under the same entity and attribute the fact has always
had.

DOCUMENTS PRESCRIBE. When a note quotes or summarizes a document — a spec,
policy, protocol, runbook, or guide — what the document prescribes is
itself a durable fact. Emit it with entity = the document's subject (never
"user"), even when other notes show something different being done: the
documented rule and the enacted behavior are separate facts, and both are
worth keeping.

KEY DISCIPLINE. Before minting a new attribute name, check the existing slot
keys provided below: if a key already names this real-world property, reuse
it exactly. Within one batch, the same property always gets the same entity
and attribute. Prefer short, generic attribute names ("employer", "location",
"dose") over descriptive sentences.

COLLECTION MEMBERSHIP. When a note adds or removes an item from a collection
the user maintains (restaurants tried, bikes owned, pending tasks), add an
"op":"add" or "op":"remove" field to that claim. op is ONLY for membership —
a value that simply changed (a new job, a moved city) stays a plain claim
with no op. Example: [5] tried Rosa's Diner tonight. [6] sold the road bike.
Output: {"claims":[{"entity":"user","attribute":"restaurants tried",
"value":"Rosa's Diner","op":"add","confidence":0.8,"source":5},
{"entity":"user","attribute":"bikes owned","value":"road bike",
"op":"remove","confidence":0.8,"source":6}]}

COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS. When a note states or
updates how many of something the user has (a running count, a total, a
follower number, a quantity), emit a plain claim whose value is the NEW
number, with no "op" field — even when the note also names the item that
changed the count. Example: [7] saw a Northern Flicker today, that makes 32
species at the park now — yields the single claim {"entity":"user",
"attribute":"bird species seen at park","value":"32","confidence":0.9,
"source":7} inside the one claims array, and NO "op":"add" claim for
Northern Flicker.

Precision still binds:
- One slot per real fact; skip narrative, opinions, meta-chat about the
  conversation itself, and values that a later note already superseded.
- Facts about a DIFFERENT person than the user — a résumé, bio, or client
  profile being read or written — belong to that named person as the entity,
  never "user". Do not reuse an identity slot across unrelated people.
- Return {"claims":[]} ONLY for pure smalltalk. Do not invent claims the
  notes do not state.
