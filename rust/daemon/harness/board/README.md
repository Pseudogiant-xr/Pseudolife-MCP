# Board differential harness (slice W2-F)

Runs the Python daemon (the oracle) and the Rust daemon side by side, each on
its own disposable bank (`pl_cf_w2f_*`, created through the test login), and
plays the same scripted scenario against both. The contract is the
"coordination board" section of `../../spec.md`.

```
cd rust/daemon/harness
python -m board.run live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
python -m board.run golden  --rust-bin PATH [--only NAME ...]
python -m board.run mutants --rust-bin PATH_BUILT_WITH_--features_mutants
python -m pytest board/test_norm.py
```

Per step it compares the status, the headers clients read (`content-type`,
`cache-control`, `x-pl-board`) and the body by value. After every write it
dumps the board's tables on both arms (agents, messages, events, leases,
waiters, wakes, the two board meta keys and the three board sequences) and
diffs them row for row. After every scenario it runs Python's own
`verify_audit_chain` on each arm's chain, recomputes every row's hash with
Python's `audit_hash`, requires every stored payload to equal Python's
`_canonical` of itself byte for byte, and checks that every credential a
register returned hashes to its stored `credential_hash`. The `mixed`
scenario appends to one chain from both daemons in turn (Python first, then
Rust first) and requires Python's verifier to accept the whole chain.

## Normalizers (`norm.py`, recorded with the goldens)

- Random ids become ordinals by first appearance (responses walked in step
  order, object keys sorted); prefixes and cursors map to their id.
- Wall-clock times become `T<step>+<offset>` inside the arm's own window,
  the offset being one declared per field (0, 60, a TTL, a scenario's ttl).
  HLC stamps become `H<step>:<logical>`.
- Digests, salts, credentials and the bank id are replaced after a shape
  check; a value of the wrong shape stays visible as a diff.
- Lists both producers sort by a raw random id (`participants`, prune's
  `agent_ids` and `message_ids`, a burst's `receipts`) are checked to be
  sorted raw, then compared as sets; a burst's audit `seq`s are relabelled
  by recipient ordinal, the order inside a burst being free.

`age` steps shift every time column outside the audit log back by N seconds
on both arms identically (the audit log is hashed and stays as written).

## Running it

Linux runs go to the slice's lane box with its own test PostgreSQL on
127.0.0.1:5434 (`PSEUDOLIFE_TEST_PG_HOST_PORT`, `PSEUDOLIFE_TEST_PG_LOGIN_FILE`);
the Rust daemon needs no ONNX model for the board. The oracle must finish its
warmup before the first step (the runner waits for it).
