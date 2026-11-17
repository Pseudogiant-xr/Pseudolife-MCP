# CLI-TRANSFER-DSN: native export and import

The implementation is `rust/shim/src/cli/transfer.rs` and
`transfer/{export,import,json,sql,schema}.rs`. Neither command defers an input
to another implementation. `test_transfer_native.py` pins native admission,
refusal messages, unchanged state on refusal and successful imports.
`cli_transfer_contract` covers the process boundary without PostgreSQL.

## Arguments and paths

Both commands accept `-h` and `--help` at any terminal width. Options accept
separate values and `--option=value`; repeated values use the last value,
repeated `--force` is harmless, and `--` ends option parsing. Abbreviated or
unknown flags, missing values and invalid Unicode get explicit refusals.
Filesystem paths use the operating system's spelling rules, including `.`
components and forward slashes on Windows. An explicit database URL does not
require a home directory or a lite data directory.

`PSEUDOLIFE_MCP_DATABASE_URL` selects the database. Missing configuration and
retired lite banks retain their existing refusals. Unsupported DSN options,
invalid configuration, certificate failures and unavailable connections are
reported without echoing the DSN or credentials. Native connection options
are preserved. The default export filename uses the native local clock.

## Export

One repeatable-read, read-only snapshot validates the table roster, schema
version, column types and JSON values before creating an archive. Unknown
public tables, missing exported tables and unsupported column types are
refused rather than silently omitted. Non-UTF8 databases are refused.
An older bank needs a matching daemon upgrade; a newer bank needs a newer
client. A missing or invalid schema stamp requires restoring a valid bank.
Database lock waits are bounded to five seconds and refusals include SQLSTATE.

The snapshot sets UTC and ISO timestamp rendering locally. Finite timestamps
in years 1–9999 retain their ISO spelling; infinity, negative infinity and
extended years retain PostgreSQL's portable timestamp input spelling. JSON
nesting beyond 200 levels, integers beyond 4300 digits and unpaired Unicode
surrogates are refused with the relevant table and column.

The archive contains the exported tables in roster order and `manifest.json`.
Existing metadata skip policy and exclusion of operational tables remain.
An existing `<out>.part` is refused without overwriting or removing it.
Directory destinations and inaccessible parents name the filesystem problem.

## Import and transaction ownership

ZIP structure, encryption, compression, duplicate names, UTF-8 decoding,
manifest fields and schema-independent JSONL validation are checked before
connecting. UTF-8 manifests may have a BOM. Archives use Stored or Deflated
members. Malformed input names its member or field and the repair step.

The connection guard runs before schema initialization. The transfer-local
schema adapter interprets the daemon's generated schema artifacts; it does
not change shared schema behavior or the schema version. Schema initialization,
target-type validation and row insertion now share one transaction. A refusal
rolls back schema repairs as well as rows. A fresh database remains supported.
The server checks UUID, vector and timestamp input against the actual column
types before row insertion. Unsupported value/type combinations identify the
column; a PostgreSQL cast refusal also includes SQLSTATE.
Only data-exception SQLSTATEs blame archive values. Other database failures
name the failed operation and advise retrying. The adapter restores the
session's timeout settings immediately after the schema plan, so its
30-second statement limit does not cap the data load. The nonempty-target
check runs under the import locks before target-dependent value validation.

Schema DDL locks last until that transaction ends. With `--force`, connected
clients can wait on reads as well as writes for the whole import. Without
`--force`, other connected clients are refused before taking these locks.

A server ERROR at COMMIT reports rollback. A lost or fatal COMMIT response
retains the unknown-outcome diagnostic and instructs the operator to inspect
the target bank before retrying. It does not claim that rollback occurred.

## Validation and historical fixtures

Run native acceptance with `PSEUDOLIFE_RUST_BINARY` naming the built client:

```sh
python -m pytest rust/cli_harness/test_transfer_native.py -q
cargo test -p pseudolife-stdio --test cli_transfer_contract
cargo test -p pseudolife-stdio --lib cli::transfer
python rust/cli_harness --row transfer --golden --candidate <binary>
```

Database tests use disposable databases and the restricted test login.
Committed goldens retain six cases: export/import help, no-database refusals,
and float-tie round trips. Update only deliberately changed snapshots with
`--record-candidate`, following the harness README. The historical live
Python cases remain fixtures; their old deferral expectations are not the
native contract. Native tests replaced the superseded deferral expectations;
the remaining source mutants were retired on 17 November 2026.

Import reads each member in full; its memory use is bounded by the largest
member. Supporting additional compression methods, non-UTF8 databases,
unclassified tables or additional PostgreSQL column types remains follow-up
work. Each currently receives an explicit refusal.
