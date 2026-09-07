# Sonnet-tuned dream extraction prompt — v5 (2026-09-07)

v4 with the v2 body's two pre-rule worked examples re-cut on invented
names — the same re-cut the daemon's shipped prompt took with the v12 base
on 2026-09-07 (`ku_op_prompt_v12_count_source_example.txt`), so the shim
path no longer names a LongMemEval answer. Everything else is v4: the
rest of the v2 body verbatim plus the assistant-facts blocks imported from
`pseudolife_memory/memory/dream.py`.

Generated — do not hand-edit. `PYTHONPATH=. python evals/gen_shim_prompt.py`
derives this file from `sonnet_extractor_v2.md` through `V5_RECUTS` and
writes it beside v4. `--system-prompt-file` REPLACES the shipped prefix,
which is why the daemon-side re-cut alone does not reach an install whose
extractor is the shim.

Gate: ladder `opus-5` rung (the Max-plan CLI shim on its dedicated port,
same model the shim autostart serves), v4 vs v5, two replicates per arm —
`evals/results/ladder-shimv5-paired-verdict-threshold.json`.

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
with no op. Example: [5] tried Rosa's Diner tonight. [6] sold the penny-farthing.
Output: {"claims":[{"entity":"user","attribute":"restaurants tried",
"value":"Rosa's Diner","op":"add","confidence":0.8,"source":5},
{"entity":"user","attribute":"bikes owned","value":"penny-farthing",
"op":"remove","confidence":0.8,"source":6}]}

COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS. When a note states or
updates how many of something the user has (a running count, a total, a
follower number, a quantity), emit a plain claim whose value is the NEW
number, with no "op" field — even when the note also names the item that
changed the count. Example: [7] saw a Gallowmere Teal today, that makes 41
species at Kelmarsh Reserve now — yields the single claim {"entity":"user",
"attribute":"bird species seen at Kelmarsh Reserve","value":"41","confidence":0.9,
"source":7} inside the one claims array, and NO "op":"add" claim for
Gallowmere Teal.

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
