# Rust cutover: where it stands and what is left

A Docker install of Pseudolife-MCP runs the Rust daemon. Everything on the
user's own machine (the stdio shim, the `pseudolife-mcp` command, the
installers and the updater) still runs Python. This page records what is true
on master and what is left before Python can be deleted. It replaces the
October 2026 plan that lived here; that plan's decisions are kept, shortened,
under "Decision record".

Status audit: master `201311de3`, 14 November 2026, read from the code and
the workflows, not from release notes. Nothing here authorizes a release, a
deploy or a live-bank change.

## Decisions in force

| Date | Decision (maintainer) | Effect |
|---|---|---|
| 2026-11-14 | The bar is that the Rust system works and does what its users need. Matching Python exactly is no longer required. | New Rust work carries its own Rust tests. No new differential rows, source mutants or two-OS recordings against Python. The committed goldens stay as regression snapshots of the Rust behaviour and are re-recorded from Rust when behaviour changes on purpose. |
| 2026-11-14 | Python is hard-frozen. | Nothing under `pseudolife_memory/` changes again, except the Console build output in `pseudolife_memory/web/static/` until it moves. A defect is fixed in Rust only. The one exception is the already finished program-lookup security fix, which lands as the last Python change. This replaces the soft freeze of 2026-10-23 (D10). |
| 2026-11-14 | The lite tier is dropped. | Embedded PostgreSQL and file mode are not ported. A lite bank gets a refusal that names the Docker install. This reverses the lite half of D1. |
| 2026-11-14 | Banks older than the Rust daemon can open step through 0.23.x. | A bank below schema 57, or one that still keeps counters in `weights.pt` or documents in Chroma, is first updated to 0.23.1, whose Python daemon imports them. Those imports are not ported. |
| 2026-11-14 | `move` and `tunnel` are rebuilt in Rust as features, after the native client ships. | Until then the native command refuses plainly. Everything else unported is finished or retired first (below). |
| 2026-11-14 | Two CI trims: the Python lanes do not re-run on the master push of a Rust-only batch, and the source mutants run weekly, not nightly. | Folded into the CI change under "Stop comparing with Python". |
| 2026-11-07 | G2: Docker installs and updates select the Rust daemon. | Shipped in 0.23.0 (2026-11-11). `DEFAULT_ENGINE` is `"rust"` in `pseudolife_memory/update_engine.py` and `rust/shim/src/cli/update/release.rs`. |

## What runs where today

| Surface | What a release-mode user runs | Python? |
|---|---|---|
| Daemon, Docker tier | `ghcr.io/…/pseudolife-daemon-rust:<version>`, the default engine for a new install and for an attended update. Its final image stage is `debian:bookworm-slim` with `pseudolife-daemon` and a native `pseudolife-mcp`; it holds no Python. | No |
| Daemon on a host that chose Python, or whose bank the Rust daemon cannot open yet | `ghcr.io/…/pseudolife-daemon` (`ops/Dockerfile.daemon`). | Yes |
| Stdio shim and `pseudolife-mcp` on the user's machine | A Python virtual environment the installers build with `ops/shim_runtime.py`; every client registration points at its launcher. | Yes |
| Installers (`ops/install.sh`, `ops/install.ps1`) | Shell and PowerShell that call Python helpers: `client_credentials.py` (the bearer token), `installer_state.py`, `installer_engine.py`, `preflight.py`, the Codex setup scripts and the Claude Desktop registrar. | Yes |
| `pseudolife-mcp update` | The installed Python updater; its client step installs a shim runtime from PyPI. The native updater runs only when asked for or when no Python is found. | Yes |
| Native client (`rust/shim`, binary `pseudolife-stdio`, installed as `pseudolife-mcp`) | Opt-in through `pseudolife-mcp runtime install --archive`. `release.yml` builds archives for five targets but publishes them only when the repository variable `PSEUDOLIFE_PUBLISH_RUST_ASSETS` is `true`; it is unset, and release 0.23.1 carries no assets. | Not installed |
| Plugin hooks | bash and PowerShell scripts (`plugin/hooks/`). | No |
| Published packages | The PyPI wheel, the Python daemon image, the PostgreSQL image, the Rust daemon image, and an MCP registry entry (`server.json`) that points at PyPI. | PyPI, the Python image and the registry entry |

