# CLI-RESTORE-GATES and CLI-RESTORE-DOCKER (start) contract (`restore`)

Oracle: `pseudolife_memory/restore_cli.py`, `docker_backup_restore.py`
(`validate_state`, `DockerBank.__init__`, `inspect`, `psql`,
`database_exists`, `_backup_space`, `_restore_space` and the start of
`restore`), `disk_space.py`, and `update_cli` (`_dump_complete`,
`interactive`, `UpdateLock`, `_backup_connection_details`, `run_cli`,
`docker_cmd`, `data_dir`, `home`) at origin/master 01fba4b6, under CPython
3.11 (the CI interpreter) with psycopg 3's `conninfo_to_dict` and
`make_conninfo`. Candidate: `rust/shim/src/cli/restore_cli/`. Harness row:
`restore` (`rust/cli_harness/rows/restore.py`). Rust contract test:
`rust/shim/tests/cli_restore_contract.rs`.

## Scope

`restore` replaces a Docker bank. This row ports what the command decides
before it changes anything, in the oracle's order:

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
6. Confirmed (CLI-RESTORE-DOCKER, its start): the update lock, then
   `DockerBank(...)` and `restore()` up to the oracle's first change. Every
   Docker call there only reads: `inspect` of the daemon and PostgreSQL,
   `psql -Atc SELECT EXISTS(...)`, `df -Pk` in PostgreSQL (and the daemon for
   Docker storage), the socket mount's `find`, a second `inspect` of
   PostgreSQL. Each refusal there is printed as `restore failed: <message>`
   with the oracle's exit code.

The oracle's first change is stopping a running daemon, a helper container
(the Docker-storage fallback, the state mounts' free space with
`--state-archive`) or the safety backup (its step line, the directory, the
dump). The candidate defers there; that path (the stop, the safety backup,
staging, the drop and replay, the state replay, the start and readiness wait,
the recovery command) remains CLI-RESTORE-DOCKER's deferred part.

The candidate runs the read-only calls before it takes the update lock (the
oracle takes it first), so it can still defer after them having changed
nothing. A refusal it answers is printed while it holds the lock, so the lock
file the oracle leaves behind is left the same way; a lock this run cannot
take (held or an OS error) defers. A lock already held when the run starts
is answered, before any Docker call, as the oracle answers it.

## Exact contract items

