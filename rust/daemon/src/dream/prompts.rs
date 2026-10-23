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

/// `_LESSON_SYSTEM_PROMPT` (`memory/dream.py:897`): the clustering contract.
pub const LESSON_SYSTEM_PROMPT: &str = concat!(
    "You consolidate an agent's work-outcome signals into reusable LESSONS. Each ",
    "signal records something that happened while doing a task: a success, a ",
    "failure/dead-end, or a user correction. Produce durable, actionable lessons ",
    "as JSON: ",
    "{\"lessons\":[{\"task\":..,\"aspect\":..,\"lesson\":..,\"about\":..,\"polarity\":\"+\"|\"-\",\"outcome\":\"success\"|\"failure\"|\"correction\",\"confidence\":0..1}]}.\n",
    "- ",
    "task = the kind of task, reusing stable wording across signals.\n",
    "- aspect = ",
    "approach | pitfall | tool-choice | correction.\n",
    "- lesson = the actionable ",
    "takeaway, phrased as what to DO (or what to avoid).\n",
    "- about = the ",
    "tool/source/approach the lesson concerns.\n",
    "- outcome = the signal class it ",
    "came from.\n",
    "- polarity = \"+\" when the lesson is something to DO \u{2014} an ",
    "approach that worked, or the corrected, now-correct way; \"-\" ONLY when the ",
    "lesson is something to AVOID (a dead-end), phrased as \"avoid X\". A ",
    "CORRECTION is almost always \"+\": state the new correct behavior to follow, ",
    "never the mistake.\n",
    "Cluster related signals into one lesson. SKIP trivial or ",
    "non-durable signals \u{2014} generic knowledge any competent agent already ",
    "has (e.g. basic language/library usage), one-off chatter, or anything a ",
    "future run would not benefit from recalling. Return {\"lessons\":[]} if ",
    "nothing qualifies.",
);

/// `_RULE_LESSON_SYSTEM_PROMPT` (`memory/dream.py:926`): one rule per signal.
pub const RULE_LESSON_SYSTEM_PROMPT: &str = concat!(
    "You turn ONE outcome signal from an agent's episode into ONE ",
    "situation-specific RULE. Reply with JSON only: ",
    "{\"lessons\":[{\"task\":..,\"aspect\":\"rule\",\"lesson\":..,\"about\":..,\"polarity\":\"+\"|\"-\",\"outcome\":\"success\"|\"failure\"|\"correction\",\"confidence\":0..1}]} ",
    "containing exactly one lesson.\n",
    "- task = a short name for the SITUATION: the ",
    "request and the observable behaviour that distinguishes it from look-alike ",
    "situations, taken from the signal's detail. Specific to this situation, never ",
    "a generic task type.\n",
    "- lesson = one sentence. A success: \"WHEN <situation> ",
    "THEN <the exact action(s) that produced the outcome>\". A correction (the ",
    "detail carries a CORRECT SOLUTION): \"WHEN <situation> THEN <the exact ",
    "correct action(s), copied verbatim from the correct solution>\"; if an ACTION ",
    "DIFF is given, add one clause naming the decisive divergence. A failure with ",
    "no correct solution: \"WHEN <situation> do NOT <the exact action(s) taken> ",
    "\u{2014} verified wrong against the outcome\"; never invent the right ",
    "answer.\n",
    "- Copy decision-critical values (names of things, options, amounts, ",
    "reasons, arguments) VERBATIM from the signal; do not paraphrase, soften, or ",
    "add conditions, alternatives or exceptions the signal does not contain. ",
    "Identifiers of people become placeholders. If the detail lists MUST INCLUDE ",
    "values, every one of them appears verbatim in the lesson.\n",
    "- polarity = \"+\" ",
    "for a THEN rule, \"-\" for a do-NOT rule. outcome = the signal's class. about ",
    "= the tool or action the rule concerns. confidence = 0..1.\n",
    "Do not cluster ",
    "and do not skip: this signal yields exactly one rule.",
);

