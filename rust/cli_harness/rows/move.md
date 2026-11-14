# CLI-MOVE-REFUSALS contract (`move`, refusals before any effect)

Oracle: `pseudolife_memory/move_cli.py` and `codex_connection._validated_daemon_url`
at origin/master 151c3faa. Candidate: `rust/shim/src/cli/move_cli.rs` (the
refusals Python makes before any effect) and, for the argv it passes on,
`rust/shim/src/cli/restore_cli/move_native/` (the native dry run).
Harness row: `move` (`rust/cli_harness/rows/move.py`). Oracle test:
`tests/test_move_cli.py::test_usage_errors_exit_2`.

## Scope

Since the maintainer's ruling of 14 November 2026 (works, not matches),
`move` is rebuilt as a native feature rather than ported. This row compares
the two where they still answer alike: what the command decides from its
argv before any effect. The native dry run (`--dry-run`: both hosts checked
read-only, the target's half in the hidden `move-target` helper over ssh)
and its own refusals are not compared (below); they are tested in Rust
(`restore_cli/move_native/tests.rs` against recording fakes of both hosts,
and `tests/cli_move_contract.rs` through the binary).

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
| `--help` / `-h` alone: argparse help on stdout, exit 0; only `COLUMNS=80` (wrap 78) is reproduced | `_parser` 1807-1829, `main` 1833-1836 |
| No `--to` (every other token canonical): usage (3 lines) + `pseudolife-mcp move: error: the following arguments are required: --to`, stderr, exit 2; only at `COLUMNS=80` | `_parser` 1816 (`required=True`) |
| `--target-url` given and not an origin: `move: --target-url must be an http(s) origin such as http://100.64.0.2:8765 (no path, query or credentials)`, stderr, exit 2, before any `--to` check | `main` 1837-1844; `codex_connection.py:104-117` |
| `--to` empty, or any `str.isspace` character: `move: --to takes an ssh target such as root@box or a ~/.ssh/config alias`, stderr, exit 2 (also under `--json`) | `main` 1848-1850 |
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

Each of these was a case of this row while the native command deferred:

- Every argv that passes the checks above: the native module answers it.
  Without `--dry-run` it refuses, naming it; with `--dry-run` it refuses, in
  this order, `--target-checkout`, `--expose-target`, `--resume`,
  `--target-env-file` / `--target-compose-file`, a missing `--target-url`,
  carried tokens (no `--no-keep-tokens`) and a relative `--target-work-dir`
  (exit 2), and otherwise runs the read-only preflight (exit 0 with the
  plan, or 4 with its refusal). The oracle would contact the live daemon,
  so these never ran it; `tests/cli_move_contract.rs` pins each answer.
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

## Deferred (the dispatcher's `mode 'move' is deferred` line, exit 1)

- Argparse shapes: `--opt=value`, abbreviations, repeated options,
  positionals, a missing value, any value starting with `-` (so the oracle's
  `--to` starts-with-`-` refusal, reachable only through `--to=-x`,
  `--to -1`, `--to -` or a spaced value, always defers here), `--help` with
  other arguments, help or usage at any `COLUMNS` other than `80`.
- Non-Unicode argv.
- Any non-ASCII argument when CPython would not decode a POSIX argv as
  UTF-8: UTF-8 mode (`PYTHONUTF8=1`, or `PYTHONUTF8` unset or empty under
  the C or POSIX locale, PEP 540) or a UTF-8 codeset in the first non-empty
  of `LC_ALL`, `LC_CTYPE`, `LANG`. Otherwise the oracle decodes argv with the
  locale codec and sees other characters. Windows argv is always exact.
- argparse help when stdout is a terminal, and argparse usage when stderr
  is a terminal, or either when `FORCE_COLOR`, `PYTHON_COLORS` or `NO_COLOR`
  is set: newer CPython argparse colours its output there. The plain
  `print` refusals are never coloured and stay answered.
- An answer whose write fails before any byte is accepted (stderr on
  `/dev/full`, a closed stdout): nothing was answered, so the dispatcher's
  deferral follows (its own write fails too, exit 1).

## Free items

None: every answered byte is compared.

## Declared divergence

An answer whose write fails part-way exits 1 with the bytes already
written. CPython's `print` raises there; its traceback write fails too and
the exit is 1, or 120 when the final flush fails. Unit-tested only
(`a_failed_answer_write_defers_only_when_nothing_left`): the harness has no
stderr-on-`/dev/full` arm.

## Safety of the harness

The oracle runs only argv that refuse before any effect, with `PATH` set to
an empty directory and every proxy variable naming a recording listener;
a once-per-process positive control proves the oracle's own HTTP reader
(`move_cli.Runner.get_json`) reaches that listener for a loopback URL, so
no request could reach the live daemon on 8765. The argv shapes that still
defer run a verified stub `pseudolife_memory.cli` in the Python arm, never
the oracle; the candidate must print the deferral and make no request. The
Rust contract test `rust/shim/tests/cli_move_contract.rs` checks the native
answers and the deferrals against a bound listener that every proxy and
daemon variable names, which must see no connection, with `PATH` holding
nothing (no docker, no ssh), so a native dry run refuses at its first need,
ssh, before any connection.

A direct socket (a hardcoded `127.0.0.1:8765`) would bypass both
listeners, so the leaf's source is scanned for network, process, file and
raw-OS names, and for any crate module but the dispatcher's
`super::text_bytes` (the native module is a separate file, reached
through the dispatcher): by `the_leaf_source_reaches_no_io_beyond_its_answer`
in the contract test, and by every harness case on the source the
candidate binary was built from (the mutant copy under `--mutants`). The
`move-direct-socket` mutant inserts such a connect behind `if false`, so it
never runs and only the scan can catch it.

## Hosted CI and goldens

Both Parity lanes run the row live and against `goldens/move.<os>.json`.
The guards above are unchanged on a runner: `PATH` points at an empty
directory and every proxy variable at the recording listener.
