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

## Isolation

- Each arm gets a fresh home under the default temporary directory (owner-only
  credential checks need its ACLs), the same path for both arms, reset between
  them. `HOME`, `USERPROFILE`, `APPDATA` and `LOCALAPPDATA` point into it.
- The environment is an allowlist of process plumbing (`PATH`, `SYSTEMROOT`,
  `TEMP`, ...). Tokens, digest and lock directories never leak in from the
  caller. `PSEUDOLIFE_MCP_DAEMON_URL` always names the case's fixture daemon,
  or a closed loopback port, so no case reaches a real daemon.
- Bank-backed rows use disposable databases on the bench PostgreSQL through
  the test login only, never the live bank.

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