| Item | Python source |
|---|---|
| `--help` / `-h`: the help on stdout, exit 0, when argparse wraps at 80 columns (`COLUMNS=80`, or no `COLUMNS` and no terminal width) | `main`'s parser |
| Usage errors, exit 2: the missing `--backup-file`; a missing value; an invalid `--db-port` (`int()`), quoted with `repr`; an explicit argument to a flag; an ambiguous `--` prefix; unrecognized arguments; argparse's order (classification and ambiguity, then left-to-right actions, then the required check, then the unrecognized ones) | argparse 3.11.9 `_parse_optional`, `consume_optional`, `parse_args` |
| A value after an option may be a negative number (`-1`, `-.5`) or `-`: argparse takes it as the value | `_negative_number_matcher` |
| Repeated options: the last value wins; `--opt=value` | argparse |
| Paths are pathlib's spelling (`dump.sql.gz/` and `./dump.sql.gz` name the file, `''` is `.`) | `type=Path` |
| The dump: every gzip member read as Python's `gzip` reads it (padding, trailing bytes, truncation, a CRC or length mismatch, a deflate error: each a refusal); lines split on `\n`, `\r\n` and a lone `\r`; the marker counts only outside `COPY ... FROM stdin;` data, with `\S` read as Python's `str.isspace` complement | `_dump_complete`, `gzip._GzipReader` |
| The state archive: the gzip check, then members as CPython 3.11 `tarfile` reads them (`frombuf`, `nti` with Python `int(..., 8)`, both checksums, the ustar prefix, the V7 trailing-slash directory, GNU long names and links (`L`, `K`: each record's data up to its first NUL patches the header after it, a directory losing one trailing slash per record, and any header problem after a record a `ReadError`), a bad or short header after the first ending the archive quietly, a member's data present to the end of its last block, non-regular supported types skipping no data, unknown types skipping theirs), then every `validate_state` check: absolute, `..` or backslash names, devices and FIFOs, absolute or backslash link targets, escaping links through the final and the transient link graph, and a link loop ending the walk | `validate_state` |
| The confirmation gate: on Windows a character device counts as a terminal (the CRT's `isatty`, so `NUL` does) | `update_cli.interactive` |
| A held update lock: `another pseudolife-mcp update is running (pid N); nothing was changed. Run this again when it has finished`, exit 2, the pid read from `update.lock.pid` (`unknown` unless digits), no Docker call; otherwise the lock file under `PSEUDOLIFE_MCP_DATA_DIR` or `<USERPROFILE or HOME>/.pseudolife-mcp` stays after a refusal and its pid file does not | `UpdateLock`, `os_lock.OsLock` |
| Docker commands: `PSEUDOLIFE_DOCKER` or `docker`, stdin closed, the oracle's argv, order and timeouts; the text read is stdout only, with stderr added for a failed `inspect` or `psql`; a command that cannot start or times out reads as exit 1 | `run_cli`, `DockerBank.command`, `disk_space` |
| `inspect`: a failure is `Docker inspect failed for the backup/restore; check the containers with docker logs <pg> and docker logs <daemon>, then retry`, exit 1; output that is not a JSON list whose first item is an object is `cannot inspect <name>; restore its Docker container configuration and retry`, exit 2 | `DockerBank.inspect` |
| The selection: `--container`, `--daemon-container` or the stack's names; with all four of `--container`, `--db`, `--user`, `--db-port` the daemon's database URL is not needed, otherwise it must resolve as `_backup_connection_values` resolves it (the environment list, the URL through libpq, `PGSERVICE` refused, the host, the port, the runtime user, the database, a host or address on a network both containers share), or `the daemon's database connection cannot be resolved safely; ...`, exit 2; each option overrides its resolved value | `DockerBank.__init__` |
| The port in 1..65535, else `the database port is ambiguous; ...`, exit 2; a daemon image and a `/data` mount, else `the daemon has no reusable /data mount or local image; ...`, exit 2; PostgreSQL running, else `PostgreSQL is stopped; run docker start <pg> and retry. ...`, exit 2 | `DockerBank.__init__` |
| The health URL: `--daemon-url`, else the daemon's published `8765/tcp` binding (`0.0.0.0` read as `127.0.0.1`, `::` as `::1`, IPv6 in brackets); with neither and no `--leave-stopped`, `the daemon has no published health port; ...`, exit 2 | `DockerBank.__init__`, `restore` |
| The database probe: `psql -X -h /var/run/postgresql -p <port> -U <user> -d dbname=postgres` (psycopg's `make_conninfo` quoting) under `env -u PGHOSTADDR -u PGSERVICE -u PGSERVICEFILE`; exit 2 naming a missing or refused socket is `PostgreSQL's local socket is unavailable on port <port>; ...`, exit 2; another failure `PostgreSQL failed inside <pg>; ...`, exit 1; an answer other than `t` or `f` `cannot determine whether the destination database exists; ...`, exit 2 | `psql`, `database_exists` |
| The safety directory: `--safety-dir` or `<home>/.pseudolife-mcp/backups`, as `Path.resolve()` makes it; a `.git` in it or any parent is `the safety backup directory <dir> is inside a checkout; ...`, exit 2; inside a `/data` mount's source (Docker Desktop's `/run/desktop/mnt/host/<drive>/` and `/host_mnt/<drive>/` forms read as the Windows path) is `the safety backup would be inside the state mount being replaced; ...`, exit 2 | `restore` |
| Free space: the safety directory's filesystem (its nearest existing parent), then Docker storage (PostgreSQL's `df -Pk /`, else the daemon's), PostgreSQL's `/tmp`, then each writable PostgreSQL mount and `/` in sorted order (a socket mount only when `find` reports payload): at least 8 GiB, else `less than 8 GiB is free on <location>; ...`, exit 2; a failed `df` `cannot check free space on <pg>:<path>; ...`, exit 2; output whose last line has no fourth field holding a Python `int` in range `cannot read free space on <location>; ...`, exit 2; a failed `find` `cannot inspect the PostgreSQL socket mount; ...`, exit 2 | `_restore_space`, `_backup_space`, `disk_space` |
| Streams: Python text streams, CRLF on Windows | `print(..., file=sys.stderr)` |

## Deferred (the dispatcher's `mode 'restore' is deferred` line, exit 1)

- Confirmed runs that reach the oracle's first change (above): the stop,
  the safety backup and everything after it.
- Confirmed runs whose outcome the candidate is not sure of, before the
  lock: Docker output that is not ASCII (the oracle decodes it in the
  locale's encoding); `inspect` output serde cannot parse (`json.loads`
  also reads `NaN`, `Infinity`, huge exponents and lone surrogates, and
  nesting past serde's limit, which the oracle refuses); container fields
  of a type Python would
  iterate or fail on in a way this leaf does not reproduce (a string or
  dict where a list is expected, a non-dict where `.get` is called, a
  non-string image, alias or binding); a daemon URL outside the libpq
  spellings read here (a URI with a query, a percent escape, an IPv6
  literal or a host list; keyword form with quotes, backslashes, spaces
  around `=`, or a keyword outside `host`, `hostaddr`, `port`, `dbname`,
  `user`, `password`, `sslmode`, `connect_timeout`, `application_name`,
  `options`, `service`, `target_session_attrs`); a network address other
  than dotted IPv4; a non-ASCII port or runtime user (`int`, `isdecimal`
  read Unicode); a safety, backup, state or mount path `Path.resolve()`
  would raise on or that this leaf does not resolve (a symlink loop, a
  dangling link, UNC, device or drive-relative paths, Win32 names ending
  in a dot or space, reserved device names, a result that keeps the `\\?\`
  prefix); a `.git` probe whose error pathlib would raise; no
  `USERPROFILE` or `HOME`; a lock probe that fails.
- Without `--yes` at a terminal (stdin and stdout both terminals, or on
  Windows character devices): the oracle prompts.
- Argparse shapes: abbreviations (`--back`), `--`, single-dash arguments
  other than `-h`, `-` and negative numbers (option clusters such as `-hx`,
  `-h=x`), non-Unicode argv, non-ASCII in a single-dash argument or a
  `--db-port` value (Python reads Unicode digits and spaces there), any
  error message that would hold non-ASCII text, and help or usage at any
  other wrap width.
- State archives with PAX headers (`x`, `g`, `X`), sparse members (`S`),
  a negative size on a member that skips data or on a GNU long record, more
  than 32 GNU long records before one header, a link walk deeper than 400
  levels or longer than 50 million steps (CPython's recursion limit decides
  there).

## Declared divergences

- A stream holding a corruption miniz_oxide decodes and zlib rejects (a
  back-reference before the member's start) whose trailer's CRC and length
  match miniz_oxide's wrong bytes: the oracle refuses it (`zlib.error`),
  this leaf reads those bytes, which pass only if they hold the dump's
  marker or a safe archive. Not in the harness: no such stream is known; a
  stream that was not built for it fails the CRC check here and is refused
  (`dump-distance-too-far`).
- A deflate stream miniz_oxide rejects and zlib accepts, if one exists:
  this leaf refuses it (exit 2) where the oracle reads it. Not in the
  harness: none is known.
- Order of the lock and the read-only calls: the oracle takes the update
  lock first; the candidate makes the read-only calls first. Their
  sequence, the output and the files are the same in every case answered.
  Another update taking the lock between the candidate's start-of-run lock
  check and its read-only calls makes the candidate defer after making
  those calls, where the oracle would have refused with none. Not in the
  harness (a race).
- The candidate re-reads neither file between the gates and the Docker
  checks (the oracle checks the dump and the state archive again after
  `DockerBank`): a file replaced in that interval is not seen.

## Named rules

- `restore-declared-deferral`: the oracle answers before any Docker command
  (exit 0 or 2, no docker call, fixtures intact) where this leaf defers by
  design; the expectation becomes the deferral.
- `restore-defer-at-first-change`: a case names how many of the oracle's
  Docker calls precede its first change (`reached`). When the oracle's next
  call is a `stop` or `create`, or it printed the safety step line, and the
  fixtures are intact, its observation becomes those calls only, the
  deferral (exit 1, nothing on stdout), and no update lock or safety
  directory; the candidate must have made exactly those calls and written
  nothing. The oracle's own run past that point is scripted to fail at its
  first change, so no data is copied in either arm.

## Safety of the harness

Every case runs with `PATH` holding only a recording stand-in `docker` (also
named by `PSEUDOLIFE_DOCKER`), and core's program preflight proves no other
`docker` resolves; the disposable home is the per-user directory, so the
update lock and a default safety directory land there, and every confirmed
case names `--safety-dir` inside its directory (the row asserts it). The
stand-in answers a case's script call by call: each argv must fully match
that call's patterns, or it is refused with exit 1 and recorded, which makes
the observations differ; without a script it refuses every call. The call log (and any refused call) is compared like the streams. Scripted
`inspect` documents never publish a binding on 8765, and the only daemon URL
named is the closed loopback port (the row asserts both); no case reaches
the readiness check. The lock-busy case holds the lock in a child process of
its own, which it ends after the arm. Fixture files (dumps and archives,
built byte by byte or with Python's `gzip` and `tarfile`) are checked
unchanged after each arm and then removed, so the goldens hold no
compressor-dependent bytes. The Rust contract test scans the leaf's
non-test source: only `docker.rs` starts processes, only `mod.rs` writes
(the lock's pid file), and none uses networking.

## Mutants

64 source mutants, one per decision the row pins. The gates (27): exit code,
`--yes`, the state check and its order after the dump check, argparse's error
order and negative numbers, the 80-column guard, the COPY pattern, lone `\r`,
gzip padding, the CRC check and its refusal, the deflate error's refusal, tar
header and data handling, the transient pass, FIFOs, loop handling, the ustar
prefix, the signed checksum, `int`'s base prefix, and GNU long records (the
name and link patches, the end block and short read after a record, and the
cut at the first NUL). The confirmed path (37): deferring it all, the busy
pid, the lock check, the lock around a refusal, deferring at the first
change, `inspect`'s exit code, stderr and parse errors, its refusal of a JSON
object, the four selectors
and their values, the port range, the image, the `/data` mount, PostgreSQL
running, the health port and `--leave-stopped`, the socket message, the
database answer, the checkout and state-mount checks, Docker Desktop's
source form (Windows), the storage order and location, the order of the
space checks, the 8 GiB floor at both edges, read-only and socket mounts,
`find`'s failure, `psql -X`, `make_conninfo`'s quoting, the URL query
deferral, the host match, `PGSERVICE`, shared networks, and non-ASCII
output. Each is caught by a named case.
