# CLI-MOVE-REFUSALS contract (`move`, refusals before any effect)

Oracle: `pseudolife_memory/move_cli.py` and `codex_connection._validated_daemon_url`
at origin/master 151c3faa. Candidate: `rust/shim/src/cli/move_cli.rs` (the
refusals Python makes before any effect) and, for the argv it passes on,
`rust/shim/src/cli/restore_cli/move_native/` (the native move and its dry
run).
Harness row: `move` (`rust/cli_harness/rows/move.py`). Oracle test:
`tests/test_move_cli.py::test_usage_errors_exit_2`.

## Scope

The native move dispatcher handles help, argument refusals, dry runs,
execution, resume, rollback and target release. This row replays its
before-effect command-line answers. The transaction, including the dry run
and its read-only checks on both hosts, has its own Rust tests:
`restore_cli/move_native/engine_tests.rs` runs the transaction against a
model of both hosts with a fault at every call;
`restore_cli/move_native/tests.rs` and `ops.rs` use recording fakes; and
`tests/cli_move_contract.rs` runs through the binary.

`move --rollback <move id>` and `move --release-target <move id>` use
the native recovery parser. The dispatcher checks their syntax first. Every
`move` argv ends in native code: the recovery forms, help and the early
refusals in `move_cli.rs`, the transaction, or the named usage refusal
below. None reaches the dispatcher's deferral line.

## What runs before the first effect (checked)

1. `cli.main` dispatches `move` with `sys.argv[2:]` (`cli.py:190-192`).
2. `main` (`move_cli.py:1832-1851`): `_parser().parse_args` (argparse may
   print help or a usage error); the `--target-url` check; the `--to` check;
   then `Options(...)` and `Mover(options)`.
3. `Mover.__init__` (`move_cli.py:481-533`) only imports `connect_cli` and
   `update_cli`, reads `PSEUDOLIFE_DOCKER` and `PSEUDOLIFE_MCP_DATA_DIR`
   (`update_cli.py:207-212`), draws a move id (`_new_move_id`, :434) and sets
   fields: no file, process or network.
4. `Mover.run` (`move_cli.py:617-622`): the `--to` control-character check,
   then the `--target-checkout` check, both before `preflight()`, whose
   first act is the GET of `SOURCE_URL/health` (:651).

## Exact contract items

