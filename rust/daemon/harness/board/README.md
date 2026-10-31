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
python -m pytest board/test_norm.py board/test_run.py
python -m board.boundary
python -m board.boundary --shared-clock
python -m board.boundary --json-keys
```

The signed Console actions run with
`--only maintainer-enrol maintainer-roles maintainer-mail maintainer-refusals`.
These scenarios additionally compare `maintainer_passkeys`,
`maintainer_bootstrap` and `maintainer_nonces` after each request. They cover
bootstrap and approved enrolment, quarantine cancellation, self-revocation,
conductor and coordinator changes (including legacy purposes), signed mail
and both unread and acknowledged withdrawals. Refusals check signatures,
MACs, purpose, expiry, nonce replay, caller admission and counter regression.
A stale role preview is retried with the exact same assertion after restoring
the signed holder, proving the refusal rolls back its nonce and sign count.
The request checks require the expected status on each arm, so two identical
refusals cannot pass as a successful action. Refused transactions also get an
exact before/after state check, except for documented side effects (wrong
bootstrap guesses, counter flags and the lazy maintainer address).

The fixture uses deterministic software Ed25519 keys and a fixed secret in
each disposable bank. Challenge MACs and WebAuthn challenge bytes are verified
against their raw payloads, and stored mail proofs are cryptographically
verified before their varying signatures and challenges are normalized;
signed fields and authenticator counters remain compared. Maintainer clocks
must match the captured request windows, including the 24-hour quarantine.
Comparator checks are in `python -m pytest board/test_maintainer.py`.

Additional boundary scenarios are selectable separately:
`maintainer-bootstrap-refusals`, `maintainer-key-refusals`,
`maintainer-recipient-refusals`, `maintainer-mail-refusals`,
`maintainer-assertion-refusals` and `maintainer-zero-counter-replay`.
They exercise exhausted and expired host codes, key and approver state
changes, recipient deletion and role changes after preview, sender and
mailbox capacity, malformed assertions, caller admission on all six actions,
and authenticators without a signature counter. Capacity fixtures copy one
genuine signed message into the disposable table; the queue fixture's old
`created_at=1` and the expired code's `expires_at=1` are explicit inputs
compared exactly. They do not wait for wall-clock expiry or send hundreds of
HTTP requests. Retrying the exact refused queue assertion after freeing the
mailbox proves its nonce and counter were rolled back.

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
  A failed window match records a harness error and retains the raw value.
  An explicit listener deadline must match the captured request value,
  capped by the producer's 60-second attachment lease inside that request's window.
  Retention cutoffs must match the UTC day boundary for the scenario's
  configured retention interval and captured clock windows before becoming
  `<utc-day>`. HLC stamps become `H<step>:<logical>`.
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
both daemons need the embedding model (`ORT_DYLIB_PATH`, `PSEUDOLIFE_DAEMON_ONNX_DIR` and the
reranker directory for the Rust arm), because a `send` ticks the service clock, which only the
initialized service holds (W2F-SHARED-HLC). Both daemons must expose `db: ok` and finish embedding
warmup before the first step (the runner waits for these tiers).


The completion run uses the original supplied oracle recordings, preserved
byte for byte outside version control (archive SHA-256
`6cd2bf2fa9c44986289514379f7269d6f234211033e24eecc1db5a1e695f9c92`). The mail scenario's one-second activity window makes its standalone
replay sensitive to elapsed time between calls; see `W2F-MAIL-PACING` in
`../../divergences.md`. Replay reports those differences and exits nonzero;
they are not normalized away. The live comparison and mutant controls remain
separate evidence from this timing-limited recording.

They are not admitted as portable checked-in expectations: the capture lacks
the elapsed input schedule needed to reproduce its activity decisions. A
local `--record` run writes fresh recordings to `board/goldens/`; recording
is separate from proving replay parity.

`board.boundary` creates and removes one owned disposable bank on loopback
port 5434, then explicitly runs the otherwise ignored Cargo regression: a
registered, attached agent must heartbeat while full initialization holds the
service mutex after publishing storage. The normal Cargo suite also covers a
PostgreSQL server that accepts TCP but never completes authentication. With
`--shared-clock` (W2F-SHARED-HLC) it also writes the offline four-dimension
embedding fixture and its config, initializes the full service, and requires
board sends to take the service clock's next tick: above a stamp only that
clock holds, strictly between memory ticks on either side, and below what the
reseed a restart runs reads back from the board's stored high-water mark.

CI runs `test_norm.py` and `test_run.py` on Linux and Windows; the existing
daemon checks compile the board unconditionally and build the `mutants` feature.
The live corpus and eight controls currently run on the development test service.
Hosted live coverage remains a follow-up: the proven runner warms both arms
with a cached model, while a portable offline fixture has not yet been
integrated and validated for this board runner (`board.boundary --shared-clock`
uses the four-dimension fixture for its one Cargo test). The original timing-sensitive
goldens remain strict failing evidence, not CI expectations.

The foundation `tokens` scenario now compares the implemented board routes;
its Console timestamp is checked against the captured request window. The
existing H5 coverage plan excludes this whole scenario for retrieval telemetry
and sequence differences, so that lane cannot validate these board cases.
The board harness CI tests cover their comparator behavior independently.

`--json-keys` registers the serde-reserved capability key, updates the agent,
and checks byte-identical capabilities through roster and Console reads. Board
JSON/JSONB decoding uses the protected HTTP JSON parser for every JSON column.

Mutant mode first clears any inherited mutation and requires a live baseline
with zero response, state and chain differences. A broken baseline returns
failure before any control runs; the caller’s original mutation environment
is restored on every exit.