/// `_OUTCOME_INFER_SYSTEM_PROMPT` (`memory/dream.py:991`).
pub const OUTCOME_INFER_SYSTEM_PROMPT: &str = concat!(
    "You review the stored record of one work session and infer what OUTCOMES it ",
    "reached. Reply with JSON only: {\"outcomes\": [{\"task\": <short stable ",
    "task-type phrase>, \"outcome\": \"success\" | \"failure\" | \"correction\", ",
    "\"about\": <tool/approach concerned, or null>, \"detail\": <one sentence of ",
    "evidence quoted or paraphrased from the record>}]}.\n",
    "- Claim only outcomes ",
    "the record actually evidences; prefer fewer, better-grounded claims.\n",
    "- ",
    "failure = an approach was TRIED and hit a dead-end; correction = the USER ",
    "explicitly corrected the assistant's belief or approach (an approach failing ",
    "on its own is failure, not correction); success = something verifiably ",
    "worked.\n",
    "- An outcome requires an ATTEMPT: something was tried, deployed, ",
    "fixed, or decided, and its result is visible in the record. Sessions that ",
    "only read, browse, take notes, or collect facts have NO outcome. An ",
    "unfinished task, a deferred decision, or 'revisit later' is NOT an outcome ",
    "\u{2014} abstain. When unsure, abstain \u{2014} a missed outcome is cheap, an ",
    "invented one poisons downstream lessons.\n",
    "- Abstain example: record = ",
    "'Session: reading about css grid\\n- (notes) grid-template-areas allows named ",
    "layout regions' -> {\"outcomes\": []} \u{2014} a fact was noted, nothing was ",
    "attempted.\n",
    "- If the record shows no clear outcome, return {\"outcomes\": []}.",
);

/// `_DIGEST_SYSTEM_PROMPT` (`memory/dream.py:1017`) before `{target_chars}`, its braces unescaped.
pub const DIGEST_SYSTEM_PROMPT_HEAD: &str = concat!(
    "You write a factual narrative digest of one work session from its stored ",
    "record. Reply with JSON only: {\"digest\": <the digest text>}.\n",
    "- Target ",
    "length about ",
);

/// `_DIGEST_SYSTEM_PROMPT` after `{target_chars}`.
pub const DIGEST_SYSTEM_PROMPT_TAIL: &str = concat!(
    " characters of plain prose.\n",
    "- Cover, in order: what the session set out to ",
    "do; the phases or steps it went through; key decisions and the stated reasons ",
    "for them; problems hit and how each was resolved; stated preferences or ",
    "changes of direction.\n",
    "- Use ONLY events present in the record. Omit anything ",
    "uncertain rather than inferring it \u{2014} a missing detail is cheap, an ",
    "invented one poisons later recall.\n",
    "- Keep explicit dates, versions, and ",
    "numbers exactly as recorded.\n",
    "- Write in past tense anchored to the session ",
    "(\"in this session\u{2026}\", \"the user then\u{2026}\") so the digest reads ",
    "as history, never as a claim about the present.\n",
    "- No headings, no bullet ",
    "lists \u{2014} one compact narrative paragraph (two at most).",
);

/// `_RELATION_VOCAB` (`memory/dream.py:374-380`): the typed-edge vocabulary
/// the graph judges name relations from.
pub const RELATION_VOCAB: &str = concat!(
    "depends-on (src needs dst), part-of (src is a component of dst), ",
    "runs-on (src executes on host/platform dst), hosts (src serves dst), ",
    "uses (src makes use of dst), configures (src sets up dst), ",
    "stores-data-in, tests (src is a test of dst), implements (src code ",
    "realizes concept dst), superseded-by (src replaced by dst), related-to ",
    "(untyped; weakest)",
);