The native `pseudolife-mcp` never falls back to Python. A shape it has not
ported prints `mode '<mode>' is deferred in this candidate` and exits 1
(`rust/shim/src/cli.rs`, `dispatch`). The day the native client is what the
installers register, each such shape is a dead end for the user, so each one
below is either finished or turned into a refusal that says the true cause and
the fix.

## What is left

### 1. No Python on a user's machine

1. The native shim on Windows starts without a board address about half the
   time on a slow machine (#1324) and refuses the markers the plugin hooks
   write, so board mail never wakes a session (#1326).
2. The native updater runs the client steps after the shim step in the old
   binary (#1308).
3. The native archives publish with every release.
4. The installers and `update` install the native client, re-point existing
   registrations at it, and stop calling Python helpers.
5. The registered shim can start a local daemon without Python. Today
   `rust/shim/src/lifecycle.rs` starts one only from `PSEUDOLIFE_MCP_PYTHON`
   or `PSEUDOLIFE_MCP_SERVE_COMMAND`; with the lite tier dropped this becomes
   a refusal that names the Docker install.
6. Each command below works natively or refuses plainly:

| Command | Native today | Left |
|---|---|---|
| `restore` | The checks before any change. | The restore itself: stop the daemon, safety backup, drop, replay, state archive, restart; the confirmation prompt. |
| `conductor-watch` (`delegate-watch`) | Help, `--stop`, the setup refusals. | The watch loop and `--detach`. |
| `connect` | Most shapes. | Pairing codes, the Codex configuration, the interactive prompts. |
| `expose` | Nothing. | The whole command; done on a branch with the program-lookup fix. |
| `move` | The refusals before any change. | Rebuilt later; until then a plain refusal. |
| `tunnel` | Status and a pending-only update. | Rebuilt later; until then a plain refusal. |
| `embedded` | Nothing. | Retired with file mode; a plain refusal. |
| `backup`, `export`, `import`, `invite`, `maintainer`, `lease`, `board-audit` on a lite bank | A refusal or a deferral. | One refusal that names the Docker install. |
| Edge shapes in `doctor`, `console`, `connect`, `invite`, `pair`, `board-audit` | Several hundred `Defer` sites that reproduce Python's handling of unusual input. | A sensible native answer; no reproduction of Python quirks. |

The release that makes the native client the default is accepted on installed
proof, not on parity: a fresh install, an update from the previous release, a
backup and restore, a rollback and a real client session, on Windows, Linux and
macOS.

### 2. Stop comparing with Python

CI spends most of its hosted time comparing Rust with a live Python oracle or
testing Python code that can no longer change. Measured from runs of 13 and 14
November 2026 (job durations, not billed minutes): an ordinary pull request
about 140 to 156 hosted minutes, a Rust-only batch about 445 (about 355 of
them in the parity shards), a master push about 230.

1. A check fails an ordinary pull request that changes `pseudolife_memory/`.
2. No workflow builds or runs a Python oracle: the judges suite, the frozen
   judge probes, the live arm of each parity shard, `cli-live`, the nightly
   body evidence and the wait-mail capture stop. Golden replays stay.
3. The Python lanes run only the tests that survive Python: the installers and
   update wrappers, the `ops/` tooling, the plugin hooks, the docs and release
   guards, and the CI tooling. Tests of the frozen package are not collected
   by default.
4. Issue #1378 (the daemon's `w2e.py golden` replay cannot catch a
   regression) is fixed or that replay is replaced by Rust tests.

What remains after that is the Rust build and test, mostly on Windows.

### 3. Delete Python

1. The Console build output moves out of `pseudolife_memory/web/static`
   (`frontend/vite.config.ts` writes it; `rust/daemon/src/static_files.rs`
   and `ops/Dockerfile.daemon-rust` read it).
2. Whatever still reads `pseudolife_memory/storage/schema.py` (the schema
   generator, the native updater's engine switch) reads a Rust source.
3. A last PyPI release carries a deprecation note; the Python daemon image
   stops publishing; `server.json` points at what the native install offers.
4. `pseudolife_memory/`, the tests that only test it, the oracle harness code
   under `rust/` and `evals/rust_port/`, and the Python lanes are deleted.
5. `evals/` is decided separately: about half of its scripts import the
   package and would have to drive the Rust daemon over HTTP.
6. Follow-ups that waited for this: #1357 (regenerate the Unicode tables).

## Decision record (October and November 2026)

These are the decisions the October plan rested on, with their outcome. Code
comments and parity rows cite them by ID. The full text of each, and of the
batch labels B1 to B17 that some comments cite, is in this file's history
(`git log -- rust/CUTOVER.md`).

| ID | Question | Outcome |
|---|---|---|
| D1 | Lite and embedded tiers: port or retire. | 2026-10-23: port lite, retire embedded and file mode. Lite dropped 2026-11-14. |
| D2 | Where counters and band weights persist. | A bank table (`cms_counters`, schema 57), with a one-time import of `weights.pt` by the Python daemon. |
| D3 | Tokenless MCP rebinding gate. | Accepted; implemented (#987). |
| D4 | Model provisioning. | Provisioned at install or first start with a pinned checksum (#1114), not baked into the image. |
| D5 | Existing Chroma documents. | Migrated once into the `reference_chunks` table by the Python daemon; Chroma is not in the Rust daemon. |
| D6, D11 | Evidence bar for a ported surface. | A published nightly differential run. Superseded 2026-11-14: the bar is that it works. |
| D7 | Declared divergences from Python. | Triaged 2026-11-07; the register under `rust/daemon/divergences/` is now a record, not a gate. |
| D8 | Operator-only CLI modes. | `coordination-recovery` ported; the rest are in "What is left". |
| 2026-10-19, Option B | Graph insight: port NetworkX's Louvain or not. | The Rust daemon computes communities and sampled betweenness with its own deterministic algorithm; compared structurally with Python, not byte for byte. |
| D9 (Proposal A, batch B2r) | Console routes no Console code calls. | Five retired and answered with a declared 501 by the Rust daemon (`routes/console.rs`, `D9_RETIRED`); three episode routes ported. |
| D10 | Soft Python freeze. | Replaced by the hard freeze of 2026-11-14. |
| G1 | Read-only shadow on the live bank. | Approved 2026-11-06 and run. |
| G2 | Rust as the default daemon. | Decided 2026-11-07; shipped in 0.23.0. |
| G3 | Release and deploy. | 0.23.0 and 0.23.1 released and deployed on the Rust daemon. |
| G4 | Freeze, then retire Python. | Frozen 2026-11-14; retirement is "What is left". |

The eight named cutover blockers of the October plan (tokenless MCP
rebinding, the document store, pairing over HTTP, the clock retry, the shared
clock, model provisioning, image model agreement, Console sign-in) are closed.

## Historical measurement

This timing study predates the batch-only heavy lanes and is kept because a
test recomputes its figures from the committed artifact.

Measured per batch, 2026-10-23, over every integration batch opened since
2026-10-19 ([`evals/results/batch-lifecycle-20261023/`](../evals/results/batch-lifecycle-20261023/README.md),
`python ops/batch_timeline.py`): 8 merged batches (n=7 before #890's
batch-only heavy CI, n=1 after it) and one closed and rebuilt. Before #890,
a member's median time from opened to `ready-to-batch` was 7h47m (n=21),
and the median from its last commit to the label 4h38m; from the label to
merged it was 2h35m. A batch's CI first pass took a median 1h17m (n=7), with
a median 15m07s of queue wait on its critical path (37% pooled over the 8
merged batches, 19% without #868's burst); local validation took 10m09s
and the build seconds. Every merged batch's critical path ran through a
Windows parity shard (8 of 8). The one post-#890 batch (#892, n=1) took
1h16m from its PR to merged, 15m43s of it a rerun of a macOS lane the
ruleset does not require, which no hosted runner had acquired. So the wait
before a change joins a batch, which includes its review, is the largest
measured cost; GitHub holds no review event here, so it cannot be split
further. One post-#890 batch is not a trend; rerun the report as batches land.
