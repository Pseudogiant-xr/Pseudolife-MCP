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
  marker is exact for both installs.
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
  1e-4 / 1e16 exponent window, two-digit exponents); bool; jsonb parsed and
  re-serialized with Python's separators, key order as decoded, ints as
  `str(int)`, floats as repr (overflow to `Infinity`); timestamptz as
  `isoformat()` (6-digit micros when non-zero, `+00:00`); NULL as `null`.
- `manifest.json` = `json.dumps(manifest, indent=2)` (228-239): format 1,
  created_at, schema_version (decoded jsonb), embedding_dim (atttypmod > 0,
  else null; 182-190), pseudolife_version, counts in table order,
  excluded_tables (87-108).
- `out.parent.mkdir(parents=True)`, write `<out>.part`, then replace `out`
  (208-243).

Import (`perform_import` 290-360), only into a bank whose
`meta.schema_version` is already 55 with every rostered table present and
`entries.embedding` at 1024 or undimensioned (so `ensure_schema`, 316, is a
no-op; proven by the dump diff of catalog shape and rows):

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
- The default archive name's timestamp: rule `transfer-default-name`.
- Server-side cursor names, fetch batch sizes and statements per message.

## Deferrals and named substitutions

- Generic deferral (dispatcher line, exit 1, nothing changed): non-canonical
  argv/paths; unreadable, encrypted, duplicate-member or non-deflate
  archives; manifest that is not a UTF-8 JSON object; archive values outside
  the export's own value domain or that would fail to load (dict/list for a
  non-jsonb column, ints out of range, float4 overflow/underflow, NUL, NaN in
  jsonb, lone surrogates, ints over 4,300 digits, nesting over 200); export
  of a bank with a missing rostered table or an unported column type; a
  timestamptz value outside a UTC session / ISO DateStyle / years 1-9999;
  DSN not understood or connection failure; an ensure_schema lock that a
  5-second ACCESS EXCLUSIVE probe cannot take.
- `import into a bank below the current schema is deferred ... (needs native
  ensure_schema)`: target without meta/roster tables or schema_version != 55.
- `mode '<m>' on the embedded lite tier is deferred ... (needs native
  embedded_pg)`.
- `<mode> failed (native-pg-diagnostics); nothing was committed`: a
  PostgreSQL error after the work began (Python prints a traceback). Import
  rolls back exactly as the oracle does; export removes `.part` and keeps the
  directories it made, as the oracle does.
