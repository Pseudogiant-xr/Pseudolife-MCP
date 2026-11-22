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
The jobs use signed-out Codex CLI installations and disposable bank
credentials.

| Column | Daemon peer | Coverage limit |
| --- | --- | --- |
| Linux arm64 | The native arm64 Docker stack that the tag's installer builds on the runner. | The stock v0.25.0 installer can fall back to a checkout-built Python daemon and PostgreSQL. The published daemon and PostgreSQL images are Linux amd64. The workflow records container image provenance and exercises the real release updater from a stock v0.24.0 install without changing its image selection. |
| macOS Intel | The published Linux amd64 daemon in a private Lima VZ guest, if the runner can start and reach it. | The Mac client uses the guest's private address. The Docker backup and restore command runs inside the guest. |
| macOS Apple silicon | A daemon built from the selected release tag, with a private PostgreSQL cluster, if the runner can compile and start it. | The published Mac client is exercised against the source build. A published Docker daemon and its bank recovery remain open. |

The Ubuntu arm64 runner includes Docker. The macOS Apple silicon runner has no
nested virtualization, and the Intel runner can deny direct private network
access to the guest. The Intel peer probe has a 12-minute limit per attempt.
The Apple silicon source peer probe has a 25-minute limit per attempt. A Mac
job that cannot start and reach its peer records offline observations and
leaves the daemon-dependent results pending. [GitHub runner
specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories)
· [Lima private networking](https://lima-vm.io/docs/config/network/vznat/)
· [macOS local network access](https://github.com/actions/runner-images/issues/10924)

The ordinary-use scenario leaves the live tunnel pending in every hosted
column. Each Mac runner probes the GUI launchd domain before recording any
login-dependent evidence. The published native update scheduler in v0.25.0
supports Linux and Windows. The signed-out run leaves provider wake and
browser sign-in pending.

For releases affected by the fresh-bank pairing bug, the harness makes an
MCP board update through the installed shim and waits for the bank fingerprint
before issuing a disposable invite. [Pairing issue
#1864](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1864)

The workflow summary covers the 15 hosted cells. Download an
`acceptance-<platform>` artifact for the fixed command labels, observed
exit codes, deciding assertions and daemon type. Unavailable prerequisites
leave a cell pending. An observed assertion failure fails the job. The
artifact contains no raw command output, pairing codes, tokens or paths.
The Linux artifact also records the stock install's exit, elapsed time,
sanitized diagnostics, Codex registration and image provenance. The upgrade
receipt records whether the original daemon continued serving and whether
the runtime, configuration and volumes changed.

The `ledger/` folder contains version 1 records for the selected column.
Copy these into a new [installed acceptance
ledger](../../ops/acceptance/README.md#ledger) bound to the same release
commit and manifest. The adjacent receipts retain partial observations when
a ledger row needs more evidence. The Linux x64 and Windows x64 columns
come from their separate disposable-host runs.
