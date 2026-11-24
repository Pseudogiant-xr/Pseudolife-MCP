# Finish the move to Rust

The remaining migration is chiefly a client rollout and the removal of build,
test and release dependencies on the frozen Python package. The Rust daemon
already runs the Docker tier. This handoff identifies the dependencies that
must be removed before deleting `pseudolife_memory/`, with bounded work items
and acceptance checks for the next conductor.

Audit baseline: `c6afc43ce`, 15 November 2026; refreshed against master
`4c6062535` the same day. Release 0.24.0 is published (tag `v0.24.0` at
`4c6062535`, 15 November 2026), through the cut
[#1588](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/1588) and its
integration batch
[#1589](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/1589), and the
move to the native client is opt-in in it. The installer, move and tunnel
state and the client-workflow row were re-read at master `690bc4402` on
18 November 2026. Re-read the queue below before acting.

## Read this before assigning work

Read [CLAUDE.md](../../CLAUDE.md), the decisions in
[CUTOVER.md](../../rust/CUTOVER.md), and
[merge-batches.md](merge-batches.md). The 0.24 cut's CUTOVER inventory is on
master: do not overwrite it with another rewrite.
Use this page for execution dependencies and the package-deletion work.

Carry these decisions forward:

- Python is frozen; fix behaviour in Rust. A frozen-package exception needs
  the maintainer's ruling, not merely a label another agent supplied.
- Acceptance means working supported workflows, rather than byte-for-byte
  reproduction of Python. Keep useful Rust regression goldens; do not create
  another differential-port programme.
- Lite, embedded and file mode are retired in the native client. Preserve the
  documented old-bank upgrade route through a release that still opens them;
  do not rebuild those engines or discard their state.
- The installers and an attended `update` select the native client by
  default on master (the 0.26.0 change); an unattended update never changes
  the client. The release and the retirement after it need their installed
  proof; publishing archives alone does not prove a supported native client.
- Merge, publication, deploy and live-bank changes retain their existing
  user-owned gates. A previous conductor's delegation is not transferred by
  this handoff.

## What has already landed

At the audit baseline, the following are implemented, rather than new work:

| Surface | Evidence to start from | Remaining distinction |
|---|---|---|
| Rust Docker daemon and model provisioning | `ops/Dockerfile.daemon-rust`, `rust/daemon/src/` | Its final image has no Python; the ONNX wheel-extraction build stage may still use Python. |
| Native archives and compose bundle before PyPI | `.github/workflows/release.yml`, [release-assets.md](release-assets.md) | Workflow implementation and a build-only dispatch are distinct from published, downloadable assets. |
| Windows board registration, hook-marker ownership and updater handoff fixes | Landings #1536, #1544, #1549, #1535; `rust/shim/tests/update_client_handoff.rs` | Reuse their regressions; do not reopen the original ports from stale CUTOVER prose. |
| Docker backup, destructive restore, conductor-watch | `rust/shim/src/cli/backup/`, `restore_cli/`, `conductor_watch/` | Exercise installed binaries and recovery, rather than reimplementing these commands. |
| Retired-mode and lite-bank refusals | `rust/shim/src/cli/retired.rs`, `rust/shim/tests/cli_retired_contract.rs` | `serve` does not need a second daemon implementation in the client. |
| Python freeze, live/frozen test classification, Rust golden recorders | `rust/ci/python_freeze.py` (the freeze check `python-freeze.yml` runs), `rust/ci/inventory.py` (live invocations), `tests/suite_classes.txt`, [DEV.md](../../rust/DEV.md) | Every `rust/daemon/harness/run.py` invocation but `embedding` loads the Python package at module load (through `pgdisposable.py` and `daemons.py`). |
| Native bank-move transaction | `rust/shim/src/cli/restore_cli/move_native/` | #1583 implements the supported Docker move, including dry run, rollback and target release. See **Move acceptance** for what is still to prove. |
| Native provider extractor shims and autostart | `rust/shim/src/extractor_shim/`, [extractor harness](../../rust/extractor_harness/README.md) | Existing Python autostart registrations still need migration proof; do not port these endpoints again. |

## Reuse the open work first

This is a dated queue, not an instruction to label or merge these heads.
Read the current diff, reviews, platform evidence and batch admission result;
head updates and completed work do not inherit an older CI claim.

| Existing work | Order or hold | What the conductor must establish |
|---|---|---|
| #1588 in #1589: 0.24 cut | Merged; 0.24.0 released | Done: see step 1 of the dependency order. |
| #1541 → #1556 → #1560: native installers, native helpers, preflight/hooks without Python | Merged. Native install is the installers' default on master (the 0.26.0 change). | Reuse the installer paths for the installed acceptance matrix. |
| #1568: terminal connect and unattended schedule retargeting | Merged in #1613 (16 November 2026) | Preserve a declined prompt, hidden input, unrelated configuration and the existing schedule's time/kind. |
| #1559 → #1577; #1579, #1580, #1584 | At the time of writing #1559, #1579, #1580 and #1584 are in integration batch [#1591](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/1591); #1577 is stacked on #1559 and follows it | Reuse release/batch preparation and CI fixes; do not merge policy edits on assumed delegation. |
| #1586: Windows Codex backup-path reporting | At the time of writing in batch #1591; check overlap with the installer stack | Keep normal user-visible paths and long-path support. |
| #1583: move transaction and recovery | Merged | Native release-mode moves can carry bearer settings to a Linux Docker target, accept explicitly selected original files, and use a target tailnet forward with `--expose-target`. Checkout handover receives an early refusal with the installed release route. |
| #1526: historical shared-row refactor | Parked | Do not revive it merely to finish migration; native behaviour tests avoid the original shared-row conflict. |

## Dependency order

1. **Verify the published bridge release.** Done for 0.24.0: the six
   published archives were checked against the release's `SHA256SUMS` (the
   seventh asset) and the bridge's own archive and checksum code, and the
   opt-in move ran from 0.23.1 and from 0.24.0 against the real release on
   Windows and Linux. The ordinary 0.24 update stays on Python as specified.
   The procedure, for a later release the bridge moves clients to: check the
   exact release's complete asset set with its real checksum manifest, then
   run one opt-in transition from the previous Python client in a disposable
   home, and test the installed client-handoff probes and registration
   changes. A build artifact alone is insufficient.
2. **Finish the remaining workflows and reuse the native installers.** See
   the move and tunnel work below. Publishing an installer that selects
   native by default needs the installed acceptance matrix.
3. **Prepare package independence in parallel.** Schema ownership, Console
   output, native security tests, fixture writers and release discovery have
   separate owners and need no new Python features.
4. **Release and prove the native default.** Prove install, upgrade, ordinary
   use and rollback on supported platforms before removing the old client
   escape path. Keep the old image/artifacts needed for legacy banks.
5. **Remove the package and its obsolete consumers.** First select the evals
   that survive and replace their package imports; then remove obsolete
   oracle code, packaging and publication edges. Python used to orchestrate
   pytest, CI or evals is not itself a runtime migration blocker.

## Bounded implementation work

Each row is a separate brief. An agent reads the code and existing PRs for its
row, writes a failing behavioural test before changing behaviour, and returns
the reviewed head, actual commands/results, limitations and dependencies.
Keep reviews and platform requirements from CLAUDE.md; this table adds no
review rounds or new CI jobs.

| Work item | Sources and boundary | Acceptance and useful existing tests |
|---|---|---|
| **Client workflow completion** | `rust/shim/src/cli.rs` and [the cutover command table](../../rust/CUTOVER.md#what-is-left) | The sweeps finished native answers for `connect` (#1677), `invite` and `pair` (#1679), `export` and `import` (#1676), `board-audit` (#1699), `doctor` (#1704), `conductor-watch` (#1723), `expose` (#1636, #1725), `test-login` (#1727), `maintainer` (#1730), `console` (#1736) and `move`. The native move dispatcher owns help, argument refusals, execution and recovery. Some `tunnel` arguments and profile operations still defer. Real Console launch on macOS is unverified. |
| **Tunnel lifecycle and update** | `rust/shim/src/cli/tunnel/`, `cli/update/clients/` and `cli/update/tunnel_guard.rs` | Native setup creates and resumes prepared profiles, imports and renews private keys, and recovers interrupted renewal. It retains the previous key and expiry until the replacement completes a control-plane poll, with explicit restore and discard commands. `tunnel run` supervises a foreground launch and confirms the live vendor and its bound bridge are ready. `tunnel stop` requests owned shutdown. Native verification binds receipts to the launch identity. A compatible supervisor checks the installed shim protocol and saved daemon before replacing its child. It attempts rollback when the candidate fails readiness and its exit is confirmed. Pre-protocol foreground supervisors and vendor version changes require a manual restart. The client switch refuses a host with registered Python tunnel autostart. Finish background start and service registration and migration, then prove the installed lifecycle on each platform. Keep keys private and refuse ambiguous authority. |
| **Move acceptance** | #1583 and its follow-ups in `restore_cli/move_native/`, `cli_move_contract` and the move harness row | The native client can move to installed Linux Docker targets, carry bearer settings with explicit Compose files, or keep the target’s settings. It can manage tailnet exposure and recover an interrupted move. Every `move` command line ends in native code; one the native parsers cannot take is refused with its cause named, before any record or host is read. Still to prove: the installed acceptance matrix below (move interruption and rollback on a supported disposable Docker deployment), and a real-process kill of the source client after its commit marker (`moved.json`) reaches the source and before its move record says committed, a window only the engine model tests cover. |
| **Schema ownership and legacy upgrade route** | `rust/daemon/src/storage/{schema.sql,schema_meta.rs,schema_plan.json,schema.rs}`, `daemon/harness/gen_schema_sql.py`, `shim/src/cli/update/{engine,release}.rs` | The Rust daemon owns the base DDL in `schema.sql` and the ordered startup plan, version and embedding dimension in `schema_plan.json`. The existing schema check validates those inputs and the derived `schema_meta.rs`. Checkout update still needs to read the native schema version. Preserve startup transaction/order, newer-schema and dimension refusals. Run schema-generation/guard/startup tests and checkout-update cases. |
| **Console output relocation** | The committed build lives in `frontend/dist/`. The Rust image, static replays, shipped-asset tests, producer census and CI read that directory. The Python image and wheel package the same files at the frozen daemon's existing path. | The frontend job verifies the rebuilt bytes. The disposable Rust image job verifies the served bytes, including fonts, licences and vendor files. The move uses the existing freeze exemption for the old build path. |
| **Native security and fixture independence** | `daemon/harness/{schema_cases,startup_cases,pgdisposable}.py`, `tests/{conftest,pg_fixtures,helpers}.py` | Security half done (16 November 2026): CI no longer compares security cases against live Python. All 69 cases of the three live scenarios (`security-refusals`, `security-remote-open`, `security-remote-auth`) are Rust tests the Rust job runs: `rust/daemon/tests/security_startup.rs` (startup off-loopback without a token, exact trust-bind opt-in, invalid/reserved token maps), and `src/http_security_tests.rs` (every OS) with `tests/security_admission.rs` (Linux process) for the open and closed cases configured on 0.0.0.0 (Host/Origin restrictions, pairing Origin, bearer enforcement, authorization order). The loopback goldens of the "Daemon security admission golden" step still replay; the three scenarios stay runnable by hand with `run.py live`. Replace package-based seeding, bank-name/login guards and shared helpers without weakening disposable-bank checks. Golden replay must still detect a deliberately changed response/state. Schema, startup hydration and clock-retry goldens now use package-free fixtures. Run the writer session control by hand with `python rust/daemon/harness/writer_session_cases.py --rust-test-bin <mutants-test-bin> --mutants --out <scratch>/writer-session.json`. Expect exit 0, `writer session: 0 diffs; mutants all caught`, and `caught: true` in the receipt for both `writer-no-reconnect` (witness `a live session to kill`) and `writer-epoch-unchecked` (witness `assertion failed: current.tick_hlc() > (4_102_444_800_000, 3)`). |
| **Release discovery and publication independence** | `shim/src/cli/update/mod.rs::latest_release`, `daemon/src/release_check.rs`, `rust/ci/release_assets.py`, `.github/workflows/release.yml`, `server.json` | Native clients and daemons discover GitHub releases with the complete uploaded asset set and readiness marker. Version checks gate the native assets independently of the wheel build. Keep the current PyPI-backed MCP Registry listing frozen and mark it deprecated with a pointer to native installation once that route is proven. A native registry bundle is separate work. After proving native install and upgrade, publish one final bridge wheel with a deprecation note, then stop wheel publication. Keep publishing the Python daemon image until the legacy import (#1778) lands and retain the pinned 0.23.1 image afterward. After the last wheel, the maintainer creates a dedicated release environment with required reviewers and release-tag protection before a workflow references it. Preserve pinned `--tag` and rollback. |
| **Retained evals and final deletion** | `evals/{regression_gate.ps1,rebuild_contexts.py,ladder_sweep.py,retrieval_replay.py,memory_policy_bench.py,memory_policy_daemon.py}`, CI, package guards/docs | Choose retained evals with the maintainer; port their implementation calls to a disposable Rust daemon/HTTP/MCP adapter. A Python driver is acceptable. Keep old artifacts and their implementation provenance intact; do not present frozen-Python scores as measurements of Rust. Remove dead evals only after that decision. Then delete the package and obsolete consumers as a coherent, validated change. |

### Two release dependencies that are easy to miss

**Legacy banks need the archived 0.23.1 image.** The native release updater
checks the bank and the selected Rust image before it replaces the daemon.
When a bank needs an import, the updater uses the target's verified Compose
bundle to run the archived 0.23.1 Python image by digest. It verifies the
schema, counters and any Chroma import before switching to the selected Rust
image. The archived image is available for Linux amd64 Docker engines.
Keep its digest available when Python image publication ends. The original
image tag, dump and state archive are retained separately from the Rust hop's
rollback. Prove old schema, pending counters, failed import and already
imported banks on disposable copies before retirement.

**PyPI is still both a release index and a workflow prerequisite.** Older
clients read its JSON endpoint, while release images and registry
publication wait for `publish`, and the native asset jobs depend on `build`'s
wheel validation. Deleting the Python package, removing PyPI jobs or changing
only `server.json` does not remove those edges. Select the replacement
discovery/registry contract first and ship a compatible transition.

## Installed acceptance matrix

Use disposable homes, credentials, client registrations, schedules and Docker
banks. Never use a personal client configuration or the live bank as a fixture.
Use actual release archives for final proof; stubbed installer tests and a
successful `version` probe cover different risks.

| Scenario | Windows x64 | Linux x64/arm64 | macOS Intel/Apple Silicon |
|---|---|---|---|
| Clean install with Python and pip absent from the executable search path | Real archive, installer, registration, first MCP session | Both supported archives; supported Docker host for a local bank | Both archives; compile/version alone is insufficient |
| Upgrade from 0.23.x and from 0.24 Python; opt-in, native rerun, rollback | Old runtime remains readable; ownership, locale and long paths | Existing registrations, umask/group ownership, links | Existing registrations, user paths and scheduling |
| Ordinary native use | Board registration/wake, connect, doctor, Console, tunnel, update | Same, with ordinary browser and user-service paths | Same; exercise browser and launchd paths natively |
| Bank recovery | Backup/restore and failure recovery on a supported disposable Docker host | Backup/restore, move interruption/rollback, old-bank upgrade route | Client-to-Docker operation; document actual host/target coverage |
| Failure and retry | Corrupt/missing archive, cancellation, locked files, interrupted refresh | Same plus child cleanup and interrupted publication | Same plus native cancellation and launch failure |

For each result record the commit, archive checksum, OS/architecture, command,
exit code, actual outcome and cleanup. A missing platform is **not yet run**,
not an inferred pass. Windows-sensitive changes retain the dev-VM and hosted
stress requirements in CLAUDE.md. Do not start a native Windows full suite.
Check disk and full-suite leases before heavy work; use the existing WSL/remote
launchers and batch gates rather than inventing parallel full-suite runs.

## Prove deletion, rather than counting Python strings

Use a disposable checkout of the final candidate with the package actually
absent. The following must still work:

1. Cargo check/build, Rust tests and the retained regression harnesses,
   including schema startup and security behaviour.
2. Native release archive and compose-bundle assembly from the new version
   authority, the image build, Console serving and release discovery.
3. Installer, native update, checkout update, client hooks and a real client
   session without a host Python runtime.
4. The retained Python test/eval runners with a tooling-only dependency set:
   no `pip install .`, editable package, implicit import from another
   checkout or Python daemon subprocess hiding a dependency.
5. Version/schema/docs/atlas/llms guards and the release procedure, updated
   for the surviving source paths and public install instructions.

Inventory imports, subprocesses, `include_str!`, Docker COPY inputs, workflow
installs and metadata readers. For example, run these against tracked files
(exit 1 from `git grep` means no matches):

```text
git grep -n -E '(from|import) pseudolife_memory' -- ops rust evals tests
git grep -n -F 'pseudolife_memory/' -- frontend ops rust .github pyproject.toml server.json
git grep -n -E 'pypi.org|pip install|python-runtime' -- rust .github ops
```

Classify matches as active dependency, intentional legacy compatibility,
retained test tooling, or historical evidence. Goldens, old evidence and
comments can legitimately contain Python names. A zero-string grep is not the
completion bar, and a broad deletion of every `.py` file would remove tooling
that still tests the native product.

Keep production prompt assets under `evals/prompts/`: Rust embeds them, and
the Docker builder copies them. An eval-directory cleanup must distinguish
those inputs from obsolete Python drivers. Also inspect recovery messages and
generated hook launchers: `cli/installer/codex_hooks/definitions.rs` still
preserves a `python ops/setup-codex-hooks.py` repair instruction. Give users a
working native repair path without silently changing approved hook definitions;
the hook approval rules still apply.

Read `tests/suite_classes.txt` as collection policy, not proof that live tests
are package-independent: a live guard can import Python helpers. The source
mutants were explicitly retired on 17 November 2026; manual stress oracle
selection still needs a replacement or an explicit retirement decision
before deleting its implementation (the security exceptions in `rust/ci/inventory.py` were
replaced by Rust tests and removed on 16 November 2026).

## Work to leave until afterwards

Keep performance profiling and Python-quirk cleanup
([#1533](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1533)) and the
Unicode refresh
([#1357](https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues/1357)) separate
from runtime retirement. Broad retrieval, graph and dream improvements are
not prerequisites merely because they are in the backlog.

The audit read source, the open PR inventory and release metadata, and its
code-anchored claims were re-read against master `4c6062535`; it did not
run the installed matrix, builds, migrations or evals. Existing PR validation
is evidence to inspect at its named head, not new verification of this handoff.
