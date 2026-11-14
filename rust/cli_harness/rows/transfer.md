# CLI-TRANSFER-DSN: `pseudolife-mcp export` / `import` on an explicit DSN

Python oracle: `pseudolife_memory/transfer_cli.py` (line numbers below), plus
`backup_cli._default_data_dir` (180-188), `storage/embedded_pg.py`
`available` (65-71) and `default_lite_data_dir` (74-96), and
`storage/schema.py` `ensure_schema` (1145), `_backfill_trace_invalidations`
(1072-1088), `_EXPECTED_EMBEDDING_DIM` (1069), `SCHEMA_META_VERSION` (currently 58).
Native leaf: `rust/shim/src/cli/transfer.rs` and `transfer/{export,import,json,sql,schema}.rs`.

## Exact contract

Argv and resolution (`run_transfer` 579-629):

- Canonical argv only: export `[--out P] [--data-dir D]`, import
  `ARCHIVE [--force] [--data-dir D]`, any order, each option once, values
  non-empty and not dash-led. `--help` alone prints the captured argparse help
  (only at `COLUMNS=80`). Every other shape defers generically.
- `PSEUDOLIFE_MCP_DATABASE_URL` set and non-empty selects the explicit-DSN
  path (571). Empty or unset: `data_dir = --data-dir or _default_data_dir`
  (602, backup_cli 180-188); a bank marker `<data_dir>/embedded_pg/PG_VERSION`
  (574) is refused plainly as a retired lite-tier bank (`<mode> refused: <dir>
  holds a lite-tier bank ...`, exit 1), otherwise the "no database configured" refusal
  on stderr, exit 1 (604-611). An existing lite default dir is the data dir
  (pg0 installed) or irrelevant (pg0 absent: never attached), so checking its
  marker is exact for both installs. Both outcomes are decided before path
  admission, as in the oracle: `import ./x.zip` without a DSN gets the
  no-database refusal, not a path deferral.
- With a DSN and no data dir, the oracle still evaluates `_default_data_dir`
  (home, then `Path.cwd()`); an unresolvable home or cwd defers.
- Printed paths are `str(Path(arg))`: only spellings where that equals the
  argument are ported (Windows: drive-absolute or relative backslash paths,
  no `/`, `:`, `.` or empty components, trailing dot/space or device names;
  POSIX: no empty or `.` components). Others defer.
- Default `--out`: `<cwd>/pseudolife-export-<local %Y%m%d-%H%M%S>.zip`
  (614-615); on Windows with `TZ` set it defers (MSVCRT honours TZ).
- Success stdout: `exported to: <path>` / `imported: <archive>`, then
  `  <table>: <n>` for each non-zero count in count order (617-623).
  Refusals: `<mode> refused: <message>` on stderr, exit 1 (624-626).
- A refused stdout: the report (and argparse's help) waits in CPython's
  stdout buffer, so the export or import completes and the
  interpreter-shutdown flush then fails with exit 120; the native leaf
  exits 120 too. Cases `export-help-stdout-closed`, `export-stdout-closed`,
  `import-stdout-closed` (live only, rule `python-stdout-closed-trailer`).

Export (`perform_export` 205-244, `_export_table` 247-276):

- One `REPEATABLE READ READ ONLY` transaction with `SET LOCAL
  extra_float_digits = 3` (213-221); `schema_version` read from meta (222).
- Members in `EXPORTED_TABLES` order (67-82), then `manifest.json`; each
  table `SELECT * ... ORDER BY 1` (262); meta rows whose key is in
  `_META_SKIP_KEYS` or ends `_schema_version` are skipped (119-138, 267);
  `outcome_signals.used_ids` is dropped (113, 269-270).
- Each line is `json.dumps(rec, default=_json_default, ensure_ascii=False)`
  of psycopg's text-format decoding (271-272): text/uuid/vector as JSON
  strings with Python escaping; int verbatim; float4/float8 parsed to a
  double then `float.__repr__` (`NaN`, `Infinity`, `-Infinity`, `-0.0`, the
  1e-4 / 1e16 exponent window, two-digit exponents, and on an exact decimal
  tie at the shortest length the even digit: `562949953421312.25` is
  `562949953421312.2`; cases `export-float-ties`, `import-float-ties`, a
  meta value written by `PostgresStorage.set_meta`); bool; jsonb parsed and
  re-serialized with Python's separators, key order as decoded, ints as
  `str(int)`, floats as repr (overflow to `Infinity`); timestamptz as
  `isoformat()` (6-digit micros when non-zero, `+00:00`); NULL as `null`.
- `manifest.json` = `json.dumps(manifest, indent=2)` (228-239): format 1,
  created_at, schema_version (decoded jsonb), embedding_dim (atttypmod > 0,
  else null; 182-190), pseudolife_version, counts in table order,
  excluded_tables (87-108).
- `out.parent.mkdir(parents=True)`, write `<out>.part`, then replace `out`
  (208-243).
- Every deferral is decided inside the snapshot before the first filesystem
  effect: server encoding UTF8; each non-null timestamptz value needs a UTC
  session and ISO DateStyle and a year in 1-9999 (checked in SQL); every
  jsonb document must decode under the leaf's JSON domain (a pre-pass over the
  jsonb columns); `<out>.part` must not already exist (the oracle would
  truncate and remove a file this run did not create).

