# Local Rust parity loop

Run these commands from the repository root in the checkout's development
Python environment. The [CLI harness](cli_harness/README.md) compares exit
status, streams, files, requests and bank state; live mode uses the Python
oracle, while golden mode uses the committed record for the current OS.

Keep Cargo output on E: and reuse the same target directory between iterations.
Give concurrent worktrees separate subdirectories. Keep test TEMP at its
default location so Windows credential fixtures retain the expected ACLs.

```powershell
$env:CARGO_TARGET_DIR = 'E:\rust-target\port-dev'
python -m pseudolife_memory.cli lease check full-suite
if ($LASTEXITCODE -ne 0) { throw 'Wait for the full-suite lease before building' }
Push-Location rust
try {
    cargo build --locked --bin pseudolife-stdio -j 3
    if ($LASTEXITCODE -ne 0) { throw 'Cargo build failed' }
} finally {
    Pop-Location
}
$candidate = Join-Path $env:CARGO_TARGET_DIR 'debug/pseudolife-stdio.exe'
```

Building inside `rust/` selects its pinned toolchain. On Linux, use a separate
Linux target directory and the `debug/pseudolife-stdio` filename.

Run a single row, live or against its golden:

```powershell
python rust/cli_harness --row mail --candidate $candidate -v --out mail-live.json
python rust/cli_harness --row mail --golden --candidate $candidate -v --out mail-golden.json
```

For one case, add `--case ring-past-seen`. Other rows are listed by
`python rust/cli_harness --help`. Bank-backed rows need the disposable bench
PostgreSQL and test login described in [CLAUDE.md](../CLAUDE.md); the harness
refuses production database names and non-test logins.

Regenerate the whole oracle row for the current OS, then inspect the golden diff:

```powershell
python rust/cli_harness --row mail --record -v
git diff -- rust/cli_harness/goldens/mail.*.json
```

Recording writes `goldens/<row>.<platform>.json` and replaces that row's record;
omit `--case` to avoid replacing it with a partial row.

Run the existing source mutants; every selected mutant must be caught:

```powershell
python rust/cli_harness --row mail --mutants -v
```

The mutant runner builds copies under `$CARGO_TARGET_DIR/mutant-src` into
`$CARGO_TARGET_DIR/mutant`, retaining the ordinary build. `--mutant <id>` selects
one control. Use the same full-suite lease check before this build work.

The [daemon harness](daemon/README.md#the-harness) has equivalent `live`,
`golden`, `live --record` and `mutants` modes, with `--only <scenario>` for a
single scenario. Its README names the model/runtime and disposable-bank setup.

Command sources, verified at `049863c9`: [CLI runner](cli_harness/runner.py)
(`_default_candidate`, `record`, `main`), [mutant builder](cli_harness/mutants.py)
(`_target`, `_sync_source`, `_build`), and [bank guards](cli_harness/rows/_bank.py)
(`_login`, `url`, `_connect`). The existing commands need no wrapper.