/// `_RELATION_NAMES` (`memory/dream.py:382-383`): the bare relation names,
/// the only relations a merge "relate" may carry.
pub const RELATION_NAMES: [&str; 11] = [
    "depends-on",
    "part-of",
    "runs-on",
    "hosts",
    "uses",
    "configures",
    "stores-data-in",
    "tests",
    "implements",
    "superseded-by",
    "related-to",
];

/// `_JUDGE_SYSTEM_PROMPT` (`memory/dream.py:392-424`): the merge judge.
pub const JUDGE_SYSTEM_PROMPT: &str = concat!(
    "You judge MERGE PROPOSALS for a knowledge-graph deduper. Each numbered ",
    "proposal asks: are FROM and INTO two names for the SAME real-world ",
    "referent (accept folds FROM into INTO), or different things (reject)? ",
    "Historically only ~23% of proposals are true merges \u{2014} be skeptical.\n",
    "Rules:\n",
    "1. Evidence over name-shape: accept only when BOTH sides' evidence ",
    "snippets describe the same referent. Similar names with mismatched or ",
    "absent evidence are not enough.\n",
    "2. Differing variant tokens (sizes, quants, versions, dates: v0.8.5 vs ",
    "v0.8.6, 26B vs 4B, two dated filenames) mean sibling artifacts \u{2014} ",
    "reject.\n",
    "3. A dated run/session slug paired with a broader name (a project, a ",
    "process) is one event vs a category \u{2014} reject.\n",
    "4. File-vs-concept, feature-vs-phase, tool-vs-its-output, table-vs-tool ",
    "pairs are related but distinct \u{2014} reject, and name the relation that ",
    "holds from FROM to INTO in \"relation\" when one below fits.\n",
    "5. Legitimate merge shapes: branch-vs-slug, path-vs-basename of the ",
    "same file, bare-vs-qualified name, abbreviation-vs-full name \u{2014} when ",
    "the evidence agrees.\n",
    "6. Use \"leave\" only when the evidence is genuinely insufficient to ",
    "decide; do not use it to avoid judging.\n",
    "\"relation\" is optional and only annotates a reject (src is FROM, ",
    "dst is INTO): ",
    "depends-on (src needs dst), part-of (src is a component of dst), ",
    "runs-on (src executes on host/platform dst), hosts (src serves dst), ",
    "uses (src makes use of dst), configures (src sets up dst), ",
    "stores-data-in, tests (src is a test of dst), implements (src code ",
    "realizes concept dst), superseded-by (src replaced by dst), related-to ",
    "(untyped; weakest)",
    ". Null when none fits. It never ",
    "changes the verdict or its confidence.\n",
    "Return JSON only: {\"verdicts\":[{\"id\":<proposal number>,",
    "\"verdict\":\"accept\"|\"reject\"|\"leave\",\"confidence\":<0..1>,",
    "\"relation\":<vocab or null, with reject only>,",
    "\"note\":\"<reason, max 25 words>\"}]} \u{2014} one entry per proposal, ",
    "confidence is your honest probability that the verdict is correct ",
    "(for a reject: that FROM and INTO are different things).",
);

#[cfg(test)]
mod judge_prompt_tests {
    use sha2::{Digest, Sha256};

    #[test]
    fn judge_prompt_is_pythons_bytes() {
        // sha256 and length of dream._JUDGE_SYSTEM_PROMPT (CPython 3.11).
        let p = super::JUDGE_SYSTEM_PROMPT;
        assert_eq!(p.chars().count(), 2012);
        assert_eq!(
            hex::encode(Sha256::digest(p.as_bytes())),
            "74bc177861381c1a15c6390a1dd719264f3e37d0a0ecdc13915de76c36ac9cbb"
        );
        let names: Vec<&str> = super::RELATION_VOCAB
            .split(", ")
            .map(|part| part.split(" (").next().unwrap_or("").trim())
            .collect();
        assert_eq!(names, super::RELATION_NAMES);
    }
}
