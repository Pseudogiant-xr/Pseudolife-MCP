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
- The 0.24 client move is opt-in. The installer-default change and subsequent
  retirement need their installed proof; publishing archives alone does not
  prove a supported native client.
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
| Native bank-move transaction | `rust/shim/src/cli/restore_cli/move_native/` | #1583 implements the supported Docker move, including dry run, rollback and target release. See **Move completion** for the remaining shapes and proof. |
| Native provider extractor shims and autostart | `rust/shim/src/extractor_shim/`, [extractor harness](../../rust/extractor_harness/README.md) | Existing Python autostart registrations still need migration proof; do not port these endpoints again. |

## Reuse the open work first

This is a dated queue, not an instruction to label or merge these heads.
Read the current diff, reviews, platform evidence and batch admission result;
head updates and completed work do not inherit an older CI claim.

| Existing work | Order or hold | What the conductor must establish |
|---|---|---|
| #1588 in #1589: 0.24 cut | Merged; 0.24.0 released | Done: see step 1 of the dependency order. |
| #1541 → #1556 → #1560: native installers, native helpers, preflight/hooks without Python | Merged. Native install is opt-in. | Reuse the installer paths for the installed acceptance matrix. |
| #1568: terminal connect and unattended schedule retargeting | Merged in #1613 (16 November 2026) | Preserve a declined prompt, hidden input, unrelated configuration and the existing schedule's time/kind. |
| #1559 → #1577; #1579, #1580, #1584 | At the time of writing #1559, #1579, #1580 and #1584 are in integration batch [#1591](https://github.com/Pseudogiant-xr/Pseudolife-MCP/pull/1591); #1577 is stacked on #1559 and follows it | Reuse release/batch preparation and CI fixes; do not merge policy edits on assumed delegation. |
| #1586: Windows Codex backup-path reporting | At the time of writing in batch #1591; check overlap with the installer stack | Keep normal user-visible paths and long-path support. |
| #1583: move transaction and recovery | Merged | Remaining shapes include carried tokens, `--expose-target`, custom env/compose files and checkout targets. |
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
| **Client workflow completion** | `rust/shim/src/cli.rs` and [the cutover command table](../../rust/CUTOVER.md#what-is-left) | The sweeps finished native answers for `connect` (#1677), `invite` and `pair` (#1679), `export` and `import` (#1676), `board-audit` (#1699), `doctor` (#1704), `conductor-watch` (#1723), `expose` (#1636, #1725), `test-login` (#1727), `maintainer` (#1730) and `console` (#1736). `tunnel` and `move` are the two remaining dispatcher deferrals. Some tunnel arguments and profile operations still fall through. Move falls through when its native handlers decline the command line. The rows below own that work. Real Console launch on macOS is unverified. |
| **Tunnel lifecycle and update** | `rust/shim/src/cli/tunnel/`, `cli/update/clients/` and `cli/update/tunnel_guard.rs` | `tunnel update` checks process records and reports running or unconfirmed tunnels as stale while leaving them in place. The client switch refuses a host with a registered Python tunnel autostart. Finish and prove setup, verification, start, stop, refresh, interrupted refresh, restart and the next normal operation. Keep keys private, refuse ambiguous authority, and retain a running tunnel when refresh fails. |
| **Move completion** | Continue #1583 and its approved transaction design in `restore_cli/move_native/`; do not create a competing implementation | Finish remaining supported token/exposure/config shapes as bounded follow-ups. Build on #1583's disposable Docker runs. Prove the remaining shapes and current recovery paths on disposable hosts: abort, interrupted commit, rollback after target writes, resume and target release. At most one daemon serves the bank; uncertain target state keeps the source fenced; recovery preserves writes. Reuse `cli_move_contract` and the PR's failure-injection cases. |
| **Schema ownership and legacy upgrade route** | `rust/daemon/src/storage/{schema.sql,schema_meta.rs,schema_plan.json,schema.rs}`, `daemon/harness/gen_schema_sql.py`, `shim/src/cli/update/{engine,release}.rs` | Rust consumes committed artifacts, but generation still parses/hashes `pseudolife_memory/storage/schema.py`, and checkout update reads it. Establish one native-owned schema source, update readers/guards and the schema-bump procedure together, without a gratuitous schema bump. Preserve startup transaction/order, newer-schema and dimension refusals. Run schema-generation/guard/startup tests and checkout-update cases. |
| **Console output relocation** | Readers include `frontend/vite.config.ts`, `frontend/scripts/check-dist.mjs`, `ops/Dockerfile.daemon-rust` (its `COPY` of the output, pinned by `rust/daemon/test_container.py`), `daemon/container_inputs.py`, `container_probe.py`, `daemon/harness/run.py` (the replays' static directory), the tests in `rust/daemon/src/static_files.rs`, the freeze exemption in `rust/ci/python_freeze.py`, `.gitattributes` and the committed-build check in `ci.yml` | Move the built output and every active reader together. Verify a clean frontend build, static route/cache/path tests and the Console served from the packaged image. Preserve fonts/licenses/vendor files. Coordinate a changed frozen-package output path with freeze enforcement; do not quietly broaden the exception. |
| **Native security and fixture independence** | `daemon/harness/{schema_cases,startup_cases,pgdisposable}.py`, `tests/{conftest,pg_fixtures,helpers}.py` | Security half done (16 November 2026): CI no longer compares security cases against live Python. All 69 cases of the three live scenarios (`security-refusals`, `security-remote-open`, `security-remote-auth`) are Rust tests the Rust job runs: `rust/daemon/tests/security_startup.rs` (startup off-loopback without a token, exact trust-bind opt-in, invalid/reserved token maps), and `src/http_security_tests.rs` (every OS) with `tests/security_admission.rs` (Linux process) for the open and closed cases configured on 0.0.0.0 (Host/Origin restrictions, pairing Origin, bearer enforcement, authorization order). The loopback goldens of the "Daemon security admission golden" step still replay; the three scenarios stay runnable by hand with `run.py live`. Every `run.py` invocation but `embedding` loads the package, so the golden shards need this work too. Replace package-based seeding, bank-name/login guards and shared helpers without weakening disposable-bank checks. Golden replay must still detect a deliberately changed response/state. |
| **Release discovery and publication independence** | `shim/src/cli/update/mod.rs::latest_release`, `daemon/src/release_check.rs`, `rust/ci/release_assets.py`, `.github/workflows/release.yml`, `server.json` | The native client and daemon use GitHub's latest release and require the complete uploaded asset set. Ship the discovery transition while installed clients can still learn it. Before stopping wheel publication, resolve the legacy import dependency, separate version and asset checks from wheel build and PyPI approval, retain an explicit human publication gate, and establish a registry representation the registry accepts. Preserve pinned `--tag` and rollback. |
| **Retained evals and final deletion** | `evals/{regression_gate.ps1,rebuild_contexts.py,ladder_sweep.py,retrieval_replay.py,memory_policy_bench.py,memory_policy_daemon.py}`, CI, package guards/docs | Choose retained evals with the maintainer; port their implementation calls to a disposable Rust daemon/HTTP/MCP adapter. A Python driver is acceptable. Keep old artifacts and their implementation provenance intact; do not present frozen-Python scores as measurements of Rust. Remove dead evals only after that decision. Then delete the package and obsolete consumers as a coherent, validated change. |

### Two release dependencies that are easy to miss

**The legacy-bank import leg currently selects the target version's Python
image.** In `rust/shim/src/cli/update/release.rs`, the pending-CMS/schema branch
calls `deploy_release` with the engine temporarily set to Python and the same
target version before the Rust leg. Both legs use the selected version.
CUTOVER documents stepping through 0.23.1. Stopping Python image publication
without changing that path breaks an upgrade past the final Python image. Implement
the documented intermediate-release route, or refuse before mutation with a
working explicit route. Prove old schema, pending counters, failed import and
already-migrated banks on disposable copies. The native updater need not
implement Chroma or `weights.pt` imports itself.

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
