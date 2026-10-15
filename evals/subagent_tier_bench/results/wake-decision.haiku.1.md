All paths are under `<repo>\pseudolife_memory\storage\coordination.py` (abbreviated as `coordination.py` below).

**Active, not parked:** `hinted`, reason `active`, when `last_activity` is within `active_seconds` (`coordination.py:2871-2873`). No ring. The mail rides along on the recipient's next tool result. The one exception is unparked Codex with urgent mail, which goes to `no_path` attention instead (`:2870`).

**Parked with a need (not done), four ways to ring** (`:2888-2902`):
1. `clearer`: the sender is `park_clear_by`, or `park_clear_by` names a lease the sender has just released (`_lease_clearer`). Urgency is not required.
2. `anyone`: `park_clear_by == "anyone"`. Urgency is not required.
3. `clears`: the message's `clears` matches `park_needs` via `_need_matches`.
4. `urgent`: the sender set `urgent`. The reason is `maintainer` or `delegate` if the sender holds that authority, otherwise `urgent`.

If none applies, the decision is `withheld`, reason `need_not_cleared`, with `park_needs` and the retry text (`:2904-2905`, `WITHHELD_RETRY` at `:230`).

**Done park:** `_live_park` returns the park only while it stands. A done park accepts (`:2876-2887`, `DONE_REOPEN_BY` at `:227`):
- urgent mail from the maintainer or the project's delegate (`authority`, reason `maintainer` or `delegate`);
- urgent mail from the agent named in `park_clear_by` (reason `clearer`).

Anything else returns `not_needed`, reason `parked_done`, with `reopen_by`. `anyone` does not reopen a done park.

**Unparked, idle, plain mail:** `not_needed`, reason `no_park` (`:2911-2915`). Urgent mail to an unparked idle recipient rings as `authority` or `urgent` and is subject to the caps below.

**Default caps** (`coordination.py:481-490`, mirrored in `utils/config.py:1414-1424`):
- per recipient per hour: 20
- urgent per sender per hour: 6 (`urgent_per_sender_per_hour`)
- authority rings per sender per hour: 12
- nightly total: 200
- maintainer rings per recipient per hour: 30 (separate branch, `_maintainer_wake` at `:2976`)
- burst stagger: 30 s
- active window: 60 s

Caps are checked in the order urgent sender, recipient, nightly (`:2919-2942`). A maintainer message bypasses the park logic entirely (`:2861-2862`).

**Word rule (`_need_matches`, `:500-513`):** both strings are lowercased and split into `\w+` words. Either side empty means no match. The shorter word list must contain at least one word of four or more letters, and it must appear as a contiguous run of whole words inside the longer list. Case, spacing and punctuation are ignored. Filler words alone never match.
