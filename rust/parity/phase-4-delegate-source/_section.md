## Phase 4 delegate source preparation

Native `delegate` and its deprecated `designate` alias prepare the existing
external-DSN role grant. Both role rows lock in name order before resolving a
registered, unrevoked recipient. The transaction removes its coordinator
queue entry, breaks its live coordinator role, settles that queue, drops
reserved conductor waiters, vacates a hold under the role's old lease name
(`delegate:<project>`, a `lease_break` with reason `renamed`), replaces the
conductor (`conductor:<project>`, record `lease_conductor`, since 2026-10-31;
the source is `shim/src/cli/lease/operator/store/conductor.rs`) and appends
the ordered audit chain with the recipient's project/task scope. The ordinary prefix grammar,
60-second to seven-day grant, 24-hour default, reachability warning and JSON
field order are retained. The shared PG/TLS policy and container transport are
unchanged; embedded banks still refuse `phase4-embedded-pg-deferred`, naming
the requested action. Generic failures use `native-pg-diagnostics`.

The original delegate successor was source preparation only. Subsequent
compilation and whole-command receipts are recorded above against `90fd0082`
and `67478612`; they do not validate later source repairs. Mutation proof requires `fixed-clock-replay` and
exact rows, audit payloads, hashes, sequences and streams. Earlier break/hold
images retain their original source identities and do not validate this
changed runtime. The port-owned operator deferral assertion is replaced by
the approved explicit malformed-DSN refusal control, retaining exact exit,
streams, deprecation ordering and unchanged local files. At source checkpoint
`3583bb6c`, that control had run only on the historical candidates; the repairs
at that checkpoint remained unrun. Original Python
and tests retain accepted master bytes.
No lease mode is promoted.
