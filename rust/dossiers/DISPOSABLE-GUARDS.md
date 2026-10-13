# DISPOSABLE-GUARDS

Source commit: `3b4de2c515bee07e97cd35b6e1473ea48511ec77`.
Producer attribution: **dossier author's source census**. All source references
refer to that commit; no database was created, connected to or reset here.

## 1. What the row promises

Refuse reset of the known production bank, and refuse every reset when the
production database identity cannot be resolved (`pseudolife_memory/storage/schema.py:1011`).
Ask the connected server for its actual database before connection reap, DDL or
truncate, so a DSN spelling cannot bypass the guard (`:1052`). This is a
production-name refusal, not a disposable-prefix allowlist (`rust/PARITY.md:292`).

## 2. Entry points

All guard functions are in `pseudolife_memory/storage/schema.py`:

| Entry | Role |
|---|---|
| `:943 dsn_database_name` | libpq DSN parse; dbname or permitted PGDATABASE fallback |
| `:971 dsn_endpoint` | Safe endpoint diagnostic without credential |
| `:994 _unresolved_error` | Fixed unresolved-production refusal |
| `:1004 _fold` | Strip trailing slashes, then casefold names |
| `:1011 _production_database_names` | Default bank plus recorded/run environment bank |
| `:1033 is_production_database` | Resolve refusal set plus caller extra names |
| `:1041 refuse_production_database` | Raise ProductionDatabaseError on match |
| `:1052 assert_disposable_database` | Server current_database SELECT, then same refusal |

No HTTP route is added. Calls flow from reset/proof caller -> configured-name
guard -> connect -> actual-server guard -> caller-owned destructive operation.
Example production workflow: `pseudolife_memory/coordination_proof.py:29
_disposable_bank` checks the explicitly named fixture server/database before
creating a generated owned proof database; its exercise checks that actual
bank again at `:132`. Test reset is `tests/pg_fixtures.py:427`; benchmark reset
is `evals/rust_baseline/daemon.py:53`. Guard functions do no destructive SQL.

## 3. State touched

Reads environment identity snapshot `PRODUCTION_DATABASE_ENV`, the unresolved
sentinel, `PSEUDOLIFE_MCP_DATABASE_URL`, `PGDATABASE`, `PGSERVICE`, and the
module's fixed production-name set (`schema.py:1011`). No environment mutation
is performed by these guard helpers. An inherited run snapshot takes priority
over resolving the daemon DSN again; `<unresolved>` fails closed (`:1018`).
DSN without dbname uses PGDATABASE only with no service in play; explicit
empty dbname remains unresolved (`:958`). Invalid DSN returns None without
printing driver text. `dsn_endpoint` has a separate logging role; do not use
its sanitized display text to prove connection identity (`:971`).

The sole guard SQL is `SELECT current_database()` (`schema.py:1061`), reading
the connection's database identity with no application table, column, sequence
or file mutation. It does not commit/rollback or create a transaction boundary;
the caller owns its connection mode. Refusal must precede caller SQL such as
reap, ensure_schema and TRUNCATE (`tests/pg_fixtures.py:427`). A permitted name
is not proof the caller owns the database or has user permission to destroy it.

## 4. Shipped producers

`coordination_proof._disposable_bank(dsn)` receives the doctor's explicitly
selected disposable-fixture DSN, parses it, requires a nonempty dbname, then
generates `pseudolife_memory_test_proof_<uuid hex>` (`coordination_proof.py:29`).
CLI-DOCTOR owns its argv and lifecycle; this dossier owns only shared admission.

The following are **developer test/evaluation producers**, not daemon requests:
`tests/pg_fixtures.py:112` sends a configured database string to
`refuse_production_database`; `:427` sends the connected psycopg connection to
`assert_disposable_database`. `evals/rust_baseline/daemon.py:40,53` verifies its
owned generated database is also the actual connected name. Other reset sites
are enumerated by the original static guard test
`tests/test_disposable_database_guard.py:643`; reconcile that inventory rather
than inventing a new list of authorized prefixes.

## 5. Python incidentals to defer

Do not copy libpq parser quirks into an ad hoc string splitter
(`schema.py:943`). If native DSN admission is narrower, name the unsupported
grammar and refuse before destructive work. The Rust-side canonical DSN
domain is a separate port contract; arbitrary service/default-database
resolution must not silently become a guessed production name.
Exception-class internals are Python mechanics, but fail-closed category,
normalization and first-statement ordering are behavioral safety pins.
No prefix allowlist substitution is authorized by this dossier.

## 6. Oracle tests

Behavioral pins:

- `tests/test_disposable_database_guard.py::test_assert_disposable_refuses_the_default_bank`
- `tests/test_disposable_database_guard.py::test_a_trailing_slash_does_not_launder_the_bank_name`
- `tests/test_disposable_database_guard.py::test_dsn_database_name_parses_like_libpq`
- `tests/test_disposable_database_guard.py::test_a_service_beats_pgdatabase_so_the_name_is_unknown`
- `tests/test_disposable_database_guard.py::test_an_implicit_daemon_dsn_fails_every_check_closed`
- `tests/test_disposable_database_guard.py::test_an_explicit_empty_dbname_is_the_user_name_not_pgdatabase`
- `tests/test_disposable_database_guard.py::test_an_unresolved_record_fails_every_check_closed`
- `tests/test_disposable_database_guard.py::test_every_harness_guard_allows_a_replay_copy`

Implementation/inventory pins:

- `tests/test_disposable_database_guard.py::test_every_reap_and_full_bank_truncate_is_guarded_first`
- `tests/test_disposable_database_guard.py::test_conftest_removes_the_daemon_dsn_and_keeps_its_bank_name`
- `tests/test_bench_production_port_guard.py::test_both_harnesses_share_variable_names`

The last file concerns benchmark endpoint isolation, a related but separate
guard; a passing name guard does not prove a safe port choice. Tests were not
run for this dossier. Existing Python tests remain immutable.

## 7. Proposed harness cases and mutants

1. Pure guard vectors: default/custom production names, mixed case, trailing
   slash, unrelated replay-copy name, recorded identity and unresolved
   sentinel; observe refusal category and no attempted connection/reset.
2. DSN URI/keyword/percent-encoded/query-dbname, explicit-empty, no dbname with
   PGDATABASE, and service cases; compare the Python resolved identity and
   refusal without saving raw credentials.
3. Stub a connected server name that differs from the configured DSN; record
   SQL ordering and reject if reap/DDL/truncate is attempted before the guard.
4. End-to-end owned disposable proof: compare all pre/post rows and cleanup;
   its generated name must not grant permission to reset an unrelated bank.

Six mutants: compare case-sensitively; retain trailing slash; ignore unresolved
snapshot; infer dbname from user; guard only configured DSN; move actual-server
check after connection reap. Each must fail a specific refusal/ordering case.

## 8. Dependencies and risks

PG-HYDRATE and all write harnesses need this admission before their fixture
DDL. CLI-DOCTOR owns its proof, while OPS-MAINTENANCE has distinct operator
gates and does not automatically inherit a reset guard. The recorded run
identity exists because tests clear the daemon DSN; losing that snapshot can
hide a custom production bank (`schema.py:1011`). Server identity is necessary
but not an ownership lock. This documentation grants no live-bank mutation.

## 9. Effort estimate

**Small.** The shared pure-name/server check is bounded; the main proof is
showing every destructive caller executes it first and refuses unresolved state.