Import (`perform_import`): connection guard first, then native ensure_schema.
`transfer/schema.rs` interprets the daemon's generated `schema_plan.json`,
`schema.sql` and `schema_meta.rs`; it maintains no separate DDL and changes no
schema version. Statement order, simple versus typed protocol, version and
embedding guards, one-time repairs and healing use that existing plan.
Schema initialization commits before dimension/emptiness/archive checks, as
Python does; a refused nonempty import can therefore leave schema repairs
committed while importing no archive rows. PostgreSQL errors have native safe
diagnostics; future/malformed-version refusals retain the oracle's guard text.

- Manifest refusals before connecting (299-308): missing `manifest.json`;
  `format_version != 1` with its Python repr (None/False/ints/floats/strings).
- Connection guard first (315, 363-375) with its exact count message;
  `--force` passes it.
- Dimension refusal after ensure_schema (317, 392-401), `str(exported)`.
- One transaction: `SET LOCAL lock_timeout = '5s'`, `LOCK TABLE <exported>
  IN EXCLUSIVE MODE` (323-326); emptiness check of every exported table but
  meta/relations with its exact `t=n` detail in table order (327, 378-389);
  `DELETE` of `curation_listing_spelling_v2` (328-329).
- Members in table order (330-350); meta rows by key/value with skip keys and
  `json.dumps(value)::jsonb` upserts, unknown-column refusal (404-427); other
  tables: unknown-column refusal with `sorted()` list repr (497-504), legacy
  `dream_state = None` when manifest schema < 38 (345-349, 491-496),
  relations inserted with `inverse_of` NULL and `ON CONFLICT (name) DO
  NOTHING`, inverses updated afterwards (455-456, 505-507, 515-519), rows
  grouped by sorted key set and flushed every 500 rows (461-514) with
  psycopg's parameter typing (str untyped; int int2/int4/int8/numeric by
  range; float float8; bool; jsonb via `json.dumps`; `::vector`,
  `::jsonb`, `::timestamptz` placeholders 430-445). Counts are affected rows.
- Before the transaction a dry pass runs the same loop without writing: it
  finds the oracle's first refusal and defers any value the export would not
  write for the column type (strings only for text/uuid/vector/timestamptz),
  and casts every uuid/vector/timestamptz string with the server's input
  function, so the transaction meets no value error. Archives with stored or
  deflated members are read; the rest defer.
- `_backfill_trace_invalidations` only when the member is absent, its count
  appended last (356-358); `_advance_sequences` including the entries
  high-water over invalidations and reinstatement decisions (523-558).

## Free items

- ZIP container bytes (compression level, timestamps, zip64 extras, data
  descriptors): member names, order and decompressed contents are the
  contract. Python's zipfile reads every native archive in the harness.
- `manifest.created_at` (validated UTC within the arm's window) and
  `pseudolife_version` (the oracle reports its installed distribution, the
  candidate its crate version): rule `transfer-zip`.
- The default archive name's timestamp: rule `transfer-default-name`,
  validated with the arm's own recorded UTC offset, so a golden recorded in
  one time zone replays in another. The exact `pseudolife-export-YYYYMMDD-HHMMSS.zip`
  filename shape is required; its timestamp is replaced in stdout, file keys
  and mode keys, while contents and mode values remain exact.
- An import case's input archive, which stays in the home, compares as
  members too (rule `transfer-zip`): its container bytes and version string
  are the setup's. The oracle-built input archives have `created_at` pinned
  (import never reads it), and the tie bank's built-in relations, which
  `PostgresStorage` stamps with the wall clock when it opens, are pinned to
  1000.0, so every input is the same on every run and host.
- Server-side cursor names, fetch batch sizes and statements per message.

## Declared limits

- Import reads each member whole, twice (dry pass, then transaction); memory
  is bounded by the largest member, not streamed.
- Native ensure_schema takes the existing generated plan's DDL locks under
  its five-second lock timeout, including when `--force` admits another connection.
- The ownership check could not be exercised by the harness: the test login
  cannot create a second role.

## Deferrals and named substitutions

- Roster guard: export defers before any effect when `meta.schema_version`
  is not this build's (58), or when any public table is in neither
  `EXPORTED_TABLES` nor `EXCLUDED_TABLES`. Python exports whatever its own
  build's roster names, so a native build paired with a newer bank (one
  whose schema adds a table) would otherwise drop a table without a word.
  Cases `export-other-schema-defers`, `export-unknown-table-defers` (watched
  RED on the unguarded binary); mutants `transfer-schema-guard`,
  `transfer-roster-guard`.
