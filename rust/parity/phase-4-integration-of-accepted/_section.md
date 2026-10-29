## Phase 4 integration of accepted PostgreSQL repair

Phase 4 merges accepted master
`bde0cf327dff2cb16736d342938fb9e0c19f7569` through ordinary additive history.
The shared PostgreSQL module is identical to that accepted source, including
the libpq18 `prefer` negotiation repair; Phase 4 carries no separate repair.
Strict TLS modes, explicit DSN/ambient refusal and SAN-only hostname policies
remain unchanged. The local hold helpers retain Phase 4 visibility and board
failure codes while receiving master's inherited-SIGINT handling.

The `19f14fa0` hold/break and `90fd0082` delegate/designate receipts remain
historical. In particular, `90fd0082` inherited the `0341ad38` wrong-CA `prefer`
defect; its durable operator cells used `sslmode=disable`. They do not validate
this merged client or imply TLS acceptance. New-image execution, every control
touching the PostgreSQL client, independent review and hosted acceptance remain
unrun or pending. The `fixed-clock-replay`, `hold-mirror-best-effort` and exact
minted-field/name-suffix rules below remain in force. No parity row is promoted.
