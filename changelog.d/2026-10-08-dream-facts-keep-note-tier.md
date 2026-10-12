### Security (2026-10-08 — dream facts keep their note's provenance tier)

- A fact the dream consolidated from a user-stated note (`source="user"` or
  `"conversation"`) was stored at `agent` tier, because the tier came from the
  claim's `origin` field, which the shipped extractors always set to `agent`.
  The dream now stores each fact at the tier of its backing note's `source`
  tag, the same rule the quarantine's witness check uses; the claim's `origin`
  field, being model output, is no longer read.
- The backing note is the extractor's citation, so a tier above `agent` is
  kept only when the cited note passes deterministic checks, in every batch,
  one note or many: the claim's whole value appears in the note as one phrase
  (case-insensitive, on Unicode word boundaries, every word kept) at least
  once without a negator (`not`, `no`, `never`, `without`, `neither`, `nor`,
  `cannot` or an `n't` form) among the three words before it in the same
  clause, and the note contains at least one word of the claim's entity or
  attribute name. A retraction (`op: "remove"`) skips the negation rule. A
  claim that fails is treated as unbacked: `agent` tier, the tier every dream
  fact had before this change, and low-trust to the quarantine. A paraphrase
  therefore stays at `agent` tier. A `source` index that is a boolean is not
  read as a note number. The check confirms that the note mentions the value
  and names the slot, not that it asserts the value; the next rule covers
  that gap for values the user set directly.
- A dream claim never replaces a value that a direct user or action write set
  (`memory_fact_set`, a resolve, a store's auto-promote): with
  `protect_provenance` on, a dream claim for a different value parks as a
  contender, with or without the quarantine, and a quarantine promotion
  cannot replace it either. A value the dream itself consolidated from a user
  note still updates from a later backed user note. The dream marks the tiers
  it adds (`dream:<tier>` in the fact's provenance) and a direct write at that
  tier clears the mark; facts written before this release count as direct.
- A dream restatement that is a shorter form of the current value (a
  compression echo) still confirms it, but no longer raises its tier: only a
  restatement equal to the stored value after normalisation can.
- With the quarantine on, a value promoted by a second witness gains that
  witness's tier (it was stamped `agent`), and the promotion check compares
  that same tier, after the assistant-speaker demotion, against the current
  value, so a promotion is never weaker than a direct write of the same
  claim.
- A dream write at a user note's tier records no correction signal: those
  remain the record of a human changing a value directly.
- Set-valued slots get the scalar path's tier guard (with `protect_provenance`
  on): a weaker-tier dream op can no longer convert a stronger tier's scalar
  into a set (it parks as a contender, `member_add_blocked_tier`) or retract a
  stronger tier's member (`member_remove_refused`). The MCP set tools pass no
  tier and stay unguarded.
- Behaviour changes to expect. With the quarantine on, a user claim that
  fails the citation check (a paraphrase, a negated mention, or a note that
  never names the slot) parks as a contender instead of being written. A
  dream claim that conflicts with a value set by `memory_fact_set` (or another
  direct write) at user or action tier parks instead of superseding it, so
  such a change waits for `memory_fact_resolve`. And `memory_fact_set`, whose
  default origin is `agent`, now parks against a fact the dream consolidated
  at `user` tier instead of superseding it; pass `origin="user"` when the
  human stated the change, or settle it with `memory_fact_resolve`.
- Facts consolidated before this release keep their stored tier until
  restated; nothing is backfilled.
