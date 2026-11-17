# CLI-CONDUCTOR-WATCH-SETUP contract (`conductor-watch`, before its lock)

Oracle: `pseudolife_memory/conductor_watch_cli.py` (`main` up to the
state's `OsLock`, with `lease_cli.parse_duration` / `_pid_alive` and
`coordination_identity.resolve_digest_path`), and `cli.py`'s dispatch of
`conductor-watch` and its deprecated name `delegate-watch`, with the
conductor-watch defect fixes (changelog fragment
`2026-11-13-conductor-watch-defects`), CPython 3.11.9. Candidate: `rust/shim/src/cli/conductor_watch/`
(argparse subset in `args.rs`), reusing `cli/wait_mail.rs` (`digest_path`,
`pathlib`), `cli/lease/hold.rs` (`pid_alive`) and `cli/pyjson.rs`. Harness
row: `conductor_watch` (`rust/cli_harness/rows/conductor_watch.py`). Oracle
tests: `tests/test_conductor_watch.py`.

## Scope

`conductor-watch` polls GitHub, the suite results and the suite leases, and
sends its owner board mail when something needs a decision. This row
compares everything `main` does before it takes the state's lock, plus a
start that takes the lock and stops at "no board" (no bearer token, a
closed daemon address). The watch itself runs natively since 2026-11-14
(`start.rs`, `watch.rs`); it is held to its own Rust tests
(`shim/src/cli/conductor_watch/watch_tests.rs`,
`shim/tests/cli_conductor_watch.rs`), not to this row, because past the
lock it works rather than matches.

## What runs, in the oracle's order (checked)

1. `cli.main`: `delegate-watch` prints `DEPRECATED_MODE` on stderr, then
   both names run `main(sys.argv[2:])`.
2. `parser().parse_args`: help exits there; a missing value, an explicit
   argument to a flag, or a refused type (`_positive`, `_non_negative`,
   `_age`, `_state_path`) errors there; words and unknown `--words` collect
   as extras and error once every option is consumed.
3. `resolve_owner`: `--owner` (32 lowercase hex, else its `repr` in the
   refusal), else the `.agent` file beside the session's digest
   (`CLAUDE_CODE_SESSION_ID`, `PSEUDOLIFE_DIGEST_DIR`): a link, a missing
   file, a directory (Windows: `could not be read (Permission denied)`;
   POSIX: `is not a regular file`), a FIFO, a first line (CRs stripped) that
   is not an id. A failure is kept, not raised, until step 6.
4. `state`: `--state`, else `default_state` (beside the digest as
   `.dwatch.json`, else `~/.pseudolife-mcp/watch/<owner>.json`).
5. `--stop`: no state (exit 2); `_running(state)` none (stdout `no watcher
   running`, exit 0); else the empty `.stop` file beside it and stdout
   `asked to stop` (exit 0), or, when that file cannot be written, `cannot
   write STOP (strerror); nothing changed. Make DIR writable, then run
   --stop again` on stderr (exit 2).
6. The owner refusal (exit 2).
7. `_running(state)`: the `.pid` file's record when it parses as an object
   whose `int(pid)` is below 2**31 and alive and whose `process_start` is
   absent, null, or
   the process's own token (Windows creation `FILETIME`, Linux `/proc` stat
   field 22); `_foreign` (stderr, exit 2) or `_already` (stdout, exit 0).
   This holds with `--detach` too.
