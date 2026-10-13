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
  therefore stays at `agent` tier, as does a claim whose value is not a
  string (a number, a boolean, a list or a dict), including one the shipped
  extractor's parser turned into text; the value is still stored. A `source`
  index
  that is a boolean is not read as a note number. The check confirms that
  the note mentions the value and names the slot, not that it asserts the
  value; the next rule covers that gap for values the user set directly.
- A dream claim never replaces a value that a direct user or action write set
  (`memory_fact_set`, a resolve, a store's auto-promote): with
  `protect_provenance` on, a dream claim for a different value parks as a
  contender, with or without the quarantine, and a quarantine promotion
  cannot replace it either. On set-valued slots a dream add parks rather
  than convert such a scalar into a set (`member_add_blocked_direct`), and a
  dream retraction of a member a direct user- or action-tier write added is
  dropped (`member_remove_refused`). Members added by `memory_set_add`, which
  passes no origin, carry no tier and are not guarded against a dream
  retraction (an assistant-origin retraction of one is still refused). A
  scalar converted into a set keeps every support tier and its marks. A value
  the dream itself consolidated from a user note still updates from a later
  backed user note. The dream marks the tiers it adds (`dream:<tier>` in the fact's
  provenance) and a direct write at that tier, a member confirmation by
  exact or near-duplicate value included, clears the mark; only the dream
  writes a mark (a `dream:` token in a caller's provenance or source tag is
  dropped). Facts written before this release count as direct, except that a
  literal `dream:` token already stored in a fact's provenance (possible
  before this release from a store with such a source tag and auto-promote
  on) is indistinguishable from a mark and is honoured as one.
- A dream restatement that is a shorter form of the current value (a
  compression echo) still confirms it, but no longer raises its tier: only a
  restatement equal to the stored value after normalisation can.
- With the quarantine on, a value promoted by a second witness gains that
  witness's tier (it was stamped `agent`), and the promotion check compares
  that same tier, after the assistant-speaker demotion, against the current
  value, so a promotion is never weaker than a direct write of the same
  claim. The store checks again at promotion time, so a direct write that
  lands after the quarantine chose to promote is not replaced, and a
  different value parked in place of the one the quarantine validated is not
  promoted; the claim parks its own value instead. That holds for a trusted
  claim too: once the quarantine has chosen to promote, a refused promotion
  parks the claim as a contender and never falls back to a direct write.
- A dream write at a user note's tier records no correction signal: those
  remain the record of a human changing a value directly.
- When a dream makes a parked contender current, the value keeps its direct
  user or action support, including after a restart.
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
  such a change waits for `memory_fact_resolve`; likewise a dream note can no
  longer convert such a value into a set or retract a member a direct user-
  or action-tier write added. And `memory_fact_set`, whose
  default origin is `agent`, now parks against a fact the dream consolidated
  at `user` tier instead of superseding it; pass `origin="user"` when the
  human stated the change, or settle it with `memory_fact_resolve`.
- The shipped extractor's parser drops a claim whose value is JSON `null`
  instead of storing the text `None`.
- A dream rollback restores the previous value's full support set and dream
  marks from its newest matching audit record, including removed members.
  It preserves later direct support for the same value and removes a tier
  the rolled-back run's confirmations raised only while it remains dream-only.
- Known limits: a retraction skips the negation rule, so a note that affirms a
  dream-consolidated member can still back a misread removal of it. If the
  previous audit record is no longer available, rollback falls back to the
  journal's strongest prior tier as a direct write. Confirmations without a
  journaled prior tier remain unchanged. A member confirmation that converted
  a scalar into a set leaves the set in place and reports a partial rollback.
  A rollback keeps every direct tier that backs the restored value, a
  surviving set member's included. A direct write of the same value that
  lands while the rollback restores it is kept too, except at the tier the
  rollback's own write stamps (the previous tier, and the user tier when it
  has to resolve a contest), which cannot be told apart. If the daemon
  stops partway through a rollback, a value it already restored can keep
  direct backing instead of its dream mark; that only protects it more.
- Facts consolidated before this release keep their stored tier until
  restated; nothing is backfilled.
