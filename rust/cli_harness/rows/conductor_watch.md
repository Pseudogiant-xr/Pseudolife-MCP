# CLI-CONDUCTOR-WATCH-SETUP contract (`conductor-watch`, before its lock)

Oracle: `pseudolife_memory/conductor_watch_cli.py` (`main` up to the
state's `OsLock`, with `lease_cli.parse_duration` / `_pid_alive` and
`coordination_identity.resolve_digest_path`), and `cli.py`'s dispatch of
`conductor-watch` and its deprecated name `delegate-watch`, at this branch's
base, CPython 3.11.9. Candidate: `rust/shim/src/cli/conductor_watch/`
(argparse subset in `args.rs`), reusing `cli/wait_mail.rs` (`digest_path`,
`pathlib`), `cli/lease/hold.rs` (`pid_alive`) and `cli/pyjson.rs`. Harness
row: `conductor_watch` (`rust/cli_harness/rows/conductor_watch.py`). Oracle
tests: `tests/test_conductor_watch.py`.

## Scope

`conductor-watch` polls GitHub, the suite results and the suite leases, and
sends its owner board mail when something needs a decision. This row ports
everything `main` does before it takes the state's lock. From the lock on,
the run writes (the lock file, the board registration, the state file, the
ledger) and reaches branches the oracle answers with an uncaught exception
(a `gh` answer of the wrong JSON shape, a suite result that is JSON but not
an object, an old state of the wrong shape, an unwritable state directory)
or with text this port does not reproduce (`JSONDecodeError` messages in
`gh failed (n/5): ...`, the httpx exception class in "the daemon at ... is
unreachable (...)"). A native run that took the lock could no longer defer,
so the watch defers by name until those branches are decided.

## What runs, in the oracle's order (checked)

1. `cli.main`: `delegate-watch` prints `DEPRECATED_MODE` on stderr, then
   both names run `main(sys.argv[2:])`.
2. `parser().parse_args`: help exits there; a missing value, an explicit
   argument to a flag, or a refused type (`_positive`, `_non_negative`,
   `_age`) errors there; words and unknown `--words` collect as extras and
   error once every option is consumed.
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
   `asked to stop` (exit 0).
6. The owner refusal (exit 2).
7. `_running(state)`: the `.pid` file's record when it parses as an object
   whose `int(pid)` is alive and whose `process_start` is absent, null, or
   the process's own token (Windows creation `FILETIME`, Linux `/proc` stat
   field 22); `_foreign` (stderr, exit 2) or `_already` (stdout, exit 0).
   This holds with `--detach` too.
8. Otherwise `--detach` or the lock: deferred.

## Exact contract items

| Item | Python source |
|---|---|
| Help at `COLUMNS=80`, stdout, exit 0; usage + `pseudolife-mcp conductor-watch: error: ...` on stderr, exit 2 | `parser`, argparse 3.11 |
| `must be above 0`, `must be 0 or more`, `invalid _positive value: 'x'`, `not a duration: 'x' (use ...)` | `_positive`, `_non_negative`, `_age`, `parse_duration` |
| The owner refusals and their `; pass --owner <...>` tail | `resolve_owner`, `main` |
| `no session to find the watcher by; ...`, `no watcher running for STATE; nothing changed`, `ID asked to stop; ...` and the empty `.stop` file | `main`, `_stop` |
| `already running for OWNER; park with ...`, `STATE belongs to the watcher for OWNER; ...` | `_already`, `_foreign` |
| `int()` of the record's pid: ints, bools, floats (truncated; NaN is none), padded or signed decimal text; anything else reads as no watcher | `_running` |
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
- Number spellings outside plain ASCII decimal (`inf`, `nan`, `1_0`,
  surrounding spaces, other digits: case `defer-infinite-interval`); a
  non-ASCII `--max-age` (IGNORECASE matches `ſ` as `s`, and the unit lookup
  then raises).
- A state whose last component pathlib's `with_suffix` refuses or treats
  differently across versions (empty, `.`, `..`, ending in a dot), a
  non-UTF-8 path or session id, an empty POSIX `HOME`.
- A pid record nested past 500 levels (RecursionError is uncaught there), a
  pid outside the 32-bit signed range or an infinite one (the process
  probes overflow), pid text with underscores or non-ASCII digits, and
  outside Windows and Linux any live pid (the oracle runs `ps`); on Linux a
  stat that is not UTF-8 or has no field 22.
- `PYTHONINTMAXSTRDIGITS` set; an `.agent` file that cannot be opened or
  read for another reason; a write that fails before its first byte.

## Declared divergences

None in the compared shapes. The row's own pid files (each arm's sleeper
has its own pid and start time) are checked unchanged after each arm and
then taken out of the snapshot; every other byte of the home is compared.

## Hosted CI and goldens

The row runs live on both OSes in the `w1c-connection` shard. Every case is
built from fixed bytes, so all are golden cases. Both goldens bind the
oracle tree of this branch's base: Windows from this checkout (CPython
3.11.9), Linux from WSL Ubuntu.
