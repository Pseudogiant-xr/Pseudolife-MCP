# Hosted release acceptance

Use **Actions → Hosted release acceptance → Run workflow** to exercise the
published native client on Linux arm64, macOS Intel, or macOS Apple silicon.
Select the columns and enter a published release tag. The default is
`v0.25.0`. The workflow runs only when dispatched.

Each selected runner checks out the release tag, verifies the native archive
and Compose bundle against the release's `SHA256SUMS`, and calls
`PSEUDOLIFE_SHIM_ENGINE=native bash ops/install.sh` from that checkout.
The release API's asset digests also check `SHA256SUMS` itself. When the
anonymous API refuses the request, which shared hosted addresses hit, the
job takes `SHA256SUMS` from the public release download alone. Every
receipt records which check applied as `release_verified_by`.
The installer and native client run with Python and pip off their PATH.
Legacy start and rollback operations use Python in a separate environment.
The jobs use signed-out Codex CLI 0.171.0 installations and disposable bank
credentials. A full dispatch uses up to four hosted slots across the platform
jobs and the summary, with a maximum of 75 minutes per platform job.

## The test peer

Every client row needs a daemon, and hosted jobs cannot reach each other or
a private daemon. Each macOS job therefore builds the daemon from the tagged source
(`cargo build --locked --bin pseudolife-daemon` with Rust 1.94.0), starts it
on a loopback port with its own PostgreSQL 18 and pgvector, and provisions its
pinned models. This daemon is a test peer, not the published daemon image:
only the client steps are evidence about the published release. Each receipt
from it carries that statement as `peer_note`.

| Column | PostgreSQL | ONNX Runtime |
| --- | --- | --- |
| macOS Intel | PostgreSQL 18.7 and pgvector 0.8.7 built from their source archives, checked by SHA-256, because Homebrew has no Intel bottle | 1.23.2 PyPI wheel, the last with an Intel macOS build |
| macOS Apple silicon | Homebrew bottles | 1.27.0 PyPI wheel |

Every wheel is checked against its PyPI SHA-256 before use. Setup shares one
time budget per job: 25 minutes on Apple silicon, 35 on Intel. Model provisioning is retried up to three times inside that budget,
without rebuilding the daemon. A job whose peer cannot come up still runs the
offline refusal probes and leaves the daemon rows pending with the setup
step's reason.

Every home used with the test peer has a Codex home folder before the
install, as after a first Codex start. Without it the published v0.25.1
installer's Codex setup fails. The client rows therefore do not cover a
never-started Codex, and each of their receipts says so. [Issue
#1897](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1897)

The daemon is not supported on Linux arm64 for now (the maintainer's
decision of 23 November 2026): no arm64 daemon image is published. A client
on arm64 against a daemon on a supported host is not ruled out. The arm64 job
runs only the two stock rows, as witnesses, in about seven minutes. Their homes have no Codex folder, so the
clean install row witnesses issue #1897. The clean install runs the tagged
installer's default Docker install, which can fall back to a checkout-built
Python daemon. The upgrade runs the real release updater from a stock
v0.24.0 install and records the missing arm64 image manifest. The three
client rows count as "cannot run", with that decision as their reason. [Issue
#1872](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1872)

The Mac receipts record GUI launchd availability. The published native
update scheduler supports Linux and Windows. [GitHub runner
specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories)

## What can pass

Clean install can pass on both Mac columns. Upgrade and rollback can pass on
Apple silicon. After the hand-off it runs the native installer twice with the
inputs of the legacy install. The first run may change only the Codex settings
the harness expects for that starting release, each the expected way. For
v0.23.1 and v0.24.0 that is the `memory_message` approval those installers
lost, added as `"approve"` ([issue
#1903](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1903)), and,
until [issue #1908](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1908)
is fixed, the token-file setting the rerun re-points. The second run must leave
every fingerprinted file byte-identical and print the installer's "nothing
changed" line. On Intel the upgrade cannot start: the v0.23.1 Python client
it upgrades from no longer installs there, because one of its dependencies
has no Intel macOS build. The legacy clients install with the job's Python
3.12, which those releases supported. The other rows record their observed steps
and stay pending, each with one sentence naming what is missing:

- Ordinary use needs a live tunnel, which a hosted job may not open.
- Failure and retry needs the launch and interruption fault probes, which
  the harness does not yet inject.
- Bank recovery needs a restore, and restore replaces only a Docker bank.
  The test peer is not one, and no published daemon image runs as one on
  these runners.

For releases affected by the fresh-bank pairing bug, the harness makes an
MCP board update through the installed shim and waits for the bank fingerprint
before issuing a disposable invite. [Pairing issue
#1864](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1864)

## Reading the results

The summary opens with a verdict. It reads "pass" only when every selected
cell passed. Otherwise it reads "no pass", with separate counts of passes,
fails, cells that cannot run and missing receipts. A green job with pending
cells is still "no pass". Below the table, each cell without a pass has its
one-sentence reason.

Download an `acceptance-<platform>` artifact for the receipts. Each one has
the fixed command labels, observed exit codes, deciding assertions, daemon
kind and engine. `peer-setup.json` records every setup step and the free disk
around the build. A failed setup or product step also keeps a `failure`
entry with its step, exit status, error class and its last output lines.
Those lines pass through `ops/acceptance/sanitize.py` first. That removes every
token, password and pairing code the job generated, URL credentials and
query strings, private keys, secret-shaped values, user names in home paths,
host names and non-loopback addresses. Raw output never reaches a receipt
or the job log unfiltered.

The `ledger/` folder contains version 1 records for the selected column.
These records are for the [installed acceptance
ledger](../../ops/acceptance/README.md#ledger), which describes how to record
them against the release commit and manifest. The adjacent receipts retain
partial observations when a ledger row needs more evidence.

The workflow records the daemon kind and engine on observed rows. It does not
count a pass with a source-built or checkout-built peer as a pass against the
published daemon. Once arm64 daemon and PostgreSQL images ship, the
published-image path needs its own digest and architecture checks.
The Linux x64 and Windows x64 columns come from their separate disposable-host
runs.
