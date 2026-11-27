---
name: release-procedure
description: Use when cutting a release or publishing anything to a public surface — GitHub release, PyPI, the MCP registry, the GHCR container images, or the Claude Code plugin marketplace. Covers the docs currency pass, the seven-file version cut, build/inspect, the Trusted Publishing + registry + image automation, and verification.
---

# Release / publish procedure (five public surfaces)

GitHub releases, PyPI, the MCP registry, the GHCR container images, and the
Claude Code plugin marketplace (`.claude-plugin/marketplace.json` +
`plugin/` — served straight from master, no publish step; users pull via
`/plugin marketplace update`) all serve from this repo; a release touches
them in this order (first done 2026-07-16, v0.8.0; GHCR images added
2026-08-14).

0. **Docs currency pass before the cut** — three checks, and (a) is the one
   that gets skipped because nothing fails when you miss it.

   **(a) Absence — is the new behavior documented at all?** List the
   behavior changes since the last tag (`git log vN.N.N..HEAD`, the
   CHANGELOG's `[Unreleased]` plus the pending `changelog.d/` fragments) and
   ask of each: *which user-facing page
   describes this?* A capability with no guide entry contradicts nothing,
   so no guard test and no re-verify pass will ever surface it — only this
   question will. Schema v16–v18 shipped undocumented exactly this way, and
   0.10.0's headline extraction change lived in the CHANGELOG alone until
   it was caught by hand at the release gate. A CHANGELOG entry is a record
   of a change, not documentation of a behavior. Corollary: never exclude a
   file from this pass on the strength of an earlier read that was looking
   for something else — a narrow check does not justify a broad exclusion.

   **(b) Contradiction — do the existing claims still hold?** The guard
   tests pin numbers (schema, identifiers), but *framing* drifts silently:
   the 2026-07-16 pass found 15 stale claims the guards can't see.
   Re-verify the drift-prone claim classes against code before any release:
   what's bundled/default (extractor model + size, embedding weights),
   the transport story (HTTP-first; shim = host-process only), lifecycle
   ownership (episodes are daemon-owned; briefing is the only hook),
   tool count/tiers and the hidden-tools-need-expand rule, shipped config
   defaults (surprise gate is permissive), image/install sizes, and any
   "can't / doesn't / no X" absolute — those age worst. **Translated front
   doors** (`docs/i18n/README.{zh,ja,ko,pt-br,es}.md`): if the narrative or
   quickstart in `docs/i18n/README.source.md` changed, bump its
   `i18n-source` version and re-run the translation subagents — the guard
   (`tests/test_i18n_readme.py`) pins code blocks + sync markers but cannot
   read prose. Surfaces: README,
   **docs/guide/*.md** (the user-facing guide pages the 2026-07-16
   restructure moved the README's deep material into — configuration,
   retrieval, dreaming, episodes, memory-model, benchmarks; they carry the
   same drift-prone claims the README used to),
   CONTRIBUTING, SECURITY, evals/README, examples/ (CLAUDE.memory.md is
   injected into user CLAUDE.md / AGENTS.md files — its tool surface must match exactly),
   docs/runbooks, ops/.env.example comments. The README is the PyPI
   description, so its fixes only reach PyPI at the next version.

   **(c) Setup — does any change since the last tag leave the user a manual
   step?** For each behavior change, ask what a user must do by hand after
   `pseudolife-mcp update` or a fresh install: a config edit, a login, a
   serve, a command to run once. If the answer is anything, the release
   waits until the installer or update does it (CLAUDE.md, "Install and
   update experience"). The 2026-10-04 passkey and test-login changes held
   the release after 0.16.1 this way.

1. **First fold the changelog fragments**: `python ops/assemble_changelog.py`
   moves every `changelog.d/*.md` entry into `## [Unreleased]` (newest first)
   and deletes the fragment files. Review the folded section, then commit it
   with the cut. Fragments landed since 2026-10-09 exist only there until this
   runs.

   **Version cut touches seven files together**: the CHANGELOG (`## [N.N.N]`
   header over `[Unreleased]` — one fragile line; the tag↔section guard test
   exists because an adjacent edit once deleted it silently), `pyproject.toml`,
   the daemon image tag in **both** compose files (`ops/docker-compose.yml`
   and the packaged copy `pseudolife_memory/compose/docker-compose.yml`,
   so, as nothing under the package may change, the cut PR is an exception: the maintainer rules it for each cut, and only then does it carry the `freeze-exception` label, applied by the maintainer or by the conductor after that ruling),
   **both** version fields in `server.json`,
   `plugin/release.json` (pinned to pyproject by
   `tests/test_plugin_packaging.py`; the release the hooks report for the
   version handshake. `plugin/.claude-plugin/plugin.json` carries no
   version: Claude Code keys its cache by it, and the marketplace ships the
   plugin from every master commit), and
   `docs/atlas/atlas.json` `meta` (pinned by `tests/test_atlas_currency.py`;
   re-verify the map's claims, don't just renumber it — update
   `meta.verified` to the date you actually checked). `server.json` (both
   fields) and the `ops/docker-compose.yml` daemon image tag are also pinned
   locally by
   `tests/test_plugin_packaging.py::test_server_json_versions_match_pyproject`
   (issue #185) — run it as part of the cut, before tagging, so a missed
   field fails in the suite rather than mid-CI. Nothing pins the packaged
   compose copy's tag to the version: check it by hand.

   The release version's home is `[package] version` in
   `rust/shim/Cargo.toml`; before tagging, run
   `python rust/ci/release_version.py check --tag v<version>`, which names
   any version field the cut missed (`pyproject.toml` is one of them while
   the wheel is published).

   **While the release still publishes the Python package** (the wheel and
   the Python daemon image, whose dependencies are floors), the cut's local
   validation runs the whole suite with `--include-frozen`
   (`pwsh ops/wsl-suite.ps1 tests/ --include-frozen`), since a run collects
   only the live files otherwise and the frozen package's tests are what can
   see a dependency regression.

   **The same PR also bumps the native crates** (`evals/rust_port/README.md`,
   the version-only release paragraph; #751 did it for 0.18.1): the
   `version` in `rust/shim/Cargo.toml` and in `rust/daemon/Cargo.toml`
   (the Rust daemon's `/health` version), the `pseudolife-stdio` and
   `pseudolife-daemon` entries in `rust/Cargo.lock`, `PACKAGE_VERSION` in
   `rust/shim/src/lifecycle.rs`, and the version literals in
   `rust/shim/tests/auth_lifecycle_http.rs`
   Without the shim's, `evals/rust_port/test_phase1_inventory.py`
   (`test_candidate_runtime_identity_matches_the_selected_python_pin`)
   fails, as it did at the first 0.18.2 cut; without the daemon's,
   `tests/test_plugin_packaging.py::test_the_native_crates_report_the_release_version`
   fails.

   **Keep the cut to the release.** Since the Python freeze nothing in CI
   compares the cut with the old Python release (the parity jobs replay
   goldens, which carry no version), so the cut no longer has to pass
   `release_cut()`; keep unrelated fixes in their own PRs all the same, so
   the cut's review is the release. `ops/integrate.py build` batches a cut
   alone, because the release is tagged at the batch's merge: anything
   beside the cut would ship without being in the notes the cut folded. For
   the same reason `build` leaves out a cut that would leave a changelog
   fragment unfolded on the batch head (one that landed on master after the
   fold, or one the cut adds), saying so: merge master forward, run
   `python ops/assemble_changelog.py` again and rerun the pinned tests.
   The cut also carries `freeze-exception`, once the maintainer has ruled
   it for this cut, while it edits the packaged compose copy, or `build`
   drops it with the guard's refusal. Inside the package a cut may change
   only that file: `build` refuses a cut that changes any other package
   path, label or no label, naming the path.
   Tag `vN.N.N` at the exact commit the artifacts build from.
2. **Build + inspect before upload**: `python -m build`, `twine check dist/*`,
   then open the wheel. Every file returned by `git ls-files frontend/dist/`
   must appear byte for byte under `pseudolife_memory/web/static/` at its
   relative path within `frontend/dist/`. That wheel directory must contain
   no other files. The only wheel roots are `pseudolife_memory/` and the
   distribution's `.dist-info/` directory. Check the `mcp-name` marker in METADATA,
   no identifiers (grep the METADATA for the guard list).
3. **PyPI**: publishing the GitHub release triggers
   `.github/workflows/release.yml` (Trusted Publishing — OIDC, no token).
   The `guards` job checks the tag against the pyproject version and both
   `server.json` version fields, and checks that both native crate versions
   match. These checks run before either build or any upload (issue #185).
   Afterward, the `build` job builds and twine-checks the sdist and wheel
   while the native jobs build, verify and attach the five native CLI
   archives, Compose bundle and `SHA256SUMS`. The `publish` job asks for
   approval in the `pypi` environment after both paths succeed, so a failed
   native build or attachment still stops the release before PyPI. Existing
   PyPI-based updaters can obtain the archives once the wheel becomes
   visible. Native GitHub discovery waits for `RELEASE-READY.json`, which
   the workflow attaches only after the approved `publish` job and both
   image jobs succeed, so the PostgreSQL and Rust daemon images are both
   published. A manual wheel upload does not create the readiness
   marker.
   If the wheel build fails, the native assets can still attach to the
   public release before approval. Explicit tags and daemons with the
   original GitHub discovery rule can see the assets. PyPI upload, image
   and registry publication, and the readiness marker wait for a
   successful wheel build and the approval it enables. If the wheel build
   cannot be made to pass by re-running the failed job, abandon the version
   as step 5 describes, because its native assets are already public.
   Manual `twine upload
   dist/*` remains the fallback. PyPI never accepts a same-version
   re-upload — metadata-only fixes are a `.postN`.
4. **MCP registry — automated.** The `registry` job in `release.yml` runs
   after the PyPI job and authenticates with `mcp-publisher login
   github-oidc` (same trust model as Trusted Publishing: a short-lived token
   minted per run, nothing stored, nothing to expire between cuts). It waits
   for PyPI to actually serve the new version, checks the marker survived
   into the published description, publishes, then confirms the registry
   serves it as latest — the `server.json` version check itself already
   happened in `guards`, above. Publishing the GitHub release is now the
   single action that lands all four surfaces.
   *Manual fallback* (`mcp-publisher login github` then `publish`, from the
   repo root) if the job is ever broken — note the binary is often not on
   `PATH`; resolve it with `Get-Command mcp-publisher` rather than assuming.
   Either way: the README marker must read exactly
   `mcp-name: io.github.Pseudogiant-xr/pseudolife-mcp` — the namespace is
   matched **case-sensitively** against the GitHub username (capital P), and
   validation reads the **latest** PyPI release's description. The registry
   `description` field caps at 100 chars. Verify:
   `curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=pseudolife"`.
5. **GHCR images — automated.** The `images` job in `release.yml` (also
   downstream of the human-approved `publish` gate) builds and pushes
   `ghcr.io/pseudogiant-xr/pseudolife-pg:{18,18-<version>}` with the
   repo-scoped `GITHUB_TOKEN` — no stored secret. Since 0.26.0 no release
   publishes the retired Python daemon image
   `ghcr.io/pseudogiant-xr/pseudolife-daemon` (its earlier tags stay, and
   legacy bank imports run the archived 0.23.1 image by digest), so do not
   look for it. One-time after the FIRST release that runs the job: flip the
   package to public in its GHCR package settings, or pulls fail with
   "denied". Verify:
   `docker manifest inspect ghcr.io/pseudogiant-xr/pseudolife-pg:18-<version>`.

   **Rust artifacts.** One repository variable, disabled when unset, gates
   the Rust image, and its publication remains downstream of the human-approved
   `publish` job (the native archives, which have no variable, come
   before it). `PSEUDOLIFE_PUBLISH_RUST_DAEMON=true` publishes the
   separate `pseudolife-daemon-rust:<version>` image. Since 0.23.0 the Rust
   daemon is the default Docker engine, so confirm before the release that
   this variable is still `true`: with the image published, install and
   release-mode `pseudolife-mcp update` select it (`--engine python` keeps
   or switches back to Python); without it, no readiness marker is attached,
   so the native client's unpinned update and check report nothing newer and
   a daemon with the readiness rule gives no notice; clients that discover
   through PyPI, and `--tag <version>`, still reach the release, where a host
   that never chose an engine stays on Python and `--engine rust` stops at its
   `docker pull` with nothing changed. The Rust image gets only the explicit version
   tag; the workflow pushes no `latest` for it.
   The five native CLI archives, `SHA256SUMS` and
   `pseudolife-compose-<version>.tar.gz` attach to the release after
   `guards` passes, independently of the wheel build and ahead of
   `publish`. The installers download the archive for their host.

   After the run, use `gh release view v<version> --json assets` to check that
   the five native CLI archives, Compose bundle, `SHA256SUMS` and
   `RELEASE-READY.json` each appear once with state `uploaded` and a positive
   size. Use `gh release download v<version> --pattern RELEASE-READY.json` to
   verify the marker's version and commit match the tag and its digests match
   the published daemon, PostgreSQL and Rust images.
   `PSEUDOLIFE_PUBLISH_RUST_DAEMON` must be `true`: with it off the Rust image
   job is skipped, no marker is attached, and native clients and daemons never
   learn of the release. The build-only dispatch verifies the archive bundle
   and skips the marker job.

   Verify a published image with
   `docker manifest inspect ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:<version>`
   and flip its GHCR package to public after the first release that runs
   the job, as for the PostgreSQL image.

   Run the build-only `release.yml` workflow dispatch on the candidate
   first. The native build and bundle jobs must compile and execute the
   version command for each unpacked target binary and verify the complete
   Compose and checksum artifact. The native matrix checks the crate
   versions. Dispatch skips `guards`, the wheel build, PyPI, GHCR, the
   registry, GitHub asset attachment and the readiness marker. The
   build-only dispatch does not run `guards`, so the tag check
   (`release_version.py check --tag`) first runs on hosted CI at the real
   release. After an
   enabled release, inspect the Rust image digest, download the six
   archives plus `SHA256SUMS`, and verify every checksum and the archive-root
   layout in [the asset contract](../../../docs/dev/release-assets.md).
   The Rust GHCR package needs public visibility after its first approved
   push. On a release, `guards` checks native crate versions before either
   build starts.
   If native asset attachment stops after a partial upload, rerun the
   asset publication job with the same complete artifact. It requires the
   exact file set and checksums, downloads and verifies already published
   assets (including `SHA256SUMS`), and uploads only missing ones. It never
   replaces existing bytes; investigate a mismatch before publication.
   Use "Re-run failed jobs" to reuse the successful build outputs. Never use
   "Re-run all jobs": rebuilt archives need not be byte-identical, so the
   attachment refuses the new `SHA256SUMS` and PyPI cannot follow until the
   attached assets are deleted. Asset and
   marker publication verifies any existing intended bytes and uploads only
   missing assets. A differing existing marker requires a maintainer decision
   before any removal. If the marker job fails in a way a re-run cannot clear
   (it runs the tagged commit's own script, so a fault in it repeats), the
   maintainer chooses between attaching a hand-built `RELEASE-READY.json` with
   `gh release upload` after checking the two images by hand, and cutting a
   patch release. If approval is declined, the marker stays absent and native
   GitHub discovery withholds the release. Older consumers and explicit tags
   still use their existing routes. To abandon a version after declining approval,
   delete the GitHub release or its assets: its archives are already attached,
   so a version-pinned native client install still downloads them and daemons
   with the original GitHub rule still announce it, and while it stays GitHub's
   latest release, current native discovery sees nothing newer.
   Missing opt-in assets are a failed native publication, not permission
   to enable a fallback or change the default engine; keep installation
   and update readiness as release prerequisites.
6. **Deploy, then run the client step once.** `pseudolife-mcp update
   --clients-only` starts the updater of the runtime it was launched from,
   which is the *previous* release's. From the first release that carries
   the client hand-off (`CLIENT_HANDOFF` in
   `pseudolife_memory/update_cli.py`) onwards, that updater installs the new
   runtime and then hands the plugin, Codex and tunnel steps to it, so one
   run applies this release's client-step changes; its output says "the
   client steps after the shim continue in runtime …". When the release
   being replaced predates the hand-off (the hand-off release itself is the
   last such case), run `--clients-only` a second time from the new runtime
   (2026-10-05: 0.17.0's plugin-marketplace HTTPS move did nothing on the
   first run). Verify the release's own client-side changes after the run
   that printed the hand-off line, or after the second run.
