# Installed client acceptance

This tool lives beside the installers and release tooling in `ops/`. It
prepares a disposable fixture and checks the [installed acceptance
matrix](../../docs/dev/python-retirement.md#installed-acceptance-matrix).
The execution boundary currently launches one fixed Bash stand-in. Results
from that fixture leave the installed matrix pending.

## Ledger

From the repository root, create a new ledger and check its coverage.

```sh
python -m ops.acceptance.matrix init acceptance-records
python -m ops.acceptance.matrix check acceptance-records
python -m ops.acceptance.matrix describe clean-install linux-x64
```

To gate the rollout, supply the full candidate commit and the trusted
`SHA256SUMS` from that candidate's release assets.

```sh
python -m ops.acceptance.matrix check acceptance-records --commit "$candidate_sha" --manifest "$candidate_manifest" --require-pass
```

The folder has one UTF-8 JSON file per scenario and platform, named
`<scenario>--<platform>.json`. The scenarios cover clean install,
upgrade and rollback, ordinary native use, bank recovery, and failure and
retry. Platform keys combine OS and architecture. They are `windows-x64`,
`linux-x64`, `linux-arm64`, `macos-x64` and `macos-arm64`.

| Field | Format |
| --- | --- |
| `schema` | Integer `1` |
| `scenario` and `platform` | A cell from `ops.acceptance.matrix.cells()` |
| `status` | `pass`, `fail` or `not yet run` |
| `commit` | Full lowercase Git SHA of the release candidate under test |
| `archive`, `sha256` | Candidate's release archive basename and its lowercase 64-digit SHA-256 from the verified release manifest |
| `machine_role` | `disposable VM`, `disposable physical host` or `ephemeral hosted runner` |
| `commands` | Ordered objects with a published `command` template and its observed integer `exit_code` |
| `outcome` | One sentence selected from the scenario's success outcome or `FAILURE_OUTCOMES` |
| `cleanup` | Boolean `done` and `verified` fields |
| `daemon`, `daemon_engine` | Optional paired fields for observed rows. The hosted workflow includes them when it identifies the peer and requires them for passes. The kind is a listed fixture provenance and the engine is `python` or `rust`. |

A pending row has the first four fields only. The checker prints the
matrix, counts each status, and exits 1 for malformed or missing rows.
By default exit 0 means the ledger is structurally complete. Supply both
`--commit` and `--manifest` to check that every observed row belongs to the
same candidate and its exact archive basename has the recorded digest.
Observed rows must use one archive version. `--require-pass` also returns
exit 1 if any cell has failed or is pending.

`describe` prints the command templates and host requirements for a cell.
For Linux clean install it also shows the proposed client-only probe with
stand-in client programs. That probe leaves the clean-install cell pending
until the first real MCP session and the remaining checks are observed.

Use `matrix.commands(scenario, platform)` to get the published command
templates. Write the observed command results with `matrix.write_record`.
The writer creates a file exclusively, so keep the old ledger and write the
updated rows into a new snapshot folder. A pass requires the full ordered
command list, the expected successes and refusals, the scenario observation
and verified cleanup. A failure can carry a partial command list. For
missing or deliberately corrupted copies, record the original release
archive and its verified manifest digest. Keep the unredacted evidence
private, including any additional assets and the actual Docker target
platform. A Linux arm64 client can record the legacy import case after
observing it against a separate disposable Linux amd64 Docker target that
can run the archived Python image.

Templates use placeholders for fixture arguments and operator-specific
commands. The writer and checker reject unlisted templates, arbitrary prose,
paths, names and fields outside the schema. Supply commit and digest values from the
release evidence. Keep raw argv, environment, stdout, stderr and cleanup
inventories out of the ledger.

## Stand-in fixture

`runner.DisposableHome` creates a fresh directory under a dedicated scratch
parent, redirects the profile and temporary folders, and builds a new child
environment. The child receives no runtime overrides or user-service bus
settings from the parent. The Windows environment preserves the OS root
as `SystemRoot` and `windir`. A real Windows run requires a host without
the `py` launcher, which Windows can resolve outside the staged search
path. Supply trusted executables by absolute path to
`prepare_path`.
Only selected tools enter the child search path. The Bash stand-in checks
that `python`, `python3`, `pip` and `py` do not resolve and that `tar` does.
It verifies a staged checksum manifest is present and leaves a marker in the
disposable home. The search path is an environment fixture. Real process
isolation and executable dependencies still have to be verified on the
actual platform.

Pass a local release folder and the desired release asset basenames to
`prepare_archives`. It reads `SHA256SUMS`, rejects duplicate or unknown
asset names, verifies the copied bytes, and publishes the staging folder
after verification. It does not extract or run the archives. Obtain the
manifest from the same trusted build or release as the archives. This step
checks bytes against the manifest. It does not establish release provenance.

`runner.run_installer` is the installer execution boundary. In this slice it
runs only its own fixed shell script, from the disposable working directory
with the fixture environment. The context manager removes its own directory
on exit and checks that the scratch parent's top-level listing is unchanged.
Read the private `cleanup_receipt` after leaving the context. Keep
the scratch parent dedicated to the fixture so changes from other runs
cannot obscure the result.

The stand-in is exercised with the focused tests.

```sh
python -m pytest -q -p no:cacheprovider tests/test_installed_acceptance.py
```

A real installed run requires a disposable OS account and any applicable
isolated service manager, browser profile, Docker engine and bank. Record
those host checks independently of the filesystem inventory before claiming
verified cleanup.
