All references are to `<repo>\pseudolife_memory\storage\coordination.py`.

**Accepted forms (`_message_ids`, 744-773)**
- The input is a non-empty string of at most 8192 characters (751). Whitespace around it is trimmed (752).
- If it starts with `[`, it is parsed as a JSON array of strings. Bad JSON, an empty array or a non-string item raises `invalid_message_id` (753-760). This exists for hosts that turn list parameters into strings.
- Otherwise the string is split on commas, and it counts as a batch only if it contains a comma (761-763).
- Each item is trimmed and must be 1-120 characters from `[A-Za-z0-9_-]` (642, 767). Duplicates are dropped and the order is kept (769).
- An item of 8 to 31 lowercase hex characters is treated as a prefix, not an exact id (649-650, 661-664). It only matches messages in this mailbox (3753-3756).
- Two spellings that resolve to the same message count once (3766).

**Maximum:** `MAX_PAGE = 50` (274). More than 50 distinct ids raises `invalid_message_id` (771-772).

**Result shapes (3788-3789)**
- One id returns that message's receipt dict.
- Several ids return `{"receipts": [...], "missing": [...]}`. Receipts are in the order given, and `missing` lists the ids exactly as you passed them (3781).

**An id that isn't this mailbox's**
- In a batch, the id goes into `missing` and the other ids are still acknowledged. A prefix that matches nothing is passed `missing=None`, so it stays as given and also ends up in `missing` (3762-3765, 684-690).
- In a single call, it raises `message_not_found`. That happens either during prefix resolution (3763) or after the lookup (3782-3783).
- Re-acknowledging a message that is already acknowledged still returns its receipt. It does not update `last_activity` and writes no audit event (3772-3787).

**Ambiguous prefix**
- In a batch or a single call, it raises `ambiguous_message_id` and refuses the whole call (comment at 3746-3747, code at 3762-3764). The comment's reason is that acknowledging the wrong message cannot be undone.
- The error lists up to 8 candidates (`AMBIGUITY_LISTED`, 652). Each is cut to the shortest prefix that tells them apart, and never shorter than 8 characters, so one can be pasted back (667-674, 692-694).
- The error is raised inside the transaction before any update, so nothing is acknowledged.
