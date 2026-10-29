## Phase 4 lease hold preparation

The Phase 4 branch includes the accepted Phase 2d source base. Native
`hold` takes the existing named local OS lock, follows an external PID using a
read-only process probe, and releases without signalling that PID. Its board
mirror registers `lease-hold@<instance-id>`, renews and notifies the concerned
peers asynchronously; local exclusion ends before bounded board cleanup.
The parser and pinned Python help guards include hold/break/delegate and the
`designate` deprecation alias. The historical hold checkpoint retained break/
delegate/designate exit-1 deferrals. Subsequent operator preparation supersedes
those implementation deferrals; acceptance of all four actions remains deferred.

The named `hold-mirror-best-effort` rule follows the producer contract in
`pseudolife_memory/lease_cli.py:487-509` (`_connect`), lines 1103-1115
(`BoardMirror` connection failure), and lines 1285-1324 (`_hold`). Hold acquires
its local lock before attempting the mirror. An unusable configured bearer
therefore reports board skipped after acquisition; it does not refuse local
ownership. This corrects the earlier pre-lock bearer refusal in the historical
Phase 4 candidate. A failure after acquisition, including forbidden registration headers or
`HTTP_REPLY_NOT_UNDERSTOOD`, stops only the mirror. The hold keeps its lock,
leaves the followed PID running and retains its normal exits. Core refusal
before lock/fallback/child effects remains scoped to check/list/run. The
additive `lease_hold.rs` controls distinguish these two admission boundaries.
Ordinary non-ASCII registration failures also remain mirror-only.

The `nondeterministic-bytes-semantic` rule applies to identities issued by
the real daemon during mirrored hold registration. Its exact field scope is
the registration reply's `agent_id` and `credential`, their subsequent
`X-PL-Agent`/`X-PL-Agent-Key` echoes, `coordination_agents.agent_id` and
`credential_hash`, lease holder IDs (including HTTP holder views), and audit
rows' `agent_id` plus the chain links derived from that identity. Each arm
must receive a canonical UUID4 hex ID and a 43-character URL-safe token;
the stored credential hash must equal SHA-256 of its actual issued token.
Each arm also validates the exact display name produced by its label, one
space and its own `agent_id[:8]` suffix. The producer is `_board_name` at
`pseudolife_memory/storage/coordination.py:623`, with the derivation at lines
627-628; only that eight-character UUID echo is semantic.
Every captured audit chain must independently verify its original payload
text, hashes and links before semantic comparison, with exact event kinds,
order, sequence and all other columns. Request-body bytes, CLI streams,
lock/child behavior and all unrelated rows remain exact. This rule does not
permit replacing entropy sources or discarding unrelated state differences.
Mirrored hold whole-command acceptance remains pending.

This historical hold checkpoint was source preparation, with no Phase 4 native
execution or acceptance claim. Windows format, both-feature Clippy and targeted cargo tests, both-OS
public-process/state corpora, Windows interruption and PostgreSQL transaction/
audit/ownership controls remain pending. Linux runtime gates run on the
existing homelab machine; nextest remains in hosted CI. The Phase 2d runtime
receipts keep their old identities and do not validate this changed runtime.
Full CLI-LEASE and scoped lease-core acceptance remain separate and deferred.

Pending operator comparisons use the named `fixed-clock-replay` rule: both
arms receive the identical fixed clock through the same existing test or
OS-level injection seam. Receipts identify that clock and its source; expiry,
audit rows and chain hashes remain exact. Cases without a shared clock seam
remain explicitly uncompared. No production Python clock hook, timestamp
removal or hash relaxation is introduced.
