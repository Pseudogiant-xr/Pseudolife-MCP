# CLI differential harness

Runs the Python CLI (the oracle, `python -m pseudolife_memory.cli`) and the
Rust CLI (`pseudolife-stdio`) on the same case in the same disposable home,
then compares exit code, stdout, stderr, every file under the home, the HTTP
requests a fixture daemon received and any database rows the case dumps. It
exits non-zero on any difference. Comparison is exact except for named rules
a case opts into; each rule validates its span first and is listed in the
row's spec under `specs/` and in `rust/PARITY.md`.

```sh
# live: oracle and candidate side by side (CI runs this on both OSes)
python rust/cli_harness --row mail --candidate <pseudolife-stdio> -v
# mutant control: each mutant must turn at least one case red
python rust/cli_harness --row mail --mutants
# goldens: record the oracle, or compare a candidate against the record
python rust/cli_harness --row mail --record
python rust/cli_harness --row mail --golden --candidate <pseudolife-stdio>
python -m pytest rust/cli_harness/test_cli_harness.py   # offline self-tests
```

Defaults: the oracle is this checkout with the running interpreter
(`--oracle-source`, `--oracle-python`); the candidate is
`$CARGO_TARGET_DIR/debug/pseudolife-stdio`.

A golden names the oracle commit it was recorded from, and `--record`
refuses an unbound recording before any arm runs. The binding is to the
bytes the oracle imports: every file under `pseudolife_memory/`, read from
disk (so an `assume-unchanged` edit counts), must equal the commit's blob,
with no extra file (an untracked or ignored shadow module, a sourceless
`.pyc`) and no `__pycache__` entry without its source. The oracle arm never
runs in-tree bytecode: it reads caches only under an empty
`PYTHONPYCACHEPREFIX`. A git checkout binds to its HEAD
(`oracle_commit_source: git`), and any git command that fails there
refuses. Any other tree (an export) needs the commit it came from, as
`--oracle-commit` or `CLI_HARNESS_ORACLE_COMMIT`: when this harness's
checkout holds that commit, the tree must match it file for file
(`verified-tree`); with no checkout to ask, the commit is recorded as
`declared`.

`--out` writes a JSON summary keyed by row name (each with its PARITY ID
under `parity`); with `--record` it lists each row's recorded and skipped
cases. The file is replaced atomically after every row, so a crash keeps
the rows already finished; `complete` is true only once every row ran.

## Isolation

- Each arm gets a fresh home under the default temporary directory (owner-only
  credential checks need its ACLs), the same path for both arms, reset between
  them. `HOME`, `USERPROFILE`, `APPDATA` and `LOCALAPPDATA` point into it, and
  on Windows so do `ProgramW6432` and `ProgramFiles` (`{HOME}\Program Files`):
  a 64-bit child derives `ProgramFiles` from `ProgramW6432`, and with neither
  set a default-location lookup falls back to the real `C:\Program Files`.
- External programs: a case whose oracle may look up a real program (docker,
  pg_dump, tailscale, cloudflared, codex, claude, git, bash, ssh, a browser,
  the tunnel runtime) names it in `Case.programs`. Before each arm, on the
  arm's still-empty home and before the case's setup, core runs the same
  Python interpreter (and nothing else) with that arm's exact environment and
  cwd, and asks it for `shutil.which` of each name (current directory first
  and `PATHEXT` on Windows) and the lookup inputs it sees (`PATH`, `PATHEXT`,
  the Program Files variables, `LOCALAPPDATA`, `APPDATA`, `SystemRoot`,
  `HOME`, `USERPROFILE`). The case is refused (`ProgramLeak`, the run fails)
  if any name resolves outside the home, or on Windows unless `ProgramW6432`
  and `ProgramFiles` are non-empty and inside it. A case declaring a program
  therefore pins `PATH` inside the home. One child runs per distinct
  environment, cwd and name list. The check covers lookups, not what setup
  later places in the home (a hard-linked host binary is inside the home).
- `Case.real_programs` is the escape hatch for a case that deliberately runs
  a named host program (backup's `dsn-path` runs this host's bundled
  `pg_dump` from `PATH` against a disposable database). Only the named
  programs may resolve outside the home; each run prints them on the case's
  line (`match dsn-path  [real programs: pg_dump]`) and in the `--out`
  summary.
- The environment is an allowlist of process plumbing (`PATH`, `SYSTEMROOT`,
  `TEMP`, ...). Tokens, digest and lock directories never leak in from the
  caller. `PSEUDOLIFE_MCP_DAEMON_URL` always names the case's fixture daemon,
  or a closed loopback port, so no case reaches a real daemon.
- Bank-backed rows use disposable databases on the bench PostgreSQL through
  the test login only, never the live bank.

## Daemons

- `fixture_daemon.py`: a loopback HTTP fixture with scripted routes, fresh per
  arm, that records every request (method, raw target, header fields in
  order, body). Requests compare on the wire by case-insensitive field name,
  with every field's presence and value exact and repeated names failing.
  `Trickle` writes a reply a few bytes at a time; `tls=True` serves the
  disposable `shim/tests/fixtures/pg_tls` certificate at `https://localhost`.
- `rows/_daemon.py`: real oracle daemons (`pseudolife-mcp serve`), one per
  arm on its own disposable bank (`rows/_bank.py`, test login only), shared
  across a row's cases and settled under the CLIs' health-probe budget before
  each one. Cases marked `bank=True` dump the rows their keys own after each
  run; they need the oracle daemon, so they run live only (`--skip-bank`
  skips them; `--golden` and `--record` never include them).

## Seeding

State is written through the oracle's own writers (`producers.py`): the
coordination adapter's private atomic writer and digest renderer. The digest
renderer runs with its clock read in UTC so seeded bytes, and goldens, do not
depend on the host timezone.

## Mutants

`mutants.py` lists exact source substitutions in the Rust leaf. Each is
applied to a copy of `rust/` under `$CARGO_TARGET_DIR/mutant-src`, built into
`$CARGO_TARGET_DIR/mutant`, and run against its row. A mutant no case catches
fails the control.

## Goldens

`goldens/<row>.<platform>.json` holds the oracle's observations with the home
path replaced by `{HOME}` and the arm's invocation window kept, so `--golden`
applies the same rules a live run does.
