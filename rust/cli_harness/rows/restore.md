# CLI-RESTORE-GATES contract (`restore`)

Oracle: `pseudolife_memory/restore_cli.py` and `docker_backup_restore.py`
(frozen), under CPython 3.11. Candidate: `rust/shim/src/cli/restore_cli/`,
which now performs the whole Docker restore natively. Harness row:
`restore` (`rust/cli_harness/rows/restore.py`). Rust tests:
`rust/shim/src/cli/restore_cli/` (unit tests, including a failure and an
interrupt at every step of the restore in `execute_tests.rs`) and
`rust/shim/tests/cli_restore_contract.rs`.

## Scope

Since the maintainer's ruling of 14 November 2026 (works, not matches), the
native restore is no longer a port of the oracle's every answer. This row
compares the two where they still answer alike: what `restore` decides
before it changes anything.

1. argparse: help and usage at 80 columns, its errors in order, values that
   are negative numbers, unique abbreviations, a `-h` cluster.
2. The dump: every gzip member read to its end; a refusal for a CRC or
   length mismatch, truncation, trailing bytes or a deflate error; the
   completion marker outside `COPY ... FROM stdin;` data.
3. The state archive: the gzip check, the tar members and the
   escape checks (absolute, `..` and backslash names, devices and FIFOs,
   absolute or escaping links through the final and transient link graph,
   GNU long names and links, a POSIX extended header naming a long path).
4. The refusal without `--yes` when stdin and stdout are not terminals (on
   Windows, consoles; the null device is no terminal).
5. Confirmed: a held update lock, and the refusals of the bank's selection
   and its read-only checks against a scripted stand-in `docker` (`inspect`
   failures and malformed output, the connection resolution, the port, the
   image and `/data` mount, PostgreSQL running, the health port, the
   database probe, the safety directory's checkout and state-mount checks,
   free space on Docker storage, PostgreSQL's `/tmp` and mounts).

## Not compared (the native behaviour is by design)

Each of these was a case or rule of this row while the native command
deferred; it is now tested in Rust, and the frozen oracle answers otherwise.

- Every confirmed run that passes the checks: the native command stops the
  daemon, takes and verifies a safety backup, replays and restarts (the
  oracle's run is scripted to fail at its first change). Tested in
  `execute_tests.rs` and `bank.rs` (`probe_*`).
- Archives `tar` would read differently from Python: a damaged header after
  the first member, data after the end marker, an archive of only zero
  blocks, numeric fields in Python's `int()` spellings (`0o`, `_`, `+`), a
  negative size, two GNU long names for one member, sparse members, global
  extended headers that set a name or size, link graphs too deep to walk,
  member types other than plain files, links and directories (Python reads
  an unknown type as a file), and a link or directory with a non-zero size
  (Python skips no data for it, as tar does, but accepts it), a hard link
  to anything but a plain file before it, an extended header that repeats
  or empties a name or size and a GNU long name over 1 MiB: all refused
  natively (`state.rs` tests). A top-level `.pseudolife-restore-*` entry is
  left out of the extract natively.
- The staged extraction and its outcome check (the extracted tree in
  `/data/.pseudolife-restore-<run>` judged before the drop, then renamed
  into place): past the first change, so tested in `execute_tests.rs` and
  `state.rs` (`judge_extracted`) and on a disposable stack.
- A missing container: the native message names the missing container and
  the fix (the oracle reports a failed `inspect`).
- Help and usage at other widths (one 80-column text natively), a trailing
  `--` (the end of options natively, an unrecognized argument in 3.11's
  argparse), a `--db-port` in non-ASCII digits (refused natively).

## Safety of the harness

Every case runs with `PATH` holding only a recording stand-in `docker` (also
named by `PSEUDOLIFE_DOCKER`), and core's program preflight proves no other
`docker` resolves; the disposable home is the per-user directory, so the
update lock and a default safety directory land there, and every confirmed
case names `--safety-dir` inside its directory (the row asserts it). The
stand-in answers a case's script call by call: each argv must fully match
that call's patterns, or it is refused with exit 1 and recorded; without a
script it refuses every call. No remaining case passes every check, so
neither arm reaches a change. Scripted `inspect` documents never publish a
binding on 8765, and the only daemon URL named is the closed loopback port
(the row asserts both). The lock-busy case holds the lock in a child
process of its own. Fixture files are checked unchanged after each arm and
then removed, so the goldens hold no compressor-dependent bytes.

## Mutants

50 source mutants, one per decision the row pins, each caught by a named
case: the refusal's exit code, `--yes`, the null device (Windows), the state
check and its order after the dump check, argparse's error order and
negative numbers, the COPY pattern, lone `\r`, gzip padding and CRC, tar
directory data, padding, the transient pass, FIFOs, link loops, the ustar
prefix, the signed checksum, GNU long names and links (patches, a short
read, the cut at the first NUL); the confirmed path itself, the busy pid,
the held lock, the lock around a refusal, `inspect`'s exit code, stderr and
JSON object, the four selectors, the port range, the image, the `/data`
mount, PostgreSQL running, the health port, the socket message, the
database answer, the checkout and state-mount checks, Docker Desktop's
source form (Windows), the storage order and location, the order of the
space checks, the 8 GiB floor, `find`'s failure, `psql -X`, the database
selector's quoting, the host match, `PGSERVICE` and shared networks.