8. Otherwise `--detach` (not compared here) or the lock: with no bearer
   token, `no board: no bearer token (...)` on stderr, exit 2, leaving the
   empty `.lock` and one `.ledger` line (cases `start-no-board` and
   `start-stale-record-no-board`; rule `conductor-watch-start-no-board`
   blanks the ledger line's timestamp and line ending).

## Exact contract items

| Item | Python source |
|---|---|
| Help at `COLUMNS=80`, stdout, exit 0; usage + `pseudolife-mcp conductor-watch: error: ...` on stderr, exit 2 | `parser`, argparse 3.11 |
| `must be above 0`, `must be 0 or more` (NaN fails both), `invalid _positive value: 'x'`, `not a duration: 'x' (use ...)` (ASCII digits and unit letters only) | `_positive`, `_non_negative`, `_age`, `parse_duration` |
| `float()` of `nan`, `inf`, `infinity` in any case, signed | `_positive`, `_non_negative` |
| `'TEXT' has no file name; give a file path such as ~/.pseudolife-mcp/watch/<owner>.json` when pathlib's `name` of `--state` is empty, `.` or `..` (POSIX: after the last `/`, empty and `.` parts dropped; Windows: `\` and `/`, after an `X:` drive) | `_state_path`, pathlib 3.11 |
| The owner refusals and their `; pass --owner <...>` tail | `resolve_owner`, `main` |
| `no session to find the watcher by; ...`, `no watcher running for STATE; nothing changed`, `ID asked to stop; ...` and the empty `.stop` file | `main`, `_stop` |
| `cannot write STOP (strerror); ...` (exit 2): POSIX the C library's `strerror`; Windows the C runtime's errno text for Win32 codes 2 and 3 (`No such file or directory`), 5 and 19 to 36 (`Permission denied`) and 112 (`No space left on device`), with a hidden or system `.stop` refused as `CREATE_ALWAYS` refuses it | `_stop`, `open` |
| `already running for OWNER; park with ...`, `STATE belongs to the watcher for OWNER; ...` | `_already`, `_foreign` |
| `int()` of the record's pid: ints, bools, floats (truncated; NaN and infinity are none), padded or signed decimal text; at or past 2**31, at or below 0, or anything else, it reads as no watcher | `_running`, `_pid_alive` |
| `str()` of record values (`None`, `repr` of lists and dicts), a lone surrogate written as `?` | `_utf8_stdio`, f-strings |
| Paths as pathlib prints them; `with_suffix` replacing only the last suffix | `pathlib` |
| The deprecation notice before anything else for `delegate-watch` | `cli.main` |

## Native completion (18 November 2026)

Neither command name has a dispatcher deferral. Historical `defer-*` case
names remain stable; their goldens now pin the native answers:

- Help and usage use one fixed layout regardless of terminal, colour or
  `COLUMNS`. Python locale and integer-digit-limit settings are ignored.
  Help and errors act in argument order.
- Abbreviations name the full options; unknown short options, `--` and
  unknown `--word=value` forms give a usage error with `--help` as the remedy.
  Unknown values are not echoed. A value beginning with a dash is accepted;
  if it is a known option, a prefix of one, or a known option with `=`, use
  `--option=VALUE` to disambiguate the literal file name.
- Nonstandard numeric spellings are refused with a plain ASCII decimal
  remedy. `--max-age` has no Python digit limit; a finite positive duration
  is accepted, including long zero-padded numbers, and overflow is refused.
- Windows UNC and device state paths are refused with a local-file remedy.
  Trailing-dot state names and non-Unicode paths, arguments or session ids
  are refused with a corrected-path or Unicode remedy. An empty POSIX HOME
  needs a home directory or an explicit state path.
- A pid string containing underscores or non-ASCII digits, or a pid record
  exceeding 500 nesting levels, is refused without changing the record or
  writing a stop request. Repair the record before retrying. Ordinary ASCII
  pid records retain their existing start-time and lock checks.
- Unreadable session `.agent` files name the read failure and explicit
  `--owner` remedy for a start. An explicit-state stop treats owner-lookup
  failures as an unknown owner and stops the named watcher; missing or
  non-Unicode session ids and unresolved digest paths cannot prevent it.
  Stop-write failures always name the writable-directory
  remedy, with native OS text when no historical errno mapping exists.
- Failed output exits 1; a failed stdout write reports its cause and remedy
  on stderr when possible. A failed alias notice prevents any stop request.

Process start times are native on Windows, Linux and macOS. An unqueryable
process falls back to the existing state-lock proof; it does not defer.
The missing-source case in `watch.rs` and optional owner-record lookups
return absence internally, not a dispatcher deferral.

Previously supported inputs also change deliberately: invalid values in
`--interval=-x`, `--max-age=-5` and `--owner=-abc` are no longer echoed.
A `--max-age` of 309 to 640 nonzero digits is refused as non-finite rather
than clamped to the watcher's maximum lifetime. A stdout failure adds a
cause/remedy line on stderr when possible, and a failed first write exits 1
without a dispatcher deferral. Invalid pid-record errors name the `.pid`
path and ask the operator to restore a valid watcher record before retrying;
they preserve both that record and the process it might identify.

## Declared divergences

- `conductor-watch-notice-unwritten` (case
  `stop-deprecated-name-stderr-closed`): under the deprecated name the
  oracle prints its notice before anything else, so when stderr cannot be
  written it dies in that print before it asks the watcher to stop, with an
  exit status of its interpreter's choosing (1, or 120 from its shutdown
  flush). The candidate writes nothing and exits 1. Both must leave the
  home as setup left it: no stop request.
- Not cased: a `.stop` write refused with a Windows code outside the
  listed ones, under either command name. Both
  refuse with exit 2 and nothing changed; the reason in parentheses is the
  C runtime's errno text in the oracle and Rust's description of the Win32
  code in the candidate. With a listed code (case
  `stop-request-unwritable-deprecated-name`) the bytes match.

The row's own pid files (each arm's sleeper has its own pid and start time)
are checked unchanged after each arm and then taken out of the snapshot;
every other byte of the home is compared. A pid given as text is read as
`int()` reads it: C `isspace` is stripped, U+001C..U+001F are not, and such
a record names no watcher (unit test `record_pids_read_as_int_does`).

## Hosted CI and goldens

The row replays committed Rust regression goldens on both OSes in the `w1c-connection` shard. Every case is
built from fixed bytes, so all are golden cases. Both goldens bind the
oracle tree with the conductor-watch defect fixes (the `_state_path`,
`_stop`, `_running` and `_non_negative` refusals above): Windows from this
checkout (CPython 3.11.9), Linux from WSL Ubuntu. The source-mutant shards
were retired on 17 November 2026; committed golden replay remains.
