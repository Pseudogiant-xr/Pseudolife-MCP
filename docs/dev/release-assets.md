# Native release assets

The native CLI and Rust daemon image are opt-in. Publishing requires the
existing human-approved `publish` job in `.github/workflows/release.yml`
and an explicitly enabled repository variable. This contract does not
change the default engine or authorize a release.

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

## Checksums and compose bundle

`SHA256SUMS` is UTF-8 text with one lowercase SHA-256 digest, two spaces,
and the exact asset basename per line. It lists all five binary archives
and `pseudolife-compose-<version>.tar.gz`, sorted by basename; it does not
hash itself. Consumers fetch it and the selected archive from the same
release, verify the archive before extraction, and refuse missing or
duplicate entries and digest mismatches. A missing native asset means
that release did not publish the opt-in native set.

The compose archive has these five regular files at its root, copied from
the release's `ops/` directory without rewriting their contents:

- `docker-compose.yml`
- `docker-compose.ghcr.yml`
- `docker-compose.rust-daemon.yml`
- `docker-compose.rust-shadow.yml`
- `docker-compose.rust-ghcr.yml`

The disposable development stack `docker-compose.rust.yml` is excluded.
Release-mode consumers no longer need a Python wheel to obtain compose
files. Resolve each filename against the extracted bundle directory.
For Rust release activation, use the base file, the GHCR file, and the
Rust GHCR overlay in that order. Set `PSEUDOLIFE_IMAGE_TAG=<version>`;
the Rust overlay refuses an absent tag. Release updates pull the selected
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

The publication jobs are enabled only by the maintainer setting
`PSEUDOLIFE_PUBLISH_RUST_DAEMON=true` for the image and
`PSEUDOLIFE_PUBLISH_RUST_ASSETS=true` for the native asset set. Unset or
other values disable them. Build-only CI proofs neither attach release
assets nor push images. Installers and updaters must treat these assets
as unavailable until an enabled, approved release has actually published
them; this document defines their contract, not their availability.
