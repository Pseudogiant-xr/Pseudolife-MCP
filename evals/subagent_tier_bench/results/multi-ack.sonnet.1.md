All references are to `pseudolife_memory/storage/coordination.py`. The main logic is `ack()` at 3729-3789 and the parser `_message_ids()` at 744-773.

**Accepted forms** (`_message_ids`, 744-773)
- A single id.
- Comma-separated ids (762-763).
- A JSON array of strings, for hosts that stringify lists (753-760). A malformed array, an empty array, or non-string items raises `invalid_message_id`.
- Each item is trimmed and must be non-empty, at most 120 characters, and use only allowed id characters (766-768). Otherwise the whole call raises `invalid_message_id`.
- Duplicates are dropped in order (769-770).
- An item can be a full id (`ID_LENGTH` 32, line 650) or a hex prefix of 8 to 31 characters (`is_id_prefix`, 661-664; `ID_PREFIX_MIN` 8, line 649).
- The whole string is capped at 8192 characters (751).

**Maximum:** `MAX_PAGE` = 50 (274). More than 50 unique ids raises `invalid_message_id` (771-772).

**Result shapes** (3788-3789)
- A single id returns one receipt.
- Any comma or JSON-array input counts as a batch and returns `{"receipts": [...], "missing": [...]}`. Receipts are in the order given, and `missing` lists the ids as given. A one-element array is still a batch.

**Id that is not this mailbox's**
- Lookups are scoped to `recipient_agent_id=agent_id` (3769-3771).
- In a single call, a missing id raises `CoordinationError("message_not_found")` (3782-3783).
- In a batch, the id goes into `missing` and the call still succeeds (3781; docstring 3732-3734).
- Ids already acknowledged are not re-written. Replays leave the mailbox and audit trail unchanged (3772-3775).

**Ambiguous prefix**
- A prefix is resolved only among this mailbox's own messages (3748-3759).
- `_resolved` (684-695) raises `ambiguous_message_id` when several messages match. The error lists up to `AMBIGUITY_LISTED` = 8 candidates, each cut to a distinguishing prefix, so the caller can paste one back (667-674).
- This refuses the whole call, batch or not, and nothing is acknowledged (comment at 3746-3747).
- A prefix that matches nothing is treated like any other unknown id: `message_not_found` in a single call, or `missing` in a batch (3763).
