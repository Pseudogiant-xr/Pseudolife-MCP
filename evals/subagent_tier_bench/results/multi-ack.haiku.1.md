**Accepted forms of `message_id`** (`_message_ids`, coordination.py:744-773, called from `ack` at 3737)

- A single full id: 32 lowercase hex characters.
- A single prefix: 8 to 31 lowercase hex characters (`ID_PREFIX_MIN = 8`, `ID_LENGTH = 32`, lines 649-650; `is_id_prefix`, 661-664). Shorter strings and uppercase hex are not treated as prefixes, so they are looked up as exact ids.
- Several ids as a comma-separated string, or as a JSON array of strings (lines 753-763). The array form exists because some hosts stringify list parameters.
- Each item is stripped, must be 1 to 120 characters, and may use only `[0-9A-Za-z-_]` (lines 766-768). Anything else raises `invalid_message_id`.
- Duplicates are dropped in order (769-770).

**Maximum:** `MAX_PAGE = 50` (line 274), checked after deduplication (771-772). Going over it raises `invalid_message_id`.

**Result shapes** (`ack`, 3729-3789)
- One id returns a single receipt dict (line 3789, `receipts[0]`).
- Several ids return `{"receipts": [...], "missing": [...]}` (3789). Receipts follow the order given, and `missing` holds the values as the caller spelled them.

**An id that is not this mailbox's**
- Single call: raises `message_not_found` (3782-3783).
- Batch: the id is listed in `missing` and the rest of the batch proceeds (3781, 3784-3787). A prefix that matches nothing in a batch is kept as given and also lands in `missing` (3761-3765, where `_resolved` is called with `missing=None`).

**Ambiguous prefix**
- Matches are searched only among this mailbox's own messages (3753-3759).
- Several matches raise `ambiguous_message_id` (3762-3764, via `_resolved` at 684-695). This happens in both single and batch calls, and the whole call is refused before any `UPDATE` at 3778. No partial acknowledgement occurs.
- The error lists up to 8 candidates (`AMBIGUITY_LISTED`, line 652), each cut to the shortest distinguishing prefix of at least 8 characters (`distinguishing_prefixes`, 667-674).
