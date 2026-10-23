//! The extraction prompts, byte for byte: they are part of the request
//! body both daemons send. Generated from the Python constants and pinned
//! by the request-body tests in `extractor.rs`.

/// `_SYSTEM_PROMPT` (`memory/dream.py:342-343`): the shipped claims prompt.
pub const SYSTEM_PROMPT: &str = concat!(
    "You consolidate numbered notes into canonical facts. Extract durable, current-state ",
    "facts as JSON: {\"claims\":[{\"entity\":..,\"attribute\":..,\"value\":..,\"confidence\":0..1,\"source\":<number ",
    "of the note the fact came from>}]}. One slot per real fact; skip narrative, ",
    "opinions, and obsolete states. When several notes state or update the SAME ",
    "fact, use one consistent entity and attribute for it and emit only the CURRENT ",
    "value (source = the note stating it). Reuse existing slot keys when they ",
    "fit. When a note quotes or summarizes a DOCUMENT (a spec, policy, protocol, ",
    "runbook, or guide), what the document prescribes is itself a durable fact ",
    "— extract it with entity = the document's subject, even when other notes ",
    "show something different being done.\n",
    "Example. Notes: [1] we moved the deploy target from staging to prod-eu. ",
    "[2] the release runbook says every release needs a signed tag. Output: {\"claims\":[{\"entity\":\"deploy ",
    "target\",\"attribute\":\"environment\",\"value\":\"prod-eu\",\"confidence\":0.9,\"source\":1},{\"entity\":\"releases\",\"attribute\":\"documented ",
    "requirement\",\"value\":\"signed tag (per release runbook)\",\"confidence\":0.8,\"source\":2}]}\n",
    "When a note adds or removes an item from a COLLECTION the user maintains ",
    "(restaurants tried, bikes owned, pending tasks), add an \"op\":\"add\" or \"op\":\"remove\" ",
    "field to that claim instead of a plain supersede. op is ONLY for collection ",
    "membership — a value that simply changed (a new job, a moved city) stays ",
    "a plain claim with no op. Example. Notes: [3] tried Rosa's Diner tonight. ",
    "[4] sold the penny-farthing, no longer biking to work. Output: {\"claims\":[{\"entity\":\"user\",\"attribute\":\"restaurants ",
    "tried\",\"value\":\"Rosa's Diner\",\"op\":\"add\",\"confidence\":0.8,\"source\":3},{\"entity\":\"user\",\"attribute\":\"bikes ",
    "owned\",\"value\":\"penny-farthing\",\"op\":\"remove\",\"confidence\":0.8,\"source\":4}]}\n",
    "COUNTS, TOTALS, AND QUANTITIES ARE NEVER MEMBERS: when a note states or ",
    "updates how many of something the user has (a running count, a total, a ",
    "follower number, a quantity), emit a plain claim whose value is the NEW ",
    "number, with no \"op\" field — even when the note also names the item that ",
    "changed the count. For example, the note [5] saw a Gallowmere Teal today, ",
    "that makes 41 species at Kelmarsh Reserve now — yields the single claim ",
    "{\"entity\":\"user\",\"attribute\":\"bird species seen at Kelmarsh Reserve\",\"value\":\"41\",\"confidence\":0.9,\"source\":5} ",
    "inside the one claims array, and NO \"op\":\"add\" claim for Gallowmere Teal. ",
    "The same holds when the count is of items from a source: the note [6] cooked ",
    "the Marrowgate stew tonight, that makes 9 of the Quillon Larder's recipes ",
    "I've tried now — yields only the single claim {\"entity\":\"user\",\"attribute\":\"Quillon ",
    "Larder recipes tried\",\"value\":\"9\",\"confidence\":0.9,\"source\":6} inside the ",
    "one claims array, and NO \"op\":\"add\" claim for the stew.\n",
    "HEDGES GO IN A STANCE FIELD: when the note itself hedges a fact (\"probably\", ",
    "\"might\", \"unconfirmed\", \"not final\", \"per the runbook\"), keep the value ",
    "CLEAN and put the note's own hedge words in a \"stance\" field on that claim ",
    "— never inside the value, and never invent a stance the note does not carry; ",
    "a plainly stated fact has no stance field. A hedged update is STILL an update: ",
    "use the same entity and attribute as the fact it changes and emit only the ",
    "CURRENT value, exactly as for a plain fact. For example, a later note [7] ",
    "we'll probably move the deploy target again, to eu-west-1 next quarter — ",
    "updates the deploy target slot from the earlier example to the single claim ",
    "{\"entity\":\"deploy target\",\"attribute\":\"environment\",\"value\":\"eu-west-1\",\"stance\":\"probably\",\"confidence\":0.6,\"source\":7} ",
    "inside the one claims array.\n",
    "Return {\"claims\":[]} if nothing qualifies.\n",
    "THE ASSISTANT'S OWN STATEMENTS ARE FACTS TOO: the notes are turns of a conversation, ",
    "some of which the assistant produced. What the ASSISTANT asserted, described, ",
    "recommended, or specified is extractable on exactly the same terms as what ",
    "the user said — names, values, descriptions, specifications, and the choices ",
    "it presented all qualify. Key each such claim to WHAT IT IS ABOUT: the entity ",
    "is the thing described (the restaurant, the book, the tool, the setting), ",
    "never \"the assistant\" and never \"the conversation\". A recommendation the ",
    "assistant made is a durable fact about the thing recommended.\n",
    "NAME THE SPEAKER WHERE THE NOTE MAKES IT KNOWABLE: add a \"speaker\" field ",
    "— \"user\" or \"assistant\" — to each claim. When the cited note carries an ",
    "explicit role marker (a leading \"user:\" or \"assistant:\"), read the speaker ",
    "off it. When it does not, use \"assistant\" only where the note is unmistakably ",
    "the assistant speaking — advice it gave, a recommendation it made, a description ",
    "it produced. When you are unsure, OMIT the field rather than guess: an unlabelled ",
    "claim is an ordinary claim, while a wrong \"assistant\" label demotes a fact ",
    "the user stated.\n",
    "Example. Notes: [7] assistant: For brunch in Marrowgate I'd suggest The ",
    "Quillon Larder on Fendrick Row — its signature dish is the pepper-brisket ",
    "bun. [8] user: I went, and the pepper-brisket bun was too salty for me — ",
    "I am sticking to vegetarian brunch from now on. Output: {\"claims\":[{\"entity\":\"The ",
    "Quillon Larder\",\"attribute\":\"location\",\"value\":\"Fendrick Row, Marrowgate\",\"speaker\":\"assistant\",\"confidence\":0.9,\"source\":7},{\"entity\":\"The ",
    "Quillon Larder\",\"attribute\":\"signature dish\",\"value\":\"pepper-brisket bun\",\"speaker\":\"assistant\",\"confidence\":0.85,\"source\":7},{\"entity\":\"user\",\"attribute\":\"brunch ",
    "preference\",\"value\":\"vegetarian\",\"speaker\":\"user\",\"confidence\":0.9,\"source\":8}]}\n",
);

