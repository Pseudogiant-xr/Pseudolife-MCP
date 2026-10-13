# Dream acknowledgement contract (W3-H, first increment)

Python oracle: the PR merge base. This increment implements PostgreSQL dream
tracking, pull and signed acknowledgement; it advances DREAM without closing
the extractor, run journal, claim application or scheduling contracts.

| Contract | Python source |
|---|---|
| Initialize in one transaction: classify only NULL dream states, preserving explicit pending/acknowledged states; source eligibility and `ts <= cursor` determine legacy-covered rows. Create one 32-byte secret or reject a corrupt existing secret without changing rows. | `storage/postgres.py:1459-1557`, `service.py:3606-3728` |
| Pull pending eligible entries ordered by `(timestamp, db_id)`; cap a pull at 4096 IDs, retain text, timestamp, episode, source, authority and distortion tolerance. Empty pulls omit the token. | `service_dream.py:959-989,1040-1102` |
| The v1 token binds backend, bank generation, ordered distinct positive IDs and finite display timestamp with HMAC-SHA256. Tampered/foreign/malformed tokens reject before acknowledgement. | `dream_token.py:16-156` |
| Acknowledgement atomically locks exact IDs, accepts already acknowledged rows, reports deleted IDs, rejects NULL/legacy-covered states, changes only pending members and advances display cursor by max. Failure rolls back both rows and cursor; retry is idempotent. | `storage/postgres.py:1559-1644`, `service_dream.py:1104-1216` |
| Failure classes determine recovery: ValueError refusals stay latched, operational failures can retry. The first pull retries a transient lazy-init failure, later pulls retry transient latches, and commit never retries an existing latch. Commit maps validation refusals and persistence failures by class, even when a database error resembles a refusal message. | `service.py:3719-3736`, `service_dream.py:990-1002,1040-1051,1104-1216` |

Responses compare by JSON value, including numeric types and list order. Token
issuance uses Python's canonical sorted compact JSON and verifies across arms.
Finite entry timestamps compare numerically; equal timestamps, including signed
zero, use entry ID order. Canonical PostgreSQL IDs fit signed 64-bit storage.
Database dumps use the existing `harness/dbstate.py`; every action compares all
tables and catalogs, including rejected actions and restart. Generated bank
secrets compare by validated shape when independently generated; seeded secrets
and every acknowledgement cursor/state compare exactly. Clocks used by the
existing storage constructor use its existing named normalization rules.
Live catalogs compare by value. Golden catalogs assert exact equality to the
same arm's seeded template for unchanged categories, so fixture role names and
server extension versions do not enter the golden; changed categories compare
by value. Vectors use the existing harness's value digest. All table rows,
cursor values and state transitions remain part of every action's comparison.

The writer session uses the shared `txn::run` seam for initialization and
acknowledgement, and `txn::with_client` for resident reads. Graph and dream
share its mutex and abandoned-transaction recovery; cursor engines have no
separate writer guard. Once
admitted, an operation owns its task through COMMIT or ROLLBACK even if the
waiting request is cancelled, matching Python's offloaded service operation.
The harness also panics after a real partial write and checks that the next
ordinary cached pull recovers the abandoned transaction, confirms the writer
is idle, and matches Python bank state. Every cursor operation crosses
`with_client` first, including pulls whose resident snapshot is already loaded.
Resident acknowledgement changes only after confirmed persistence; lost replies
are recovered by exact-ID retry. Storage acknowledgements alone do not mutate
the resident service snapshot.

The contract harness is a separate feature-gated process adapter importing the
same Rust dream and storage modules as the daemon; Python is a separate oracle
process. It uses the existing harness helpers, disposable homes and test login,
with `pl_cf_w3d_*` banks. No Python/Rust bridge, model, shim or production bank is
used. Production HTTP/MCP dispatch is deferred to the cycle integration.

Declared deferrals: file-mode/embedded PostgreSQL; unpersisted resident entries
(owned by W2-E); noncanonical argument coercions; extractor/run journal/rollback,
quarantine, literal gate, chronicle and session digests (later increments).
Stored display cursors use numeric JSON or plain finite decimal strings from
the canonical producers. Python-only stored-cursor coercions (boolean values,
whitespace-padded strings and underscore numeric spellings) are deferred by
name. Corrupt nonnumeric strings and nonfinite legacy cursors retain their
tested refusal/recovery behavior. The private resident snapshot must receive
newly stored entries during cycle integration; the storage initialize response
is harness-only and its signing secret must never enter production dispatch.
