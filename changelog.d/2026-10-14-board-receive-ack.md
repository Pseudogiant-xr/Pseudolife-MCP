### Added (2026-10-14 — Receive and acknowledge board mail in one call)

- `memory_message(action="receive", ack=true)` returns pending mail and
acknowledges it in the same call. A session that reads mail now makes one
request where it made two. The result is compact: each message drops the
reader's own address, its sequence, HLC and expiry, and an empty `reply_to`.
The continuity block appears only when it reports expired mail or a gap.
`acknowledged` gives the count, and `ack_missing` lists any message that left
the mailbox between the two steps. Measured on 2026-10-14 over the week's
transcripts: 1,438 receives and 1,361 acks, with board results 25% of what
filled long-running delegate sessions' context. Without `ack`, receive is
unchanged. Both the Python and the Rust daemon implement it.

- The plugin's subagent guard refuses `receive` with `ack=true` from a Claude
Code subagent, as it already refused `ack`: a subagent shares its parent's
board address, so acknowledging would mark the parent's mail read. Existing
sessions pick this up after a plugin update.
