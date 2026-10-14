All references are to `<repo>\pseudolife_memory\storage\coordination.py`. `_wake_decision` starts at line 2822. Maintainer messages take a separate route through `_maintainer_wake` (2861-2862); everything below covers the normal path.

**Active and not parked:** if the recipient has no live park and its `last_activity` is within `active_seconds` (default 60, line 485), the result is `{"decision": "hinted", "reason": "active"}` (2871-2873). The mail rides along on its next tool result. One exception: urgent mail to an unparked Codex recipient skips this branch (2870).

**Parked with a need:** it can be rung four ways, checked in this order (2888-2902):
1. **Clearer:** `park_clear_by` equals the sender's ID, or names a lease the sender has just released via `_lease_clearer` (2890-2891, 2894-2898).
2. **Anyone:** `park_clear_by == "anyone"` (2892-2893).
3. **Clears text:** `clears` matches `park_needs` under `_need_matches` (2899-2900).
4. **Urgent:** the reason is the sender's authority (maintainer or delegate) if it has one, otherwise `"urgent"` (2901-2902).

If none applies, the result is `withheld`, reason `need_not_cleared`, with the need and `retry` attached (2903-2905). Any ring is still subject to the caps below (2919-2942).

**Done park:** only urgent mail can ring it. It must come from the maintainer or the project's delegate, or from the park's named clearer (directly or through a lease) (2876-2881). Everything else, including plain mail and a peer's urgent mail even when the park says `anyone`, gets `not_needed`, reason `parked_done`, with `reopen_by` (2882-2887). A ring that gets through is marked `reopened` (2951). A named clearer's ring counts against the urgent allowance (2918).

**Unparked, idle, plain mail:** `{"decision": "not_needed", "reason": "no_park"}` (2911-2915). Unparked urgent mail does go on to the caps (2906-2910).

**Default caps** (the `WakePolicy` dataclass, 481-483):
- `per_recipient_per_hour = 20`, checked at 2939.
- `urgent_per_sender_per_hour = 6`, checked at 2923.
- `nightly_total = 200`, checked at 2941. Despite the name, it counts all rings in the last 24 hours (`day = now - 86400`, line 2875), not just overnight.

Other caps: authority senders get 12 per hour, maintainer messages 30 per recipient per hour, and rings from one sender's burst are spaced 30 seconds apart (484-490).

**`_need_matches` word rule (500-513):** both strings are lowercased and split into words with `\w+`. It returns False if either is empty. The shorter word list must contain at least one word of 4 or more letters. That list must then appear as a contiguous run of whole words inside the longer list; identical lists count. A single letter or a filler word like "the" or "of" therefore never matches.
