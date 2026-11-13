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
sends its owner board mail when something needs a decision. This row ports
everything `main` does before it takes the state's lock. From the lock on,
the run writes (the lock file, the board registration, the state file, the
pid file, the ledger) and must own every branch after that: the `gh` and
suite-result shapes it refuses or skips, the fresh baseline from a state it
cannot read, the refusal of a state it cannot write, and texts this port
does not reproduce yet (the httpx exception class in "the daemon at ... is
unreachable (...)", the `strerror` of a lock or state write that fails, the
`gh` and lease CLI it runs). None of that is ported, so the watch, in the
foreground or with `--detach`, defers by name before its first write.

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
8. Otherwise `--detach` or the lock: deferred.

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

## Deferred (the dispatcher's `mode 'conductor-watch' is deferred` line, exit 1)

Every deferral is decided before any effect.

- The watch itself: a start with no watcher running on its state, in the
  foreground (the lock, the board, `gh`) or with `--detach` (a detached
  process, separate Windows work). Cases `defer-foreground-start`,
  `defer-stale-record-start`.
- argparse corners: abbreviations and ambiguous prefixes (case
  `defer-abbreviation`), `--`, a value starting with `-` after a space (case
  `defer-dash-value`), an unknown `--word=V`, any other token starting with
  `-`; help or usage at a width other than `COLUMNS=80`, with `FORCE_COLOR`
  / `PYTHON_COLORS` / `NO_COLOR` set or on a terminal; non-UTF-8 argv.
  argparse classifies every argument before it runs any action, so one such
  token anywhere defers the whole argv, even after a `--help` or a type
  error that would otherwise act first (cases `defer-help-before-ambiguous`,
  `defer-error-before-ambiguous`).
- Number spellings outside plain ASCII decimal and the `nan` / `inf` /
  `infinity` words (`1_0`, surrounding spaces, other digits, `nan(1)`: case
  `defer-underscore-interval`); a `--max-age` longer than 640 characters,
  where the oracle's integer digit limit decides the count (case
  `defer-age-digit-limit`).
- On Windows a `--state` starting with two separators (UNC, `\\?\`,
  `\\.\`), whose drive rules decide pathlib's `name` (case
  `defer-state-unc`).
- A state whose last component ends in a dot (pathlib's `with_suffix`
  treats it differently across versions; case `defer-state-trailing-dot`),
  a non-UTF-8 path or session id, an empty POSIX `HOME`.
- A pid record nested past 500 levels (the oracle parses deeper ones, where
  the record may still name a watcher, and reads its RecursionError, at a
  depth not measured, as no watcher), pid text with underscores or
  non-ASCII digits, and outside Windows and Linux any live pid (the oracle
  runs `ps`); on Linux a stat that cannot be read, is not UTF-8 or has no
  field 22 (the oracle falls back to `ps`).
- `PYTHONINTMAXSTRDIGITS` set; an `.agent` file that cannot be opened or
  read for another reason; a `.stop` write refused with a Windows code
  outside the listed ones while nothing is printed yet.

## Declared divergences

- `conductor-watch-notice-unwritten` (case
  `stop-deprecated-name-stderr-closed`): under the deprecated name the
  oracle prints its notice before anything else, so when stderr cannot be
  written it dies in that print before it asks the watcher to stop, with an
  exit status of its interpreter's choosing (1, or 120 from its shutdown
  flush). The candidate writes nothing and exits 1. Both must leave the
  home as setup left it: no stop request.
- Not cased: under the deprecated name, a `.stop` write refused with a
  Windows code outside the listed ones, after the notice was printed. Both
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

The row runs live on both OSes in the `w1c-connection` shard. Every case is
built from fixed bytes, so all are golden cases. Both goldens bind the
oracle tree with the conductor-watch defect fixes (the `_state_path`,
`_stop`, `_running` and `_non_negative` refusals above): Windows from this
checkout (CPython 3.11.9), Linux from WSL Ubuntu. Its source mutants run
in the `conductor_watch-0` and `conductor_watch-1` shards.
