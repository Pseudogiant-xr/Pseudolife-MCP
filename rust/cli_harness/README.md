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

Goldens recorded before the binding existed carry `oracle_commit` without
`oracle_commit_source`, and the Linux ones from an exported tree carry the
8-character ids `846ccd38` (invite, move, pair, transfer) and `9ff2c8cd`
(episode, hook). None is stale: checked on 2026-10-10 (`git rev-parse
<commit>:pseudolife_memory`), every golden's commit has the same
`pseudolife_memory/` tree as this branch (`78c12208`): `846ccd38`,
`9ff2c8cd`, `306b5515`, `6034e5c3`, `35b8f5d2`, `7cd78741`, `8263e7e3`,
`9bddd207`, `e4f350d5`, `992847be`, `f92dd048`, and `2ee86df5` (the
pull-request merge commit CI recorded `test_login.linux.json` from; not on
this branch, fetchable by id). `lease.linux.json` says `unknown`.

The harness process imports oracle code itself (writers, seeders,
observers), so `python rust/cli_harness` relaunches itself once with an
empty `PYTHONPYCACHEPREFIX` and `PYTHONDONTWRITEBYTECODE=1`. A recording
checks, before any row runs and again before each golden is written, that
every `pseudolife_memory` module loaded in the harness process came from
the bound source, through no cache outside that prefix, with source bytes
equal to the bound commit's.

The recorder replaces every SCRAM verifier in recorded streams and files
with `<scram-sha-256-verifier>` and refuses to write a golden in which
`SCRAM-SHA-256$` still appears, raw or base64-encoded.

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
  the test login only, never the live bank. Every database and template name
  has a random run suffix; cleanup accepts only names allocated by that run.
  The normalizer maps exact allocated names back to their fixture labels for
  comparison with existing goldens. Cleanup does not sweep other runs' banks.
  Initial database creation briefly retries a busy shared extension template;
  it never closes another run's template sessions.

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
