All references are to `pseudolife_memory/storage/coordination.py`.

**Active, not parked.** `_wake_decision` (2822) returns `{"decision": "hinted", "reason": "active"}` at 2872-2873. This applies when `park is None` and `last_activity > now - active_seconds`, which defaults to 60 (485). Urgent mail to an unparked Codex recipient skips this check (`attention`, 2870).

**Parked with a need.** The ways to ring it are checked in this order (2888-2902):
1. Clearer: `park_clear_by` equals the sender (2890). A lease clearer also counts, when `_lease_clearer` says the sender just released that lease (2894-2898).
2. Anyone: `park_clear_by == "anyone"` (2892).
3. Clears text: `clears` is given and `_need_matches(clears, park_needs)` is true (2899).
4. Urgent: `how = authority or "urgent"` (2901-2902).

If none applies, the result is `withheld` with reason `need_not_cleared`, plus the need and a `retry` hint (2903-2905). A ring still goes through the caps below.

**Done park (2876-2887).** Plain mail, or a peer's urgent mail, gets `not_needed` with reason `parked_done` and `reopen_by` (2886). Only two senders can reopen it, and only with urgent mail:
- The maintainer or the project's delegate, through `_authority`.
- The park's named clearer, including a lease clearer (2878-2881).

**Unparked idle recipient, plain mail.** The result is `{"decision": "not_needed", "reason": "no_park"}` (2911-2915).

**Default caps (`WakePolicy`, 481-483).**
- Per recipient per hour: 20. It returns `capped` with reason `recipient_hour` (2939).
- Urgent per sender per hour: 6. It returns `capped` with reason `urgent_sender_hour` (2923). It also applies when a named clearer reopens a done park (2918).
- Nightly total: 200, counted over the last 86400 seconds. It returns `capped` with reason `nightly` (2941).

Other limits not asked about:
- Authority senders get 12 per hour (486).
- Rings from one sender's burst are staggered by 30 seconds (484).
- Maintainer messages take a separate path (2861) capped at 30 per recipient per hour (490).

**`_need_matches` word rule (500-513).**
- Both strings are lowercased and split into words with `\w+`.
- If either has no words, it returns false.
- It matches when the shorter word list appears as a contiguous run of whole words inside the longer one. Equal word lists also match.
- The shorter list must contain at least one word of four or more letters. This stops fillers like "the" or "of" from matching any need.
