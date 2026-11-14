# Native release assets

The native CLI archives publish with every release
(`.github/workflows/release.yml`), because the installers download them,
and they are on the GitHub release before PyPI publishes. The updaters
learn that a release exists from PyPI and then download its archives,
and PyPI cannot be unpublished, so a failed native build stops the
release before PyPI. The Rust daemon image needs the human-approved
`publish` job, and additionally its
repository variable, which stays enabled since it became the default
Docker engine in 0.23.0. This contract does not authorize a release.

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
on the Windows 2022 runner. The workflow uses native runners for all five
targets and executes each unpacked archive's `version` command there.
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

## Image and publication gates

The Rust image is
`ghcr.io/pseudogiant-xr/pseudolife-daemon-rust:<version>`. Until the default
switch is separately approved, it publishes version tags only, never
`latest`. The initial image target is `linux/amd64`; CLI target support
does not imply a daemon image for every target. The runtime installs
`pseudolife-daemon` and `/usr/local/bin/pseudolife-mcp`; the latter supports
`docker run --rm <image> pseudolife-mcp version` without provisioning models.

The image job is enabled only by the maintainer setting
`PSEUDOLIFE_PUBLISH_RUST_DAEMON=true`; unset or other values disable it.
The native asset set has no variable: it publishes on every release
event once the `build` job's version guards passed, before the `publish`
(PyPI) job, which needs it. Build-only CI proofs neither attach release
assets nor push images. Installers and updaters still treat an asset as
unavailable until the release actually carries it (a 404 or a missing
`SHA256SUMS` line refuses with nothing changed).

Release-mode `pseudolife-mcp update --engine rust` (both the Python and the
native updater) pulls `pseudolife-daemon-rust:<target>` with
`docker-compose.rust-ghcr.yml` applied after `docker-compose.ghcr.yml`. When
the target release has no such image, the pull fails before any backup or
rollback tag and the host is unchanged. A host that never chose an engine
(Python, with no accepted selection recorded) moves to Rust on an attended
update without `--engine` when the target release publishes the image
(`docker manifest inspect`), and stays on Python with one line otherwise or
when the move refuses before its backup (no bearer, a pending counters
import, a newer bank schema); unattended runs and `--no-backup` never move,
and `--engine python` records the choice to stay. On a Rust host an update without `--engine` keeps Rust, so a release published without its Rust image stops
that host's updates at the pull (nothing changed) until the image exists;
`--engine python` switches back on the same bank. The overlay keeps the
pinned models in the project volume `pseudolife_rust_models` at `/models`,
outside the `/data` volume that every backup archives; the updater
provisions them there before its backup.

To prove the matrix without publication, dispatch `release.yml` on the
candidate branch. The native build and bundle jobs upload an Actions
artifact named `native-release-complete`; all publishing jobs are skipped,
even if the Rust image variable is enabled. A release event builds the same
archives after the `build` job's guards, then verifies the downloaded
`SHA256SUMS` immediately before attaching the complete set to the
already published GitHub release; PyPI, the images and the registry wait
for that attachment. Publication requires exactly the six listed archives plus the
checksum manifest. On a retry, it downloads and verifies every already
published asset, including `SHA256SUMS`, before uploading only missing assets;
any differing published bytes fail the job without replacement. Rerun the
publication job with the same complete artifact after a partial upload.
Re-running the failed jobs of a release run reruns only what failed and
what waits for it: a failed target build rebuilds that target, then the
bundle, the attachment (only missing assets) and PyPI's approval; a
failure after the attachment leaves the assets in place.
Packaging fixes
gzip/tar timestamps at the Unix epoch and ZIP timestamps at 1980-01-01;
identical input bytes produce identical archives with the same packager.
