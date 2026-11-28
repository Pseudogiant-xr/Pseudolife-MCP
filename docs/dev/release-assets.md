# Native release assets

The native CLI archives publish with every release
(`.github/workflows/release.yml`), because the installers download them,
and they are on the GitHub release before PyPI publishes. Older updaters
learn that a release exists from PyPI and then download its archives.
Native discovery waits for `RELEASE-READY.json`, which the workflow attaches
after protected publication and both image jobs succeed. Releases need
the Rust image for discovery because it is the default Docker engine.

## Names and targets

`<version>` is the release version without the tag's leading `v` (for
example, tag `v0.21.0` means version `0.21.0`). Assets belong to that exact
GitHub release and are built from its tagged commit.

| Target | Asset name | Archive member |
|---|---|---|
| `x86_64-unknown-linux-gnu` | `pseudolife-mcp-<version>-x86_64-unknown-linux-gnu.tar.gz` | `pseudolife-mcp` |
| `aarch64-unknown-linux-gnu` | `pseudolife-mcp-<version>-aarch64-unknown-linux-gnu.tar.gz` | `pseudolife-mcp` |
| `x86_64-apple-darwin` | `pseudolife-mcp-<version>-x86_64-apple-darwin.tar.gz` | `pseudolife-mcp` |
| `aarch64-apple-darwin` | `pseudolife-mcp-<version>-aarch64-apple-darwin.tar.gz` | `pseudolife-mcp` |
| `x86_64-pc-windows-msvc` | `pseudolife-mcp-<version>-x86_64-pc-windows-msvc.zip` | `pseudolife-mcp.exe` |

Each binary archive contains just the named regular file at its root, with
no enclosing directory. POSIX executables have mode `0755`. The source
binary is Cargo's `pseudolife-stdio`, built with its default features and
renamed for installation. These binaries need no Python runtime.

