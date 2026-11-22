# Hosted release acceptance

Use **Actions → Hosted release acceptance → Run workflow** to exercise the
published native client on Linux arm64, macOS Intel, or macOS Apple silicon.
Select the columns and enter a published release tag. The default is
`v0.25.0`. The workflow runs only when dispatched.

Each selected runner checks out the release tag, verifies the native archive
and Compose bundle against the release's `SHA256SUMS`, and calls
`PSEUDOLIFE_SHIM_ENGINE=native bash ops/install.sh` from that checkout.
The installer and native client run with Python and pip off their PATH.
Legacy start and rollback operations use Python in a separate environment.
The jobs use signed-out Codex CLI 0.171.0 installations and disposable bank
credentials. A full dispatch uses up to four hosted slots across the platform
jobs and the summary, with a maximum of 75 minutes per platform job.

| Column | Daemon peer | Coverage limit |
| --- | --- | --- |
| Linux arm64 | The Linux arm64 checkout build produced by the tagged installer, with its engine recorded from the running image. | The stock v0.25.0 installer can fall back to a checkout-built Python daemon and PostgreSQL. The published daemon and PostgreSQL images are Linux amd64. The workflow records container image provenance and exercises the real release updater from a stock v0.24.0 install without changing its image selection. |
| macOS Intel | The published Linux amd64 daemon in a private Lima VZ guest, if the runner can start and reach it. | The running daemon image must match the Rust daemon digest in the verified release readiness marker. The Mac client uses the guest's private address. The Docker backup and restore command runs inside the guest. |
| macOS Apple silicon | A daemon built from the selected release tag, with a private PostgreSQL cluster, if the runner can compile and start it. | The published Mac client is exercised against the source build. A published Docker daemon and its bank recovery remain open. |

The Ubuntu arm64 runner includes Docker. The macOS Apple silicon runner has no
nested virtualization, and the Intel runner can deny direct private network
access to the guest. The Intel peer probe shares a 12-minute budget across
up to two attempts. The Apple silicon source peer probe shares a 25-minute
budget across up to two attempts. A Mac job that cannot start and reach its
peer records offline observations and
leaves the daemon-dependent results pending. [GitHub runner
specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories)
· [Lima private networking](https://lima-vm.io/docs/config/network/vznat/)
· [macOS local network access](https://github.com/actions/runner-images/issues/10924)

Five cells can reach `pass` with the current probes. Those are clean
install on all three platforms and upgrade and rollback on both Mac
platforms. The ordinary-use scenario leaves the live tunnel pending in
every hosted column. The Mac receipts record GUI launchd availability.
The published native update scheduler in v0.25.0 supports Linux and Windows.
The signed-out run leaves provider wake and browser sign-in pending.

For releases affected by the fresh-bank pairing bug, the harness makes an
MCP board update through the installed shim and waits for the bank fingerprint
before issuing a disposable invite. [Pairing issue
#1864](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1864)

The workflow summary covers the 15 hosted cells. Download an
`acceptance-<platform>` artifact for the fixed command labels, observed
exit codes, deciding assertions, daemon kind and engine. Unavailable prerequisites
leave a cell pending. An observed assertion failure fails the job. The
artifact contains no raw command output, pairing codes, tokens or paths.
The Linux artifact also records the stock install's exit, elapsed time,
sanitized diagnostics, Codex registration and image provenance. The upgrade
receipt records whether the original daemon continued serving and whether
the runtime, configuration and volumes changed. The authenticated HTTP sample
starts alongside the operation and does not gate it.

The `ledger/` folder contains version 1 records for the selected column.
These records are for the [installed acceptance
ledger](../../ops/acceptance/README.md#ledger), which describes how to record
them against the release commit and manifest. The adjacent receipts retain
partial observations when a ledger row needs more evidence.

The workflow records the daemon kind and engine on observed rows. It does not
count a pass with a source-built or checkout-built peer as a pass against the
published daemon. Once arm64 daemon and PostgreSQL images ship, the
published-image path needs its own digest and architecture checks. A
checkout-build observation remains evidence for that installation path.
The Linux x64 and Windows x64 columns come from their separate disposable-host
runs.
