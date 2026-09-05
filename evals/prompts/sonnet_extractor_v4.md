# Sonnet-tuned dream extraction prompt — v4 (2026-09-05)

v2 plus the assistant-facts blocks that shipped in
`pseudolife_memory/memory/dream.py` on 2026-09-05
(`_ASSISTANT_FACTS_INSTRUCTION`, `_ASSISTANT_SPEAKER_RULE`,
`_ASSISTANT_PROVENANCE_EXAMPLE`). Everything above them is v2 verbatim.

Generated — do not hand-edit. `PYTHONPATH=. python evals/gen_shim_prompt.py`
imports the three blocks from `dream.py`, so the shim path asks for exactly
what the daemon's own prompt asks for. `--system-prompt-file` REPLACES the
shipped prefix, which is why the daemon-side change alone does not reach an
install whose extractor is the shim.

v3 is a different, unadopted lineage (2026-08-02 coverage mandates); this
file stacks on v2, the variant the deployed config actually names.

Gate: ladder `opus-5` rung (the Max-plan CLI shim on its dedicated port,
same model the shim autostart serves), v2 vs v4 —
`evals/results/ladder-shimprompt-paired-verdict-threshold.json`. The JSON
schema stays byte-compatible with production apart from the `speaker`
field, which the parser already accepts and ignores when absent.

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

THE ASSISTANT'S OWN STATEMENTS ARE FACTS TOO: the notes are turns of a conversation, some of which the assistant produced. What the ASSISTANT asserted, described, recommended, or specified is extractable on exactly the same terms as what the user said — names, values, descriptions, specifications, and the choices it presented all qualify. Key each such claim to WHAT IT IS ABOUT: the entity is the thing described (the restaurant, the book, the tool, the setting), never "the assistant" and never "the conversation". A recommendation the assistant made is a durable fact about the thing recommended.
NAME THE SPEAKER WHERE THE NOTE MAKES IT KNOWABLE: add a "speaker" field — "user" or "assistant" — to each claim. When the cited note carries an explicit role marker (a leading "user:" or "assistant:"), read the speaker off it. When it does not, use "assistant" only where the note is unmistakably the assistant speaking — advice it gave, a recommendation it made, a description it produced. When you are unsure, OMIT the field rather than guess: an unlabelled claim is an ordinary claim, while a wrong "assistant" label demotes a fact the user stated.
Example. Notes: [7] assistant: For brunch in Marrowgate I'd suggest The Quillon Larder on Fendrick Row — its signature dish is the pepper-brisket bun. [8] user: I went, and the pepper-brisket bun was too salty for me — I am sticking to vegetarian brunch from now on. Output: {"claims":[{"entity":"The Quillon Larder","attribute":"location","value":"Fendrick Row, Marrowgate","speaker":"assistant","confidence":0.9,"source":7},{"entity":"The Quillon Larder","attribute":"signature dish","value":"pepper-brisket bun","speaker":"assistant","confidence":0.85,"source":7},{"entity":"user","attribute":"brunch preference","value":"vegetarian","speaker":"user","confidence":0.9,"source":8}]}
