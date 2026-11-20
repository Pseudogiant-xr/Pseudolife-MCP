# Rust cutover: where it stands and what is left

A Docker install of Pseudolife-MCP runs the Rust daemon by default. The
installers still select the Python client by default. Set
`PSEUDOLIFE_SHIM_ENGINE=native` on install or when updating from Python to
opt in to the native client. This page records the state on master and the
work left before Python can be deleted. The October 2026 plan's decisions
are under "Decision record".

The 14 November 2026 audit read the code and workflows at `201311de3`, with
a follow-up at `81057402a`. On 18 November 2026, the installer engine selection,
native dispatch, command rows changed by the sweeps, remaining `move` and
`tunnel` paths, the lite-bank refusal and D8 were re-read at master
`690bc4402`. The other release, CI and package-removal claims retain their
earlier audit. This is a source audit. Installed acceptance is a separate
gate below.

## Decisions in force

| Date | Decision (maintainer) | Effect |
|---|---|---|
| 2026-11-14 | The bar is that the Rust system works and does what its users need. Matching Python exactly is no longer required. | New Rust work carries its own Rust tests. No new differential rows, source mutants or two-OS recordings against Python. The committed goldens stay as regression snapshots of the Rust behaviour and are re-recorded from Rust when behaviour changes on purpose. |
| 2026-11-14 | Python is hard-frozen. | Nothing under `pseudolife_memory/` changes again, except the Console build output in `pseudolife_memory/web/static/` until it moves. A defect is fixed in Rust only. The one exception is the already finished program-lookup security fix, which lands as the last Python change. This replaces the soft freeze of 2026-10-23 (D10). |
| 2026-11-14 | The lite tier is dropped. | The embedded PostgreSQL port is not finished and file mode is not ported. A lite bank gets a refusal that names the Docker install, and the partial lite code (`rust/shared/embedded_pg.rs`, `pg_runtime.rs`, `native_provision.rs`) goes with Python. This reverses the lite half of D1. |
| 2026-11-14 | Banks older than the Rust daemon can open step through 0.23.x. | A bank below schema 57, or one that still keeps counters in `weights.pt` or documents in Chroma, is first updated to 0.23.1, whose Python daemon imports them. Those imports are not ported. |
| 2026-11-14 | `move` and `tunnel` are rebuilt in Rust as features, after the native client ships. | See their native subsets and remaining deferrals below. |
| 2026-11-14 | Two CI trims: the Python lanes do not re-run on the master push of a Rust-only batch, and the source mutants run weekly, not nightly. | Folded into the CI change under "Stop comparing with Python". |
| 2026-11-17 | Retire the CLI source mutants to reduce CI cost; reconsider a Rust-native mutation check after cutover. | Remove the job and its declarations, shards and upkeep checks. Rust unit/contract tests and committed golden replay remain; automatic proof that each planted source-line bug is detected is lost. Manual stress is unchanged. |
| 2026-11-07 | G2: Docker installs and updates select the Rust daemon. | Shipped in 0.23.0 (2026-11-11). `DEFAULT_ENGINE` is `"rust"` in `pseudolife_memory/update_engine.py` and `rust/shim/src/cli/update/release.rs`. |

## What runs where today