The Linux builds use Ubuntu 22.04 (glibc 2.35) on each architecture;
macOS builds set a deployment target of 11.0. The Windows build uses MSVC
on the Windows 2022 runner and links the C runtime statically. The Windows
client can start without the Microsoft Visual C++ Redistributable. The
workflow checks the packaged Windows executable's normal and delay-load
imports against the system DLL allowlist, then executes its `version`
command. The other targets also run `version` on their native runners.
The runner labels follow [GitHub's hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## Checksums and compose bundle

`SHA256SUMS` is UTF-8 text with one lowercase SHA-256 digest, two spaces,
and the exact asset basename per line. It lists all five binary archives
and `pseudolife-compose-<version>.tar.gz`, sorted by basename; it does not
hash itself. Consumers fetch it and the selected archive from the same
release, verify the archive before extraction, and refuse missing or
duplicate entries and digest mismatches. A missing native asset means
that release predates the native set (0.23.1 and earlier publish none) or
its publication job failed.

`RELEASE-READY.json` is separate from the checksum manifest. It contains
the release `version`, the full tagged `commit` SHA, and an `images` object
mapping `pseudolife-pg` and `pseudolife-daemon-rust` to their published
`sha256:` digests. Releases before 0.26.0 also map `pseudolife-daemon`, the
retired Python daemon image, which 0.26.0 and later no longer publish. The
JSON contains no timestamp or run attempt.
These fields give operators a stable record of what the workflow approved
and published. Discovery uses the marker's asset metadata from GitHub.

The compose archive has these six regular files at its root, copied from
the release's `ops/` directory without rewriting their contents:

- `docker-compose.yml`
- `docker-compose.ghcr.yml`
- `docker-compose.rust-daemon.yml`
- `docker-compose.rust-shadow.yml`
- `docker-compose.rust-ghcr.yml`
- `Dockerfile.extractor`

The Rust GHCR overlay resolves the extractor build context to this bundle
directory and its `Dockerfile.extractor`. That Dockerfile has no local
`COPY` inputs; it downloads its GGUF in a build stage and copies it into
the llama.cpp server image. A fresh native install builds this sidecar
locally, just as the existing installer does; it needs network access for
the first build but no Python. The sidecar has no published GHCR image
contract. Daemon-only updates keep it unchanged. Future local extractor
build inputs must be added to this bundle before release.

The disposable development stack `docker-compose.rust.yml` is excluded.
Release-mode consumers no longer need a Python wheel to obtain compose
files. Install each release in its own directory:
`~/.local/share/pseudolife-mcp/compose/<version>/` on POSIX and
`%LOCALAPPDATA%\\pseudolife-mcp\\compose\\<version>\\` on Windows.
Extract into a temporary sibling directory after checksum verification,
then rename it into place. A complete version directory is immutable;
never overwrite it in place or replace the previous version's directory.
Different versions coexist so rollback can select the prior bundle;
retention is the installer's or updater's policy. Resolve each filename
against the selected version directory.
For Rust release activation, use the base file, the GHCR file, and the
Rust GHCR overlay in that order. Set `PSEUDOLIFE_IMAGE_TAG=<version>`;
the Rust overlay refuses an absent tag and clears the inherited local
build definition with Compose's `!reset` tag. A failed pull cannot build
the Python image under a Rust tag. Release updates pull the selected
image and recreate with `--no-build --no-deps`, after their backup and
rollback safeguards. The checkout and shadow overlays remain available
for their respective updater paths; `PSEUDOLIFE_RUST_IMAGE` must be set
to the selected image when using them with a release.

## How a release is discovered

The daemon discovers releases with one unauthenticated GET to
`https://api.github.com/repos/Pseudogiant-xr/Pseudolife-MCP/releases/latest`.
The native client's unpinned release updates, update checks and unattended
Docker updates use the same endpoint and metadata contract. Client-only
updates on a separate machine still follow the configured daemon's version.
Use GitHub's latest release as selected by this endpoint. Do not fall back
to PyPI or an older release when that answer is incomplete.

- Require JSON booleans `draft: false` and `prerelease: false`.
- Require `tag_name` to be `v<version>` with exactly one lowercase `v`.
  The version has at most 32 ASCII characters and two to four nonempty
  decimal components separated by dots, each fitting an unsigned 64-bit
  integer. No whitespace, sign, suffix or build metadata is accepted.
  Preserve the version's spelling, including leading zeroes, for asset
  names and the reported version. This mirrors the client's `plain_release`
  shape and `version_key` integer range.
- Require `RELEASE-READY.json`, `SHA256SUMS`, the five binary archive names
  in the table above, and `pseudolife-compose-<version>.tar.gz`, using that
  exact version in the archive names. Each required name must occur exactly once, with `state` equal
  to `uploaded` and an integer `size` greater than zero. Unrelated assets
  are ignored. Discovery checks metadata only; installation still verifies
  the checksum manifest and the downloaded bytes. The publisher verifies
  that manifest before uploading, but uploads missing assets alphabetically:
  `SHA256SUMS` can arrive first and is not a completion marker by itself.
- Compare numeric component tuples lexicographically, ignoring leading
  zeroes; if one is a prefix, the longer tuple is greater (`1.2 < 1.2.0`).
  For the running daemon, compare its leading dotted numeric components,
  preserving the existing prerelease-notice policy: `1.2.0-rc1` and
  `1.2.0` have equal numeric tuples. Only a strictly greater release produces
  the release notice. `/health` reports the accepted release even when equal
  to or older than the running daemon.

The request has no credentials, body or installed-version telemetry. Send
`Accept: application/vnd.github+json` and the fixed User-Agent
`pseudolife-daemon` (daemon) or `pseudolife-mcp` (client), disable redirects,
and use a five-second total timeout. The client makes one request per
discovery with no retry. An unattended Docker update runs daily and carries
the discovered tag into deployment. An unattended client-only update makes
no discovery request. Hosts behind the same public IP share the GitHub limit.

The background task checks immediately at startup; neither startup nor a
request waits for the result. Keep the last accepted release and its
`checked_at` in memory through failures; restart starts with an empty cache.
An incomplete asset set is not a new release and uses the normal configured
interval, even before the first success. Other failures are silent and retry
after `min(interval, 900 seconds)` until the first success, then the normal
interval. There is no immediate retry.

For HTTP 403 or 429, wait at least the normal interval. Parse `Retry-After`
first (nonnegative integer seconds or an HTTP date), otherwise
`X-RateLimit-Reset` (integer Unix seconds). Clamp the indicated remaining wait
to zero through 3,600 seconds, then take the maximum with the configured
interval; absent or invalid headers use that interval. The ordinary interval
defaults to six hours and retains its 60-second floor. GitHub's unauthenticated
limit is shared by source IP (60 requests per hour); the minimum interval can
consume that entire allowance. No ETag is stored: incomplete publication and
the last accepted version remain separate, without conditional-response state.

The existing `updates.check_releases: false` setting and
`PSEUDOLIFE_RELEASE_CHECK=0` environment override still disable all checks.
The marker closes the gap between native asset attachment and protected
publication. The new client reports nothing newer for an unmarked release
and the new daemon offers no notice for that version. The daemon can still
report its last accepted release from a previous check.

| Consumer | Marked release | Unmarked release |
|---|---|---|
| Released 0.24.0 clients | Discover through PyPI and use the selected release's assets. | Still follow PyPI and can update when the wheel is visible. |
| Client with the readiness rule | Accepts the target once the archives, checksum manifest and marker are uploaded. | Unpinned discovery reports nothing newer. |
| Daemon with the original GitHub rule | Accepts a complete archive and checksum set. | Can offer a release once the original asset set is complete. |
| Daemon with the readiness rule | Accepts the full asset set and can advertise a newer version. | Offers no notice for that release. |

The first cut with this rule must publish the traditional asset set, wheel,
all three images and `RELEASE-READY.json` from the same tagged commit.

## Image and publication gates

The Rust image is
`ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:<version>`. Until the default
switch is separately approved, it publishes version tags only, never
`latest`. The initial image target is `linux/amd64`; CLI target support
does not imply a daemon image for every target. The runtime installs
`pseudolife-daemon` and `/usr/local/bin/pseudolife-mcp`; the latter supports
`docker run --rm <image> pseudolife-mcp version` without provisioning models.

Every published release runs the image job; no repository variable gates it,
because a skipped image job would leave the marker absent without failing
the run. On a published release, `guards` checks the tag, Python package metadata,
both `server.json` version fields and both native crate versions. Then
`build` builds and checks the wheel and sdist while `rust-assets`,
`rust-assets-bundle` and `rust-assets-publish` build, verify and attach the
native assets independently. `publish` requires both the wheel build and the
native attachment to succeed before the `pypi` environment asks for approval.
The image jobs and registry wait for `publish`. The `release-ready` job
requires successful `publish`, `images` and `rust-image` jobs. A skipped,
failed or cancelled image job leaves the marker absent.

A failed wheel build can leave the complete native asset set on the public
GitHub release before approval. Explicit tags can reach those assets, and a
daemon with the original GitHub discovery rule can announce the release.
The approval job and both image jobs are skipped while the wheel build
has failed, so PyPI-based clients and native consumers with the readiness
rule do not discover the new version.
Build-only CI proofs skip the marker job, asset attachment and image
publication. Installers and pinned updates still treat an asset as
unavailable until the release actually carries it (a 404 or a missing
`SHA256SUMS` line refuses with nothing changed).

Release-mode `pseudolife-mcp update --engine rust` (both the Python and the
native updater) pulls `pseudolife-daemon-rust:<target>` with
`docker-compose.rust-ghcr.yml` applied after `docker-compose.ghcr.yml`. When
the target release has no such image, the pull fails before any backup or
rollback tag and the host is unchanged. The Python daemon is retired
(maintainer, 27 November 2026), so the native updater moves every Python
host to Rust on an attended update, whatever choice it recorded. A release
that publishes no Rust image (`docker manifest inspect`) is refused, and a
move that refuses before its backup (no bearer, a pending counters import,
a newer bank schema, a configuration the Rust image refuses) stops with its
refusal and leaves the Python daemon running untouched. On a Python host
an unattended run holds for an attended one, `--no-backup` is refused, and
only the bearer handover (`--allow-auth-change`) runs on Python. Release
mode refuses `--engine python`; checkout mode still builds either engine.
On a Rust host an update without `--engine` keeps Rust, so a release published without its Rust image stops
that host's updates at the pull (nothing changed) until the image exists. The overlay keeps the
pinned models in the project volume `pseudolife_rust_models` at `/models`,
outside the `/data` volume that every backup archives; the updater
provisions them there before its backup.

To prove the matrix without publication, dispatch `release.yml` on the
candidate branch. The native build and bundle jobs upload an Actions
artifact named `native-release-complete`. The version-guard job, wheel build
and publishers are skipped. The
matrix still checks native crate versions. A release event builds the same
archives after `guards` succeeds, then verifies the downloaded
`SHA256SUMS` immediately before attaching the complete set to the
already published GitHub release; PyPI, the images and the registry wait
for that attachment. The archive attachment job requires exactly the six listed
archives plus the checksum manifest. On a retry, it downloads and verifies every already
published asset, including `SHA256SUMS`, before uploading only missing assets;
any differing published bytes fail the job without replacement. Rerun the
publication job with the same complete artifact after a partial upload.
Re-running the failed jobs of a release run reruns only what failed and
what waits for it: a failed target build rebuilds that target, then the
bundle, the attachment (only missing assets) and PyPI's approval; a
failure after the attachment leaves the assets in place. When the marker
job is retried, it uses the successful jobs' image digests and verifies any
existing marker byte for byte. It uploads the marker only if absent.
A differing existing marker requires a maintainer decision before removal.
Use "Re-run failed jobs" to reuse successful build outputs. Never use
"Re-run all jobs": rebuilt archives need not be byte-identical, so the
attachment refuses the new `SHA256SUMS` and PyPI cannot follow until the
attached assets are deleted. If a rerun
cannot clear a marker failure, the tagged commit's script still runs on
each attempt. The maintainer chooses between checking the two published
images and attaching a hand-built `RELEASE-READY.json` with
`gh release upload`, or cutting a patch release. If approval is declined,
the marker stays absent and native GitHub discovery withholds the release.
To abandon a version after declining approval, delete the GitHub release
or its assets: its archives are already attached, so a version-pinned
native client install still downloads them and daemons with the original
GitHub rule still announce it, and while it stays GitHub's latest release,
current native discovery sees nothing newer.
Packaging fixes
gzip/tar timestamps at the Unix epoch and ZIP timestamps at 1980-01-01;
identical input bytes produce identical archives with the same packager.
