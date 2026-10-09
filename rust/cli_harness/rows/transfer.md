# CLI-TRANSFER-DSN: `pseudolife-mcp export` / `import` on an explicit DSN

Python oracle: `pseudolife_memory/transfer_cli.py` (line numbers below), plus
`backup_cli._default_data_dir` (180-188), `storage/embedded_pg.py`
`available` (65-71) and `default_lite_data_dir` (74-96), and
`storage/schema.py` `ensure_schema` (1145), `_backfill_trace_invalidations`
(1072-1088), `_EXPECTED_EMBEDDING_DIM` (1069), `SCHEMA_META_VERSION` (20, = 55).
Native leaf: `rust/shim/src/cli/transfer.rs` and `transfer/{export,import,json,sql}.rs`.

## Exact contract

Argv and resolution (`run_transfer` 579-629):

- Canonical argv only: export `[--out P] [--data-dir D]`, import
  `ARCHIVE [--force] [--data-dir D]`, any order, each option once, values
  non-empty and not dash-led. `--help` alone prints the captured argparse help
  (only at `COLUMNS=80`). Every other shape defers generically.
- `PSEUDOLIFE_MCP_DATABASE_URL` set and non-empty selects the explicit-DSN
  path (571). Empty or unset: `data_dir = --data-dir or _default_data_dir`
  (602, backup_cli 180-188); a bank marker `<data_dir>/embedded_pg/PG_VERSION`
  (574) defers by name (`mode '<m>' on the embedded lite tier is deferred ...
  (needs native embedded_pg)`), otherwise the "no database configured" refusal
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

Import (`perform_import` 290-360). `ensure_schema` (316) is not run; it
changes nothing only when all of these hold, and the leaf defers otherwise:
`meta.schema_version` is 55 and every rostered table exists; `entries.embedding`
is vector(1024) or undimensioned; the role owns (or has USAGE in the owner of)
every public table and has CREATE on `public` (its ALTER/CREATE INDEX need
ownership); and no `facts`/`world_facts`/`lessons`/`edges` row has a NULL
`tx_time`, `valid_time` or `writer_id` (its v11 block backfills those and
commits before the emptiness check; schema.py:361-376). A 5-second ACCESS
EXCLUSIVE probe of every public table stands in for its DDL locks. The
harness proves the no-op for fresh banks (the dump diff of catalog shape and
rows) and the deferral for a non-empty bank holding daemon-written edges.

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
  one time zone replays in another.
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
- The lock probe takes ACCESS EXCLUSIVE on every public table for up to 5 s,
  also under `--force` beside a running daemon; the oracle's ensure_schema
  takes the same class of lock on the tables it alters.
- The ownership check could not be exercised by the harness: the test login
  cannot create a second role.

## Deferrals and named substitutions

- Roster guard: export defers before any effect when `meta.schema_version`
  is not this build's (55), or when any public table is in neither
  `EXPORTED_TABLES` nor `EXCLUDED_TABLES`. Python exports whatever its own
  build's roster names, so a native build paired with a newer bank (schema
  v56 adds `reference_chunks`) would otherwise drop a table without a word.
  Cases `export-other-schema-defers`, `export-unknown-table-defers` (watched
  RED on the unguarded binary); mutants `transfer-schema-guard`,
  `transfer-roster-guard`.
- Generic deferral (dispatcher line, exit 1, nothing changed): non-canonical
  argv/paths; unreadable, encrypted or duplicate-member archives, or members
  neither stored nor deflated; manifest that is not a UTF-8 JSON object;
  archive values outside the export's own value domain or that would fail to
  load (strings for numeric/bool columns, dict/list for a non-jsonb column,
  ints out of range, float4 overflow/underflow, NUL, NaN in jsonb, lone
  surrogates, ints over 4,300 digits, nesting over 200, uuid/vector/
  timestamptz strings the server refuses); export of a bank with a missing
  rostered table or an unported column type, a non-UTF8 server, an
  out-of-domain timestamptz or jsonb value, or an existing `<out>.part`;
  import into a non-UTF8 server, a bank whose tables the role does not own,
  or a bank with NULL v11 stamps; DSN not understood or connection failure; an
  ensure_schema lock that the probe cannot take.
- `import into a bank not at this build's schema (55) is deferred ... (needs
  native ensure_schema)`: target without the rostered tables, or with
  schema_version other than 55 (older or newer).
- `mode '<m>' on the embedded lite tier is deferred ... (needs native
  embedded_pg)`.
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