| Surface | What a release-mode user runs | Python? |
|---|---|---|
| Daemon, Docker tier | `ghcr.io/…/pseudolife-daemon-rust:<version>`, the default engine for a new install and for an attended update. Its final image stage is `debian:bookworm-slim` with `pseudolife-daemon` and a native `pseudolife-mcp`; it holds no Python. | No |
| Daemon on a host that chose Python, or whose bank the Rust daemon cannot open yet | `ghcr.io/…/pseudolife-daemon` (`ops/Dockerfile.daemon`). | Yes |
| Stdio shim and `pseudolife-mcp` on the user's machine | By default, a Python virtual environment the installers build with `ops/shim_runtime.py`. The client registrations point at its launcher. An install or update with `PSEUDOLIFE_SHIM_ENGINE=native` selects the native client behind that launcher. | Yes, unless native is selected |
| Installers (`ops/install.sh`, `ops/install.ps1`) | The default path calls Python helpers for credentials, state, engine choice, preflight and host registration. With `PSEUDOLIFE_SHIM_ENGINE=native`, the wrappers call `ops/install-native.sh` or `ops/install-native.ps1`. | Yes, on the default path |
| `pseudolife-mcp update` | The installed updater. The Python one's client step installs a shim runtime from PyPI and hands the steps after it to that runtime (`client-handoff-v1`); a 0.23.x updater's hand-off reaches this release's `pseudolife_memory/update_bridge.py`. The move is opt-in in 0.24.0: without `PSEUDOLIFE_SHIM_ENGINE=native` the bridge keeps Python and, on a hand-off where a native build exists for the platform, prints the opt-in command; with it, it downloads and verifies the native archive for the platform and runs the native updater's client steps in its place, or keeps Python and says why (a machine already on this release's Python client moves with that setting and `update --clients-only`). On a native client, the native updater. From a checkout, the wrappers (`ops/update.sh`, `update.ps1`, `update.py`) use the native updater only with `--native` or `PSEUDOLIFE_NATIVE_UPDATE=1`, or when no Python 3.10 or newer is found. | Yes, unless moved |
| Native client (`rust/shim`, binary `pseudolife-stdio`, installed as `pseudolife-mcp`) | Installed through the opt-in installer or update bridge, or by hand with `pseudolife-mcp runtime install --archive`. Every release builds the five native archives, the compose bundle and `SHA256SUMS` and attaches them to the GitHub release before PyPI publishes (`release.yml`: `rust-assets`, `rust-assets-bundle`, `rust-assets-publish`); release 0.23.1 carries none. A manual dispatch builds them without publishing. | No |
| Plugin hooks | bash and PowerShell scripts (`plugin/hooks/`). | No |
| Published packages | The PyPI wheel, the Python daemon image, the PostgreSQL image, the Rust daemon image, the native archives on the GitHub release, and an MCP registry entry (`server.json`) that points at PyPI. | PyPI, the Python image and the registry entry |

The generic dispatcher deferral prints `pseudolife-stdio: mode '<mode>' is
deferred in this candidate` on stderr and exits 1. The only entries in
`DEFERRED_MODES` in `rust/shim/src/cli.rs` are `tunnel` and `move`.
The command exits at that point without starting the Python client.

## What is left

### 1. No Python on a user's machine

1. The native shim on Windows starts without a board address about half the
   time on a slow machine (#1324) and refuses the markers the plugin hooks
   write, so board mail never wakes a session (#1326). Both fixed at
   `81057402a`.
2. The native updater runs the client steps after the shim step in the old
   binary (#1308). Fixed: it hands them to the new runtime.
3. The native archives publish with every release. Done.
4. Make the native client the default for install and update. Both paths
   currently select it on request with `PSEUDOLIFE_SHIM_ENGINE=native`.
5. The registered shim can start a local daemon without Python. Done:
   `rust/shim/src/lifecycle.rs` starts one only from
   `PSEUDOLIFE_MCP_SERVE_COMMAND`, and otherwise its note names the Docker
   tier's start command; `PSEUDOLIFE_MCP_PYTHON` no longer starts one.
6. Each command below works natively or refuses plainly:

| Command | Native today | Left |
|---|---|---|
| `backup` | A Docker bank (the verified dump and state archive, rotation) and an explicit DSN through `pg_dump`. | Nothing; a lite bank is refused. |
| `restore` | All of it for a Docker bank: the checks, the confirmation prompt, the safety backup, drop, replay, state archive and restart. | Nothing; a lite bank is refused. |
| `update` | The native updater supports release and checkout modes. The Python updater uses the opt-in client hand-off. A registered Python tunnel autostart blocks the switch. On the native path, the tunnel step reports running or unconfirmed tunnels as stale and leaves them in place. | Select native by default (item 4). Complete tunnel refresh. |
| `conductor-watch` (`delegate-watch`) | The watch loop, `--detach`, help and input refusals run natively. Invalid arguments, paths and PID records get a reason and a remedy before changes. `--lease-cli` is accepted and ignored. (#1723) | macOS runtime proof. |
| `connect` | Re-points Claude Code, Codex through its app-server, Claude Desktop and Gemini CLI. Handles pairing codes, Windows user-scope settings, terminal prompts and the unattended-update schedule. Help and supported option spellings run natively. Unreadable client files and unsupported arguments receive named diagnostics. (#1677) | — |
| `console` | Sign-in, session listing, revocation and help at any terminal width run natively. Invalid arguments, credential setup, daemon transport and response failures receive named diagnostics. An unsupported Windows `BROWSER` value prints the validated sign-in link with the cause. (#1736) | Real macOS browser launch is unverified. |
| `doctor` | Help and diagnostics run natively. Malformed arguments receive a usage error. An unreadable file, failed probe or invalid saved Codex connection is a finding with a cause and a repair. Independent checks continue. (#1704) | — |
| `serve` | A refusal that names the Docker install; the daemon is the separate `pseudolife-daemon` binary. The bounded HTTP candidate (`sent_serve.rs`) runs only for maintainer test harnesses. | Nothing. |
| `expose` | Status, consent, Tailscale serve changes and read-back run natively. Malformed arguments get an actionable usage error before host access. (#1636, #1725) | — |
| `test-login` | Help, argument checks, admin-URL and container setup, and private credential staging run natively. Preflight refusals name the failed condition. (#1727) | — |
| `maintainer` | Guided setup and administration (`enrol-code`, `confirm`, `revoke`, `reset`, `list`) run natively. Invalid arguments and database settings receive named refusals. (#1730) | — |
| `move` | On Linux Docker targets, preflight checks both bearer modes and the native transaction can carry bearer settings or keep the target's. Rollback and target release run natively. | Remaining exposure and configuration shapes. Prove those and the final recovery paths on disposable hosts. |
| `tunnel` | Status, read-only update reporting, the native bridge, and explicit setup of a fresh profile after consent and a daemon handshake. Equivalent native and Python-written profiles are reused on rerun. | Saved-profile changes and private key intake still defer. Finish verification and the running tunnel lifecycle, including refresh. |
| `embedded` | A plain refusal: retired with file mode. | Nothing. |
| `backup`, `restore`, `export`, `import`, `invite`, `maintainer`, `lease`, `board-audit` on a lite bank | One refusal that names the Docker install and the 0.23.x pip release that still opens the bank (`cli/retired.rs`). | Nothing. |
| Edge shapes in `tunnel` and `move` | The named deferral is still reachable. `tunnel` can defer on unsupported arguments, a profile directory it cannot open outside `update`, or work its `status`, `verify` and `setup` paths cannot complete. `move` can defer when its rollback, target-release, preflight and transaction parsers cannot handle the invocation. | Give supported operations a native result and other calls a refusal with the cause and remedy. |

The sweeps also completed native answers for `invite` and `pair` (#1679),
`board-audit` (#1699), and `export` and `import` (#1676).

The release that makes the native client the default is accepted on installed
proof, not on parity: a fresh install, an update from the previous release, a
backup and restore, a rollback and a real client session, on Windows, Linux and
macOS.

### 2. Stop comparing with Python

CI spends most of its hosted time comparing Rust with a live Python oracle or
testing Python code that can no longer change. Summed job durations (not
billed minutes) from single runs of 13 and 14 November 2026: an ordinary pull
request about 140 to 156 hosted minutes (runs 43433514840, 43392781569,
43392771163), a Rust-only batch about 445, about 355 of them in the parity
shards (run 43449162029), and a master push about 230 (runs 43456549508 and
43456549451).

1. A check fails an ordinary pull request that changes `pseudolife_memory/`,
   other than the Console build output in `pseudolife_memory/web/static/`.
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

1. The committed Console build lives in `frontend/dist/`. Both daemon
   images and the static-file tests read it there. The Python wheel maps it
   to the package's static directory at build time.
2. Whatever still reads `pseudolife_memory/storage/schema.py` (the schema
   generator, the native updater's engine switch) reads a Rust source.
3. A last PyPI release carries a deprecation note; the Python daemon image
   stops publishing; `server.json` points at what the native install offers.
4. `pseudolife_memory/`, the tests that only test it, the oracle harness code
   under `rust/` and `evals/rust_port/`, the partial lite-tier code under
   `rust/shared/`, and the Python lanes are deleted.
5. `evals/` is decided separately: about half of its scripts import the
   package and would have to drive the Rust daemon over HTTP.
6. Follow-ups that waited for this: #1357 (regenerate the Unicode tables).

## Decision record (October and November 2026)

These are the decisions the October plan rested on, with their outcome. Code
comments and parity rows cite them by ID. The full text of each, the batch
labels B1 to B17, and the sections older comments cite by name ("Ordered
plan", "Maintainer decisions and gates", "Recovery", "What sets the pace")
are in the previous version of this file: `git show 201311de3:rust/CUTOVER.md`.

| ID | Question | Outcome |
|---|---|---|
| D1 | Lite and embedded tiers: port or retire. | 2026-10-23: port lite, retire embedded and file mode. Lite dropped 2026-11-14. |
| D2 | Where counters and band weights persist. | A bank table (`cms_counters`, schema 57), with a one-time import of `weights.pt` by the Python daemon. |
| D3 | Tokenless MCP rebinding gate. | Accepted; implemented (#987). |
| D4 | Model provisioning. | Provisioned at install or first start with a pinned checksum (#1114), not baked into the image. |
| D5 | Existing Chroma documents. | Migrated once into the `reference_chunks` table by the Python daemon; Chroma is not in the Rust daemon. |
| D6, D11 | Evidence bar for a ported surface. | A published nightly differential run. Superseded 2026-11-14: the bar is that it works. |
| D7 | Declared divergences from Python. | Triaged 2026-11-07; the register under `rust/daemon/divergences/` is now a record, not a gate. |
| D8 | Operator-only CLI modes. | `coordination-recovery` and `maintainer setup` are native. The administration sweep finished the other `maintainer` actions (#1730). `conductor-watch` and `delegate-watch` answer natively for watch, detach and invalid inputs (#1723). |
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