- Generic deferral (dispatcher line, exit 1, no archive rows imported): non-canonical
  argv/paths; unreadable, encrypted or duplicate-member archives, or members
  neither stored nor deflated; manifest that is not a UTF-8 JSON object;
  archive values outside the export's own value domain or that would fail to
  load (strings for numeric/bool columns, dict/list for a non-jsonb column,
  ints out of range, float4 overflow/underflow, NUL, NaN in jsonb, lone
  surrogates, ints over 4,300 digits, nesting over 200, uuid/vector/
  timestamptz strings the server refuses); export of a bank with a missing
  rostered table or an unported column type, a non-UTF8 server, an
  out-of-domain timestamptz or jsonb value, or an existing `<out>.part`;
  DSN not understood or connection failure. Import DDL permission/lock failures
  now produce a native failure after rolling back schema initialization;
  NULL stamps and missing tables are repaired by the shared generated plan.
- `<mode> refused: <dir> holds a lite-tier bank (embedded PostgreSQL) ...`
  (the shared lite-tier refusal, `rust/shim/src/cli/retired.rs`).
- `import lost the answer to its COMMIT (native-pg-diagnostics); it may
  have been committed: inspect the target bank before retrying`, exit 1:
  the COMMIT's answer was FATAL or PANIC, or never came (the connection was
  lost), so the import may have become durable. The oracle raises out of
  `conn.transaction()` there (a traceback, exit 1) and claims nothing either
  way; this line says the outcome is open rather than borrowing the certain
  one below. Only a server ERROR at COMMIT, which rolls the transaction
  back, keeps that certain line. Cases `import-commit-refused-fails` (a
  deferred constraint trigger raises at COMMIT) and
  `import-commit-lost-unknown` (it ends its own backend there), rules
  `expect-import-commit-refused` / `expect-import-commit-unknown`, mutants
  `transfer-commit-error-uncertain`, `transfer-commit-fatal-certain`.
- `<mode> failed (native-pg-diagnostics); nothing was committed`: a
  PostgreSQL error after the work began, or an export value the pre-checks
  should have deferred (Python prints a traceback). Import rolls back exactly
  as the oracle does (a constraint violation is the reachable case); export
  removes its own `.part` and keeps the directories it made, as the oracle
  does.
- `export failed (native-io-diagnostics); no archive was written`: a
  filesystem failure after export began writing (creating directories or
  `.part`, writing, or the final replace, which leaves `.part` as the oracle
  does). Python prints a traceback.

## Hosted CI and goldens

Both Parity lanes run every case live on the disposable PostgreSQL that the
lease step provisions (pg0, with the test login). The goldens
(`goldens/transfer.<os>.json`) hold a subset: `export-help`, `import-help`,
`export-no-database`, `import-no-database`, `export-float-ties` and
`import-float-ties`. The whole row recorded is 14 MB, mostly each case's
full bank dump. The others are marked live-only (`Case.golden` False,
`GOLDEN_CASES` in `transfer.py`), so `--record` and `--golden` leave them
out.

## Native schema acceptance (B10b)

Cases now compare successful imports into blank databases, older stamped
banks and banks missing a table. NULL v11 stamps are repaired before the
nonempty-bank refusal. A blank database with another connection checks that
the connection guard still precedes DDL. Future, string, null, boolean and zero
schema stamps exercise refusal without mutation; a held table lock exercises
DDL rollback. The existing complete catalog/row/sequence dumps compare both
arms. The NULL-stamp fixture pins its oracle writer's assertion time before
either arm, rather than discarding a database difference.

`transfer-schema-refusal` substitutes only a checked Python RuntimeError for
the native schema refusal line and requires the oracle bank to be unchanged.
`transfer-schema-lock-failure` recognizes the specific LockNotAvailable
traceback; full database dumps remain compared. The generic import failure
line says archive rows were not committed, since schema initialization can
already have committed separately. Lost COMMIT answers remain uncertain.

No Cargo or shared dispatcher integration is required. The shim includes the
same generated schema artifacts directly; the existing schema-version pin now
checks that source relationship instead of obsolete import deferral literals.

Linux native compilation and all 69 differential cases passed. The NULL-stamp
case initially differed because fixture construction used the wall clock;
pinning the oracle writer's fixture timestamp made the full database snapshots
match without discarding any stamp. Native Windows transfer contracts passed.

```sh
cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio cli::transfer
python rust/cli_harness --row transfer --candidate "$CARGO_TARGET_DIR/debug/pseudolife-stdio" --out /tmp/transfer.json
python rust/daemon/harness/gen_schema_sql.py --check
```

New bank cases remain live-only. Linux validation uses a private allocator
adapter that prefixes disposable database names with `r1_` and retains the
original labels for normalization. No shared or production bank is involved.
Windows stress selection: `row:transfer` (20 hosted runs for connection,
lock and commit paths). All six Linux goldens were refreshed from the verified oracle at `7dcd738f`
and replayed. A Python-free Rust image proof initialized a blank bank through
native import. Five Windows live schema cases and all six goldens passed; hosted stress
remains pending.
