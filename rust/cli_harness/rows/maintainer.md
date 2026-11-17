# Native maintainer host commands

The native `maintainer enrol-code`, `confirm`, `revoke`, `reset` and `list` commands answer every invocation. Guided `maintainer setup` remains a separate implementation and is outside this row's change.

## Commands and refusal order

- `enrol-code [--no-wait] [--poll SECONDS]` requires finite polling seconds greater than 0 and below 18446744073709551616 when waiting. Invalid values get a usage error before any bank access or code storage; `--no-wait` leaves its unused numeric polling value alone.
- `confirm [--] PREFIX` and `revoke [--] PREFIX` accept a literal value after `--`, including one that resembles a help flag. Prefix length and base64url validation still happen inside the existing transaction before its first statement.
- `reset [--yes]` retains its consent check after the table probe. Repeated or abbreviated flags get usage errors; no broader reset is admitted.
- `list` has no additional arguments. Bare, malformed or undecodable arguments receive exit 2 and a repair before any bank access. Known abbreviations name their full spelling. Argument values are not reproduced in errors.

Help and setup dispatch retain their existing priority. A supplied, nonempty `PSEUDOLIFE_MCP_DATABASE_URL` must pass the shared native DSN parser, including its ordinary `application_name` and `connect_timeout` connection options. Invalid Unicode, syntax, hosts, ports and unsupported options are refused with the setting name and a fixing instruction; the URL, its password and untrusted option keys are never printed. An absent or empty setting preserves container resolution, the retired lite-bank refusal and the existing missing-bank error.

## Bank behavior

The table probe, transaction and locking order, prefix and state checks, passkey changes, session revocation and audit append remain unchanged. A failure before COMMIT rolls back through the existing path; COMMIT failures retain their existing native error handling. This completion adds no schema or authentication changes.

Previously deferred failures now have native answers:

| Condition | Answer |
|---|---|
| Runtime creation | Exit 2, runtime cause and retry instruction |
| Table-probe row cannot be decoded | Exit 2, bank schema repair instruction |
| System clock before the epoch, random generation, audit timestamp or sequence overflow | Exit 2, named cause and repair; the transaction still rolls back |
| Listing query fails | Exit 2, schema or SELECT-permission repair where identifiable, otherwise the existing safe SQLSTATE-class error |
| Listing columns cannot be decoded | Exit 2, maintainer table type/schema repair |
| Timestamp cannot be represented | Exit 2 before printing any list line, naming active_from/last_used_at and timestamp repair |
| Listing reaches or exceeds 8192 bytes | Write the complete report without a size cutoff |
| Stdout refuses before any byte | Exit 120 with a writable-output repair; a partial write retains exit 120 |

Runtime, entropy, system-clock and audit-overflow failures are practically unreachable without injection and have no dedicated failure test. Their messages contain only fixed text or the runtime's OS error, never a connection setting or credential.

## UTC timestamps

Every displayed time is floored to a whole second and printed in UTC as `YYYY-MM-DDTHH:MM:SSZ`. Chrono's signed expanded-year form is used beyond year 9999; dates before 1970 are supported. NULL prints `-`. Non-finite values and dates outside Chrono's supported range produce the named refusal above. `TZ`, local year and daylight-saving boundaries do not select another path. Labels, credential prefixes, state padding and the flagged marker keep their existing form.

## Fixtures and regression snapshots

The existing seeded banks are created with the test login and the disposable-bank guards on the dev machine's test PostgreSQL only. Each arm receives its own copy of a seed. The row wraps connection URL values in a local str subclass with a fixed repr placeholder; the real string payload still reaches the child environment. A synthetic regression checks case repr, pytest parameter ID and actual child delivery. Bank-backed runs are redirected to private machine logs and checked for test-password disclosure before logs are copied or summarized.

Historical `defer-*` IDs remain regression cases for native outcomes. Deferral and local-time normalizers are removed; UTC output is compared directly. New `list-read-error` and `list-column-type-error` cases alter only their guarded disposable copies to exercise failed reads and row decoding, and require the database state to remain unchanged by the command. These read-fault goldens are the collected replay coverage; the former standalone qualification function in the row was not collected by ordinary pytest and has been removed.

Goldens are recorded from the native candidate with `--record-candidate --write` only for cases whose observable output changes, then replayed. Mutating command observations retain the existing code/secret, clock and audit-chain normalization; no real bank, enrolment or credential is used.
