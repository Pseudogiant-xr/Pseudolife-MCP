# Rust workspace

This workspace holds the Rust implementation of Pseudolife-MCP. Its daemon is
what a Docker install runs; its client is built and tested but not yet what
the installers put on a user's machine. [CUTOVER.md](CUTOVER.md) records
where the move from Python stands and what is left.

| Path | What it is |
|---|---|
| `daemon/` | `pseudolife-daemon`, the HTTP service that owns a bank: the MCP tools, the Console and hook routes, the board, dreams and background duties. Published as `pseudolife-daemon-rust` and the default engine for Docker installs since 0.23.0. |
| `shim/` | The package `pseudolife-stdio`: the stdio MCP bridge to the daemon and the native `pseudolife-mcp` command. The Rust daemon image ships it as `pseudolife-mcp`; on a user's machine it is opt-in (`pseudolife-mcp runtime install`). |
| `shared/` | Modules both crates include: PostgreSQL runtime and TLS helpers, Windows file-security checks, float formatting, and the unfinished embedded-PostgreSQL (lite tier) lifecycle, which is being removed. |
| `cli_harness/`, `daemon/harness/`, `extractor_harness/` | Python harnesses that run the Rust binaries and compare them with the live Python implementation and with committed recordings (goldens). Python is frozen since 2026-11-14; once CI stops the live comparisons ([CUTOVER.md](CUTOVER.md), "Stop comparing with Python"), the goldens remain as regression snapshots. |
| `parity/`, `daemon/divergences/`, [PORT-STATE.md](PORT-STATE.md), [PORTING.md](PORTING.md), [PARITY.md](PARITY.md) | The register of what was ported and where Rust knowingly differs from Python. Since the 2026-11-14 ruling that matching Python is no longer the bar, it is a record of the port; the CI rules that still enforce it are being removed. |
| `ci/` | Scripts and tests behind `.github/workflows/rust.yml`: scope, budget, stress and timing. |

## Building

Build from this directory so `rust-toolchain.toml` selects Rust 1.94.0, after
checking the host's full-suite lease ([DEV.md](DEV.md) has the local loop):

```text
cargo build --locked --jobs 4
cargo fmt --check
cargo clippy --locked --all-targets --jobs 4 -- -D warnings
cargo nextest run --locked --jobs 4
```

`release.yml`'s native-archive job builds `x86_64-pc-windows-msvc`,
`x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu` and both macOS
targets. Each platform needs its target toolchain and linker; a Windows build
alone is no Linux evidence.

## Dependencies worth knowing

RMCP is pinned to 3.5.0 so that an SDK upgrade is a deliberate change to the
wire behaviour. Reqwest uses rustls with default features disabled, and each
client refuses redirects. JSON values use `serde_json`'s arbitrary precision
and ordered dictionary features, to preserve arbitrary integers and opaque
JSON member order. `Cargo.lock` records the resolved versions.

## Unsafe code

The crates deny unsafe Rust by default and allow it only in commented,
module-local exceptions (`#[allow(unsafe_code)]`) in the shim, the daemon and
`shared/`: Windows security descriptors, job objects and process handles,
POSIX session and exec setup, file locks, and the daemon's PDFium, BLAS and
heap-trim calls. [PORTING.md](PORTING.md), "Unsafe code and dependencies",
records the main ones and their invariants. Review any change to an
`allow(unsafe_code)` site as security-sensitive.

## The shim at runtime

With no mode, or `shim` or `channel`, the binary is the stdio bridge: it reads
the client's configuration environment and forwards daemon tool operations
over fresh MCP HTTP sessions. Remote daemon URLs never start a process. A
local daemon is started only from an explicit `PSEUDOLIFE_MCP_PYTHON`
interpreter or a `PSEUDOLIFE_MCP_SERVE_COMMAND` JSON argv; with neither, the
shim waits for a daemon and says so on stderr.

Every other mode is a `pseudolife-mcp` command. A command or shape that is not
ported prints a deferral on stderr, usually `pseudolife-stdio: mode '<mode>'
is deferred in this candidate`, and exits 1; it never runs Python.
[CUTOVER.md](CUTOVER.md) lists the ones left.
