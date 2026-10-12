### Security (2026-10-08 — dream facts keep their note's provenance tier)

- A fact the dream consolidated from a user-stated note (`source="user"` or
  `"conversation"`) was stored at `agent` tier, because the tier came from the
  claim's `origin` field, which the shipped extractors always set to `agent`.
  The dream now stores each fact at the tier of its backing note's `source`
  tag, the same rule the quarantine's witness check uses; the claim's `origin`
  field, being model output, is no longer read. In a multi-note batch, where
  the backing note is the extractor's citation, a tier above `agent` also
  needs the claim's value in the cited note's text; a claim that fails the
  check is treated as unbacked (agent tier, low-trust to the quarantine), so a
  mis-cited claim cannot borrow a user note's tier. With the quarantine on, a
  value promoted by a user-sourced second witness is likewise stored at user
  tier (it was stamped `agent`). Facts consolidated before this release keep
  their stored tier until restated; nothing is backfilled.
- Set-valued slots get the scalar path's tier guard (with `protect_provenance`
  on): a weaker-tier dream op can no longer convert a stronger tier's scalar
  into a set (it parks as a contender, `member_add_blocked_tier`) or retract a
  stronger tier's member (`member_remove_refused`). The MCP set tools pass no
  tier and stay unguarded.
