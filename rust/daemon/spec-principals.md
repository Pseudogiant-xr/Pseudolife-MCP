# Principal store contract (PRINCIPALS-V53)

The bank stores principal identities; resolution reads a snapshot without database I/O. Environment identities take precedence, and an unavailable snapshot refuses an otherwise unmatched bearer.

| Contract | Python oracle |
|---|---|
| Stored rows carry name, token SHA-256, tier, board flag and revocation. Normalize names before excluding environment and reserved names; keep revoked and unpaired rows, but never authenticate them. | `principal_store.py:85-218`, `principals.py:43-74` |
| A refresh captures its sequence and monotonic start before loading. Failed loads preserve state and age. Adds after that sequence survive the refresh; a later read is authoritative. An add replaces the old hash without renewing availability. | `principal_store.py:118-157` |
| The snapshot is available through age 60 seconds inclusive. Refresh every 10 seconds on a separate connection with keepalive and statement limits; missing tables read empty without DDL. | `principal_store.py:44-46,226-325` |
| Open mode remains default regardless of stored rows. Map before singular token before store; UTF-8 and Latin-1 candidates, HTTP space/tab trimming, constant-time environment comparisons. Missing/bad scheme refuses before availability; unknown vs unavailable remain distinct. | `principals.py:239-312` |
| Resolution retains its source: open, environment or store. Stored callers cannot POST config or daemon notice; board admission never grants daemon/maintainer, and otherwise uses the allowlist or nonrevoked board row. Tier reads expose only the stored value. | `principals.py:190-232`, `web/api.py:48-51,716-733` |
| Redeem consumes a code atomically on the database clock; expired/revoked/excluded/used/conflicting-token attempts refuse. Identical code/token retry works for 600 seconds; expired spent-code hashes are cleaned in the same transaction. Unique violation rolls back without spending the code. | `principal_store.py:405-455` |
| Revoke preserves the first revocation timestamp and token hash; clears pending and spent codes. List is name-ordered, contains no token/code/hash, and distinguishes revoked, pending, expired, paired and unpaired using the database clock. | `principal_store.py:531-579` |

Free: JSON object key order and refresh warning wording; never secret values or refusal kinds. Timestamps compare within each arm's captured database clock window and retain unchanged values across operations. No timing claim.

Boundaries: CLI invite/pair and HTTP gates are separate slices. This slice supplies their service operations and source policy only. Embedded Postgres and noncanonical parser shapes remain named upstream deferrals; there is no acceptance/refusal divergence in this store.