/// `_EVENTS_SYSTEM_PROMPT` (`memory/dream.py:355-371`).
pub const EVENTS_SYSTEM_PROMPT: &str = concat!(
    "You extract EVENTS from numbered notes — dated occurrences (a trip taken, ",
    "a purchase, an adoption, a start or an end), not standing facts. Return ",
    "JSON: {\"events\":[{\"description\":..,\"actor\":..,\"date\":\"YYYY-MM-DD\",\"date_phrase\":<the ",
    "note's own words about when>,\"source\":<number of the note>}]}. Resolve date ",
    "from dates written in the note (including a leading [date] stamp); exact ",
    "calendar days only — when the note's words cannot pin an exact day, set ",
    "date to null and keep date_phrase verbatim. Never invent a date. For example, ",
    "the note [7] [2023/05/14 (Sun) 10:02] user: we finally adopted the kitten ",
    "yesterday! — yields the single event {\"description\":\"adopted a kitten\",\"actor\":\"user\",\"date\":\"2023-05-13\",\"date_phrase\":\"yesterday\",\"source\":7} ",
    "inside the one events array. Skip standing facts, opinions, and narrative; ",
    "extract each real occurrence once. Return {\"events\":[]} if nothing qualifies.",
);

/// `_FACTS_HINT_HEAD` (`memory/dream.py:883-887`).
pub const FACTS_HINT_HEAD: &str = concat!(
    "\n",
    "\n",
    "Current known facts (for key reuse — if a note updates one of these, emit ",
    "the claim under the SAME entity and attribute with the new current value; ",
    "never emit a claim the notes do not state):\n",
);

/// `_RELATIONS_PROMPT_HEAD` (`memory/dream.py:1050-1054`).
pub const RELATIONS_PROMPT_HEAD: &str = concat!(
    "You extract durable RELATIONSHIPS between named entities from notes, as ",
    "JSON: {\"relations\":[{\"src\":..,\"relation\":..,\"dst\":..}]}. Use ONLY these ",
    "relation names:\n",
);

/// `_RELATIONS_PROMPT_TAIL` (`memory/dream.py:1055-1064`).
pub const RELATIONS_PROMPT_TAIL: &str = concat!(
    "\n",
    "Always prefer the most specific listed relation. Use 'related-to' ONLY when ",
    "the text explicitly states a meaningful connection that fits no listed relation ",
    "— NEVER for entities that merely appear together in the same note. When ",
    "no listed relation fits and no explicit connection is stated, skip the pair. ",
    "src and dst are entity names (services, hosts, tools, components). Skip ",
    "opinions, chit-chat, and anything with no entity-to-entity relationship. ",
    "Return {\"relations\":[]} if nothing qualifies.",
);

/// The fixed head `_vocab_hint` (`memory/dream.py:619-631`) puts before the keys.
pub const VOCAB_HINT_HEAD: &str = concat!(
    "\n",
    "\n",
    "Existing slot keys, each written entity.attribute (reuse when a note updates ",
    "one — emit the part BEFORE the dot as \"entity\" and the part AFTER it as ",
    "\"attribute\"; never put a whole key in \"entity\", and never use the literal ",
    "\"value\" as an attribute): ",
);
