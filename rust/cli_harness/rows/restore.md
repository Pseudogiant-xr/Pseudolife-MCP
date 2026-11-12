# CLI-RESTORE-GATES contract (`restore`, the gates before Docker)

Oracle: `pseudolife_memory/restore_cli.py`, `docker_backup_restore.validate_state`
and `update_cli._dump_complete` / `interactive` at origin/master 01fba4b6,
under CPython 3.11 (the CI interpreter). Candidate:
`rust/shim/src/cli/restore_cli/`. Harness row: `restore`
(`rust/cli_harness/rows/restore.py`). Rust contract test:
`rust/shim/tests/cli_restore_contract.rs`.

## Scope

`restore` replaces a Docker bank. Everything after the confirmation gate
(the update lock, `DockerBank`, the safety backup, the drop and replay;
CLI-RESTORE-DOCKER) stays deferred. This row ports what the command decides
before any of that, in the oracle's order:

1. `cli.main` dispatches `restore` with `sys.argv[2:]` (`cli.py`).
2. argparse (`main`): help, or a usage error, exit 0 or 2.
3. `_dump_complete(--backup-file)`; false: `restore failed: the backup is
   unreadable or incomplete; use a complete .sql.gz dump. Nothing was
   restored`, exit 2.
4. With `--state-archive`, `validate_state`; any refusal: `restore failed:
   the state archive is unreadable or contains unsafe paths; use a complete,
   unmodified state backup. Nothing was restored`, exit 2.
5. Without `--yes`: when stdin or stdout is not a terminal, `restore
   failed: restore replaces the existing bank; rerun with --yes to confirm.
   Nothing was restored`, exit 2. At a terminal the oracle prompts.

## Exact contract items

| Item | Python source |
|---|---|
| `--help` / `-h`: the help on stdout, exit 0, when argparse wraps at 80 columns (`COLUMNS=80`, or no `COLUMNS` and no terminal width) | `main`'s parser |
| Usage errors, exit 2: the missing `--backup-file`; a missing value; an invalid `--db-port` (`int()`), quoted with `repr`; an explicit argument to a flag; an ambiguous `--` prefix; unrecognized arguments; argparse's order (classification and ambiguity, then left-to-right actions, then the required check, then the unrecognized ones) | argparse 3.11.9 `_parse_optional`, `consume_optional`, `parse_args` |
| A value after an option may be a negative number (`-1`, `-.5`) or `-`: argparse takes it as the value | `_negative_number_matcher` |
| Repeated options: the last value wins; `--opt=value` | argparse |
| Paths are pathlib's spelling (`dump.sql.gz/` and `./dump.sql.gz` name the file, `''` is `.`) | `type=Path` |
| The dump: every gzip member read as Python's `gzip` reads it (padding, trailing bytes, truncation); lines split on `\n`, `\r\n` and a lone `\r`; the marker counts only outside `COPY ... FROM stdin;` data, with `\S` read as Python's `str.isspace` complement | `_dump_complete`, `gzip._GzipReader` |
| The state archive: the gzip check, then members as CPython 3.11 `tarfile` reads them (`frombuf`, `nti` with Python `int(..., 8)`, both checksums, the ustar prefix, the V7 trailing-slash directory, a bad or short header after the first ending the archive quietly, a member's data present to the end of its last block, non-regular supported types skipping no data, unknown types skipping theirs), then every `validate_state` check: absolute, `..` or backslash names, devices and FIFOs, absolute or backslash link targets, escaping links through the final and the transient link graph, and a link loop ending the walk | `validate_state` |
| The confirmation gate: on Windows a character device counts as a terminal (the CRT's `isatty`, so `NUL` does) | `update_cli.interactive` |
| Streams: Python text streams, CRLF on Windows | `print(..., file=sys.stderr)` |

## Deferred (the dispatcher's `mode 'restore' is deferred` line, exit 1)

- Every argv that passes the gates with `--yes`: the oracle takes the
  update lock and drives Docker (CLI-RESTORE-DOCKER).
- Without `--yes` at a terminal (stdin and stdout both terminals, or on
  Windows character devices): the oracle prompts.
- Argparse shapes: abbreviations (`--back`), `--`, single-dash arguments
  other than `-h`, `-` and negative numbers (option clusters such as `-hx`,
  `-h=x`), non-Unicode argv, non-ASCII in a single-dash argument or a
  `--db-port` value (Python reads Unicode digits and spaces there), any
  error message that would hold non-ASCII text, and help or usage at any
  other wrap width.
- A deflate error in either file: the oracle's `zlib.error` escapes both
  checks and it prints a traceback (exit 1).
- A gzip CRC or length mismatch in either file. The oracle refuses it
  (`BadGzipFile`), but miniz_oxide, unlike zlib, decodes a back-reference
  before the start of a member's output, so a stream zlib stops on with a
  traceback reaches the trailer here with the wrong bytes; this leaf cannot
  tell the two apart.
- State archives with PAX headers (`x`, `g`, `X`), GNU long names or links
  (`L`, `K`), sparse members (`S`), a negative size on a member that skips
  data, a link walk deeper than 400 levels or longer than 50 million steps
  (CPython's recursion limit decides there).

## Declared divergence

A stream holding a corruption miniz_oxide decodes and zlib rejects (a
back-reference before the member's start) that is also cut off before its
trailer: the oracle prints its `zlib.error` traceback (exit 1), this leaf
refuses the file as incomplete (exit 2). Both stop before any effect. Not
in the harness: no fixture is known whose two outcomes are this pair.

## Named rules

- `restore-oracle-not-run`: argv that passes every gate runs a verified
  stub `pseudolife_memory.cli` (exit 0, a marker on stderr, no docker call,
  fixtures intact), whose observation becomes the deferral.
- `restore-declared-deferral`: the oracle answers before any Docker command
  (exit 0 or 2, no docker call, fixtures intact) where this leaf defers by
  design (including `dump-bad-crc` and `state-bad-crc-gzip`); the
  expectation becomes the deferral.
- `restore-zlib-traceback`: the oracle's uncaught `zlib.error` traceback
  (exit 1, nothing on stdout, last line `zlib.error: Error -3 while
  decompressing data: ...`, no docker call) becomes the deferral.

## Safety of the harness

The oracle runs only on argv that stops at a gate. Every case runs with
`PATH` holding only a recording stand-in `docker` (also named by
`PSEUDOLIFE_DOCKER`) that refuses every call, and core's program preflight
proves no other `docker` resolves; the disposable home is the per-user
directory, so neither the update lock nor a safety backup could land in a
real one. Its call log is compared and is empty in every case. Fixture
files (dumps and archives, built byte by byte or with Python's `gzip` and
`tarfile`) are checked unchanged after each arm and then removed, so the
goldens hold no compressor-dependent bytes. The Rust contract test scans
the leaf's non-test source for process, network, lock and write APIs.

## Mutants

22 source mutants, one per decision the row pins (exit code, `--yes`, the
state check and its order after the dump check, argparse's error order and
negative numbers, the 80-column guard, the COPY pattern, lone `\r`, gzip
padding, the CRC check and its deferral, the zlib deferral, tar header and data handling, the
transient pass, FIFOs, loop handling, the ustar prefix, the signed checksum
and `int`'s base prefix); each is caught by a named case.
