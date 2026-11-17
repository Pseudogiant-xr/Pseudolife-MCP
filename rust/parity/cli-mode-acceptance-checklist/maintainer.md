| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| maintainer | 4/5 | ported-with-substitution | native | Enrol-code, confirm, revoke, reset and list answer every invocation natively. Invalid arguments and explicit database settings are refused without exposing values; existing transaction and admission checks are retained. Listings have no 8192-byte deferral and use stated UTC timestamps, with native read/write errors. Guided setup is separate and unchanged. See rust/cli_harness/rows/maintainer.md and native unit/contract tests. |