| Item | Python source |
|---|---|
| No `--to` (every other token canonical, or the only problem): usage (7 lines, argparse's layout at 78 columns) + `pseudolife-mcp move: error: the following arguments are required: --to`, stderr, exit 2, at every `COLUMNS`, on a terminal and with colour variables set; the oracle matches it only at `COLUMNS=80` | `_parser` 1816 (`required=True`) |
| `--target-url` given and not an origin: `move: --target-url must be an http(s) origin such as http://100.64.0.2:8765 (no path, query or credentials)`, stderr, exit 2, before any `--to` check | `main` 1837-1844; `codex_connection.py:104-117` |
| `--to` empty, or any `str.isspace` character: `move: --to takes an ssh target such as user@box or a ~/.ssh/config alias` (the frozen Python says `root@box`; the native text, since 16 November 2026, does not teach a root login), stderr, exit 2 (also under `--json`) | `main` 1848-1850 |
| `--to` holding a C0 character that is not whitespace (U+0000-U+0008, U+000E-U+001B): the same text, stderr, exit 2 | `Mover.run` 619-620, `fail` 555-559 |
| `--target-checkout` holding CR or LF (after `rstrip("/") or DEFAULT`): `move: --target-checkout holds a control character`, stderr, exit 2 | `Mover.__init__` 498, `Mover.run` 621-622 |
| Order: argparse, `--target-url`, `--to` (main), `--to` (Mover.run), `--target-checkout` | as above |
| Streams: Python text streams, CRLF on Windows | `print(..., file=sys.stderr)` |

## Canonical argv admitted by the parser

`move` followed by any order of `--to V`, `--target-checkout V`,
`--target-url V`, `--no-keep-tokens`, `--resume`, `--dry-run`, `--yes`,
`--json`, each at most once, with each value the next argument, valid
Unicode, not starting with `-`.

## Not compared (the native behaviour is by design)

- Every argv that passes the checks above: the native module answers it.
  On Linux targets, `--dry-run` checks both bearer modes. It checks carried
  bearer settings against the installed Compose files or the original files
  supplied explicitly, without sending the source values. Execution requires
  an execution-capable target helper and a loopback daemon endpoint for
  carried-bearer verification. `--expose-target` checks or creates a
  target tailnet forward when `--target-url` is omitted. Checkout handover
  receives an early refusal that points to the installed release route. A
  missing `--target-url` without `--expose-target` and relative paths
  outside `~/` are refused (exit 2).
  The oracle would contact the live daemon, so these use the Rust tests.
- `--target-url` values whose validity differs between Python releases
  (bracketed hosts, empty userinfo, empty or `+`/leading-zero ports, `_` or
  non-ASCII in a host): the native client decides them
  (`argv.rs` `origin`, tested by `target_urls_are_origins`).
- `--json` with a `Mover.run` refusal (a C0 `--to`, a CR/LF checkout): the
  oracle prints a JSON report with a fresh move id; the native client
  answers the `--to` check as text, as without `--json`, and a checkout as
  unsupported.

`move-valid-refused`, `move-json-control` and `move-isspace-separators`
left the row's mutants with them: the first changes only the answer to
valid argv, which `tests/cli_move_contract.rs` pins; the other two are now
equivalent mutants (each turns a `--to` refusal into a deferral, and the
native module then prints the same `--to` refusal).

## Native help and usage

`--help` or `-h` anywhere in argv prints the same help at every terminal
width, as argparse's does; `--rollback --help` is help too, since a move id
never starts with `-`. The help describes installed release targets, says
`--target-checkout` is refused and `--target-release` changes nothing, and
names the recovery commands.

Argv neither native parser takes gets one refusal on stderr, exit 2,
`move: <cause>. Nothing was changed`, before any move record is read or
either host consulted. The cause names the first problem in argv order:
a joined `--opt=value`, a flag given a value, an abbreviation (with the
full name it stands for), an unknown option, a repeated option, a
positional argument, a missing value or one starting with `-` (for `--to`
that would be an ssh option), non-Unicode argv, `--target-gone` outside
`--release-target`, and for the recovery forms a missing move id, both
forms at once or an option they do not take. An option spelling is echoed
only when it is a plain `-`/`--` word; anything else, which could be a
value or a pasted secret, is named by its position. Repeated
`--target-compose-file` entries are overlays, in order, not a repeat.

The cases whose native answer differs from argparse's (the help cases, the
missing `--to` at `COLUMNS=120`, and every case that names a usage cause)
replay goldens recorded from the candidate with `--record-candidate`; the
case IDs keep their `defer-` names from when the command deferred.

## Free items

None: every answered byte is compared.

## Declared divergence

An answer whose write fails exits 1, including when the stream accepts
zero bytes. The partial and zero-byte cases use the writer fake in
`a_failed_answer_write_ends_the_command`.

The `--to` help and refusal give `user@box` as the example target, where
the frozen oracle says `root@box`. Every case applies `move-native-wording`,
which rewrites those two oracle phrases (and only those, so an argv that
passes `root@box` stays compared) before a live comparison.

## Safety of the harness

The oracle runs only argv that refuse before any effect, with `PATH` set to
an empty directory and every proxy variable naming a recording listener;
a once-per-process positive control proves the oracle's own HTTP reader
(`move_cli.Runner.get_json`) reaches that listener for a loopback URL, so
no request could reach the live daemon on 8765. The Python arm runs a
verified stub for argv shapes the Python parser accepts but the native
parser refuses. The candidate must print native usage and make no request.
The Rust contract test `rust/shim/tests/cli_move_contract.rs` checks these
answers against a bound listener that every proxy and
daemon variable names, which must see no connection, with `PATH` holding
nothing (no docker, no ssh), so a native dry run refuses at its first need,
ssh, before any connection.

A direct socket (a hardcoded `127.0.0.1:8765`) would bypass both
listeners, so the leaf's source is scanned for network, process, file and
raw-OS names, and for any crate module but the dispatcher's
`super::text_bytes` (the native module is a separate file, reached
through the dispatcher): by `the_leaf_source_reaches_no_io_beyond_its_answer`
in the contract test, and by every harness case on the source the
candidate is tested against in this checkout. The former
`move-direct-socket` mutant inserted such a connect behind `if false`, so
only the scan could catch it. Source mutants were retired on 17 November
2026; the contract test and harness source scan remain.

## Hosted CI and goldens

Both Parity lanes replay `goldens/move.<os>.json`.
The guards above are unchanged on a runner: `PATH` points at an empty
directory and every proxy variable at the recording listener.
